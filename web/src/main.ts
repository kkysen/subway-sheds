import "maplibre-gl/dist/maplibre-gl.css";
import { contours } from "d3-contour";
import {
  type ExpressionSpecification,
  type GeoJSONSource,
  Map as MapLibreMap,
  type MapMouseEvent,
  setWorkerUrl,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";

// MapLibre loads its worker from a file of its own, which it looks for next to the bundle;
// importing it with `?url` is what gets Vite to emit it at all.
setWorkerUrl(workerUrl);

type Scenario = "baseline" | "queenslink";
type View = Scenario | "saved";

const views: readonly View[] = ["baseline", "queenslink", "saved"];

// In the order the pipeline writes them, as `Day` lists them in `src/subway_sheds/scenarios.py`.
const days = ["weekday", "saturday"] as const;
type Day = (typeof days)[number];

function isDay(value: unknown): value is Day {
  return days.some((day) => day === value);
}

const dayNames: Record<Day, string> = {
  weekday: "in the weekday morning peak",
  saturday: "on Saturday midday",
};

function isView(value: unknown): value is View {
  return views.some((view) => view === value);
}

// As the pipeline writes them; see `src/subway_sheds/export.py`.
interface Grid {
  readonly x0: number;
  readonly y0: number;
  readonly spacing: number;
  readonly cols: readonly number[];
  readonly rows: readonly number[];
}

interface Station {
  readonly id: string;
  readonly name: string;
  readonly lon: number;
  readonly lat: number;
  readonly scenarios: readonly Scenario[];
}

// In a station's times, a cell not reached within the `ROUTED_MINUTES` routed.
const UNREACHED = 255;

const data = (path: string): string => `${import.meta.env.BASE_URL}data/${path}`;

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(data(path));
  if (!response.ok) {
    throw new Error(`${response.status} fetching ${path}`);
  }
  // The pipeline writes these files, so they're trusted to have the shape asked for.
  const json: T = await response.json();
  return json;
}

async function fetchTimes(station: string): Promise<Uint8Array> {
  const response = await fetch(data(`times/${encodeURIComponent(station)}.bin`));
  if (!response.ok) {
    throw new Error(`${response.status} fetching times from ${station}`);
  }
  return new Uint8Array(await response.arrayBuffer());
}

const EARTH_RADIUS = 6378137;

function toLngLat(x: number, y: number): [number, number] {
  const lng = (x / EARTH_RADIUS) * (180 / Math.PI);
  const lat = Math.atan(Math.sinh(y / EARTH_RADIUS)) * (180 / Math.PI);
  return [lng, lat];
}

// The grid as a raster, `width` cells by `height`, cell (0, 0) at its southwest corner.
function rasterSize(grid: Grid): { width: number; height: number } {
  return { width: Math.max(...grid.cols) + 1, height: Math.max(...grid.rows) + 1 };
}

// Each band the view is drawn in, from its minutes up to the next band's, and its color.
type Band = readonly [number, string];

// Where bands of minutes, traveled or saved, start, the same for both,
// so that, e.g., 15 minutes there and 15 minutes saved fall on the same edge.
const BAND_STARTS = [5, 10, 15, 20, 30, 45, 60, 75] as const;

// The minutes travel times are drawn out to.
const MAX_MINUTES = 90;

// The minutes routed out to, past which a cell isn't reached: past `MAX_MINUTES`,
// so that there's saying how much QueensLink saves places that take longer than that without it.
const ROUTED_MINUTES = 120;

// Travel time in minutes, from green to red.
const travelTimeBands: readonly Band[] = [
  [0, "#1a9850"],
  [BAND_STARTS[0], "#66bd63"],
  [BAND_STARTS[1], "#a6d96a"],
  [BAND_STARTS[2], "#d9ef8b"],
  // Deeper than the palette's `#ffffbf`, which over the map at 60% is all but white.
  [BAND_STARTS[3], "#fdf28d"],
  [BAND_STARTS[4], "#fee08b"],
  [BAND_STARTS[5], "#fdae61"],
  [BAND_STARTS[6], "#f46d43"],
  [BAND_STARTS[7], "#d73027"],
];

