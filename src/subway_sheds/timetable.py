"""
A feed's trips as times in seconds, to re-stop, re-time, and fit trains between others,
which `scenarios` makes its feeds with.
"""

import bisect
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from subway_sheds import gtfs

# The least time a train added or moved is scheduled behind another on the same track,
# at each platform: the least MTA itself schedules between its trains in the morning peak,
# on the Queens Blvd local toward Forest Hills, through 53 St, and on the 6 Av local.
FLOOR = 90
# Under CBTC, as on Queens Blvd and QueensLink's new track, trains can run 30 s apart,
# but with their dwells, they're scheduled at least this far apart.
CBTC_FLOOR = 60
# A train added waits at most this long where it waits for a gap, in steps of `STEP`.
MOST_HELD = 60
STEP = 30


class ServiceDay(StrEnum):
    """What a service runs on, as MTA's service IDs say, e.g. `BFA19GEN-7086-Weekday-00`."""

    WEEKDAY = "Weekday"
    SATURDAY = "Saturday"
    SUNDAY = "Sunday"

    @staticmethod
    def of(service: str) -> ServiceDay:
        return next(day for day in ServiceDay if day in service)


@dataclass
class Call:
    """A train's stop at a platform."""

    stop: str
    arrival: int
    departure: int


@dataclass
class Trip:
    id: str
    route: str
    service: str
    calls: list[Call]

    @property
    def day(self) -> ServiceDay:
        return ServiceDay.of(self.service)

    def index(self, station: str) -> int:
        """Where it stops at `station` (a parent station, the platform's ID without N or S)."""
        return next(i for i, call in enumerate(self.calls) if call.stop[:-1] == station)

    def stations(self) -> list[str]:
        return [call.stop[:-1] for call in self.calls]


def calls(stops: list[str], start: int, stretch: Callable[[str, str], int]) -> list[Call]:
    """Calls at `stops`, the first leaving at `start`, each `stretch` after the one before."""
    out = [Call(stops[0], start, start)]
    for a, b in pairwise(stops):
        time = out[-1].departure + stretch(a, b)
        out.append(Call(b, time, time))
    return out


def ending(stops: list[str], end: int, stretch: Callable[[str, str], int]) -> list[Call]:
    """Calls at `stops`, the last leaving at `end`, each `stretch` after the one before."""
    out = calls(stops, 0, stretch)
    shift(out, end - out[-1].departure)
    return out


def shift(calls_: Iterable[Call], seconds: int) -> None:
    for call in calls_:
        call.arrival += seconds
        call.departure += seconds


class Timetable:
    """`feed`'s trips, to change and write back into it with `write`."""

    def __init__(self, feed: gtfs.Feed) -> None:
        self.feed = feed
        self.trips = {
            t["trip_id"]: Trip(t["trip_id"], t["route_id"], t["service_id"], [])
            for t in feed.rows["trips.txt"]
        }
        rows = sorted(
            feed.rows["stop_times.txt"], key=lambda st: (st["trip_id"], int(st["stop_sequence"]))
        )
        for st in rows:
            self.trips[st["trip_id"]].calls.append(
                Call(
                    st["stop_id"],
                    gtfs.seconds(st["arrival_time"]),
                    gtfs.seconds(st["departure_time"]),
                )
            )

    def of(self, *routes: str) -> list[Trip]:
        return [trip for trip in self.trips.values() if trip.route in routes]

    def add(self, trip: Trip, row: dict[str, str]) -> None:
        """Add `trip`, with `row` its row of `trips.txt`."""
        self.trips[trip.id] = trip
        self.feed.rows["trips.txt"].append(row)

    def drop(self, trips: Iterable[str]) -> None:
        for trip in trips:
            del self.trips[trip]

    def stretches(
        self, routes: Iterable[str], day: ServiceDay, hours: tuple[int, int] | None = None
    ) -> dict[tuple[str, str], int]:
        """
        The median time between consecutive platforms of `routes`' trips on `day`,
        leaving one to leaving the next, of trips starting in `hours` if given.
        """
        times: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
        for trip in self.of(*routes):
            if trip.day is not day:
                continue
            if hours and not hours[0] * 3600 <= trip.calls[0].departure < hours[1] * 3600:
                continue
            for a, b in pairwise(trip.calls):
                times[a.stop, b.stop].append(b.departure - a.departure)
        return {pair: round(statistics.median(ts)) for pair, ts in times.items()}

    def write(self) -> None:
        """Write the trips back into the feed, numbering their stops anew."""
        self.feed.rows["trips.txt"] = [
            t for t in self.feed.rows["trips.txt"] if t["trip_id"] in self.trips
        ]
        self.feed.rows["stop_times.txt"] = [
            {
                "trip_id": trip.id,
                "stop_id": call.stop,
                "arrival_time": gtfs.time(call.arrival),
                "departure_time": gtfs.time(call.departure),
                "stop_sequence": str(i),
            }
            for trip in self.trips.values()
            for i, call in enumerate(trip.calls, start=1)
        ]


class Track:
    """
    When trains leave `stops` (platforms) on one track, by service,
    of the trips `on` says run on that track there,
    e.g. those stopping at a local-only station, for a local track's platforms
    that express trains also stop at, on the express track beside it.
    """

    def __init__(
        self,
        timetable: Timetable,
        stops: set[str],
        on: Callable[[Trip], bool],
        floor: int = FLOOR,
    ) -> None:
        self.stops = stops
        self.floor = floor
        self.times: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
        for trip in timetable.trips.values():
            if on(trip):
                self.add(trip)

    def add(self, trip: Trip) -> None:
        for call in trip.calls:
            if call.stop in self.stops:
                bisect.insort(self.times[trip.service, call.stop], call.departure)

    def clear(self, service: str, calls_: Iterable[Call]) -> bool:
        """Whether `calls_` leave each of `stops` at least `floor` from every other train."""
        for call in calls_:
            if call.stop not in self.stops:
                continue
            times = self.times[service, call.stop]
            i = bisect.bisect_left(times, call.departure)
            near = times[max(i - 1, 0) : i + 1]
            if any(abs(t - call.departure) < self.floor for t in near):
                return False
        return True

    def gaps(self, service: str, start: int, end: int) -> dict[str, int]:
        """The least time between trains leaving each of `stops` from `start` to `end`."""
        out: dict[str, int] = {}
        for stop in self.stops:
            times = [t for t in self.times[service, stop] if start <= t < end]
            if len(times) > 1:
                out[stop] = min(b - a for a, b in pairwise(times))
        return out


def hold(
    trip: Trip, at: int, later: bool, tracks: list[Track], most: int = MOST_HELD
) -> int | None:
    """
    Hold `trip` at its `at`th stop the least that leaves every one of `tracks` clear,
    the calls after it later if `later`, else the calls before it earlier,
    up to `most`; how long, or `None`, leaving it as it was, if none is.
    """
    for held in range(0, most + 1, STEP):
        moved = trip.calls[at + 1 :] if later else trip.calls[:at]
        shift(moved, held if later else -held)
        if all(track.clear(trip.service, trip.calls) for track in tracks):
            if later:
                trip.calls[at].departure += held
            else:
                trip.calls[at].arrival -= held
            return held
        shift(moved, -held if later else held)
    return None
