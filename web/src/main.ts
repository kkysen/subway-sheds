import "@fontsource-variable/nunito-sans";
import "maplibre-gl/dist/maplibre-gl.css";
import {
  type CircleLayerSpecification,
  type ExpressionSpecification,
  FullscreenControl,
  type LngLat,
  Map as MapLibreMap,
  type MapMouseEvent,
  type MapTouchEvent,
  type Point,
  Popup,
  type SymbolLayerSpecification,
  setWorkerUrl,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { BandsLayer, EARTH_CIRCUMFERENCE } from "./bands";
import {
  type Arrivals,
  type ExitWalks,
  exitWalkTo,
  medianDeparture,
  mergeExits,
  NO_STATION,
  parseExits,
  parseExitWalks,
  parseRoutes,
  type RouteLeg,
  type Routes,
  routesArrivals,
  routeTo,
  UNREACHED,
  WALK_LEG,
  walkOutSeconds,
} from "./times";
import type { Request as TimesRequest, Response as TimesResponse } from "./times.worker";

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
  weekday: "around 8 am on a weekday",
  saturday: "around 12 pm on a Saturday",
};

// Shorter, for the summary shown with the controls collapsed,
// each shorter than the last for when the one before doesn't fit.
const shortDayNames: Record<Day, readonly string[]> = {
  weekday: ["weekday @ 8 am", "wkdy @ 8 am", "wkdy 8 am"],
  saturday: ["Saturday @ 12 pm", "Sat @ 12 pm", "Sat 12 pm"],
};

const shortViewNames: Record<View, readonly string[]> = {
  baseline: ["without QueensLink", "w/o QL", "w/o QL"],
  queenslink: ["with QueensLink", "w/ QL", "w/ QL"],
  saved: ["minutes saved", "min saved", "min saved"],
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
  // Its complex, the stations changed between without paying again.
  readonly complex: string;
  // The complexes changed to free from it, though out of the system.
  readonly free?: readonly string[];
}

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

// Fetched first, as reading a station's routes needs how many stations there are.
const stationList = fetchJson<Station[]>("stations.json");

// Whether the browser decompresses zstd itself; its `DecompressionStream` throws if not.
const nativeZstd = ((): boolean => {
  try {
    new DecompressionStream("zstd" as CompressionFormat);
    return true;
  } catch {
    return false;
  }
})();

// The WebAssembly decoder for browsers without zstd, fetched and started only the first time one's needed.
let zstdDecoder: Promise<import("zstddec").ZSTDDecoder> | undefined;

// Decompresses a zstd file the pipeline wrote, as GitHub Pages sends them as they are,
// with the browser's own decompression if it has zstd, else a WebAssembly decoder.
async function unzstd(response: Response): Promise<ArrayBuffer> {
  if (nativeZstd && response.body !== null) {
    const stream = response.body.pipeThrough(new DecompressionStream("zstd" as CompressionFormat));
    return new Response(stream).arrayBuffer();
  }
  zstdDecoder ??= import("zstddec").then(async ({ ZSTDDecoder }) => {
    const decoder = new ZSTDDecoder();
    await decoder.init();
    return decoder;
  });
  const [compressed, decoder] = await Promise.all([response.arrayBuffer(), zstdDecoder]);
  // Copied out of the decoder's memory; the size is in each file's frame, as the pipeline writes it.
  return new Uint8Array(decoder.decode(new Uint8Array(compressed))).buffer;
}

async function fetchBinary(path: string): Promise<ArrayBuffer> {
  const response = await fetch(data(path));
  if (!response.ok) {
    throw new Error(`${response.status} fetching ${path}`);
  }
  return unzstd(response);
}

// What a station's times and routes are made from.
interface FromStation {
  readonly arrivals: Arrivals;
  readonly routes: Routes;
}

async function fetchTimesUncached(station: string): Promise<FromStation> {
  const id = encodeURIComponent(station);
  const [buffer, list] = await Promise.all([fetchBinary(`routes/${id}.bin`), stationList]);
  const routes = parseRoutes(buffer, list.length);
  return { arrivals: routesArrivals(routes, list.length), routes };
}

// How many stations' arrivals are kept, about 240 KB each unpacked,
// so that pointing across the city doesn't keep all of them.
const CACHED_STATIONS = 64;

// The times fetched or being fetched, least recently asked for first,
// so that a click on a station already fetched for being pointed at waits on that fetch.
const timesCache = new Map<string, Promise<FromStation>>();

function fetchTimes(station: string): Promise<FromStation> {
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

// How long the mouse stays on a station before it's prefetched,
// so that sweeping across the map doesn't fetch every station it passes.
const HOVER_PREFETCH_MS = 100;
let hovered: string | undefined;
let hoverPrefetch: number | undefined;

// Prefetches `station` once the mouse has stayed on it, or stops waiting to if `undefined`.
function hoverStation(station: string | undefined): void {
  if (station === hovered) {
    return;
  }
  hovered = station;
  clearTimeout(hoverPrefetch);
  hoverPrefetch =
    station === undefined
      ? undefined
      : window.setTimeout(() => prefetchTimes(station), HOVER_PREFETCH_MS);
}

// The grid as a raster, `width` cells by `height`, cell (0, 0) at its southwest corner.
function rasterSize(grid: Grid): { width: number; height: number } {
  return { width: Math.max(...grid.cols) + 1, height: Math.max(...grid.rows) + 1 };
}

// Each band the view is drawn in, from its minutes up to the next band's, and its color.
type Band = readonly [number, string];

// Where bands of minutes traveled start.
// Few of them, so that each band is easy to tell from the next.
const BAND_STARTS = [15, 30, 45, 60, 75, 90, 105] as const;

// The minutes routed out to, past which a cell isn't reached, and travel times are drawn out to,
// so that past it, there's no saying how much QueensLink saves places.
const ROUTED_MINUTES = 120;
const MAX_MINUTES = ROUTED_MINUTES;

// Travel time in minutes, from light to dark through QueensLink's orange, `#f48335`, to red, as QueensLink's own maps are,
// lighter to darker all the way, so that the order reads without telling hues apart.
const travelTimeBands: readonly Band[] = [
  [0, "#f8dbc9"],
  [BAND_STARTS[0], "#f6be99"],
  [BAND_STARTS[1], "#f9a66e"],
  [BAND_STARTS[2], "#f48335"],
  [BAND_STARTS[3], "#dc5f27"],
  [BAND_STARTS[4], "#c44027"],
  [BAND_STARTS[5], "#a3292a"],
  [BAND_STARTS[6], "#7f1d2b"],
];

// Minutes saved, from a minute, from light teal to QueensLink's green, `#24685b`, as QueensLink's own maps are.
// Finer than travel times' bands, and finest where most places are:
// QueensLink saves half of all trips 3 minutes or less, 9 in 10 under about 15,
// at most 81 minutes, and an hour or more for at most a tenth of a percent of them.
const savedBands: readonly Band[] = [
  [1, "#8cd1c6"],
  [5, "#6ebfb0"],
  [10, "#62b5a5"],
  [15, "#55aa9a"],
  [20, "#449586"],
  [30, "#3a8578"],
  [45, "#31796c"],
  [60, "#24685b"],
];

// Places only QueensLink reaches within `ROUTED_MINUTES`, which there's no saying how much it saves.
// QueensLink's orange, apart from the greens by more than red and green, which some can't tell apart.
const QUEENSLINK_ONLY_COLOR = "#f48335";

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

// In a station's times, each scenario's: every cell's, then every station's, as `stations.json` lists them.
const timesStride = (grid: Grid): number => grid.cols.length + stations.length;

// Each cell of the raster's class: the index of its color in `classColors`, or `NOTHING`.
function cellClasses(
  grid: Grid,
  times: Uint8Array,
  saved: Int8Array,
  day: Day,
  view: View,
): Int8Array {
  const { width, height } = rasterSize(grid);
  const stride = timesStride(grid);
  // Each run's times are every cell's and station's, the baseline's, then with QueensLink.
  const start = baselineRun(day) * stride;
  const classes = new Int8Array(width * height).fill(NOTHING);
  grid.cols.forEach((col, i) => {
    const baseline = times[start + i] ?? UNREACHED;
    const queenslink = times[start + stride + i] ?? UNREACHED;
    const at = (grid.rows[i] ?? 0) * width + col;
    if (view !== "saved") {
      const minutes = view === "baseline" ? baseline : queenslink;
      classes[at] = minutes > MAX_MINUTES ? NOTHING : bandOf(minutes, travelTimeBands);
    } else if (queenslink !== UNREACHED) {
      classes[at] = baseline === UNREACHED ? savedBands.length : bandOf(saved[i] ?? 0, savedBands);
    }
  });
  return classes;
}

// In a station's minutes saved, one only QueensLink reaches, which there's no saying how much it saves.
const ONLY_QUEENSLINK = -2;

// The minutes from the selected station to `station` itself, in the view shown:
// the trip's, or those saved, `ONLY_QUEENSLINK`, or `NOTHING` where there's none to say.
function stationMinutes(station: Station): number {
  if (selectedTimes === undefined || selectedSaved === undefined) {
    return NOTHING;
  }
  const stride = timesStride(grid);
  const at = baselineRun() * stride + grid.cols.length + stations.indexOf(station);
  const baseline = selectedTimes[at] ?? UNREACHED;
  const queenslink = selectedTimes[at + stride] ?? UNREACHED;
  const view = currentView();
  if (view !== "saved") {
    const trip = view === "baseline" ? baseline : queenslink;
    return trip === UNREACHED ? NOTHING : trip;
  }
  if (queenslink === UNREACHED) {
    return NOTHING;
  }
  const saved = selectedSaved[grid.cols.length + stations.indexOf(station)] ?? 0;
  return baseline === UNREACHED ? ONLY_QUEENSLINK : Math.max(saved, 0);
}

// What the cells of class `band` are, as said when they're pointed at or tapped.
function describeBand(band: number, view: View): string {
  if (view === "saved" && band === savedBands.length) {
    return `Reached only with QueensLink, within ${ROUTED_MINUTES} minutes`;
  }
  const viewBands = bands(view);
  const [start = 0] = viewBands[band] ?? [];
  const end = viewBands[band + 1]?.[0] ?? (view === "saved" ? undefined : MAX_MINUTES);
  const minutes = end === undefined ? `${start}+ minutes` : `${start} to ${end} minutes`;
  if (view === "saved") {
    return `${minutes} saved by QueensLink`;
  }
  const scenario = view === "baseline" ? "without QueensLink" : "with QueensLink";
  return `${minutes} from ${selectedName} ${scenario}`;
}

// A band's key in the legend, highlighting it on the map when pointed at, and pinning it when clicked or tapped.
function legendBand(band: number, color: string, view: View): HTMLElement {
  const key = document.createElement("span");
  key.style.background = color;
  key.title = describeBand(band, view);
  bandKey(key, band);
  return key;
}

// Makes an element of the legend stand for a band, as its key or a label on it.
function bandKey(key: HTMLElement, band: number): void {
  key.dataset["band"] = String(band);
  // Only from a mouse, since a tap's touch enters and then leaves at once, unhighlighting it as soon as it's tapped.
  key.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "mouse") {
      highlightBand(band);
    }
  });
  key.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "mouse") {
      restorePinnedBand();
    }
  });
  key.addEventListener("click", () => pinLegendBand(band));
}

