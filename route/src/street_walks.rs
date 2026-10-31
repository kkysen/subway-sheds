//! The walks `walks.rs` reads, computed on OpenStreetMap's streets with the `streets` crate,
//! as R5 7.5.1 computes them, rather than read from R5.

use std::fs::File;
use std::path::Path;
use std::sync::Arc;

use anyhow::Result;
use arrow_array::{ArrayRef, Int32Array, RecordBatch, StringArray};
use arrow_schema::{Field, Schema};
use parquet::arrow::ArrowWriter;
use raptor::gtfs::Secs;
use raptor::timetable::{StopIdx, Timetable};
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use rustc_hash::FxHashMap as HashMap;
use streets::{Graph, Mm, Search, Split};

/// How far R5 walks between stops (`TransitLayer.TRANSFER_DISTANCE_LIMIT_METERS`).
const TRANSFER_LIMIT: Mm = 1_000_000;

/// How far R5 walks from stops (`TransitLayer.WALK_DISTANCE_LIMIT_METERS`).
const EGRESS_LIMIT: Mm = 2_000_000;

/// The walks, in seconds, keyed as `walks.rs` reads them.
#[derive(Default)]
pub struct Tables {
    /// `(origin, stop, secs)`.
    pub access: Vec<(String, String, Secs)>,
    /// `(origin, destination, secs)`.
    pub direct: Vec<(String, u32, Secs)>,
    /// `(stop, destination, secs)`.
    pub egress: Vec<(String, u32, Secs)>,
    /// `(from_stop, to_stop, secs)`.
    pub transfers: Vec<(String, String, Secs)>,
}

/// Where each of `tt`'s stops with coordinates links to the streets.
fn stop_splits(graph: &Graph, tt: &Timetable) -> Vec<(StopIdx, Split)> {
    tt.stops
        .iter()
        .enumerate()
        .filter_map(|(s, stop)| {
            let (lat, lon) = stop.coords?;
            Some((s as StopIdx, graph.snap(lat, lon)?))
        })
        .collect()
}

/// Of `reached` stops, only the closest on each pattern, as R5 keeps
/// (`TransferFinder.retainClosestStopsOnPatterns`), the first stop of any tie.
fn closest_on_patterns(tt: &Timetable, reached: &[(StopIdx, Mm)]) -> Vec<(StopIdx, Mm)> {
    let at: HashMap<StopIdx, Mm> = reached.iter().copied().collect();
    let mut kept: Vec<(StopIdx, Mm)> = tt
        .patterns
        .iter()
        .filter_map(|p| {
            p.stops
                .iter()
                .filter_map(|s| at.get(s).map(|&d| (d, *s)))
                .min()
                .map(|(d, s)| (s, d))
        })
        .collect();
    kept.sort_unstable();
    kept.dedup();
    kept
}

