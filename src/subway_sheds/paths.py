from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Everything downloaded or computed: it's large and reproducible, so it isn't committed.
CACHE = ROOT / ".cache"

OSM_PBF = CACHE / "nyc.osm.pbf"

# NYC's land, from `nyc-subway-isochrones`, to keep the grid out of the water.
SHORELINE = CACHE / "nyc-shoreline.json"

# The QueensLink scenario feed, committed unpacked so its repairs are commits.
QUEENSLINK_FEED = ROOT / "gtfs" / "queenslink"

# Feeds zipped or downloaded as R5 reads them.
GTFS = CACHE / "gtfs"

TRAVEL_TIMES = CACHE / "travel_times"

# What the web page loads; `web/` serves `public/` as is.
WEB_DATA = ROOT / "web" / "public" / "data"
