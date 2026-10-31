"""The subway's lines as the web page draws them, between the stations trains run between."""

import heapq
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path

import shapely
from pyproj import Transformer
from shapely import LineString, Point
from shapely.ops import substring

from subway_sheds import _route, gtfs

# How near, in meters, a shape has to pass both of a pair's stations to draw the line between them.
# MTA's feed leaves many trips without a shape (every B, most Fs and Qs),
# but they run on track some other route's shape follows.
SHAPE_REACH = 100

# A pair of stations is left out if a path through others, at most this much longer, joins them:
# an express run alongside a local's stops would otherwise be drawn over it, cutting corners
# wherever it's drawn straight.
DETOUR = 1.2

_to_meters = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
_to_degrees = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)


@dataclass(frozen=True)
class Segment:
    # The two parent stations, the smaller ID first.
    stations: tuple[str, str]
    color: str
    # In WGS84, from the first station to the second.
    coordinates: list[tuple[float, float]]


def _point(lon: float, lat: float) -> Point:
    return Point(*_to_meters.transform(lon, lat))


def _between(line: LineString, a: Point, b: Point) -> LineString | None:
    """The piece of `line` from nearest `a` to nearest `b`, if it passes near both."""
    if line.distance(a) > SHAPE_REACH or line.distance(b) > SHAPE_REACH:
        return None
    start = line.project(a)
    end = line.project(b)
    piece = substring(line, start, end)
    # A shape that passes near both stations but not along the way between them, e.g. a loop.
    if not isinstance(piece, LineString) or piece.length > 2 * a.distance(b) + 2 * SHAPE_REACH:
        return None
    return piece


def _is_shortcut(
    pair: tuple[str, str], neighbors: dict[str, dict[str, float]], length: float
) -> bool:
    """Whether a path through other stations joins `pair` within `DETOUR` of its length."""
    a, b = pair
    limit = DETOUR * length
    distances = {a: 0.0}
    queue = [(0.0, a)]
    while queue:
        distance, station = heapq.heappop(queue)
        if station == b:
            return True
        if distance > distances.get(station, math.inf):
            continue
        for neighbor, step in neighbors[station].items():
            if {station, neighbor} == {a, b}:
                continue
            through = distance + step
            if through <= limit and through < distances.get(neighbor, math.inf):
                distances[neighbor] = through
                heapq.heappush(queue, (through, neighbor))
    return False


def segments(feed_path: Path) -> list[Segment]:
    """Each pair of stations trains run between without stopping, but for shortcuts."""
    with zipfile.ZipFile(feed_path) as feed:
        stops = list(gtfs.read_csv(feed, "stops.txt"))
        route_rows = list(gtfs.read_csv(feed, "routes.txt"))
        shape_rows = list(gtfs.read_csv(feed, "shapes.txt"))
    points = {
        s["stop_id"]: _point(float(s["stop_lon"]), float(s["stop_lat"]))
        for s in stops
        if not s["parent_station"]
    }
    routes = {
        route["route_id"]: (i, f"#{route['route_color']}") for i, route in enumerate(route_rows)
    }

    shape_points: dict[str, list[tuple[int, float, float]]] = {}
    for row in shape_rows:
        shape_points.setdefault(row["shape_id"], []).append(
            (int(row["shape_pt_sequence"]), float(row["shape_pt_lon"]), float(row["shape_pt_lat"]))
        )
    shapes = {
        shape: LineString([_to_meters.transform(lon, lat) for _, lon, lat in sorted(rows)])
        for shape, rows in shape_points.items()
        if len(rows) > 1
    }

    # Each pair's color, as the first route in `routes.txt` running between them, and its shapes,
    # from `stop_times.txt`, read in Rust (`route/src/feed.rs`), as reading it in Python is slow.
    colors: dict[tuple[str, str], tuple[int, str]] = {}
    own_shapes: dict[tuple[str, str], set[str]] = {}
    for a, b, pair_routes, pair_shapes in _route.station_pairs(feed_path):
        colors[a, b] = min(routes[r] for r in pair_routes)
        own_shapes[a, b] = {s for s in pair_shapes if s in shapes}

    neighbors: dict[str, dict[str, float]] = {}
    for a, b in colors:
        length = points[a].distance(points[b])
        neighbors.setdefault(a, {})[b] = length
        neighbors.setdefault(b, {})[a] = length

    tree = shapely.STRtree(list(shapes.values()))
    shape_ids = list(shapes)
    result: list[Segment] = []
    for pair, (_, color) in colors.items():
        a, b = (points[station] for station in pair)
        if _is_shortcut(pair, neighbors, a.distance(b)):
            continue
        # Its own trips' shapes first, then any passing near both stations, the nearest first.
        nearby = [shape_ids[i] for i in tree.query(a, predicate="dwithin", distance=SHAPE_REACH)]
        candidates = sorted(own_shapes.get(pair, set())) + sorted(
            nearby, key=lambda shape: shapes[shape].distance(a) + shapes[shape].distance(b)
        )
        line = next(
            (piece for shape in candidates if (piece := _between(shapes[shape], a, b))),
            LineString([a, b]),
        )
        # About 2 m, well under a pixel at the zooms the page is looked at.
        line = line.simplify(2)
        result.append(
            Segment(
                pair,
                color,
                [
                    (round(lon, 5), round(lat, 5))
                    for lon, lat in (_to_degrees.transform(x, y) for x, y in line.coords)
                ],
            )
        )
    return result
