//! Travel times from a station, from when its trains reach every station each minute
//! and the walks out of stations, so that walking out of them can be at any speed,
//! for the page, which calls this in a worker (`web/src/times.worker.ts`), compiled to WebAssembly.
//! See `src/subway_sheds/export.py` for the data.
//!
//! It has no bindings: the page puts the data in this module's memory
//! with `alloc`, then calls `travel_times` with where it is.

use std::{ptr, slice};

/// As `src/subway_sheds/travel_times.py` routes: departing each minute of an hour,
/// keeping trips of up to 2 hours, walking out of a station for up to an hour.
const MINUTES: usize = 60;
const MAX_SECONDS: u32 = 120 * 60;
const MAX_EXIT_WALK_SECONDS: u32 = 60 * 60;
/// The median over the window's departures, as routing picks it.
const MEDIAN: usize = MINUTES.div_ceil(2) - 1;
/// The runs each station's arrivals have, by day, then fare, then scenario.
pub const RUNS: usize = 8;

/// In arrivals, a station not reached.
const UNREACHED_SECONDS: u32 = 0xffff;
/// In times, a cell or station not reached.
pub const UNREACHED: u8 = 255;
/// In `exit_from`, a cell not reached.
pub const NO_STATION: u16 = 0xffff;

const METERS_PER_SECOND_PER_MPH: f64 = 1609.344 / 3600.0;

/// Every walk out of a station to a cell, by station.
pub struct Exits<'a> {
    pub cell: &'a [u16],
    pub exit_seconds: &'a [u16],
    pub meters: &'a [u16],
    /// Where each station's walks start, and the last one's end.
    pub starts: &'a [u32],
}

/// What `travel_times` computes, each as `TravelTimes` in `web/src/times.ts` documents it.
pub struct TravelTimes<'a> {
    /// Each run's minutes to every cell, then every station, `UNREACHED` if not within `MAX_SECONDS`.
    pub times: &'a mut [u8],
    /// Each run's cells' station walked out of on its median trip, `origin` if walked from it.
    pub exit_from: &'a mut [u16],
    /// And the minute of the window its median trip departs.
    pub exit_minute: &'a mut [u8],
    /// The minutes the second of two runs saves over the first to every cell, then every station:
    /// the difference of their medians in seconds, in whole minutes, rounded toward zero,
    /// so that trips a few seconds apart don't differ by a minute from rounding each down.
    /// 0 where either isn't within `MAX_SECONDS`, and left as it is unless there are two runs.
    pub saved: &'a mut [i8],
}

/// Where `station`'s walks out are in `exits`.
fn own_range(exits: &Exits, station: usize) -> std::ops::Range<usize> {
    exits.starts[station] as usize..exits.starts[station + 1] as usize
}

/// The minutes `with` saves over `without`, as `TravelTimes::saved` says.
fn saved(without: u32, with: u32) -> i8 {
    if without <= MAX_SECONDS && with <= MAX_SECONDS {
        // Within `MAX_SECONDS`, the most either way is 120 minutes, which fits.
        ((i64::from(without) - i64::from(with)) / 60) as i8
    } else {
        0
    }
}

/// Whole minutes, if within `MAX_SECONDS`.
fn minutes(seconds: u32) -> u8 {
    if seconds <= MAX_SECONDS {
        (seconds / 60) as u8
    } else {
        UNREACHED
    }
}

/// The seconds walking `meters` takes at `speed` meters a second, rounded down.
fn walk(meters: u16, speed: f64) -> u32 {
    (f64::from(meters) / speed) as u32
}

/// Minutes from station `origin` to every cell, then every station, in each of `runs`,
/// walking out of stations at `mph`, into `out`, whose other runs are left as they are:
/// each cell's median over the minutes of its quickest trip,
/// by train and walking out of a station, or walking from `origin` itself.
/// Each station's own, its median arrival on its platforms.
/// `arrivals` are when trains reach each station, in seconds from the window's start,
/// by run, then minute, then station.
#[allow(clippy::too_many_arguments)]
pub fn compute(
    arrivals: &[u16],
    exits: &Exits,
    origin: usize,
    cells: usize,
    stations: usize,
    mph: f64,
    runs: &[usize],
    out: TravelTimes,
) {
    compute_tracking::<false>(
        arrivals,
        exits,
        origin,
        cells,
        stations,
        mph,
        runs,
        out,
        &mut [],
    );
}