// Minutes saved, from a minute, from light to dark blue.
const savedBands: readonly Band[] = [
  [1, "#deebf7"],
  [BAND_STARTS[0], "#c6dbef"],
  [BAND_STARTS[1], "#9ecae1"],
  [BAND_STARTS[2], "#6baed6"],
  [BAND_STARTS[3], "#4292c6"],
  [BAND_STARTS[4], "#2171b5"],
  [BAND_STARTS[5], "#08519c"],
  [BAND_STARTS[6], "#08458a"],
  [BAND_STARTS[7], "#08306b"],
  [MAX_MINUTES, "#041f47"],
];

// Places only QueensLink reaches within `ROUTED_MINUTES`, which there's no saying how much it saves.
const QUEENSLINK_ONLY_COLOR = "#6a3d9a";

const bands = (view: View): readonly Band[] => (view === "saved" ? savedBands : travelTimeBands);

// The colors the view's cells are drawn in, the class of cell each is for being its index.
const classColors = (view: View): string[] => [
  ...bands(view).map(([, color]) => color),
  ...(view === "saved" ? [QUEENSLINK_ONLY_COLOR] : []),
];

// Cells with nothing to show: off the land, not reached, or, for minutes saved, not saving any.
const NOTHING = -1;

// The band of `bands` that `value` is in, or `NOTHING` before the first.
function bandOf(value: number, bands: readonly Band[]): number {
  let band = NOTHING;
  bands.forEach(([start], i) => {
    if (value >= start) {
      band = i;
    }
  });
  return band;
}

// Each cell of the raster's class: the index of its color in `classColors`, or `NOTHING`.
function cellClasses(grid: Grid, times: Uint8Array, day: Day, view: View): Int8Array {
  const { width, height } = rasterSize(grid);
  const count = grid.cols.length;
  // Each day's times are every cell's in the baseline, then every cell's with QueensLink.
  const start = days.indexOf(day) * 2 * count;
  const classes = new Int8Array(width * height).fill(NOTHING);
  grid.cols.forEach((col, i) => {
    const baseline = times[start + i] ?? UNREACHED;
    const queenslink = times[start + count + i] ?? UNREACHED;
    const at = (grid.rows[i] ?? 0) * width + col;
    if (view !== "saved") {
      const minutes = view === "baseline" ? baseline : queenslink;
      classes[at] = minutes > MAX_MINUTES ? NOTHING : bandOf(minutes, travelTimeBands);
    } else if (queenslink !== UNREACHED) {
      classes[at] =
        baseline === UNREACHED ? savedBands.length : bandOf(baseline - queenslink, savedBands);
    }
  });
  return classes;
}

// How many times finer than the grid the bands are traced, so that their edges curve
// rather than turn at each cell.
const UPSAMPLE = 3;

