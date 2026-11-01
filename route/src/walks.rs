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
use rayon::iter::{
    IndexedParallelIterator, IntoParallelIterator, IntoParallelRefIterator, ParallelIterator,
};
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};
use streets::{Graph, Mm, Search, Split};

/// How far apart stations may be to change between them on the streets:
/// about the 1000 m R5 walked between stops, as a time, entrances included.
const TRANSFER_LIMIT: Secs = 15 * 60;

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
    /// From each station's platforms to other stations' within `TRANSFER_LIMIT`.
    near: Vec<Vec<(u32, Secs)>>,
}

/// One feed's walks, by its stops' indices.
#[derive(Debug, Default)]
pub struct Walks {
    /// From each origin to the stops it walks to.
    pub access: HashMap<String, Vec<(StopIdx, Secs)>>,
    /// `(from, to, walk)` between stops of stations in different complexes.
    pub transfers: Vec<(StopIdx, StopIdx, Secs)>,
    /// Those of them between complexes changed between free though out of the system,
    /// to every stop, not just the closest on each pattern, which may not be free.
    pub free_transfers: Vec<(StopIdx, StopIdx, Secs)>,
    /// Each stop's complex, to tell which of `transfers.txt`'s footpaths to keep.
    pub complexes: Vec<Option<String>>,
    /// Each stop's station, by its index in `StreetWalks`' stations.
    pub stations: Vec<Option<u32>>,
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
    /// and between `stations`' platforms,
    /// at `speed` millimeters a second, rounded down to whole seconds,
    /// walking from origins for at most `max_secs`.
    pub fn compute(
        graph: &Graph,
        stations: &[Station],
        origins: &[String],
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

        let near = snapped
            .par_iter()
            .enumerate()
            .map_init(
                || Search::new(graph),
                |search, (s, station)| {
                    search.run_from_vertices(&station.sources(true), mm(TRANSFER_LIMIT));
                    to_stations(search, Some(s), mm(TRANSFER_LIMIT))
                },
            )
            .collect();

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
                        return (origin.clone(), Vec::new());
                    };
                    search.run_from_vertices(&snapped[o].sources(false), walk_limit);
                    (origin.clone(), to_stations(search, None, walk_limit))
                },
            )
            .collect();
        Self {
            stations: stations.iter().map(|(id, ..)| id.clone()).collect(),
            complexes: stations.iter().map(|(_, c, _)| c.clone()).collect(),
            access: from_origins.into_iter().collect(),
            near,
        }
    }
}

impl StreetWalks {
    /// Each station's ID, by its index.
    pub fn stations(&self) -> &[String] {
        &self.stations
    }

    /// Every walk between stations of different complexes, as `(from, to, seconds)`, by their IDs.
    pub fn transfers(&self) -> Vec<(String, String, Secs)> {
        self.near
            .iter()
            .enumerate()
            .flat_map(|(s, near)| near.iter().map(move |&(t, walk)| (s, t as usize, walk)))
            .filter(|&(s, t, _)| self.complexes[s] != self.complexes[t])
            .map(|(s, t, walk)| (self.stations[s].clone(), self.stations[t].clone(), walk))
            .collect()
    }

    /// The complex of the station `id`, if it's one of these.
    pub fn complex(&self, id: &str) -> Option<&str> {
        let i = self.stations.iter().position(|s| s == id)?;
        Some(&self.complexes[i])
    }
}

