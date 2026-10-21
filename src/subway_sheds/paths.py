from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Everything downloaded or computed: it's large and reproducible, so it isn't committed.
CACHE = ROOT / ".cache"

OSM_PBF = CACHE / "nyc.osm.pbf"

# The QueensLink scenario feed as given, a zip with its files under `B1_MVS/`.
QUEENSLINK_SOURCE = ROOT / "B1_MVS.zip"

# Feeds repacked as R5 wants them: flat zips with valid stops.
GTFS = CACHE / "gtfs"

TRAVEL_TIMES = CACHE / "travel_times"

# What the web page loads; `web/` serves `public/` as is.
WEB_DATA = ROOT / "web" / "public" / "data"