// How much of a cell's class it is, 1 or 0, for each cell, a little blurred into its neighbors,
// then interpolated between cells' centers at `UPSAMPLE` times the resolution.
// It's run for every class each time a station is picked, so its loops are kept free of
// closures and allocations, with indices clamped to the edges ahead of time.
function membership(classes: Int8Array, which: number, width: number, height: number) {
  const is = new Uint8Array(width * height);
  for (let j = 0; j < is.length; j++) {
    is[j] = classes[j] === which ? 1 : 0;
  }
  // Blurred by 1-2-1 across and then up, so that edges round off rather than run cell by cell.
  const across = new Float32Array(width * height);
  for (let row = 0; row < height; row++) {
    const start = row * width;
    for (let col = 0; col < width; col++) {
      const left = start + Math.max(col - 1, 0);
      const right = start + Math.min(col + 1, width - 1);
      across[start + col] = ((is[left] ?? 0) + 2 * (is[start + col] ?? 0) + (is[right] ?? 0)) / 4;
    }
  }
  const blurred = new Float32Array(width * height);
  for (let row = 0; row < height; row++) {
    const start = row * width;
    const below = Math.max(row - 1, 0) * width;
    const above = Math.min(row + 1, height - 1) * width;
    for (let col = 0; col < width; col++) {
      blurred[start + col] =
        ((across[below + col] ?? 0) + 2 * (across[start + col] ?? 0) + (across[above + col] ?? 0)) /
        4;
    }
  }

  const fineWidth = width * UPSAMPLE;
  const fineHeight = height * UPSAMPLE;
  const fine = new Float32Array(fineWidth * fineHeight);
  // Each fine pixel's position in cells, along one axis: the cells either side of it,
  // and its weight between them.
  const between = (size: number) => {
    const low = new Int32Array(size * UPSAMPLE);
    const high = new Int32Array(size * UPSAMPLE);
    const weight = new Float64Array(size * UPSAMPLE);
    for (let i = 0; i < low.length; i++) {
      const position = Math.min(Math.max((i + 0.5) / UPSAMPLE - 0.5, 0), size - 1);
      const cell = Math.min(Math.floor(position), Math.max(size - 2, 0));
      low[i] = cell;
      high[i] = Math.min(cell + 1, size - 1);
      weight[i] = position - cell;
    }
    return { low, high, weight };
  };
  const xs = between(width);
  const ys = between(height);
  for (let y = 0; y < fineHeight; y++) {
    const south = (ys.low[y] ?? 0) * width;
    const north = (ys.high[y] ?? 0) * width;
    const ty = ys.weight[y] ?? 0;
    for (let x = 0; x < fineWidth; x++) {
      const x0 = xs.low[x] ?? 0;
      const x1 = xs.high[x] ?? 0;
      const tx = xs.weight[x] ?? 0;
      const southValue = (blurred[south + x0] ?? 0) * (1 - tx) + (blurred[south + x1] ?? 0) * tx;
      const northValue = (blurred[north + x0] ?? 0) * (1 - tx) + (blurred[north + x1] ?? 0) * tx;
      fine[y * fineWidth + x] = southValue * (1 - ty) + northValue * ty;
    }
  }
  return fine;
}

// The view's classes as smooth shapes. Each fine pixel goes to the class it's most of,
// which nothing to show competes for too, so that the shapes meet without gaps or overlaps,
// and a band is drawn only where some cell is in it.
function bandFeatures(grid: Grid, classes: Int8Array, view: View): GeoJSON.FeatureCollection {
  const { width, height } = rasterSize(grid);
  const fineWidth = width * UPSAMPLE;
  const fineHeight = height * UPSAMPLE;
  const colors = classColors(view);
  const present = [NOTHING, ...colors.keys()].filter((which) => classes.includes(which));
  // Computed once each, as they're needed both to rank the classes and to trace each one.
  const memberships = new Map(
    present.map((which) => [which, membership(classes, which, width, height)]),
  );

  // Each fine pixel's most and next most class, and how much of each it is.
  const most = new Int8Array(fineWidth * fineHeight).fill(NOTHING);
  const mostAmount = new Float32Array(fineWidth * fineHeight).fill(-1);
  const nextAmount = new Float32Array(fineWidth * fineHeight).fill(-1);
  for (const [which, amounts] of memberships) {
    amounts.forEach((amount, j) => {
      if (amount > (mostAmount[j] ?? -1)) {
        nextAmount[j] = mostAmount[j] ?? -1;
        mostAmount[j] = amount;
        most[j] = which;
      } else if (amount > (nextAmount[j] ?? -1)) {
        nextAmount[j] = amount;
      }
    });
  }

  const contour = contours().size([fineWidth, fineHeight]).thresholds([0]);
  // A fine pixel's amount is at its center, a half pixel in from its corner.
  const toMap = ([x, y]: number[]): [number, number] =>
    toLngLat(
      grid.x0 + ((x ?? 0) / UPSAMPLE - 0.5) * grid.spacing,
      grid.y0 + ((y ?? 0) / UPSAMPLE - 0.5) * grid.spacing,
    );
  const features: GeoJSON.Feature[] = [];
  for (const [which, amounts] of memberships) {
    if (which === NOTHING) {
      continue;
    }
    // By how much the pixel is more of this class than any other, or less than the most.
    // Filled by a plain loop, as `Array.from` and `Float64Array.from` call back per pixel.
    const ahead = new Float64Array(amounts.length);
    for (let j = 0; j < ahead.length; j++) {
      const amount = amounts[j] ?? 0;
      ahead[j] = most[j] === which ? amount - (nextAmount[j] ?? 0) : amount - (mostAmount[j] ?? 0);
    }
    // Its types ask for an array, but it only indexes into the values, as a typed array allows.
    const [shape] = contour(ahead as unknown as number[]);
    features.push({
      type: "Feature",
      geometry: {
        type: "MultiPolygon",
        coordinates: (shape?.coordinates ?? []).map((polygon) =>
          polygon.map((ring) => ring.map(toMap)),
        ),
      },
      properties: { color: colors[which] },
    });
  }
  return { type: "FeatureCollection", features };
}

