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
    base = gtfs.read_feed(_feed_dir(tmp_path / "base", FILES))

    # The donor runs the 7 on a calendar of its own, to a station the base doesn't have.
    donor = gtfs.read_feed(
        _feed_dir(
            tmp_path / "donor",
            {
                "routes.txt": "route_id,route_color\n7,B933AD\n",
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
    )
    gtfs.splice_routes(base, donor, {"7"})
    dest = tmp_path / "spliced.zip"
    gtfs.write_feed(base, dest)

    assert [t["trip_id"] for t in _read(dest, "trips.txt")] == ["a-0", "a-1", "a-2", "new-7"]
    assert [st["trip_id"] for st in _read(dest, "stop_times.txt")] == [
        "a-0",
        "a-1",
        "a-2",
        "new-7",
    ]
    assert [r["route_id"] for r in _read(dest, "routes.txt")] == ["A", "7"]
    assert [s["stop_id"] for s in _read(dest, "stops.txt")] == ["H04", "H04N", "Q08", "Q08N"]
    assert "NEW-7" in {c["service_id"] for c in _read(dest, "calendar.txt")}
    assert "7..S" in {s["shape_id"] for s in _read(dest, "shapes.txt")}


# A branching route, M, through stations 1 and 2 to a branch at 3 toward 4,
# and a local, R, from 3 on to 5 and 6, whose running times M takes in turning toward them.
BRANCH_STOPS = "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n" + "".join(
    f"S{i},S{i},40.7,-73.8,1,\nS{i}N,S{i},40.7,-73.8,0,S{i}\nS{i}S,S{i},40.7,-73.8,0,S{i}\n"
    for i in range(1, 7)
)

BRANCH_STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    + "".join(
        f"out,{t},{t},{stop},{i}\n"
        for i, (stop, t) in enumerate(
            [("S1N", "08:00:00"), ("S2N", "08:02:00"), ("S3N", "08:04:00"), ("S4S", "08:10:00")],
            start=1,
        )
    )
    + "".join(
        f"in,{t},{t},{stop},{i}\n"
        for i, (stop, t) in enumerate(
            [("S4N", "07:00:00"), ("S3S", "07:06:00"), ("S2S", "07:08:00"), ("S1S", "07:10:00")],
            start=1,
        )
    )
    + "".join(
        f"r,{t},{t},{stop},{i}\n"
        for i, (stop, t) in enumerate(
            [("S3N", "09:00:00"), ("S5N", "09:01:30"), ("S6N", "09:03:00")], start=1
        )
    )
)


def test_reroute_branch(tmp_path: Path) -> None:
    feed = gtfs.read_feed(
        _feed_dir(
            tmp_path / "feed",
            {
                "stops.txt": BRANCH_STOPS,
                "trips.txt": "route_id,service_id,trip_id\nM,W,out\nM,W,in\nR,W,r\n",
                "stop_times.txt": BRANCH_STOP_TIMES,
                "frequencies.txt": "trip_id,start_time,end_time,headway_secs\n"
                "out,08:00:00,09:00:00,600\nin,07:00:00,08:00:00,600\n",
            },
        )
    )
    gtfs.reroute_branch(feed, "M", "S3", "S4", ["S5", "S6"], "R")
    dest = tmp_path / "rerouted.zip"
    gtfs.write_feed(feed, dest)

    stop_times = _read(dest, "stop_times.txt")
    assert [
        (st["stop_id"], st["departure_time"], st["stop_sequence"])
        for st in stop_times
        if st["trip_id"] == "out"
    ] == [
        ("S1N", "08:00:00", "1"),
        ("S2N", "08:02:00", "2"),
        ("S3N", "08:04:00", "3"),
        ("S5N", "08:05:30", "4"),
        ("S6N", "08:07:00", "5"),
    ]
    assert [
        (st["stop_id"], st["departure_time"]) for st in stop_times if st["trip_id"] == "in"
    ] == [
        ("S6S", "07:03:00"),
        ("S5S", "07:04:30"),
        ("S3S", "07:06:00"),
        ("S2S", "07:08:00"),
        ("S1S", "07:10:00"),
    ]
    # The inbound trip now starts 3 minutes later, so its frequencies do too.
    assert [
        (f["trip_id"], f["start_time"], f["end_time"]) for f in _read(dest, "frequencies.txt")
    ] == [
        ("out", "08:00:00", "09:00:00"),
        ("in", "07:03:00", "08:03:00"),
    ]


def test_keep_route_stations(tmp_path: Path) -> None:
    feed = gtfs.read_feed(
        _feed_dir(
            tmp_path / "feed",
            {
                "stops.txt": BRANCH_STOPS,
                "trips.txt": "route_id,service_id,trip_id\nM,W,out\nM,W,in\nR,W,r\n",
                "stop_times.txt": BRANCH_STOP_TIMES,
            },
        )
    )
    # R would be left with one station, so it goes.
    gtfs.keep_route_stations(feed, "R", {"S3"})
    gtfs.keep_route_stations(feed, "M", {"S1", "S2"})
    dest = tmp_path / "cut.zip"
    gtfs.write_feed(feed, dest)

    assert [t["trip_id"] for t in _read(dest, "trips.txt")] == ["out", "in"]
    assert [(st["trip_id"], st["stop_id"]) for st in _read(dest, "stop_times.txt")] == [
        ("out", "S1N"),
        ("out", "S2N"),
        ("in", "S2S"),
        ("in", "S1S"),
    ]
