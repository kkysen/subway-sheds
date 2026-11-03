//! Travel times from subway stations to places around the city,
//! routed with `mta-od-data`'s RAPTOR between stops,
//! walking at either end and between stations on OpenStreetMap's streets
//! with `mta-od-data`'s `streets`, through stations' entrances (see `walks.rs`).
//! Python drives it through the `subway_sheds._route` module.

mod feed;
mod sat;
mod walks;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Result, bail};
use arrow_array::{
    ArrayRef, Float64Array, RecordBatch, StringArray, UInt8Array, UInt16Array, UInt32Array,
};
use arrow_schema::{Field, Schema};
use jiff::civil::Date;
use parquet::arrow::ArrowWriter;
use pyo3::types::{PyBytes, PyModule, PyModuleMethods};
use pyo3::{Bound, PyResult, Python, pyfunction, pymodule, wrap_pyfunction};
use raptor::assign::{DEFAULT_STACKED_SECS_PER_LEVEL, DEFAULT_UP_AND_OVER_SECS};
use raptor::gtfs::{Feed, Secs, parse_time};
use raptor::platforms::Platforms;
use raptor::raptor::{Labels, Leg, Router};
use raptor::timetable::{StopIdx, Timetable};
use rayon::iter::{IntoParallelIterator, IntoParallelRefIterator, ParallelIterator};
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};
use streets::osm::Osm;
use streets::{Graph, Mm};

use crate::walks::{Station, StreetWalks, Walks};

/// What `travel_times` routes on and how.
pub struct Request<'a> {
    pub tt: &'a Timetable,
    pub walks: &'a Walks,
    /// The first departure of the window.
    pub depart: Secs,
    /// Departures, one a minute from `depart`.
    pub minutes: u32,
    pub max_rides: usize,
    /// The longest journey kept.
    pub max_secs: Secs,
    /// How many stations `StreetWalks` has.
    pub stations: usize,
}

/// Each origin's ride to each station's platforms each minute, before walking out
/// (see `Arrivals`), and the journeys behind them (see `Routes`).
pub fn travel_times(req: &Request, origins: &[String]) -> Vec<OriginTimes> {
    let router = Router::with_max_rides(req.tt, req.max_rides);
    // Each stop's departures, from every pattern leaving it, sorted.
    let mut departures: Vec<Vec<Secs>> = vec![Vec::new(); req.tt.stops.len()];
    for p in &req.tt.patterns {
        for (pos, &stop) in p
            .stops
            .iter()
            .enumerate()
            .take(p.stops.len().saturating_sub(1))
        {
            departures[stop as usize].extend(p.trips.iter().map(|t| t.times[pos].1));
        }
    }
    for d in &mut departures {
        d.sort_unstable();
    }
    origins
        .par_iter()
        .map(|origin| origin_times(req, &router, &departures, origin))
        .collect()
}

/// An origin's arrival on each station's platforms each minute of the window,
/// by train or, if sooner, walking there,
/// within `max_secs`, as `(station, minute, seconds from departing)`,
/// and at its own station, when it's on the first train it can board there,
/// by the station's index in `StreetWalks`:
/// what the page adds walks out of stations to, at whatever speed its reader walks,
/// before taking each destination's median over the minutes.
pub type Arrivals = Vec<(u32, u8, u16)>;

/// A leg of a journey: a ride on `pattern` from its stop at `board` to its stop at `alight`,
/// or with `pattern` `WALK`, a walk between stations, by their indices in `StreetWalks`,
/// from `start` to `end`, in seconds from the window's start.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RouteLeg {
    pub pattern: u32,
    pub board: u16,
    pub alight: u16,
    pub from: u32,
    pub to: u32,
    pub start: u16,
    pub end: u16,
}

/// `RouteLeg::pattern` for a walk.
pub const WALK: u32 = u32::MAX;

/// An origin's journeys to each station it reaches by train, as `(station, minute, legs)`,
/// each read back departing at `minute` when the station's arrival differs from departing the minute after,
/// and the journey departing any minute until the one before it read back, ascending:
/// so that the page can show the one behind any trip it times, a station's or a cell's.
pub type Routes = Vec<(u32, u8, Vec<RouteLeg>)>;

/// What `origin_times` gives for an origin.
pub type OriginTimes = (Arrivals, Routes);

