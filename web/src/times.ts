// Travel times from a station, computed here from when its trains reach every station
// and the walks out of stations, so that walking out of them can be at any speed.
// See `src/subway_sheds/export.py` for the files.

// As `src/subway_sheds/travel_times.py` routes: departing each minute of an hour,
// keeping trips of up to 2 hours, walking out of a station for up to an hour.
const MINUTES = 60;
const MAX_SECONDS = 120 * 60;
const MAX_EXIT_WALK_SECONDS = 60 * 60;
// The median over the window's departures, as routing picks it.
const MEDIAN = Math.ceil(0.5 * MINUTES) - 1;
// The runs each station's arrivals have, by day, then fare, then scenario.
const RUNS = 8;

// In arrivals, a station not reached; in times, a cell or station not reached.
const UNREACHED_SECONDS = 0xffff;
export const UNREACHED = 255;

const METERS_PER_SECOND_PER_MPH = 1609.344 / 3600;

// Every walk out of a station to a cell, by station.
export interface Exits {
  readonly cell: Uint16Array;
  readonly exitSeconds: Uint16Array;
  readonly meters: Uint16Array;
  // Where each station's walks start, and the last one's end.
  readonly starts: Uint32Array;
}

export function parseExits(buffer: ArrayBuffer, stations: number): Exits {
  const view = new DataView(buffer);
  const n = view.getUint32(0, true);
  const column = (i: number): Uint16Array => {
    const out = new Uint16Array(n);
    const at = 4 + i * 2 * n;
    for (let k = 0; k < n; k++) {
      out[k] = view.getUint16(at + 2 * k, true);
    }
    return out;
  };
  const station = column(0);
  const starts = new Uint32Array(stations + 1);
  for (let k = 0; k < n; k++) {
    starts[(station[k] ?? 0) + 1] = k + 1;
  }
  // Stations with no walks start where the last one before them ended.
  for (let s = 1; s <= stations; s++) {
    starts[s] = Math.max(starts[s] ?? 0, starts[s - 1] ?? 0);
  }
  return { cell: column(1), exitSeconds: column(2), meters: column(3), starts };
}

// `near`'s walks out of stations and `far`'s, by station.
export function mergeExits(near: Exits, far: Exits, stations: number): Exits {
  const n = near.cell.length + far.cell.length;
  const cell = new Uint16Array(n);
  const exitSeconds = new Uint16Array(n);
  const meters = new Uint16Array(n);
  const starts = new Uint32Array(stations + 1);
  let at = 0;
  for (let s = 0; s < stations; s++) {
    starts[s] = at;
    for (const part of [near, far]) {
      const from = part.starts[s] ?? 0;
      const to = part.starts[s + 1] ?? 0;
      cell.set(part.cell.subarray(from, to), at);
      exitSeconds.set(part.exitSeconds.subarray(from, to), at);
      meters.set(part.meters.subarray(from, to), at);
      at += to - from;
    }
  }
  starts[stations] = at;
  return { cell, exitSeconds, meters, starts };
}

// When trains reach each station each minute, in seconds from the window's start:
// by run, then minute, then station.
export type Arrivals = Uint16Array;

export function parseArrivals(buffer: ArrayBuffer, stations: number): Arrivals {
  const bytes = new Uint8Array(buffer);
  const view = new DataView(buffer);
  const counts = bytes.subarray(0, RUNS * stations);
  const changes = counts.reduce((sum, c) => sum + c, 0);
  let minuteAt = RUNS * stations;
  let clockAt = minuteAt + changes;
  const arrivals = new Uint16Array(RUNS * MINUTES * stations);
  // Each change's minute, from those since the one before.
  const minutes = new Uint8Array(MINUTES);
  for (let run = 0; run < RUNS; run++) {
    for (let s = 0; s < stations; s++) {
      const count = counts[run * stations + s] ?? 0;
      for (let c = 0; c < count; c++) {
        minutes[c] = (c > 0 ? (minutes[c - 1] ?? 0) : 0) + (bytes[minuteAt + c] ?? 0);
      }
      // Its time, from the change since the one before, wrapping around.
      let clock = 0;
      for (let c = 0; c < count; c++) {
        clock = (clock + view.getUint16(clockAt + 2 * c, true)) & 0xffff;
        const to = c + 1 < count ? (minutes[c + 1] ?? MINUTES) : MINUTES;
        for (let m = minutes[c] ?? 0; m < to; m++) {
          arrivals[(run * MINUTES + m) * stations + s] = clock;
        }
      }
      minuteAt += count;
      clockAt += 2 * count;
    }
  }
  return arrivals;
}

