// Computes travel times off the page's thread, in WebAssembly with SIMD from `web/times/`,
// which every browser has run since 2023.
import wasmUrl from "../times/target/wasm32-unknown-unknown/release/times.wasm?url";
import type { Arrivals, Exits, TravelTimes } from "./times";

// What the page asks: first the walks out of stations, once, then a station's times each time.
export type Request =
  | {
      readonly kind: "exits";
      readonly exits: Exits;
      readonly cells: number;
      readonly stations: number;
    }
  | {
      readonly kind: "times";
      readonly id: number;
      readonly arrivals: Arrivals;
      readonly origin: number;
      readonly mph: number;
      readonly runs: readonly [number, number];
    };

// And what it's told: the times asked for by `id`, or why they couldn't be computed.
export type Response =
  | { readonly id: number; readonly times: TravelTimes }
  | { readonly id: number; readonly error: string };

// `web/times/src/lib.rs`'s exports.
interface Times {
  readonly memory: WebAssembly.Memory;
  alloc(bytes: number): number;
  dealloc(at: number, bytes: number): void;
  travel_times(
    arrivals: number,
    cell: number,
    exitSeconds: number,
    meters: number,
    exits: number,
    starts: number,
    origin: number,
    cells: number,
    stations: number,
    mph: number,
    runA: number,
    runB: number,
    times: number,
    exitFrom: number,
    exitMinute: number,
    saved: number,
  ): void;
}

// As `RUNS` and `MINUTES` in `times.ts`.
const RUNS = 8;
const MINUTES = 60;

// Where each of the module's arrays is in its memory, allocated once for the walks given,
// and reused for every station's, and each's size, to free them when the walks change.
interface Laid {
  readonly cells: number;
  readonly stations: number;
  readonly exits: number;
  readonly arrivals: number;
  readonly cell: number;
  readonly exitSeconds: number;
  readonly meters: number;
  readonly starts: number;
  readonly times: number;
  readonly exitFrom: number;
  readonly exitMinute: number;
  readonly saved: number;
  // Each array's place and size, to free.
  readonly allocated: readonly (readonly [number, number])[];
}

const module = WebAssembly.instantiateStreaming(fetch(wasmUrl)).then(
  ({ instance }) => instance.exports as unknown as Times,
);

let laid: Promise<Laid | undefined> = Promise.resolve(undefined);

function layOut(times: Times, given: Exits, cells: number, stations: number): Laid {
  const n = given.cell.length;
  const allocated: [number, number][] = [];
  const alloc = (bytes: number): number => {
    const place = times.alloc(bytes);
    allocated.push([place, bytes]);
    return place;
  };
  const at = {
    cells,
    stations,
    exits: n,
    arrivals: alloc(2 * RUNS * MINUTES * stations),
    cell: alloc(2 * n),
    exitSeconds: alloc(2 * n),
    meters: alloc(2 * n),
    starts: alloc(4 * (stations + 1)),
    times: alloc(RUNS * (cells + stations)),
    exitFrom: alloc(2 * RUNS * cells),
    exitMinute: alloc(RUNS * cells),
    saved: alloc(cells + stations),
    allocated,
  };
  const memory = times.memory.buffer;
  new Uint16Array(memory, at.cell, n).set(given.cell);
  new Uint16Array(memory, at.exitSeconds, n).set(given.exitSeconds);
  new Uint16Array(memory, at.meters, n).set(given.meters);
  new Uint32Array(memory, at.starts, stations + 1).set(given.starts);
  return at;
}

// A station's times, in the module's memory, copied out to send.
function compute(
  times: Times,
  at: Laid,
  request: Extract<Request, { kind: "times" }>,
): TravelTimes {
  const { cells, stations } = at;
  let memory = times.memory.buffer;
  new Uint16Array(memory, at.arrivals, RUNS * MINUTES * stations).set(request.arrivals);
  new Uint8Array(memory, at.times, RUNS * (cells + stations)).fill(255);
  new Uint16Array(memory, at.exitFrom, RUNS * cells).fill(0xffff);
  new Uint8Array(memory, at.exitMinute, RUNS * cells).fill(0);
  new Int8Array(memory, at.saved, cells + stations).fill(0);
  times.travel_times(
    at.arrivals,
    at.cell,
    at.exitSeconds,
    at.meters,
    at.exits,
    at.starts,
    request.origin,
    cells,
    stations,
    request.mph,
    request.runs[0],
    request.runs[1],
    at.times,
    at.exitFrom,
    at.exitMinute,
    at.saved,
  );
  // It may have grown for its own scratch space, which replaces the buffer.
  memory = times.memory.buffer;
  return {
    times: new Uint8Array(memory, at.times, RUNS * (cells + stations)).slice(),
    exitFrom: new Uint16Array(memory, at.exitFrom, RUNS * cells).slice(),
    exitMinute: new Uint8Array(memory, at.exitMinute, RUNS * cells).slice(),
    saved: new Int8Array(memory, at.saved, cells + stations).slice(),
  };
}

self.onmessage = async (event: MessageEvent<Request>): Promise<void> => {
  const request = event.data;
  if (request.kind === "exits") {
    const { exits, cells, stations } = request;
    laid = Promise.all([module, laid]).then(([times, old]) => {
      for (const [place, bytes] of old?.allocated ?? []) {
        times.dealloc(place, bytes);
      }
      return layOut(times, exits, cells, stations);
    });
    return;
  }
  // Always answered, so that the page asks again rather than waiting forever.
  let result: TravelTimes;
  try {
    const times = await module;
    const at = await laid;
    if (at === undefined) {
      throw new Error("times asked for before the walks out of stations");
    }
    result = compute(times, at, request);
  } catch (error) {
    const response: Response = { id: request.id, error: String(error) };
    self.postMessage(response);
    return;
  }
  const response: Response = { id: request.id, times: result };
  self.postMessage(response, {
    transfer: [
      result.times.buffer,
      result.exitFrom.buffer,
      result.exitMinute.buffer,
      result.saved.buffer,
    ],
  });
};