fn origin_times(
    req: &Request,
    router: &Router,
    departures: &[Vec<Secs>],
    origin: &str,
) -> OriginTimes {
    let walks = req.walks;
    let access = walks.access.get(origin).map_or(&[][..], Vec::as_slice);
    // Each station's arrival each minute, minute-major.
    let ns = req.stations;
    let mut at_stations = vec![Secs::MAX; req.minutes as usize * ns];
    // Each station's arrival the minute after, when its journey was last read back.
    let mut later = vec![Secs::MAX; ns];
    // Each station's journeys, as `(minute, legs)`, latest first:
    // read back only when its arrival changes, the same journey until it does.
    let mut journeys: Vec<Vec<(u8, Vec<RouteLeg>)>> = vec![Vec::new(); ns];
    // How each station was reached this minute: the stop a train reached,
    // and the stop of the station walked to from it, if it was walked to,
    // or `None` if walked to from the origin.
    let mut via: Vec<Option<(StopIdx, Option<StopIdx>)>> = vec![None; ns];
    // Walking there from the origin, on the streets, the same every minute.
    let mut on_foot = vec![Secs::MAX; ns];
    for &(stop, walk) in access {
        if let Some(station) = walks.stations[stop as usize] {
            let at = &mut on_foot[station as usize];
            *at = (*at).min(walk);
        }
    }
    let mut labels = router.labels();
    for m in (0..req.minutes).rev() {
        let depart = req.depart + 60 * m as Secs;
        labels.run_access(access, depart);
        at_stations[m as usize * ns..][..ns].copy_from_slice(&on_foot);
        via.fill(None);
        for (s, arrive) in labels.arrivals().enumerate() {
            if arrive == Secs::MAX {
                continue;
            }
            let ride = arrive - depart;
            let row = &mut at_stations[m as usize * ns..][..ns];
            if let Some(station) = walks.stations[s] {
                let at = &mut row[station as usize];
                if ride < *at {
                    *at = ride;
                    via[station as usize] = Some((s as StopIdx, None));
                }
            }
            // On the platforms of a station walked to from the train,
            // as across a complex, which the walk out of the station doesn't count.
            for &(to, walk) in &req.tt.footpaths[s] {
                if let Some(station) = walks.stations[to as usize] {
                    let at = &mut row[station as usize];
                    if ride + walk < *at {
                        *at = ride + walk;
                        via[station as usize] = Some((s as StopIdx, Some(to)));
                    }
                }
            }
        }
        for station in 0..ns {
            let ride = at_stations[m as usize * ns + station];
            let arrive = if ride == Secs::MAX {
                ride
            } else {
                ride + depart
            };
            if arrive != Secs::MAX && arrive != later[station] {
                // No rides if walked to from the origin.
                let (mut legs, walked) = match via[station] {
                    Some((stop, walked)) => (
                        fastest_journey(req, &labels, &[stop]),
                        walked.map(|to| (stop, to)),
                    ),
                    None => (Vec::new(), None),
                };
                if let Some((stop, to)) = walked {
                    let of = |s: StopIdx| walks.stations[s as usize].unwrap_or(u32::MAX);
                    if of(stop) != of(to) {
                        let start = legs.last().map_or(0, |leg| leg.end);
                        legs.push(RouteLeg {
                            pattern: WALK,
                            board: 0,
                            alight: 0,
                            from: of(stop),
                            to: of(to),
                            start,
                            end: start.saturating_add(footpath(req.tt, stop, to) as u16),
                        });
                    }
                }
                journeys[station].push((m as u8, legs));
            }
            later[station] = arrive;
        }
    }
    // On the first train from the origin's own platforms, after going in to them.
    let own: Vec<(StopIdx, Secs)> = access
        .iter()
        .copied()
        .filter(|&(s, _)| req.tt.stops[s as usize].station == origin)
        .collect();
    if let Some(&(first, _)) = own.first()
        && let Some(station) = walks.stations[first as usize]
    {
        for m in 0..req.minutes as usize {
            let depart = req.depart + 60 * m as Secs;
            let board = own
                .iter()
                .filter_map(|&(s, walk)| {
                    let times = &departures[s as usize];
                    times
                        .get(times.partition_point(|&t| t < depart + walk))
                        .copied()
                })
                .min();
            at_stations[m * ns + station as usize] = board.map_or(Secs::MAX, |b| b - depart);
        }
    }
    let arrivals = (0..ns)
        .flat_map(|s| (0..req.minutes as usize).map(move |m| (s, m)))
        .filter_map(|(s, m)| {
            let t = at_stations[m * ns + s];
            (t <= req.max_secs).then_some((s as u32, m as u8, t as u16))
        })
        .collect();
    // Each station's journeys within `max_secs`, earliest departure first.
    let at_stations = &at_stations;
    let routes = journeys
        .into_iter()
        .enumerate()
        .flat_map(|(station, journeys)| {
            journeys.into_iter().rev().filter_map(move |(m, legs)| {
                (at_stations[usize::from(m) * ns + station] <= req.max_secs).then_some((
                    station as u32,
                    m,
                    legs,
                ))
            })
        })
        .collect();
    (arrivals, routes)
}

