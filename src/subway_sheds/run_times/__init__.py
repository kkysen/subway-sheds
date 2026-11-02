"""
Scheduled running times for the QueensLink M's new track, from its curves and how trains run,
calibrated against MTA's schedule on the Queens Blvd local, which runs CBTC as the new track would.

A stretch's scheduled time is its dwell plus its run, padded as the local's runs are.
"""

import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from subway_sheds import gtfs
from subway_sheds.run_times import diagram, train
from subway_sheds.run_times.track import Graph, is_track, load, radii, to_m

DWELL = 30
# Weekday trips leaving their first stop in these hours,
# whose times the schedule's are the median of.
HOURS = (6, 20)
# OSM's track is smoothed until it's this far (m RMS) from OSM's points;
# the times are the same from 0.5 m to 2 m.
OSM_NOISE = 1.0

# QueensLink's new stations, where its feed puts them, as (lon, lat).
NEW_STATIONS = {
    "V01": (-73.857155, 40.710522),
    "V02": (-73.846748, 40.694237),
    "V03": (-73.844543, 40.689545),
    "V04": (-73.840736, 40.682060),
}


@dataclass(frozen=True)
class Line:
    """Consecutive stations of `routes` among `stations`, run under CBTC as the new track would."""

    name: str
    routes: frozenset[str]
    stations: frozenset[str]
    calibrates: bool


LINES = [
    # Queens Blvd's local, Queens Plaza to Forest Hills.
    Line(
        "Queens Blvd local",
        frozenset({"R", "M"}),
        frozenset(f"G{i:02d}" for i in range(8, 22)),
        calibrates=True,
    ),
    # Queens Blvd past Forest Hills, to Jamaica, and the Culver line, Church Av to W 8 St,
    # to compare but not to calibrate: some of their stretches are scheduled
    # far beyond any run, like 4:00 for 600 m into Kings Hwy and Parsons Blvd, as holds.
    Line(
        "Queens Blvd past Forest Hills",
        frozenset({"E", "F"}),
        frozenset({"G08", "F07", "F06", "F05", "F04", "F03", "F02", "F01", "G07", "G06", "G05"}),
        calibrates=False,
    ),
    Line(
        "Culver",
        frozenset({"F"}),
        frozenset({f"F{i}" for i in range(27, 40)} | {"D42"}),
        calibrates=False,
    ),
    # Queens Blvd's express, Queens Plaza to Forest Hills, to compare but not to calibrate:
    # it's slowed by speed limits the track doesn't show
    # (a slow switch out of Queens Plaza, wheel wear), which would count as padding.
    Line(
        "Queens Blvd express",
        frozenset({"E", "F"}),
        frozenset({"G21", "G14", "G08"}),
        calibrates=False,
    ),
]

# The new track's stretches in OSM; 63 Dr to Metropolitan Av-Parkside is the diagram's.
BRANCH = [
    ("V01", "V02"),
    ("V02", "V01"),
    ("V02", "V03"),
    ("V03", "V02"),
    ("V03", "V04"),
    ("V04", "V03"),
    # Toward Rockaway, Aqueduct Racetrack has no platform, so the M runs on to Aqueduct-N Conduit.
    ("V04", "H02"),
    ("H01", "V04"),
    # Not new, but no train's run on it since the G stopped running past Court Sq in 2010:
    # the G's track between Court Sq and Queens Plaza's local platforms, which it'd run on again.
    ("G22", "G21"),
    ("G21", "G22"),
]
DR_63 = "G10"
METROPOLITAN_AV = "V01"


@dataclass(frozen=True)
class Stretch:
    from_stop: str
    to_stop: str
    track_m: int
    run_s: float


def _stations(mta: gtfs.Feed) -> dict[str, tuple[float, float]]:
    out = {
        s["stop_id"]: (float(s["stop_lon"]), float(s["stop_lat"])) for s in mta.rows["stops.txt"]
    }
    return out | NEW_STATIONS


