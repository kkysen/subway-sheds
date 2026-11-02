"""
The scenarios' feeds, made from MTA's latest:
the baseline, with the 7 as it'll run once the Flushing Line's work is done,
and QueensLink, adding the QueensLink M and running the G on to Forest Hills.
"""

import copy
import csv
import datetime
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from subway_sheds import paths
from subway_sheds.timetable import (
    Call,
    ServiceDay,
    Timetable,
    Track,
    Trip,
    calls,
    ending,
    hold,
    shift,
)


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
    # MTA's latest feed, with the 7 run as it was before the Flushing Line's work.
    BASELINE = "baseline"
    # The baseline with QueensLink.
    QUEENSLINK = "queenslink"

    @property
    def gtfs(self) -> Path:
        return paths.GTFS / f"{self}.zip"


# Times between stations taken from MTA's schedule are the median of trips starting in these hours.
HOURS = (6, 20)


def _local(*stations: str) -> Callable[[Trip], bool]:
    """Whether a trip stops at any of `stations`, which only trains on one track stop at."""
    return lambda trip: not set(stations).isdisjoint(trip.stations())


def _platforms(*stations: str) -> set[str]:
    return {f"{station}{d}" for station in stations for d in "NS"}


# The 7 and 7X, whose express the Flushing Line's work has run local since 2023,
# between Queensboro Plaza and 74 St-Broadway toward Manhattan,
# and whose locals it has run past 52 St and 69 St toward Flushing.
SEVEN = ("7", "7X")
QUEENSBORO_PLAZA = "718"


def _outward(trip: Trip) -> list[Call]:
    """`trip`'s calls toward Flushing from Queensboro Plaza, starting there."""
    return trip.calls[trip.index(QUEENSBORO_PLAZA) :]


def restore_seven(timetable: Timetable, before: Timetable) -> None:
    """
    Run the 7 and 7X as `before` did, MTA's last feed before the Flushing Line's work,
    with as many trains as now.

    Toward Manhattan, `before`'s trains, as many as now, 30 in the busiest hour,
    but quicker west of Queensboro Plaza, where every train runs on the same track,
    wherever MTA's schedule now runs quicker between two stations,
    by the difference in their median times, for every train alike, so they stay as far apart.
    East of it, where the express and local run on their own tracks and join again,
    quickening only some would put some too near others leaving Flushing-Main St.

    Toward Flushing, where `before` ran 29 trains in the busiest hour and MTA's schedule now 30,
    today's trains, east of Queensboro Plaza stopping and timed as `before`'s trip did
    on the same route and day, to the same station, passing Queensboro Plaza nearest the same time.
    """
    quicker: dict[ServiceDay, dict[tuple[str, str], int]] = {}
    for day in ServiceDay:
        now = timetable.stretches(SEVEN, day, HOURS)
        then = before.stretches(SEVEN, day, HOURS)
        quicker[day] = {pair: min(0, now[pair] - t) for pair, t in then.items() if pair in now}
    # Each trip's headsign, direction, and shape from a trip of today's with its route and ends.
    rows = {t["trip_id"]: t for t in timetable.feed.rows["trips.txt"]}
    like: dict[tuple[str, str, str], dict[str, str]] = {}
    services: dict[ServiceDay, str] = {}
    for trip in timetable.of(*SEVEN):
        like.setdefault((trip.route, trip.calls[0].stop, trip.calls[-1].stop), rows[trip.id])
        services[trip.day] = trip.service
    toward_flushing: dict[tuple[str, ServiceDay, str], list[list[Call]]] = defaultdict(list)
    for trip in before.of(*SEVEN):
        if trip.calls[-1].stop.endswith("N"):
            toward_flushing[trip.route, trip.day, trip.calls[-1].stop].append(_outward(trip))
    for trip in timetable.of(*SEVEN):
        if trip.calls[-1].stop.endswith("N"):
            at = trip.index(QUEENSBORO_PLAZA)
            plaza = trip.calls[at].departure
            then = min(
                toward_flushing[trip.route, trip.day, trip.calls[-1].stop],
                key=lambda outward: abs(outward[0].departure - plaza),
            )
            east = [Call(c.stop, c.arrival, c.departure) for c in then[1:]]
            shift(east, plaza - then[0].departure)
            trip.calls = trip.calls[: at + 1] + east
        else:
            timetable.drop([trip.id])
    for trip in before.of(*SEVEN):
        if trip.calls[-1].stop.endswith("N"):
            continue
        at = trip.index(QUEENSBORO_PLAZA)
        faster = quicker[trip.day]
        for i in range(at + 1, len(trip.calls)):
            shift(trip.calls[i:], faster.get((trip.calls[i - 1].stop, trip.calls[i].stop), 0))
        trip.service = services[trip.day]
        ends = (trip.route, trip.calls[0].stop, trip.calls[-1].stop)
        timetable.add(trip, like[ends] | {"trip_id": trip.id, "service_id": trip.service})


