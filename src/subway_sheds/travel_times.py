"""Travel times from subway stations to the grid over the city, routed with R5."""

import datetime
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import override

import geopandas as gpd
import pandas as pd
import r5py

from subway_sheds import gtfs, paths
from subway_sheds.grid import WGS84
from subway_sheds.scenarios import Day, Scenario

# From each day's departure time, R5 departs every minute in the window
# and reports percentiles across them,
# so waiting for a train counts the way it does for someone who doesn't time their arrival.
DEPARTURE_WINDOW = datetime.timedelta(hours=1)
PERCENTILES = [50]
# Past the 90 minutes the page draws travel times out to,
# so that it can tell how much QueensLink saves places that take longer than that without it.
MAX_TIME = datetime.timedelta(minutes=120)


class _ParallelTravelTimeMatrix(r5py.TravelTimeMatrix):
    """
    A `TravelTimeMatrix` that routes its origins on several threads at once.

    `r5py` routes them one at a time, keeping only a few cores busy,
    though it defines how many threads to use (`NUM_THREADS`, half the cores).
    Each origin is routed in Java, which `jpype` runs without holding the GIL.
    """

    @override
    def _compute(self) -> pd.DataFrame:
        self._prepare_origins_destinations()
        self.request.destinations = self.destinations
        with ThreadPoolExecutor(self.NUM_THREADS) as pool:
            matrices = list(pool.map(self._travel_times_per_origin, self.origins.id))
        return pd.concat(matrices, ignore_index=True)


# `TravelTimeMatrix` subclasses `GeoDataFrame`, so type checkers check calls to it against
# pandas' constructor overloads rather than its own `__init__`, which takes keywords for R5.
_travel_time_matrix: Callable[..., pd.DataFrame] = _ParallelTravelTimeMatrix


def output_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}.parquet"


def from_stations(
    scenario: Scenario,
    day: Day,
    origins: list[gtfs.Station],
    destinations: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """
    Median minutes from each of `origins` to each of `destinations`, walking at both ends.

    Only the pairs reachable within `MAX_TIME`, as `station`, `cell`, and `minutes`.
    """
    date = gtfs.service_date(scenario.gtfs, day.weekday)
    origin_points = gpd.GeoDataFrame(
        {"id": [s.id for s in origins]},
        geometry=gpd.points_from_xy([s.lon for s in origins], [s.lat for s in origins]),
        crs=WGS84,
    )
    matrix = _travel_time_matrix(
        r5py.TransportNetwork(paths.OSM_PBF, [scenario.gtfs]),
        origins=origin_points,
        destinations=destinations,
        departure=datetime.datetime.combine(date, day.departure),
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