/// From each station's platforms to destinations, for walking at any speed:
/// `(station, destination, seconds out of the station, meters on the streets)`,
/// through whichever entrance is quickest at `speed` millimeters a second,
/// walking at most `limit` millimeters.
pub fn exits(
    graph: &Graph,
    stations: &[Station],
    destinations: &[(f64, f64)],
    speed: Mm,
    limit: Mm,
) -> Vec<(u32, u32, u16, u16)> {
    let dest_splits: Vec<(u32, Split)> = destinations
        .iter()
        .enumerate()
        .filter_map(|(i, &(lat, lon))| Some((i as u32, graph.snap(lat, lon)?)))
        .collect();
    let entrances: Vec<(u32, Secs, Split)> = stations
        .iter()
        .enumerate()
        .flat_map(|(s, (_, _, entrances))| {
            entrances
                .iter()
                .filter_map(move |&(lat, lon, secs)| Some((s as u32, secs, graph.snap(lat, lon)?)))
        })
        .collect();
    let reached: Vec<Vec<(u32, u32, Secs, Mm)>> = entrances
        .par_iter()
        .map_init(
            || Search::new(graph),
            |search, &(s, secs, ref split)| {
                search.run(split, limit);
                dest_splits
                    .iter()
                    .filter_map(|(d, to)| {
                        let mm = search.to(to).filter(|&mm| mm <= limit)?;
                        Some((s, *d, secs, mm))
                    })
                    .collect()
            },
        )
        .collect();
    // The quickest entrance at `speed` for each station and destination.
    let mut best: HashMap<(u32, u32), (Secs, Mm)> = HashMap::default();
    let cost = |(secs, mm): (Secs, Mm)| secs as Mm * speed + mm;
    for (s, d, secs, mm) in reached.into_iter().flatten() {
        let entry = best.entry((s, d)).or_insert((secs, mm));
        if cost((secs, mm)) < cost(*entry) {
            *entry = (secs, mm);
        }
    }
    let mut exits: Vec<(u32, u32, u16, u16)> = best
        .into_iter()
        .map(|((s, d), (secs, mm))| (s, d, secs as u16, (mm / 1000) as u16))
        .collect();
    exits.sort_unstable();
    exits
}

/// The street walk out of station `s` to each of `dests`, as `exits` takes it:
/// out of whichever entrance is quickest at `speed` millimeters a second,
/// from the entrance to the destination, walking at most `limit` millimeters,
/// simplified, as `(latitude, longitude)`s in 100,000ths of a degree, or `None` if there's none.
#[allow(clippy::too_many_arguments)]
fn exit_paths(
    graph: &Graph,
    search: &mut Search,
    stations: &[Station],
    destinations: &[(f64, f64)],
    speed: Mm,
    limit: Mm,
    s: u32,
    dests: &[u32],
) -> Vec<Option<E5Points>> {
    let (_, _, entrances) = &stations[s as usize];
    let linked: Vec<(Linked, Secs)> = entrances
        .iter()
        .filter_map(|&(lat, lon, secs)| Some((((lat, lon), graph.snap(lat, lon)?, 0), secs)))
        .collect();
    let splits: Vec<Option<Split>> = dests
        .iter()
        .map(|&d| {
            let (lat, lon) = destinations[d as usize];
            graph.snap(lat, lon)
        })
        .collect();
    // Each destination's quickest entrance, as `exits` picks it, and its path.
    let mut best: Vec<Option<(Mm, E5Points)>> = vec![None; dests.len()];
    for (entrance, secs) in &linked {
        search.run(&entrance.1, limit);
        for (k, split) in splits.iter().enumerate() {
            let Some(split) = split else { continue };
            let Some(mm) = search.to(split).filter(|&mm| mm <= limit) else {
                continue;
            };
            let cost = *secs as Mm * speed + mm;
            if best[k].as_ref().is_some_and(|(c, _)| *c <= cost) {
                continue;
            }
            let Some(edges) = search.path(split) else {
                continue;
            };
            let to = destinations[dests[k] as usize];
            let points = chain(graph, std::slice::from_ref(entrance), &edges, to, split);
            let quantized: Vec<(i32, i32)> = points
                .iter()
                .map(|&(lat, lon)| ((lat * 1.0e5).round() as i32, (lon * 1.0e5).round() as i32))
                .collect();
            best[k] = Some((cost, simplify(&quantized)));
        }
    }
    best.into_iter().map(|best| Some(best?.1)).collect()
}

/// How far a point may be from the line between its neighbors to leave it out of a walk to draw,
/// less than a sidewalk is wide.
const SIMPLIFY_MM: f64 = 1000.0;

/// Millimeters north per 100,000th of a degree of latitude, on a sphere as big as the Earth.
const MM_PER_E5_DEGREE: f64 = 6_371_000_000.0 * std::f64::consts::PI / 180.0 / 1.0e5;

