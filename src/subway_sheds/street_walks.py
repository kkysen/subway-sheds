"""
Every walk on the streets that routing and the page take, walked once and kept in `paths.WALKS`,
with the grid of cells they walk out to,
so that routing and exporting needn't read and walk the streets again,
and made again only when what they're made from changes.
"""

import datetime
import hashlib
import pickle

import numpy as np
import pandas as pd

from subway_sheds import _route, grid, gtfs, inputs, paths, travel_times
from subway_sheds.scenarios import Scenario

# Every walk from and between stations, as `_route.walks` writes them for `_route.travel_times`.
STREET_WALKS = paths.WALKS / "street_walks.bin"
# A hash of what they were last made from, as `digest` hashes it.
INPUTS = paths.WALKS / "inputs.sha256"
MADE = [STREET_WALKS, paths.GRID, travel_times.EXITS, travel_times.WALK_PATHS]


def stations() -> list[gtfs.Station]:
    """
    Every scenario's stations, each walked from in every scenario,
    so that from QueensLink's new ones, the baseline is walking from where they'd be,
    and there's saying how much they save.
    """
    return list({s.id: s for scenario in Scenario for s in gtfs.stations(scenario.gtfs)}.values())


def digest(*args: object) -> str:
    """
    A hash of `args`, as a call to the `route` crate takes them,
    with the streets and the crate's own source, which it makes what it does from.
    """
    out = hashlib.sha256(pickle.dumps(args))
    for path in [paths.OSM_PBF, *inputs.route_files()]:
        out.update(str(path.relative_to(paths.ROOT)).encode() + b"\0")
        out.update(hashlib.sha256(path.read_bytes()).digest())
    return out.hexdigest()


def write(force: bool = False) -> bool:
    """
    Walk every station's walks and find the grid, unless already made from the same,
    or `force`.
    Whether they were made.
    """
    land = grid.on_land()
    args = (
        travel_times._entrances(stations()),
        grid.lat_lons(land),
        float(grid.MAX_STREET_DISTANCE),
        travel_times.WALKING_SPEED_MM_PER_S,
        travel_times.MAX_TIME // datetime.timedelta(minutes=1),
        travel_times.EXIT_WALK_MM,
    )
    key = digest(*args)
    made = INPUTS.exists() and all(path.exists() for path in MADE)
    if not force and made and INPUTS.read_text() == key:
        return False
    paths.WALKS.mkdir(parents=True, exist_ok=True)
    # So that failing midway isn't taken for made.
    INPUTS.unlink(missing_ok=True)
    entrances, points, meters, speed, max_minutes, exits_limit = args
    raw_exits = paths.WALKS / "exits-raw.parquet"
    near = np.array(
        _route.walks(
            paths.OSM_PBF,
            entrances,
            points,
            meters,
            speed,
            max_minutes,
            STREET_WALKS,
            raw_exits,
            exits_limit,
            travel_times.WALK_PATHS,
        ),
        dtype=bool,
    )
    exits = pd.read_parquet(raw_exits).rename(columns={"destination": "cell"})
    exits.astype({"station": "category"}).to_parquet(travel_times.EXITS, index=False)
    raw_exits.unlink()
    near_streets = grid.Grid(x0=land.x0, y0=land.y0, cols=land.cols[near], rows=land.rows[near])
    grid.write(near_streets, paths.GRID)
    INPUTS.write_text(key)
    return True


# `exit_trees`' last trees, and a hash of what they were walked from.
EXIT_TREES = paths.WALKS / "exit_trees.pickle"
EXIT_TREES_INPUTS = paths.WALKS / "exit_trees.sha256"


def exit_trees(
    entrances: list[tuple[str, str, list[tuple[float, float, int]]]],
    destinations: list[tuple[float, float]],
    speed: int,
    limit: int,
    wanted: list[tuple[int, int, bool]],
) -> list[tuple[bytes, bytes, bytes, int, bytes, int]]:
    """
    `_route.exit_trees` on the streets,
    or the trees it last gave if it was last given the same, on the same streets,
    as which walks are `wanted` changes only when the routed times do.
    """
    key = digest(entrances, destinations, speed, limit, wanted)
    if EXIT_TREES_INPUTS.exists() and EXIT_TREES_INPUTS.read_text() == key:
        with EXIT_TREES.open("rb") as f:
            return pickle.load(f)
    EXIT_TREES_INPUTS.unlink(missing_ok=True)
    trees = _route.exit_trees(paths.OSM_PBF, entrances, destinations, speed, limit, wanted)
    with EXIT_TREES.open("wb") as f:
        pickle.dump(trees, f)
    EXIT_TREES_INPUTS.write_text(key)
    return trees
