import datetime
import zipfile
from pathlib import Path

from subway_sheds import gtfs

STOPS = """\
stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station
V01,"Metropolitan Av-Parkside",40.710522,-73.857155,0,
V01N,"Metropolitan Av-Parkside",40.710522,-73.857155,0,V01
H04,"Broad Channel",40.608382,-73.815925,1,
H04N,"Broad Channel",40.608382,-73.815925,0,H04
"""

ROUTES = """\
route_id,agency_id,route_short_name,route_long_name,route_desc,route_type,route_url
M*,MTA NYCT,QL-M,Rockaway Local,QueensLink M Scenario,1,thequeenslink.org
A,MTA NYCT,A,8 Avenue Express,,1,https://www.mta.info/schedules/subway/a-train
"""

# One old calendar running through a long span with a single trip,
# and the scenario's own calendar covering a month with many.
CALENDAR = """\
service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
OLD-Weekday,1,1,1,1,1,0,0,20241118,20260501
Weekday,1,1,1,1,1,0,0,20251102,20251207
"""

CALENDAR_DATES = """\
service_id,date,exception_type
Weekday,20251105,2
"""

TRIPS = "route_id,service_id,trip_id\n7,OLD-Weekday,old\n" + "".join(
    f"M*,Weekday,new-{i}\n" for i in range(3)
)


def _feed(path: Path, directory: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as feed:
        for name, text in [
            ("stops.txt", STOPS),
            ("routes.txt", ROUTES),
            ("calendar.txt", CALENDAR),
            ("calendar_dates.txt", CALENDAR_DATES),
            ("trips.txt", TRIPS),
        ]:
            feed.writestr(directory + name, text)
    return path


def test_repack_flattens_and_repairs(tmp_path: Path) -> None:
    source = _feed(tmp_path / "source.zip", directory="B1_MVS/")
    dest = tmp_path / "dest.zip"
    repairs = gtfs.repack(source, dest)

    assert repairs == [
        "stop V01: location_type 1",
        "route M*: route_url https://thequeenslink.org",
    ]
    with zipfile.ZipFile(dest) as feed:
        assert sorted(feed.namelist()) == [
            "calendar.txt",
            "calendar_dates.txt",
            "routes.txt",
            "stops.txt",
            "trips.txt",
        ]
    assert [s.id for s in gtfs.stations(dest)] == ["V01", "H04"]


def test_weekday_service_date_goes_by_trips(tmp_path: Path) -> None:
    feed = _feed(tmp_path / "feed.zip")
    # 2025-11-05 has the most trips but is removed, so the next Wednesday.
    assert gtfs.weekday_service_date(feed) == datetime.date(2025, 11, 12)
