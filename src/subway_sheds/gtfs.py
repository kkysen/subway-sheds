"""Reading just enough of a GTFS feed for routing it with R5."""

import csv
import datetime
import io
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


def _read_csv(feed: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with feed.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def zip_feed(directory: Path, dest: Path) -> None:
    """Zip a feed committed as a directory of `.txt` files, as R5 reads it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for path in sorted(directory.glob("*.txt")):
            out.write(path, path.name)


def _read_dir_csv(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / name).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def splice_routes(base: Path, donor: Path, routes: set[str], dest: Path) -> None:
    """
    Write the feed at `base` (a zip) to `dest`, its `routes`' trips replaced by `donor`'s.

    `donor` is a feed directory; the stops, calendars, and shapes its trips of `routes` use
    come along with them where `base` lacks them.
    """
    with zipfile.ZipFile(base) as feed:
        names = [name for name in feed.namelist() if name.endswith(".txt")]
        base_files = {name: list(_read_csv(feed, name)) for name in names}
        headers = {}
        for name in names:
            with feed.open(name) as f:
                headers[name] = next(csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig")))

    donor_trips = [t for t in _read_dir_csv(donor, "trips.txt") if t["route_id"] in routes]
    donor_trip_ids = {t["trip_id"] for t in donor_trips}
    dropped = {t["trip_id"] for t in base_files["trips.txt"] if t["route_id"] in routes}

    def merge(name: str, key: str, wanted: set[str]) -> None:
        """Add `donor`'s rows of `name` whose `key` is in `wanted` and not already in `base`."""
        have = {row[key] for row in base_files[name]}
        base_files[name] += [
            row for row in _read_dir_csv(donor, name) if row[key] in wanted and row[key] not in have
        ]

    base_files["trips.txt"] = [
        t for t in base_files["trips.txt"] if t["trip_id"] not in dropped
    ] + donor_trips
    base_files["stop_times.txt"] = [
        st for st in base_files["stop_times.txt"] if st["trip_id"] not in dropped
    ] + [st for st in _read_dir_csv(donor, "stop_times.txt") if st["trip_id"] in donor_trip_ids]

    stop_ids = {st["stop_id"] for st in base_files["stop_times.txt"]}
    donor_stops = {s["stop_id"]: s for s in _read_dir_csv(donor, "stops.txt")}
    stop_ids |= {donor_stops[i]["parent_station"] for i in stop_ids if i in donor_stops}
    merge("stops.txt", "stop_id", stop_ids)
    services = {t["service_id"] for t in donor_trips}
    merge("calendar.txt", "service_id", services)
    merge("calendar_dates.txt", "service_id", services)
    merge("shapes.txt", "shape_id", {t["shape_id"] for t in donor_trips})

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for name in names:
            out.writestr(name, _write_csv(headers[name], base_files[name]))


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