def _new_stops() -> list[dict[str, str]]:
    with paths.QUEENSLINK_STOPS.open() as f:
        return list(csv.DictReader(f))


def baseline(mta: Timetable, before: Timetable) -> None:
    """
    MTA's latest feed, with the 7 as `restore_seven` runs it,
    and QueensLink's stations, though no train stops at them,
    so that QueensLink's new stations can be routed from in the baseline too.
    """
    restore_seven(mta, before)
    mta.feed.rows["stops.txt"] += _new_stops()


# QueensLink's M runs as MTA's M, but past 63 Dr-Rego Park,
# off the Queens Blvd local onto the old Rockaway Beach Branch,
# and on along the A to Rockaway Park, as the Rockaway Park shuttle does,
# rather than on to Forest Hills.
M = "M"
TOWARD_ROCKAWAY = [
    "G10N",
    "V01S",
    "V02S",
    "V03S",
    "V04S",
    "H02S",
    "H03S",
    "H04S",
    "H12S",
    "H13S",
    "H14S",
    "H15S",
]
TOWARD_MANHATTAN = [
    "H15N",
    "H14N",
    "H13N",
    "H12N",
    "H04N",
    "H03N",
    "H02N",
    "H01N",
    "V04N",
    "V03N",
    "V02N",
    "V01N",
    "G10S",
]
REGO_PARK = "G10"
FOREST_HILLS = "G08"
LIBERTY_AV = "V04"
QUEENSBRIDGE = "B04"
ROCKAWAY_PARK_SHUTTLE = "H"
A = "A"
ROCKAWAY_PARK = "H15"
BROAD_CHANNEL = "H04"
# On weekends, MTA's M runs only between Middle Village and Essex St;
# QueensLink's runs on as on weekdays, waiting at Essex St for a gap if it has to.
ESSEX_ST = "M18"
# With the M running on weekends, the F runs via 53 St every day, as on weekdays,
# rather than via 63 St with the M's stations.
F = "F"
E = "E"
JACKSON_HTS = "G14"
VIA_53_ST = ["G14", "G21", "F09", "F11", "F12", "D15"]
VIA_63_ST = "B04"
# The G runs on past Court Sq on weekdays along the Queens Blvd local to Forest Hills,
# waiting at Court Sq for a gap between the R and M if it has to.
G = "G"
R = "R"
COURT_SQ = "G22"
QUEENS_BLVD_LOCAL = [f"G{i}" for i in (21, 20, 19, 18, 16, 15, 14, 13, 12, 11, 10, "09", "08")]


def _run_times() -> dict[tuple[str, str], int]:
    with paths.QUEENSLINK_RUN_TIMES.open() as f:
        return {
            (r["from_stop_id"], r["to_stop_id"]): int(r["scheduled_s"]) for r in csv.DictReader(f)
        }


def _stretch(*tables: dict[tuple[str, str], int]) -> Callable[[str, str], int]:
    """The time between two platforms, from the first of `tables` to have it."""

    def stretch(a: str, b: str) -> int:
        for table in tables:
            if (a, b) in table:
                return table[a, b]
            if (a[:-1], b[:-1]) in table:
                return table[a[:-1], b[:-1]]
        raise KeyError(f"no time from {a} to {b}")

    return stretch