// Set once the map's layer is made, after the legend is first shown.
let highlightBand = (_band: number): void => {};
let restorePinnedBand = (): void => {};
let pinLegendBand = (_band: number): void => {};

function legendItems(view: View): HTMLElement[] {
  const viewBands = bands(view);
  const heading = document.createElement("h2");
  heading.textContent = view === "saved" ? "Minutes saved" : "Minutes from Station";
  const bar = document.createElement("div");
  bar.className = "legend-bands";
  const labels = document.createElement("div");
  labels.className = "legend-labels";
  // With a last band of its own for places only QueensLink reaches, in minutes saved.
  const slots = viewBands.length + (view === "saved" ? 1 : 0);
  // Each at the edge its band starts at, as in QueensLink's mockup.
  const label = (text: string | HTMLElement, edge: number): void => {
    const span = document.createElement("span");
    span.append(text);
    span.style.left = `${(edge / slots) * 100}%`;
    labels.append(span);
  };
  for (const [i, [minutes, color]] of viewBands.entries()) {
    bar.append(legendBand(i, color, view));
    // The last band of minutes saved goes on without end, so it's marked as starting with no end.
    label(view === "saved" && i === viewBands.length - 1 ? `${minutes}+` : String(minutes), i);
  }
  if (view === "saved") {
    // Set apart from the minutes saved, as there's no saying how many it saves,
    // and labeled in its middle, with what it means in full on pointing at it.
    const onlyQueensLink = legendBand(savedBands.length, QUEENSLINK_ONLY_COLOR, view);
    onlyQueensLink.className = "legend-only-queenslink";
    bar.append(onlyQueensLink);
    // QueensLink's M bullet, as its new stations are drawn, for "only with QueensLink".
    const m = document.createElement("span");
    m.className = "legend-station legend-station-new";
    label(m, savedBands.length + 0.5);
    // As easy to tap as the band, as it's small.
    bandKey(m, savedBands.length);
  } else {
    label(String(MAX_MINUTES), viewBands.length);
    // Beside the heading, to save a line.
    const note = document.createElement("span");
    note.className = "heading-note";
    note.textContent = ", walking at the end";
    heading.append(note);
  }
  heading.append(stationKeyShow);
  // What a band pinned from the legend is, said here, as there's no pointing at it on a phone to see its title.
  const description = document.createElement("p");
  description.className = "band-description";
  description.hidden = true;
  return [heading, bar, labels, description];
}

// The lines of the scenario the view shows, with QueensLink's for what it saves,
// but for express runs over a local's stops, drawn only in journeys along them.
const linesFilter = (view: View): ExpressionSpecification => [
  "all",
  ["has", view === "saved" ? "queenslink" : view],
  ["!", ["get", "shortcut"]],
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
const speedValue = element("#speed-value", HTMLOutputElement);
const speedSlider = element("input[name=speed]", HTMLInputElement);
const summaryStatus = element("#summary-status", HTMLElement);
// Kept apart from the stations, which load after the controls are first shown.
let selectedName = "…";
const legend = element("#legend", HTMLElement);
const stationKeyShow = element("#station-key-show", HTMLButtonElement);
const fullMap = element("#full-map", HTMLAnchorElement);

// Narrow screens, as on phones, where the controls and credits open would cover much of the map.
// Or short ones, as on phones turned sideways, where the controls open are 300px of under 500.
// The same as `style.css`'s compact layout.
const smallScreen = window.matchMedia("(max-width: 600px), (max-height: 500px)");
const small = smallScreen.matches;

// Collapsed to start with on small screens.
const details = element("#controls details", HTMLDetailsElement);
details.open = !small;

// Shown by clicking or tapping, as well as by pointing on devices that can.
const timeInfoButton = element("#time-info-button", HTMLButtonElement);
const timeInfo = element("#time-info", HTMLElement);
timeInfoButton.addEventListener("click", () => {
  const open = timeInfo.classList.toggle("open");
  timeInfoButton.setAttribute("aria-expanded", String(open));
});

// Only when embedded, as on thequeenslink.org, since it's already the full map otherwise.
fullMap.hidden = window.top === window.self;

// The controls kept across reloads and in links, by their inputs' name.
const savedControls = ["day", "view", "speed", "fare"] as const;

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
    } else if (value !== null && inputs instanceof HTMLInputElement) {
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
  showSpeed();
  showView(currentView());
  showSummaryStatus();
}

// What the map shows, for when the controls are collapsed and don't say.
// It can only be measured while shown, so it's shown again on collapsing and resizing.
function showSummaryStatus(): void {
  const days = shortDayNames[currentDay()];
  const views = shortViewNames[currentView()];
  summaryStatus.classList.remove("wraps");
  for (const [i, day] of days.entries()) {
    summaryStatus.textContent = [selectedName, day, views[i]].join(" · ");
    if (summaryStatus.scrollWidth <= summaryStatus.clientWidth) {
      return;
    }
  }
  // Too narrow for even the shortest, as on the narrowest phones, so wrapped rather than cut off.
  summaryStatus.classList.add("wraps");
}

// As wide as the choices need on larger screens, measured open, and kept collapsed,
// so that the marker stays put as it turns. Small screens have their own width in `style.css`.
function fitControls(): void {
  controls.style.width = "";
  if (smallScreen.matches) {
    return;
  }
  const open = details.open;
  details.open = true;
  controls.style.width = "max-content";
  controls.style.width = `${Math.ceil(controls.getBoundingClientRect().width)}px`;
  details.open = open;
}