/// The seconds `tt`'s footpath from `from` to `to` takes, or 0 if there's none.
fn footpath(tt: &Timetable, from: StopIdx, to: StopIdx) -> Secs {
    tt.footpaths[from as usize]
        .iter()
        .find(|&&(t, _)| t == to)
        .map_or(0, |&(_, walk)| walk)
}

/// The legs of the earliest arriving journey `labels` has to any of `targets`.
fn fastest_journey(req: &Request, labels: &Labels, targets: &[StopIdx]) -> Vec<RouteLeg> {
    let (legs, journeys) = labels.journeys(targets);
    let Some(journey) = journeys.iter().min_by_key(|j| j.arrive) else {
        return Vec::new();
    };
    let station = |stop: StopIdx| req.walks.stations[stop as usize].unwrap_or(u32::MAX);
    // From the window's start, as the arrivals are.
    let since = |t: Secs| t.saturating_sub(req.depart).min(u16::MAX.into()) as u16;
    let mut at = 0;
    legs.of(journey)
        .iter()
        .map(|leg| {
            let leg = match leg {
                Leg::Ride(ride) => RouteLeg {
                    pattern: ride.pattern,
                    board: ride.board_pos,
                    alight: ride.alight_pos,
                    from: station(ride.board_stop(req.tt)),
                    to: station(ride.alight_stop(req.tt)),
                    start: since(ride.depart(req.tt)),
                    end: since(ride.arrive(req.tt)),
                },
                // Walked as soon as off the train before.
                Leg::Walk { from, to, duration } => RouteLeg {
                    pattern: WALK,
                    board: 0,
                    alight: 0,
                    from: station(*from),
                    to: station(*to),
                    start: at,
                    end: at.saturating_add(*duration as u16),
                },
            };
            at = leg.end;
            leg
        })
        .collect()
}

/// `tt`'s changes within a complex timed by OSM's platforms (`Timetable::use_osm_transfers`):
/// free on one platform, up and over to another of a station's, stacked a flight a level,
/// and to another station of the complex a walk by the distance,
/// with `islands`' stops, which `platforms` doesn't know, all on one island platform.
/// Each copy of a stop split by platform keeps its station.
fn use_platforms(
    tt: &mut Timetable,
    platforms: &Platforms,
    islands: &HashSet<String>,
    all: &StreetWalks,
) {
    tt.split_platforms(platforms);
    for stop in &mut tt.stops {
        if stop.platform.is_none() && islands.contains(&stop.station) {
            stop.platform = Some(format!("island {}", stop.station));
        }
    }
    let mut complexes: HashMap<&str, Vec<StopIdx>> = HashMap::default();
    for (i, stop) in tt.stops.iter().enumerate() {
        if let Some(complex) = all.complex(&stop.station) {
            complexes.entry(complex).or_default().push(i as StopIdx);
        }
    }
    let complexes: Vec<Vec<StopIdx>> = complexes.into_values().collect();
    tt.use_osm_transfers(
        complexes.iter().map(Vec::as_slice),
        platforms,
        DEFAULT_UP_AND_OVER_SECS,
        DEFAULT_STACKED_SECS_PER_LEVEL,
    );
}

/// `tt`, changing trains within a complex as `use_platforms` times it,
/// and between complexes by `walks`' transfers on the streets,
/// which go out of the system and, but for its free ones, pay another fare to go back in,
/// so only if `pay_again`.
fn change_trains(tt: &mut Timetable, walks: &Walks, pay_again: bool) {
    for (from, paths) in tt.footpaths.iter_mut().enumerate() {
        let complex = &walks.complexes[from];
        paths.retain(|&(to, _)| complex.is_some() && walks.complexes[to as usize] == *complex);
    }
    let paid = if pay_again { &walks.transfers[..] } else { &[] };
    for &(from, to, walk) in paid.iter().chain(&walks.free_transfers) {
        tt.footpaths[from as usize].push((to, walk));
    }
}

