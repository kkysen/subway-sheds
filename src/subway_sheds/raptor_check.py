"""
Travel times routed by RAPTOR (`subway_sheds._route`) walking exactly as R5 does,
to compare with R5's own, and so check RAPTOR alone: the first step of switching from R5.

R5's walks are read from R5 itself, in seconds, as its routing computes them
(R5 7.5.1's `TravelTimeComputer`, `TransferFinder`, and `PerTargetPropagater`):
from each origin to stops and straight to destinations, from stops to destinations,
and between stops.
"""

import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import jpype
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import r5py

from subway_sheds import _route, gtfs, paths, travel_times
from subway_sheds.scenarios import Day, Scenario

# Each scenario's walks, and RAPTOR's times walking them.
WALKS = paths.CACHE / "r5_walks"

# Most rides in a journey, as r5py has R5 route (`ProfileRequest.maxRides`).
R5_MAX_RIDES = 8

# R5's integers for "not reached".
_UNREACHED = 2**31 - 1


def walks_dir(scenario: Scenario) -> Path:
    return WALKS / str(scenario)


def raptor_output_path(scenario: Scenario, day: Day) -> Path:
    return walks_dir(scenario) / f"raptor-{day}.parquet"


def _java_ints(array: Any) -> np.ndarray:
    return np.asarray(array, dtype=np.int64)