// The station key hidden once it's been seen, if chosen, and kept hidden across reloads, as the controls are.
function showStationKey(shown: boolean): void {
  controls.classList.toggle("station-key-hidden", !shown);
  try {
    localStorage.setItem(storageKey("station-key"), shown ? "shown" : "hidden");
  } catch {
    // Not saved, as when storage is unavailable.
  }
}
element("#station-key-hide", HTMLButtonElement).addEventListener("click", () =>
  showStationKey(false),
);
stationKeyShow.addEventListener("click", () => showStationKey(true));
controls.classList.toggle("station-key-hidden", savedValue("station-key") === "hidden");

restoreControls();
showControls();
fitControls();
// Again once the fonts are in, which are wider or narrower than what's shown until then.
document.fonts.ready.then(fitControls);
document.fonts.addEventListener("loadingdone", fitControls);
smallScreen.addEventListener("change", fitControls);
details.addEventListener("toggle", showSummaryStatus);
window.addEventListener("resize", showSummaryStatus);

function currentDay(): Day {
  const value = new FormData(controls).get("day");
  if (!isDay(value)) {
    throw new Error(`unknown day ${String(value)}`);
  }
  return value;
}

// The reader's walking speed out of stations, in miles an hour.
function currentSpeed(): number {
  return Number(new FormData(controls).get("speed"));
}

function showSpeed(): void {
  speedValue.textContent = `${currentSpeed()} mph`;
}

// How journeys are routed, in the order `Fare` lists them in `src/subway_sheds/scenarios.py`:
// paying a single fare, so not changing outside the system, or with an unlimited card, so changing there too.
const fares = ["single", "unlimited"] as const;
type Fare = (typeof fares)[number];