def _scheduled(mta: gtfs.Feed, line: Line) -> dict[tuple[str, str], float]:
    """
    The median weekday time from departing one of `line`'s stations to departing the next,
    on its routes, but for each trip's last stretch, slowed by congestion at its terminal.
    """
    trips = {
        t["trip_id"]
        for t in mta.rows["trips.txt"]
        if t["route_id"] in line.routes and "Weekday" in t["service_id"]
    }
    stops: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
    for st in mta.rows["stop_times.txt"]:
        if st["trip_id"] in trips:
            h, m, s = map(int, st["departure_time"].split(":"))
            stops[st["trip_id"]].append(
                (int(st["stop_sequence"]), st["stop_id"][:3], h * 3600 + m * 60 + s)
            )
    times: dict[tuple[str, str], list[int]] = defaultdict(list)
    for trip in stops.values():
        trip.sort()
        if not HOURS[0] * 3600 <= trip[0][2] < HOURS[1] * 3600:
            continue
        for (_, a, ta), (_, b, tb) in list(zip(trip, trip[1:], strict=False))[:-1]:
            if a in line.stations and b in line.stations:
                times[(a, b)].append(tb - ta)
    return {pair: statistics.median(t) for pair, t in sorted(times.items())}


def _osm_stretch(graph: Graph, stations: dict[str, tuple[float, float]], a: str, b: str) -> Stretch:
    points = graph.path(to_m(*stations[a]), to_m(*stations[b]))
    r = radii(points, OSM_NOISE)
    return Stretch(a, b, len(r), train.run_time(train.head_limits(train.curve_limits(r))))


type Row = dict[str, str | int]


def compute(mta: gtfs.Feed, osm: Path, diagram_image: Path) -> tuple[list[Row], list[Row]]:
    """The new stretches' scheduled times, and the calibration they're padded by, as CSV rows."""
    ways = load(osm)
    graph = Graph([w for w in ways if is_track(w)])
    stations = _stations(mta)

    calibration = [
        (line, _osm_stretch(graph, stations, a, b), sched)
        for line in LINES
        for (a, b), sched in _scheduled(mta, line).items()
    ]
    # The padding, as a factor on the run,
    # that best fits the calibrating lines' schedule with `DWELL`, by least squares.
    runs = np.array([s.run_s for line, s, _ in calibration if line.calibrates])
    over = np.array([sched - DWELL for line, _, sched in calibration if line.calibrates])
    padding = float((runs * over).sum() / (runs * runs).sum())

    def scheduled(run_s: float) -> int:
        return round(DWELL + padding * run_s)

    calibration_rows = [
        {
            "from_stop_id": s.from_stop,
            "to_stop_id": s.to_stop,
            "track_m": s.track_m,
            "run_s": round(s.run_s),
            "mta_scheduled_s": round(sched),
            "model_scheduled_s": scheduled(s.run_s),
            "line": line.name,
            "calibrates": "yes" if line.calibrates else "no",
            "padding": f"{padding:.3f}",
        }
        for line, s, sched in calibration
    ]

    curves = diagram.radii(diagram_image, ways, NEW_STATIONS[METROPOLITAN_AV])
    stretches = []
    for name, (a, b) in [("eb", (DR_63, METROPOLITAN_AV)), ("wb", (METROPOLITAN_AV, DR_63))]:
        limits = train.curve_limits(curves[name])
        grade = diagram.grades(len(limits))
        if name == "wb":
            limits, grade = limits[::-1], -grade[::-1]
        stretches.append(
            Stretch(a, b, len(limits), train.run_time(train.head_limits(limits), grade=grade))
        )
    stretches += [_osm_stretch(graph, stations, a, b) for a, b in BRANCH]
    rows = [
        {
            "from_stop_id": s.from_stop,
            "to_stop_id": s.to_stop,
            "track_m": s.track_m,
            "run_s": round(s.run_s),
            "scheduled_s": scheduled(s.run_s),
        }
        for s in stretches
    ]
    return rows, calibration_rows


def write_csv(rows: list[Row], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