/// Writes `columns` to `out` as Parquet.
fn write_parquet(out: &Path, columns: Vec<(&str, ArrayRef)>) -> Result<()> {
    let schema = Arc::new(Schema::new(
        columns
            .iter()
            .map(|(name, array)| Field::new(*name, array.data_type().clone(), false))
            .collect::<Vec<_>>(),
    ));
    let batch = RecordBatch::try_new(
        schema.clone(),
        columns.into_iter().map(|(_, a)| a).collect(),
    )?;
    let mut writer = ArrowWriter::try_new(std::fs::File::create(out)?, schema, None)?;
    writer.write(&batch)?;
    writer.close()?;
    Ok(())
}

/// Each origin repeated once for each of its rows.
fn origin_column<T>(origins: &[String], rows: &[Vec<T>]) -> ArrayRef {
    Arc::new(StringArray::from_iter_values(
        origins
            .iter()
            .zip(rows)
            .flat_map(|(o, r)| std::iter::repeat_n(o.as_str(), r.len())),
    ))
}

/// Writes each origin's arrivals as `origin`, `station`, `minute`, and `seconds`.
fn write_arrivals(
    out: &Path,
    origins: &[String],
    stations: &[String],
    arrivals: &[Arrivals],
) -> Result<()> {
    let flat = || arrivals.iter().flatten();
    write_parquet(
        out,
        vec![
            ("origin", origin_column(origins, arrivals)),
            (
                "station",
                Arc::new(StringArray::from_iter_values(
                    flat().map(|&(s, ..)| stations[s as usize].as_str()),
                )),
            ),
            (
                "minute",
                Arc::new(UInt8Array::from_iter_values(flat().map(|&(_, m, _)| m))),
            ),
            (
                "seconds",
                Arc::new(UInt16Array::from_iter_values(flat().map(|&(.., t)| t))),
            ),
        ],
    )
}

/// Writes `walks::exits` as `station`, `destination`, `exit_seconds`, and `meters`.
fn write_exits(out: &Path, stations: &[String], exits: &[(u32, u32, u16, u16)]) -> Result<()> {
    write_parquet(
        out,
        vec![
            (
                "station",
                Arc::new(StringArray::from_iter_values(
                    exits.iter().map(|e| stations[e.0 as usize].as_str()),
                )),
            ),
            (
                "destination",
                Arc::new(UInt32Array::from_iter_values(exits.iter().map(|e| e.1))),
            ),
            (
                "exit_seconds",
                Arc::new(UInt16Array::from_iter_values(exits.iter().map(|e| e.2))),
            ),
            (
                "meters",
                Arc::new(UInt16Array::from_iter_values(exits.iter().map(|e| e.3))),
            ),
        ],
    )
}

/// Writes `walks::transfer_paths` as `from`, `to`, `latitude`, and `longitude`,
/// a row for each point, each walk's in order.
fn write_paths(out: &Path, stations: &[String], paths: &[walks::Path]) -> Result<()> {
    let points = || {
        paths
            .iter()
            .flat_map(|(s, t, p)| p.iter().map(move |q| (*s, *t, *q)))
    };
    write_parquet(
        out,
        vec![
            (
                "from",
                Arc::new(StringArray::from_iter_values(
                    points().map(|(s, ..)| stations[s as usize].as_str()),
                )),
            ),
            (
                "to",
                Arc::new(StringArray::from_iter_values(
                    points().map(|(_, t, _)| stations[t as usize].as_str()),
                )),
            ),
            (
                "latitude",
                Arc::new(Float64Array::from_iter_values(points().map(|(.., q)| q.0))),
            ),
            (
                "longitude",
                Arc::new(Float64Array::from_iter_values(points().map(|(.., q)| q.1))),
            ),
        ],
    )
}

/// Where a run writing its times to `out` writes its `what`: `{out}-{what}.parquet`.
fn beside(out: &Path, what: &str) -> PathBuf {
    let stem = out.file_stem().unwrap_or_default().to_string_lossy();
    out.with_file_name(format!("{stem}-{what}.parquet"))
}

/// `write_routes`' `leg` for a journey with no legs, walked from the origin, on a row of its own.
pub const NO_LEGS: u8 = u8::MAX;

