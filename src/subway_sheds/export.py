"""Travel times as the web page loads them."""

import json
import shutil

import numpy as np
import pandas as pd

from subway_sheds import gtfs, paths, travel_times
from subway_sheds.grid import SPACING, Grid
from subway_sheds.scenarios import Scenario

# In a station's times, a cell not reached within `travel_times.MAX_TIME`.
UNREACHED = 255


def write(grid: Grid) -> None:
    """
    Write the grid, the stations, and each station's times to `paths.WEB_DATA`.

    - `grid.json`: the Web Mercator center of cell (0, 0), the spacing,
      and each cell's column and row.
    - `stations.json`: each station in either scenario, and which.
    - `times/{station}.bin`: minutes from the station to each cell, one byte each,
      every cell's in the baseline and then every cell's with QueensLink.
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

    cell_count = len(grid.cols)
    minutes = np.full((len(stations), len(Scenario), cell_count), UNREACHED, dtype=np.uint8)
    index = {station: i for i, station in enumerate(stations)}
    for s, scenario in enumerate(Scenario):
        times = pd.read_parquet(travel_times.output_path(scenario))
        rows = times["station"].astype(str).map(index).to_numpy()
        minutes[rows, s, times["cell"].to_numpy()] = times["minutes"].to_numpy()
    for station, i in index.items():
        (times_dir / f"{station}.bin").write_bytes(minutes[i].tobytes())
