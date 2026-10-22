from enum import StrEnum
from pathlib import Path

from subway_sheds import gtfs, paths

# The Mobility Database's copy of MTA's subway feed that the QueensLink feed was made from
# (https://mobilitydatabase.org/feeds/gtfs/mdb-516), 2025-11-02 to 2025-12-07.
BASELINE_DATASET = "mdb-516-202511010043"

# The baseline is the QueensLink feed without QueensLink, so that QueensLink is all that differs,
# rather than MTA's, from before the F and M swapped tunnels, which the QueensLink feed has.
# QueensLink's M branches off the Queens Blvd local at 63 Dr toward the Rockaways;
# in the baseline it runs on to Forest Hills-71 Av as the M does since the swap,
# with the R's running times, and the Rockaway Park shuttle is back, from MTA's feed.
QUEENSLINK_M = "M*"
QUEENSLINK_M_BRANCH = "G10"
# A station on QueensLink's M's branch.
QUEENSLINK_M_BRANCH_STATION = "V01"
M_TERMINAL = ["G09", "G08"]
M_TERMINAL_LIKE = "R"
ROCKAWAY_PARK_SHUTTLE = "H"
# QueensLink also runs the G past Court Sq along the Queens Blvd local to Forest Hills;
# the baseline cuts it back to the stations it serves in MTA's feed.
G = "G"


class Scenario(StrEnum):
    # The QueensLink feed without QueensLink's M branch and G extension.
    BASELINE = "baseline"
    # The QueensLink scenario feed.
    QUEENSLINK = "queenslink"

    @property
    def gtfs(self) -> Path:
        return paths.GTFS / f"{self}.zip"


def mta_feed() -> Path:
    return paths.CACHE / "mdb" / f"{BASELINE_DATASET}.zip"


def mta_feed_url() -> str:
    feed = BASELINE_DATASET.rsplit("-", 1)[0]
    return f"https://files.mobilitydatabase.org/{feed}/{BASELINE_DATASET}/{BASELINE_DATASET}.zip"


def baseline_feed() -> gtfs.Feed:
    """The QueensLink feed without QueensLink, as `BASELINE` describes."""
    feed = gtfs.read_feed(paths.QUEENSLINK_FEED)
    mta = gtfs.read_feed(mta_feed())
    gtfs.splice_routes(feed, mta, {ROCKAWAY_PARK_SHUTTLE})
    gtfs.reroute_branch(
        feed,
        QUEENSLINK_M,
        QUEENSLINK_M_BRANCH,
        QUEENSLINK_M_BRANCH_STATION,
        M_TERMINAL,
        M_TERMINAL_LIKE,
    )
    mta_parents = {s["stop_id"]: s["parent_station"] or s["stop_id"] for s in mta.rows["stops.txt"]}
    mta_g_trips = {t["trip_id"] for t in mta.rows["trips.txt"] if t["route_id"] == G}
    mta_g_stations = {
        mta_parents[st["stop_id"]]
        for st in mta.rows["stop_times.txt"]
        if st["trip_id"] in mta_g_trips
    }
    gtfs.keep_route_stations(feed, G, mta_g_stations)
    return feed