// Minutes from station `origin` to every cell, then every station, in each of `runs`,
// each `cells + stations` long, in the order they're in the arrivals,
// walking out of stations at `mph`: each cell's median over the minutes of its quickest trip,
// by train and walking out of a station, or walking from `origin` itself.
// Each station's own, its median arrival on its platforms; `origin`'s, on the first train there.
// The runs not asked for are left unreached.
// With each cell's station walked out of on its median trip, by run, `origin` if walked from it.
export interface TravelTimes {
  readonly times: Uint8Array;
  readonly exitFrom: Uint16Array;
  // And the minute of the window its median trip departs, by run.
  readonly exitMinute: Uint8Array;
}
export const NO_STATION = 0xffff;

export function travelTimes(
  arrivals: Arrivals,
  exits: Exits,
  origin: number,
  cells: number,
  stations: number,
  mph: number,
  runs: readonly number[],
): TravelTimes {
  const speed = mph * METERS_PER_SECOND_PER_MPH;
  // Each walk out's seconds, through the station's entrance and on the streets.
  const out = new Uint16Array(exits.meters.length);
  for (let k = 0; k < out.length; k++) {
    const walk = Math.floor((exits.meters[k] ?? 0) / speed);
    out[k] = walk <= MAX_EXIT_WALK_SECONDS ? walk + (exits.exitSeconds[k] ?? 0) : UNREACHED_SECONDS;
  }
  // Walking straight there from `origin`, out of its entrances without its time to the platforms.
  const direct = new Uint16Array(cells).fill(UNREACHED_SECONDS);
  for (let k = exits.starts[origin] ?? 0; k < (exits.starts[origin + 1] ?? 0); k++) {
    const walk = Math.floor((exits.meters[k] ?? 0) / speed);
    const cell = exits.cell[k] ?? 0;
    if (walk <= MAX_EXIT_WALK_SECONDS && walk < (direct[cell] ?? UNREACHED_SECONDS)) {
      direct[cell] = walk;
    }
  }
  const stride = cells + stations;
  const times = new Uint8Array(RUNS * stride).fill(UNREACHED);
  const exitFrom = new Uint16Array(RUNS * cells).fill(NO_STATION);
  const exitMinute = new Uint8Array(RUNS * cells);
  // The earliest time, from the window's start, each cell is reached by train this minute,
  // and the station walked out of to it.
  const best = new Uint16Array(cells);
  const bestFrom = new Uint16Array(cells);
  // Each cell's trip each minute, cell-major, in the high half, and its station in the low,
  // so that sorting them sorts by trip and keeps each's station with it.
  const trips = new Uint32Array(cells * MINUTES);
  const eachTrip = new Uint32Array(MINUTES);
  const each = new Uint16Array(MINUTES);
  const minutes = (seconds: number): number =>
    seconds <= MAX_SECONDS ? Math.floor(seconds / 60) : UNREACHED;
  for (const run of runs) {
    best.fill(UNREACHED_SECONDS);
    // Latest departure first: departing earlier, every station is reached at the same time or
    // earlier, so each minute's earliest are the next minute's, lowered by the stations that changed.
    for (let m = MINUTES - 1; m >= 0; m--) {
      const at = (run * MINUTES + m) * stations;
      const next = m + 1 < MINUTES ? at + stations : -1;
      for (let s = 0; s < stations; s++) {
        const clock = arrivals[at + s] ?? UNREACHED_SECONDS;
        if (clock === UNREACHED_SECONDS || (next >= 0 && arrivals[next + s] === clock)) {
          continue;
        }
        for (let k = exits.starts[s] ?? 0; k < (exits.starts[s + 1] ?? 0); k++) {
          const t = clock + (out[k] ?? UNREACHED_SECONDS);
          const cell = exits.cell[k] ?? 0;
          if (t < (best[cell] ?? UNREACHED_SECONDS)) {
            best[cell] = t;
            bestFrom[cell] = s;
          }
        }
      }
      const departs = 60 * m;
      for (let cell = 0; cell < cells; cell++) {
        const reached = best[cell] ?? UNREACHED_SECONDS;
        const byTrain = reached >= UNREACHED_SECONDS ? reached : reached - departs;
        const walked = direct[cell] ?? UNREACHED_SECONDS;
        trips[cell * MINUTES + m] =
          walked <= byTrain
            ? walked * 0x10000 + origin
            : byTrain * 0x10000 + (bestFrom[cell] ?? NO_STATION);
      }
    }
    const first = run * stride;
    for (let cell = 0; cell < cells; cell++) {
      eachTrip.set(trips.subarray(cell * MINUTES, (cell + 1) * MINUTES));
      const median = eachTrip.sort()[MEDIAN] ?? 0xffffffff;
      const trip = Math.floor(median / 0x10000);
      times[first + cell] = minutes(trip);
      if (trip !== UNREACHED_SECONDS) {
        exitFrom[run * cells + cell] = median % 0x10000;
        // The first minute of the median trip, as `route` sorts ties.
        exitMinute[run * cells + cell] = Math.max(
          trips.subarray(cell * MINUTES, (cell + 1) * MINUTES).indexOf(median),
          0,
        );
      }
    }
    for (let s = 0; s < stations; s++) {
      for (let m = 0; m < MINUTES; m++) {
        const clock = arrivals[(run * MINUTES + m) * stations + s] ?? UNREACHED_SECONDS;
        each[m] = clock === UNREACHED_SECONDS ? clock : clock - 60 * m;
      }
      times[first + cells + s] = minutes(each.sort()[MEDIAN] ?? UNREACHED_SECONDS);
    }
  }
  return { times, exitFrom, exitMinute };
}