function currentFare(): Fare {
  const value = new FormData(controls).get("fare");
  const fare = fares.find((fare) => fare === value);
  if (fare === undefined) {
    throw new Error(`unknown fare ${String(value)}`);
  }
  return fare;
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

// In the controls' heading bar on small screens, so that the controls can take the full width below it,
// and back in the map's corner otherwise.
const fullscreenGroup = element(".maplibregl-ctrl-fullscreen", HTMLButtonElement).parentElement;
const fullscreenCorner = fullscreenGroup?.parentElement;
if (fullscreenGroup !== null && fullscreenCorner !== null && fullscreenCorner !== undefined) {
  // Not also toggling the controls open or closed, as clicking anything else in their heading bar does.
  fullscreenGroup.addEventListener("click", (event) => event.preventDefault());
  const placeFullscreen = (): void => {
    (smallScreen.matches ? element("#controls summary", HTMLElement) : fullscreenCorner).append(
      fullscreenGroup,
    );
    showSummaryStatus();
  };
  placeFullscreen();
  smallScreen.addEventListener("change", placeFullscreen);
}

// Listened for before awaiting the data, which the map can finish loading before.
const loaded = new Promise((resolve) => map.once("load", resolve));

// As the pipeline writes them; see `src/subway_sheds/export.py`.
interface RouteInfo {
  readonly name: string;
  readonly color: string;
  readonly text: string;
}
interface Pattern {
  readonly route: string;
  readonly stations: readonly number[];
}

const [grid, stations, lines, nearExits, routeInfo, patterns] = await Promise.all([
  fetchJson<Grid>("grid.json"),
  stationList,
  fetchJson<GeoJSON.FeatureCollection>("lines.json"),
  stationList.then(async (list) => parseExits(await fetchBinary("exits.bin"), list.length)),
  fetchJson<Record<string, RouteInfo>>("routes.json"),
  fetchJson<Pattern[][]>("patterns.json"),
]);
const { width, height } = rasterSize(grid);
const bandsLayer = new BandsLayer("bands", grid, width, height);
// Each raster pixel's cell, as `grid.json` lists them, or -1 where there's none.
const cellOfPixel = new Int32Array(width * height).fill(-1);
grid.cols.forEach((col, i) => {
  cellOfPixel[(grid.rows[i] ?? 0) * width + col] = i;
});
const stationsById = new Map(stations.map((station) => [station.id, station]));

// Broad Channel, whose trip to most of the city QueensLink shortens.
const DEFAULT_STATION = "H04";

// The station last asked for, so that a slower fetch for an earlier one can't replace it.
let selected = "";
// When trains from the station shown reach every station.
let selectedArrivals: Arrivals | undefined;
// The journeys from the station shown.
let selectedRoutes: Routes | undefined;
// The times from the station shown, at the walking speed chosen,
// and each cell's station walked out of to it.
let selectedTimes: Uint8Array | undefined;
let selectedExitFrom: Uint16Array | undefined;
let selectedExitMinute: Uint8Array | undefined;
// And the minutes QueensLink saves, to every cell, then every station.
let selectedSaved: Int8Array | undefined;
// Each cell's class, as drawn.
let selectedClasses: Int8Array | undefined;

// The walks out of stations: the near ones first, then the rest of each part as it's in.
const exitParts = [nearExits];
let exits = nearExits;

// Computes the times off the page's thread; see `times.worker.ts`.
const timesWorker = new Worker(new URL("./times.worker.ts", import.meta.url), { type: "module" });
function sendExits(): void {
  const request: TimesRequest = {
    kind: "exits",
    exits,
    cells: grid.cols.length,
    stations: stations.length,
  };
  timesWorker.postMessage(request);
}
sendExits();
// The far walks, after the page has drawn with the near ones, as they're most of the download.
void fetchBinary("exits-far.bin").then((far) => {
  addExits(far);
});
// The walks only walking faster than the slider starts at reaches, once it is,
// as they're most of the walks.
let fastExits: Promise<void> | undefined;
function loadFastExits(): void {
  if (fastExits === undefined && currentSpeed() > Number(speedSlider.defaultValue)) {
    fastExits = fetchBinary("exits-fast.bin").then(addExits, () => {
      fastExits = undefined;
    });
  }
}
// Each station's walks out to the cells, to draw, by station, as `stations.json` lists them,
// those faster than the slider starts too once it is: the ones loaded, and those being loaded,
// the least recently asked for first, so that pointing across the city doesn't keep all of them.
const CACHED_EXIT_WALKS = 32;
const exitWalks = new Map<number, ExitWalks>();
const exitWalksLoading = new Map<number, Promise<void>>();

// Loads `station`'s walks out, and if `fast`, the faster ones too, unless they're loaded or loading.
function loadExitWalks(
  station: number,
  fast = currentSpeed() > Number(speedSlider.defaultValue),
): Promise<void> {
  const key = fast ? -1 - station : station;
  const loading = exitWalksLoading.get(key);
  if (loading !== undefined) {
    exitWalksLoading.delete(key);
    exitWalksLoading.set(key, loading);
    return loading;
  }
  const id = encodeURIComponent(stations[station]?.id ?? "");
  const load = (
    fast
      ? Promise.all([loadExitWalks(station, false), fetchBinary(`walks-out-fast/${id}.bin`)]).then(
          ([, buffer]) => {
            const before = exitWalks.get(station);
            return before === undefined ? undefined : parseExitWalks(buffer, before);
          },
        )
      : fetchBinary(`walks-out/${id}.bin`).then((buffer) => parseExitWalks(buffer))
  ).then(
    (walks) => {
      if (walks !== undefined) {
        exitWalks.delete(station);
        exitWalks.set(station, walks);
      }
    },
    // Forgotten if it fails, so that it's tried again next time.
    () => {
      exitWalksLoading.delete(key);
    },
  );
  exitWalksLoading.set(key, load);
  for (const [oldest] of exitWalks) {
    if (exitWalks.size <= CACHED_EXIT_WALKS) {
      break;
    }
    exitWalks.delete(oldest);
    exitWalksLoading.delete(oldest);
    exitWalksLoading.delete(-1 - oldest);
  }
  return load;
}

function addExits(part: ArrayBuffer): void {
  exitParts.push(parseExits(part, stations.length));
  exits = mergeExits(exitParts, stations.length);
  sendExits();
  recompute();
}
// The last request sent, and whether the controls or station have changed since,
// so that while one's computing, any number of changes ask for one more, not one each.
let timesAsked = 0;
let timesComputing = false;
let timesStale = false;
// The station the times computing are from, which another may have been picked over since.
let timesOrigin = "";

// Recomputes the times for the station and controls as they are, then redraws.
function recompute(): void {
  loadFastExits();
  if (selectedArrivals === undefined) {
    return;
  }
  if (timesComputing) {
    timesStale = true;
    return;
  }
  timesComputing = true;
  timesStale = false;
  timesAsked += 1;
  timesOrigin = selected;
  const request: TimesRequest = {
    kind: "times",
    id: timesAsked,
    arrivals: selectedArrivals,
    origin: stations.findIndex((station) => station.id === selected),
    mph: currentSpeed(),
    // Only the day and fare shown, both scenarios, as the minutes saved need both.
    runs: [baselineRun(), baselineRun() + 1],
  };
  timesWorker.postMessage(request);
}

timesWorker.addEventListener("message", (event: MessageEvent<TimesResponse>) => {
  timesComputing = false;
  // Asked again since, or for a station since replaced, which `recompute` asks for, once it's loaded.
  if (timesStale || timesOrigin !== selected) {
    recompute();
    return;
  }
  if ("error" in event.data) {
    console.error("computing times failed:", event.data.error);
    return;
  }
  ({
    times: selectedTimes,
    exitFrom: selectedExitFrom,
    exitMinute: selectedExitMinute,
    saved: selectedSaved,
  } = event.data.times);
  drawCells();
});

// The worker failing to load never answers, so that otherwise nothing would ask it again.
timesWorker.addEventListener("error", (event) => {
  console.error("times worker failed:", event.message);
  timesComputing = false;
});

// Recomputed as the speed slides.
controls.addEventListener("input", (event) => {
  if (event.target instanceof HTMLInputElement && event.target.name === "speed") {
    showSpeed();
    recompute();
  }
});

// Forgets the times shown, and their bands, so that they aren't taken for another station's.
function clearTimes(): void {
  selectedTimes = undefined;
  selectedExitFrom = undefined;
  selectedExitMinute = undefined;
  selectedSaved = undefined;
  selectedClasses = undefined;
  const { width, height } = rasterSize(grid);
  bandsLayer.setClasses(new Int8Array(width * height).fill(NOTHING), []);
  clearBand();
}

function drawCells(): void {
  if (selectedTimes !== undefined && selectedSaved !== undefined) {
    const view = currentView();
    selectedClasses = cellClasses(grid, selectedTimes, selectedSaved, currentDay(), view);
    bandsLayer.setClasses(selectedClasses, classColors(view));
    clearBand();
  }
}

// A station's name, with QueensLink's M bullet after a new station's, as the map draws it.
function stationLabel(station: Station): (string | HTMLElement)[] {
  if (station.scenarios.length !== 1) {
    return [station.name];
  }
  const m = document.createElement("span");
  m.className = "legend-station legend-station-new";
  return [station.name, " ", m];
}

async function selectStation(id: string): Promise<void> {
  const station = stationsById.get(id);
  // Picked again, as when a link to another view of it is followed, it's already loaded.
  if (station === undefined || (id === selected && selectedArrivals !== undefined)) {
    return;
  }
  selected = id;
  // The last station's, which its journeys would otherwise be drawn over, mixed with this one's.
  selectedArrivals = undefined;
  selectedRoutes = undefined;
  clearTimes();
  // Shown picked straight away, so that the click feels taken while its times load.
  for (const layer of ["selected-station", "selected-station-circle"]) {
    map.setFilter(layer, ["==", ["get", "id"], id]);
  }
  // Only a new station has an M, which would otherwise be drawn over the others' white too.
  map.setFilter("selected-station-m", ["all", ["get", "new"], ["==", ["get", "id"], id]]);
  stationName.replaceChildren(...stationLabel(station));
  selectedName = station.name;
  showSummaryStatus();
  writeHash();
  const { arrivals, routes } = await fetchTimes(id);
  if (selected !== id) {
    return;
  }
  selectedArrivals = arrivals;
  selectedRoutes = routes;
  recompute();
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
  recompute();
}

// How far from a station, in pixels, a click or tap still picks it,
// since the dots are only a few pixels across when zoomed out.
const HIT_RADIUS = 12;
// And with a mouse, which can be precise, only on its dot, give or take a little,
// so that the bands between the stations are still easy to point at and click.
const MOUSE_RADIUS = 2;

// The station nearest the point, of those within reach of it.
function stationAt(event: { readonly point: Point }, radius = HIT_RADIUS): string | undefined {
  const { x, y } = event.point;
  let nearest: string | undefined;
  let nearestDistance = Infinity;
  const near = map.queryRenderedFeatures(
    [
      [x - radius, y - radius],
      [x + radius, y + radius],
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

// The class of the cell nearest a point on the map, or `NOTHING`.
// 1-2-1 weights, for a point `t` of the way from the second cell to the third of four.
const blurWeights = (t: number): readonly number[] => [
  (1 - t) / 4,
  (2 * (1 - t) + t) / 4,
  (1 - t + 2 * t) / 4,
  t / 4,
];

// The class drawn at a point on the map, or `NOTHING`,
// weighing the cells around it as the bands' shader does, so that it's the band seen there.
// Where `lngLat` is in the raster, in cells, or `undefined` off it.
function rasterAt(lngLat: LngLat): { readonly col: number; readonly row: number } | undefined {
  // In Web Mercator meters, as the grid is, then in cells.
  const x = (lngLat.lng / 360) * EARTH_CIRCUMFERENCE;
  const y =
    (Math.log(Math.tan(Math.PI / 4 + (lngLat.lat * Math.PI) / 360)) / (2 * Math.PI)) *
    EARTH_CIRCUMFERENCE;
  const col = (x - grid.x0) / grid.spacing;
  const row = (y - grid.y0) / grid.spacing;
  if (col < -0.5 || col > width - 0.5 || row < -0.5 || row > height - 0.5) {
    return undefined;
  }
  return { col, row };
}

// The cell at `lngLat`, as `grid.json` lists them, or -1 where there's none.
function cellAt(lngLat: LngLat): number {
  const at = rasterAt(lngLat);
  return at === undefined
    ? -1
    : (cellOfPixel[Math.round(at.row) * width + Math.round(at.col)] ?? -1);
}

// The station walked out of to `cell`, in the run shown or `run`, or `NO_STATION`.
function exitFromOf(cell: number, run = currentRun()): number {
  return cell < 0 || selectedExitFrom === undefined
    ? NO_STATION
    : (selectedExitFrom[run * grid.cols.length + cell] ?? NO_STATION);
}

function bandAt(lngLat: LngLat): number {
  const at = rasterAt(lngLat);
  if (selectedClasses === undefined || at === undefined) {
    return NOTHING;
  }
  const { col, row } = at;
  const clamp = (value: number, max: number): number => Math.min(Math.max(value, 0), max);
  const lowCol = Math.min(Math.floor(clamp(col, width - 1)), Math.max(width - 2, 0));
  const lowRow = Math.min(Math.floor(clamp(row, height - 1)), Math.max(height - 2, 0));
  const across = blurWeights(clamp(col, width - 1) - lowCol);
  const up = blurWeights(clamp(row, height - 1) - lowRow);
  const amounts = new Map<number, number>();
  for (let dy = 0; dy < 4; dy++) {
    for (let dx = 0; dx < 4; dx++) {
      const atCol = clamp(lowCol + dx - 1, width - 1);
      const atRow = clamp(lowRow + dy - 1, height - 1);
      const band = selectedClasses[atRow * width + atCol] ?? NOTHING;
      amounts.set(band, (amounts.get(band) ?? 0) + (across[dx] ?? 0) * (up[dy] ?? 0));
    }
  }
  let most = NOTHING;
  let mostAmount = -1;
  for (const [band, amount] of amounts) {
    if (amount > mostAmount) {
      most = band;
      mostAmount = amount;
    }
  }
  return most;
}

// MapLibre's own, which a band's description wraps in, unless the journey under it is wider.
const BAND_TEXT_WIDTH = 240;
// Wide enough for a journey on one line, but not past the edges of a phone's screen.
const BAND_POPUP_WIDTH = "calc(100vw - 32px)";
// From its top left corner, offset to wherever it keeps off the journey drawn (see `placeBandPopup`).
const bandPopup = new Popup({
  closeButton: false,
  closeOnClick: false,
  className: "band-popup",
  anchor: "top-left",
});

// The journey drawn, for keeping the popup off it.
let routeShown: GeoJSON.Feature[] = [];
// How far the popup keeps from the point it's for, and from the journey, in pixels.
const POPUP_GAP_PX = 12;
const POPUP_ROUTE_MARGIN_PX = 8;
// How finely the journey's lines are sampled, in pixels, to see whether the popup covers them.
const ROUTE_SAMPLE_PX = 4;

// Puts the popup beside its point, wherever it covers the least of the journey drawn,
// and then the least off the map, and then nearest above the point, as MapLibre would have it.
function placeBandPopup(): void {
  if (!bandPopup.isOpen()) {
    return;
  }
  const element = bandPopup.getElement();
  const w = element.offsetWidth;
  const h = element.offsetHeight;
  const at = map.project(bandPopup.getLngLat());
  const container = map.getContainer();
  // The journey's points, and along its lines, on the screen, from the popup's point.
  const samples: [number, number][] = [];
  const sample = (position: GeoJSON.Position): [number, number] => {
    const p = map.project([position[0] ?? 0, position[1] ?? 0]);
    return [p.x - at.x, p.y - at.y];
  };
  for (const feature of routeShown) {
    const { geometry } = feature;
    const lines =
      geometry.type === "LineString"
        ? [geometry.coordinates]
        : geometry.type === "MultiLineString"
          ? geometry.coordinates
          : geometry.type === "Point"
            ? [[geometry.coordinates]]
            : [];
    for (const line of lines) {
      const points = line.map(sample);
      for (const [k, [x, y]] of points.entries()) {
        samples.push([x, y]);
        const [nx, ny] = points[k + 1] ?? [x, y];
        const steps = Math.floor(Math.hypot(nx - x, ny - y) / ROUTE_SAMPLE_PX);
        for (let i = 1; i < steps; i++) {
          samples.push([x + ((nx - x) * i) / steps, y + ((ny - y) * i) / steps]);
        }
      }
    }
  }
  // Above or below, from all to its left to all to its right, or left or right, from all above to all below.
  const SLIDES = 8;
  const candidates: [number, number][] = [];
  for (let i = 0; i <= SLIDES; i++) {
    const x = -w - POPUP_GAP_PX + ((w + 2 * POPUP_GAP_PX) * i) / SLIDES;
    const y = -h - POPUP_GAP_PX + ((h + 2 * POPUP_GAP_PX) * i) / SLIDES;
    candidates.push([x, -h - POPUP_GAP_PX], [x, POPUP_GAP_PX]);
    candidates.push([-w - POPUP_GAP_PX, y], [POPUP_GAP_PX, y]);
  }
  const cost = ([x, y]: [number, number]): number => {
    const m = POPUP_ROUTE_MARGIN_PX;
    const covered = samples.filter(
      ([sx, sy]) => sx >= x - m && sx <= x + w + m && sy >= y - m && sy <= y + h + m,
    ).length;
    const off =
      Math.max(-(at.x + x), 0) +
      Math.max(at.x + x + w - container.clientWidth, 0) +
      Math.max(-(at.y + y), 0) +
      Math.max(at.y + y + h - container.clientHeight, 0);
    const fromAbove = Math.abs(x + w / 2) + Math.abs(y + h + POPUP_GAP_PX);
    return covered * 1e6 + off * 1e3 + fromAbove;
  };
  let best: [number, number] = [-w / 2, -h - POPUP_GAP_PX];
  for (const candidate of candidates) {
    if (cost(candidate) < cost(best)) {
      best = candidate;
    }
  }
  bandPopup.setOffset(best);
}
// Zooming moves the journey's lines from the point, under the popup or out from it.
map.on("zoom", placeBandPopup);

highlightBand = (band: number): void => {
  bandsLayer.setHighlight(band);
  for (const key of legend.querySelectorAll<HTMLElement>(".legend-bands [data-band]")) {
    key.classList.toggle("highlighted", key.dataset["band"] === String(band));
  }
};

// The band clicked or tapped, kept highlighted and said until clicked again or another is,
// and where, for its popup, if on the map rather than in the legend.
let pinned: { readonly band: number; readonly lngLat?: LngLat } | undefined;

// The cell whose journey is shown, and the wait to load the walks out to draw it,
// so that pointing across the city doesn't load every station's.
let shownCell: number | undefined;
let exitWalksWait: number | undefined;

// Highlights a band and says what it is at a point, or shows the pinned one if there's none.
function showBand(band: number, lngLat: LngLat): void {
  showRoute(undefined);
  if (band === NOTHING) {
    restorePinnedBand();
    return;
  }
  highlightBand(band);
  // The journey there: the trains to the station walked out of, and the walk from it,
  // straight, then along the streets once the station's walks out are loaded.
  const cell = cellAt(lngLat);
  const drawRoute = (): void =>
    showRoute(
      (run) => stations[exitFromOf(cell, run)],
      lngLat,
      (run) => cellDepartureOf(cell, run),
      cell,
    );
  drawRoute();
  shownCell = cell;
  clearTimeout(exitWalksWait);
  exitWalksWait = window.setTimeout(() => {
    for (const run of shownRuns()) {
      const from = exitFromOf(cell, run);
      if (from !== NO_STATION) {
        void loadExitWalks(from).then(() => {
          if (shownCell === cell) {
            drawRoute();
          }
        });
      }
    }
  }, HOVER_PREFETCH_MS);
  const label = document.createElement("span");
  // What the band is, wrapped at `BAND_TEXT_WIDTH`, or as wide as the journey under it.
  const text = document.createElement("span");
  text.className = "band-text";
  text.style.maxWidth = `${BAND_TEXT_WIDTH}px`;
  text.append(describeBand(band, currentView()));
  label.append(text);
  // The trains there, and the walk out of the last station at the speed chosen.
  for (const run of shownRuns()) {
    const exitFrom = exitFromOf(cell, run);
    const from = stations[exitFrom];
    if (from === undefined) {
      continue;
    }
    const origin = stations.findIndex((station) => station.id === selected);
    const walk = walkOutSeconds(exits, exitFrom, cell, currentSpeed(), exitFrom === origin);
    const line =
      exitFrom === origin
        ? journeyLine([], undefined, walk, run)
        : journeyAt(from, cellDepartureOf(cell, run), walk, run);
    if (line !== undefined) {
      label.append(line);
    }
  }
  bandPopup.setLngLat(lngLat).setMaxWidth(BAND_POPUP_WIDTH).setDOMContent(label).addTo(map);
  // The journeys kept on one line, as far as the map has room for, and the text as wide as the widest.
  const widths = [...label.querySelectorAll<HTMLElement>(".route-trains")].map(
    (j) => j.offsetWidth,
  );
  text.style.maxWidth = `${Math.max(BAND_TEXT_WIDTH, ...widths)}px`;
  placeBandPopup();
}

restorePinnedBand = (): void => {
  showRoute(undefined);
  if (pinned?.lngLat === undefined) {
    highlightBand(pinned?.band ?? NOTHING);
    bandPopup.remove();
  } else {
    showBand(pinned.band, pinned.lngLat);
  }
  const description = legend.querySelector<HTMLElement>(".band-description");
  if (description !== null) {
    description.hidden = pinned === undefined || pinned.lngLat !== undefined;
    description.textContent = pinned === undefined ? "" : describeBand(pinned.band, currentView());
  }
};

pinLegendBand = (band: number): void => {
  pinned = band === pinned?.band && pinned.lngLat === undefined ? undefined : { band };
  restorePinnedBand();
};

// Pins the band at a point, or unpins it if it's the one pinned or there's none there.
function pinBand(lngLat: LngLat): void {
  const band = bandAt(lngLat);
  pinned = band === NOTHING || band === pinned?.band ? undefined : { band, lngLat };
  restorePinnedBand();
}

function clearBand(): void {
  pinned = undefined;
  restorePinnedBand();
}

// How long a touch on a station is held to say its minutes rather than pick it.
const HOLD_MS = 500;

// The MTA's M bullet's orange, from its SVG.
const M_BULLET_ORANGE = "#ff6319";

// The radii of QueensLink's new stations at zooms 10 and 14.
const NEW_STATION_RADII = [6, 11] as const;
// And of the other stations.
const STATION_RADII = [2.5, 6] as const;
// Their outlines' widths.
const NEW_STATION_OUTLINE = 2;
const STATION_OUTLINE = 1;

// The selected station's ring, the same width whichever kind it is,
// a gap out from the other stations' gray outline,
// but right on QueensLink's new ones' white outline, which already sets it apart.
const RING_GAP = 1.5;
const RING_WIDTH = 2;

// A selected station's ring's `circle-radius` at each zoom.
function ringRadius(): ExpressionSpecification {
  const outer = (zoom: 0 | 1): ExpressionSpecification => [
    "case",
    ["get", "new"],
    NEW_STATION_RADII[zoom] + NEW_STATION_OUTLINE,
    STATION_RADII[zoom] + STATION_OUTLINE + RING_GAP,
  ];
  return ["interpolate", ["linear"], ["zoom"], 10, outer(0), 14, outer(1)];
}

// The white M drawn in each new station, filling it at its biggest,
// as tall and as wide for the circle as the MTA's own M bullet's is,
// measured from its SVG: 50.4% of the circle's diameter tall, 47.2% wide.
const M_HEIGHT = 0.504;
const M_WIDTH = 0.472;

function queenslinkM(): HTMLCanvasElement {
  const size = 2 * NEW_STATION_RADII[1] * devicePixelRatio;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d");
  if (context === null) {
    throw new Error("no 2D canvas context");
  }
  context.fillStyle = "#ffffff";
  context.font = `bold ${size}px "Helvetica Neue", Helvetica, Arial, sans-serif`;
  // The M's own bounds, not the font's, which leave room for accents and descenders,
  // scaled to the bullet's, Helvetica's M being wider for its height than the bullet's.
  const m = context.measureText("M");
  const width = m.actualBoundingBoxLeft + m.actualBoundingBoxRight;
  const height = m.actualBoundingBoxAscent + m.actualBoundingBoxDescent;
  context.translate(size / 2, size / 2);
  context.scale((M_WIDTH * size) / width, (M_HEIGHT * size) / height);
  context.fillText(
    "M",
    (m.actualBoundingBoxLeft - m.actualBoundingBoxRight) / 2,
    (m.actualBoundingBoxAscent - m.actualBoundingBoxDescent) / 2,
  );
  return canvas;
}

function imageData(canvas: HTMLCanvasElement): ImageData {
  const context = canvas.getContext("2d");
  if (context === null) {
    throw new Error("no 2D canvas context");
  }
  return context.getImageData(0, 0, canvas.width, canvas.height);
}

// The legend's new station, the same bullet as on the map.
const legendM = queenslinkM();
// As a variable for `style.css`, for the legend's bullets made since, too.
document.documentElement.style.setProperty("--queenslink-m", `url(${legendM.toDataURL()})`);

// The run of `day` and the fare chosen without QueensLink, as the pipeline writes them,
// by day, then fare, then scenario; the one with it is next.
function baselineRun(day = currentDay()): number {
  return (days.indexOf(day) * fares.length + fares.indexOf(currentFare())) * 2;
}

// The run shown: the day's with the fare chosen, with QueensLink unless only without it is.
function currentRun(): number {
  return baselineRun() + (currentView() === "baseline" ? 0 : 1);
}

// The runs whose journeys are shown: the one shown, or for minutes saved, the day's without QueensLink, then with it.
function shownRuns(): number[] {
  const run = currentRun();
  return currentView() === "saved" ? [run - 1, run] : [run];
}

// The minute of the window the median trip to `station` itself departs, in the run shown or `run`.
function departureOf(station: Station, run = currentRun()): number | undefined {
  if (selectedArrivals === undefined) {
    return undefined;
  }
  return medianDeparture(selectedArrivals, run, stations.indexOf(station), stations.length);
}

// The journey to `station` departing at `minute` in `run`, as `journeyLine` says it, if it's reached.
function journeyAt(
  station: Station,
  minute: number | undefined,
  walkOut: number | undefined,
  run: number,
): HTMLElement | undefined {
  return minute === undefined
    ? undefined
    : journeyLine(routeLegs(station, minute, run), 60 * minute, walkOut, run);
}

// The minute of the window the median trip to `cell` departs, in the run shown or `run`.
function cellDepartureOf(cell: number, run = currentRun()): number | undefined {
  return cell < 0 || selectedExitMinute === undefined
    ? undefined
    : selectedExitMinute[run * grid.cols.length + cell];
}

// The journey from the selected station to `station` departing at `minute`, in the run shown or `run`.
function routeLegs(station: Station, minute: number, run = currentRun()): RouteLeg[] {
  if (selectedRoutes === undefined) {
    return [];
  }
  return routeTo(selectedRoutes, run, stations.indexOf(station), stations.length, minute);
}

// A ride's train, and whether it's an express variant, as the FX, 6X and 7X are,
// which the MTA draws as a diamond with the line's own name, without the X,
// and for a shuttle, which shuttle, which the MTA writes small in its S's upper right.
interface Train extends RouteInfo {
  readonly express: boolean;
  readonly corner: string;
}
const SHUTTLE_CORNERS: Readonly<Record<string, string>> = { GS: "42", FS: "F", H: "R" };
// The patterns `run` rides, listed by day, then scenario, as either fare's runs have the same.
function patternsOf(run: number): readonly Pattern[] | undefined {
  const day = Math.floor(run / 4);
  const scenario = run % 2;
  return patterns[2 * day + scenario];
}

function trainOf(leg: RouteLeg, run = currentRun()): Train {
  const route = patternsOf(run)?.[leg.pattern]?.route ?? "";
  const info = routeInfo[route] ?? { name: route, color: "#808183", text: "#ffffff" };
  const express = /^.X$/.test(info.name);
  return {
    ...info,
    name: express ? info.name.slice(0, -1) : info.name,
    express,
    corner: SHUTTLE_CORNERS[route] ?? "",
  };
}

// A train's bullet, as the MTA draws it.
function bullet(train: Train): HTMLElement {
  const b = document.createElement("span");
  b.className = train.express ? "route-bullet route-bullet-express" : "route-bullet";
  b.textContent = train.name;
  if (train.corner !== "") {
    const corner = document.createElement("span");
    corner.className = "route-bullet-corner";
    corner.textContent = train.corner;
    b.append(corner);
  }
  b.style.setProperty("--bullet-color", train.color);
  b.style.color = train.text;
  return b;
}

// An express train's diamond on the map, white, for MapLibre to color as a signed distance field.
const DIAMOND_PX = 28;
function diamond(): ImageData {
  const canvas = document.createElement("canvas");
  canvas.width = DIAMOND_PX;
  canvas.height = DIAMOND_PX;
  const context = canvas.getContext("2d");
  if (context === null) {
    throw new Error("no 2D canvas context");
  }
  const half = DIAMOND_PX / 2;
  // Leaving room for the halo drawn as its outline.
  const reach = half - 3;
  context.fillStyle = "#ffffff";
  context.beginPath();
  context.moveTo(half, half - reach);
  context.lineTo(half + reach, half);
  context.lineTo(half, half + reach);
  context.lineTo(half - reach, half);
  context.closePath();
  context.fill();
  return context.getImageData(0, 0, DIAMOND_PX, DIAMOND_PX);
}

// The fare, said over each step a journey pays it at, entering the system, whatever the card.
// Neither feed has GTFS fares yet; once one does, it should come from them.
const FARE = "$3";

// Seconds as `m:ss`, or `h:mm:ss` from an hour.
function clock(seconds: number): string {
  const s = Math.max(Math.round(seconds), 0);
  const hours = Math.floor(s / 3600);
  const pad = (n: number): string => String(n).padStart(2, "0");
  const minutes = Math.floor(s / 60) % 60;
  return hours === 0 ? `${minutes}:${pad(s % 60)}` : `${hours}:${pad(minutes)}:${pad(s % 60)}`;
}

// One step of a journey in its popup: `top`, with its time in small text under it,
// and the fare over it if it's paid there.
function journeyStep(
  top: Node | string,
  seconds: number,
  className: string,
  pays = false,
): HTMLElement {
  const step = document.createElement("span");
  step.className = `journey-step ${className}`;
  const fare = document.createElement("span");
  fare.className = "journey-fare";
  fare.textContent = pays ? FARE : "";
  const time = document.createElement("span");
  time.className = "journey-time";
  time.textContent = clock(seconds);
  step.append(fare, top, time);
  return step;
}

// Whether changing from ride `from` to ride `to` of `legs` pays again:
// if it walks between stations of different complexes, out of the system, but not free.
function paysBetween(legs: readonly RouteLeg[], from: RouteLeg, to: RouteLeg): boolean {
  return legs.slice(legs.indexOf(from) + 1, legs.indexOf(to)).some((leg) => {
    const to = stations[leg.b]?.complex ?? "";
    const from = stations[leg.a];
    return leg.pattern === WALK_LEG && from?.complex !== to && !(from?.free ?? []).includes(to);
  });
}

// A journey's trains, each with its ride's time under it, and between them,
// the time changing, walking and waiting, then, if given, the walk out at the end, an arrow too.
// Before them, as an arrow with its time as a change's is, from departing at `departs`
// seconds into the window: going in and waiting for the first, or walking to another station for it.
// In `run`, which, for minutes saved, it starts by saying, without QueensLink or with it,
// then the whole trip's time.
function journeyLine(
  legs: readonly RouteLeg[],
  departs: number | undefined,
  walkOut?: number,
  run = currentRun(),
): HTMLElement | undefined {
  const rides = legs.filter((leg) => leg.pattern !== WALK_LEG);
  if (rides.length === 0 && walkOut === undefined) {
    return undefined;
  }
  const line = document.createElement("span");
  line.className = "route-trains";
  if (currentView() === "saved") {
    const scenario = document.createElement("span");
    scenario.className = "journey-scenario";
    // "Now" for the one without, and QueensLink's logo for the one with it.
    if (run % 2 === 0) {
      scenario.textContent = "Now";
    } else {
      const logo = document.createElement("img");
      logo.className = "journey-logo";
      logo.src = `${import.meta.env.BASE_URL}favicon-192.png`;
      logo.alt = "with QueensLink";
      scenario.append(logo);
    }
    line.append(scenario);
  }
  const first = rides[0];
  const since = departs ?? first?.start ?? 0;
  const arrives = (rides.at(-1)?.end ?? since) + (walkOut ?? 0);
  const total = document.createElement("span");
  total.className = "journey-total";
  total.textContent = clock(arrives - since);
  line.append(total);
  if (first !== undefined && departs !== undefined) {
    line.append(journeyStep("→", first.start - departs, "journey-change", true));
  }
  let last: RouteLeg | undefined;
  for (const leg of legs) {
    if (leg.pattern === WALK_LEG) {
      continue;
    }
    if (last !== undefined) {
      line.append(
        journeyStep("→", leg.start - last.end, "journey-change", paysBetween(legs, last, leg)),
      );
    }
    line.append(journeyStep(bullet(trainOf(leg, run)), leg.end - leg.start, "journey-ride"));
    last = leg;
  }
  if (walkOut !== undefined) {
    line.append(journeyStep("→", walkOut, "journey-change"));
  }
  return line;
}

// The street walks between stations that changing trains may walk, by `from|to`,
// from the entrance walked out of to the one walked into, fetched after the page is drawn,
// as they're only for drawing journeys, which walk straight until then.
const walksBetween: Record<string, GeoJSON.Position[]> = {};
void Promise.all([fetchBinary("walks.bin"), stationList]).then(([buffer, list]) => {
  const view = new DataView(buffer);
  const n = view.getUint16(0, true);
  const column = (i: number, k: number): number => view.getUint16(2 + 2 * (i * n + k), true);
  let at = 2 + 6 * n;
  let lon = 0;
  let lat = 0;
  for (let k = 0; k < n; k++) {
    const walk: GeoJSON.Position[] = [];
    for (let p = column(2, k); p > 0; p--) {
      lon += view.getInt32(at, true);
      lat += view.getInt32(at + 4, true);
      at += 8;
      walk.push([lon / 1e5, lat / 1e5]);
    }
    walksBetween[`${list[column(0, k)]?.id}|${list[column(1, k)]?.id}`] = walk;
  }
});

// The lines between consecutive stations, from the first to the second, by `from|to`.
const segmentsBetween = new Map<string, GeoJSON.Position[]>();
for (const feature of lines.features) {
  const { from, to } = feature.properties ?? {};
  if (
    feature.geometry.type === "LineString" &&
    typeof from === "string" &&
    typeof to === "string"
  ) {
    segmentsBetween.set(`${from}|${to}`, feature.geometry.coordinates);
    segmentsBetween.set(`${to}|${from}`, [...feature.geometry.coordinates].reverse());
  }
}

const coordinatesOf = (i: number): GeoJSON.Position => {
  const station = stations[i];
  return station === undefined ? [0, 0] : [station.lon, station.lat];
};

// How faint the journey without QueensLink is drawn, under the one with it, for minutes saved.
const ROUTE_FADED_OPACITY = 0.4;

// The walk out of station `to` to `cell` along the streets, if it's loaded, else none.
function exitWalkOf(to: number, cell: number | undefined): GeoJSON.Position[] {
  const walks = exitWalks.get(to);
  return walks === undefined || cell === undefined ? [] : (exitWalkTo(walks, cell) ?? []);
}

// The journey to `to` on the map: each ride along its line, walks dashed, and a bullet where each ride boards.
// Then, if given, walking on from `to` to `end`, in `cell`, along the streets once they're loaded.
// In `run`, and if `faded`, drawn fainter.
function routeFeatures(
  legs: readonly RouteLeg[],
  to: number,
  end: GeoJSON.Position | undefined,
  cell: number | undefined,
  run: number,
  faded: boolean,
): GeoJSON.Feature[] {
  const features: GeoJSON.Feature[] = [];
  const origin = stations.findIndex((station) => station.id === selected);
  let at = origin;
  // Between stations, along the streets if changing trains may walk it, else straight.
  const walk = (from: number, to: number): void => {
    if (from !== to && from >= 0 && to >= 0) {
      const streets = walksBetween[`${stations[from]?.id}|${stations[to]?.id}`] ?? [];
      features.push({
        type: "Feature",
        geometry: {
          type: "LineString",
          coordinates: [coordinatesOf(from), ...streets, coordinatesOf(to)],
        },
        properties: { kind: "walk" },
      });
    }
  };
  for (const leg of legs) {
    if (leg.pattern === WALK_LEG) {
      walk(at, leg.a);
      walk(leg.a, leg.b);
      at = leg.b;
      continue;
    }
    const passed = patternsOf(run)?.[leg.pattern]?.stations.slice(leg.a, leg.b + 1) ?? [];
    const first = passed[0] ?? at;
    walk(at, first);
    const train = trainOf(leg, run);
    const coordinates: GeoJSON.Position[] = [];
    for (let k = 0; k + 1 < passed.length; k++) {
      const a = stations[passed[k] ?? -1]?.id;
      const b = stations[passed[k + 1] ?? -1]?.id;
      const between = segmentsBetween.get(`${a}|${b}`) ?? [
        coordinatesOf(passed[k] ?? 0),
        coordinatesOf(passed[k + 1] ?? 0),
      ];
      coordinates.push(...between);
    }
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates },
      properties: { kind: "ride", color: train.color },
    });
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: coordinatesOf(first) },
      properties: {
        name: train.name,
        color: train.color,
        text: train.text,
        express: train.express,
        corner: train.corner,
      },
    });
    at = passed[passed.length - 1] ?? at;
  }
  // Walking the rest of the way, or all of it, from a station as near as the origin.
  walk(at, to);
  if (end !== undefined) {
    features.push({
      type: "Feature",
      geometry: {
        type: "LineString",
        coordinates: [coordinatesOf(to), ...exitWalkOf(to, cell), end],
      },
      properties: { kind: "walk" },
    });
  }
  for (const feature of features) {
    feature.properties = { ...feature.properties, faded };
  }
  return features;
}

