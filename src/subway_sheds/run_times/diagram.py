"""
The QueensLink M's two tracks from 63 Dr to Metropolitan Av-Parkside,
traced from QueensLink's diagram of where they leave the Queens Blvd local
(`sources.QUEENSLINK_DIAGRAM`).

1. The diagram is georeferenced by an affine fit
   from street intersections in it to the same ones in OSM.
2. Each orange track's centerline is traced.
3. Each track is spliced together:
   OSM's local track from 63 Dr's platform center to the diagram's edge,
   the trace, and OSM's Rockaway Beach Branch to Metropolitan Av-Parkside,
   each shifted onto the end of the one before, so the joins add no curvature.
"""

from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Point

from subway_sheds.run_times.track import Way, hermite, rms, smooth, spline_radii, to_m

STREET_RGB = [204, 204, 203]
TRACK_RGB = [244, 132, 55]

# Pixel boxes (x0, x1, y0, y1) that select each straight street's pixels in the diagram,
# and nothing else.
BOXES = {
    "64th Road": [(122, 158, 180, 455)],
    "65th Road": [(358, 398, 185, 485)],
    "66th Avenue": [(595, 632, 150, 515)],
    "67th Avenue": [(835, 866, 150, 440)],
    "67th Road": [(952, 988, 240, 515)],
    "Saunders Street": [(0, 120, 215, 247), (330, 840, 215, 247)],
    "Wetherole Street": [(0, 120, 403, 433), (340, 840, 403, 433)],
    "Booth Street": [(0, 120, 308, 340), (320, 580, 308, 340)],
    "Burns Street": [(660, 840, 670, 700), (860, 895, 670, 700)],
    "Clyde Street": [(755, 840, 762, 795), (860, 895, 762, 795)],
}
VERTICAL = {"64th Road", "65th Road", "66th Avenue", "67th Avenue", "67th Road"}
# Thornton Place runs diagonally, near the line between these pixels.
THORNTON = (np.array([650.0, 690.0]), np.array([1000.0, 1060.0]))
# The street intersections the fit uses: those that cross within the diagram.
PAIRS = [
    (v, h)
    for v in ["64th Road", "65th Road", "66th Avenue", "67th Avenue"]
    for h in ["Saunders Street", "Booth Street", "Wetherole Street"]
] + [("Thornton Place", "Clyde Street")]

# The branch's two tracks south of the diagram, from OSM's disused railway ways.
BRANCH = {
    # The east track, for westbound trains.
    "wb": [291710789, 291710330, 291710329, 291710331, 291710325, 291710326, 291710333],
    # The west track, for eastbound (Rockaway-bound) trains, which keep right heading south.
    "eb": [291710794, 291710337, 291710336, 291710327, 291710328, 291710332, 291710333],
}
PLATFORM_63_DR = "63rd Drive-Rego Park (M,R)"

# Westbound is drawn under the other tracks between these x pixels, so its trace is bridged there.
WB_GAP = (748.0, 789.0)
# Below this y pixel, the two lines touch, 5 px apart center to center,
# so their shared center is offset.
MERGE_Y = 282
HALF_SPACING = 2.5
# The trace's own jitter, in meters:
# its RMS distance from a straight line along 67th Ave.
NOISE = 0.21

type Line = tuple[np.ndarray, np.ndarray]


def fit_line(points: np.ndarray, trim: float | None = None) -> Line:
    """
    The total least squares line through `points`, as (point, unit direction).

    With `trim`, refit without points farther than that from the line,
    like where a label or track crosses a street.
    """
    c = points.mean(0)
    _, _, vt = np.linalg.svd(points - c)
    for _ in range(5 if trim else 0):
        if trim:
            keep = np.abs((points - c) @ np.array([-vt[0][1], vt[0][0]])) < trim
            if keep.all():
                break
            points = points[keep]
            c = points.mean(0)
            _, _, vt = np.linalg.svd(points - c)
    return c, vt[0]


