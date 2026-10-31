"""
Travel times from subway stations to the grid over the city,
routed with `mta-od-data`'s RAPTOR, walking on the streets, through `subway_sheds._route`.
"""

import datetime
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import geopandas as gpd
import pandas as pd

from subway_sheds import _route, access, gtfs, paths
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
# The speeds the page's slider walks out of stations at, in miles an hour.
SLIDER_MPH = (1.0, 5.0)
# How far the page may walk from a station: `EXIT_WALK` at the slider's fastest.
EXIT_WALK = datetime.timedelta(minutes=30)
EXIT_WALK_MM = int(SLIDER_MPH[1] * 1609.344 / 3600 * 1000 * EXIT_WALK.total_seconds())
# Every station's walks out to the cells, for walking at any of the slider's speeds.
EXITS = paths.TRAVEL_TIMES / "exits.parquet"
# A route's leg that walks between stations, rather than riding a pattern.
WALK = 0xFFFFFFFF
# Most rides in a journey, as R5 routed before.
MAX_RIDES = 8


def output_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}.parquet"


def arrivals_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}-arrivals.parquet"


def routes_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}-routes.parquet"


def patterns_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}-patterns.parquet"


def stations_output_path(scenario: Scenario, day: Day) -> Path:
    return paths.TRAVEL_TIMES / f"{scenario}-{day}-stations.parquet"


def _mta_stations() -> set[str]:
    """The stations `access.TABLE` has: every MTA station, not QueensLink's new ones."""
    return set(pd.read_csv(access.TABLE, dtype={"station": str})["station"])


def _entrances(
    stations: list[gtfs.Station],
) -> list[tuple[str, str, list[tuple[float, float, int]]]]:
    """
    Each of `stations`' entrances, from `access.TABLE`,
    or for stations it doesn't have, like QueensLink's new ones, their own coordinates,
    as an elevated station's, each its own complex.
    """
    table = pd.read_csv(access.TABLE, dtype={"station": str, "complex": str})
    by_station = {
        station: (
            str(group["complex"].iloc[0]),
            list(zip(group["lat"], group["lon"], group["secs"].astype(int), strict=True)),
        )
        for station, group in table.groupby("station")
    }
    elevated = round(access.entrance_secs("", access.STRUCTURE_FT["Elevated"], False, None))
    return [(s.id, *by_station.get(s.id, (s.id, [(s.lat, s.lon, elevated)]))) for s in stations]


def from_stations(
    runs: list[tuple[Scenario, Day]],
    origins: list[gtfs.Station],
    cells: gpd.GeoDataFrame,
    stations: list[gtfs.Station],
) -> Iterator[tuple[Scenario, Day, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]]:
    """
    Median minutes from each of `origins` to each of `cells` and `stations`, walking at both ends,
    in each scenario on each day of `runs`, routed together so that the streets are walked once.

    Only the pairs reachable within `MAX_TIME`:
    to the cells as `station`, `cell`, and `minutes`,
    and to the stations themselves as `station`, `to_station`, and `minutes`,
    so that a station's own time doesn't count walking to the middle of the cell it's in.
    With them, the median ride to each station's platforms reached by train,
    as `station`, `to_station`, and `seconds`, which the page adds walks out of stations to
    at whatever speed its reader walks.
    And the journey each takes to each station at the minute its arrival is the median of,
    as `station`, `to_station`, `leg`, `pattern` (`WALK` for a walk), `board`, `alight`,
    `from`, and `to`, with each run's patterns' stations going to `patterns_path`.
    Every station's walks out to the cells, for that, go to `EXITS`.
    """
    # The stations after the cells, numbered on from them, as one set of destinations.
    destinations = list(zip(cells.geometry.y, cells.geometry.x, strict=True)) + [
        (s.lat, s.lon) for s in stations
    ]
    paths.CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.CACHE) as tmp:
        outs = [Path(tmp) / f"{scenario}-{day}.parquet" for scenario, day in runs]
        _route.travel_times(
            paths.OSM_PBF,
            MAX_RIDES,
            MAX_TIME // datetime.timedelta(minutes=1),
            _entrances(stations),
            paths.PLATFORMS,
            [s.id for s in stations if s.id not in _mta_stations()],
            [s.id for s in origins],
            destinations,
            WALKING_SPEED_MM_PER_S,
            [
                (
                    scenario.gtfs,
                    gtfs.service_date(scenario.gtfs, day.weekday),
                    day.departure.isoformat(),
                    DEPARTURE_WINDOW // datetime.timedelta(minutes=1),
                    out,
                )
                for (scenario, day), out in zip(runs, outs, strict=True)
            ],
            Path(tmp) / "exits.parquet",
            EXIT_WALK_MM,
        )
        exits = pd.read_parquet(Path(tmp) / "exits.parquet").rename(columns={"destination": "cell"})
        EXITS.parent.mkdir(parents=True, exist_ok=True)
        exits[exits["cell"] < len(cells)].astype({"station": "category"}).to_parquet(
            EXITS, index=False
        )
        for (scenario, day), out in zip(runs, outs, strict=True):
            times = pd.read_parquet(out).rename(
                columns={"origin": "station", "destination": "cell"}
            )
            to_station = times["cell"] >= len(cells)
            station_times = times[to_station].assign(
                to_station=lambda t: [stations[i - len(cells)].id for i in t["cell"]]
            )[["station", "to_station", "minutes"]]
            arrivals = (
                pd.read_parquet(out.with_name(f"{out.stem}-arrivals.parquet"))
                .rename(columns={"origin": "station", "station": "to_station"})
                .astype({"station": "category", "to_station": "category"})
            )
            routes = (
                pd.read_parquet(out.with_name(f"{out.stem}-routes.parquet"))
                .rename(columns={"origin": "station", "station": "to_station"})
                .astype({"station": "category", "to_station": "category"})
            )
            shutil.copy(out.with_name(f"{out.stem}-patterns.parquet"), patterns_path(scenario, day))
            yield (
                scenario,
                day,
                times[~to_station].astype(
                    {"station": "category", "cell": "int32", "minutes": "uint8"}
                ),
                station_times.astype(
                    {"station": "category", "to_station": "category", "minutes": "uint8"}
                ),
                arrivals,
                routes,
            )