/// Writes each origin's routes as `origin`, `station`, `minute`, `leg`,
/// `pattern` (`WALK` for a walk), `board`, `alight`, `from`, and `to`, the stations,
/// `start`, and `end`, a journey with no legs as one row with `leg` `NO_LEGS`.
fn write_routes(
    out: &Path,
    origins: &[String],
    stations: &[String],
    routes: &[Routes],
) -> Result<()> {
    const NONE: RouteLeg = RouteLeg {
        pattern: WALK,
        board: 0,
        alight: 0,
        from: u32::MAX,
        to: u32::MAX,
        start: 0,
        end: 0,
    };
    let rows: Vec<(&str, u32, u8, u8, &RouteLeg)> = origins
        .iter()
        .zip(routes)
        .flat_map(|(o, r)| {
            r.iter().flat_map(move |(s, minute, legs)| {
                let none = legs
                    .is_empty()
                    .then_some((o.as_str(), *s, *minute, NO_LEGS, &NONE));
                legs.iter()
                    .enumerate()
                    .map(move |(i, leg)| (o.as_str(), *s, *minute, i as u8, leg))
                    .chain(none)
            })
        })
        .collect();
    let name = |s: u32| stations.get(s as usize).map_or("", String::as_str);
    write_parquet(
        out,
        vec![
            (
                "origin",
                Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.0))),
            ),
            (
                "station",
                Arc::new(StringArray::from_iter_values(
                    rows.iter().map(|r| name(r.1)),
                )),
            ),
            (
                "minute",
                Arc::new(UInt8Array::from_iter_values(rows.iter().map(|r| r.2))),
            ),
            (
                "leg",
                Arc::new(UInt8Array::from_iter_values(rows.iter().map(|r| r.3))),
            ),
            (
                "pattern",
                Arc::new(UInt32Array::from_iter_values(
                    rows.iter().map(|r| r.4.pattern),
                )),
            ),
            (
                "board",
                Arc::new(UInt16Array::from_iter_values(
                    rows.iter().map(|r| r.4.board),
                )),
            ),
            (
                "alight",
                Arc::new(UInt16Array::from_iter_values(
                    rows.iter().map(|r| r.4.alight),
                )),
            ),
            (
                "from",
                Arc::new(StringArray::from_iter_values(
                    rows.iter().map(|r| name(r.4.from)),
                )),
            ),
            (
                "to",
                Arc::new(StringArray::from_iter_values(
                    rows.iter().map(|r| name(r.4.to)),
                )),
            ),
            (
                "start",
                Arc::new(UInt16Array::from_iter_values(
                    rows.iter().map(|r| r.4.start),
                )),
            ),
            (
                "end",
                Arc::new(UInt16Array::from_iter_values(rows.iter().map(|r| r.4.end))),
            ),
        ],
    )
}

/// Writes `tt`'s patterns' stations, as `pattern`, `route`, `position`, and `station`.
fn write_patterns(out: &Path, tt: &Timetable, walks: &Walks, stations: &[String]) -> Result<()> {
    let rows: Vec<(u32, &str, u16, &str)> = tt
        .patterns
        .iter()
        .enumerate()
        .flat_map(|(p, pattern)| {
            pattern.stops.iter().enumerate().map(move |(i, &stop)| {
                let station = walks.stations[stop as usize]
                    .and_then(|s| stations.get(s as usize))
                    .map_or("", String::as_str);
                (p as u32, pattern.route_id.as_str(), i as u16, station)
            })
        })
        .collect();
    write_parquet(
        out,
        vec![
            (
                "pattern",
                Arc::new(UInt32Array::from_iter_values(rows.iter().map(|r| r.0))),
            ),
            (
                "route",
                Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.1))),
            ),
            (
                "position",
                Arc::new(UInt16Array::from_iter_values(rows.iter().map(|r| r.2))),
            ),
            (
                "station",
                Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.3))),
            ),
        ],
    )
}

/// One routing: a feed on a date, departing each minute of `minutes` from `depart`,
/// written to `out`, and whether a journey may pay again (see `change_trains`).
type Run = (PathBuf, Date, String, u32, PathBuf, bool);

