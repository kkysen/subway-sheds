//! Walks at either end of a journey and between stops, in seconds,
//! on OpenStreetMap's streets with `mta-od-data`'s `streets` crate,
//! with R5 7.5.1's limits and transfers, as `travel_times` used to have R5 route them.
//!
//! The streets are walked once, for every feed's stops (`StreetWalks`),
//! and each feed takes its own stops' walks from them (`Walks`).

use raptor::gtfs::Secs;
use raptor::timetable::{StopIdx, Timetable};
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use rustc_hash::FxHashMap as HashMap;
use streets::{Graph, Mm, Search, Split};

/// How far R5 walks between stops (`TransitLayer.TRANSFER_DISTANCE_LIMIT_METERS`).
const TRANSFER_LIMIT: Mm = 1_000_000;

/// How far R5 walks from stops (`TransitLayer.WALK_DISTANCE_LIMIT_METERS`).
const EGRESS_LIMIT: Mm = 2_000_000;

/// Every walk, from origins and stops by their IDs, whatever feed has them.
pub struct StreetWalks {
    /// Each stop's ID, by its index here.
    stops: Vec<String>,
    /// From each origin to the stops it walks to, by index.
    access: HashMap<String, Vec<(u32, Secs)>>,
    /// From each origin straight to destinations.
    direct: HashMap<String, Vec<(u32, Secs)>>,
    /// From each stop to destinations.
    egress: Vec<Vec<(u32, Secs)>>,
    /// From each stop to the others within `TRANSFER_LIMIT`, before any are dropped.
    near: Vec<Vec<(u32, Mm)>>,
    /// Millimeters a second.
    speed: Mm,
}

/// One feed's walks, by its stops' indices.
#[derive(Debug, Default)]
pub struct Walks {
    /// From each origin to the stops it walks to.
    pub access: HashMap<String, Vec<(StopIdx, Secs)>>,
    /// From each origin straight to destinations.
    pub direct: HashMap<String, Vec<(u32, Secs)>>,
    /// From each stop to destinations, by stop.
    pub egress: Vec<Vec<(u32, Secs)>>,
    /// `(from, to, walk)` between stops.
    pub transfers: Vec<(StopIdx, StopIdx, Secs)>,
}

/// The splits in `to` the latest search reached within `limit`, and how far.
fn reach<'a>(
    search: &Search,
    to: impl Iterator<Item = (u32, &'a Split)>,
    limit: Mm,
) -> Vec<(u32, Mm)> {
    to.filter_map(|(i, split)| search.to(split).filter(|&d| d <= limit).map(|d| (i, d)))
        .collect()
}