function legendItems(view: View): HTMLElement[] {
  const viewBands = bands(view);
  const bar = document.createElement("div");
  bar.className = "legend-bands";
  const labels = document.createElement("div");
  labels.className = "legend-labels";
  for (const [minutes, color] of viewBands) {
    const band = document.createElement("span");
    band.style.background = color;
    bar.append(band);
    const label = document.createElement("span");
    label.textContent = String(minutes);
    labels.append(label);
  }
  const last = document.createElement("span");
  last.textContent = view === "saved" ? "+" : String(MAX_MINUTES);
  labels.append(last);
  const note = document.createElement("p");
  if (view === "saved") {
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.background = QUEENSLINK_ONLY_COLOR;
    note.append(
      "Minutes saved. ",
      swatch,
      ` Reached only with QueensLink, within ${ROUTED_MINUTES} minutes.`,
    );
  } else {
    note.textContent = "Minutes from the station, walking at the end.";
  }
  return [bar, labels, note];
}

// The lines of the scenario the view shows, with QueensLink's for what it saves.
const linesFilter = (view: View): ExpressionSpecification => [
  "has",
  view === "saved" ? "queenslink" : view,
];

function element<T extends Element>(selector: string, type: new () => T): T {
  const found = document.querySelector(selector);
  if (!(found instanceof type)) {
    throw new Error(`no ${type.name} matching ${selector}`);
  }
  return found;
}

const controls = element("#controls", HTMLFormElement);
const stationName = element("#station-name", HTMLElement);
const dayName = element("#day-name", HTMLElement);
const legend = element("#legend", HTMLElement);

function showView(view: View): void {
  legend.replaceChildren(...legendItems(view));
}

showView(currentView());

function currentDay(): Day {
  const value = new FormData(controls).get("day");
  if (!isDay(value)) {
    throw new Error(`unknown day ${String(value)}`);
  }
  return value;
}

function currentView(): View {
  const value = new FormData(controls).get("view");
  if (!isView(value)) {
    throw new Error(`unknown view ${String(value)}`);
  }
  return value;
}

const map = new MapLibreMap({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/positron",
  center: [-73.87, 40.7],
  zoom: 10.5,
});

// Listened for before awaiting the data, which the map can finish loading before.
const loaded = new Promise((resolve) => map.once("load", resolve));

const [grid, stations, lines] = await Promise.all([
  fetchJson<Grid>("grid.json"),
  fetchJson<Station[]>("stations.json"),
  fetchJson<GeoJSON.FeatureCollection>("lines.json"),
]);
const stationsById = new Map(stations.map((station) => [station.id, station]));

// Broad Channel, whose trip to most of the city QueensLink shortens.
const DEFAULT_STATION = "H04";

// The station last asked for, so that a slower fetch for an earlier one can't replace it.
let selected = "";
// The times from the station shown.
let selectedTimes: Uint8Array | undefined;

function drawCells(): void {
  if (selectedTimes !== undefined) {
    map
      .getSource<GeoJSONSource>("cells")
      ?.setData(
        bandFeatures(
          grid,
          cellClasses(grid, selectedTimes, currentDay(), currentView()),
          currentView(),
        ),
      );
  }
}

