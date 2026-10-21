from enum import StrEnum
from pathlib import Path

from subway_sheds import paths

# The Mobility Database's copy of MTA's subway feed that the QueensLink feed was made from
# (https://mobilitydatabase.org/feeds/gtfs/mdb-516), 2025-11-02 to 2025-12-07.
BASELINE_DATASET = "mdb-516-202511010043"

# The QueensLink feed's changes to that feed that aren't QueensLink, taken into the baseline too,
# so that QueensLink is all that differs:
# the Q to 125 St, since the QueensLink feed assumes Second Avenue Subway Phase 2 is open,
# and the 7 on its regular schedule, rather than the one without a regular 7 express
# during construction.
BASELINE_ROUTES_FROM_QUEENSLINK = frozenset({"Q", "7", "7X"})


class Scenario(StrEnum):
    # The subway as MTA scheduled it then, with `BASELINE_ROUTES_FROM_QUEENSLINK`.
    BASELINE = "baseline"
    # The same with QueensLink's M added, from the QueensLink scenario feed.
    QUEENSLINK = "queenslink"

    @property
    def gtfs(self) -> Path:
        return paths.GTFS / f"{self}.zip"


def mta_feed() -> Path:
    return paths.CACHE / "mdb" / f"{BASELINE_DATASET}.zip"


def mta_feed_url() -> str:
    feed = BASELINE_DATASET.rsplit("-", 1)[0]
    return f"https://files.mobilitydatabase.org/{feed}/{BASELINE_DATASET}/{BASELINE_DATASET}.zip"
