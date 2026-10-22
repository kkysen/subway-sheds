"""Travel times as the web page loads them."""

import itertools
import json
import shutil

import numpy as np
import pandas as pd

from subway_sheds import gtfs, lines, paths, travel_times
from subway_sheds.grid import SPACING, Grid
from subway_sheds.scenarios import Day, Scenario

# In a station's times, a cell not reached within `travel_times.MAX_TIME`.
UNREACHED = 255


def write(grid: Grid) -> None:
    """
    Write the grid, the stations, and each station's times to `paths.WEB_DATA`.

    - `grid.json`: the Web Mercator center of cell (0, 0), the spacing,
      and each cell's column and row.
    - `stations.json`: each station in either scenario, and which.
    - `lines.json`: GeoJSON of the lines between stations, each with its color
      and which scenarios run on it.
    - `times/{station}.bin`: minutes from the station to each cell, one byte each,
      every cell's in each scenario on each day, by day and then by scenario,
      in the order `Day` and `Scenario` list them.
    """
    shutil.rmtree(paths.WEB_DATA, ignore_errors=True)
    times_dir = paths.WEB_DATA / "times"
    times_dir.mkdir(parents=True)

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
        for segment in lines.segments(gtfs.read_feed(scenario.gtfs)):
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

    cell_count = len(grid.cols)
    minutes = np.full(
        (len(stations), len(Day), len(Scenario), cell_count), UNREACHED, dtype=np.uint8
    )
    index = {station: i for i, station in enumerate(stations)}
    for (d, day), (s, scenario) in itertools.product(enumerate(Day), enumerate(Scenario)):
        times = pd.read_parquet(travel_times.output_path(scenario, day))
        rows = times["station"].astype(str).map(index).to_numpy()
        minutes[rows, d, s, times["cell"].to_numpy()] = times["minutes"].to_numpy()
    for station, i in index.items():
        (times_dir / f"{station}.bin").write_bytes(minutes[i].tobytes())