async function selectStation(id: string): Promise<void> {
  const station = stationsById.get(id);
  if (station === undefined) {
    return;
  }
  selected = id;
  const times = await fetchTimes(id);
  if (selected !== id) {
    return;
  }
  selectedTimes = times;
  drawCells();
  map.setFilter("selected-station", ["==", ["get", "id"], id]);
  const only = station.scenarios.length === 1 ? " (QueensLink only)" : "";
  stationName.textContent = `${station.name}${only}`;
  history.replaceState(null, "", `#${encodeURIComponent(id)}`);
}

function stationFromHash(): string {
  const id = decodeURIComponent(location.hash.slice(1));
  return stationsById.has(id) ? id : DEFAULT_STATION;
}

// How far from a station, in pixels, a click or tap still picks it,
// since the dots are only a few pixels across when zoomed out.
const HIT_RADIUS = 12;

// The station nearest the point, of those within reach of it.
function stationAt(event: MapMouseEvent): string | undefined {
  const { x, y } = event.point;
  let nearest: string | undefined;
  let nearestDistance = Infinity;
  const near = map.queryRenderedFeatures(
    [
      [x - HIT_RADIUS, y - HIT_RADIUS],
      [x + HIT_RADIUS, y + HIT_RADIUS],
    ],
    { layers: ["stations"] },
  );
  for (const feature of near) {
    const id: unknown = feature.properties["id"];
    const station = typeof id === "string" ? stationsById.get(id) : undefined;
    if (station === undefined) {
      continue;
    }
    const distance = event.point.dist(map.project([station.lon, station.lat]));
    if (distance < nearestDistance) {
      nearest = station.id;
      nearestDistance = distance;
    }
  }
  return nearest;
}

await loaded;
{
  const view = currentView();
  // Under the labels, so street and place names stay readable.
  const firstLabels = map.getStyle().layers.find((layer) => layer.type === "symbol")?.id;
  map.addSource("cells", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer(
    {
      id: "cells",
      type: "fill",
      source: "cells",
      paint: { "fill-color": ["get", "color"], "fill-opacity": 0.6 },
    },
    firstLabels,
  );
  // The edges between bands, so that each stands out from the next.
  map.addLayer(
    {
      id: "band-edges",
      type: "line",
      source: "cells",
      paint: { "line-color": "#000000", "line-opacity": 0.25, "line-width": 0.75 },
    },
    firstLabels,
  );

  map.addSource("lines", { type: "geojson", data: lines });
  map.addLayer({
    id: "lines",
    type: "line",
    source: "lines",
    filter: linesFilter(view),
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": ["get", "color"],
      "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 14, 4],
    },
  });

  map.addSource("stations", {
    type: "geojson",
    data: {
      type: "FeatureCollection",
      features: stations.map((station) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [station.lon, station.lat] },
        properties: { id: station.id, name: station.name, new: station.scenarios.length === 1 },
      })),
    },
  });
  map.addLayer({
    id: "stations",
    type: "circle",
    source: "stations",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 14, 6],
      "circle-color": ["case", ["get", "new"], "#ff8319", "#ffffff"],
      "circle-stroke-color": "#333333",
      "circle-stroke-width": 1,
    },
  });
  map.addLayer({
    id: "selected-station",
    type: "circle",
    source: "stations",
    filter: ["==", ["get", "id"], ""],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 6, 14, 10],
      "circle-color": "rgba(0, 0, 0, 0)",
      "circle-stroke-color": "#000000",
      "circle-stroke-width": 3,
    },
  });

  map.on("click", (event: MapMouseEvent) => {
    const id = stationAt(event);
    if (id !== undefined) {
      void selectStation(id);
    }
  });
  map.on("mousemove", (event: MapMouseEvent) => {
    map.getCanvas().style.cursor = stationAt(event) === undefined ? "" : "pointer";
  });

  void selectStation(stationFromHash());
}

addEventListener("hashchange", () => {
  void selectStation(stationFromHash());
});

controls.addEventListener("change", () => {
  const view = currentView();
  dayName.textContent = dayNames[currentDay()];
  map.setFilter("lines", linesFilter(view));
  showView(view);
  drawCells();
});
