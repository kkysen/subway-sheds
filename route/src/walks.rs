//! Walks at either end of a journey and between stops, in seconds,
//! on OpenStreetMap's streets with `mta-od-data`'s `streets` crate,
//! with R5 7.5.1's limits and transfers, as `travel_times` used to have R5 route them.

use raptor::gtfs::Secs;
use raptor::timetable::{StopIdx, Timetable};
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use rustc_hash::FxHashMap as HashMap;
use streets::{Graph, Mm, Search, Split};

/// How far R5 walks between stops (`TransitLayer.TRANSFER_DISTANCE_LIMIT_METERS`).
const TRANSFER_LIMIT: Mm = 1_000_000;

/// How far R5 walks from stops (`TransitLayer.WALK_DISTANCE_LIMIT_METERS`).
const EGRESS_LIMIT: Mm = 2_000_000;

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

/// The splits in `to` the latest search reached within `limit`, and how far.
fn reach<'a>(
    search: &Search,
    to: impl Iterator<Item = (u32, &'a Split)>,
    limit: Mm,
) -> Vec<(u32, Mm)> {
    to.filter_map(|(i, split)| search.to(split).filter(|&d| d <= limit).map(|d| (i, d)))
        .collect()
}

impl Walks {
    /// The walks from `origins`, as `(id, latitude, longitude)`, and `tt`'s stops
    /// to `destinations`, as `(latitude, longitude)`, and between stops,
    /// at `speed` millimeters a second, rounded down to whole seconds,
    /// walking from origins for at most `max_secs`.
    pub fn compute(
        graph: &Graph,
        tt: &Timetable,
        origins: &[(String, f64, f64)],
        destinations: &[(f64, f64)],
        speed: Mm,
        max_secs: Secs,
    ) -> Self {
        let secs = |d: Mm| (d / speed) as Secs;
        let stops = stop_splits(graph, tt);
        let destinations: Vec<(u32, Split)> = destinations
            .iter()
            .enumerate()
            .filter_map(|(d, &(lat, lon))| Some((d as u32, graph.snap(lat, lon)?)))
            .collect();
        let to_destinations = || destinations.iter().map(|(d, s)| (*d, s));

        let mut walks = Self {
            egress: vec![Vec::new(); tt.stops.len()],
            ..Self::default()
        };
        let from_stops: Vec<_> = stops
            .par_iter()
            .map_init(
                || Search::new(graph),
                |search, &(s, ref split)| {
                    search.run(split, TRANSFER_LIMIT);
                    let others = stops
                        .iter()
                        .filter(|(o, _)| *o != s)
                        .map(|(o, sp)| (*o, sp));
                    let transfers: Vec<(StopIdx, Secs)> =
                        closest_on_patterns(tt, &reach(search, others, TRANSFER_LIMIT))
                            .into_iter()
                            .map(|(to, d)| (to, secs(d)))
                            .collect();
                    search.run(split, EGRESS_LIMIT);
                    let egress: Vec<(u32, Secs)> = reach(search, to_destinations(), Mm::MAX)
                        .into_iter()
                        .map(|(d, mm)| (d, secs(mm)))
                        .filter(|&(_, t)| t < max_secs)
                        .collect();
                    (s, transfers, egress)
                },
            )
            .collect();
        for (s, transfers, egress) in from_stops {
            walks
                .transfers
                .extend(transfers.into_iter().map(|(to, t)| (s, to, t)));
            walks.egress[s as usize] = egress;
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
                    let stops = stops.iter().map(|(s, sp)| (*s, sp));
                    let access = reach(search, stops, walk_limit)
                        .into_iter()
                        .map(|(s, d)| (s, secs(d)))
                        .collect();
                    let direct = reach(search, to_destinations(), Mm::MAX)
                        .into_iter()
                        .map(|(d, mm)| (d, secs(mm)))
                        .collect();
                    (origin.clone(), access, direct)
                },
            )
            .collect();
        for (origin, access, direct) in from_origins {
            walks.access.insert(origin.clone(), access);
            walks.direct.insert(origin, direct);
        }
        walks
    }
}
