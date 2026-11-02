"""
Where each download comes from, pinned, and where it's kept.

CI caches the downloads by this file's hash, so changing a pin here downloads again,
and nothing else here should change it.
"""

from dataclasses import dataclass
from pathlib import Path

from subway_sheds import paths

# The Mobility Database's copy of MTA's latest subway feed
# (https://mobilitydatabase.org/feeds/gtfs/mdb-516), 2026-11-01 to 2027-05-15.
MTA_DATASET = "mdb-516-202610290031"

# NYC's land, from `nyc-subway-isochrones`, to keep the grid out of the water.
SHORELINE_URL = (
    "https://raw.githubusercontent.com/chriswhong/nyc-subway-isochrones"
    "/master/data/nyc-shoreline-clip-multi.json"
)


@dataclass(frozen=True)
class Archived:
    """A file only an archive still has, checked against its SHA-256 once downloaded."""

    url: str
    sha256: str
    dest: Path


# MTA's subway feed of fall 2019, after CBTC on the 7 (November 2018),
# before the pandemic, and before the Flushing Line work that has run the 7X local
# between Queensboro Plaza and 74 St-Broadway since 2023-06-26,
# for the 7's full service as it'll be once that work is done.
# Built 2020-01-09 for 2020-01-02 to 2020-05-01; the Wayback Machine's only copy of it,
# which it serves gzipped, so its SHA-256 is of the zip inside.
MTA_2019 = Archived(
    "https://web.archive.org/web/20200426183023id_/"
    "http://web.mta.info/developers/data/nyct/subway/google_transit.zip",
    "0a2ddc58a9274f1d4d0a551eb63607d44c91a17878750f0824b5c29d6f65b847",
    paths.CACHE / "wayback" / "mta-subway-2019.zip",
)


# QueensLink's diagram of where its M's tracks leave the Queens Blvd local at Rego Park,
# which `run_times.diagram` traces the curves from.
QUEENSLINK_DIAGRAM = Archived(
    "https://thequeenslink.org/wp-content/uploads/2023/07/QBL_RP.png",
    "fb157d514ab4f1e357b52930ce2500d4ccaced72221bfc6d81f33fc205ab09ef",
    paths.CACHE / "queenslink" / "QBL_RP.png",
)


def mta_feed() -> Path:
    return paths.CACHE / "mdb" / f"{MTA_DATASET}.zip"


def mta_feed_url() -> str:
    feed = MTA_DATASET.rsplit("-", 1)[0]
    return f"https://files.mobilitydatabase.org/{feed}/{MTA_DATASET}/{MTA_DATASET}.zip"