impl StreetWalks {
    /// The walks from `origins` and `stops`, each as `(id, latitude, longitude)`,
    /// to `destinations`, as `(latitude, longitude)`, and between stops,
    /// at `speed` millimeters a second, rounded down to whole seconds,
    /// walking from origins for at most `max_secs`.
    pub fn compute(
        graph: &Graph,
        stops: &[(String, f64, f64)],
        origins: &[(String, f64, f64)],
        destinations: &[(f64, f64)],
        speed: Mm,
        max_secs: Secs,
    ) -> Self {
        let secs = |d: Mm| (d / speed) as Secs;
        let snapped = |points: &mut dyn Iterator<Item = (f64, f64)>| -> Vec<(u32, Split)> {
            points
                .enumerate()
                .filter_map(|(i, (lat, lon))| Some((i as u32, graph.snap(lat, lon)?)))
                .collect()
        };
        let stop_splits = snapped(&mut stops.iter().map(|&(_, lat, lon)| (lat, lon)));
        let dest_splits = snapped(&mut destinations.iter().copied());
        let to_stops = || stop_splits.iter().map(|(s, sp)| (*s, sp));
        let to_destinations = || dest_splits.iter().map(|(d, sp)| (*d, sp));

        let mut near = vec![Vec::new(); stops.len()];
        let mut egress = vec![Vec::new(); stops.len()];
        let from_stops: Vec<_> = stop_splits
            .par_iter()
            .map_init(
                || Search::new(graph),
                |search, &(s, ref split)| {
                    search.run(split, TRANSFER_LIMIT);
                    let others = to_stops().filter(|&(o, _)| o != s);
                    let near = reach(search, others, TRANSFER_LIMIT);
                    search.run(split, EGRESS_LIMIT);
                    let egress: Vec<(u32, Secs)> = reach(search, to_destinations(), Mm::MAX)
                        .into_iter()
                        .map(|(d, mm)| (d, secs(mm)))
                        .filter(|&(_, t)| t < max_secs)
                        .collect();
                    (s, near, egress)
                },
            )
            .collect();
        for (s, n, e) in from_stops {
            near[s as usize] = n;
            egress[s as usize] = e;
        }

        let walk_limit = speed.saturating_mul(max_secs as Mm);
        let from_origins: Vec<_> = origins
            .par_iter()
            .map_init(
                || Search::new(graph),
                |search, (origin, lat, lon)| {
                    let Some(split) = graph.snap(*lat, *lon) else {
                        return (origin.clone(), Vec::new(), Vec::new());
                    };
                    search.run(&split, walk_limit);
                    let to_secs = |walks: Vec<(u32, Mm)>| -> Vec<(u32, Secs)> {
                        walks.into_iter().map(|(i, d)| (i, secs(d))).collect()
                    };
                    (
                        origin.clone(),
                        to_secs(reach(search, to_stops(), walk_limit)),
                        to_secs(reach(search, to_destinations(), Mm::MAX)),
                    )
                },
            )
            .collect();
        let mut access = HashMap::default();
        let mut direct = HashMap::default();
        for (origin, a, d) in from_origins {
            access.insert(origin.clone(), a);
            direct.insert(origin, d);
        }
        Self {
            stops: stops.iter().map(|(id, ..)| id.clone()).collect(),
            access,
            direct,
            egress,
            near,
            speed,
        }
    }
}

/// Of `reached` stops, only the closest on each of `tts`' patterns, as R5 keeps
/// (`TransferFinder.retainClosestStopsOnPatterns`), the first stop of any tie.
fn closest_on_patterns(tts: &[&Timetable], reached: &[(StopIdx, Mm)]) -> Vec<(StopIdx, Mm)> {
    let at: HashMap<StopIdx, Mm> = reached.iter().copied().collect();
    let mut kept: Vec<(StopIdx, Mm)> = tts
        .iter()
        .flat_map(|tt| &tt.patterns)
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

impl Walks {
    /// `all`'s walks for `tts`' stops, timetables of one feed on different dates,
    /// which number their stops alike,
    /// walking between stops only to the closest on each of their patterns, as R5 does.
    pub fn for_feed(all: &StreetWalks, tts: &[&Timetable]) -> Self {
        let tt = tts[0];
        let index: HashMap<&str, StopIdx> = tt
            .stops
            .iter()
            .enumerate()
            .map(|(i, s)| (s.id.as_str(), i as StopIdx))
            .collect();
        // This feed's stop for each of `all`'s, if it has it.
        let mine: Vec<Option<StopIdx>> = all
            .stops
            .iter()
            .map(|id| index.get(id.as_str()).copied())
            .collect();
        let mut walks = Self {
            egress: vec![Vec::new(); tt.stops.len()],
            ..Self::default()
        };
        for (s, &m) in mine.iter().enumerate() {
            let Some(m) = m else { continue };
            walks.egress[m as usize] = all.egress[s].clone();
            let near: Vec<(StopIdx, Mm)> = all.near[s]
                .iter()
                .filter_map(|&(o, d)| Some((mine[o as usize]?, d)))
                .collect();
            for (to, d) in closest_on_patterns(tts, &near) {
                walks.transfers.push((m, to, (d / all.speed) as Secs));
            }
        }
        for (origin, access) in &all.access {
            let access = access
                .iter()
                .filter_map(|&(s, t)| Some((mine[s as usize]?, t)))
                .collect();
            walks.access.insert(origin.clone(), access);
        }
        walks.direct = all.direct.clone();
        walks
    }
}
