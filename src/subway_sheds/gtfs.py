"""Reading just enough of a GTFS feed for routing it with R5."""

import csv
import datetime
import io
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


def _read_csv(feed: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with feed.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def _write_entry(out: zipfile.ZipFile, name: str, data: str | bytes) -> None:
    """
    Write a file into a zip with a fixed time rather than its own or the current one,
    so that the same feed always zips to the same bytes,
    since `r5py` caches the network it builds by its files' hashes.
    """
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    out.writestr(info, data)


def zip_feed(directory: Path, dest: Path) -> None:
    """Zip a feed committed as a directory of `.txt` files, as R5 reads it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as out:
        for path in sorted(directory.glob("*.txt")):
            _write_entry(out, path.name, path.read_bytes())


@dataclass
class Feed:
    """A whole feed's files, each's header and rows, to change and write back out."""

    headers: dict[str, list[str]]
    rows: dict[str, list[dict[str, str]]]


def read_feed(path: Path) -> Feed:
    """Read a feed, either zipped or committed as a directory of `.txt` files."""
    texts: dict[str, str] = {}
    if path.is_dir():
        for file in sorted(path.glob("*.txt")):
            texts[file.name] = file.read_text(encoding="utf-8-sig")
    else:
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


def drop_trips(feed: Feed, trips: set[str]) -> None:
    """Remove `trips`, with their stop times and frequencies."""
    for name in ("trips.txt", "stop_times.txt", "frequencies.txt"):
        if name in feed.rows:
            feed.rows[name] = [row for row in feed.rows[name] if row["trip_id"] not in trips]


def splice_routes(
    base: Feed, donor: Feed, routes: set[str], services: Callable[[str], bool] = lambda _: True
) -> None:
    """
    Replace `base`'s trips of `routes` with `donor`'s whose service IDs `services` is true of.

    The routes, stops, calendars, and shapes those trips use
    come along with them where `base` lacks them.
    """
    donor_trips = [
        t for t in donor.rows["trips.txt"] if t["route_id"] in routes and services(t["service_id"])
    ]
    donor_trip_ids = {t["trip_id"] for t in donor_trips}
    dropped = {t["trip_id"] for t in base.rows["trips.txt"] if t["route_id"] in routes}

    def merge(name: str, key: str, wanted: set[str]) -> None:
        """Add `donor`'s rows of `name` whose `key` is in `wanted` and not already in `base`."""
        have = {row[key] for row in base.rows[name]}
        base.rows[name] += [
            row for row in donor.rows[name] if row[key] in wanted and row[key] not in have
        ]

    base.rows["trips.txt"] = [
        t for t in base.rows["trips.txt"] if t["trip_id"] not in dropped
    ] + donor_trips
    base.rows["stop_times.txt"] = [
        st for st in base.rows["stop_times.txt"] if st["trip_id"] not in dropped
    ] + [st for st in donor.rows["stop_times.txt"] if st["trip_id"] in donor_trip_ids]

    stop_ids = {st["stop_id"] for st in base.rows["stop_times.txt"]}
    donor_stops = {s["stop_id"]: s for s in donor.rows["stops.txt"]}
    stop_ids |= {donor_stops[i]["parent_station"] for i in stop_ids if i in donor_stops}
    merge("routes.txt", "route_id", routes)
    merge("stops.txt", "stop_id", stop_ids)
    service_ids = {t["service_id"] for t in donor_trips}
    merge("calendar.txt", "service_id", service_ids)
    merge("calendar_dates.txt", "service_id", service_ids)
    merge("shapes.txt", "shape_id", {t["shape_id"] for t in donor_trips})


def _seconds(time: str) -> int:
    hours, minutes, seconds = (int(part) for part in time.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _trip_stop_times(feed: Feed, route: str) -> dict[str, list[dict[str, str]]]:
    """Each of `route`'s trips' stop times, in order, the trips in `trips.txt`'s order."""
    stop_times: dict[str, list[dict[str, str]]] = {
        t["trip_id"]: [] for t in feed.rows["trips.txt"] if t["route_id"] == route
    }
    for stop_time in feed.rows["stop_times.txt"]:
        if stop_time["trip_id"] in stop_times:
            stop_times[stop_time["trip_id"]].append(stop_time)
    for rows in stop_times.values():
        rows.sort(key=lambda st: int(st["stop_sequence"]))
    return stop_times


def _replace_stop_times(feed: Feed, stop_times: dict[str, list[dict[str, str]]]) -> None:
    """Replace the stop times of the trips in `stop_times`, numbering them anew."""
    for rows in stop_times.values():
        for i, row in enumerate(rows, start=1):
            row["stop_sequence"] = str(i)
    feed.rows["stop_times.txt"] = [
        st for st in feed.rows["stop_times.txt"] if st["trip_id"] not in stop_times
    ] + [st for rows in stop_times.values() for st in rows]


def _parents(feed: Feed) -> dict[str, str]:
    return {s["stop_id"]: s["parent_station"] or s["stop_id"] for s in feed.rows["stops.txt"]}


def keep_route_stations(feed: Feed, route: str, stations: set[str]) -> None:
    """Cut `route`'s trips back to `stations` (parent stations), dropping ones left with one."""
    parents = _parents(feed)
    stop_times = {
        trip: [st for st in rows if parents[st["stop_id"]] in stations]
        for trip, rows in _trip_stop_times(feed, route).items()
    }
    _replace_stop_times(feed, {trip: rows for trip, rows in stop_times.items() if len(rows) > 1})
    gone = {trip for trip, rows in stop_times.items() if len(rows) <= 1}
    feed.rows["trips.txt"] = [t for t in feed.rows["trips.txt"] if t["trip_id"] not in gone]
    feed.rows["stop_times.txt"] = [
        st for st in feed.rows["stop_times.txt"] if st["trip_id"] not in gone
    ]


def reroute_branch(
    feed: Feed, route: str, branch: str, beyond: str, terminal: list[str], like: str
) -> None:
    """
    Turn `route`'s trips at `branch` toward `terminal` instead of their branch beyond it,
    which `beyond` is a station of.

    `terminal` is the parent stations past `branch`, the farthest last,
    which `like`'s trips run through, and whose running times between them this takes.
    Each trip's frequencies shift with the time it now starts.
    """
    parents = _parents(feed)
    path = [branch, *terminal]
    # Running times from `branch` to each of `terminal`, from `like`'s first trip through them.
    offsets: list[int] = []
    for rows in _trip_stop_times(feed, like).values():
        stations = [parents[st["stop_id"]] for st in rows]
        for i in range(len(stations) - len(path) + 1):
            if stations[i : i + len(path)] in (path, path[::-1]):
                times = [_seconds(st["departure_time"]) for st in rows[i : i + len(path)]]
                offsets = [abs(t - times[0]) for t in times[1:]]
                break
        if offsets:
            break
    if not offsets:
        raise ValueError(f"no {like} trip runs through {path}")

    shifts: dict[str, int] = {}
    stop_times: dict[str, list[dict[str, str]]] = {}
    for trip, rows in _trip_stop_times(feed, route).items():
        at = next(i for i, st in enumerate(rows) if parents[st["stop_id"]] == branch)
        platform = rows[at]["stop_id"].removeprefix(branch)
        time = _seconds(rows[at]["departure_time"])
        added = [
            rows[at] | {"stop_id": f"{station}{platform}", "arrival_time": "", "departure_time": ""}
            for station in terminal
        ]
        # Away from the trunk if the branch comes after `branch`.
        outbound = any(parents[st["stop_id"]] == beyond for st in rows[at + 1 :])
        for row, offset in zip(added, offsets, strict=True):
            row["arrival_time"] = row["departure_time"] = _time(
                time + offset if outbound else time - offset
            )
        if outbound:
            stop_times[trip] = [*rows[: at + 1], *added]
        else:
            stop_times[trip] = [*added[::-1], *rows[at:]]
        shifts[trip] = _seconds(stop_times[trip][0]["departure_time"]) - _seconds(
            rows[0]["departure_time"]
        )
    _replace_stop_times(feed, stop_times)
    for frequency in feed.rows.get("frequencies.txt", []):
        shift = shifts.get(frequency["trip_id"], 0)
        for key in ("start_time", "end_time"):
            frequency[key] = _time(_seconds(frequency[key]) + shift)


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


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def service_date(feed_path: Path, weekday: int) -> datetime.date:
    """
    The date on `weekday` (0 for Monday) with the most trips scheduled in the feed.

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


def match_frequencies(feed: Feed, route: str, like: Feed, like_route: str, station: str) -> None:
    """
    Run `route`'s trips, which run on frequencies, as often as `like_route` runs in `like`,
    hour by hour, as each passes `station` (a parent station) in each direction.

    A trip matches `like_route`'s trips on the same platform of `station`
    whose service ID contains its own, e.g. `Weekday` matches `BFA19GEN-M063-Weekday-00`.
    """
    like_parents = _parents(like)
    like_services = {
        t["trip_id"]: t["service_id"] for t in like.rows["trips.txt"] if t["route_id"] == like_route
    }
    # Each platform's and service's departures in each hour.
    departures: Counter[tuple[str, str, int]] = Counter()
    for st in like.rows["stop_times.txt"]:
        if st["trip_id"] in like_services and like_parents[st["stop_id"]] == station:
            hour = _seconds(st["departure_time"]) // 3600
            departures[st["stop_id"], like_services[st["trip_id"]], hour] += 1

    parents = _parents(feed)
    services = {t["trip_id"]: t["service_id"] for t in feed.rows["trips.txt"]}
    frequencies = feed.rows["frequencies.txt"]
    matched: list[dict[str, str]] = []
    for trip, rows in _trip_stop_times(feed, route).items():
        old = [f for f in frequencies if f["trip_id"] == trip]
        if not old:
            continue
        at = next(st for st in rows if parents[st["stop_id"]] == station)
        # How long after the trip starts it passes `station`.
        offset = _seconds(at["departure_time"]) - _seconds(rows[0]["departure_time"])
        hourly: Counter[int] = Counter()
        for (platform, service, hour), count in departures.items():
            if platform == at["stop_id"] and services[trip] in service:
                hourly[hour] += count
        if not hourly:
            raise ValueError(f"no {like_route} trips like {trip} at {at['stop_id']}")
        matched += [
            old[0]
            | {
                # Not before the service day starts, which trips passing just after it miss.
                "start_time": _time(max(hour * 3600 - offset, 0)),
                "end_time": _time((hour + 1) * 3600 - offset),
                # Rounded up, so that no more than `count` fit in the hour.
                "headway_secs": str(-(-3600 // count)),
            }
            for hour, count in sorted(hourly.items())
            if (hour + 1) * 3600 > offset
        ]
    trips = {f["trip_id"] for f in matched}
    feed.rows["frequencies.txt"] = [f for f in frequencies if f["trip_id"] not in trips] + matched
