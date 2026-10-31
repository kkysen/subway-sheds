"""Travel times as the web page loads them."""

import datetime
import itertools
import json
import os
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import brotli
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from subway_sheds import access, gtfs, lines, paths, travel_times
from subway_sheds.grid import SPACING, Grid
from subway_sheds.scenarios import FREE_TRANSFERS, Day, Fare, Scenario

# In a station's arrivals, a station not reached by train within `travel_times.MAX_TIME`.
UNREACHED = 0xFFFF
# In a station's routes, a leg that walks between stations.
WALK_LEG = 0xFFFF


class _BrotliFiles:
    """
    Writes files compressed with Brotli, as GitHub Pages won't:
    it sends `.bin` files as they are, so the page decompresses them itself.
    Compressed on every core, as Brotli's best takes minutes for every station's files,
    and `brotli` lets go of the GIL to compress, with only a few files waiting at a time.
    """

    def __init__(self) -> None:
        workers = os.cpu_count() or 1
        self._pool = ThreadPoolExecutor(workers)
        self._waiting = threading.BoundedSemaphore(2 * workers)
        self._written: list[Future[None]] = []

    def write(self, path: Path, data: bytes) -> None:
        self._waiting.acquire()
        self._written.append(self._pool.submit(self._write, path, data))

    def _write(self, path: Path, data: bytes) -> None:
        try:
            path.write_bytes(brotli.compress(data, quality=11))
        finally:
            self._waiting.release()

    def close(self) -> None:
        """Wait for every file, raising what writing any did."""
        self._pool.shutdown()
        for written in self._written:
            written.result()


def _station_rows(ids: pa.ChunkedArray, index: dict[str, int]) -> np.ndarray:
    """Each of `ids`' index in `stations.json`, by their dictionary, not their many rows."""
    encoded = ids.combine_chunks().dictionary_encode()
    lookup = np.array(
        [index.get(str(s), 0) for s in encoded.dictionary.to_pylist()], dtype=np.uint16
    )
    return lookup[encoded.indices.to_numpy()]


def _bounds(groups: np.ndarray, n: int) -> np.ndarray:
    """Where each of `n` groups' rows start in `groups`, which has each's together, and end."""
    bounds = np.zeros(n + 1, dtype=np.int64)
    changes = np.flatnonzero(np.concatenate([[True], groups[1:] != groups[:-1]]))
    ends = np.append(changes[1:], len(groups))
    bounds[groups[changes].astype(np.int64) + 1] = ends - changes
    return np.cumsum(bounds)


def _deltas(values: np.ndarray, first: np.ndarray) -> np.ndarray:
    """Each of `values` less the one before it, but where `first`, which starts a series."""
    values = values.astype(np.int64)
    before = np.concatenate([[0], values[:-1]])
    return np.where(first, values, values - before)