/// `compute`, but also marking in `used` each walk out, by its place in `exits`,
/// that some cell's quickest trip takes in some minute, walking out of a station or from `origin`.
/// The rest change nothing `compute` gives: none is ever the quickest.
#[allow(clippy::too_many_arguments)]
pub fn used_exits(
    arrivals: &[u16],
    exits: &Exits,
    origin: usize,
    cells: usize,
    stations: usize,
    mph: f64,
    runs: &[usize],
    out: TravelTimes,
    used: &mut [bool],
) {
    compute_tracking::<true>(
        arrivals, exits, origin, cells, stations, mph, runs, out, used,
    );
}

/// `compute`, and if `TRACK`, `used_exits`, which is only for the pipeline,
/// so that the page's doesn't do the work.
#[allow(clippy::too_many_arguments)]
fn compute_tracking<const TRACK: bool>(
    arrivals: &[u16],
    exits: &Exits,
    origin: usize,
    cells: usize,
    stations: usize,
    mph: f64,
    runs: &[usize],
    out: TravelTimes,
    used: &mut [bool],
) {
    let speed = mph * METERS_PER_SECOND_PER_MPH;
    // Each walk out's seconds, through the station's entrance and on the streets.
    let walks: Vec<u32> = exits
        .meters
        .iter()
        .zip(exits.exit_seconds)
        .map(|(&meters, &inside)| {
            let walk = walk(meters, speed);
            if walk <= MAX_EXIT_WALK_SECONDS {
                walk + u32::from(inside)
            } else {
                UNREACHED_SECONDS
            }
        })
        .collect();
    // Walking straight there from `origin`, out of its entrances without its time to the platforms.
    let mut direct = vec![UNREACHED_SECONDS; cells];
    // And if `TRACK`, the walk out it is.
    let mut direct_exit = vec![0u32; if TRACK { cells } else { 0 }];
    let own = own_range(exits, origin);
    for (k, (&meters, &cell)) in own
        .clone()
        .zip(exits.meters[own.clone()].iter().zip(&exits.cell[own]))
    {
        let walk = walk(meters, speed);
        let cell = usize::from(cell);
        if walk <= MAX_EXIT_WALK_SECONDS && walk < direct[cell] {
            direct[cell] = walk;
            if TRACK {
                direct_exit[cell] = k as u32;
            }
        }
    }
    // If `TRACK`, the cells walked to straight from `origin`, each once.
    let mut walked_to: Vec<usize> = if TRACK {
        exits.cell[own_range(exits, origin)]
            .iter()
            .map(|&cell| usize::from(cell))
            .filter(|&cell| direct[cell] < UNREACHED_SECONDS)
            .collect()
    } else {
        Vec::new()
    };
    walked_to.sort_unstable();
    walked_to.dedup();
    // And the cells whose earliest by train changed this minute, each once.
    let mut changed: Vec<usize> = Vec::new();
    let mut is_changed = vec![false; if TRACK { cells } else { 0 }];
    let stride = cells + stations;
    // The earliest time, from the window's start, each cell is reached by train this minute,
    // and the station walked out of to it.
    let mut best = vec![UNREACHED_SECONDS; cells];
    let mut best_from = vec![0u32; cells];
    // And if `TRACK`, the walk out it is.
    let mut best_exit = vec![0u32; if TRACK { cells } else { 0 }];
    // Each cell's trip each minute, minute-major so that each minute's are computed together,
    // in the high half, and its station in the low,
    // so that ordering them orders by trip and keeps each's station with it.
    let mut trips = vec![0u32; MINUTES * cells];
    let mut each = [0u32; MINUTES];
    // The first of two runs' medians, in seconds, to every cell, then every station, for `saved`.
    let pair = !TRACK && runs.len() == 2;
    let mut first_medians = vec![UNREACHED_SECONDS; if pair { stride } else { 0 }];
    for (i, &run) in runs.iter().enumerate() {
        best.fill(UNREACHED_SECONDS);
        // Latest departure first: departing earlier, every station is reached at the same time or
        // earlier, so each minute's earliest are the next minute's, lowered by the stations that changed.
        for m in (0..MINUTES).rev() {
            let at = (run * MINUTES + m) * stations;
            for s in 0..stations {
                let clock = u32::from(arrivals[at + s]);
                if clock == UNREACHED_SECONDS
                    || (m + 1 < MINUTES && u32::from(arrivals[at + stations + s]) == clock)
                {
                    continue;
                }
                let range = exits.starts[s] as usize..exits.starts[s + 1] as usize;
                for (k, (&walk, &cell)) in range
                    .clone()
                    .zip(walks[range.clone()].iter().zip(&exits.cell[range]))
                {
                    let t = clock + walk;
                    let cell = usize::from(cell);
                    if t < best[cell] {
                        best[cell] = t;
                        best_from[cell] = s as u32;
                        if TRACK {
                            best_exit[cell] = k as u32;
                            if !is_changed[cell] {
                                is_changed[cell] = true;
                                changed.push(cell);
                            }
                        }
                    }
                }
            }
            let departs = 60 * m as u32;
            // Only the cells whose earliest by train changed can newly be reached quickest by train:
            // while it's the same, departing earlier only takes it longer, so it was quickest by train
            // the minute after too, through the same walk out, if it is now.
            // And walking straight from `origin`, only to the cells it walks to.
            if TRACK {
                for &cell in &changed {
                    is_changed[cell] = false;
                    if best[cell] - departs < direct[cell] {
                        used[best_exit[cell] as usize] = true;
                    }
                }
                changed.clear();
                for &cell in &walked_to {
                    let by_train = if best[cell] >= UNREACHED_SECONDS {
                        best[cell]
                    } else {
                        best[cell] - departs
                    };
                    if direct[cell] <= by_train {
                        used[direct_exit[cell] as usize] = true;
                    }
                }
                continue;
            }
            // Branch-free, so that it's vectorized.
            let row = &mut trips[m * cells..][..cells];
            for (((trip, &reached), &from), &walked) in
                row.iter_mut().zip(&best).zip(&best_from).zip(&direct)
            {
                let by_train = if reached >= UNREACHED_SECONDS {
                    reached
                } else {
                    reached - departs
                };
                *trip = if walked <= by_train {
                    walked << 16 | origin as u32
                } else {
                    by_train << 16 | from
                };
            }
        }
        // Only tracking, `used_exits` needs no medians: they're `compute`'s.
        if TRACK {
            continue;
        }
        let first = run * stride;
        for cell in 0..cells {
            for (m, trip) in each.iter_mut().enumerate() {
                *trip = trips[m * cells + cell];
            }
            let mut sorted = each;
            let median = *sorted.select_nth_unstable(MEDIAN).1;
            let trip = median >> 16;
            out.times[first + cell] = minutes(trip);
            if pair {
                if i == 0 {
                    first_medians[cell] = trip;
                } else {
                    out.saved[cell] = saved(first_medians[cell], trip);
                }
            }
            if trip != UNREACHED_SECONDS {
                out.exit_from[run * cells + cell] = median as u16;
                // The first minute of the median trip, as `route` orders ties.
                out.exit_minute[run * cells + cell] =
                    each.iter().position(|&t| t == median).unwrap_or(0) as u8;
            }
        }
        for s in 0..stations {
            for (m, trip) in each.iter_mut().enumerate() {
                let clock = u32::from(arrivals[(run * MINUTES + m) * stations + s]);
                *trip = if clock == UNREACHED_SECONDS {
                    clock
                } else {
                    clock - 60 * m as u32
                };
            }
            let trip = *each.select_nth_unstable(MEDIAN).1;
            out.times[first + cells + s] = minutes(trip);
            if pair {
                if i == 0 {
                    first_medians[cells + s] = trip;
                } else {
                    out.saved[cells + s] = saved(first_medians[cells + s], trip);
                }
            }
        }
    }
}

