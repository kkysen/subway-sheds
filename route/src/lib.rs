//! Travel times from subway stations to places around the city,
//! routed with `mta-od-data`'s RAPTOR between stops,
//! walking at either end and between stations on OpenStreetMap's streets
//! with `mta-od-data`'s `streets`, through stations' entrances (see `walks.rs`).
//! Python drives it through the `subway_sheds._route` module.

mod feed;
mod walks;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Result, bail};
use arrow_array::{ArrayRef, RecordBatch, StringArray, UInt8Array, UInt16Array, UInt32Array};
use arrow_schema::{Field, Schema};
use jiff::civil::Date;
use parquet::arrow::ArrowWriter;
use pyo3::types::{PyModule, PyModuleMethods};
use pyo3::{Bound, PyResult, Python, pyfunction, pymodule, wrap_pyfunction};
use raptor::assign::{DEFAULT_STACKED_SECS_PER_LEVEL, DEFAULT_UP_AND_OVER_SECS};
use raptor::gtfs::{Feed, Secs, parse_time};
use raptor::platforms::Platforms;
use raptor::raptor::Router;
use raptor::timetable::{StopIdx, Timetable};
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};
use streets::osm::Osm;
use streets::{Graph, Mm};

use crate::walks::{Station, StreetWalks, Walks};

/// The percentile R5 reports: the median.
const PERCENTILE: f64 = 50.0;

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
    pub destinations: usize,
    /// How many stations `StreetWalks` has.
    pub stations: usize,
}

/// Each origin's travel times, as R5's `TravelTimeMatrix` reports them:
/// its `PERCENTILE`th journey over a departure each minute of the window,
/// in whole minutes rounded down, to each destination reached within `max_secs`.
/// With each, its ride to each station's platforms each minute, before walking out
/// (see `Arrivals`).
pub fn travel_times(req: &Request, origins: &[String]) -> Vec<(Vec<(u32, u8)>, Arrivals)> {
    let router = Router::with_max_rides(req.tt, req.max_rides);
    origins
        .par_iter()
        .map(|origin| origin_times(req, &router, origin))
        .collect()
}

/// An origin's arrival by train on each station's platforms each minute of the window,
/// within `max_secs`, as `(station, minute, seconds from departing)`,
/// by the station's index in `StreetWalks`:
/// what the page adds walks out of stations to, at whatever speed its reader walks,
/// before taking each destination's `PERCENTILE`th over the minutes, as here.
pub type Arrivals = Vec<(u32, u8, u16)>;

fn origin_times(req: &Request, router: &Router, origin: &str) -> (Vec<(u32, u8)>, Arrivals) {
    let walks = req.walks;
    let access = walks.access.get(origin).map_or(&[][..], Vec::as_slice);
    let direct = walks.direct.get(origin).map_or(&[][..], Vec::as_slice);
    // Each destination's time each minute, minute-major.
    let n = req.destinations;
    let mut times = vec![Secs::MAX; req.minutes as usize * n];
    // Each station's arrival each minute, minute-major.
    let ns = req.stations;
    let mut at_stations = vec![Secs::MAX; req.minutes as usize * ns];
    let mut labels = router.labels();
    for m in (0..req.minutes).rev() {
        let depart = req.depart + 60 * m as Secs;
        labels.run_access(access, depart);
        let times = &mut times[m as usize * n..][..n];
        for &(d, walk) in direct {
            times[d as usize] = times[d as usize].min(walk);
        }
        for (s, arrive) in labels.arrivals().enumerate() {
            if arrive == Secs::MAX {
                continue;
            }
            let ride = arrive - depart;
            if let Some(station) = walks.stations[s] {
                let at = &mut at_stations[m as usize * ns + station as usize];
                *at = (*at).min(ride);
            }
            for &(d, walk) in &walks.egress[s] {
                times[d as usize] = times[d as usize].min(ride + walk);
            }
        }
    }
    let index = percentile_index(req.minutes as usize, PERCENTILE);
    let medians = |times: &[Secs], n: usize| -> Vec<(u32, Secs)> {
        let mut each_minute = Vec::with_capacity(req.minutes as usize);
        (0..n)
            .filter_map(|d| {
                each_minute.clear();
                each_minute.extend((0..req.minutes as usize).map(|m| times[m * n + d]));
                each_minute.sort_unstable();
                let t = each_minute[index];
                (t <= req.max_secs).then_some((d as u32, t))
            })
            .collect()
    };
    let cells = medians(&times, n)
        .into_iter()
        .map(|(d, t)| (d, (t / 60) as u8))
        .collect();
    let arrivals = (0..ns)
        .flat_map(|s| (0..req.minutes as usize).map(move |m| (s, m)))
        .filter_map(|(s, m)| {
            let t = at_stations[m * ns + s];
            (t <= req.max_secs).then_some((s as u32, m as u8, t as u16))
        })
        .collect();
    (cells, arrivals)
}