def write(grid: Grid) -> None:
    """
    Write the grid, the stations, and each station's times to `paths.WEB_DATA`.

    - `grid.json`: the Web Mercator center of cell (0, 0), the spacing,
      and each cell's column and row.
    - `stations.json`: each station in either scenario, and which, with its complex,
      and the complexes changed to free from it out of the system, if any.
    - `lines.json`: GeoJSON of the lines between stations, each with its color
      and which scenarios run on it.
    - `walks.json`: the street walk between each pair of stations of different complexes
      that some journey in `routes/` walks, by `{from}|{to}`, their IDs,
      as `[longitude, latitude]`s from the entrance walked out of to the one walked into.
    - `routes.json`: each route's name and colors, by its ID.
    - `patterns.json`: in each run, by day, then fare, then scenario,
      each pattern's route and its stops' stations, as `stations.json` lists them.
    - `routes/{station}.bin`: the journeys from the station to each station,
      each departing at the minute it was read back at and every minute before
      until the one before it, so that the page can show the one behind any trip it times,
      in each run, by day, then fare, then scenario, then station, as `stations.json` lists them:
      a `uint8` count of journeys for each, then each journey's minute, a `uint8`,
      as the change from the one before but a station's first, then each's count of legs, a `uint8`,
      then every leg, as five little-endian `uint16`s,
      a ride's pattern and the positions in it it boards and gets off at,
      or `WALK_LEG` and the stations a walk between stations is from and to,
      then when it starts, in seconds from the window's start for a journey's first leg
      and after the leg before for the rest, and how long it takes,
      as those are small and alike, which compresses better.
    - `exits.bin`: every walk out of a station to a cell within `travel_times.NEAR_EXIT_WALK_M`,
      to time at the reader's walking speed,
      as four little-endian `uint16` arrays of equal length after their length, a `uint32`:
      the station, as `stations.json` lists them, the cell, as `grid.json` lists them,
      the seconds out through the entrance, and the meters on the streets after it,
      by station.
    - `exits-far.bin`: the rest, as `exits.bin` has them, which the page loads after,
      as there are about as many again, but few are any cell's quickest way.
    - `arrivals/{station}.bin`: when trains from the station reach each station's platforms,
      for each minute of the departure window, in each run,
      in seconds from the window's start, `UNREACHED` if not within `travel_times.MAX_TIME`.
      As that only changes when the best train does, only the changes:
      by day, then fare, then scenario, in the order `Day`, `Fare`, and `Scenario` list them,
      then station, as `stations.json` lists them,
      a `uint8` count of changes for each, then each change's minute, a `uint8`,
      then each change's time, a little-endian `uint16`, all in that order,
      each minute and time but a station's first as the change from the one before,
      wrapping around, as those are small and alike, which compresses better.
      The page adds each departure's walk out to each cell and takes the median, as routing does.

    The `.bin` files are each compressed with Brotli (see `_BrotliFiles`).
    """
    # Only the files, keeping the directories, which a running `vite` watches:
    # replacing them, it serves the page itself for every file in them until restarted.
    for old in paths.WEB_DATA.rglob("*"):
        if not old.is_dir():
            old.unlink()
    arrivals_dir = paths.WEB_DATA / "arrivals"
    arrivals_dir.mkdir(parents=True, exist_ok=True)
    files = _BrotliFiles()

    (paths.WEB_DATA / "grid.json").write_text(
        json.dumps(
            {
                "x0": grid.x0,
                "y0": grid.y0,
                "spacing": SPACING,
                "cols": grid.cols.tolist(),
                "rows": grid.rows.tolist(),
            },
            separators=(",", ":"),
        )
    )

    # Each station's complex, the stations changed between without paying again, its own if none.
    complexes = (
        pd.read_csv(access.TABLE, dtype=str)
        .drop_duplicates("station")
        .set_index("station")["complex"]
    )
    stations: dict[str, dict[str, object]] = {}
    for scenario in Scenario:
        for station in gtfs.stations(scenario.gtfs):
            entry = stations.setdefault(
                station.id,
                {
                    "id": station.id,
                    "name": station.name,
                    "lon": station.lon,
                    "lat": station.lat,
                    "complex": str(complexes.get(station.id, station.id)),
                    "scenarios": [],
                },
            )
            scenarios = entry["scenarios"]
            assert isinstance(scenarios, list)
            scenarios.append(str(scenario))
    # Every station of each complex, as routing changes free from any of them.
    complex_of = {station: str(entry["complex"]) for station, entry in stations.items()}
    for a, b in FREE_TRANSFERS:
        for one, other in ((complex_of[a], complex_of[b]), (complex_of[b], complex_of[a])):
            for entry in stations.values():
                if entry["complex"] == one:
                    free = entry.setdefault("free", [])
                    assert isinstance(free, list)
                    free.append(other)
    (paths.WEB_DATA / "stations.json").write_text(
        json.dumps(list(stations.values()), separators=(",", ":"))
    )

    segments: dict[tuple[str, tuple[str, str]], dict[str, object]] = {}
    for scenario in Scenario:
        for segment in lines.segments(scenario.gtfs):
            feature = segments.setdefault(
                (segment.color, segment.stations),
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": segment.coordinates},
                    "properties": {
                        "color": segment.color,
                        "from": segment.stations[0],
                        "to": segment.stations[1],
                        "shortcut": segment.shortcut,
                    },
                },
            )
            properties = feature["properties"]
            assert isinstance(properties, dict)
            properties[str(scenario)] = True
    (paths.WEB_DATA / "lines.json").write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": list(segments.values())},
            separators=(",", ":"),
        )
    )

    index = {station: i for i, station in enumerate(stations)}

    def rows_of(ids: pd.Series) -> np.ndarray:
        """Each of `ids`' index in `stations`, by its categories, not its many rows."""
        codes = ids.astype("category").cat
        return np.array([index[str(c)] for c in codes.categories], dtype=np.intp)[codes.codes]

    exits = pd.read_parquet(travel_times.EXITS)
    exits = exits.assign(station=rows_of(exits["station"])).sort_values(["station", "cell"])
    near = exits["meters"] <= travel_times.NEAR_EXIT_WALK_M
    for name, part in [("exits.bin", exits[near]), ("exits-far.bin", exits[~near])]:
        columns = [
            part[c].to_numpy(dtype="<u2") for c in ["station", "cell", "exit_seconds", "meters"]
        ]
        files.write(
            paths.WEB_DATA / name,
            np.array([len(part)], dtype="<u4").tobytes() + b"".join(c.tobytes() for c in columns),
        )

    minutes = travel_times.DEPARTURE_WINDOW // datetime.timedelta(minutes=1)
    seconds = np.full(
        (len(stations), len(Day) * len(Fare) * len(Scenario), minutes, len(stations)),
        UNREACHED,
        dtype="<u2",
    )
    runs = list(itertools.product(Day, Fare, Scenario))
    for r, (day, fare, scenario) in enumerate(runs):
        arrivals = pd.read_parquet(travel_times.arrivals_path(scenario, day, fare))
        seconds[
            rows_of(arrivals["station"]),
            r,
            arrivals["minute"].to_numpy(),
            rows_of(arrivals["to_station"]),
        ] = arrivals["seconds"].to_numpy()
    routes: dict[str, dict[str, str]] = {}
    for scenario in Scenario:
        with zipfile.ZipFile(scenario.gtfs) as feed:
            for route in gtfs.read_csv(feed, "routes.txt"):
                routes[route["route_id"]] = {
                    "name": route["route_short_name"],
                    "color": f"#{route.get('route_color') or '808183'}",
                    "text": f"#{route.get('route_text_color') or 'FFFFFF'}",
                }
    (paths.WEB_DATA / "routes.json").write_text(json.dumps(routes, separators=(",", ":")))
    patterns = []
    for day, fare, scenario in runs:
        stops = pd.read_parquet(travel_times.patterns_path(scenario, day, fare))
        patterns.append(
            [
                {
                    "route": str(group["route"].iloc[0]),
                    "stations": [
                        index.get(s, -1) for s in group.sort_values("position")["station"]
                    ],
                }
                for _, group in stops.groupby("pattern", sort=True)
            ]
        )
    (paths.WEB_DATA / "patterns.json").write_text(json.dumps(patterns, separators=(",", ":")))
    routes_dir = paths.WEB_DATA / "routes"
    routes_dir.mkdir(exist_ok=True)
    # Each run's legs, as `route` writes them: by origin, then destination, minute, and leg,
    # as compact arrays, sliced by origin below rather than grouped, as there are tens of millions.
    columns_of: list[dict[str, np.ndarray]] = []
    for day, fare, scenario in runs:
        table = pq.read_table(travel_times.routes_path(scenario, day, fare))
        walk = table["pattern"].to_numpy() == travel_times.WALK
        origin = _station_rows(table["station"], index)
        columns_of.append(
            {
                "origin": origin,
                "dest": _station_rows(table["to_station"], index),
                "minute": table["minute"].to_numpy(),
                "leg": table["leg"].to_numpy(),
                "p": np.where(walk, WALK_LEG, table["pattern"].to_numpy()).astype(np.uint16),
                "a": np.where(
                    walk, _station_rows(table["from"], index), table["board"].to_numpy()
                ).astype(np.uint16),
                "b": np.where(
                    walk, _station_rows(table["to"], index), table["alight"].to_numpy()
                ).astype(np.uint16),
                "start": table["start"].to_numpy(),
                "end": table["end"].to_numpy(),
                # Where each origin's rows start and end, as they're together.
                "bounds": _bounds(origin, len(stations)),
            }
        )
        del table
    # Only the walks between stations some journey walks, as most never are.
    walked: set[int] = set()
    for c in columns_of:
        walk = c["p"] == WALK_LEG
        walked.update(
            np.unique(c["a"][walk].astype(np.int64) * len(stations) + c["b"][walk]).tolist()
        )
    station_ids = list(stations)
    walked_ids = {
        (station_ids[ab // len(stations)], station_ids[ab % len(stations)]) for ab in walked
    }
    walks = pd.read_parquet(travel_times.WALK_PATHS)
    walks = walks[[(a, b) in walked_ids for a, b in zip(walks["from"], walks["to"], strict=True)]]
    (paths.WEB_DATA / "walks.json").write_text(
        json.dumps(
            {
                f"{a}|{b}": [
                    [round(lon, 5), round(lat, 5)]
                    for lat, lon in zip(walk["latitude"], walk["longitude"], strict=True)
                ]
                for (a, b), walk in walks.groupby(["from", "to"], sort=False)
            },
            separators=(",", ":"),
        )
    )

    for station, i in index.items():
        counts = np.zeros((len(runs), len(stations)), dtype=np.uint8)
        pieces = [
            {k: v[c["bounds"][i] : c["bounds"][i + 1]] for k, v in c.items() if k != "bounds"}
            for c in columns_of
        ]
        legs = {k: np.concatenate([piece[k] for piece in pieces]) for k in pieces[0]}
        run = np.repeat(np.arange(len(runs)), [len(piece["leg"]) for piece in pieces])
        # Each journey, starting where its run, destination, or minute changes,
        # and its legs: none for one walked from the origin, on a row of its own.
        starts = np.flatnonzero(
            np.concatenate(
                [
                    [True],
                    (run[1:] != run[:-1])
                    | (legs["dest"][1:] != legs["dest"][:-1])
                    | (legs["minute"][1:] != legs["minute"][:-1]),
                ]
            )
        )[: len(run)]
        has_leg = legs["leg"] != travel_times.NO_LEGS
        leg_counts = (
            np.add.reduceat(has_leg.astype(np.uint8), starts) if len(starts) else np.zeros(0, int)
        )
        journey_run = run[starts]
        journey_dest = legs["dest"][starts]
        np.add.at(counts, (journey_run, journey_dest), 1)
        first_journey = np.concatenate(
            [
                [True],
                (journey_run[1:] != journey_run[:-1]) | (journey_dest[1:] != journey_dest[:-1]),
            ]
        )[: len(starts)]
        start = legs["start"][has_leg].astype(np.int64)
        end = legs["end"][has_leg].astype(np.int64)
        first = legs["leg"][has_leg] == 0
        before = np.concatenate([[0], end[:-1]])[: len(end)]
        fields = np.column_stack(
            [
                legs["p"][has_leg],
                legs["a"][has_leg],
                legs["b"][has_leg],
                np.where(first, start, start - before),
                end - start,
            ]
        ).astype("<u2")
        files.write(
            routes_dir / f"{station}.bin",
            counts.tobytes()
            + _deltas(legs["minute"][starts], first_journey).astype(np.uint8).tobytes()
            + leg_counts.astype(np.uint8).tobytes()
            + fields.tobytes(),
        )
    del columns_of

    # When, from the window's start, rather than how long after each departure.
    reached = seconds != UNREACHED
    offsets = (np.arange(minutes, dtype=np.uint32) * 60)[:, np.newaxis]
    clock = np.where(reached, seconds.astype(np.uint32) + offsets, UNREACHED).astype("<u2")
    del seconds, reached
    # Station-major, so that each station's minutes are together.
    clock = np.ascontiguousarray(clock.swapaxes(2, 3))
    changed = np.ones_like(clock, dtype=bool)
    changed[..., 1:] = clock[..., 1:] != clock[..., :-1]
    counts = changed.sum(axis=-1, dtype=np.uint8)
    minute_of = np.broadcast_to(np.arange(minutes, dtype=np.uint8), clock.shape[1:])
    for station, i in index.items():
        minute = minute_of[changed[i]]
        first = minute == 0
        files.write(
            arrivals_dir / f"{station}.bin",
            counts[i].tobytes()
            + _deltas(minute, first).astype(np.uint8).tobytes()
            + (_deltas(clock[i][changed[i]], first) % 0x10000).astype("<u2").tobytes(),
        )
    files.close()
