import csv
import datetime
import io
import zipfile
from pathlib import Path

from subway_sheds import gtfs

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


def _read(feed: Path, name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(feed) as z:
        return list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")))


def test_zip_feed_is_flat(tmp_path: Path) -> None:
    dest = tmp_path / "feed.zip"
    gtfs.zip_feed(_feed_dir(tmp_path / "feed", FILES), dest)
    with zipfile.ZipFile(dest) as feed:
        assert sorted(feed.namelist()) == sorted(FILES)
    assert [s.id for s in gtfs.stations(dest)] == ["H04"]


def test_weekday_service_date_goes_by_trips(tmp_path: Path) -> None:
    feed = tmp_path / "feed.zip"
    gtfs.zip_feed(_feed_dir(tmp_path / "feed", FILES), feed)
    # 2025-11-05 has the most trips but is removed, so the next Wednesday.
    assert gtfs.weekday_service_date(feed) == datetime.date(2025, 11, 12)


def test_splice_routes(tmp_path: Path) -> None:
    base = tmp_path / "base.zip"
    gtfs.zip_feed(_feed_dir(tmp_path / "base", FILES), base)

    # The donor runs the 7 on a calendar of its own, to a station the base doesn't have.
    donor = _feed_dir(
        tmp_path / "donor",
        {
            "stops.txt": STOPS
            + 'Q08,"125 St",40.804382,-73.937408,1,\n'
            + 'Q08N,"125 St",40.804382,-73.937408,0,Q08\n',
            "calendar.txt": CALENDAR + "NEW-7,1,1,1,1,1,0,0,20251102,20251207\n",
            "calendar_dates.txt": CALENDAR_DATES,
            "trips.txt": "route_id,service_id,trip_id,shape_id\n7,NEW-7,new-7,7..S\n",
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "new-7,08:00:00,08:00:00,Q08N,1\n",
            "shapes.txt": SHAPES + "7..S,0,40.80,-73.93\n",
        },
    )
    dest = tmp_path / "spliced.zip"
    gtfs.splice_routes(base, donor, {"7"}, dest)

    assert [t["trip_id"] for t in _read(dest, "trips.txt")] == ["a-0", "a-1", "a-2", "new-7"]
    assert [st["trip_id"] for st in _read(dest, "stop_times.txt")] == [
        "a-0",
        "a-1",
        "a-2",
        "new-7",
    ]
    assert [s["stop_id"] for s in _read(dest, "stops.txt")] == ["H04", "H04N", "Q08", "Q08N"]
    assert "NEW-7" in {c["service_id"] for c in _read(dest, "calendar.txt")}
    assert "7..S" in {s["shape_id"] for s in _read(dest, "shapes.txt")}