/// Room for `bytes` in this module's memory, for the page to put data in, aligned for any of it.
///
/// # Safety
///
/// Freed only with `dealloc`, given the same `bytes`.
#[unsafe(no_mangle)]
pub extern "C" fn alloc(bytes: usize) -> *mut u8 {
    let words = vec![0u32; bytes.div_ceil(4)];
    Box::into_raw(words.into_boxed_slice()).cast()
}

/// Frees what `alloc` gave for `bytes`.
///
/// # Safety
///
/// `at` must be what `alloc(bytes)` returned, not yet freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn dealloc(at: *mut u8, bytes: usize) {
    // SAFETY: `alloc` made it a boxed slice of as many words.
    drop(unsafe {
        Box::from_raw(ptr::slice_from_raw_parts_mut(
            at.cast::<u32>(),
            bytes.div_ceil(4),
        ))
    });
}

/// `compute`, with each array where `alloc` put it, `exits` as many as `n_exits`,
/// `arrivals` for `stations`, and the outputs for `cells` and `stations`, for runs `run_a` and `run_b`.
///
/// # Safety
///
/// Each pointer must be to as many elements as `compute` reads or writes, from `alloc`.
#[unsafe(no_mangle)]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn travel_times(
    arrivals: *const u16,
    cell: *const u16,
    exit_seconds: *const u16,
    meters: *const u16,
    n_exits: usize,
    starts: *const u32,
    origin: usize,
    cells: usize,
    stations: usize,
    mph: f64,
    run_a: usize,
    run_b: usize,
    times: *mut u8,
    exit_from: *mut u16,
    exit_minute: *mut u8,
    saved: *mut i8,
) {
    // SAFETY: the caller gives each as long as it is.
    unsafe {
        let exits = Exits {
            cell: slice::from_raw_parts(cell, n_exits),
            exit_seconds: slice::from_raw_parts(exit_seconds, n_exits),
            meters: slice::from_raw_parts(meters, n_exits),
            starts: slice::from_raw_parts(starts, stations + 1),
        };
        compute(
            slice::from_raw_parts(arrivals, RUNS * MINUTES * stations),
            &exits,
            origin,
            cells,
            stations,
            mph,
            &[run_a, run_b],
            TravelTimes {
                times: slice::from_raw_parts_mut(times, RUNS * (cells + stations)),
                exit_from: slice::from_raw_parts_mut(exit_from, RUNS * cells),
                exit_minute: slice::from_raw_parts_mut(exit_minute, RUNS * cells),
                saved: slice::from_raw_parts_mut(saved, cells + stations),
            },
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The minutes the second run saves, and each run's minutes, to the one cell, then station 1,
    /// where trains from station 0 reach station 1 `without` and `with` seconds after departing,
    /// every minute, and station 1 walks out to the cell in no time.
    fn saved_between(without: u16, with: u16) -> ([i8; 3], [u8; 3], [u8; 3]) {
        let (cells, stations) = (1, 2);
        let mut arrivals = vec![UNREACHED_SECONDS as u16; RUNS * MINUTES * stations];
        for (run, trip) in [(0, without), (1, with)] {
            for m in 0..MINUTES {
                let departs = 60 * m as u16;
                let at = (run * MINUTES + m) * stations;
                arrivals[at] = departs;
                arrivals[at + 1] = departs + trip;
            }
        }
        let exits = Exits {
            cell: &[0],
            exit_seconds: &[0],
            meters: &[0],
            starts: &[0, 0, 1],
        };
        let mut times = vec![UNREACHED; RUNS * (cells + stations)];
        let mut exit_from = vec![NO_STATION; RUNS * cells];
        let mut exit_minute = vec![0; RUNS * cells];
        let mut saved = vec![0; cells + stations];
        compute(
            &arrivals,
            &exits,
            0,
            cells,
            stations,
            3.0,
            &[0, 1],
            TravelTimes {
                times: &mut times,
                exit_from: &mut exit_from,
                exit_minute: &mut exit_minute,
                saved: &mut saved,
            },
        );
        let run = |r: usize| {
            let at = r * (cells + stations);
            [times[at], times[at + 1], times[at + 2]]
        };
        (saved.try_into().unwrap(), run(0), run(1))
    }

    #[test]
    fn saved_from_seconds_not_rounded_minutes() {
        // A second apart, across a minute: 10 and 9 minutes, rounded down, but none saved.
        let (saved, without, with) = saved_between(600, 599);
        assert_eq!((without, with), ([10, 0, 10], [9, 0, 9]));
        assert_eq!(saved, [0, 0, 0]);
        // 59 seconds apart, likewise.
        assert_eq!(saved_between(660, 601).0, [0, 0, 0]);
        // A minute apart, within a minute: 10 minutes either way, but one saved.
        assert_eq!(saved_between(659, 599).0, [1, 0, 1]);
        // Slower, rounded toward zero too.
        assert_eq!(saved_between(600, 719).0, [-1, 0, -1]);
    }

    #[test]
    fn nothing_saved_where_either_is_unreached() {
        let past = (MAX_SECONDS + 1) as u16;
        assert_eq!(saved_between(past, 600).0, [0, 0, 0]);
        assert_eq!(saved_between(600, past).0, [0, 0, 0]);
    }
}
