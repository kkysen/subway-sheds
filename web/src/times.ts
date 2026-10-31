// Travel times from a station, computed here from when its trains reach every station
// and the walks out of stations, so that walking out of them can be at any speed.
// See `src/subway_sheds/export.py` for the files.

// As `src/subway_sheds/travel_times.py` routes: departing each minute of an hour,
// walking out of a station for up to an hour.
const MINUTES = 60;
const MAX_EXIT_WALK_SECONDS = 60 * 60;
// As `MAX_TIME` there: what's reached later isn't.
const MAX_SECONDS = 2 * 60 * 60;
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

// Every one of `parts`' walks out of stations, by station.
export function mergeExits(parts: readonly Exits[], stations: number): Exits {
  const n = parts.reduce((sum, part) => sum + part.cell.length, 0);
  const cell = new Uint16Array(n);
  const exitSeconds = new Uint16Array(n);
  const meters = new Uint16Array(n);
  const starts = new Uint32Array(stations + 1);
  let at = 0;
  for (let s = 0; s < stations; s++) {
    starts[s] = at;
    for (const part of parts) {
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

// Read from `routes`: a journey's last leg's end, every minute it's taken,
// or for one walked from the origin, the minute and its walk,
// unreached if that's more than `MAX_SECONDS` after departing.
export function routesArrivals(routes: Routes, stations: number): Arrivals {
  const arrivals = new Uint16Array(RUNS * MINUTES * stations).fill(UNREACHED_SECONDS);
  for (let run = 0; run < RUNS; run++) {
    for (let s = 0; s < stations; s++) {
      const i = run * stations + s;
      let from = 0;
      for (let j = routes.starts[i] ?? 0; j < (routes.starts[i + 1] ?? 0); j++) {
        const to = (routes.minutes[j] ?? 0) + 1;
        const last = (routes.legStarts[j + 1] ?? 0) - 1;
        const walked = last < (routes.legStarts[j] ?? 0);
        for (let m = from; m < to; m++) {
          const clock = walked
            ? 60 * m + (routes.walks[j] ?? 0)
            : (routes.legs[LEG_FIELDS * last + 4] ?? 0);
          if (clock - 60 * m <= MAX_SECONDS) {
            arrivals[(run * MINUTES + m) * stations + s] = clock;
          }
        }
        from = to;
      }
    }
  }
  return arrivals;
}

// What the worker computes in WebAssembly (`web/times/src/lib.rs`):
// minutes from station `origin` to every cell, then every station, in each of the runs asked for,
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

// The seconds walking out of `station` to `cell` takes at `mph`, as the worker counts it,
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
  // How long each journey with no legs, walked from the station, walks, in seconds.
  readonly walks: Uint16Array;
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
  const walks = new Uint16Array(journeys);
  const walkView = new DataView(buffer, RUNS * stations + 2 * journeys);
  let walked = 0;
  for (let k = 0; k < journeys; k++) {
    if (legCounts[k] === 0) {
      walks[k] = walkView.getUint16(2 * walked++, true);
    }
  }
  const view = new DataView(buffer, RUNS * stations + 2 * journeys + 2 * walked);
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
  return { starts, minutes, legStarts, walks, legs };
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
