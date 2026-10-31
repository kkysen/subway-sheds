//! Travel times from subway stations to places around the city,
//! routed with `mta-od-data`'s RAPTOR between stops,
//! walking at either end and between stops on OpenStreetMap's streets
//! with `mta-od-data`'s `streets` (see `walks.rs`), as R5 did.
//! Python drives it through the `subway_sheds._route` module.

mod feed;
mod walks;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Result, bail};
use arrow_array::{RecordBatch, StringArray, UInt8Array, UInt32Array};
use arrow_schema::{DataType, Field, Schema};
use jiff::civil::Date;
use parquet::arrow::ArrowWriter;
use pyo3::types::{PyModule, PyModuleMethods};
use pyo3::{Bound, PyResult, Python, pyfunction, pymodule, wrap_pyfunction};
use raptor::gtfs::{Feed, Secs, parse_time};
use raptor::raptor::Router;
use raptor::timetable::Timetable;
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use rustc_hash::FxHashMap as HashMap;
use streets::osm::Osm;
use streets::{Graph, Mm};

use crate::walks::{StreetWalks, Walks};

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
}

/// Each origin's travel times, as R5's `TravelTimeMatrix` reports them:
/// its `PERCENTILE`th journey over a departure each minute of the window,
/// in whole minutes rounded down, to each destination reached within `max_secs`.
pub fn travel_times(req: &Request, origins: &[String]) -> Vec<Vec<(u32, u8)>> {
    let router = Router::with_max_rides(req.tt, req.max_rides);
    origins
        .par_iter()
        .map(|origin| origin_times(req, &router, origin))
        .collect()
}

fn origin_times(req: &Request, router: &Router, origin: &str) -> Vec<(u32, u8)> {
    let walks = req.walks;
    let access = walks.access.get(origin).map_or(&[][..], Vec::as_slice);
    let direct = walks.direct.get(origin).map_or(&[][..], Vec::as_slice);
    // Each destination's time each minute, minute-major.
    let n = req.destinations;
    let mut times = vec![Secs::MAX; req.minutes as usize * n];
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
            for &(d, walk) in &walks.egress[s] {
                times[d as usize] = times[d as usize].min(ride + walk);
            }
        }
    }
    let index = percentile_index(req.minutes as usize, PERCENTILE);
    let mut each_minute = Vec::with_capacity(req.minutes as usize);
    (0..n)
        .filter_map(|d| {
            each_minute.clear();
            each_minute.extend((0..req.minutes as usize).map(|m| times[m * n + d]));
            each_minute.sort_unstable();
            let t = each_minute[index];
            (t <= req.max_secs).then_some((d as u32, (t / 60) as u8))
        })
        .collect()
}

/// The index of a percentile in a sorted list of `n`, as R5 picks it, without interpolating
/// (`TravelTimeReducer.findPercentileIndex`).
fn percentile_index(n: usize, percentile: f64) -> usize {
    ((percentile / 100.0 * n as f64).ceil() as usize).saturating_sub(1)
}

/// `tt`, changing trains as R5 does: walking between stops by `walks`' transfers,
/// not `transfers.txt`'s, and with no time to change trains at a stop.
fn change_as_r5(tt: &mut Timetable, walks: &Walks) {
    tt.min_change.fill(0);
    for paths in &mut tt.footpaths {
        paths.clear();
    }
    for &(from, to, walk) in &walks.transfers {
        tt.footpaths[from as usize].push((to, walk));
    }
}

/// Writes each origin's travel times as `origin`, `destination`, and `minutes`.
fn write_times(out: &Path, origins: &[String], times: &[Vec<(u32, u8)>]) -> Result<usize> {
    let rows: usize = times.iter().map(Vec::len).sum();
    let origin = StringArray::from_iter_values(
        origins
            .iter()
            .zip(times)
            .flat_map(|(o, t)| std::iter::repeat_n(o.as_str(), t.len())),
    );
    let destination = UInt32Array::from_iter_values(times.iter().flatten().map(|&(d, _)| d));
    let minutes = UInt8Array::from_iter_values(times.iter().flatten().map(|&(_, m)| m));
    let schema = Arc::new(Schema::new(vec![
        Field::new("origin", DataType::Utf8, false),
        Field::new("destination", DataType::UInt32, false),
        Field::new("minutes", DataType::UInt8, false),
    ]));
    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![Arc::new(origin), Arc::new(destination), Arc::new(minutes)],
    )?;
    let mut writer = ArrowWriter::try_new(std::fs::File::create(out)?, schema, None)?;
    writer.write(&batch)?;
    writer.close()?;
    Ok(rows)
}

