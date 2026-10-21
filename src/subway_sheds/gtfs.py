"""Reading and repairing just enough of a GTFS feed for routing it with R5."""

import csv
import datetime
import io
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def _read_csv(feed: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with feed.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def _to_csv(rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def repack(source: Path, dest: Path) -> list[str]:
    """
    Copy the feed at `source` to `dest` as a flat zip, repairing what R5 would reject.

    Returns a description of each repair.
    """
    with zipfile.ZipFile(source) as src:
        # Some feeds, like the QueensLink one, keep their files in a directory in the zip.
        members = {PurePosixPath(info.filename).name: info.filename for info in src.infolist()}
        members = {name: member for name, member in members.items() if name.endswith(".txt")}

        # A stop that is some other stop's `parent_station` is a station (`location_type` 1).
        # The QueensLink feed leaves its new stations at 0.
        stops = list(_read_csv(src, members["stops.txt"]))
        parents = {stop["parent_station"] for stop in stops if stop["parent_station"]}
        repaired: list[str] = []
        for stop in stops:
            if stop["stop_id"] in parents and stop["location_type"] != "1":
                stop["location_type"] = "1"
                repaired.append(f"stop {stop['stop_id']}: location_type 1")

        # R5 rejects a URL without a scheme, like the QueensLink route's `thequeenslink.org`.
        routes = list(_read_csv(src, members["routes.txt"]))
        for route in routes:
            url = route.get("route_url", "")
            if url and "://" not in url:
                route["route_url"] = f"https://{url}"
                repaired.append(f"route {route['route_id']}: route_url https://{url}")

        rewritten = {"stops.txt": _to_csv(stops), "routes.txt": _to_csv(routes)}

        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
            for name, member in members.items():
                if name in rewritten:
                    out.writestr(name, rewritten[name])
                else:
                    out.writestr(name, src.read(member))
    return repaired


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float


def stations(feed_path: Path) -> list[Station]:
    with zipfile.ZipFile(feed_path) as feed:
        return [
            Station(
                id=stop["stop_id"],
                name=stop["stop_name"],
                lat=float(stop["stop_lat"]),
                lon=float(stop["stop_lon"]),
            )
            for stop in _read_csv(feed, "stops.txt")
            if stop["location_type"] == "1"
        ]


def _date(value: str) -> datetime.date:
    return datetime.datetime.strptime(value, "%Y%m%d").date()


def weekday_service_date(feed_path: Path) -> datetime.date:
    """
    The Wednesday with the most trips scheduled in the feed.

    The two feeds cover different months, so each is routed on a date of its own.
    Going by trips rather than by calendars matters for the QueensLink feed,
    which also carries calendars from older feeds that only a route or two still use.
    """
    with zipfile.ZipFile(feed_path) as feed:
        calendar = list(_read_csv(feed, "calendar.txt"))
        added: dict[datetime.date, set[str]] = {}
        removed: dict[datetime.date, set[str]] = {}
        for row in _read_csv(feed, "calendar_dates.txt"):
            by_date = added if row["exception_type"] == "1" else removed
            by_date.setdefault(_date(row["date"]), set()).add(row["service_id"])
        trips = Counter(trip["service_id"] for trip in _read_csv(feed, "trips.txt"))

    def trip_count(date: datetime.date) -> int:
        services = {
            row["service_id"]
            for row in calendar
            if row["wednesday"] == "1"
            and _date(row["start_date"]) <= date <= _date(row["end_date"])
        }
        services |= added.get(date, set())
        services -= removed.get(date, set())
        return sum(trips[service] for service in services)

    start = min(_date(row["start_date"]) for row in calendar)
    end = max(_date(row["end_date"]) for row in calendar)
    first = start + datetime.timedelta(days=(2 - start.weekday()) % 7)
    wednesdays = [
        first + datetime.timedelta(weeks=week) for week in range((end - first).days // 7 + 1)
    ]
    # `max` keeps the first of equals, so the earliest date with full service.
    return max(wednesdays, key=trip_count)
