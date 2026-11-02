"""Track from OSM: a graph of it, the path between two stations, and its curves."""

import heapq
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import numpy as np
from scipy.interpolate import BSpline, make_smoothing_spline

# Meters east and north of a point in Queens, near enough for the few kilometers measured here.
LON0 = -73.86
LAT0 = 40.70
KX = math.cos(math.radians(LAT0)) * 111320
KY = 110574

# Points along each way at most this far apart, so stations find a point near them,
# and way ends can join anywhere along another way.
STEP = 5.0
# Ways that end on another without sharing a node, as OSM sometimes has them, join within this.
SNAP = 3.0
# OSM maps no track for 439 m of the old Rockaway Beach Branch through Forest Park,
# between these two ends and the points before and after them, as (lon, lat).
FOREST_PARK = (
    (-73.8524529, 40.7023956),
    (-73.8519163, 40.7017494),
    (-73.8492044, 40.6983605),
    (-73.8490971, 40.6982283),
)

type Point = tuple[float, float]


class Way(TypedDict):
    """An OSM way: its id, the tags kept, and its nodes as [lon, lat]."""

    id: int
    tags: dict[str, str]
    geometry: list[list[float]]


def to_m(lon: float, lat: float) -> Point:
    return ((lon - LON0) * KX, (lat - LAT0) * KY)


def load(path: Path) -> list[Way]:
    """The OSM ways in `path`."""
    return json.loads(path.read_text())["elements"]


def is_track(way: Way) -> bool:
    """
    Subway track but yards, and the old branch however it's tagged,
    with junctions and crossovers.
    """
    tags = way["tags"]
    return (
        (tags.get("railway") == "subway" and tags.get("service") != "yard")
        or tags.get("railway") in ("disused", "abandoned")
        or "disused:railway" in tags
        or "abandoned:railway" in tags
    )


def hermite(p0: np.ndarray, t0: np.ndarray, p1: np.ndarray, t1: np.ndarray, n: int) -> np.ndarray:
    """A cubic from `p0` heading `t0` to `p1` heading `t1`, in `n` steps."""
    length = np.linalg.norm(p1 - p0)
    u = np.linspace(0, 1, n + 1)[:, None]
    return (
        (2 * u**3 - 3 * u**2 + 1) * p0
        + (u**3 - 2 * u**2 + u) * length * t0
        + (-2 * u**3 + 3 * u**2) * p1
        + (u**3 - u**2) * length * t1
    )


