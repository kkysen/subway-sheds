"""Travel times from subway stations to the grid over the city, routed with R5."""

import datetime
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import override

import geopandas as gpd
import numpy as np
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
# 3 mph, a typical adult's pace, rather than R5's default, 3.6 km/h, which is slower.
WALKING_SPEED_KM_PER_H = 3 * 1.609344


class _ParallelTravelTimeMatrix(r5py.TravelTimeMatrix):
    """
    A `TravelTimeMatrix` that routes its origins on several threads at once.

    `r5py` routes them one at a time, keeping only a few cores busy,
    though it defines how many threads to use (`NUM_THREADS`, half the cores).
    Each origin is routed in Java, which `jpype` runs without holding the GIL.
    One thread per core rather than half as many routes about 1.7 times as fast
    (11 s rather than 19 s for 150 stations on 4 cores, like a GitHub runner's),
    and twice as many again is no faster.
    """

    NUM_THREADS = os.process_cpu_count() or 1

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


def stations_output_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}-stations.parquet"


def _points(stations: list[gtfs.Station], ids: object) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": ids},
        geometry=gpd.points_from_xy([s.lon for s in stations], [s.lat for s in stations]),
        crs=WGS84,
    )


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
    # The stations after the cells, numbered on from them, as R5 takes one set of destinations.
    station_points = _points(stations, len(cells) + np.arange(len(stations)))
    matrix = _travel_time_matrix(
        r5py.TransportNetwork(paths.OSM_PBF, [scenario.gtfs]),
        origins=_points(origins, [s.id for s in origins]),
        destinations=pd.concat([cells, station_points], ignore_index=True),
        departure=datetime.datetime.combine(date, day.departure),
        departure_time_window=DEPARTURE_WINDOW,
        percentiles=PERCENTILES,
        transport_modes=[r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK],
        max_time=MAX_TIME,
        speed_walking=WALKING_SPEED_KM_PER_H,
    )
    times = pd.DataFrame(matrix).rename(
        columns={"from_id": "station", "to_id": "cell", "travel_time": "minutes"}
    )
    times = times.dropna(subset=["minutes"])
    to_station = times["cell"] >= len(cells)
    station_times = times[to_station].assign(
        to_station=lambda t: [stations[i - len(cells)].id for i in t["cell"]]
    )[["station", "to_station", "minutes"]]
    return (
        times[~to_station].astype({"station": "category", "cell": "int32", "minutes": "uint8"}),
        station_times.astype({"station": "category", "to_station": "category", "minutes": "uint8"}),
    )