/// The index of a percentile in a sorted list of `n`, as R5 picks it, without interpolating
/// (`TravelTimeReducer.findPercentileIndex`).
fn percentile_index(n: usize, percentile: f64) -> usize {
    ((percentile / 100.0 * n as f64).ceil() as usize).saturating_sub(1)
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
/// and between complexes by `walks`' transfers on the streets.
fn change_trains(tt: &mut Timetable, walks: &Walks) {
    for (from, paths) in tt.footpaths.iter_mut().enumerate() {
        let complex = &walks.complexes[from];
        paths.retain(|&(to, _)| complex.is_some() && walks.complexes[to as usize] == *complex);
    }
    for &(from, to, walk) in &walks.transfers {
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

/// Writes each origin's travel times as `origin`, `destination`, and `minutes`.
fn write_times(out: &Path, origins: &[String], times: &[Vec<(u32, u8)>]) -> Result<usize> {
    write_parquet(
        out,
        vec![
            ("origin", origin_column(origins, times)),
            (
                "destination",
                Arc::new(UInt32Array::from_iter_values(
                    times.iter().flatten().map(|&(d, _)| d),
                )),
            ),
            (
                "minutes",
                Arc::new(UInt8Array::from_iter_values(
                    times.iter().flatten().map(|&(_, m)| m),
                )),
            ),
        ],
    )?;
    Ok(times.iter().map(Vec::len).sum())
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

/// Where a run writing its times to `out` writes its arrivals: `{out}-arrivals.parquet`.
fn arrivals_path(out: &Path) -> PathBuf {
    let stem = out.file_stem().unwrap_or_default().to_string_lossy();
    out.with_file_name(format!("{stem}-arrivals.parquet"))
}

/// One routing: a feed on a date, departing each minute of `minutes` from `depart`,
/// written to `out`.
type Run = (PathBuf, Date, String, u32, PathBuf);

/// Median minutes from each of `origins`, by station ID,
/// to each of `destinations`, as `(latitude, longitude)`, in each of `runs`,
/// walking on the streets in `osm`, an `.osm.pbf`, at `speed` millimeters a second,
/// through `stations`' entrances (see `walks.rs`),
/// changing trains within a complex by `platforms`, `mta-od-data`'s `platforms.csv`,
/// with `islands`' stations' stops all on one island platform (see `use_platforms`).
/// Each run also writes its origins' arrivals at stations to `{out}-arrivals.parquet`,
/// and the walks out of every station to destinations, for walking at any speed,
/// up to `exits_limit` millimeters, go to `exits_out` (see `walks::exits`).
/// Returns how many rows each run wrote.
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
    origins: Vec<String>,
    destinations: Vec<(f64, f64)>,
    speed: Mm,
    runs: Vec<Run>,
    exits_out: PathBuf,
    exits_limit: Mm,
) -> Result<Vec<usize>> {
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
        let all = StreetWalks::compute(&graph, &stations, &origins, &destinations, speed, max_secs);
        let exits = walks::exits(&graph, &stations, &destinations, speed, exits_limit);
        write_exits(&exits_out, all.stations(), &exits)?;
        drop(exits);
        drop(graph);

        let platforms = Platforms::load(&platforms)?;
        let islands: HashSet<String> = islands.into_iter().collect();
        let mut rows = vec![0; runs.len()];
        // Each run's own walks, as splitting stops by platform numbers them by its patterns.
        for (i, (tt, (_, _, depart, minutes, out))) in tts.iter_mut().zip(&runs).enumerate() {
            let Some(depart) = parse_time(depart) else {
                bail!("not a time: {depart}");
            };
            use_platforms(tt, &platforms, &islands, &all);
            let walks = Walks::for_feed(&all, &[tt]);
            change_trains(tt, &walks);
            let req = Request {
                tt,
                walks: &walks,
                depart,
                minutes: *minutes,
                max_rides,
                max_secs,
                destinations: destinations.len(),
                stations: all.stations().len(),
            };
            let (times, arrivals): (Vec<_>, Vec<_>) =
                travel_times(&req, &origins).into_iter().unzip();
            rows[i] = write_times(out, &origins, &times)?;
            write_arrivals(&arrivals_path(out), &origins, all.stations(), &arrivals)?;
        }
        Ok(rows)
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

#[pymodule]
#[pyo3(name = "_route")]
fn route_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_travel_times, m)?)?;
    m.add_function(wrap_pyfunction!(near_streets, m)?)?;
    m.add_function(wrap_pyfunction!(stations, m)?)?;
    m.add_function(wrap_pyfunction!(station_pairs, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn median_of_60_is_the_30th() {
        assert_eq!(percentile_index(60, 50.0), 29);
        assert_eq!(percentile_index(1, 50.0), 0);
        assert_eq!(percentile_index(5, 100.0), 4);
    }

    #[test]
    fn median_over_the_window_walking_at_either_end() {
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
        let mut egress = vec![Vec::new(); tt.stops.len()];
        egress[b as usize].push((0, 60));
        let mut stations = vec![None; tt.stops.len()];
        stations[a as usize] = Some(0);
        stations[b as usize] = Some(1);
        let walks = Walks {
            access: [("O".to_owned(), vec![(a, 120)])].into_iter().collect(),
            direct: [("O".to_owned(), vec![(1, 300)])].into_iter().collect(),
            egress,
            transfers: Vec::new(),
            complexes: Vec::new(),
            stations,
        };
        let req = Request {
            tt: &tt,
            walks: &walks,
            depart: parse_time("08:00:00").unwrap(),
            minutes: 3,
            max_rides: 8,
            max_secs: 120 * 60,
            destinations: 3,
            stations: 2,
        };
        // Leaving at 08:00, 08:01, and 08:02, the walk makes the 08:05: 16, 15, and 14 minutes.
        // Destination 2 is never reached.
        let times = travel_times(&req, &["O".to_owned()]);
        // On B's platform when the 08:05 arrives, 15, 14, and 13 minutes after departing;
        // A's only on foot, which walking straight to destinations covers.
        let b_each_minute = vec![(1, 0, 15 * 60), (1, 1, 14 * 60), (1, 2, 13 * 60)];
        assert_eq!(times, [(vec![(0, 15), (1, 5)], b_each_minute)]);
    }
}
