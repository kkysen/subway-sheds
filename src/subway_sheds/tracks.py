"""
Which OSM track ways each route runs along between each two stations it stops at in a row,
from OSM's NYC Subway route relations, each a direction and service pattern,
listing its stops in order and the ways it runs along.

Two routes' trains between the same two stations run on the same track
if their ways there overlap: the F and M on 6 Av's local track and the B and D on its express,
from 47-50 Sts to 42 St, share none.
"""

import csv
import json
import math
from collections import defaultdict
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from subway_sheds import gtfs, lines, paths
from subway_sheds.timetable import Timetable

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Every NYC Subway route relation, the ways it runs along, and its nodes, stops among them.
OVERPASS_QUERY = """[out:json][timeout:120];
rel["type"="route"]["route"="subway"]["network"~"^NYC"](40.49,-74.27,40.92,-73.68)->.r;
.r out body;
way(r.r);
out skel;
node(r.r);
out skel;"""
DOWNLOAD = paths.CACHE / "tracks" / "relations.json"
# OSM's shuttles are all `S`, told apart by name, as GTFS's routes.
SHUTTLES = {"Rockaway Park": "H", "Franklin Avenue": "FS", "42nd Street": "GS"}
# A relation's stop farther than this (m) from every station its route stops at
# is one the feed doesn't have, and is skipped.
MAX_MATCH = 300


def _download(dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": "subway-sheds (https://github.com/kkysen/subway-sheds)"}
        with httpx.Client(timeout=180, headers=headers) as client:
            response = client.post(OVERPASS_URL, data={"data": OVERPASS_QUERY})
            response.raise_for_status()
            dest.write_bytes(response.content)
    return dest


def build(feed: gtfs.Feed, osm: dict[str, Any]) -> dict[tuple[str, str, str], set[int]]:
    """Each `(route, station, next station)`'s ways, for `feed`'s routes, from `osm`."""
    elements = osm["elements"]
    ways = {e["id"]: set(e["nodes"]) for e in elements if e["type"] == "way"}
    nodes = {e["id"]: lines.to_meters(e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
    parents = gtfs.parents(feed)
    where = {
        s["stop_id"]: lines.to_meters(float(s["stop_lon"]), float(s["stop_lat"]))
        for s in feed.rows["stops.txt"]
    }
    served: dict[str, set[str]] = defaultdict(set)
    for trip in Timetable(feed).trips.values():
        served[trip.route] |= {parents[c.stop] for c in trip.calls}

    def station(route: str, node: int) -> str | None:
        x, y = nodes[node]
        best = min(served[route], key=lambda s: math.dist(where[s], (x, y)), default=None)
        if best is None or math.dist(where[best], (x, y)) > MAX_MATCH:
            return None
        return best

    out: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    for rel in (e for e in elements if e["type"] == "relation"):
        route = rel["tags"].get("ref", "")
        if route == "S":
            name = rel["tags"].get("name", "")
            route = next((r for part, r in SHUTTLES.items() if part in name), route)
        members = rel["members"]
        track = [
            m["ref"] for m in members if m["type"] == "way" and m["role"] == "" and m["ref"] in ways
        ]
        # Each stop's station, and the first of `track` it's on.
        stops = []
        for m in members:
            if m["type"] != "node" or not m["role"].startswith("stop") or m["ref"] not in nodes:
                continue
            on = next((i for i, w in enumerate(track) if m["ref"] in ways[w]), None)
            stops.append((station(route, m["ref"]), on))
        for (a, i), (b, j) in pairwise(stops):
            if a and b and a != b and i is not None and j is not None and i <= j:
                out[route, a, b] |= set(track[i : j + 1])
    return out


def write(feed: gtfs.Feed) -> int:
    """Write `build`'s ways to `paths.TRACKS`, how many stretches."""
    out = build(feed, json.loads(_download(DOWNLOAD).read_text()))
    paths.TRACKS.parent.mkdir(parents=True, exist_ok=True)
    with paths.TRACKS.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["route", "from_station", "to_station", "ways"])
        for (route, a, b), found in sorted(out.items()):
            w.writerow([route, a, b, " ".join(map(str, sorted(found)))])
    return len(out)


def read() -> dict[tuple[str, str, str], set[int]]:
    """`write`'s ways, by `(route, station, next station)`."""
    with paths.TRACKS.open() as f:
        return {
            (r["route"], r["from_station"], r["to_station"]): {int(w) for w in r["ways"].split()}
            for r in csv.DictReader(f)
        }