/// Rides from each of `origins`, by station ID, to every station's platforms, in each of `runs`,
/// walking on the streets in `osm`, an `.osm.pbf`, at `speed` millimeters a second,
/// through `stations`' entrances (see `walks.rs`),
/// changing trains within a complex by `platforms`, `mta-od-data`'s `platforms.csv`,
/// with `islands`' stations' stops all on one island platform (see `use_platforms`),
/// and changing free out of the system between the complexes of `free`'s pairs of stations.
/// Each run writes its origins' arrivals at stations to `{out}-arrivals.parquet`,
/// the journeys behind them to `{out}-routes.parquet`,
/// and its patterns' stations to `{out}-patterns.parquet`,
/// and the walks out of every station to `destinations`, as `(latitude, longitude)`,
/// for walking at any speed,
/// up to `exits_limit` millimeters, go to `exits_out` (see `walks::exits`),
/// and the street walks between stations to draw go to `paths_out` (see `walks::transfer_paths`).
///
/// The streets are read and walked once, for every feed's stations (see `walks.rs`).
#[pyfunction(name = "travel_times")]
#[allow(clippy::too_many_arguments)]
fn py_travel_times(
    py: Python<'_>,
    osm: PathBuf,
    max_rides: usize,
    max_minutes: u32,
    stations: Vec<Station>,
    platforms: PathBuf,
    islands: Vec<String>,
    free: Vec<(String, String)>,
    origins: Vec<String>,
    destinations: Vec<(f64, f64)>,
    speed: Mm,
    runs: Vec<Run>,
    exits_out: PathBuf,
    exits_limit: Mm,
    paths_out: PathBuf,
) -> Result<()> {
    let max_secs = max_minutes as Secs * 60;
    py.detach(|| {
        // Each feed once, and each run's timetable.
        let mut opened: HashMap<&PathBuf, Feed> = HashMap::default();
        for (feed, ..) in &runs {
            if !opened.contains_key(feed) {
                opened.insert(feed, Feed::open(feed)?);
            }
        }
        // In the order of `runs`.
        let mut tts = runs
            .iter()
            .map(|(f, date, ..)| Timetable::build(&opened[f], *date))
            .collect::<Result<Vec<_>>>()?;
        drop(opened);
        let graph = Graph::build(&Osm::read(&osm)?);
        let all = StreetWalks::compute(&graph, &stations, &origins, speed, max_secs);
        let exits = walks::exits(&graph, &stations, &destinations, speed, exits_limit);
        write_exits(&exits_out, all.stations(), &exits)?;
        drop(exits);
        let paths = walks::transfer_paths(&graph, &stations, speed);
        write_paths(&paths_out, all.stations(), &paths)?;
        drop(paths);
        drop(graph);

        let platforms = Platforms::load(&platforms)?;
        let islands: HashSet<String> = islands.into_iter().collect();
        // Each run's own walks, as splitting stops by platform numbers them by its patterns.
        for (tt, (_, _, depart, minutes, out, pay_again)) in tts.iter_mut().zip(&runs) {
            let Some(depart) = parse_time(depart) else {
                bail!("not a time: {depart}");
            };
            use_platforms(tt, &platforms, &islands, &all);
            let walks = Walks::for_feed(&all, &[tt], &free);
            change_trains(tt, &walks, *pay_again);
            let req = Request {
                tt,
                walks: &walks,
                depart,
                minutes: *minutes,
                max_rides,
                max_secs,
                stations: all.stations().len(),
            };
            let (arrivals, routes): (Vec<_>, Vec<_>) =
                travel_times(&req, &origins).into_iter().unzip();
            write_arrivals(
                &beside(out, "arrivals"),
                &origins,
                all.stations(),
                &arrivals,
            )?;
            write_routes(&beside(out, "routes"), &origins, all.stations(), &routes)?;
            write_patterns(&beside(out, "patterns"), tt, &walks, all.stations())?;
        }
        Ok(())
    })
}

/// Every walk on the streets between `stations` of different complexes that routing may change on,
/// as `(from, to, seconds)`, walking at `speed` millimeters a second, entrances included.
#[pyfunction]
fn transfers(
    py: Python<'_>,
    osm: PathBuf,
    stations: Vec<Station>,
    speed: Mm,
) -> Result<Vec<(String, String, Secs)>> {
    py.detach(|| {
        let graph = Graph::build(&Osm::read(&osm)?);
        Ok(StreetWalks::compute(&graph, &stations, &[], speed, 0).transfers())
    })
}

