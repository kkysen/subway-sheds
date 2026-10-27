import "maplibre-gl/dist/maplibre-gl.css";
import {
  type ExpressionSpecification,
  FullscreenControl,
  Map as MapLibreMap,
  type MapMouseEvent,
  type MapTouchEvent,
  type Point,
  setWorkerUrl,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { BandsLayer } from "./bands";

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

async function fetchTimesUncached(station: string): Promise<Uint8Array> {
  const response = await fetch(data(`times/${encodeURIComponent(station)}.bin`));
  if (!response.ok) {
    throw new Error(`${response.status} fetching times from ${station}`);
  }
  return new Uint8Array(await response.arrayBuffer());
}

// How many stations' times are kept, about 86 KB each,
// so that pointing across the city doesn't keep all of them.
const CACHED_STATIONS = 64;

// The times fetched or being fetched, least recently asked for first,
// so that a click on a station already fetched for being pointed at waits on that fetch.
const timesCache = new Map<string, Promise<Uint8Array>>();

function fetchTimes(station: string): Promise<Uint8Array> {
  let times = timesCache.get(station);
  timesCache.delete(station);
  if (times === undefined) {
    times = fetchTimesUncached(station);
    // Forgotten if it fails, so that it's tried again next time.
    times.catch(() => {
      if (timesCache.get(station) === times) {
        timesCache.delete(station);
      }
    });
  }
  timesCache.set(station, times);
  for (const oldest of timesCache.keys()) {
    if (timesCache.size <= CACHED_STATIONS) {
      break;
    }
    timesCache.delete(oldest);
  }
  return times;
}

// Fetched ahead of being picked, for being pointed at or touched.
function prefetchTimes(station: string): void {
  fetchTimes(station).catch(() => {});
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

// Stations new with QueensLink, among the others in white.
const NEW_STATION_COLOR = "#ff8319";

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
  const stationKey = document.createElement("p");
  const station = document.createElement("span");
  station.className = "legend-station";
  const newStation = document.createElement("span");
  newStation.className = "legend-station";
  newStation.style.background = NEW_STATION_COLOR;
  stationKey.append(station, " Station ", newStation, " New with QueensLink");
  return [bar, labels, note, stationKey];
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
const fullMap = element("#full-map", HTMLAnchorElement);

// Narrow screens, as on phones, where the controls and credits open would cover much of the map.
// The same width as `style.css`'s narrow layout.
// Or short ones, as on phones turned sideways, where the controls open are 300px of under 500.
const small = window.matchMedia("(max-width: 600px), (max-height: 500px)").matches;

// Collapsed to start with on small screens.
element("#controls details", HTMLDetailsElement).open = !small;

// Only when embedded, as on thequeenslink.org, since it's already the full map otherwise.
fullMap.hidden = window.top === window.self;

// The controls kept across reloads and in links, by their inputs' name.
const savedControls = ["day", "view"] as const;

// Prefixed, since every page on kkysen.github.io shares the same storage.
const storageKey = (name: string): string => `subway-sheds:${name}`;

// The URL's hash: the station, then the controls, as in `#H04&day=saturday&view=saved`,
// so that links from before the controls were in it, just `#H04`, still work.
function parseHash(): { readonly station: string; readonly params: URLSearchParams } {
  const [station = "", ...params] = location.hash.slice(1).split("&");
  return { station: decodeURIComponent(station), params: new URLSearchParams(params.join("&")) };
}

function savedValue(name: string): string | null {
  try {
    return localStorage.getItem(storageKey(name));
  } catch {
    // Storage can be unavailable, as in some private windows.
    return null;
  }
}

// From the link if it says, else as they were last left, else as they start.
function restoreControls(): void {
  const { params } = parseHash();
  for (const name of savedControls) {
    const value = params.get(name) ?? savedValue(name);
    const inputs = controls.elements.namedItem(name);
    // Checks the radio with that value, if there still is one.
    if (value !== null && inputs instanceof RadioNodeList) {
      inputs.value = value;
    }
  }
}

function saveControls(): void {
  const data = new FormData(controls);
  try {
    for (const name of savedControls) {
      localStorage.setItem(storageKey(name), String(data.get(name)));
    }
  } catch {
    // Not saved, as when storage is unavailable.
  }
}

function showView(view: View): void {
  legend.replaceChildren(...legendItems(view));
}

// Shows what's outside the map for the controls as they are.
function showControls(): void {
  dayName.textContent = dayNames[currentDay()];
  showView(currentView());
}

restoreControls();
showControls();

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
  // Behind a button on small screens, as MapLibre already does on its own when the map is 640px wide or less.
  attributionControl: small ? { compact: true } : {},
});