def intersect(l1: Line, l2: Line) -> np.ndarray:
    (p, d), (q, e) = l1, l2
    t = np.linalg.solve(np.c_[d, -e], q - p)
    return p + t[0] * d


def _centers(
    mask: np.ndarray, box: tuple[int, int, int, int], vertical: bool
) -> list[tuple[float, float]]:
    """
    The street's center in each row (vertical streets) or column (horizontal ones)
    crossing it alone.
    """
    x0, x1, y0, y1 = box
    sub = mask[y0:y1, x0:x1]
    out: list[tuple[float, float]] = []
    for i in range(sub.shape[0] if vertical else sub.shape[1]):
        idx = np.nonzero(sub[i] if vertical else sub[:, i])[0]
        if 5 <= len(idx) <= 20 and idx[-1] - idx[0] == len(idx) - 1:
            c = float(idx.mean())
            out.append((c + x0, float(i + y0)) if vertical else (float(i + x0), c + y0))
    return out


def _image_lines(street: np.ndarray) -> dict[str, Line]:
    lines = {}
    for name, boxes in BOXES.items():
        points = np.array(
            [c for box in boxes for c in _centers(street, box, name in VERTICAL)], float
        )
        lines[name] = fit_line(points, trim=1.5)
    p0, p1 = THORNTON
    u = (p1 - p0) / np.linalg.norm(p1 - p0)
    n = np.array([-u[1], u[0]])
    ys, xs = np.nonzero(street)
    pixels = np.c_[xs, ys].astype(float)
    along, off = (pixels - p0) @ u, (pixels - p0) @ n
    sel = (np.abs(off) < 20) & (along > 30) & (along < np.linalg.norm(p1 - p0) - 30)
    points = []
    for k in np.unique(np.round(along[sel])):
        o = off[sel][np.round(along[sel]) == k]
        if 5 <= len(o) <= 20 and o.max() - o.min() < 20:
            points.append(p0 + k * u + o.mean() * n)
    lines["Thornton Place"] = fit_line(np.array(points), trim=1.5)
    return lines


def georeference(image: np.ndarray, ways: list[Way]) -> tuple[np.ndarray, np.ndarray]:
    """
    The affine map from the diagram's pixels to meters (3x2),
    and each intersection's residual (m).
    """
    lines = _image_lines(np.abs(image - STREET_RGB).sum(-1) < 8)
    streets: dict[str, list[np.ndarray]] = {}
    for way in ways:
        name = way["tags"].get("name")
        if name in lines and way["tags"].get("highway"):
            streets.setdefault(name, []).append(
                np.array([to_m(lon, lat) for lon, lat in way["geometry"]])
            )

    def osm_line(name: str, near: np.ndarray | None = None) -> Line:
        points = np.concatenate(streets[name])
        if near is not None:
            points = points[np.hypot(*(points - near).T) < 250]
        return fit_line(points)

    pixels = np.array([intersect(lines[v], lines[h]) for v, h in PAIRS])
    meters = np.array([intersect(osm_line(v), osm_line(h)) for v, h in PAIRS])
    # Refit with only OSM's nodes near each intersection the last fit puts it at.
    for _ in range(3):
        m, *_ = np.linalg.lstsq(np.c_[pixels, np.ones(len(pixels))], meters, rcond=None)
        guesses = np.c_[pixels, np.ones(len(pixels))] @ m
        meters = np.array(
            [
                intersect(osm_line(v, g), osm_line(h, g))
                for (v, h), g in zip(PAIRS, guesses, strict=True)
            ]
        )
    m, *_ = np.linalg.lstsq(np.c_[pixels, np.ones(len(pixels))], meters, rcond=None)
    residuals = np.hypot(*(np.c_[pixels, np.ones(len(pixels))] @ m - meters).T)
    return m, residuals