/// Whether each of `points`, as `(latitude, longitude)`,
/// links to the streets in `osm`, an `.osm.pbf`, within `meters`, as routing links it.
#[pyfunction]
fn near_streets(
    py: Python<'_>,
    osm: PathBuf,
    points: Vec<(f64, f64)>,
    meters: f64,
) -> Result<Vec<bool>> {
    py.detach(|| {
        let graph = Graph::build(&Osm::read(&osm)?);
        let within = (meters * 1000.0) as Mm;
        Ok(points
            .par_iter()
            .map(|&(lat, lon)| graph.snap(lat, lon).is_some_and(|s| s.to_edge <= within))
            .collect())
    })
}

/// The stations some trip in `feed` stops at, as `(id, name, latitude, longitude)`.
#[pyfunction]
fn stations(py: Python<'_>, feed: PathBuf) -> Result<Vec<(String, String, f64, f64)>> {
    py.detach(|| Ok(feed::stations(&Feed::open(&feed)?)))
}

/// Each pair of stations some trip in `feed` runs between without stopping,
/// as `(station, station, routes, shapes)`.
#[pyfunction]
fn station_pairs(py: Python<'_>, feed: PathBuf) -> Result<Vec<feed::Pair>> {
    py.detach(|| Ok(feed::station_pairs(&Feed::open(&feed)?)))
}

/// Which of the walks out of stations to the cells the page ever takes, by their place,
/// as `web/times/`'s `used_exits` finds them,
/// from every origin, in every run, at every one of `mphs`.
/// `arrivals` is each origin's, as the page reads them, one after another,
/// and the walks are `cell`, `exit_seconds`, and `meters`, by station from `starts`,
/// all little-endian, as `export` writes them.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn used_exits(
    py: Python<'_>,
    arrivals: &[u8],
    cell: &[u8],
    exit_seconds: &[u8],
    meters: &[u8],
    starts: &[u8],
    cells: usize,
    stations: usize,
    mphs: Vec<f64>,
) -> Vec<bool> {
    let u16s = |bytes: &[u8]| -> Vec<u16> {
        bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
            .collect()
    };
    let (cell, exit_seconds, meters) = (u16s(cell), u16s(exit_seconds), u16s(meters));
    let starts: Vec<u32> = starts
        .as_chunks::<4>()
        .0
        .iter()
        .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
        .collect();
    let per_origin = arrivals.len() / stations;
    py.detach(|| {
        let exits = times::Exits {
            cell: &cell,
            exit_seconds: &exit_seconds,
            meters: &meters,
            starts: &starts,
        };
        let runs: Vec<usize> = (0..times::RUNS).collect();
        (0..stations)
            .into_par_iter()
            .fold(
                || vec![false; cell.len()],
                |mut used: Vec<bool>, origin| {
                    let arrivals = u16s(&arrivals[origin * per_origin..][..per_origin]);
                    let mut out = vec![0u8; times::RUNS * (cells + stations)];
                    let mut exit_from = vec![0u16; times::RUNS * cells];
                    let mut exit_minute = vec![0u8; times::RUNS * cells];
                    for &mph in &mphs {
                        times::used_exits(
                            &arrivals,
                            &exits,
                            origin,
                            cells,
                            stations,
                            mph,
                            &runs,
                            times::TravelTimes {
                                times: &mut out,
                                exit_from: &mut exit_from,
                                exit_minute: &mut exit_minute,
                                saved: &mut [],
                            },
                            &mut used,
                        );
                    }
                    used
                },
            )
            .reduce(
                || vec![false; cell.len()],
                |mut a: Vec<bool>, b| {
                    a.iter_mut().zip(b).for_each(|(a, b)| *a |= b);
                    a
                },
            )
    })
}