def _queenslink_m(timetable: Timetable) -> tuple[list[Trip], list[Trip]]:
    """
    Run the M along QueensLink's branch, every day:
    its trips to and from Forest Hills turn off at 63 Dr-Rego Park instead,
    and on weekends, its trips to and from Essex St run on as on weekdays.
    Those, then those of them running on past Essex St.
    """
    run_times = _run_times()
    branch = {
        day: (
            run_times,
            timetable.stretches([A], day, HOURS),
            timetable.stretches([ROCKAWAY_PARK_SHUTTLE], day, HOURS),
        )
        for day in ServiceDay
    }
    # Weekends' M runs past Essex St as weekdays' does at midday.
    midday = timetable.stretches([M], ServiceDay.WEEKDAY, (10, 16))
    weekdays = next(
        t for t in timetable.of(M) if t.stations()[-1] == FOREST_HILLS and ESSEX_ST in t.stations()
    )
    toward = [c.stop for c in weekdays.calls[weekdays.index(ESSEX_ST) : weekdays.index(REGO_PARK)]]
    back = [f"{stop[:-1]}S" for stop in reversed(toward)]
    branched: list[Trip] = []
    extended: list[Trip] = []
    for trip in timetable.of(M):
        stations = trip.stations()
        if FOREST_HILLS in stations:
            at = trip.index(REGO_PARK)
            time = trip.calls[at].departure
            if stations[-1] == FOREST_HILLS:
                added = calls(TOWARD_ROCKAWAY, time, _stretch(*branch[trip.day]))
                trip.calls = trip.calls[: at + 1] + added[1:]
            else:
                added = ending(TOWARD_MANHATTAN, time, _stretch(*branch[trip.day]))
                trip.calls = added[:-1] + trip.calls[at:]
            branched.append(trip)
        elif trip.day is not ServiceDay.WEEKDAY and ESSEX_ST in (stations[0], stations[-1]):
            via = _stretch(midday, *branch[trip.day])
            if stations[-1] == ESSEX_ST:
                added = calls(toward + TOWARD_ROCKAWAY, trip.calls[-1].departure, via)
                trip.calls = trip.calls + added[1:]
            else:
                added = ending(TOWARD_MANHATTAN + back, trip.calls[0].departure, via)
                trip.calls = added[:-1] + trip.calls
            branched.append(trip)
            extended.append(trip)
    return branched, extended


def _far_rockaway_a(timetable: Timetable) -> None:
    """
    Run the A's few weekday trips to and from Rockaway Park to and from Far Rockaway instead,
    as QueensLink's M serves Rockaway Park.
    """
    far = next(t for t in timetable.of(A) if t.stations()[-1] == "H11")
    toward = [c.stop for c in far.calls[far.index(BROAD_CHANNEL) :]]
    back = [f"{stop[:-1]}N" for stop in reversed(toward)]
    for trip in timetable.of(A):
        stations = trip.stations()
        if ROCKAWAY_PARK not in stations:
            continue
        stretch = _stretch(timetable.stretches([A], trip.day, HOURS))
        at = trip.index(BROAD_CHANNEL)
        time = trip.calls[at].departure
        if stations[-1] == ROCKAWAY_PARK:
            trip.calls = trip.calls[: at + 1] + calls(toward, time, stretch)[1:]
        else:
            trip.calls = ending(back, time, stretch)[:-1] + trip.calls[at:]


def _f_via_53_st(timetable: Timetable) -> Counter[tuple[str, int | None]]:
    """
    Run the F's weekend trips via 63 St via 53 St instead, as on weekdays,
    timed as the E through 53 St, and as they were from Jackson Hts out to Jamaica.
    Via 53 St is quicker or slower than via 63 St by a minute or so,
    so the rest moves by the difference,
    and waits at 47-50 Sts for a gap on the 6 Av and Culver locals if it has to.
    How long they wait, to report.
    """
    weekdays = timetable.stretches([F], ServiceDay.WEEKDAY, HOURS)
    # Through 53 St, as the E on the same day, and on to 47-50 Sts as the F on weekdays.
    days = {day: _stretch(timetable.stretches([E], day, HOURS), weekdays) for day in ServiceDay}
    # Not the late nights' F, local in Queens, which the M doesn't run alongside.
    trips = [
        t
        for t in timetable.of(F)
        if VIA_63_ST in t.stations() and QUEENS_BLVD_LOCAL[1] not in t.stations()
    ]
    moving = {t.id for t in trips}

    def track(stations: list[str], on: Callable[[Trip], bool]) -> Track:
        return Track(timetable, _platforms(*stations), lambda t: t.id not in moving and on(t))

    # Where it waits for a gap if it has to, and the tracks past there it waits for:
    # at Jackson Hts for the E through 53 St, and at 47-50 Sts for the 6 Av and Culver locals.
    stages = [
        (JACKSON_HTS, [track(VIA_53_ST[1:-1], _local("F11"))]),
        (
            VIA_53_ST[-1],
            [
                track([f"D{i}" for i in range(15, 22)], _local("D18", "D19")),
                track([f"F{i}" for i in range(20, 28)], _local("F21")),
            ],
        ),
    ]
    held: Counter[tuple[str, int | None]] = Counter()
    for trip in sorted(trips, key=lambda t: t.calls[t.index(JACKSON_HTS)].departure):
        at = trip.index(JACKSON_HTS)
        rockefeller = trip.index(VIA_53_ST[-1])
        time = trip.calls[at].departure
        toward_brooklyn = at < rockefeller
        if toward_brooklyn:
            via = calls([f"{s}S" for s in VIA_53_ST], time, days[trip.day])
            rest = trip.calls[rockefeller + 1 :]
            shift(rest, via[-1].departure - trip.calls[rockefeller].departure)
            trip.calls = trip.calls[: at + 1] + via[1:] + rest
        else:
            via = ending([f"{s}N" for s in reversed(VIA_53_ST)], time, days[trip.day])
            rest = trip.calls[:rockefeller]
            shift(rest, via[0].departure - trip.calls[rockefeller].departure)
            trip.calls = rest + via[:-1] + trip.calls[at:]
        for station, waited in _fit(trip, stages, toward_brooklyn):
            held[f"F at {NAMES[station]}", waited] += 1
    return held