def march(
    track: np.ndarray,
    start: tuple[float, float],
    direction: tuple[float, float],
    stop: Callable[[np.ndarray], bool],
    half: float = 7,
) -> np.ndarray:
    """
    Follow an orange line in `track` (its mask) from `start` along `direction`,
    each step moving to the middle of the orange pixels across it 2 px ahead.
    Across gaps of up to 60 px, where the line is drawn under others, it keeps straight on.
    """
    h, w = track.shape
    p = np.array(start, float)
    d = np.array(direction, float) / np.linalg.norm(direction)
    points = [p.copy()]
    gap = 0.0
    offsets = np.arange(-half, half + 0.5, 0.5)
    while not stop(p):
        q = p + 2.0 * d
        n = np.array([-d[1], d[0]])
        across = q + offsets[:, None] * n
        xi, yi = np.round(across[:, 0]).astype(int), np.round(across[:, 1]).astype(int)
        inside = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
        hit = np.zeros(len(offsets), bool)
        hit[inside] = track[yi[inside], xi[inside]]
        if hit.any():
            # The run of hits nearest where it's headed, so a neighboring line isn't averaged in.
            idx = np.nonzero(hit)[0]
            runs = np.split(idx, np.nonzero(np.diff(idx) > 2)[0] + 1)
            run = min(runs, key=lambda r: abs(offsets[r].mean()))
            q = q + offsets[run].mean() * n
            gap = 0.0
        else:
            gap += 2.0
            if gap > 60:
                raise RuntimeError(f"lost the line at {q}")
        turned = (q - p) / np.linalg.norm(q - p)
        # Headings change gradually: average with the last one to damp the pixels' noise.
        d = 0.7 * d + 0.3 * turned
        d /= np.linalg.norm(d)
        p = q
        points.append(p.copy())
    return np.array(points)


def _offset(path: np.ndarray, by: float) -> np.ndarray:
    d = np.gradient(path, axis=0)
    d /= np.linalg.norm(d, axis=1)[:, None]
    return path + by * np.c_[-d[:, 1], d[:, 0]]


def trace(image: np.ndarray) -> dict[str, np.ndarray]:
    """Each track's centerline in the diagram's pixels, from its west edge to its bottom."""
    track = np.abs(image - TRACK_RGB).sum(-1) < 40
    h = track.shape[0]

    def bottom(p: np.ndarray) -> bool:
        return p[1] >= h - 3

    # Eastbound: the south line, from the diagram's west edge.
    eb = march(track, (1, 120.5), (1, 0), bottom)
    # Westbound: the north line, drawn under the other tracks where it crosses them,
    # so traced from where it runs alone down 67th Ave, north and south;
    # then west to south, like `eb`.
    north = march(track, (850.5, 245), (0, -1), lambda p: p[0] <= 1)
    south = march(track, (850.5, 245), (0, 1), bottom)
    wb = np.r_[north[::-1], south[1:]]
    # Across where westbound is drawn under the others, a cubic matching the trace's ends.
    west, east = wb[wb[:, 0] <= WB_GAP[0]], wb[(wb[:, 0] >= WB_GAP[1]) | (wb[:, 1] > 130)]

    def heading(points: np.ndarray) -> np.ndarray:
        return (points[-1] - points[0]) / np.linalg.norm(points[-1] - points[0])

    bridge = hermite(west[-1], heading(west[-8:]), east[0], heading(east[:8]), 39)
    wb = np.r_[west, bridge[1:-1], east]
    # Where the lines touch, their shared center, offset to each side:
    # heading south, +normal (y down) points west, to eastbound, the inner track.
    center = march(track, (850.0, MERGE_Y + 2), (0, 1), bottom, half=6)
    return {
        name: np.r_[path[path[:, 1] < MERGE_Y], _offset(center, side * HALF_SPACING)]
        for name, path, side in [("eb", eb, +1), ("wb", wb, -1)]
    }


def _sub_line(line: LineString, a: float, b: float) -> np.ndarray:
    return np.array([line.interpolate(d).coords[0] for d in np.arange(a, b, 1.0)])


