"""Travel times from subway stations to the grid over the city, routed with R5."""

import datetime
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import pandas as pd
import r5py

from subway_sheds import gtfs, paths
from subway_sheds.grid import WGS84
from subway_sheds.scenarios import Scenario

# The morning peak: R5 departs every minute in the window and reports percentiles across them,
# so waiting for a train counts the way it does for someone who doesn't time their arrival.
DEPARTURE_TIME = datetime.time(7, 30)
DEPARTURE_WINDOW = datetime.timedelta(hours=1)
PERCENTILES = [50]
MAX_TIME = datetime.timedelta(minutes=90)

# `TravelTimeMatrix` subclasses `GeoDataFrame`, so type checkers check calls to it against
# pandas' constructor overloads rather than its own `__init__`, which takes keywords for R5.
_travel_time_matrix: Callable[..., pd.DataFrame] = r5py.TravelTimeMatrix


def output_path(scenario: Scenario) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}.parquet"


def from_stations(
    scenario: Scenario,
    origins: list[gtfs.Station],
    destinations: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """
    Median minutes from each of `origins` to each of `destinations`, walking at both ends.

    Only the pairs reachable within `MAX_TIME`, as `station`, `cell`, and `minutes`.
    """
    date = gtfs.weekday_service_date(scenario.gtfs)
    origin_points = gpd.GeoDataFrame(
        {"id": [s.id for s in origins]},
        geometry=gpd.points_from_xy([s.lon for s in origins], [s.lat for s in origins]),
        crs=WGS84,
    )
    matrix = _travel_time_matrix(
        r5py.TransportNetwork(paths.OSM_PBF, [scenario.gtfs]),
        origins=origin_points,
        destinations=destinations,
        departure=datetime.datetime.combine(date, DEPARTURE_TIME),
        departure_time_window=DEPARTURE_WINDOW,
        percentiles=PERCENTILES,
        transport_modes=[r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK],
        max_time=MAX_TIME,
    )
    times = pd.DataFrame(matrix).rename(
        columns={"from_id": "station", "to_id": "cell", "travel_time": "minutes"}
    )
    times = times.dropna(subset=["minutes"])
    return times.astype({"station": "category", "cell": "int32", "minutes": "uint8"})
