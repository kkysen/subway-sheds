import datetime
from enum import StrEnum
from pathlib import Path

from subway_sheds import gtfs, paths

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
# QueensLink's M runs a little more often than the M does, so the baseline's runs as often as
# MTA's M, hour by hour, at Myrtle-Wyckoff Avs, which both serve.
M = "M"
M_FREQUENCY_STATION = "M08"
ROCKAWAY_PARK_SHUTTLE = "H"
# On weekends, QueensLink's M runs its whole route,
# but MTA's runs only between Middle Village and Essex St, so the baseline runs MTA's.
WEEKEND_SERVICES = frozenset({"Saturday", "Sunday"})
# QueensLink also runs the G past Court Sq along the Queens Blvd local to Forest Hills;
# the baseline cuts it back to the stations it serves in MTA's feed.
G = "G"
# The QueensLink feed also runs the weekday Q, and one R, on past 96 St to 125 St,
# as Second Avenue Subway phase 2, with their times otherwise MTA's, but neither scenario does,
# so that the baseline is the subway now and QueensLink is all the other adds.
# Its stations, their depths (`access.PLANNED`), and their changes stay in the data,
# to add back as a scenario of their own.
PHASE_2 = frozenset({"Q06", "Q07", "Q08"})


class Day(StrEnum):
    # The morning peak of the weekday with the most trips, around 8 am.
    WEEKDAY = "weekday"
    # Midday of the Saturday with the most trips, around noon, since weekends have no peak.
    SATURDAY = "saturday"

    @property
    def weekday(self) -> int:
        return 2 if self is Day.WEEKDAY else 5

    @property
    def departure(self) -> datetime.time:
        """
        When the hour of departures starts, half an hour before the whole hour it's around,
        so that the page can call it by that hour.
        """
        return datetime.time(7, 30) if self is Day.WEEKDAY else datetime.time(11, 30)


# Stations changed between free on the streets, out of the system,
# each with any station of the other's complex:
# the MTA's at Lexington Av/63 St and 59 St, and at Junius St and Livonia Av,
# and QueensLink's, at Liberty Av to both A stations near it, and Jamaica Ave to the J's 104 St.
FREE_TRANSFERS = [
    ("B08", "R11"),  # Lexington Av/63 St, Lexington Av/59 St
    ("254", "L26"),  # Junius St, Livonia Av
    ("V04", "A61"),  # Liberty Av, Rockaway Blvd
    ("V04", "A63"),  # Liberty Av, 104 St
    ("V02", "J14"),  # Jamaica Ave, 104 St
]


class Fare(StrEnum):
    # A single fare, so a journey can't leave the system and pay again to change trains,
    # but for `FREE_TRANSFERS`.
    SINGLE = "single"
    # An unlimited card, so it can, changing between stations on the streets.
    UNLIMITED = "unlimited"

    @property
    def pays_again(self) -> bool:
        return self is Fare.UNLIMITED


class Scenario(StrEnum):
    # The QueensLink feed without QueensLink's M branch and G extension.
    BASELINE = "baseline"
    # The QueensLink scenario feed, without Second Avenue Subway phase 2 (see `PHASE_2`).
    QUEENSLINK = "queenslink"

    @property
    def gtfs(self) -> Path:
        return paths.GTFS / f"{self}.zip"


def _mta_stations(mta: gtfs.Feed, route: str) -> set[str]:
    """The parent stations `route`'s trips stop at in `mta`."""
    parents = {s["stop_id"]: s["parent_station"] or s["stop_id"] for s in mta.rows["stops.txt"]}
    trips = {t["trip_id"] for t in mta.rows["trips.txt"] if t["route_id"] == route}
    return {parents[st["stop_id"]] for st in mta.rows["stop_times.txt"] if st["trip_id"] in trips}


def queenslink_feed(mta: gtfs.Feed) -> gtfs.Feed:
    """The QueensLink feed, without Second Avenue Subway phase 2, as `PHASE_2` describes:
    each route stopping at its stations cut back to the stations it stops at in MTA's feed."""
    feed = gtfs.read_feed(paths.QUEENSLINK_FEED)
    parents = {s["stop_id"]: s["parent_station"] or s["stop_id"] for s in feed.rows["stops.txt"]}
    trips = {
        st["trip_id"] for st in feed.rows["stop_times.txt"] if parents[st["stop_id"]] in PHASE_2
    }
    for route in sorted({t["route_id"] for t in feed.rows["trips.txt"] if t["trip_id"] in trips}):
        gtfs.keep_route_stations(feed, route, _mta_stations(mta, route))
    return feed


def baseline_feed(mta: gtfs.Feed) -> gtfs.Feed:
    """`queenslink_feed` without QueensLink, as `BASELINE` describes."""
    feed = queenslink_feed(mta)
    gtfs.splice_routes(feed, mta, {ROCKAWAY_PARK_SHUTTLE})
    gtfs.drop_trips(
        feed,
        {
            t["trip_id"]
            for t in feed.rows["trips.txt"]
            if t["route_id"] == QUEENSLINK_M and t["service_id"] in WEEKEND_SERVICES
        },
    )
    gtfs.splice_routes(
        feed, mta, {M}, lambda service: any(day in service for day in WEEKEND_SERVICES)
    )
    gtfs.reroute_branch(
        feed,
        QUEENSLINK_M,
        QUEENSLINK_M_BRANCH,
        QUEENSLINK_M_BRANCH_STATION,
        M_TERMINAL,
        M_TERMINAL_LIKE,
    )
    gtfs.match_frequencies(feed, QUEENSLINK_M, mta, M, M_FREQUENCY_STATION)
    gtfs.keep_route_stations(feed, G, _mta_stations(mta, G))
    return feed