/// One routing: a feed on a date, departing each minute of `minutes` from `depart`,
/// written to `out`.
type Run = (PathBuf, Date, String, u32, PathBuf);

/// Median minutes from each of `origins`, as `(id, latitude, longitude)`,
/// to each of `destinations`, as `(latitude, longitude)`, in each of `runs`,
/// walking on the streets in `osm`, an `.osm.pbf`, at `speed` millimeters a second
/// (see `walks.rs`). Returns how many rows each run wrote.
///
/// The streets are read and walked once, for every feed's stops (see `walks.rs`).
#[pyfunction(name = "travel_times")]
#[allow(clippy::too_many_arguments)]
fn py_travel_times(
    py: Python<'_>,
    osm: PathBuf,
    max_rides: usize,
    max_minutes: u32,
    origins: Vec<(String, f64, f64)>,
    destinations: Vec<(f64, f64)>,
    speed: Mm,
    runs: Vec<Run>,
) -> Result<Vec<usize>> {
    let max_secs = max_minutes as Secs * 60;
    let ids: Vec<String> = origins.iter().map(|(id, ..)| id.clone()).collect();
    py.detach(|| {
        // Each feed once, in the order runs first name them, and each run's timetable.
        let mut feeds: Vec<&PathBuf> = Vec::new();
        for (feed, ..) in &runs {
            if !feeds.contains(&feed) {
                feeds.push(feed);
            }
        }
        let opened = feeds
            .iter()
            .map(|&f| Ok((f, Feed::open(f)?)))
            .collect::<Result<HashMap<_, _>>>()?;
        // In the order of `runs`.
        let mut tts = runs
            .iter()
            .map(|(f, date, ..)| Timetable::build(&opened[f], *date))
            .collect::<Result<Vec<_>>>()?;
        drop(opened);
        // Every feed's stops, once each.
        let mut stops: Vec<(String, f64, f64)> = Vec::new();
        let mut seen = rustc_hash::FxHashSet::default();
        for tt in &tts {
            for stop in &tt.stops {
                if let Some((lat, lon)) = stop.coords
                    && seen.insert(stop.id.clone())
                {
                    stops.push((stop.id.clone(), lat, lon));
                }
            }
        }
        let graph = Graph::build(&Osm::read(&osm)?);
        let all = StreetWalks::compute(&graph, &stops, &origins, &destinations, speed, max_secs);
        drop(graph);

        let mut rows = vec![0; runs.len()];
        for &feed_path in &feeds {
            let mine: Vec<usize> = (0..runs.len())
                .filter(|&i| &runs[i].0 == feed_path)
                .collect();
            let walks = Walks::for_feed(&all, &mine.iter().map(|&i| &tts[i]).collect::<Vec<_>>());
            for &i in &mine {
                let (_, _, depart, minutes, out) = &runs[i];
                let Some(depart) = parse_time(depart) else {
                    bail!("not a time: {depart}");
                };
                let tt = &mut tts[i];
                change_as_r5(tt, &walks);
                let req = Request {
                    tt,
                    walks: &walks,
                    depart,
                    minutes: *minutes,
                    max_rides,
                    max_secs,
                    destinations: destinations.len(),
                };
                rows[i] = write_times(out, &ids, &travel_times(&req, &ids))?;
            }
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
        let walks = Walks {
            access: [("O".to_owned(), vec![(a, 120)])].into_iter().collect(),
            direct: [("O".to_owned(), vec![(1, 300)])].into_iter().collect(),
            egress,
            transfers: Vec::new(),
        };
        let req = Request {
            tt: &tt,
            walks: &walks,
            depart: parse_time("08:00:00").unwrap(),
            minutes: 3,
            max_rides: 8,
            max_secs: 120 * 60,
            destinations: 3,
        };
        // Leaving at 08:00, 08:01, and 08:02, the walk makes the 08:05: 16, 15, and 14 minutes.
        // Destination 2 is never reached.
        let times = travel_times(&req, &["O".to_owned()]);
        assert_eq!(times, [[(0, 15), (1, 5)]]);
    }
}
