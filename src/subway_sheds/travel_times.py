"""Travel times from subway stations to a grid of points over the city, routed with R5."""

import datetime
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import r5py
import shapely

from subway_sheds import gtfs, paths
from subway_sheds.scenarios import Scenario

# UTM zone 18N: meters, for laying out the grid.
METRIC_CRS = "EPSG:32618"
WGS84 = "EPSG:4326"

GRID_SPACING_M = 200
# How far past the outermost stations the grid reaches.
GRID_MARGIN_M = 3000

# The morning peak: R5 departs every minute in the window and reports percentiles across them,
# so waiting for a train counts the way it does for someone who doesn't time their arrival.
DEPARTURE_TIME = datetime.time(7, 30)
DEPARTURE_WINDOW = datetime.timedelta(hours=1)
PERCENTILES = [50]
MAX_TIME = datetime.timedelta(minutes=90)


def grid(stations: list[gtfs.Station]) -> gpd.GeoDataFrame:
    points = gpd.GeoSeries(
        gpd.points_from_xy([s.lon for s in stations], [s.lat for s in stations]), crs=WGS84
    ).to_crs(METRIC_CRS)
    min_x, min_y, max_x, max_y = points.total_bounds
    xs = np.arange(min_x - GRID_MARGIN_M, max_x + GRID_MARGIN_M, GRID_SPACING_M)
    ys = np.arange(min_y - GRID_MARGIN_M, max_y + GRID_MARGIN_M, GRID_SPACING_M)
    x, y = np.meshgrid(xs, ys)
    cells = gpd.GeoDataFrame(
        {"id": np.arange(x.size)},
        geometry=shapely.points(x.ravel(), y.ravel()),
        crs=METRIC_CRS,
    )
    return cells.to_crs(WGS84)


# `TravelTimeMatrix` subclasses `GeoDataFrame`, so type checkers check calls to it against
# pandas' constructor overloads rather than its own `__init__`, which takes keywords for R5.
_travel_time_matrix: Callable[..., pd.DataFrame] = r5py.TravelTimeMatrix


def network(scenario: Scenario) -> r5py.TransportNetwork:
    return r5py.TransportNetwork(paths.OSM_PBF, [scenario.gtfs])


def from_stations(
    scenario: Scenario,
    origins: list[gtfs.Station],
    destinations: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """Median minutes from each of `origins` to each of `destinations`, walking at both ends."""
    date = gtfs.weekday_service_date(scenario.gtfs)
    origin_points = gpd.GeoDataFrame(
        {"id": [s.id for s in origins]},
        geometry=gpd.points_from_xy([s.lon for s in origins], [s.lat for s in origins]),
        crs=WGS84,
    )
    matrix = _travel_time_matrix(
        network(scenario),
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
    times["scenario"] = str(scenario)
    return times


def output_path(scenario: Scenario, station: str) -> Path:
    return paths.TRAVEL_TIMES / str(scenario) / f"{station}.parquet"