def _drop_shuttle(timetable: Timetable, m: list[Trip]) -> None:
    """
    Drop the Rockaway Park shuttle's trips while QueensLink's M runs to Rockaway Park,
    keeping those before its first and after its last, late at night.
    """
    spans: dict[str, tuple[int, int]] = {}
    for trip in m:
        time = trip.calls[trip.index(BROAD_CHANNEL)].departure
        lo, hi = spans.get(trip.service, (time, time))
        spans[trip.service] = (min(lo, time), max(hi, time))
    timetable.drop(
        trip.id
        for trip in timetable.of(ROCKAWAY_PARK_SHUTTLE)
        if trip.service in spans
        and spans[trip.service][0] <= trip.calls[0].departure <= spans[trip.service][1]
    )


def _g_to_forest_hills(timetable: Timetable) -> list[Trip]:
    """
    Run the G's weekday trips on past Court Sq to Forest Hills, as the R runs,
    those that fit between the R and M with a wait at Court Sq of at most `MOST_HELD`.
    The trips run on.
    """
    weekdays = _stretch(timetable.stretches([R], ServiceDay.WEEKDAY, HOURS), _run_times())
    local = Track(timetable, _platforms(*QUEENS_BLVD_LOCAL), _local("G20", "G13"))
    out = []
    trips = [t for t in timetable.of(G) if t.day is ServiceDay.WEEKDAY]
    for trip in sorted(trips, key=lambda t: t.calls[t.index(COURT_SQ)].departure):
        old = copy.deepcopy(trip.calls)
        at = trip.index(COURT_SQ)
        if at == len(trip.calls) - 1:
            stops = [f"{s}N" for s in [COURT_SQ, *QUEENS_BLVD_LOCAL]]
            trip.calls = trip.calls + calls(stops, old[at].departure, weekdays)[1:]
            later = True
        else:
            stops = [f"{s}S" for s in [*reversed(QUEENS_BLVD_LOCAL), COURT_SQ]]
            trip.calls = ending(stops, old[at].departure, weekdays)[:-1] + trip.calls
            at = len(stops) - 1
            later = False
        # Not those leaving Court Sq just after the service day starts, which would leave before.
        if hold(trip, at, later, [local]) is None or trip.calls[0].departure < 0:
            trip.calls = old
            continue
        local.add(trip)
        out.append(trip)
    return out


def queenslink(timetable: Timetable) -> Counter[tuple[str, int | None]]:
    """
    The baseline with QueensLink, as `_queenslink_m`, `_far_rockaway_a`, `_f_via_53_st`,
    `_drop_shuttle`, and `_g_to_forest_hills` run it,
    every train added or moved fitting at least `FLOOR` from the others on the same track,
    QueensLink's M waiting at Essex St on weekends and at Liberty Av for a gap,
    and the G at Court Sq,
    each trip run to another end given the headsign of the trips ending there.
    How many trips are held, or don't fit at all, at each, to report.
    """
    ends = {trip.id: trip.calls[-1].stop for trip in timetable.trips.values()}
    held = _queenslink(timetable)
    _headsigns(timetable, ends)
    return held