/// The walks from `origins` and stops to `destinations` and between stops,
/// at `speed` millimeters a second, rounded down to whole seconds,
/// walking from origins for at most `max_secs`.
pub fn compute(
    graph: &Graph,
    tt: &Timetable,
    origins: &[(String, f64, f64)],
    destinations: &[(f64, f64)],
    speed: Mm,
    max_secs: Secs,
) -> Tables {
    let secs = |d: Mm| (d / speed) as Secs;
    let stops = stop_splits(graph, tt);
    let destinations: Vec<Option<Split>> = destinations
        .iter()
        .map(|&(lat, lon)| graph.snap(lat, lon))
        .collect();
    let id = |s: StopIdx| tt.stops[s as usize].id.clone();
    let reach = |search: &Search, splits: &mut dyn Iterator<Item = (u32, &Split)>, limit: Mm| {
        splits
            .filter_map(|(i, split)| search.to(split).filter(|&d| d <= limit).map(|d| (i, d)))
            .collect::<Vec<_>>()
    };
    let dest_splits = || {
        destinations
            .iter()
            .enumerate()
            .filter_map(|(d, s)| s.as_ref().map(|s| (d as u32, s)))
    };

    let from_stops: Vec<Tables> = stops
        .par_iter()
        .map_init(
            || Search::new(graph),
            |search, &(s, ref split)| {
                let mut t = Tables::default();
                search.run(split, TRANSFER_LIMIT);
                let others = &mut stops
                    .iter()
                    .filter(|&&(o, _)| o != s)
                    .map(|(o, sp)| (*o, sp));
                for (to, d) in closest_on_patterns(tt, &reach(search, others, TRANSFER_LIMIT)) {
                    t.transfers.push((id(s), id(to), secs(d)));
                }
                search.run(split, EGRESS_LIMIT);
                for (d, mm) in reach(search, &mut dest_splits(), Mm::MAX) {
                    if secs(mm) < max_secs {
                        t.egress.push((id(s), d, secs(mm)));
                    }
                }
                t
            },
        )
        .collect();

    let walk_limit = speed.saturating_mul(max_secs as Mm);
    let from_origins: Vec<Tables> = origins
        .par_iter()
        .map_init(
            || Search::new(graph),
            |search, (origin, lat, lon)| {
                let mut t = Tables::default();
                let Some(split) = graph.snap(*lat, *lon) else {
                    return t;
                };
                search.run(&split, walk_limit);
                let stops = &mut stops.iter().map(|(s, sp)| (*s, sp));
                for (s, d) in reach(search, stops, walk_limit) {
                    t.access.push((origin.clone(), id(s), secs(d)));
                }
                for (d, mm) in reach(search, &mut dest_splits(), Mm::MAX) {
                    t.direct.push((origin.clone(), d, secs(mm)));
                }
                t
            },
        )
        .collect();

    let mut all = Tables::default();
    for t in from_stops.into_iter().chain(from_origins) {
        all.access.extend(t.access);
        all.direct.extend(t.direct);
        all.egress.extend(t.egress);
        all.transfers.extend(t.transfers);
    }
    all
}

fn write(path: &Path, columns: Vec<(&str, ArrayRef)>) -> Result<()> {
    let schema = Arc::new(Schema::new(
        columns
            .iter()
            .map(|(name, a)| Field::new(*name, a.data_type().clone(), false))
            .collect::<Vec<_>>(),
    ));
    let batch = RecordBatch::try_new(
        schema.clone(),
        columns.into_iter().map(|(_, a)| a).collect(),
    )?;
    let mut writer = ArrowWriter::try_new(File::create(path)?, schema, None)?;
    writer.write(&batch)?;
    writer.close()?;
    Ok(())
}

fn strings<'a>(values: impl Iterator<Item = &'a String>) -> ArrayRef {
    Arc::new(StringArray::from_iter_values(values))
}

fn ints(values: impl Iterator<Item = i32>) -> ArrayRef {
    Arc::new(Int32Array::from_iter_values(values))
}

impl Tables {
    /// Writes the tables to `dir` as `walks.rs` reads them.
    pub fn write(&self, dir: &Path) -> Result<()> {
        std::fs::create_dir_all(dir)?;
        write(
            &dir.join("access.parquet"),
            vec![
                ("origin", strings(self.access.iter().map(|r| &r.0))),
                ("stop", strings(self.access.iter().map(|r| &r.1))),
                ("secs", ints(self.access.iter().map(|r| r.2))),
            ],
        )?;
        write(
            &dir.join("direct.parquet"),
            vec![
                ("origin", strings(self.direct.iter().map(|r| &r.0))),
                ("destination", ints(self.direct.iter().map(|r| r.1 as i32))),
                ("secs", ints(self.direct.iter().map(|r| r.2))),
            ],
        )?;
        write(
            &dir.join("egress.parquet"),
            vec![
                ("stop", strings(self.egress.iter().map(|r| &r.0))),
                ("destination", ints(self.egress.iter().map(|r| r.1 as i32))),
                ("secs", ints(self.egress.iter().map(|r| r.2))),
            ],
        )?;
        write(
            &dir.join("transfers.parquet"),
            vec![
                ("from_stop", strings(self.transfers.iter().map(|r| &r.0))),
                ("to_stop", strings(self.transfers.iter().map(|r| &r.1))),
                ("secs", ints(self.transfers.iter().map(|r| r.2))),
            ],
        )
    }
}
