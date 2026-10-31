"""Travel times as the web page loads them."""

import datetime
import itertools
import json
import shutil

import numpy as np
import pandas as pd

from subway_sheds import gtfs, lines, paths, travel_times
from subway_sheds.grid import SPACING, Grid
from subway_sheds.scenarios import Day, Scenario

# In a station's arrivals, a station not reached by train within `travel_times.MAX_TIME`.
UNREACHED = 0xFFFF


def write(grid: Grid) -> None:
    """
    Write the grid, the stations, and each station's times to `paths.WEB_DATA`.

    - `grid.json`: the Web Mercator center of cell (0, 0), the spacing,
      and each cell's column and row.
    - `stations.json`: each station in either scenario, and which.
    - `lines.json`: GeoJSON of the lines between stations, each with its color
      and which scenarios run on it.
    - `exits.bin`: every walk out of a station to a cell, to time at the reader's walking speed,
      as four little-endian `uint16` arrays of equal length after their length, a `uint32`:
      the station, as `stations.json` lists them, the cell, as `grid.json` lists them,
      the seconds out through the entrance, and the meters on the streets after it,
      by station.
    - `arrivals/{station}.bin`: when trains from the station reach each station's platforms,
      for each minute of the departure window, in each scenario on each day,
      in seconds from the window's start, `UNREACHED` if not within `travel_times.MAX_TIME`.
      As that only changes when the best train does, only the changes:
      by day, then scenario, in the order `Day` and `Scenario` list them,
      then station, as `stations.json` lists them,
      a `uint8` count of changes for each, then each change's minute, a `uint8`,
      then each change's time, a little-endian `uint16`, all in that order.
      The page adds each departure's walk out to each cell and takes the median, as routing does.
    """
    shutil.rmtree(paths.WEB_DATA, ignore_errors=True)
    arrivals_dir = paths.WEB_DATA / "arrivals"
    arrivals_dir.mkdir(parents=True)

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
                    "scenarios": [],
                },
            )
            scenarios = entry["scenarios"]
            assert isinstance(scenarios, list)
            scenarios.append(str(scenario))
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
                    "properties": {"color": segment.color},
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
        (len(stations), len(Day), len(Scenario), minutes, len(stations)), UNREACHED, dtype="<u2"
    )
    for (d, day), (s, scenario) in itertools.product(enumerate(Day), enumerate(Scenario)):
        arrivals = pd.read_parquet(travel_times.arrivals_path(scenario, day))
        seconds[
            rows_of(arrivals["station"]),
            d,
            s,
            arrivals["minute"].to_numpy(),
            rows_of(arrivals["to_station"]),
        ] = arrivals["seconds"].to_numpy()
    # When, from the window's start, rather than how long after each departure.
    reached = seconds != UNREACHED
    offsets = (np.arange(minutes, dtype=np.uint32) * 60)[:, np.newaxis]
    clock = np.where(reached, seconds.astype(np.uint32) + offsets, UNREACHED).astype("<u2")
    del seconds, reached
    # Station-major, so that each station's minutes are together.
    clock = np.ascontiguousarray(clock.swapaxes(3, 4))
    changed = np.ones_like(clock, dtype=bool)
    changed[..., 1:] = clock[..., 1:] != clock[..., :-1]
    counts = changed.sum(axis=-1, dtype=np.uint8)
    minute_of = np.broadcast_to(np.arange(minutes, dtype=np.uint8), clock.shape[1:])
    for station, i in index.items():
        (arrivals_dir / f"{station}.bin").write_bytes(
            counts[i].tobytes() + minute_of[changed[i]].tobytes() + clock[i][changed[i]].tobytes()
        )