/// `points`, as `(latitude, longitude)`s in 100,000ths of a degree, without those within
/// `SIMPLIFY_MM` of the line from the last one kept to the next, nor repeats,
/// so that walks that start the same keep the same points until they part.
fn simplify(points: &[(i32, i32)]) -> Vec<(i32, i32)> {
    let mut kept: Vec<(i32, i32)> = Vec::with_capacity(points.len());
    for (k, &p) in points.iter().enumerate() {
        if kept.last() == Some(&p) {
            continue;
        }
        let (Some(&a), Some(&b)) = (kept.last(), points.get(k + 1)) else {
            kept.push(p);
            continue;
        };
        // Millimeters per 100,000th of a degree, north and east.
        let ky = MM_PER_E5_DEGREE;
        let kx = ky * (f64::from(a.0) / 1.0e5).to_radians().cos();
        let (ax, ay) = (f64::from(b.1 - a.1) * kx, f64::from(b.0 - a.0) * ky);
        let (px, py) = (f64::from(p.1 - a.1) * kx, f64::from(p.0 - a.0) * ky);
        let length = ax.hypot(ay);
        let off = if length > 0.0 {
            (ax * py - ay * px).abs() / length
        } else {
            px.hypot(py)
        };
        if off > SIMPLIFY_MM {
            kept.push(p);
        }
    }
    kept
}

/// A station's walks out to destinations as a tree, to draw any one of them:
/// each point's parent, `-1` for an entrance, and its latitude and longitude,
/// in 100,000ths of a degree, the near walks' points first, `near` of them,
/// and `(destination, point)` where each walk ends, near ones first.
#[derive(Debug, Default)]
pub struct ExitTree {
    pub parent: Vec<i32>,
    pub lat: Vec<i32>,
    pub lon: Vec<i32>,
    pub near: usize,
    pub ends: Vec<(u32, u32)>,
    pub near_ends: usize,
}

/// Each of `stations`' `ExitTree` of its walks out to destinations of `wanted`,
/// `(station, destination, far)`, by index, as `exit_paths` walks them,
/// with those to `far` destinations after the rest,
/// so that the near ones' points are all there is to draw them.
/// Each station's are walked and made a tree together, so that only a few stations' are kept at once.
pub fn exit_trees(
    graph: &Graph,
    stations: &[Station],
    destinations: &[(f64, f64)],
    speed: Mm,
    limit: Mm,
    wanted: &[(u32, u32, bool)],
) -> Vec<ExitTree> {
    let mut by_station: Vec<Vec<(bool, u32)>> = vec![Vec::new(); stations.len()];
    for &(s, d, far) in wanted {
        by_station[s as usize].push((far, d));
    }
    by_station
        .into_par_iter()
        .enumerate()
        .map_init(
            || Search::new(graph),
            |search, (s, mut walks)| {
                walks.sort_unstable();
                let dests: Vec<u32> = walks.iter().map(|&(_, d)| d).collect();
                let paths = exit_paths(
                    graph,
                    search,
                    stations,
                    destinations,
                    speed,
                    limit,
                    s as u32,
                    &dests,
                );
                let mut tree = ExitTree::default();
                let mut node: HashMap<(i32, i32, i32), u32> = HashMap::default();
                let mut near = true;
                for ((far, d), points) in walks.into_iter().zip(paths) {
                    let Some(points) = points else { continue };
                    if far && near {
                        near = false;
                        tree.near = tree.parent.len();
                        tree.near_ends = tree.ends.len();
                    }
                    let mut at = -1;
                    for (lat, lon) in points {
                        at = *node.entry((at, lat, lon)).or_insert_with(|| {
                            tree.parent.push(at);
                            tree.lat.push(lat);
                            tree.lon.push(lon);
                            (tree.parent.len() - 1) as u32
                        }) as i32;
                    }
                    if at >= 0 {
                        tree.ends.push((d, at as u32));
                    }
                }
                if near {
                    tree.near = tree.parent.len();
                    tree.near_ends = tree.ends.len();
                }
                tree
            },
        )
        .collect()
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
    /// only to the closest stop on each pattern,
    /// or free between `free`'s complexes, by a pair of their stations' IDs, to every stop.
    pub fn for_feed(all: &StreetWalks, tts: &[&Timetable], free: &[(String, String)]) -> Self {
        let free: HashSet<(&str, &str)> = free
            .iter()
            .filter_map(|(a, b)| Some((all.complex(a)?, all.complex(b)?)))
            .flat_map(|(a, b)| [(a, b), (b, a)])
            .collect();
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
            stations: station.clone(),
            complexes: station
                .iter()
                .map(|s| s.map(|s| all.complexes[s as usize].clone()))
                .collect(),
            ..Self::default()
        };
        for (i, &s) in station.iter().enumerate() {
            let Some(s) = s else { continue };
            let complex = &all.complexes[s as usize];
            let near: Vec<(StopIdx, Secs)> = all.near[s as usize]
                .iter()
                .filter(|&&(t, _)| &all.complexes[t as usize] != complex)
                .flat_map(|&(t, walk)| stops[t as usize].iter().map(move |&b| (b, walk)))
                .collect();
            for (to, walk) in closest_on_patterns(tts, &near) {
                walks.transfers.push((i as StopIdx, to, walk));
            }
            for &(t, walk) in &all.near[s as usize] {
                if free.contains(&(complex.as_str(), all.complexes[t as usize].as_str())) {
                    for &b in &stops[t as usize] {
                        walks.free_transfers.push((i as StopIdx, b, walk));
                    }
                }
            }
        }
        for (origin, access) in &all.access {
            let access = access
                .iter()
                .flat_map(|&(t, walk)| stops[t as usize].iter().map(move |&b| (b, walk)))
                .collect();
            walks.access.insert(origin.clone(), access);
        }
        walks
    }
}

