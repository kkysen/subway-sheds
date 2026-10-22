import "maplibre-gl/dist/maplibre-gl.css";
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

// Each cell's square, which is a square because the grid is laid out in Web Mercator.
function cellSquares(grid: Grid): GeoJSON.Polygon[] {
  const half = grid.spacing / 2;
  return grid.cols.map((col, i) => {
    const x = grid.x0 + col * grid.spacing;
    const y = grid.y0 + (grid.rows[i] ?? 0) * grid.spacing;
    return {
      type: "Polygon",
      coordinates: [
        [
          toLngLat(x - half, y - half),
          toLngLat(x + half, y - half),
          toLngLat(x + half, y + half),
          toLngLat(x - half, y + half),
          toLngLat(x - half, y - half),
        ],
      ],
    };
  });
}

// The cells either scenario reaches on `day`, with each scenario's minutes where it does.
function cellFeatures(
  squares: readonly GeoJSON.Polygon[],
  times: Uint8Array,
  day: Day,
): GeoJSON.FeatureCollection {
  const count = squares.length;
  // Each day's times are every cell's in the baseline, then every cell's with QueensLink.
  const start = days.indexOf(day) * 2 * count;
  const features: GeoJSON.Feature[] = [];
  squares.forEach((geometry, i) => {
    const baseline = times[start + i] ?? UNREACHED;
    const queenslink = times[start + count + i] ?? UNREACHED;
    if (baseline === UNREACHED && queenslink === UNREACHED) {
      return;
    }
    const properties: Partial<Record<Scenario, number>> = {};
    if (baseline !== UNREACHED) {
      properties.baseline = baseline;
    }
    if (queenslink !== UNREACHED) {
      properties.queenslink = queenslink;
    }
    features.push({ type: "Feature", geometry, properties });
  });
  return { type: "FeatureCollection", features };
}

// Travel time in minutes, in the 10-minute bands the original subway isochrones used,
// each band's color from its lowest minute on.
const travelTimeBands: readonly (readonly [number, string])[] = [
  [0, "#1a9850"],
  [10, "#66bd63"],
  [20, "#a6d96a"],
  [30, "#fee08b"],
  [40, "#fdae61"],
  [60, "#f46d43"],
  [75, "#d73027"],
];

const travelTimeColor = (scenario: Scenario): ExpressionSpecification => [
  "step",
  ["get", scenario],
  travelTimeBands[0]?.[1] ?? "",
  ...travelTimeBands.slice(1).flat(),
];

// Cells only QueensLink reaches have no baseline time; count them as saving the most.
const minutesSaved: ExpressionSpecification = [
  "-",
  ["coalesce", ["get", "baseline"], 999],
  ["coalesce", ["get", "queenslink"], 999],
];

// Minutes saved, and the color at each, between which colors blend.
const savedStops: readonly (readonly [number, string])[] = [
  [1, "#deebf7"],
  [10, "#6baed6"],
  [20, "#08519c"],
];

const savedColor: ExpressionSpecification = [
  "interpolate",
  ["linear"],
  minutesSaved,
  ...savedStops.flat(),
];

// The minutes routed out to, past which a cell isn't reached.
const MAX_MINUTES = 90;

function legendItems(view: View): HTMLElement[] {
  if (view === "saved") {
    const bar = document.createElement("div");
    bar.className = "legend-bar";
    const last = savedStops.at(-1)?.[0] ?? 1;
    const first = savedStops[0]?.[0] ?? 0;
    bar.style.background = `linear-gradient(to right, ${savedStops
      .map(([minutes, color]) => `${color} ${((minutes - first) / (last - first)) * 100}%`)
      .join(", ")})`;
    const labels = document.createElement("div");
    labels.className = "legend-labels";
    for (const [minutes] of savedStops) {
      const label = document.createElement("span");
      label.textContent = minutes === last ? `${minutes}+` : String(minutes);
      labels.append(label);
    }
    const note = document.createElement("p");
    note.textContent = "Minutes saved; places only QueensLink reaches are darkest.";
    return [bar, labels, note];
  }
  const bar = document.createElement("div");
  bar.className = "legend-bands";
  const labels = document.createElement("div");
  labels.className = "legend-labels";
  for (const [minutes, color] of travelTimeBands) {
    const band = document.createElement("span");
    band.style.background = color;
    bar.append(band);
    const label = document.createElement("span");
    label.textContent = String(minutes);
    labels.append(label);
  }
  const last = document.createElement("span");
  last.textContent = String(MAX_MINUTES);
  labels.append(last);
  const note = document.createElement("p");
  note.textContent = "Minutes from the station, walking at the end.";
  return [bar, labels, note];
}

const fillColor = (view: View): ExpressionSpecification =>
  view === "saved" ? savedColor : travelTimeColor(view);

// Only the cells the view has something to show for.
const filter = (view: View): ExpressionSpecification =>
  view === "saved" ? [">", minutesSaved, 0] : ["has", view];

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
const squares = cellSquares(grid);
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
      ?.setData(cellFeatures(squares, selectedTimes, currentDay()));
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
      filter: filter(view),
      paint: { "fill-color": fillColor(view), "fill-opacity": 0.6 },
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

controls.addEventListener("change", (event) => {
  if (event.target instanceof HTMLInputElement && event.target.name === "day") {
    dayName.textContent = dayNames[currentDay()];
    drawCells();
    return;
  }
  const view = currentView();
  map.setFilter("cells", filter(view));
  map.setPaintProperty("cells", "fill-color", fillColor(view));
  map.setFilter("lines", linesFilter(view));
  showView(view);
});