def radii(
    diagram: Path, ways: list[Way], metropolitan_av: tuple[float, float]
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """
    Each track's points (m) and radius (m) at them,
    every meter from 63 Dr's platform center to Metropolitan Av-Parkside,
    for "eb" (toward Rockaway) and "wb" (toward 63 Dr, but also measured from 63 Dr).
    """
    image = np.asarray(Image.open(diagram).convert("RGB")).astype(int)
    affine, _ = georeference(image, ways)
    by_id = {w["id"]: w for w in ways}

    def way_m(w: Way) -> np.ndarray:
        return np.array([to_m(lon, lat) for lon, lat in w["geometry"]])

    local = [way_m(w) for w in ways if w["tags"].get("name") == "IND Queens Boulevard Line"]
    platform = np.concatenate([way_m(w) for w in ways if w["tags"].get("name") == PLATFORM_63_DR])
    dr63 = platform.mean(0)
    metro = np.array(to_m(*metropolitan_av))
    out = {}
    paths = {}
    for name, pixels in trace(image).items():
        drawn = np.c_[pixels, np.ones(len(pixels))] @ affine
        # The local track the trace starts on, east from 63 Dr's platform.
        start = Point(*drawn[0])
        line = min((LineString(w) for w in local), key=lambda line: line.distance(start))
        a, b = line.project(Point(*dr63)), line.project(Point(*drawn[0]))
        qbl = _sub_line(line, min(a, b), max(a, b))
        if a > b:
            qbl = qbl[::-1]
        drawn = drawn + (qbl[-1] - drawn[0])
        branch = np.unique(np.concatenate([way_m(by_id[i]) for i in BRANCH[name]]), axis=0)
        branch_line = LineString(branch[np.argsort(-branch[:, 1])])
        rest = _sub_line(
            branch_line,
            branch_line.project(Point(*drawn[-1])),
            branch_line.project(Point(*metro)) + 1,
        )
        rest = rest + (drawn[-1] - rest[0])
        points = np.r_[qbl, drawn[1:], rest[1:]]
        joins = (len(qbl), len(qbl) + len(drawn) - 1)
        paths[name] = points
        out[name] = _smoothed_radii(points, joins)
    # Where westbound is drawn under the other tracks, its curve isn't seen:
    # the radius on either side of the gap, across it, not the bridging cubic's.
    gap = (670, 712)
    wb = out["wb"]
    wb[gap[0] : gap[1]] = np.interp(
        np.arange(*gap), [gap[0] - 1, gap[1]], [wb[gap[0] - 1], wb[gap[1]]]
    )
    # OSM maps the branch as one track for its last stretch here, so eastbound jogs 4 m onto it;
    # a double track wouldn't.
    out["eb"][2450:] = np.inf
    return {name: (paths[name], out[name]) for name in out}


def _smoothed_radii(points: np.ndarray, joins: tuple[int, int]) -> np.ndarray:
    """
    Radii from smoothing splines,
    smoothed until the traced stretch is `NOISE` from them, and no more.
    """
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(points, axis=0).T))]
    keep = np.r_[True, np.diff(s) > 1e-6]
    joins = (int(keep[: joins[0]].sum()), int(keep[: joins[1]].sum()))
    points, s = points[keep], s[keep]
    sx, sy = smooth(
        s, points, lambda fit: rms(fit[joins[0] : joins[1]] - points[joins[0] : joins[1]]) >= NOISE
    )
    return spline_radii(sx, sy, np.arange(0, s[-1], 1.0))


def grades(n: int) -> np.ndarray:
    """
    A rough profile, as rise over run every meter, since the real one isn't known:
    the local 9 m below the street to where the track leaves it (450 m from 63 Dr),
    down to 17 m by where it passes under The Virginia (720 to 800 m),
    deep enough not to disturb it,
    then climbing to the branch at street level by Fleet St (1,450 m),
    and level to Metropolitan Av-Parkside.
    """
    z = np.interp(np.arange(n), [0, 450, 720, 800, 1450, n - 1], [-9, -9, -17, -17, 0, 0])
    return np.gradient(z)
