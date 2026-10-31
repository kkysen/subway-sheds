//! Walks at either end of a journey and between stations, in seconds,
//! on OpenStreetMap's streets with `mta-od-data`'s `streets` crate,
//! through stations' entrances, each with its own time between the street and the platforms.
//!
//! The streets are walked once, for every feed's stations (`StreetWalks`),
//! and each feed takes its own stops' walks from them (`Walks`).
//! Changing trains within a station or complex is left to `transfers.txt`;
//! only changes between them walk the streets, out through one entrance and in through another.

use raptor::gtfs::Secs;
use raptor::timetable::{StopIdx, Timetable};
use rayon::iter::{IndexedParallelIterator, IntoParallelRefIterator, ParallelIterator};
use rustc_hash::FxHashMap as HashMap;
use streets::{Graph, Mm, Search, Split};

/// How far apart stations may be to change between them on the streets:
/// about the 1000 m R5 walked between stops, as a time, entrances included.
const TRANSFER_LIMIT: Secs = 15 * 60;

/// How long a walk from the last stop may take: past what almost anyone walks from a station,
/// while keeping each stop's search to a few kilometers.
const EGRESS_LIMIT: Secs = 30 * 60;

/// A station's entrance: `(latitude, longitude, seconds between it and the platforms)`.
pub type Entrance = (f64, f64, Secs);

/// A station: `(id, complex, entrances)`.
pub type Station = (String, String, Vec<Entrance>);

/// Every walk, from origins and stations by their IDs, whatever feed has them.
pub struct StreetWalks {
    /// Each station's ID, by its index here.
    stations: Vec<String>,
    /// Each station's complex, by its index.
    complexes: Vec<String>,
    /// From each origin to the stations' platforms it walks to, by index.
    access: HashMap<String, Vec<(u32, Secs)>>,
    /// From each origin straight to destinations.
    direct: HashMap<String, Vec<(u32, Secs)>>,
    /// From each station's platforms to destinations.
    egress: Vec<Vec<(u32, Secs)>>,
    /// From each station's platforms to other stations' within `TRANSFER_LIMIT`.
    near: Vec<Vec<(u32, Secs)>>,
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
    /// `(from, to, walk)` between stops of stations in different complexes.
    pub transfers: Vec<(StopIdx, StopIdx, Secs)>,
    /// Each stop's complex, to tell which of `transfers.txt`'s footpaths to keep.
    pub complexes: Vec<Option<String>>,
}

/// A station's entrances on the streets, each with its time to the platforms as a distance.
struct Snapped {
    entrances: Vec<(Split, Mm)>,
}

impl Snapped {
    /// The vertices to search from, starting on the platforms or, with `inside` false, the street.
    fn sources(&self, inside: bool) -> Vec<(u32, Mm)> {
        self.entrances
            .iter()
            .flat_map(|(split, cost)| {
                let cost = if inside { *cost } else { 0 };
                split.ends().map(|(v, d)| (v, d.saturating_add(cost)))
            })
            .collect()
    }

    /// How far the latest search reached this station's platforms, through its entrances.
    fn reached(&self, search: &Search) -> Option<Mm> {
        self.entrances
            .iter()
            .filter_map(|(split, cost)| Some(search.to(split)?.saturating_add(*cost)))
            .min()
    }
}