// The minute of the window departing at which `station` is reached at its median in `run`,
// whose journey `Routes` has, or `undefined` if it's not reached.
export function medianDeparture(
  arrivals: Arrivals,
  run: number,
  station: number,
  stations: number,
): number | undefined {
  const trips: [number, number][] = [];
  for (let m = 0; m < MINUTES; m++) {
    const clock = arrivals[(run * MINUTES + m) * stations + station] ?? UNREACHED_SECONDS;
    trips.push([clock === UNREACHED_SECONDS ? clock : clock - 60 * m, m]);
  }
  // Sorted as `route` sorts them, by trip, then minute.
  trips.sort(([a, m], [b, n]) => a - b || m - n);
  const [trip, minute] = trips[MEDIAN] ?? [UNREACHED_SECONDS, 0];
  return trip === UNREACHED_SECONDS ? undefined : minute;
}

// The seconds walking out of `station` to `cell` takes at `mph`, as `travelTimes` counts it,
// or from `origin` itself, without going in to its platforms; `undefined` if it's not walked.
export function walkOutSeconds(
  exits: Exits,
  station: number,
  cell: number,
  mph: number,
  fromOrigin: boolean,
): number | undefined {
  const speed = mph * METERS_PER_SECOND_PER_MPH;
  let best: number | undefined;
  for (let k = exits.starts[station] ?? 0; k < (exits.starts[station + 1] ?? 0); k++) {
    if (exits.cell[k] !== cell) {
      continue;
    }
    const walk = Math.floor((exits.meters[k] ?? 0) / speed);
    if (walk > MAX_EXIT_WALK_SECONDS) {
      continue;
    }
    const seconds = fromOrigin ? walk : walk + (exits.exitSeconds[k] ?? 0);
    best = Math.min(best ?? seconds, seconds);
  }
  return best;
}

