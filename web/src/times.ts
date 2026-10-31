// Travel times from a station, computed here from when its trains reach every station
// and the walks out of stations, so that walking out of them can be at any speed.
// See `src/subway_sheds/export.py` for the files.

// As `src/subway_sheds/travel_times.py` routes: departing each minute of an hour,
// keeping trips of up to 2 hours, walking out of a station for up to half an hour.
const MINUTES = 60;
const MAX_SECONDS = 120 * 60;
const MAX_EXIT_WALK_SECONDS = 30 * 60;
// The median over the window's departures, as routing picks it.
const MEDIAN = Math.ceil(0.5 * MINUTES) - 1;
// The days and scenarios each station's arrivals have, in their order.
const RUNS = 4;

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
  for (let run = 0; run < RUNS; run++) {
    for (let s = 0; s < stations; s++) {
      const count = counts[run * stations + s] ?? 0;
      for (let c = 0; c < count; c++) {
        const from = bytes[minuteAt + c] ?? 0;
        const to = c + 1 < count ? (bytes[minuteAt + c + 1] ?? MINUTES) : MINUTES;
        const clock = view.getUint16(clockAt + 2 * c, true);
        for (let m = from; m < to; m++) {
          arrivals[(run * MINUTES + m) * stations + s] = clock;
        }
      }
      minuteAt += count;
      clockAt += 2 * count;
    }
  }
  return arrivals;
}

// Minutes from station `origin` to every cell, then every station, in each run, one after another,
// walking out of stations at `mph`: each cell's median over the minutes of its quickest trip,
// by train and walking out of a station, or walking from `origin` itself.
export function travelTimes(
  arrivals: Arrivals,
  exits: Exits,
  origin: number,
  cells: number,
  stations: number,
  mph: number,
): Uint8Array {
  const speed = mph * METERS_PER_SECOND_PER_MPH;
  const walk = new Uint16Array(exits.meters.length);
  for (let k = 0; k < walk.length; k++) {
    const seconds = Math.floor((exits.meters[k] ?? 0) / speed);
    walk[k] = seconds <= MAX_EXIT_WALK_SECONDS ? seconds : UNREACHED_SECONDS;
  }
  const stride = cells + stations;
  const times = new Uint8Array(RUNS * stride).fill(UNREACHED);
  const best = new Uint16Array(MINUTES * cells);
  const each = new Uint16Array(MINUTES);
  const minutes = (seconds: number): number =>
    seconds <= MAX_SECONDS ? Math.floor(seconds / 60) : UNREACHED;
  for (let run = 0; run < RUNS; run++) {
    best.fill(UNREACHED_SECONDS);
    for (let m = 0; m < MINUTES; m++) {
      const row = best.subarray(m * cells, (m + 1) * cells);
      // Walking straight there, out of `origin`'s entrances without its time to the platforms.
      for (let k = exits.starts[origin] ?? 0; k < (exits.starts[origin + 1] ?? 0); k++) {
        const cell = exits.cell[k] ?? 0;
        row[cell] = Math.min(row[cell] ?? UNREACHED_SECONDS, walk[k] ?? UNREACHED_SECONDS);
      }
      const at = (run * MINUTES + m) * stations;
      for (let s = 0; s < stations; s++) {
        const clock = arrivals[at + s] ?? UNREACHED_SECONDS;
        if (clock === UNREACHED_SECONDS) {
          continue;
        }
        const ride = clock - 60 * m;
        for (let k = exits.starts[s] ?? 0; k < (exits.starts[s + 1] ?? 0); k++) {
          const w = walk[k] ?? UNREACHED_SECONDS;
          if (w === UNREACHED_SECONDS) {
            continue;
          }
          const cell = exits.cell[k] ?? 0;
          const t = ride + (exits.exitSeconds[k] ?? 0) + w;
          if (t < (row[cell] ?? UNREACHED_SECONDS)) {
            row[cell] = t;
          }
        }
      }
    }
    const out = run * stride;
    for (let cell = 0; cell < cells; cell++) {
      for (let m = 0; m < MINUTES; m++) {
        each[m] = best[m * cells + cell] ?? UNREACHED_SECONDS;
      }
      times[out + cell] = minutes(each.sort()[MEDIAN] ?? UNREACHED_SECONDS);
    }
    // Each station's own: reaching its platforms by train, or being there already.
    for (let s = 0; s < stations; s++) {
      for (let m = 0; m < MINUTES; m++) {
        const clock = arrivals[(run * MINUTES + m) * stations + s] ?? UNREACHED_SECONDS;
        each[m] = clock === UNREACHED_SECONDS ? clock : clock - 60 * m;
      }
      times[out + cells + s] = s === origin ? 0 : minutes(each.sort()[MEDIAN] ?? UNREACHED_SECONDS);
    }
  }
  return times;
}