def _write(path: Path, columns: dict[str, tuple[pa.DataType, list[Any]]]) -> None:
    table = pa.table(
        {name: pa.array(values, type=kind) for name, (kind, values) in columns.items()}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def write_walks(
    scenario: Scenario,
    day: Day,
    origins: list[gtfs.Station],
    destinations: gpd.GeoDataFrame,
) -> None:
    """
    Write R5's walks from `origins` and to `destinations` in `scenario`,
    as `route/src/walks.rs` reads them,
    with R5's request set as `travel_times.from_stations` sets it.
    """
    date = gtfs.service_date(scenario.gtfs, day.weekday)
    network = r5py.TransportNetwork(paths.OSM_PBF, [scenario.gtfs])
    task = r5py.RegionalTask(
        network,
        origin=destinations.geometry.iloc[0],
        destinations=destinations,
        departure=datetime.datetime.combine(date, day.departure),
        departure_time_window=travel_times.DEPARTURE_WINDOW,
        percentiles=travel_times.PERCENTILES,
        transport_modes=[r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK],
        max_time=travel_times.MAX_TIME,
        speed_walking=travel_times.WALKING_SPEED_KM_PER_H,
    )
    request = task._regional_task  # noqa: SLF001
    tn = network._transport_network  # noqa: SLF001
    street_mode = jpype.JClass("com.conveyal.r5.profile.StreetMode")
    street_router = jpype.JClass("com.conveyal.r5.streets.StreetRouter")
    routing_variable = jpype.JClass("com.conveyal.r5.streets.StreetRouter$State$RoutingVariable")
    walk = street_mode.WALK
    cost_to_vertex = jpype.JClass("com.conveyal.r5.streets.LinkedPointSet$CostToVertexFunction")

    walk_mm_per_s = int(request.walkSpeed * 1000)
    # `TravelTimeComputer`: walking to stops and destinations,
    # within the trip's limit and the walk's.
    access_limit = min(request.maxTripDurationMinutes * 60, request.getMaxTimeSeconds(walk))
    # `PerTargetPropagater`: walking from stops, within the walk's limit.
    egress_limit = request.getMaxTimeSeconds(walk)
    # `FastRaptorWorker.doTransfers`: walking between stops, within the walk's limit.
    transfer_limit_mm = walk_mm_per_s * request.maxWalkTime * 60
    # R5 scopes each stop's ID by its feed's, as `<feed>:<stop>`.
    stop_ids = [str(s).split(":", 1)[1] for s in tn.transitLayer.stopIdForIndex]
    out = walks_dir(scenario)

    from_stops: list[str] = []
    to_stops: list[str] = []
    transfer_secs: list[int] = []
    for a, packed in enumerate(tn.transitLayer.streetTransfers):
        if packed is None:
            continue
        pairs = _java_ints(packed.toArray()).reshape(-1, 2)
        for b, mm in pairs[pairs[:, 1] < transfer_limit_mm]:
            from_stops.append(stop_ids[a])
            to_stops.append(stop_ids[b])
            transfer_secs.append(int(mm) // walk_mm_per_s)
    _write(
        out / "transfers.parquet",
        {
            "from_stop": (pa.string(), from_stops),
            "to_stop": (pa.string(), to_stops),
            "secs": (pa.int32(), transfer_secs),
        },
    )

    linked = tn.linkageCache.getLinkage(request.destinationPointSets[0], tn.streetLayer, walk)
    costs = linked.getEgressCostTable()
    costs.destructivelyTransposeForPropagationAsNeeded()
    if costs.linkageCostUnit != routing_variable.DISTANCE_MILLIMETERS:
        raise ValueError(f"R5's walks from stops are in {costs.linkageCostUnit}, not millimeters")
    egress_stops: list[str] = []
    egress_destinations: list[int] = []
    egress_secs: list[int] = []
    for d in range(len(destinations)):
        table = costs.getCostTableForPoint(d)
        if table is None:
            continue
        stops = _java_ints(table.keys())
        secs = _java_ints(table.values()) // walk_mm_per_s
        kept = secs < egress_limit
        egress_stops.extend(stop_ids[s] for s in stops[kept])
        egress_destinations.extend([d] * int(kept.sum()))
        egress_secs.extend(secs[kept].tolist())
    _write(
        out / "egress.parquet",
        {
            "stop": (pa.string(), egress_stops),
            "destination": (pa.int32(), egress_destinations),
            "secs": (pa.int32(), egress_secs),
        },
    )

    on_street_mm_per_s = int(request.getSpeedForMode(walk) * 1000)
    access_origins: list[str] = []
    access_stops: list[str] = []
    access_secs: list[int] = []
    direct_origins: list[str] = []
    direct_destinations: list[int] = []
    direct_secs: list[int] = []
    for origin in origins:
        router = street_router(tn.streetLayer)
        router.profileRequest = request
        router.streetMode = walk
        if not router.setOrigin(origin.lat, origin.lon):
            continue
        router.timeLimitSeconds = access_limit
        router.quantityToMinimize = routing_variable.DURATION_SECONDS
        router.route()
        reached = router.getReachedStops()
        stops = _java_ints(reached.keys())
        access_origins.extend([origin.id] * len(stops))
        access_stops.extend(stop_ids[s] for s in stops)
        access_secs.extend(_java_ints(reached.values()).tolist())
        times = _java_ints(
            linked.eval(
                jpype.JProxy(cost_to_vertex, dict={"getCost": router.getTravelTimeToVertex}),
                jpype.JObject(on_street_mm_per_s, jpype.JClass("java.lang.Integer")),
                walk_mm_per_s,
                router.getOriginSplit(),
            ).travelTimes
        )
        reached_destinations = np.flatnonzero(times != _UNREACHED)
        direct_origins.extend([origin.id] * len(reached_destinations))
        direct_destinations.extend(reached_destinations.tolist())
        direct_secs.extend(times[reached_destinations].tolist())
    _write(
        out / "access.parquet",
        {
            "origin": (pa.string(), access_origins),
            "stop": (pa.string(), access_stops),
            "secs": (pa.int32(), access_secs),
        },
    )
    _write(
        out / "direct.parquet",
        {
            "origin": (pa.string(), direct_origins),
            "destination": (pa.int32(), direct_destinations),
            "secs": (pa.int32(), direct_secs),
        },
    )
    if request.maxRides != R5_MAX_RIDES:
        raise ValueError(f"R5 routes {request.maxRides} rides at most, not {R5_MAX_RIDES}")


def route(
    scenario: Scenario, day: Day, origins: list[gtfs.Station], destinations: int
) -> tuple[int, list[tuple[str, str, int]]]:
    """RAPTOR's times from `origins` walking R5's walks, as R5 routes them."""
    date = gtfs.service_date(scenario.gtfs, day.weekday)
    window = travel_times.DEPARTURE_WINDOW // datetime.timedelta(minutes=1)
    return _route.travel_times(
        scenario.gtfs,
        date,
        day.departure.isoformat(),
        window,
        R5_MAX_RIDES,
        travel_times.MAX_TIME // datetime.timedelta(minutes=1),
        walks_dir(scenario),
        [o.id for o in origins],
        destinations,
        raptor_output_path(scenario, day),
    )


def compare(scenario: Scenario, day: Day, cells: int, stations: list[gtfs.Station]) -> pd.DataFrame:
    """
    R5's and RAPTOR's minutes side by side, as `origin`, `destination`, `r5`, and `raptor`,
    with `NaN` where one doesn't reach it,
    origins as their index in `stations`,
    and destinations numbered as RAPTOR's: the cells, then `stations`.

    Keyed by integers, not station IDs, since there are millions of rows.
    """
    index = {s.id: i for i, s in enumerate(stations)}

    def origins(ids: pd.Series) -> np.ndarray:
        return ids.astype(str).map(index).to_numpy(dtype=np.int16)

    r5_cells = pd.read_parquet(travel_times.output_path(scenario, day))
    r5_stations = pd.read_parquet(travel_times.stations_output_path(scenario, day))
    r5 = pd.DataFrame(
        {
            "origin": np.concatenate(
                [origins(r5_cells["station"]), origins(r5_stations["station"])]
            ),
            "destination": np.concatenate(
                [
                    r5_cells["cell"].to_numpy(dtype=np.int32),
                    cells + origins(r5_stations["to_station"]).astype(np.int32),
                ]
            ),
            "r5": np.concatenate(
                [
                    r5_cells["minutes"].to_numpy(np.float32),
                    r5_stations["minutes"].to_numpy(np.float32),
                ]
            ),
        }
    )
    del r5_cells, r5_stations
    raptor = pd.read_parquet(raptor_output_path(scenario, day))
    raptor = pd.DataFrame(
        {
            "origin": origins(raptor["origin"]),
            "destination": raptor["destination"].to_numpy(dtype=np.int32),
            "raptor": raptor["minutes"].to_numpy(np.float32),
        }
    )
    return r5.merge(raptor, on=["origin", "destination"], how="outer")