class Graph:
    """Track as points at most `STEP` apart, joined along ways and where ways meet."""

    def __init__(self, ways: list[Way]) -> None:
        self.pos: dict[Point, Point] = {}
        self.edges: dict[Point, list[tuple[Point, float]]] = {}
        ends: list[Point] = []
        for way in ways:
            points = [to_m(lon, lat) for lon, lat in way["geometry"]]
            ends += self._add_line(np.array(points))
        before, a, b, after = (np.array(to_m(*p)) for p in FOREST_PARK)
        # Bridged with a cubic matching both ends' headings, so it adds no curvature but theirs.
        ta = (a - before) / np.linalg.norm(a - before)
        tb = (after - b) / np.linalg.norm(after - b)
        ends += self._add_line(hermite(a, ta, b, tb, math.ceil(np.linalg.norm(b - a) / STEP)))
        self._snap(ends)

    @staticmethod
    def _key(p: Point) -> Point:
        return (round(p[0], 2), round(p[1], 2))

    def _link(self, a: Point, b: Point) -> None:
        d = math.dist(self.pos[a], self.pos[b])
        self.edges.setdefault(a, []).append((b, d))
        self.edges.setdefault(b, []).append((a, d))

    def _add_line(self, points: np.ndarray) -> list[Point]:
        dense = [points[0]]
        for a, b in zip(points, points[1:], strict=False):
            n = max(1, math.ceil(np.linalg.norm(b - a) / STEP))
            dense += [a + (b - a) * i / n for i in range(1, n + 1)]
        keys = [self._key((float(p[0]), float(p[1]))) for p in dense]
        for k, p in zip(keys, dense, strict=True):
            self.pos.setdefault(k, (float(p[0]), float(p[1])))
        for a, b in zip(keys, keys[1:], strict=False):
            if a != b:
                self._link(a, b)
        return [keys[0], keys[-1]]

    def _snap(self, ends: list[Point]) -> None:
        cells: dict[tuple[int, int], list[Point]] = {}
        for k, p in self.pos.items():
            cells.setdefault((int(p[0] // SNAP), int(p[1] // SNAP)), []).append(k)
        for e in ends:
            cx, cy = int(self.pos[e][0] // SNAP), int(self.pos[e][1] // SNAP)
            linked = {v for v, _ in self.edges[e]}
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for k in cells.get((cx + dx, cy + dy), []):
                        if (
                            k != e
                            and k not in linked
                            and math.dist(self.pos[k], self.pos[e]) < SNAP
                        ):
                            self._link(e, k)
                            linked.add(k)

    def near(self, at: Point, within: float = 30.0) -> set[Point]:
        return {k for k, p in self.pos.items() if math.dist(p, at) < within}

    def path(self, a: Point, b: Point) -> np.ndarray:
        """
        The shortest track from any point near `a` to any near `b`,
        since a station's nearest point can be on another track than the one that reaches the next.
        """
        sources, targets = self.near(a), self.near(b)
        dist = dict.fromkeys(sources, 0.0)
        prev: dict[Point, Point] = {}
        heap = [(0.0, s) for s in sources]
        heapq.heapify(heap)
        end = None
        while heap:
            d, u = heapq.heappop(heap)
            if u in targets:
                end = u
                break
            if d > dist[u]:
                continue
            for v, w in self.edges[u]:
                if d + w < dist.get(v, math.inf):
                    dist[v], prev[v] = d + w, u
                    heapq.heappush(heap, (d + w, v))
        if end is None:
            raise LookupError(f"no track from {a} to {b}")
        out = [end]
        while out[-1] not in sources:
            out.append(prev[out[-1]])
        return np.array([self.pos[k] for k in reversed(out)])


def radii(points: np.ndarray, noise: float) -> np.ndarray:
    """
    The radius (m) every meter along `points`, from smoothing splines through them,
    smoothed just until the fit is `noise` m RMS from them,
    so that their jitter doesn't read as curvature and real curves aren't smoothed away.
    """
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(points, axis=0).T))]
    keep = np.r_[True, np.diff(s) > 1e-6]
    points, s = points[keep], s[keep]
    t = np.arange(0, s[-1], 1.0)
    # Every meter first, so the splines see the straight chords between OSM's nodes.
    dense = np.c_[np.interp(t, s, points[:, 0]), np.interp(t, s, points[:, 1])]
    return spline_radii(*smooth(t, dense, lambda fit: rms(fit - dense) >= noise), t)


def rms(offsets: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.hypot(*offsets.T) ** 2)))


def smooth(
    s: np.ndarray, points: np.ndarray, enough: Callable[[np.ndarray], bool]
) -> tuple[BSpline, BSpline]:
    """
    Smoothing splines of `points`' x and y along `s`, smoothed the least that's `enough`,
    given their fit at `s`; straight track never is, so it's smoothed the most.
    """
    lams = np.logspace(0, 9, 91)
    for lam in lams:
        sx = make_smoothing_spline(s, points[:, 0], lam=lam)
        sy = make_smoothing_spline(s, points[:, 1], lam=lam)
        if enough(np.c_[sx(s), sy(s)]):
            return sx, sy
    return (
        make_smoothing_spline(s, points[:, 0], lam=lams[-1]),
        make_smoothing_spline(s, points[:, 1], lam=lams[-1]),
    )


def spline_radii(sx: BSpline, sy: BSpline, t: np.ndarray) -> np.ndarray:
    """The radius (m) of the curve (`sx`, `sy`) at each of `t`."""
    dx, dy = sx.derivative(1)(t), sy.derivative(1)(t)
    ddx, ddy = sx.derivative(2)(t), sy.derivative(2)(t)
    k = (dx * ddy - dy * ddx) / (dx * dx + dy * dy) ** 1.5
    return 1 / np.maximum(np.abs(k), 1e-9)
