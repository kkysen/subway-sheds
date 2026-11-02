from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Everything downloaded, and what's filtered from it, kept to not download it again.
CACHE = ROOT / ".cache"

# Everything the pipeline computes, from the committed inputs and `CACHE`.
OUT = ROOT / "out"

# The streets routing walks on, filtered from Geofabrik's New York State extract,
# and the `state.txt` Geofabrik gave for the extract they were filtered from,
# which names the OSM data's version.
OSM_PBF = CACHE / "nyc-streets.osm.pbf"
OSM_SOURCE_STATE = CACHE / "nyc-streets.source-state.txt"

# NYC's land, from `nyc-subway-isochrones`, to keep the grid out of the water.
SHORELINE = CACHE / "nyc-shoreline.json"

# QueensLink's new track: the OSM ways its run times are measured on, trimmed to what's used
# (from Overpass, (c) OpenStreetMap contributors, ODbL), and the run times and their calibration.
QUEENSLINK_OSM = ROOT / "data" / "queenslink" / "osm.json"
QUEENSLINK_RUN_TIMES = ROOT / "data" / "queenslink" / "run_times.csv"
QUEENSLINK_RUN_TIME_CALIBRATION = ROOT / "data" / "queenslink" / "run_time_calibration.csv"
# QueensLink's new stations and their platforms, as `stops.txt` rows, where QueensLink puts them.
QUEENSLINK_STOPS = ROOT / "data" / "queenslink" / "stops.csv"

# The scenarios' feeds, zipped as routing reads them.
GTFS = OUT / "gtfs"

# `mta-od-data`'s physical platforms from OSM, at the commit `route/Cargo.toml` pins,
# with `stacked_platforms.csv` beside it.
PLATFORMS = CACHE / "platforms" / "platforms.csv"

TRAVEL_TIMES = OUT / "travel_times"

# What the web page loads; `web/` serves `public/` as is.
WEB_DATA = ROOT / "web" / "public" / "data"