/// A walk between stations to draw: `(from, to, points)`, by station index,
/// the points as `(latitude, longitude)`s.
pub type Path = (u32, u32, Points);

/// A walk's points, as `(latitude, longitude)`s.
pub type Points = Vec<(f64, f64)>;

/// A walk's points to draw, as `(latitude, longitude)`s in 100,000ths of a degree.
type E5Points = Vec<(i32, i32)>;

/// An entrance to walk from or to: where it is, where it links, and its time to the platforms.
type Linked = ((f64, f64), Split, Mm);

/// The points of `split`'s edge from where its point links to it to its end `v`,
/// as `(latitude, longitude)`s, starting at the linked point.
fn along_to(graph: &Graph, split: &Split, v: u32) -> Vec<(f64, f64)> {
    let geometry = graph.geometry(split.edge);
    let mut walked = 0.0;
    let mut cut = geometry.len();
    let mut linked = geometry.first().copied();
    // Where the point links, `d0` from `v0` along the edge's geometry.
    let at = f64::from(split.d0) / 1000.0;
    for (k, pair) in geometry.windows(2).enumerate() {
        let length = streets::graph::distance_m(pair[0], pair[1]);
        if walked + length >= at {
            let f = if length > 0.0 {
                (at - walked) / length
            } else {
                0.0
            };
            linked = Some((
                pair[0].0 + f * (pair[1].0 - pair[0].0),
                pair[0].1 + f * (pair[1].1 - pair[0].1),
            ));
            cut = k + 1;
            break;
        }
        walked += length;
    }
    let mut points: Vec<(f64, f64)> = linked.into_iter().collect();
    if v == split.v1 {
        points.extend_from_slice(&geometry[cut.min(geometry.len())..]);
    } else {
        points.extend(geometry[..cut.min(geometry.len())].iter().rev());
    }
    points
}

/// The street walk between each pair of stations of different complexes
/// within `TRANSFER_LIMIT` of each other, as changing trains walks it, to draw:
/// from the entrance walked out of to the one walked into,
/// at `speed` millimeters a second.
pub fn transfer_paths(graph: &Graph, stations: &[Station], speed: Mm) -> Vec<Path> {
    let mm = |s: Secs| speed.saturating_mul(s as Mm);
    let limit = mm(TRANSFER_LIMIT);
    let entrances: Vec<Vec<Linked>> = stations
        .iter()
        .map(|(_, _, entrances)| {
            entrances
                .iter()
                .filter_map(|&(lat, lon, s)| Some(((lat, lon), graph.snap(lat, lon)?, mm(s))))
                .collect()
        })
        .collect();
    entrances
        .par_iter()
        .enumerate()
        .map_init(
            || Search::new(graph),
            |search, (s, from)| {
                let sources: Vec<(u32, Mm)> = from
                    .iter()
                    .flat_map(|(_, split, cost)| {
                        split.ends().map(|(v, d)| (v, d.saturating_add(*cost)))
                    })
                    .collect();
                search.run_from_vertices(&sources, limit);
                let mut paths = Vec::new();
                for (t, to) in entrances.iter().enumerate() {
                    if t == s || stations[t].1 == stations[s].1 {
                        continue;
                    }
                    // The entrance walked into, as `Snapped::reached` picks it.
                    let Some((reached, &(point, split, _))) = to
                        .iter()
                        .filter_map(|e| Some((search.to(&e.1)?.saturating_add(e.2), e)))
                        .min_by_key(|&(reached, _)| reached)
                    else {
                        continue;
                    };
                    if reached > limit {
                        continue;
                    }
                    let Some(edges) = search.path(&split) else {
                        continue;
                    };
                    paths.push((
                        s as u32,
                        t as u32,
                        chain(graph, from, &edges, point, &split),
                    ));
                }
                paths
            },
        )
        .flatten()
        .collect()
}

