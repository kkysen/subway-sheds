"""Reading just enough of a GTFS feed to make the scenarios' feeds from it."""

import csv
import datetime
import io
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from subway_sheds import _route


def read_csv(feed: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with feed.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def _write_entry(out: zipfile.ZipFile, name: str, data: str | bytes) -> None:
    """
    Write a file into a zip with a fixed time rather than its own or the current one,
    so that the same feed always zips to the same bytes.
    """
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    out.writestr(info, data)


@dataclass
class Feed:
    """A whole feed's files, each's header and rows, to change and write back out."""

    headers: dict[str, list[str]]
    rows: dict[str, list[dict[str, str]]]


def read_feed(path: Path) -> Feed:
    """Read a zipped feed."""
    texts: dict[str, str] = {}
    with zipfile.ZipFile(path) as feed:
        for name in feed.namelist():
            if name.endswith(".txt"):
                texts[name] = feed.read(name).decode("utf-8-sig")
    headers = {name: next(csv.reader(io.StringIO(text))) for name, text in texts.items()}
    rows = {name: list(csv.DictReader(io.StringIO(text))) for name, text in texts.items()}
    return Feed(headers, rows)


def _write_csv(fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def write_feed(feed: Feed, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as out:
        for name, rows in feed.rows.items():
            _write_entry(out, name, _write_csv(feed.headers[name], rows))


def seconds(time: str) -> int:
    hours, minutes, secs = (int(part) for part in time.split(":"))
    return hours * 3600 + minutes * 60 + secs


def time(secs: int) -> str:
    return f"{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}"


def parents(feed: Feed) -> dict[str, str]:
    """Each stop's parent station, or itself if it has none."""
    return {s["stop_id"]: s["parent_station"] or s["stop_id"] for s in feed.rows["stops.txt"]}


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float


def stations(feed_path: Path) -> list[Station]:
    """
    The stations some trip stops at.

    Not every station in `stops.txt`, since the baseline keeps QueensLink's stations
    with no trips stopping at them.
    Read in Rust (`route/src/feed.rs`), as reading `stop_times.txt` in Python is slow.
    """
    return [
        Station(id=id_, name=name, lat=lat, lon=lon)
        for id_, name, lat, lon in _route.stations(feed_path)
    ]


def _date(value: str) -> datetime.date:
    return datetime.datetime.strptime(value, "%Y%m%d").date()


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def service_date(feed_path: Path, weekday: int) -> datetime.date:
    """
    The date on `weekday` (0 for Monday) with the most trips scheduled in the feed.

    Going by trips rather than by calendars,
    as a feed can carry calendars that only a route or two use.
    """
    with zipfile.ZipFile(feed_path) as feed:
        calendar = list(read_csv(feed, "calendar.txt"))
        added: dict[datetime.date, set[str]] = {}
        removed: dict[datetime.date, set[str]] = {}
        for row in read_csv(feed, "calendar_dates.txt"):
            by_date = added if row["exception_type"] == "1" else removed
            by_date.setdefault(_date(row["date"]), set()).add(row["service_id"])
        trips = Counter(trip["service_id"] for trip in read_csv(feed, "trips.txt"))

    def trip_count(date: datetime.date) -> int:
        services = {
            row["service_id"]
            for row in calendar
            if row[_DAYS[weekday]] == "1"
            and _date(row["start_date"]) <= date <= _date(row["end_date"])
        }
        services |= added.get(date, set())
        services -= removed.get(date, set())
        return sum(trips[service] for service in services)

    start = min(_date(row["start_date"]) for row in calendar)
    end = max(_date(row["end_date"]) for row in calendar)
    first = start + datetime.timedelta(days=(weekday - start.weekday()) % 7)
    dates = [first + datetime.timedelta(weeks=week) for week in range((end - first).days // 7 + 1)]
    # `max` keeps the first of equals, so the earliest date with full service.
    return max(dates, key=trip_count)