/// Each of `stations`' walks out to destinations of `wanted`, `(station, destination, far)`,
/// by index into `stations` and `destinations`, as `exits` takes them,
/// from the entrance quickest at `speed` millimeters a second, walking at most `limit` millimeters,
/// as a tree (see `walks::ExitTree`), with the walks to `far` ones after the rest:
/// `(parents, latitudes, longitudes, near, ends, near_ends)`,
/// the first three little-endian `int32`s and `ends` `(destination, point)` little-endian `uint32`s.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn exit_trees<'py>(
    py: Python<'py>,
    osm: PathBuf,
    stations: Vec<Station>,
    destinations: Vec<(f64, f64)>,
    speed: Mm,
    limit: Mm,
    wanted: Vec<(u32, u32, bool)>,
) -> Result<Vec<ExitTreeBytes<'py>>> {
    let trees = py.detach(|| -> Result<_> {
        let graph = Graph::build(&Osm::read(&osm)?);
        Ok(walks::exit_trees(
            &graph,
            &stations,
            &destinations,
            speed,
            limit,
            &wanted,
        ))
    })?;
    let bytes =
        |values: &[i32]| -> Vec<u8> { values.iter().flat_map(|v| v.to_le_bytes()).collect() };
    Ok(trees
        .into_iter()
        .map(|tree| {
            let ends: Vec<u8> = tree
                .ends
                .iter()
                .flat_map(|&(d, at)| [d.to_le_bytes(), at.to_le_bytes()])
                .flatten()
                .collect();
            (
                PyBytes::new(py, &bytes(&tree.parent)),
                PyBytes::new(py, &bytes(&tree.lat)),
                PyBytes::new(py, &bytes(&tree.lon)),
                tree.near,
                PyBytes::new(py, &ends),
                tree.near_ends,
            )
        })
        .collect())
}

/// `exit_trees`' trees, as Python has them.
type ExitTreeBytes<'py> = (
    Bound<'py, PyBytes>,
    Bound<'py, PyBytes>,
    Bound<'py, PyBytes>,
    usize,
    Bound<'py, PyBytes>,
    usize,
);

#[pymodule]
#[pyo3(name = "_route")]
fn route_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_travel_times, m)?)?;
    m.add_function(wrap_pyfunction!(near_streets, m)?)?;
    m.add_function(wrap_pyfunction!(transfers, m)?)?;
    m.add_function(wrap_pyfunction!(stations, m)?)?;
    m.add_function(wrap_pyfunction!(station_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(used_exits, m)?)?;
    m.add_function(wrap_pyfunction!(exit_trees, m)?)?;
    m.add_class::<sat::Model>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arrivals_and_journeys_over_the_window() {
        let files = std::collections::HashMap::from([
            ("stops.txt", "stop_id,stop_name\nA,A\nB,B\n"),
            ("trips.txt", "route_id,trip_id,service_id\n1,t1,W\n1,t2,W\n"),
            (
                "stop_times.txt",
                "trip_id,stop_id,arrival_time,departure_time,stop_sequence\n\
                t1,A,08:05:00,08:05:00,1\nt1,B,08:15:00,08:15:00,2\n\
                t2,A,08:20:00,08:20:00,1\nt2,B,08:30:00,08:30:00,2\n",
            ),
            (
                "calendar.txt",
                "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n\
                W,1,1,1,1,1,1,1,20250101,20251231\n",
            ),
        ]);
        let feed = Feed::load(&mut { files }).unwrap();
        let tt = Timetable::build(&feed, Date::new(2025, 9, 3).unwrap()).unwrap();
        let (a, b) = (tt.stop("A").unwrap(), tt.stop("B").unwrap());
        let mut stations = vec![None; tt.stops.len()];
        stations[a as usize] = Some(0);
        stations[b as usize] = Some(1);
        let walks = Walks {
            access: [("O".to_owned(), vec![(a, 120)])].into_iter().collect(),
            stations,
            ..Walks::default()
        };
        let req = Request {
            tt: &tt,
            walks: &walks,
            depart: parse_time("08:00:00").unwrap(),
            minutes: 3,
            max_rides: 8,
            max_secs: 120 * 60,
            stations: 2,
        };
        let times = travel_times(&req, &["O".to_owned()]);
        // On A's platforms 2 minutes after departing, on foot;
        // on B's when the 08:05 arrives, 15, 14, and 13 minutes after departing.
        let each_minute = vec![
            (0, 0, 120),
            (0, 1, 120),
            (0, 2, 120),
            (1, 0, 15 * 60),
            (1, 1, 14 * 60),
            (1, 2, 13 * 60),
        ];
        // To B on the one pattern, from A, its first stop, to B, its second,
        // on the 08:05, arriving 08:15.
        let to_b = RouteLeg {
            pattern: 0,
            board: 0,
            alight: 1,
            from: 0,
            to: 1,
            start: 5 * 60,
            end: 15 * 60,
        };
        assert_eq!(
            times,
            [(
                each_minute,
                // A's arrival changes every minute, walked to, so each minute's journey is read back;
                // B's is the 08:05's each minute, read back only at the last.
                vec![
                    (0, 0, vec![]),
                    (0, 1, vec![]),
                    (0, 2, vec![]),
                    (1, 2, vec![to_b])
                ]
            )]
        );
    }
}