/// The points of a walk along `edges` from one of `from`'s entrances to `point`, linked at `split`.
fn chain(
    graph: &Graph,
    from: &[Linked],
    edges: &[u32],
    point: (f64, f64),
    split: &Split,
) -> Vec<(f64, f64)> {
    // The vertices walked through, from the start, found from how the edges meet.
    let ends = |e: u32| {
        let edge = &graph.edges[e as usize];
        (edge.from, edge.to)
    };
    let mut vertices = Vec::with_capacity(edges.len() + 1);
    match edges {
        // Linked to edges that meet: at the end of `split`'s that one of `from`'s links to.
        [] => vertices.extend(
            [split.v0, split.v1]
                .into_iter()
                .find(|&v| from.iter().any(|(_, s, _)| s.v0 == v || s.v1 == v)),
        ),
        [only] => {
            // Its end nearer the linked point, which the walk doesn't start at.
            let (a, b) = ends(*only);
            let last = [split.v0, split.v1]
                .into_iter()
                .find(|&v| v == a || v == b)
                .unwrap_or(b);
            vertices.push(if last == a { b } else { a });
            vertices.push(last);
        }
        [first, second, ..] => {
            let (a, b) = ends(*first);
            let (c, d) = ends(*second);
            let mut v = if b == c || b == d { a } else { b };
            vertices.push(v);
            for &e in edges {
                let (a, b) = ends(e);
                v = if a == v { b } else { a };
                vertices.push(v);
            }
        }
    }
    // The entrance walked out of: the one whose link reaches the start the quickest.
    let start = vertices.first().copied();
    let out = from
        .iter()
        .filter_map(|(at, s, cost)| {
            let d = match start {
                Some(v) => s.ends().into_iter().find(|&(u, _)| u == v)?.1,
                None => s.to_edge,
            };
            Some((d.saturating_add(*cost), at, s))
        })
        .min_by_key(|&(d, ..)| d);
    let mut points = Vec::new();
    if let Some((_, &at, s)) = out {
        points.push(at);
        if let Some(v) = start {
            let mut out_along = along_to(graph, s, v);
            points.append(&mut out_along);
        }
    }
    for (k, &e) in edges.iter().enumerate() {
        let geometry = graph.geometry(e);
        if graph.edges[e as usize].from == vertices[k] {
            points.extend_from_slice(geometry);
        } else {
            points.extend(geometry.iter().rev());
        }
    }
    if let Some(&v) = vertices.last() {
        let mut in_along = along_to(graph, split, v);
        in_along.reverse();
        points.append(&mut in_along);
    }
    points.push(point);
    points.dedup();
    points
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn simplify_keeps_corners_and_drops_straight_points_and_repeats() {
        // East along a street, about 8 m a step, then a turn north.
        let points = [
            (0, 0),
            (0, 10),
            (0, 10),
            (0, 20),
            (0, 30),
            (10, 30),
            (20, 30),
        ];
        assert_eq!(simplify(&points), [(0, 0), (0, 30), (20, 30)]);
    }

    #[test]
    fn simplify_keeps_a_bend_of_more_than_a_meter() {
        // About 2 m off the line from the first to the last.
        let points = [(0, 0), (2, 10), (0, 20)];
        assert_eq!(simplify(&points), points);
        // And less than one.
        let points = [(0, 0), (0, 10), (0, 20)];
        assert_eq!(simplify(&points), [(0, 0), (0, 20)]);
    }
}
