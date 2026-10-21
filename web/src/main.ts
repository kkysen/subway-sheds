import "maplibre-gl/dist/maplibre-gl.css";
import { type ExpressionSpecification, type GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";

type View = "baseline" | "queenslink" | "saved";

const views: readonly View[] = ["baseline", "queenslink", "saved"];

function isView(value: unknown): value is View {
  return views.some((view) => view === value);
}

// Travel time in minutes, in the 10-minute bands the original subway isochrones used.
const travelTimeColor = (scenario: "baseline" | "queenslink"): ExpressionSpecification => [
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
  0,
  "rgba(0, 0, 0, 0)",
  1,
  "#deebf7",
  10,
  "#6baed6",
  20,
  "#08519c",
];

const fillColor = (view: View): ExpressionSpecification =>
  view === "saved" ? savedColor : travelTimeColor(view);

// Hide cells the shown scenario doesn't reach within the 90 minutes routed.
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
const stationInput = element("input[name=station]", HTMLInputElement);

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
  center: [-73.87, 40.68],
  zoom: 10.5,
});

const dataUrl = (station: string): string => `/data/${encodeURIComponent(station)}.geojson`;

map.on("load", () => {
  const view = currentView();
  map.addSource("cells", { type: "geojson", data: dataUrl(stationInput.value) });
  map.addLayer(
    {
      id: "cells",
      type: "fill",
      source: "cells",
      filter: filter(view),
      paint: { "fill-color": fillColor(view), "fill-opacity": 0.6 },
    },
    // Under the labels, so street and place names stay readable.
    map.getStyle().layers.find((layer) => layer.type === "symbol")?.id,
  );
});

controls.addEventListener("change", (event) => {
  if (event.target === stationInput) {
    const source = map.getSource<GeoJSONSource>("cells");
    void source?.setData(dataUrl(stationInput.value));
    return;
  }
  const view = currentView();
  map.setFilter("cells", filter(view));
  map.setPaintProperty("cells", "fill-color", fillColor(view));
});
