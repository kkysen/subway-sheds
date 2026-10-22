import "maplibre-gl/dist/maplibre-gl.css";
import {
  type ExpressionSpecification,
  type GeoJSONSource,
  type MapLayerMouseEvent,
  Map as MapLibreMap,
  setWorkerUrl,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";

// MapLibre loads its worker from a file of its own, which it looks for next to the bundle;
// importing it with `?url` is what gets Vite to emit it at all.
setWorkerUrl(workerUrl);

type Scenario = "baseline" | "queenslink";
type View = Scenario | "saved";

const views: readonly View[] = ["baseline", "queenslink", "saved"];

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

// The cells either scenario reaches, with each scenario's minutes where it does.
function cellFeatures(
  squares: readonly GeoJSON.Polygon[],
  times: Uint8Array,
): GeoJSON.FeatureCollection {
  const count = squares.length;
  const features: GeoJSON.Feature[] = [];
  squares.forEach((geometry, i) => {
    const baseline = times[i] ?? UNREACHED;
    const queenslink = times[count + i] ?? UNREACHED;
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

// Travel time in minutes, in the 10-minute bands the original subway isochrones used.
const travelTimeColor = (scenario: Scenario): ExpressionSpecification => [
  "step",
  ["get", scenario],
  "#1a9850",
  10,
  "#66bd63",
  20,
  "#a6d96a",
  30,
  "#fee08b",
  40,
  "#fdae61",
  60,
  "#f46d43",
  75,
  "#d73027",
];

// Cells only QueensLink reaches have no baseline time; count them as saving the most.
const minutesSaved: ExpressionSpecification = [
  "-",
  ["coalesce", ["get", "baseline"], 999],
  ["coalesce", ["get", "queenslink"], 999],
];

const savedColor: ExpressionSpecification = [
  "interpolate",
  ["linear"],
  minutesSaved,
  1,
  "#deebf7",
  10,
  "#6baed6",
  20,
  "#08519c",
];

const fillColor = (view: View): ExpressionSpecification =>
  view === "saved" ? savedColor : travelTimeColor(view);

// Only the cells the view has something to show for.
const filter = (view: View): ExpressionSpecification =>
  view === "saved" ? [">", minutesSaved, 0] : ["has", view];

function element<T extends Element>(selector: string, type: new () => T): T {
  const found = document.querySelector(selector);
  if (!(found instanceof type)) {
    throw new Error(`no ${type.name} matching ${selector}`);
  }
  return found;
}

const controls = element("#controls", HTMLFormElement);
const stationName = element("#station-name", HTMLElement);

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

const [grid, stations] = await Promise.all([
  fetchJson<Grid>("grid.json"),
  fetchJson<Station[]>("stations.json"),
]);
const squares = cellSquares(grid);
const stationsById = new Map(stations.map((station) => [station.id, station]));

// Broad Channel, whose trip to most of the city QueensLink shortens.
const DEFAULT_STATION = "H04";

// The station last asked for, so that a slower fetch for an earlier one can't replace it.
let selected = "";

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
  map.getSource<GeoJSONSource>("cells")?.setData(cellFeatures(squares, times));
  map.setFilter("selected-station", ["==", ["get", "id"], id]);
  const only = station.scenarios.length === 1 ? " (QueensLink only)" : "";
  stationName.textContent = `${station.name}${only}`;
  history.replaceState(null, "", `#${encodeURIComponent(id)}`);
}

function stationFromHash(): string {
  const id = decodeURIComponent(location.hash.slice(1));
  return stationsById.has(id) ? id : DEFAULT_STATION;
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

  map.on("click", "stations", (event: MapLayerMouseEvent) => {
    const id: unknown = event.features?.[0]?.properties["id"];
    if (typeof id === "string") {
      void selectStation(id);
    }
  });
  map.on("mouseenter", "stations", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "stations", () => {
    map.getCanvas().style.cursor = "";
  });

  void selectStation(stationFromHash());
}

addEventListener("hashchange", () => {
  void selectStation(stationFromHash());
});

controls.addEventListener("change", () => {
  const view = currentView();
  map.setFilter("cells", filter(view));
  map.setPaintProperty("cells", "fill-color", fillColor(view));
});
