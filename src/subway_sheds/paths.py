from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Everything downloaded or computed: it's large and reproducible, so it isn't committed.
CACHE = ROOT / ".cache"

# The streets routing walks on, filtered from Geofabrik's New York State extract,
# and the `state.txt` Geofabrik gave for the extract they were filtered from,
# which names the OSM data's version.
OSM_PBF = CACHE / "nyc-streets.osm.pbf"
OSM_SOURCE_STATE = CACHE / "nyc-streets.source-state.txt"

# NYC's land, from `nyc-subway-isochrones`, to keep the grid out of the water.
SHORELINE = CACHE / "nyc-shoreline.json"

# The QueensLink scenario feed, committed unpacked so its repairs are commits.
QUEENSLINK_FEED = ROOT / "gtfs" / "queenslink"

# Feeds zipped or downloaded as routing reads them.
GTFS = CACHE / "gtfs"

# `mta-od-data`'s physical platforms from OSM, at the commit `route/Cargo.toml` pins,
# with `stacked_platforms.csv` beside it.
PLATFORMS = CACHE / "platforms" / "platforms.csv"

TRAVEL_TIMES = CACHE / "travel_times"

# What the web page loads; `web/` serves `public/` as is.
WEB_DATA = ROOT / "web" / "public" / "data"