// A leg of a journey: a ride on `pattern`, boarding at its stop `a` and getting off at its stop `b`,
// or with `pattern` `WALK_LEG`, a walk from station `a` to station `b`,
// from `start` to `end`, in seconds from the window's start.
export interface RouteLeg {
  readonly pattern: number;
  readonly a: number;
  readonly b: number;
  readonly start: number;
  readonly end: number;
}
// Each leg's fields in `Routes.legs`.
const LEG_FIELDS = 5;
export const WALK_LEG = 0xffff;

// The journeys from a station to every station in each run,
// each read back departing at its minute and taken every minute before until the one before it.
export interface Routes {
  // Where each run's station's journeys start, run-major, and the last one's end.
  readonly starts: Uint32Array;
  // Each journey's minute, ascending for each station.
  readonly minutes: Uint8Array;
  // Where each journey's legs start, and the last one's end.
  readonly legStarts: Uint32Array;
  readonly legs: Uint16Array;
}

export function parseRoutes(buffer: ArrayBuffer, stations: number): Routes {
  const bytes = new Uint8Array(buffer);
  const counts = bytes.subarray(0, RUNS * stations);
  const starts = new Uint32Array(RUNS * stations + 1);
  for (let i = 0; i < counts.length; i++) {
    starts[i + 1] = (starts[i] ?? 0) + (counts[i] ?? 0);
  }
  const journeys = starts[RUNS * stations] ?? 0;
  // Each journey's minute, from the one before but a station's first.
  const minutes = bytes.slice(RUNS * stations, RUNS * stations + journeys);
  for (let i = 0; i < RUNS * stations; i++) {
    for (let k = (starts[i] ?? 0) + 1; k < (starts[i + 1] ?? 0); k++) {
      minutes[k] = (minutes[k] ?? 0) + (minutes[k - 1] ?? 0);
    }
  }
  const legCounts = bytes.subarray(RUNS * stations + journeys, RUNS * stations + 2 * journeys);
  const legStarts = new Uint32Array(journeys + 1);
  for (let k = 0; k < journeys; k++) {
    legStarts[k + 1] = (legStarts[k] ?? 0) + (legCounts[k] ?? 0);
  }
  const view = new DataView(buffer, RUNS * stations + 2 * journeys);
  const legs = new Uint16Array(LEG_FIELDS * (legStarts[journeys] ?? 0));
  for (let k = 0; k < legs.length; k++) {
    legs[k] = view.getUint16(2 * k, true);
  }
  // When each leg starts and ends, from when it starts after the leg before, or the window for the first,
  // and how long it takes.
  for (let j = 0; j < journeys; j++) {
    let end = 0;
    for (let k = legStarts[j] ?? 0; k < (legStarts[j + 1] ?? 0); k++) {
      const at = LEG_FIELDS * k;
      const start = (k > (legStarts[j] ?? 0) ? end : 0) + (legs[at + 3] ?? 0);
      end = start + (legs[at + 4] ?? 0);
      legs[at + 3] = start;
      legs[at + 4] = end;
    }
  }
  return { starts, minutes, legStarts, legs };
}

// The legs of the journey to `station` in `run` departing at `minute`, as `route` picks it:
// the first read back at that minute or after, none if it isn't reached by train.
export function routeTo(
  routes: Routes,
  run: number,
  station: number,
  stations: number,
  minute: number,
): RouteLeg[] {
  const i = run * stations + station;
  let journey = -1;
  for (let j = routes.starts[i] ?? 0; j < (routes.starts[i + 1] ?? 0); j++) {
    if ((routes.minutes[j] ?? 0) >= minute) {
      journey = j;
      break;
    }
  }
  const legs: RouteLeg[] = [];
  if (journey < 0) {
    return legs;
  }
  for (let k = routes.legStarts[journey] ?? 0; k < (routes.legStarts[journey + 1] ?? 0); k++) {
    const at = LEG_FIELDS * k;
    legs.push({
      pattern: routes.legs[at] ?? WALK_LEG,
      a: routes.legs[at + 1] ?? 0,
      b: routes.legs[at + 2] ?? 0,
      start: routes.legs[at + 3] ?? 0,
      end: routes.legs[at + 4] ?? 0,
    });
  }
  return legs;
}
