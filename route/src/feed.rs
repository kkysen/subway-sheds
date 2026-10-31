//! What the page draws from a feed, read in Rust, as reading `stop_times.txt` in Python is slow.

use std::collections::BTreeMap;

use raptor::gtfs::Feed;
use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

/// The stations some trip stops at, as `(id, name, latitude, longitude)`, in `stops.txt`'s order:
/// not every station in `stops.txt`, since the baseline keeps QueensLink's stations
/// with no trips stopping at them.
pub fn stations(feed: &Feed) -> Vec<(String, String, f64, f64)> {
    let stopped: HashSet<&str> = feed
        .stop_times
        .iter()
        .map(|st| st.stop_id.as_str())
        .collect();
    let served: HashSet<&str> = feed
        .stops
        .iter()
        .filter(|s| stopped.contains(s.stop_id.as_str()))
        .map(|s| s.parent_station.as_deref().unwrap_or(&s.stop_id))
        .collect();
    feed.stops
        .iter()
        .filter(|s| s.location_type == Some(1) && served.contains(s.stop_id.as_str()))
        .map(|s| {
            (
                s.stop_id.clone(),
                s.stop_name.clone(),
                s.stop_lat.unwrap_or_default(),
                s.stop_lon.unwrap_or_default(),
            )
        })
        .collect()
}

/// A pair of stations trains run between without stopping:
/// `(station, station, routes, shapes)`, the smaller station ID first,
/// with the routes whose trips run between them and those trips' shapes, each sorted.
pub type Pair = (String, String, Vec<String>, Vec<String>);

/// A pair's routes and shapes, so far.
type Uses<'a> = (HashSet<&'a str>, HashSet<&'a str>);

/// Each pair of parent stations some trip runs between without stopping.
pub fn station_pairs(feed: &Feed) -> Vec<Pair> {
    let parent: HashMap<&str, &str> = feed
        .stops
        .iter()
        .map(|s| {
            (
                s.stop_id.as_str(),
                s.parent_station.as_deref().unwrap_or(&s.stop_id),
            )
        })
        .collect();
    let trips: HashMap<&str, _> = feed.trips.iter().map(|t| (t.trip_id.as_str(), t)).collect();
    let mut stops: HashMap<&str, Vec<(u32, &str)>> = HashMap::default();
    for st in &feed.stop_times {
        stops.entry(st.trip_id.as_str()).or_default().push((
            st.stop_sequence,
            parent
                .get(st.stop_id.as_str())
                .copied()
                .unwrap_or(&st.stop_id),
        ));
    }
    // By pair, for a stable order.
    let mut pairs: BTreeMap<(&str, &str), Uses> = BTreeMap::new();
    for (trip_id, mut stops) in stops {
        let Some(trip) = trips.get(trip_id) else {
            continue;
        };
        stops.sort_unstable();
        for w in stops.windows(2) {
            let (a, b) = (w[0].1, w[1].1);
            if a == b {
                continue;
            }
            let (routes, shapes) = pairs.entry((a.min(b), a.max(b))).or_default();
            routes.insert(&trip.route_id);
            if let Some(shape) = &trip.shape_id {
                shapes.insert(shape);
            }
        }
    }
    pairs
        .into_iter()
        .map(|((a, b), (routes, shapes))| {
            let sorted = |set: HashSet<&str>| {
                let mut v: Vec<String> = set.into_iter().map(str::to_owned).collect();
                v.sort_unstable();
                v
            };
            (a.to_owned(), b.to_owned(), sorted(routes), sorted(shapes))
        })
        .collect()
}
