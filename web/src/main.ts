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
import { type Arrivals, parseArrivals, parseExits, travelTimes, UNREACHED } from "./times";

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

// Fetched first, as reading a station's arrivals needs how many stations there are.
const stationList = fetchJson<Station[]>("stations.json");

async function fetchBinary(path: string): Promise<ArrayBuffer> {
  const response = await fetch(data(path));
  if (!response.ok) {
    throw new Error(`${response.status} fetching ${path}`);
  }
  return response.arrayBuffer();
}

async function fetchTimesUncached(station: string): Promise<Arrivals> {
  const buffer = await fetchBinary(`arrivals/${encodeURIComponent(station)}.bin`);
  return parseArrivals(buffer, (await stationList).length);
}

// How many stations' arrivals are kept, about 240 KB each unpacked,
// so that pointing across the city doesn't keep all of them.
const CACHED_STATIONS = 64;

// The times fetched or being fetched, least recently asked for first,
// so that a click on a station already fetched for being pointed at waits on that fetch.
const timesCache = new Map<string, Promise<Arrivals>>();

function fetchTimes(station: string): Promise<Arrivals> {
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
// Few of them, so that each band is easy to tell from the next.
const BAND_STARTS = [15, 30, 45, 60, 75] as const;

// The minutes travel times are drawn out to.
const MAX_MINUTES = 90;

// The minutes routed out to, past which a cell isn't reached: past `MAX_MINUTES`,
// so that there's saying how much QueensLink saves places that take longer than that without it.
const ROUTED_MINUTES = 120;

// Travel time in minutes, from light to dark through QueensLink's orange, `#f48335`, to red, as QueensLink's own maps are,
// lighter to darker all the way, so that the order reads without telling hues apart.
const travelTimeBands: readonly Band[] = [
  [0, "#f8dbc9"],
  [BAND_STARTS[0], "#f6be99"],
  [BAND_STARTS[1], "#f9a66e"],
  [BAND_STARTS[2], "#f48335"],
  [BAND_STARTS[3], "#dc5f27"],
  [BAND_STARTS[4], "#c44027"],
];

// Minutes saved, from a minute, from light teal to QueensLink's green, `#24685b`, as QueensLink's own maps are.
const savedBands: readonly Band[] = [
  [1, "#8cd1c6"],
  [BAND_STARTS[0], "#6ebfb0"],
  [BAND_STARTS[1], "#55aa9a"],
  [BAND_STARTS[2], "#449586"],
  [BAND_STARTS[3], "#3a8578"],
  [BAND_STARTS[4], "#31796c"],
  [MAX_MINUTES, "#24685b"],
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
function cellClasses(grid: Grid, times: Uint8Array, day: Day, view: View): Int8Array {
  const { width, height } = rasterSize(grid);
  const stride = timesStride(grid);
  // Each day's times are every cell's and station's in the baseline, then with QueensLink.
  const start = days.indexOf(day) * 2 * stride;
  const classes = new Int8Array(width * height).fill(NOTHING);
  grid.cols.forEach((col, i) => {
    const baseline = times[start + i] ?? UNREACHED;
    const queenslink = times[start + stride + i] ?? UNREACHED;
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

// In a station's minutes saved, one only QueensLink reaches, which there's no saying how much it saves.
const ONLY_QUEENSLINK = -2;

// The minutes from the selected station to `station` itself, in the view shown:
// the trip's, or those saved, `ONLY_QUEENSLINK`, or `NOTHING` where there's none to say.
function stationMinutes(station: Station): number {
  if (selectedTimes === undefined) {
    return NOTHING;
  }
  const stride = timesStride(grid);
  const at = days.indexOf(currentDay()) * 2 * stride + grid.cols.length + stations.indexOf(station);
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
  return baseline === UNREACHED ? ONLY_QUEENSLINK : Math.max(baseline - queenslink, 0);
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
const speedValue = element("#speed-value", HTMLOutputElement);
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
const savedControls = ["day", "view", "speed"] as const;

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

const [grid, stations, lines, exits] = await Promise.all([
  fetchJson<Grid>("grid.json"),
  stationList,
  fetchJson<GeoJSON.FeatureCollection>("lines.json"),
  stationList.then(async (list) => parseExits(await fetchBinary("exits.bin"), list.length)),
]);
const { width, height } = rasterSize(grid);
const bandsLayer = new BandsLayer("bands", grid, width, height);
const stationsById = new Map(stations.map((station) => [station.id, station]));

// Broad Channel, whose trip to most of the city QueensLink shortens.
const DEFAULT_STATION = "H04";

// The station last asked for, so that a slower fetch for an earlier one can't replace it.
let selected = "";
// When trains from the station shown reach every station.
let selectedArrivals: Arrivals | undefined;
// The times from the station shown, at the walking speed chosen.
let selectedTimes: Uint8Array | undefined;
// Each cell's class, as drawn.
let selectedClasses: Int8Array | undefined;

function computeTimes(): void {
  if (selectedArrivals !== undefined) {
    const origin = stations.findIndex((station) => station.id === selected);
    selectedTimes = travelTimes(
      selectedArrivals,
      exits,
      origin,
      grid.cols.length,
      stations.length,
      currentSpeed(),
      // Only the day shown, both scenarios, as the minutes saved need both.
      [2 * days.indexOf(currentDay()), 2 * days.indexOf(currentDay()) + 1],
    );
  }
}

// Recomputed as the speed slides, at most once a frame.
let speedFrame = 0;
controls.addEventListener("input", (event) => {
  if (event.target instanceof HTMLInputElement && event.target.name === "speed") {
    showSpeed();
    cancelAnimationFrame(speedFrame);
    speedFrame = requestAnimationFrame(() => {
      computeTimes();
      drawCells();
    });
  }
});

function drawCells(): void {
  if (selectedTimes !== undefined) {
    const view = currentView();
    selectedClasses = cellClasses(grid, selectedTimes, currentDay(), view);
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
  if (station === undefined) {
    return;
  }
  selected = id;
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
  const arrivals = await fetchTimes(id);
  if (selected !== id) {
    return;
  }
  selectedArrivals = arrivals;
  computeTimes();
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
  computeTimes();
  drawCells();
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
function bandAt(lngLat: LngLat): number {
  if (selectedClasses === undefined) {
    return NOTHING;
  }
  // In Web Mercator meters, as the grid is, then in cells.
  const x = (lngLat.lng / 360) * EARTH_CIRCUMFERENCE;
  const y =
    (Math.log(Math.tan(Math.PI / 4 + (lngLat.lat * Math.PI) / 360)) / (2 * Math.PI)) *
    EARTH_CIRCUMFERENCE;
  const col = (x - grid.x0) / grid.spacing;
  const row = (y - grid.y0) / grid.spacing;
  if (col < -0.5 || col > width - 0.5 || row < -0.5 || row > height - 0.5) {
    return NOTHING;
  }
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

// MapLibre's own, which a band's description wraps in, but a station's name and minutes are kept on one line past.
const BAND_POPUP_WIDTH = "240px";
const bandPopup = new Popup({ closeButton: false, closeOnClick: false, className: "band-popup" });

highlightBand = (band: number): void => {
  bandsLayer.setHighlight(band);
  for (const key of legend.querySelectorAll<HTMLElement>(".legend-bands [data-band]")) {
    key.classList.toggle("highlighted", key.dataset["band"] === String(band));
  }
};

// The band clicked or tapped, kept highlighted and said until clicked again or another is,
// and where, for its popup, if on the map rather than in the legend.
let pinned: { readonly band: number; readonly lngLat?: LngLat } | undefined;

// Highlights a band and says what it is at a point, or shows the pinned one if there's none.
function showBand(band: number, lngLat: LngLat): void {
  if (band === NOTHING) {
    restorePinnedBand();
    return;
  }
  highlightBand(band);
  bandPopup
    .setLngLat(lngLat)
    .setMaxWidth(BAND_POPUP_WIDTH)
    .setText(describeBand(band, currentView()))
    .addTo(map);
}

restorePinnedBand = (): void => {
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
    if (id !== undefined) {
      prefetchTimes(id);
    }
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
    bandPopup
      .setLngLat([station.lon, station.lat])
      .setMaxWidth("none")
      .setDOMContent(label)
      .addTo(map);
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
  map.getCanvas().addEventListener("mouseleave", restorePinnedBand);

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
