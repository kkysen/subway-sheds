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

// In a station's times, a cell not reached within the 90 minutes routed.
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

// Travel time in minutes, in the 10-minute bands the original subway isochrones used.
const travelTimeBands: readonly Band[] = [
  [0, "#1a9850"],
  [10, "#66bd63"],
  [20, "#a6d96a"],
  [30, "#fee08b"],
  [40, "#fdae61"],
  [60, "#f46d43"],
  [75, "#d73027"],
];

// The minutes routed out to, past which a cell isn't reached.
const MAX_MINUTES = 90;

const savedBands: readonly Band[] = [
  [1, "#deebf7"],
  [5, "#9ecae1"],
  [10, "#6baed6"],
  [15, "#3182bd"],
  [20, "#08519c"],
];

// What a cell not reached in a scenario counts as.
// Only a little past the last minute, so that the edge of what's reached is traced
// between cells, as edges between bands are, rather than along them.
const FAR = MAX_MINUTES + 10;

// Minutes saved past which the last band has no end, and which more is counted as,
// so that edges there are traced between cells too.
const MOST_SAVED = 25;

const bands = (view: View): readonly Band[] => (view === "saved" ? savedBands : travelTimeBands);
const bandsEnd = (view: View): number => (view === "saved" ? MOST_SAVED : MAX_MINUTES);

// The view's value for each cell of the raster, minutes or minutes saved,
// and which cells it has one for, 1 or 0: the land's, but for minutes saved,
// only those either scenario reaches. Where a scenario doesn't reach, minutes are `FAR`;
// a place only QueensLink reaches counts as saving the most.
function cellValues(
  grid: Grid,
  times: Uint8Array,
  day: Day,
  view: View,
): { values: Float64Array; shown: Float64Array } {
  const { width, height } = rasterSize(grid);
  const count = grid.cols.length;
  // Each day's times are every cell's in the baseline, then every cell's with QueensLink.
  const start = days.indexOf(day) * 2 * count;
  const values = new Float64Array(width * height);
  const shown = new Float64Array(width * height);
  const minutes = (time: number): number => (time === UNREACHED ? FAR : time);
  grid.cols.forEach((col, i) => {
    const baseline = times[start + i] ?? UNREACHED;
    const queenslink = times[start + count + i] ?? UNREACHED;
    const at = (grid.rows[i] ?? 0) * width + col;
    shown[at] = view !== "saved" || baseline !== UNREACHED || queenslink !== UNREACHED ? 1 : 0;
    if (view === "baseline") {
      values[at] = minutes(baseline);
    } else if (view === "queenslink") {
      values[at] = minutes(queenslink);
    } else {
      const saved = (baseline === UNREACHED ? Infinity : baseline) - minutes(queenslink);
      values[at] = Math.min(saved, MOST_SAVED);
    }
  });
  // A cell not shown that's next to one shown takes the average of those it's next to,
  // so that interpolating toward it near the edge doesn't pull the minutes there toward 0.
  const filled = values.slice();
  for (let row = 0; row < height; row++) {
    for (let col = 0; col < width; col++) {
      if (shown[row * width + col] === 1) {
        continue;
      }
      let sum = 0;
      let count = 0;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const y = row + dy;
          const x = col + dx;
          if (x >= 0 && x < width && y >= 0 && y < height && shown[y * width + x] === 1) {
            sum += values[y * width + x] ?? 0;
            count++;
          }
        }
      }
      if (count > 0) {
        filled[row * width + col] = sum / count;
      }
    }
  }
  return { values: filled, shown };
}

// How many times finer than the grid the bands are traced, so that their edges curve
// rather than turn at each cell.
const UPSAMPLE = 3;

// `values` at `UPSAMPLE` times the resolution, interpolated between cells' centers.
function upsample(values: Float64Array, width: number, height: number): Float64Array {
  const fineWidth = width * UPSAMPLE;
  const fineHeight = height * UPSAMPLE;
  const fine = new Float64Array(fineWidth * fineHeight);
  // A fine pixel's position in cells, and the cells either side of it, with its weight between.
  const between = (i: number, size: number): [number, number, number] => {
    const at = Math.min(Math.max((i + 0.5) / UPSAMPLE - 0.5, 0), size - 1);
    const low = Math.min(Math.floor(at), size - 2);
    return [Math.max(low, 0), Math.min(low + 1, size - 1), at - Math.max(low, 0)];
  };
  for (let y = 0; y < fineHeight; y++) {
    const [y0, y1, ty] = between(y, height);
    for (let x = 0; x < fineWidth; x++) {
      const [x0, x1, tx] = between(x, width);
      const value = (col: number, row: number): number => values[row * width + col] ?? 0;
      const south = value(x0, y0) * (1 - tx) + value(x1, y0) * tx;
      const north = value(x0, y1) * (1 - tx) + value(x1, y1) * tx;
      fine[y * fineWidth + x] = south * (1 - ty) + north * ty;
    }
  }
  return fine;
}

// How steeply, in minutes per cell, the bands fall off at the edge of the cells shown,
// for the edge to be traced along that between them and those not shown,
// whatever the minutes either side, rather than drawing every band between in a sliver.
const EDGE = 20;

// The view's bands as smooth shapes, each where its value is from the band's minutes
// up to the next band's, among the cells shown. Bands next to each other
// are traced along the same edge, so they meet without gaps or overlaps.
function bandFeatures(
  grid: Grid,
  { values, shown }: { values: Float64Array; shown: Float64Array },
  view: View,
): GeoJSON.FeatureCollection {
  const { width, height } = rasterSize(grid);
  const fine = upsample(values, width, height);
  const fineShown = upsample(shown, width, height);
  const fineWidth = width * UPSAMPLE;
  const contour = contours()
    .size([fineWidth, height * UPSAMPLE])
    .thresholds([0]);
  // A fine pixel's value is at its center, a half pixel in from its corner.
  const toMap = ([x, y]: number[]): [number, number] =>
    toLngLat(
      grid.x0 + ((x ?? 0) / UPSAMPLE - 0.5) * grid.spacing,
      grid.y0 + ((y ?? 0) / UPSAMPLE - 0.5) * grid.spacing,
    );
  const viewBands = bands(view);
  const features: GeoJSON.Feature[] = viewBands.map(([low, color], i) => {
    const high = viewBands[i + 1]?.[0] ?? bandsEnd(view);
    const inBand = fine.map((value, j) =>
      Math.min(value - low, high - value, ((fineShown[j] ?? 0) - 0.5) * EDGE),
    );
    const [shape] = contour(Array.from(inBand));
    return {
      type: "Feature",
      geometry: {
        type: "MultiPolygon",
        coordinates: (shape?.coordinates ?? []).map((polygon) =>
          polygon.map((ring) => ring.map(toMap)),
        ),
      },
      properties: { color },
    };
  });
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
  last.textContent = view === "saved" ? "" : String(MAX_MINUTES);
  labels.append(last);
  const note = document.createElement("p");
  note.textContent =
    view === "saved"
      ? "Minutes saved; places only QueensLink reaches are darkest."
      : "Minutes from the station, walking at the end.";
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
          cellValues(grid, selectedTimes, currentDay(), currentView()),
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