def _headsigns(timetable: Timetable, ends: dict[str, str]) -> None:
    """
    Give each trip now ending elsewhere than `ends` says it did
    the headsign most of the trips still ending where it now ends have.
    """
    rows = timetable.feed.rows["trips.txt"]
    trips = timetable.trips
    kept: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        trip = trips.get(row["trip_id"])
        if trip is not None and ends.get(trip.id) == trip.calls[-1].stop:
            kept[trip.calls[-1].stop][row["trip_headsign"]] += 1
    for row in rows:
        trip = trips.get(row["trip_id"])
        if trip is not None and ends.get(trip.id) != trip.calls[-1].stop:
            row["trip_headsign"] = kept[trip.calls[-1].stop].most_common(1)[0][0]


def _queenslink(timetable: Timetable) -> Counter[tuple[str, int | None]]:
    _far_rockaway_a(timetable)
    held = _f_via_53_st(timetable)
    m, extended = _queenslink_m(timetable)
    _drop_shuttle(timetable, m)
    moving = {t.id for t in m}

    def track(stations: list[str], on: Callable[[Trip], bool]) -> Track:
        return Track(timetable, _platforms(*stations), lambda t: t.id not in moving and on(t))

    six_av = [f"D{i}" for i in range(15, 22)]
    sixty_third = ["B04", "B06", "B08", "B10"]
    # Where QueensLink's M waits for a gap if it has to, and the tracks past there it waits for:
    # on weekends, at Essex St for the 6 Av local and 63 St,
    # and at 21 St-Queensbridge for the Queens Blvd local, where the weekday M already fits,
    # and every day at Liberty Av for the A and on to Rockaway Park.
    weekends = [
        (ESSEX_ST, [track(six_av, _local("D18", "D19")), track(sixty_third, _local("B06"))]),
        (QUEENSBRIDGE, [track(QUEENS_BLVD_LOCAL, _local("G20", "G13"))]),
    ]
    rockaway = [
        (
            LIBERTY_AV,
            [
                track(["H01", "H02", "H03", "H04"], _local("H03")),
                track(["H12", "H13", "H14", "H15"], _local("H13")),
            ],
        )
    ]
    weekend_ids = {t.id for t in extended}
    for trip in sorted(m, key=lambda t: t.calls[t.index(REGO_PARK)].departure):
        stages = (weekends if trip.id in weekend_ids else []) + rockaway
        for station, waited in _fit(trip, stages, trip.stations()[-1] == ROCKAWAY_PARK):
            held[f"M at {NAMES[station]}", waited] += 1
    weekday_g = sum(t.day is ServiceDay.WEEKDAY for t in timetable.of(G))
    run_on = _g_to_forest_hills(timetable)
    for trip in run_on:
        call = trip.calls[trip.index(COURT_SQ)]
        held["G at Court Sq", call.departure - call.arrival] += 1
    held["G at Court Sq", None] += weekday_g - len(run_on)
    return held


NAMES = {
    ESSEX_ST: "Essex St",
    QUEENSBRIDGE: "21 St-Queensbridge",
    LIBERTY_AV: "Liberty Av",
    JACKSON_HTS: "Jackson Hts",
    VIA_53_ST[-1]: "47-50 Sts",
}


# Trains that run anyway, rather than only if they fit, like the G past Court Sq,
# wait at most this long for a gap, which a few late in the evening, and in the evening peak, need.
MOST_HELD_RUNNING = 600


def _fit(
    trip: Trip, stages: list[tuple[str, list[Track]]], later: bool
) -> list[tuple[str, int | None]]:
    """
    Hold `trip` at each of `stages`' stations in turn, the least that leaves its tracks clear,
    the rest of the trip after it if `later`, else before it, then add it to the tracks.
    How long it waits at each, `None` where nothing within `MOST_HELD_RUNNING` does.
    """
    waits = []
    for station, tracks in stages:
        waited = hold(trip, trip.index(station), later, tracks, MOST_HELD_RUNNING)
        waits.append((station, waited))
        for track in tracks:
            track.add(trip)
    return waits


def _wait(waited: int | None) -> str:
    return "no gap" if waited is None else f"{waited} s" if waited else "on time"


def describe(held: Counter[tuple[str, int | None]]) -> list[str]:
    """`queenslink`'s waits, a line for each place, e.g. `G at Court Sq: 84 on time, 29 30 s`."""
    lines = []
    for where in sorted({where for where, _ in held}):
        waits = sorted(
            (w for p, w in held if p == where), key=lambda w: math.inf if w is None else w
        )
        lines.append(f"{where}: " + ", ".join(f"{held[where, w]} {_wait(w)}" for w in waits))
    return lines
