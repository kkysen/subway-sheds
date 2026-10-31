//! Walks at either end of a journey and between stops, in seconds,
//! read from Parquet files in one directory:
//!
//! - `access.parquet`: `origin`, `stop`, `secs`, from each origin to the stops it walks to.
//! - `direct.parquet`: `origin`, `destination`, `secs`, from each origin straight to destinations.
//! - `egress.parquet`: `stop`, `destination`, `secs`, from each stop to destinations.
//! - `transfers.parquet`: `from_stop`, `to_stop`, `secs`, between stops.
//!
//! Origins and stops are strings, as their GTFS IDs; destinations and seconds are `int32`.
//! Stops the timetable doesn't serve are dropped.

use std::fs::File;
use std::path::Path;

use anyhow::{Context, Result, bail};
use arrow_array::{Array, Int32Array, RecordBatch, StringArray};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use raptor::gtfs::Secs;
use raptor::timetable::{StopIdx, Timetable};
use rustc_hash::FxHashMap as HashMap;

pub struct Walks {
    pub access: HashMap<String, Vec<(StopIdx, Secs)>>,
    pub direct: HashMap<String, Vec<(u32, Secs)>>,
    /// By stop.
    pub egress: Vec<Vec<(u32, Secs)>>,
}

impl Walks {
    /// The walks in `dir` from `origins` and to destinations `0..destinations`.
    pub fn read(
        dir: &Path,
        tt: &Timetable,
        origins: &[String],
        destinations: usize,
    ) -> Result<Self> {
        let stops = stop_indices(tt);
        let mut access: HashMap<String, Vec<(StopIdx, Secs)>> = HashMap::default();
        let mut direct: HashMap<String, Vec<(u32, Secs)>> = HashMap::default();
        let mut egress = vec![Vec::new(); tt.stops.len()];
        let wanted = |o: &str| origins.iter().any(|w| w == o);
        for batch in batches(&dir.join("access.parquet"))? {
            let (origin, stop, secs) = (
                strings(&batch, "origin")?,
                strings(&batch, "stop")?,
                ints(&batch, "secs")?,
            );
            for i in 0..batch.num_rows() {
                if let Some(&s) = stops.get(stop.value(i))
                    && wanted(origin.value(i))
                {
                    access
                        .entry(origin.value(i).to_owned())
                        .or_default()
                        .push((s, secs.value(i)));
                }
            }
        }
        for batch in batches(&dir.join("direct.parquet"))? {
            let (origin, dest, secs) = (
                strings(&batch, "origin")?,
                ints(&batch, "destination")?,
                ints(&batch, "secs")?,
            );
            for i in 0..batch.num_rows() {
                if wanted(origin.value(i)) {
                    let d = destination(dest.value(i), destinations)?;
                    direct
                        .entry(origin.value(i).to_owned())
                        .or_default()
                        .push((d, secs.value(i)));
                }
            }
        }
        for batch in batches(&dir.join("egress.parquet"))? {
            let (stop, dest, secs) = (
                strings(&batch, "stop")?,
                ints(&batch, "destination")?,
                ints(&batch, "secs")?,
            );
            for i in 0..batch.num_rows() {
                if let Some(&s) = stops.get(stop.value(i)) {
                    let d = destination(dest.value(i), destinations)?;
                    egress[s as usize].push((d, secs.value(i)));
                }
            }
        }
        Ok(Self {
            access,
            direct,
            egress,
        })
    }
}

/// A walk between stops, `(from_stop, to_stop, secs)`.
pub type Transfer = (String, String, Secs);

/// The transfers in `path`.
pub fn read_transfers(path: &Path) -> Result<Vec<Transfer>> {
    let mut transfers = Vec::new();
    for batch in batches(path)? {
        let (from, to, secs) = (
            strings(&batch, "from_stop")?,
            strings(&batch, "to_stop")?,
            ints(&batch, "secs")?,
        );
        for i in 0..batch.num_rows() {
            transfers.push((
                from.value(i).to_owned(),
                to.value(i).to_owned(),
                secs.value(i),
            ));
        }
    }
    Ok(transfers)
}

fn stop_indices(tt: &Timetable) -> HashMap<&str, StopIdx> {
    tt.stops
        .iter()
        .enumerate()
        .map(|(i, s)| (s.id.as_str(), i as StopIdx))
        .collect()
}

fn destination(d: i32, destinations: usize) -> Result<u32> {
    match usize::try_from(d) {
        Ok(d) if d < destinations => Ok(d as u32),
        _ => bail!("destination {d} outside 0..{destinations}"),
    }
}

fn batches(path: &Path) -> Result<Vec<RecordBatch>> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    ParquetRecordBatchReaderBuilder::try_new(file)?
        .build()?
        .collect::<Result<_, _>>()
        .with_context(|| format!("reading {}", path.display()))
}

fn column<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str, kind: &str) -> Result<&'a T> {
    batch
        .column_by_name(name)
        .with_context(|| format!("no column `{name}`"))?
        .as_any()
        .downcast_ref::<T>()
        .with_context(|| format!("column `{name}` isn't {kind}"))
}

fn strings<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a StringArray> {
    column(batch, name, "a string")
}

fn ints<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a Int32Array> {
    column(batch, name, "an int32")
}
