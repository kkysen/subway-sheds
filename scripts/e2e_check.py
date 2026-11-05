"""
Checks what `scripts/e2e.sh` ran the pipeline into, in the current directory,
having routed only the stations given:
that the scenarios' feeds are valid GTFS, as routing reads them,
that each station routed reaches stations in every run,
that from QueensLink's Metropolitan Av-Parkside it's quicker to Times Sq with QueensLink,
and that the page has each one's routes and built.
"""

import json
import sys
import zipfile
from pathlib import Path

import pandas as pd

SCENARIOS = ["baseline", "queenslink"]
DAYS = ["weekday", "saturday"]
FARES = ["single", "unlimited"]
# Most stations are within `MAX_TIME` of any, so reaching fewer means routing broke.
LEAST_REACHED = 100
METROPOLITAN_AV, TIMES_SQ = "V01", "127"


def seconds(times: pd.Series) -> pd.Series:
    parts = times.str.split(":", expand=True)
    return parts[0].astype(int) * 3600 + parts[1].astype(int) * 60 + parts[2].astype(int)


def check_feed(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as feed, feed.open("stop_times.txt") as f:
        stop_times = pd.read_csv(f, dtype=str)
    errors = []
    for column in ["arrival_time", "departure_time"]:
        negative = stop_times[column].str.startswith("-")
        if negative.any():
            errors.append(f"{path.name}: {negative.sum()} negative `{column}`s")
    if errors:
        return errors
    stop_times["at"] = seconds(stop_times["departure_time"])
    stop_times["stop_sequence"] = stop_times["stop_sequence"].astype(int)
    stop_times = stop_times.sort_values(["trip_id", "stop_sequence"])
    stops = stop_times.groupby("trip_id").size()
    if (stops < 2).any():
        errors.append(f"{path.name}: {(stops < 2).sum()} trips stopping only once")
    earlier = stop_times.groupby("trip_id")["at"].diff() < 0
    if earlier.any():
        trips = stop_times.loc[earlier, "trip_id"].nunique()
        errors.append(f"{path.name}: {trips} trips leaving a stop before the one before it")
    return errors


def check_routes(stations: list[str]) -> list[str]:
    errors = []
    medians: dict[str, float] = {}
    for scenario in SCENARIOS:
        for day in DAYS:
            for fare in FARES:
                run = f"{scenario}-{day}-{fare}"
                path = Path("out/travel_times") / f"{run}-arrivals.parquet"
                if not path.exists():
                    errors.append(f"no {path}")
                    continue
                arrivals = pd.read_parquet(path)
                reached = arrivals.groupby("station", observed=True)["to_station"].nunique()
                for station in stations:
                    if reached.get(station, 0) < LEAST_REACHED:
                        errors.append(
                            f"{run}: {station} reaches {reached.get(station, 0)} stations,"
                            f" not at least {LEAST_REACHED}"
                        )
                if day == "weekday" and fare == "single":
                    to = arrivals[
                        (arrivals["station"] == METROPOLITAN_AV)
                        & (arrivals["to_station"] == TIMES_SQ)
                    ]
                    if len(to):
                        medians[scenario] = float(to["seconds"].median())
    if {METROPOLITAN_AV, TIMES_SQ} <= set(stations):
        if set(medians) != set(SCENARIOS):
            errors.append(f"{METROPOLITAN_AV} doesn't reach {TIMES_SQ} in every scenario")
        elif not medians["queenslink"] < medians["baseline"]:
            errors.append(
                f"{METROPOLITAN_AV} to {TIMES_SQ} takes {medians['queenslink'] / 60:.0f} min"
                f" with QueensLink, not less than {medians['baseline'] / 60:.0f} min without"
            )
        else:
            print(
                f"{METROPOLITAN_AV} to {TIMES_SQ}: {medians['queenslink'] / 60:.0f} min"
                f" with QueensLink, {medians['baseline'] / 60:.0f} min without",
                file=sys.stderr,
            )
    return errors


def check_page(stations: list[str]) -> list[str]:
    errors = []
    data = Path("web/public/data")
    listed = {s["id"] for s in json.loads((data / "stations.json").read_text())}
    for station in stations:
        if station not in listed:
            errors.append(f"`stations.json` doesn't list {station}")
        if not (data / "routes" / f"{station}.bin").exists():
            errors.append(f"no routes for {station}")
    if not Path("web/dist/index.html").exists():
        errors.append("the page wasn't built into `web/dist/`")
    return errors


def main() -> None:
    stations = sys.argv[1:]
    errors = [e for name in SCENARIOS for e in check_feed(Path("out/gtfs") / f"{name}.zip")]
    errors += check_routes(stations)
    errors += check_page(stations)
    for error in errors:
        print(error, file=sys.stderr)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
