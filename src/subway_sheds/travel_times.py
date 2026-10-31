"""
Travel times from subway stations to the grid over the city,
routed with `mta-od-data`'s RAPTOR, walking on the streets, through `subway_sheds._route`.
"""

import datetime
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from subway_sheds import _route, gtfs, paths
from subway_sheds.scenarios import Day, Scenario

# From each day's departure time, departing every minute in the window
# and taking the median trip across them,
# so waiting for a train counts the way it does for someone who doesn't time their arrival.
DEPARTURE_WINDOW = datetime.timedelta(hours=1)
# Past the 90 minutes the page draws travel times out to,
# so that it can tell how much QueensLink saves places that take longer than that without it.
MAX_TIME = datetime.timedelta(minutes=120)
# 3 mph, a typical adult's pace.
WALKING_SPEED_KM_PER_H = 3 * 1.609344
# In millimeters a second, rounded down, as the routing walks.
WALKING_SPEED_MM_PER_S = int(WALKING_SPEED_KM_PER_H / 3600 * 1000 * 1000)
# Most rides in a journey, as R5 routed before.
MAX_RIDES = 8


def output_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}.parquet"


def stations_output_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}-stations.parquet"


def from_stations(
    scenario: Scenario,
    day: Day,
    origins: list[gtfs.Station],
    cells: gpd.GeoDataFrame,
    stations: list[gtfs.Station],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Median minutes from each of `origins` to each of `cells` and `stations`, walking at both ends.

    Only the pairs reachable within `MAX_TIME`:
    to the cells as `station`, `cell`, and `minutes`,
    and to the stations themselves as `station`, `to_station`, and `minutes`,
    so that a station's own time doesn't count walking to the middle of the cell it's in.
    """
    date = gtfs.service_date(scenario.gtfs, day.weekday)
    # The stations after the cells, numbered on from them, as one set of destinations.
    destinations = list(zip(cells.geometry.y, cells.geometry.x, strict=True)) + [
        (s.lat, s.lon) for s in stations
    ]
    paths.CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.CACHE) as tmp:
        out = Path(tmp) / "times.parquet"
        _route.travel_times(
            paths.OSM_PBF,
            scenario.gtfs,
            date,
            day.departure.isoformat(),
            DEPARTURE_WINDOW // datetime.timedelta(minutes=1),
            MAX_RIDES,
            MAX_TIME // datetime.timedelta(minutes=1),
            [(s.id, s.lat, s.lon) for s in origins],
            destinations,
            WALKING_SPEED_MM_PER_S,
            out,
        )
        times = pd.read_parquet(out).rename(columns={"origin": "station", "destination": "cell"})
    to_station = times["cell"] >= len(cells)
    station_times = times[to_station].assign(
        to_station=lambda t: [stations[i - len(cells)].id for i in t["cell"]]
    )[["station", "to_station", "minutes"]]
    return (
        times[~to_station].astype({"station": "category", "cell": "int32", "minutes": "uint8"}),
        station_times.astype({"station": "category", "to_station": "category", "minutes": "uint8"}),
    )