// Collapsed to start with on small screens too, as MapLibre would only do once the map's dragged.
// Once loaded, since it opens them once the style's attributions are in.
if (small) {
  map.once("load", () => {
    element(".maplibregl-ctrl-attrib", HTMLElement).classList.remove("maplibregl-compact-show");
  });
}

// Our credits, in this order, before the map data's.
// They're not given to MapLibre as custom attributions, since it sorts those in with the rest by length,
// so they're put first here instead, and again each time it redraws its attributions over them.
const credits = document.createElement("span");
credits.innerHTML = [
  '<a href="https://github.com/kkysen/subway-sheds">Map and code by Khyber Sen</a>',
  "QueensLink GTFS by Franklin Tang",
  'Inspiration and shoreline from Chris Whong’s <a href="https://github.com/chriswhong/nyc-subway-isochrones">nyc-subway-isochrones</a>',
].join(" | ");
const attribution = element(".maplibregl-ctrl-attrib-inner", HTMLElement);
function prependCredits(): void {
  if (attribution.firstChild !== credits) {
    attribution.prepend(credits);
    if (credits.nextSibling !== null) {
      credits.after(" | ");
    }
  }
}
prependCredits();
new MutationObserver(prependCredits).observe(attribution, { childList: true });

// The whole page rather than just the map, which would leave the controls out.
map.addControl(new FullscreenControl({ container: document.body }));

// Listened for before awaiting the data, which the map can finish loading before.
const loaded = new Promise((resolve) => map.once("load", resolve));

const [grid, stations, lines] = await Promise.all([
  fetchJson<Grid>("grid.json"),
  fetchJson<Station[]>("stations.json"),
  fetchJson<GeoJSON.FeatureCollection>("lines.json"),
]);
const { width, height } = rasterSize(grid);
const bandsLayer = new BandsLayer("bands", grid, width, height);
const stationsById = new Map(stations.map((station) => [station.id, station]));

// Broad Channel, whose trip to most of the city QueensLink shortens.
const DEFAULT_STATION = "H04";

// The station last asked for, so that a slower fetch for an earlier one can't replace it.
let selected = "";
// The times from the station shown.
let selectedTimes: Uint8Array | undefined;

function drawCells(): void {
  if (selectedTimes !== undefined) {
    const view = currentView();
    bandsLayer.setClasses(cellClasses(grid, selectedTimes, currentDay(), view), classColors(view));
  }
}

async function selectStation(id: string): Promise<void> {
  const station = stationsById.get(id);
  if (station === undefined) {
    return;
  }
  selected = id;
  // Shown picked straight away, so that the click feels taken while its times load.
  map.setFilter("selected-station", ["==", ["get", "id"], id]);
  const only = station.scenarios.length === 1 ? " (QueensLink only)" : "";
  stationName.textContent = `${station.name}${only}`;
  writeHash();
  const times = await fetchTimes(id);
  if (selected !== id) {
    return;
  }
  selectedTimes = times;
  drawCells();
}

function stationFromHash(): string {
  const { station } = parseHash();
  return stationsById.has(station) ? station : DEFAULT_STATION;
}

// Replaced rather than pushed, so that every click doesn't add to the history.
function writeHash(): void {
  const data = new FormData(controls);
  const params = new URLSearchParams(savedControls.map((name) => [name, String(data.get(name))]));
  history.replaceState(null, "", `#${encodeURIComponent(selected)}&${params}`);
  // Opens as it's shown here.
  fullMap.href = location.href;
}

function applyControls(): void {
  showControls();
  map.setFilter("lines", linesFilter(currentView()));
  drawCells();
}

// How far from a station, in pixels, a click or tap still picks it,
// since the dots are only a few pixels across when zoomed out.
const HIT_RADIUS = 12;

// The station nearest the point, of those within reach of it.
function stationAt(event: { readonly point: Point }): string | undefined {
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
  map.addLayer(bandsLayer, firstLabels);

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
      "circle-color": ["case", ["get", "new"], NEW_STATION_COLOR, "#ffffff"],
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
    const id = stationAt(event);
    map.getCanvas().style.cursor = id === undefined ? "" : "pointer";
    if (id !== undefined) {
      prefetchTimes(id);
    }
  });
  // On a phone, the touch starts a little before the tap it ends in.
  map.on("touchstart", (event: MapTouchEvent) => {
    const id = event.points.length === 1 ? stationAt(event) : undefined;
    if (id !== undefined) {
      prefetchTimes(id);
    }
  });

  void selectStation(stationFromHash());
}

// As when a link to another station or view is followed from the page itself.
addEventListener("hashchange", () => {
  restoreControls();
  applyControls();
  void selectStation(stationFromHash());
});

controls.addEventListener("change", () => {
  saveControls();
  writeHash();
  applyControls();
});
