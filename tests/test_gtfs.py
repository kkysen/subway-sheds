import csv
import datetime
import io
import zipfile
from pathlib import Path

from subway_sheds import gtfs

ROUTES = "route_id,route_color\nA,0062CF\n"

STOPS = """\
stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station
H04,"Broad Channel",40.608382,-73.815925,1,
H04N,"Broad Channel",40.608382,-73.815925,0,H04
"""

# One old calendar running through a long span with a single trip,
# and the feed's own calendar covering a month with many.
CALENDAR = """\
service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
OLD-Weekday,1,1,1,1,1,0,0,20241118,20260501
Weekday,1,1,1,1,1,0,0,20251102,20251207
"""

CALENDAR_DATES = """\
service_id,date,exception_type
Weekday,20251105,2
"""

TRIPS = "route_id,service_id,trip_id,shape_id\n7,OLD-Weekday,old,7..N\n" + "".join(
    f"A,Weekday,a-{i},A..N\n" for i in range(3)
)

STOP_TIMES = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(
    f"{trip},08:00:00,08:00:00,H04N,1\n" for trip in ["old", "a-0", "a-1", "a-2"]
)

SHAPES = """\
shape_id,shape_pt_sequence,shape_pt_lat,shape_pt_lon
7..N,0,40.75,-73.94
A..N,0,40.68,-73.84
"""

FILES = {
    "routes.txt": ROUTES,
    "stops.txt": STOPS,
    "calendar.txt": CALENDAR,
    "calendar_dates.txt": CALENDAR_DATES,
    "trips.txt": TRIPS,
    "stop_times.txt": STOP_TIMES,
    "shapes.txt": SHAPES,
}


def _feed_dir(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    for name, text in files.items():
        (path / name).write_text(text)
    return path


def _zip_feed(directory: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w") as out:
        for path in sorted(directory.glob("*.txt")):
            out.write(path, path.name)


def _read(feed: Path, name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(feed) as z:
        return list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")))


def test_stations_are_only_those_stopped_at(tmp_path: Path) -> None:
    unserved = "S01,Unserved,40.6,-73.8,1,\nS01N,Unserved,40.6,-73.8,0,S01\n"
    feed = tmp_path / "feed.zip"
    _zip_feed(_feed_dir(tmp_path / "feed", FILES | {"stops.txt": STOPS + unserved}), feed)
    assert [s.id for s in gtfs.stations(feed)] == ["H04"]


def test_service_date_goes_by_trips(tmp_path: Path) -> None:
    feed = tmp_path / "feed.zip"
    _zip_feed(_feed_dir(tmp_path / "feed", FILES), feed)
    # 2025-11-05 has the most trips but is removed, so the next Wednesday.
    assert gtfs.service_date(feed, 2) == datetime.date(2025, 11, 12)
