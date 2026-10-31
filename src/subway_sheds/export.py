"""Travel times as the web page loads them."""

import datetime
import itertools
import json
import zipfile

import numpy as np
import pandas as pd

from subway_sheds import access, gtfs, lines, paths, travel_times
from subway_sheds.grid import SPACING, Grid
from subway_sheds.scenarios import FREE_TRANSFERS, Day, Fare, Scenario

# In a station's arrivals, a station not reached by train within `travel_times.MAX_TIME`.
UNREACHED = 0xFFFF
# In a station's routes, a leg that walks between stations.
WALK_LEG = 0xFFFF


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
    - `routes.json`: each route's name and colors, by its ID.
    - `patterns.json`: in each run, by day, then fare, then scenario,
      each pattern's route and its stops' stations, as `stations.json` lists them.
    - `routes/{station}.bin`: the journey from the station to each station at the minute
      its arrival there is the median of, in each run, by day, then fare, then scenario,
      then station, as `stations.json` lists them:
      a `uint8` count of legs for each, then every leg, as five little-endian `uint16`s,
      a ride's pattern and the positions in it it boards and gets off at,
      or `WALK_LEG` and the stations a walk between stations is from and to,
      then when it starts, in seconds from the window's start for a journey's first leg
      and after the leg before for the rest, and how long it takes,
      as those are small and alike, which compresses better.
    - `exits.bin`: every walk out of a station to a cell, to time at the reader's walking speed,
      as four little-endian `uint16` arrays of equal length after their length, a `uint32`:
      the station, as `stations.json` lists them, the cell, as `grid.json` lists them,
      the seconds out through the entrance, and the meters on the streets after it,
      by station.
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
    """
    # Only the files, keeping the directories, which a running `vite` watches:
    # replacing them, it serves the page itself for every file in them until restarted.
    for old in paths.WEB_DATA.rglob("*"):
        if not old.is_dir():
            old.unlink()
    arrivals_dir = paths.WEB_DATA / "arrivals"
    arrivals_dir.mkdir(parents=True, exist_ok=True)

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
    columns = [
        exits[c].to_numpy(dtype="<u2") for c in ["station", "cell", "exit_seconds", "meters"]
    ]
    (paths.WEB_DATA / "exits.bin").write_bytes(
        np.array([len(exits)], dtype="<u4").tobytes() + b"".join(c.tobytes() for c in columns)
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
    counts = np.zeros((len(stations), len(runs), len(stations)), dtype=np.uint8)
    legs_of: list[list[pd.DataFrame]] = []
    for r, (day, fare, scenario) in enumerate(runs):
        legs = pd.read_parquet(travel_times.routes_path(scenario, day, fare))
        legs = legs.assign(origin=rows_of(legs["station"]), dest=rows_of(legs["to_station"]), run=r)
        walk = legs["pattern"] == travel_times.WALK
        legs["a"] = np.where(walk, [index.get(str(s), 0) for s in legs["from"]], legs["board"])
        legs["b"] = np.where(walk, [index.get(str(s), 0) for s in legs["to"]], legs["alight"])
        legs["p"] = np.where(walk, WALK_LEG, legs["pattern"])
        legs_of.append([g for _, g in legs.groupby("origin")])
        sizes = legs.groupby(["origin", "dest"]).size()
        counts[sizes.index.get_level_values(0), r, sizes.index.get_level_values(1)] = (
            sizes.to_numpy()
        )
    by_origin: dict[int, list[pd.DataFrame]] = {}
    for groups in legs_of:
        for g in groups:
            by_origin.setdefault(int(g["origin"].iloc[0]), []).append(g)
    for station, i in index.items():
        legs = (
            pd.concat(by_origin[i]).sort_values(["run", "dest", "leg"])
            if i in by_origin
            else pd.DataFrame({"p": [], "a": [], "b": [], "start": [], "end": []})
        )
        first = legs["leg"].to_numpy() == 0 if len(legs) else np.zeros(0, dtype=bool)
        start = legs["start"].to_numpy(dtype=np.int64)
        end = legs["end"].to_numpy(dtype=np.int64)
        before = np.concatenate([[0], end[:-1]])
        fields = np.column_stack(
            [
                legs[["p", "a", "b"]].to_numpy(dtype=np.int64),
                np.where(first, start, start - before),
                end - start,
            ]
        ).astype("<u2")
        (routes_dir / f"{station}.bin").write_bytes(counts[i].tobytes() + fields.tobytes())

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
        (arrivals_dir / f"{station}.bin").write_bytes(
            counts[i].tobytes()
            + _deltas(minute, first).astype(np.uint8).tobytes()
            + (_deltas(clock[i][changed[i]], first) % 0x10000).astype("<u2").tobytes(),
        )
