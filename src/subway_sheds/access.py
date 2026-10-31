"""
How long it takes to get between the street and the platforms through each station entrance:
climbing or riding the station's depth or height, and through the fare gates.

The depth comes from, in order:
the heights of the MTA's elevators and escalators,
depths published elsewhere for the few deep stations the MTA leaves blank (`DEPTHS`),
the levels OpenStreetMap puts the station's platforms on, in feet as the MTA's heights fit them,
and last the station's structure.
"""

import json
import math
import re
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from subway_sheds import paths

DIR = paths.CACHE / "access"
# The MTA's open data: every subway entrance, every elevator and escalator, and every station.
ENTRANCES_URL = "https://data.ny.gov/resource/i9wp-a4ja.csv?$limit=10000"
LIFTS_URL = "https://data.ny.gov/resource/94fv-bak7.csv?$limit=10000"
STATIONS_URL = "https://data.ny.gov/resource/39hk-dx4f.csv?$limit=10000"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Subway platforms and their levels, over the city.
OVERPASS_QUERY = """[out:json][timeout:120];
(nwr["railway"="platform"](40.49,-74.27,40.92,-73.68);
 nwr["public_transport"="platform"]["subway"="yes"](40.49,-74.27,40.92,-73.68););
out tags center;"""

# What routing reads: committed, so that what the MTA or OSM change doesn't change routing
# until it's rebuilt and reviewed.
TABLE = paths.ROOT / "data" / "station_access.csv"

# Stairs, about 1 ft a second up or down, averaged: Fruin's walking speeds on stairs,
# about 0.5 to 0.6 m/s along a 7 in riser's slope.
STAIRS_FT_PER_S = 1.0
# Escalators at the 100 ft a minute that US code allows and NYC's run at, riders standing,
# half of it upwards on a 30 degree incline.
ESCALATOR_FT_PER_S = 100 / 60 / 2
# Elevators: half a minute's wait, as if they came every minute, the doors, and the ride.
ELEVATOR_WAIT_S = 30
ELEVATOR_DOORS_S = 10
ELEVATOR_FT_PER_S = 2.0
# Through the fare gates and across to the stairs, once each way in or out.
GATES_S = 15
# Deeper than this, no one takes stairs: the station's escalators or elevators instead.
DEEP_FT = 60
# The walking speed of the street walks, for the walks inside a station.
WALK_FT_PER_S = 3 * 5280 / 3600

# Depths the MTA's heights leave blank, as published, in feet to the platform.
DEPTHS = {
    # https://en.wikipedia.org/wiki/72nd_Street_station_(Second_Avenue_Subway),
    # https://subwaynut.com/second_ave_subway/72_street/index.php
    "Q03": (99, "about 99 ft: Wikipedia, subwaynut"),
    # The upper level, toward Manhattan, about 140 ft, the lower, toward Queens, about 155:
    # https://en.wikipedia.org/wiki/Lexington_Avenue%E2%80%9363rd_Street_station
    "B08": (148, "140 and 155 ft levels: Wikipedia"),
    # https://en.wikipedia.org/wiki/190th_Street_station,
    # https://untappedcities.com/deepest-highest-subway-stations-nyc
    "A05": (140, "about 140 ft: Wikipedia, Untapped Cities"),
}


@dataclass(frozen=True)
class Walk:
    """An entrance reached by a level walk inside the station before its climb."""

    feet: float
    climb_ft: float
    note: str


# Entrances reached by a long walk inside the station, by the MTA's entrance coordinates.
WALKS = {
    # 191 St's Broadway entrance: OSM's "Tunnel Street" footway, 280 m,
    # to the elevators under St Nicholas Ave,
    # whose top is 66.8 m up by NYC's 2017 LiDAR ground model, to the entrance's 23.7 m,
    # so with the elevators' 173 ft the platform is about 32 ft below the entrance.
    (40.856413, -73.932605): Walk(280 / 0.3048, 32, "Broadway tunnel, 280 m"),
}

# Each structure's depth or height where nothing else gives it.
STRUCTURE_FT = {
    "Subway": 25,
    "Elevated": 30,
    "Viaduct": 30,
    "Embankment": 15,
    "Open Cut": 20,
    "At Grade": 0,
}