// Whether trains from the selected station reach `station` in `run`.
function reachedIn(station: Station, run: number): boolean {
  const at = run * timesStride(grid) + grid.cols.length + stations.indexOf(station);
  return selectedTimes !== undefined && (selectedTimes[at] ?? UNREACHED) !== UNREACHED;
}

// The journey to `station` on the map, or none, walking on to `end` if given, in `cell`,
// in each run shown, the station for which `station` says if it differs between them,
// departing at the minute `minute` says, or else the median trip to the station itself does.
// For minutes saved, the one without QueensLink is fainter, under the one with it.
function showRoute(
  station: Station | undefined | ((run: number) => Station | undefined),
  end?: LngLat,
  minute?: (run: number) => number | undefined,
  cell?: number,
): void {
  const source = map.getSource("route");
  if (source !== undefined && "setData" in source && typeof source.setData === "function") {
    const runs = shownRuns();
    const features = runs.flatMap((run) => {
      const to = typeof station === "function" ? station(run) : station;
      // Walked out of to a cell, it's reached, if only on foot from the origin.
      if (to === undefined || (end === undefined && !reachedIn(to, run))) {
        return [];
      }
      const position: GeoJSON.Position | undefined =
        end === undefined ? undefined : [end.lng, end.lat];
      const faded = runs.length > 1 && run !== currentRun();
      const departs = minute === undefined ? departureOf(to, run) : minute(run);
      const legs = departs === undefined ? [] : routeLegs(to, departs, run);
      return routeFeatures(legs, stations.indexOf(to), position, cell, run, faded);
    });
    source.setData({ type: "FeatureCollection", features });
    routeShown = features;
    placeBandPopup();
  }
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

  // The journey to the station pointed at, over the lines and under the stations.
  map.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "route-casing",
    type: "line",
    source: "route",
    filter: ["==", ["get", "kind"], "ride"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": "#ffffff",
      "line-width": ["interpolate", ["linear"], ["zoom"], 10, 7, 14, 12],
      "line-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
    },
  });
  map.addLayer({
    id: "route",
    type: "line",
    source: "route",
    filter: ["==", ["get", "kind"], "ride"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": ["get", "color"],
      "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4, 14, 8],
      "line-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
    },
  });
  map.addLayer({
    id: "route-walk",
    type: "line",
    source: "route",
    filter: ["==", ["get", "kind"], "walk"],
    layout: { "line-cap": "round" },
    paint: {
      "line-color": "#333333",
      "line-width": 2.5,
      "line-dasharray": [1, 2],
      "line-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
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
  const stationPaint: CircleLayerSpecification["paint"] = {
    // Stations new with QueensLink as the MTA's M bullet, an orange circle with a white M,
    // for the M trains QueensLink would run, bigger, so that the M is legible.
    "circle-radius": [
      "interpolate",
      ["linear"],
      ["zoom"],
      10,
      ["case", ["get", "new"], NEW_STATION_RADII[0], STATION_RADII[0]],
      14,
      ["case", ["get", "new"], NEW_STATION_RADII[1], STATION_RADII[1]],
    ],
    "circle-color": ["case", ["get", "new"], M_BULLET_ORANGE, "#ffffff"],
    "circle-stroke-color": ["case", ["get", "new"], "#ffffff", "#333333"],
    "circle-stroke-width": ["case", ["get", "new"], NEW_STATION_OUTLINE, STATION_OUTLINE],
  };
  map.addLayer({ id: "stations", type: "circle", source: "stations", paint: stationPaint });
  // Each ride's train, as its bullet, where it boards.
  map.addLayer({
    id: "route-bullets",
    type: "circle",
    source: "route",
    filter: ["all", ["==", ["geometry-type"], "Point"], ["!", ["get", "express"]]],
    paint: {
      "circle-radius": 10,
      "circle-color": ["get", "color"],
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 2,
      "circle-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
      "circle-stroke-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
    },
  });
  // An express variant's, as a diamond, outlined in white as the circles are.
  map.addImage("express-diamond", diamond(), { sdf: true });
  map.addLayer({
    id: "route-diamonds",
    type: "symbol",
    source: "route",
    filter: ["all", ["==", ["geometry-type"], "Point"], ["get", "express"]],
    layout: {
      "icon-image": "express-diamond",
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
    },
    paint: {
      "icon-color": ["get", "color"],
      "icon-halo-color": "#ffffff",
      "icon-halo-width": 2,
      "icon-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
    },
  });
  map.addLayer({
    id: "route-bullet-names",
    type: "symbol",
    source: "route",
    filter: ["==", ["geometry-type"], "Point"],
    layout: {
      "text-field": ["get", "name"],
      "text-font": ["Noto Sans Bold"],
      "text-size": 13,
      "text-allow-overlap": true,
      "text-ignore-placement": true,
    },
    paint: {
      "text-color": ["get", "text"],
      "text-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
    },
  });
  // A shuttle's which, small in its bullet's upper right, outlined in its color to stand off the map.
  map.addLayer({
    id: "route-bullet-corners",
    type: "symbol",
    source: "route",
    filter: ["all", ["==", ["geometry-type"], "Point"], ["!=", ["get", "corner"], ""]],
    layout: {
      "text-field": ["get", "corner"],
      "text-font": ["Noto Sans Bold"],
      "text-size": 9,
      "text-offset": [1.1, -1.1],
      "text-allow-overlap": true,
      "text-ignore-placement": true,
    },
    paint: {
      "text-color": ["get", "text"],
      "text-halo-color": ["get", "color"],
      "text-halo-width": 2,
      "text-opacity": ["case", ["get", "faded"], ROUTE_FADED_OPACITY, 1],
    },
  });
  // The white M in each new station's bullet, in bold Helvetica as the MTA's is.
  map.addImage("queenslink-m", imageData(legendM), { pixelRatio: devicePixelRatio });
  const mLayout: SymbolLayerSpecification["layout"] = {
    "icon-image": "queenslink-m",
    "icon-size": [
      "interpolate",
      ["linear"],
      ["zoom"],
      10,
      NEW_STATION_RADII[0] / NEW_STATION_RADII[1],
      14,
      1,
    ],
    "icon-allow-overlap": true,
    "icon-ignore-placement": true,
  };
  map.addLayer({
    id: "new-stations-m",
    type: "symbol",
    source: "stations",
    filter: ["get", "new"],
    layout: mLayout,
  });
  map.addLayer({
    id: "selected-station",
    type: "circle",
    source: "stations",
    filter: ["==", ["get", "id"], ""],
    paint: {
      "circle-radius": ringRadius(),
      "circle-color": "rgba(0, 0, 0, 0)",
      "circle-stroke-color": "#000000",
      "circle-stroke-width": RING_WIDTH,
    },
  });
  // The selected station again, over the ring and every other station,
  // so that it's in front of any it overlaps.
  map.addLayer({
    id: "selected-station-circle",
    type: "circle",
    source: "stations",
    filter: ["==", ["get", "id"], ""],
    paint: stationPaint,
  });
  map.addLayer({
    id: "selected-station-m",
    type: "symbol",
    source: "stations",
    filter: ["==", ["get", "id"], ""],
    layout: mLayout,
  });

  map.on("click", (event: MapMouseEvent) => {
    // Not the tap a hold ends in, which would pick the station held.
    if (held) {
      held = false;
      return;
    }
    const fromMouse =
      event.originalEvent instanceof PointerEvent && event.originalEvent.pointerType === "mouse";
    const id = stationAt(event, fromMouse ? MOUSE_RADIUS : HIT_RADIUS);
    if (id !== undefined) {
      void selectStation(id);
    } else {
      // On a phone, which has no pointing, this is the only way to see what a band is.
      pinBand(event.lngLat);
    }
  });
  map.on("mousemove", (event: MapMouseEvent) => {
    const id = stationAt(event, MOUSE_RADIUS);
    map.getCanvas().style.cursor = id === undefined ? "" : "pointer";
    hoverStation(id);
    const station = id === undefined ? undefined : stationsById.get(id);
    if (station === undefined) {
      showBand(bandAt(event.lngLat), event.lngLat);
      return;
    }
    // Which station a click would pick, as they're close together in much of the city.
    showStationPopup(station);
  });
  function showStationPopup(station: Station): void {
    highlightBand(pinned?.band ?? NOTHING);
    const label = document.createElement("span");
    label.append(...stationLabel(station));
    // How far it is, or how much is saved getting there, in the view shown.
    const minutes = stationMinutes(station);
    if (minutes === ONLY_QUEENSLINK) {
      label.append(": only with QueensLink");
    } else if (minutes !== NOTHING) {
      label.append(`: ${minutes} min${currentView() === "saved" ? " saved" : ""}`);
    }
    showRoute(station);
    for (const run of shownRuns()) {
      if (!reachedIn(station, run)) {
        continue;
      }
      const line = journeyAt(station, departureOf(station, run), undefined, run);
      if (line !== undefined) {
        label.append(line);
      }
    }
    bandPopup
      .setLngLat([station.lon, station.lat])
      .setMaxWidth("none")
      .setDOMContent(label)
      .addTo(map);
    placeBandPopup();
  }
  // On a phone, which can't point, holding a station says what pointing at it would,
  // without picking it, until the next tap.
  let holding: number | undefined;
  let held = false;
  const stopHolding = (): void => {
    clearTimeout(holding);
    holding = undefined;
  };
  // On a phone, the touch starts a little before the tap it ends in.
  map.on("touchstart", (event: MapTouchEvent) => {
    stopHolding();
    held = false;
    const id = event.points.length === 1 ? stationAt(event) : undefined;
    if (id === undefined) {
      return;
    }
    prefetchTimes(id);
    holding = window.setTimeout(() => {
      const station = stationsById.get(id);
      if (station !== undefined) {
        held = true;
        showStationPopup(station);
      }
    }, HOLD_MS);
  });
  map.on("touchmove", stopHolding);
  map.on("touchcancel", stopHolding);
  map.on("touchend", stopHolding);
  // Not the browser's own menu for a long press.
  map.getCanvas().addEventListener("contextmenu", (event) => {
    if (held) {
      event.preventDefault();
    }
  });
  map.getCanvas().addEventListener("mouseleave", () => {
    hoverStation(undefined);
    restorePinnedBand();
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