impl StreetWalks {
    /// The walks from `origins`, by station ID, starting on the street at their entrances,
    /// and between `stations`' platforms and to `destinations`, as `(latitude, longitude)`,
    /// at `speed` millimeters a second, rounded down to whole seconds,
    /// walking from origins for at most `max_secs`.
    pub fn compute(
        graph: &Graph,
        stations: &[Station],
        origins: &[String],
        destinations: &[(f64, f64)],
        speed: Mm,
        max_secs: Secs,
    ) -> Self {
        let secs = |d: Mm| (d / speed) as Secs;
        let mm = |s: Secs| speed.saturating_mul(s as Mm);
        let snapped: Vec<Snapped> = stations
            .iter()
            .map(|(_, _, entrances)| Snapped {
                entrances: entrances
                    .iter()
                    .filter_map(|&(lat, lon, s)| Some((graph.snap(lat, lon)?, mm(s))))
                    .collect(),
            })
            .collect();
        let dest_splits: Vec<(u32, Split)> = destinations
            .iter()
            .enumerate()
            .filter_map(|(i, &(lat, lon))| Some((i as u32, graph.snap(lat, lon)?)))
            .collect();
        let to_destinations = |search: &Search, limit: Mm| -> Vec<(u32, Secs)> {
            dest_splits
                .iter()
                .filter_map(|(d, split)| {
                    let reached = search.to(split).filter(|&r| r <= limit)?;
                    Some((*d, secs(reached)))
                })
                .collect()
        };
        let to_stations = |search: &Search, skip: Option<usize>, limit: Mm| -> Vec<(u32, Secs)> {
            snapped
                .iter()
                .enumerate()
                .filter(|&(t, _)| Some(t) != skip)
                .filter_map(|(t, station)| {
                    let reached = station.reached(search).filter(|&r| r <= limit)?;
                    Some((t as u32, secs(reached)))
                })
                .collect()
        };

        let from_stations: Vec<_> = snapped
            .par_iter()
            .enumerate()
            .map_init(
                || Search::new(graph),
                |search, (s, station)| {
                    let sources = station.sources(true);
                    search.run_from_vertices(&sources, mm(TRANSFER_LIMIT));
                    let near = to_stations(search, Some(s), mm(TRANSFER_LIMIT));
                    search.run_from_vertices(&sources, mm(EGRESS_LIMIT));
                    let egress = to_destinations(search, mm(EGRESS_LIMIT));
                    (near, egress)
                },
            )
            .collect();
        let (near, egress) = from_stations.into_iter().unzip();

        let index: HashMap<&str, usize> = stations
            .iter()
            .enumerate()
            .map(|(i, (id, ..))| (id.as_str(), i))
            .collect();
        let walk_limit = mm(max_secs);
        let from_origins: Vec<_> = origins
            .par_iter()
            .map_init(
                || Search::new(graph),
                |search, origin| {
                    let Some(&o) = index.get(origin.as_str()) else {
                        return (origin.clone(), Vec::new(), Vec::new());
                    };
                    search.run_from_vertices(&snapped[o].sources(false), walk_limit);
                    let access = to_stations(search, None, walk_limit);
                    let direct = to_destinations(search, walk_limit);
                    (origin.clone(), access, direct)
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
            stations: stations.iter().map(|(id, ..)| id.clone()).collect(),
            complexes: stations.iter().map(|(_, c, _)| c.clone()).collect(),
            access,
            direct,
            egress,
            near,
        }
    }
}

/// Of `reached` stops, only the closest on each of `tts`' patterns, as R5 keeps
/// (`TransferFinder.retainClosestStopsOnPatterns`), the first stop of any tie:
/// walking on to another of a pattern's stops is never quicker than riding to it.
fn closest_on_patterns(tts: &[&Timetable], reached: &[(StopIdx, Secs)]) -> Vec<(StopIdx, Secs)> {
    let at: HashMap<StopIdx, Secs> = reached.iter().copied().collect();
    let mut kept: Vec<(StopIdx, Secs)> = tts
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
    /// which number their stops alike, each stop walking as its station does,
    /// and changing between stations of different complexes
    /// only to the closest stop on each pattern.
    pub fn for_feed(all: &StreetWalks, tts: &[&Timetable]) -> Self {
        let tt = tts[0];
        let index: HashMap<&str, u32> = all
            .stations
            .iter()
            .enumerate()
            .map(|(i, s)| (s.as_str(), i as u32))
            .collect();
        // Each stop's station in `all`, if it has it.
        let station: Vec<Option<u32>> = tt
            .stops
            .iter()
            .map(|s| index.get(s.station.as_str()).copied())
            .collect();
        // Each of `all`'s stations' stops.
        let mut stops: Vec<Vec<StopIdx>> = vec![Vec::new(); all.stations.len()];
        for (i, s) in station.iter().enumerate() {
            if let Some(s) = s {
                stops[*s as usize].push(i as StopIdx);
            }
        }
        let mut walks = Self {
            egress: vec![Vec::new(); tt.stops.len()],
            complexes: station
                .iter()
                .map(|s| s.map(|s| all.complexes[s as usize].clone()))
                .collect(),
            ..Self::default()
        };
        for (i, &s) in station.iter().enumerate() {
            let Some(s) = s else { continue };
            walks.egress[i] = all.egress[s as usize].clone();
            let complex = &all.complexes[s as usize];
            let near: Vec<(StopIdx, Secs)> = all.near[s as usize]
                .iter()
                .filter(|&&(t, _)| &all.complexes[t as usize] != complex)
                .flat_map(|&(t, walk)| stops[t as usize].iter().map(move |&b| (b, walk)))
                .collect();
            for (to, walk) in closest_on_patterns(tts, &near) {
                walks.transfers.push((i as StopIdx, to, walk));
            }
        }
        for (origin, access) in &all.access {
            let access = access
                .iter()
                .flat_map(|&(t, walk)| stops[t as usize].iter().map(move |&b| (b, walk)))
                .collect();
            walks.access.insert(origin.clone(), access);
        }
        walks.direct = all.direct.clone();
        walks
    }
}