def _download(url: str, dest: Path, data: dict[str, str] | None = None) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": "subway-sheds (https://github.com/kkysen/subway-sheds)"}
        with httpx.Client(timeout=180, headers=headers) as client:
            response = client.post(url, data=data) if data else client.get(url)
            response.raise_for_status()
            dest.write_bytes(response.content)
    return dest


def feet(text: object) -> float:
    """A height the MTA gives as `172’9”`, `103'-5in`, or `15824 mm (51'-11in)`, in feet."""
    if not isinstance(text, str):
        return math.nan
    if m := re.search(r"([\d,]+)\s*mm", text):
        return int(m[1].replace(",", "")) / 304.8
    m = re.match(r"\s*(\d+)\s*(?:'|’|ft)?\s*-?\s*(\d+)?", text)
    if not m:
        return math.nan
    return int(m[1]) + int(m[2] or 0) / 12


def lift_depths(lifts: pd.DataFrame) -> pd.DataFrame:
    """
    Each station's depth or height by its elevators' and escalators' heights, by MTA station ID:
    elevators side by side, so the tallest, or the tallest from the street and then to the platform;
    escalators one after another, from the street and then down further, as at Hudson Yards.
    """
    lifts = lifts.assign(
        station_id=lifts["station_mrn"].astype(str).str.lstrip("0"),
        rise=[
            feet(e if isinstance(e, str) else s)
            for e, s in zip(lifts["elevator_rise"], lifts["escalator_rise"], strict=True)
        ],
        street=lifts["street_access"] == "YES",
    )
    rows = []
    for station_id, group in lifts.groupby("station_id"):
        elevators = group[group["elevator_or_escalator"] == "Elevator"]
        escalators = group[group["elevator_or_escalator"] == "Escalator"]

        def chain(lifts: pd.DataFrame) -> float:
            top = lifts[lifts["street"]]["rise"].max()
            rest = lifts[~lifts["street"]]["rise"].max()
            return max(
                lifts["rise"].max(),
                (0 if math.isnan(top) else top) + (0 if math.isnan(rest) else rest),
            )

        rows.append(
            {
                "station_id": station_id,
                "elevator_ft": chain(elevators) if len(elevators) else math.nan,
                "escalator_ft": chain(escalators) if len(escalators) else math.nan,
                "has_escalators": len(escalators) > 0,
            }
        )
    return pd.DataFrame(rows)


def osm_levels(stations: pd.DataFrame, osm: dict[str, Any]) -> pd.Series:
    """The level of the platform OSM puts nearest each station, within 150 m."""
    platforms = []
    for element in osm["elements"]:
        tags = element.get("tags", {})
        center = element.get("center", element)
        if "level" in tags and "lat" in center:
            try:
                level = float(tags["level"].split(";")[0])
            except ValueError:
                continue
            platforms.append((center["lat"], center["lon"], level))

    def nearest(lat: float, lon: float) -> float:
        best = (150.0, math.nan)
        for plat, plon, level in platforms:
            dy = (plat - lat) * 111_111
            dx = (plon - lon) * 111_111 * math.cos(math.radians(lat))
            best = min(best, (math.hypot(dx, dy), level))
        return best[1]

    return pd.Series(
        [
            nearest(lat, lon)
            for lat, lon in zip(stations["gtfs_latitude"], stations["gtfs_longitude"], strict=True)
        ],
        index=stations.index,
    )


def entrance_secs(kind: str, depth_ft: float, has_escalators: bool, walk: Walk | None) -> float:
    """Seconds between the street and the platform through an entrance of `kind`."""
    if walk is not None:
        return walk.feet / WALK_FT_PER_S + walk.climb_ft / STAIRS_FT_PER_S + GATES_S
    if "Elevator" in kind or (depth_ft > DEEP_FT and not has_escalators):
        return ELEVATOR_WAIT_S + ELEVATOR_DOORS_S + depth_ft / ELEVATOR_FT_PER_S + GATES_S
    if "Escalator" in kind or depth_ft > DEEP_FT:
        return depth_ft / ESCALATOR_FT_PER_S + GATES_S
    return depth_ft / STAIRS_FT_PER_S + GATES_S


def build() -> pd.DataFrame:
    """Every entrance's time between the street and its station's platforms."""
    entrances = pd.read_csv(_download(ENTRANCES_URL, DIR / "entrances.csv"), dtype=str)
    lifts = pd.read_csv(_download(LIFTS_URL, DIR / "lifts.csv"), dtype=str)
    stations = pd.read_csv(_download(STATIONS_URL, DIR / "stations.csv"), dtype=str)
    osm = json.loads(
        _download(OVERPASS_URL, DIR / "platforms.json", {"data": OVERPASS_QUERY}).read_text()
    )
    stations[["gtfs_latitude", "gtfs_longitude"]] = stations[
        ["gtfs_latitude", "gtfs_longitude"]
    ].astype(float)
    stations["level"] = osm_levels(stations, osm)
    stations = stations.merge(lift_depths(lifts), on="station_id", how="left")
    stations["mta_ft"] = stations[["elevator_ft", "escalator_ft"]].max(axis=1)
    # Feet a level, as the MTA's heights fit OSM's levels where both have a station.
    both = stations.dropna(subset=["mta_ft", "level"])
    both = both[both["level"] != 0]
    ft_per_level = (both["mta_ft"] / both["level"].abs()).median()
    stations["level_ft"] = stations["level"].abs() * ft_per_level

    def depth(row: pd.Series) -> tuple[float, str]:
        if row["gtfs_stop_id"] in DEPTHS:
            return DEPTHS[row["gtfs_stop_id"]]
        if not math.isnan(row["mta_ft"]):
            return row["mta_ft"], "MTA elevators and escalators"
        if not math.isnan(row["level_ft"]) and row["level"] != 0:
            return row["level_ft"], f"OSM level {row['level']:g}"
        return STRUCTURE_FT.get(row["structure"], 25), f"structure {row['structure']}"

    depths = [depth(r) for _, r in stations.iterrows()]
    stations["depth_ft"] = [d for d, _ in depths]
    stations["depth_source"] = [s for _, s in depths]
    stations["has_escalators"] = stations["has_escalators"].eq(True)
    by_id: dict[str, dict[Hashable, Any]] = {
        str(r["gtfs_stop_id"]): r for r in stations.to_dict("records")
    }

    def row(gtfs_id: str, lat: float, lon: float, kind: str) -> dict[str, Any]:
        station = by_id[gtfs_id]
        walk = WALKS.get((lat, lon))
        depth_ft = float(station["depth_ft"])
        return {
            "station": gtfs_id,
            "complex": station["complex_id"],
            "name": station["stop_name"],
            "lat": lat,
            "lon": lon,
            "kind": kind,
            "depth_ft": round(depth_ft, 1),
            "depth_source": walk.note if walk else station["depth_source"],
            "secs": round(entrance_secs(kind, depth_ft, bool(station["has_escalators"]), walk)),
        }

    rows = [
        row(gtfs_id, float(e["entrance_latitude"]), float(e["entrance_longitude"]), str(kind))
        for e in entrances.to_dict("records")
        if "YES" in (e["entry_allowed"], e["exit_allowed"])
        # An entrance a complex's stations share lists each of them.
        for gtfs_id in str(e["gtfs_stop_id"]).split("; ")
        for kind in [e["entrance_type"]]
    ]
    rows = _drop_misattributed(pd.DataFrame(rows), by_id).to_dict("records")
    # Stations the MTA lists no entrances for, at their own coordinates.
    listed = {r["station"] for r in rows}
    rows += [
        row(gtfs_id, float(s["gtfs_latitude"]), float(s["gtfs_longitude"]), "Station")
        for gtfs_id, s in by_id.items()
        if gtfs_id not in listed
    ]
    return pd.DataFrame(rows)


# How much nearer, as a fraction, another station sharing a street entrance must be
# for it to be only that station's.
MISATTRIBUTED_NEARER = 0.5


def _drop_misattributed(rows: pd.DataFrame, by_id: dict[str, dict[Hashable, Any]]) -> pd.DataFrame:
    """`rows` without street entrances the MTA lists under a station they aren't one of.

    The MTA lists some entrances under stations of different complexes.
    A station house or passage may really join them underground, as at Fulton St,
    but a street entrance listed under both 50 Sts, at Broadway, is only the 1's:
    the C and E's is at 8 Av, and routing changed between them through it in a minute and a half.
    A shared street entrance is dropped from a station it's farther from than any of its own,
    when another station sharing it is at most `MISATTRIBUTED_NEARER` as far.
    """
    lat = rows["station"].map(lambda s: float(by_id[s]["gtfs_latitude"]))
    lon = rows["station"].map(lambda s: float(by_id[s]["gtfs_longitude"]))
    meters_per_degree = 111_320
    rows = rows.assign(
        meters=meters_per_degree
        * ((rows["lat"] - lat) ** 2 + ((rows["lon"] - lon) * math.cos(math.radians(40.7))) ** 2)
        ** 0.5
    )
    at = rows.groupby(["lat", "lon"])
    shared = at["complex"].transform("nunique") > 1
    own_farthest = rows["station"].map(rows[~shared].groupby("station")["meters"].max())
    nearest = at["meters"].transform("min")
    misattributed = (
        shared
        & rows["kind"].eq("Easement - Street")
        & (rows["meters"] > own_farthest)
        & (nearest <= MISATTRIBUTED_NEARER * rows["meters"])
    )
    return rows[~misattributed].drop(columns="meters")


def write() -> pd.DataFrame:
    table = build()
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(TABLE, index=False)
    return table
