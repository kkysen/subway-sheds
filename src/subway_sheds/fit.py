"""
Fits QueensLink's M among the trains it shares track with, with CP-SAT (`route/src/sat.rs`),
a two-hour window at a time, keeping each window's first hour.

Each M may run earlier or later as a whole, and wait or make up 30 s at its junctions;
trains of other routes on the tracks it shares may be nudged as a whole.
Every pair of trains near each other on a shared track stays a floor apart at each platform,
or else counts as a conflict, which costs far more than anything else.
"""

import bisect
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations, pairwise

from subway_sheds._route import CpModel
from subway_sheds.timetable import CBTC_FLOOR, FLOOR, Timetable, Trip

# Times are solved in steps of this many seconds.
UNIT = 30
# On the busy locals, at most `ROW` trains leave a platform in any `SPREAD` seconds,
# or as many as MTA's own schedule already does, if more.
SPREAD = 480
ROW = 4
# Each M keeps at least this share of its scheduled gap to the M before it.
KEPT = 0.8
# How far an M runs earlier or later as a whole, waits at a junction, or another train is nudged.
MOST_MOVED = 180
MOST_HELD = 60
MOST_NUDGED = 60
# Each window's solve stops after this many seconds, with the best found.
CAP = 30.0
WORKERS = 8

# Costs, per `UNIT`: an M moved, held, or catching up, another train nudged,
# a pair of trains on a shared track falling short of its even gap,
# an M falling short of its scheduled gap to the M before it,
# a train nudged falling short of MTA's gap to its own route's trains before and after it,
# and a conflict.
MOVE = 1
HOLD = 4
CATCH_UP = 3
NUDGE = 4
UNEVEN = 2
UNEVEN_M = 3
UNEVEN_OWN = 2
CONFLICT = 1000

NUDGEABLE = {"J", "Z", "F", "R", "A", "E"}
# Myrtle Av, Essex St, 21 St-Queensbridge, and Liberty Av.
JUNCTIONS = ["M11", "M18", "B04", "V04"]
ROCKAWAY_PARK = "H15"
# MTA labels the M's platforms along Broadway by its own direction, opposite the J's:
# the J's S is toward Manhattan there, but the M's N is. So compare them as the J's.
BROADWAY = {"M11", "M12", "M13", "M14", "M16", "M18"}
# An M toward Middle Village crosses the Manhattan-bound J and Z tracks at grade past here.
MYRTLE_AV = "M11S"


def _local(*stations: str) -> Callable[[Trip], bool]:
    return lambda t: not set(stations).isdisjoint(t.stations())


def _any_of(*routes: str) -> Callable[[Trip], bool]:
    return lambda t: t.route in routes


def _platforms(*stations: str) -> set[str]:
    return {f"{s}{d}" for s in stations for d in "NS"}


QUEENS_BLVD = [f"G{i}" for i in (21, 20, 19, 18, 16, 15, 14, 13, 12, 11, 10, "09", "08")]


@dataclass(frozen=True)
class SharedTrack:
    """A track the M shares: its platforms, which other trains run on it, and if `spread`."""

    name: str
    stops: set[str]
    on: Callable[[Trip], bool]
    spread: bool


TRACKS = [
    # Myrtle Av to Marcy Av is three tracks: the M and local Js on the outer two.
    SharedTrack(
        "Broadway local", _platforms("M11", "M12", "M13", "M14", "M16"), _local("M13"), False
    ),
    # The Williamsburg Bridge is two, shared by every J, Z, and M:
    # toward Manhattan from Marcy Av, and toward Brooklyn from Essex St,
    # where the M and J merge just west of it, Essex St being three tracks toward Manhattan.
    SharedTrack("Williamsburg Bridge", {"M16S", "M18N"}, _any_of("J", "Z", "M"), False),
    SharedTrack(
        "6 Av local", _platforms(*(f"D{i}" for i in range(15, 22))), _local("D18", "D19"), True
    ),
    SharedTrack("63 St", _platforms("B04", "B06", "B08", "B10"), _local("B06"), False),
    SharedTrack("Queens Blvd local", _platforms(*QUEENS_BLVD), _local("G20", "G13"), True),
    SharedTrack("Rockaway north", _platforms("H01", "H02", "H03", "H04"), _local("H03"), False),
    SharedTrack("Rockaway south", _platforms("H12", "H13", "H14", "H15"), _local("H13"), False),
]


def _phys(trip: Trip, stop: str) -> str:
    """`stop` as the platform it physically is, by the J's letters along Broadway."""
    if trip.route == "M" and stop[:-1] in BROADWAY:
        return stop[:-1] + {"N": "S", "S": "N"}[stop[-1]]
    return stop


def _u(seconds: int) -> int:
    return round(seconds / UNIT)


type Linear = tuple[list[tuple[int, int]], int]


def _merge(*parts: tuple[list[tuple[int, int]], int]) -> list[tuple[int, int]]:
    out: dict[int, int] = defaultdict(int)
    for terms, sign in parts:
        for v, c in terms:
            out[v] += sign * c
    return [(v, c) for v, c in out.items() if c]


@dataclass
class _Plan:
    trip: Trip
    # "flex" for an M, "nudge" for a train it may nudge, or "fixed".
    kind: str
    # Whether an M's waits move the rest of the trip after them, else the part before.
    later: bool = True
    moved: int = -1
    nudged: int = -1
    # The wait and catch-up at each junction, by call.
    holds: dict[int, tuple[int, int]] = field(default_factory=dict)


@dataclass
class _Window:
    """One window's model, from `start` to `end`, with `done`'s trips as solved before."""

    timetable: Timetable
    cbtc: set[str]
    fastest: dict[tuple[str, str], int]
    leaving: dict[tuple[str, str, int], int]
    # Each route's trains at each platform, by service, in order, as `(departure, trip, call)`.
    own: dict[tuple[str, str, str], list[tuple[int, str, int]]]
    m_ids: set[str]
    day: str
    start: int
    end: int
    done: dict[str, dict[int, int]]
    model: CpModel = field(default_factory=CpModel)
    cost: list[tuple[int, int]] = field(default_factory=list)
    # Each pair's conflict, with the two trips.
    conflicts: list[tuple[int, str, str]] = field(default_factory=list)
    # The trains each train is kept apart from on a shared track, both free to move.
    partners: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    plans: dict[str, _Plan] = field(default_factory=dict)

    def dep_of(self, trip: Trip, i: int) -> Linear:
        """`trip`'s departure at call `i`, as `dep`, if it's in the window, or as solved before."""
        if trip.id in self.plans:
            return self.dep(self.plans[trip.id], i)
        return [], _u(trip.calls[i].departure) + self.done.get(trip.id, {}).get(i, 0)

    def short_of(self, x: Linear, y: Linear, gap: int, weight: int) -> None:
        """Cost `weight` for each unit `x` is less than `gap` after `y`."""
        short = self.model.new_int(0, gap)
        self.at_least((x[0] + [(short, 1)], x[1]), y, gap, [])
        self.cost.append((short, weight))

    def floor(self, stop: str) -> int:
        return _u(CBTC_FLOOR if stop[:-1] in self.cbtc else FLOOR)

    def dep(self, p: _Plan, i: int) -> Linear:
        """The departure at call `i`, in units, as `(terms, offset)`."""
        base = _u(p.trip.calls[i].departure)
        if p.kind == "fixed":
            return [], base + self.done.get(p.trip.id, {}).get(i, 0)
        if p.kind == "nudge":
            return [(p.nudged, 1)], base
        terms = [(p.moved, 1)]
        for j, (wait, catch_up) in p.holds.items():
            if (p.later and i >= j) or (not p.later and i < j):
                sign = 1 if p.later else -1
                terms += [(wait, sign), (catch_up, -sign)]
        return terms, base

    def flex(self, p: _Plan) -> int:
        """How far `p` can move, in units, at most."""
        if p.kind == "nudge":
            return _u(MOST_NUDGED)
        if p.kind == "flex":
            return _u(MOST_MOVED) + len(p.holds) * (_u(MOST_HELD) + 1)
        return 0

    def at_least(self, x: Linear, y: Linear, need: int, enforce: list[int]) -> None:
        """`x - y >= need`, where all of `enforce` hold."""
        (tx, ox), (ty, oy) = x, y
        terms = _merge((tx, 1), (ty, -1))
        lo = need - (ox - oy)
        if not terms:
            if lo > 0 and not enforce:
                raise ValueError("two fixed trains closer than they need be")
            if lo > 0:
                # It can't hold, so neither can what enforces it.
                self.model.add_linear([], 1, 1, enforce)
            return
        self.model.add_linear(terms, lo, 10**6, enforce)

    def absolute(self, v: int, weight: int) -> None:
        a = self.model.new_int(0, 10**6)
        self.model.add_linear([(a, 1), (v, -1)], 0, 10**6)
        self.model.add_linear([(a, 1), (v, 1)], 0, 10**6)
        self.cost.append((a, weight))

    def touches(self, trip: Trip) -> bool:
        return any(
            any(c.stop in t.stops for t in TRACKS) and self.start <= c.departure < self.end
            for c in trip.calls
        )

    def plan(self) -> None:
        trips = [t for t in self.timetable.trips.values() if self.day in t.service]
        # Where the window's Ms run on shared tracks past its end, as they run a while,
        # to take in the trains they meet there too.
        later: dict[str, list[int]] = defaultdict(list)
        for trip in trips:
            if trip.id in self.m_ids and trip.id not in self.done and self.touches(trip):
                for c in trip.calls:
                    if c.departure >= self.end and any(c.stop in t.stops for t in TRACKS):
                        later[_phys(trip, c.stop)].append(c.departure)
        for times in later.values():
            times.sort()
        reach = MOST_MOVED + len(JUNCTIONS) * MOST_HELD + MOST_NUDGED + FLOOR

        def meets(trip: Trip) -> bool:
            for c in trip.calls:
                times = later.get(_phys(trip, c.stop))
                if times:
                    k = bisect.bisect_left(times, c.departure - reach)
                    if k < len(times) and times[k] <= c.departure + reach:
                        return True
            return False

        for trip in trips:
            if not (self.touches(trip) or meets(trip)):
                continue
            if trip.id in self.done:
                self.plans[trip.id] = _Plan(trip, "fixed")
            elif trip.id in self.m_ids:
                self.plans[trip.id] = self.plan_m(trip)
            elif trip.route in NUDGEABLE:
                p = _Plan(trip, "nudge")
                p.nudged = self.model.new_int(-_u(MOST_NUDGED), _u(MOST_NUDGED))
                self.absolute(p.nudged, NUDGE)
                self.plans[trip.id] = p
            else:
                self.plans[trip.id] = _Plan(trip, "fixed")

    def plan_m(self, trip: Trip) -> _Plan:
        later = trip.stations()[-1] == ROCKAWAY_PARK
        p = _Plan(trip, "flex", later)
        p.moved = self.model.new_int(-_u(MOST_MOVED), _u(MOST_MOVED))
        self.absolute(p.moved, MOVE)
        for j, call in enumerate(trip.calls):
            if call.stop[:-1] not in JUNCTIONS:
                continue
            # It makes up time only where it has some to spare,
            # on the stretch the wait moves, or in its dwell.
            other = j + 1 if later else j - 1
            spare = 0
            if 0 <= other < len(trip.calls):
                a, b = (call, trip.calls[other]) if later else (trip.calls[other], call)
                spare = b.arrival - a.departure - self.fastest.get((a.stop, b.stop), 10**9)
            spare = max(spare, call.departure - call.arrival)
            wait = self.model.new_int(0, _u(MOST_HELD))
            catch_up = self.model.new_int(0, 1 if spare >= UNIT else 0)
            p.holds[j] = (wait, catch_up)
            self.cost += [(wait, HOLD), (catch_up, CATCH_UP)]
        return p

    def even_gap(self, service: str, stop: str, time: int) -> int:
        """The even gap, in units, between trains leaving `stop` around `time`, at most 10 min."""
        n = self.leaving[service, stop, time // 3600]
        return min(_u(600), _u(3600 // n)) if n else 0

    def pair(
        self,
        a: _Plan,
        b: _Plan,
        stops: list[tuple[int, int, int]],
        even: tuple[int, int, int] | None,
    ) -> None:
        """
        `a` and `b` a floor apart at each of `stops`, `(call in a, call in b, floor)`,
        in the same order, or else a conflict,
        and if `even`, `(call in a, call in b, gap)`, costing how far short of `gap` they are.
        """
        first = self.model.new_bool()
        clash = self.model.new_bool()
        self.conflicts.append((clash, a.trip.id, b.trip.id))
        if a.kind != "fixed" and b.kind != "fixed":
            self.partners[a.trip.id].add(b.trip.id)
            self.partners[b.trip.id].add(a.trip.id)
        self.cost.append((clash, CONFLICT))
        for ia, ib, need in stops:
            x, y = self.dep(a, ia), self.dep(b, ib)
            self.at_least(y, x, need, [first, ~clash])
            self.at_least(x, y, need, [~first, ~clash])
        if even is not None:
            ia, ib, gap = even
            short = self.model.new_int(0, gap)
            x, y = self.dep(a, ia), self.dep(b, ib)
            self.at_least((y[0] + [(short, 1)], y[1]), x, gap, [first])
            self.at_least((x[0] + [(short, 1)], x[1]), y, gap, [~first])
            self.cost.append((short, UNEVEN))

    def shared(self) -> None:
        """Floors on each shared track: which goes first, per pair, no overtaking."""
        for track in TRACKS:
            members = [
                p
                for p in self.plans.values()
                if (track.on(p.trip) or p.trip.id in self.m_ids)
                and any(_phys(p.trip, c.stop) in track.stops for c in p.trip.calls)
            ]
            for a, b in combinations(members, 2):
                if a.kind == "fixed" and b.kind == "fixed" or a.trip.service != b.trip.service:
                    continue
                self.shared_pair(track, a, b)

    def shared_pair(self, track: SharedTrack, a: _Plan, b: _Plan) -> None:
        def calls(p: _Plan) -> dict[str, int]:
            return {
                s: i
                for i, c in enumerate(p.trip.calls)
                if (s := _phys(p.trip, c.stop)) in track.stops
            }

        ia, ib = calls(a), calls(b)

        def apart(s: str) -> int:
            return abs(_u(a.trip.calls[ia[s]].departure) - _u(b.trip.calls[ib[s]].departure))

        reach = self.flex(a) + self.flex(b)
        # Only where they could come within the floor of each other, or swap.
        common = [s for s in ia.keys() & ib.keys() if apart(s) < reach + self.floor(s)]
        if not common:
            return
        stop = min(common, key=lambda s: ia[s])
        gap = self.even_gap(a.trip.service, stop, a.trip.calls[ia[stop]].departure)
        if a.trip.id not in self.m_ids and b.trip.id not in self.m_ids:
            # Only spacing worse than MTA's own, between trains MTA already runs.
            gap = min(gap, apart(stop))
        gap = min(gap, 3 * self.floor(stop))
        even = None
        if gap > self.floor(stop):
            even = (ia[stop], ib[stop], gap)
        need = self.floor
        if a.trip.id not in self.m_ids and b.trip.id not in self.m_ids:
            # Or as close as MTA's own schedule already runs them, if closer.
            def need(s: str) -> int:
                return min(self.floor(s), apart(s))

        self.pair(a, b, [(ia[s], ib[s], need(s)) for s in common], even)

    def myrtle_av(self) -> None:
        """An M toward Middle Village crosses the Manhattan-bound J and Z tracks at grade."""
        for a in self.plans.values():
            if a.trip.route != "M" or a.kind == "fixed":
                continue
            ia = next((i for i, c in enumerate(a.trip.calls) if c.stop == MYRTLE_AV), None)
            if ia is None:
                continue
            for b in self.plans.values():
                if b.trip.route not in ("J", "Z") or b.trip.service != a.trip.service:
                    continue
                ib = next((i for i, c in enumerate(b.trip.calls) if c.stop == MYRTLE_AV), None)
                if ib is None:
                    continue
                diff = _u(a.trip.calls[ia].departure) - _u(b.trip.calls[ib].departure)
                if abs(diff) < self.flex(a) + self.flex(b) + 3:
                    self.pair(a, b, [(ia, ib, 3)], None)

    def spread(self) -> None:
        """At most `ROW` departures in any `SPREAD` on the busy locals, at each platform."""
        for track in TRACKS:
            if not track.spread:
                continue
            for stop in sorted(track.stops):
                starts = []
                for p in self.plans.values():
                    if not (track.on(p.trip) or p.trip.id in self.m_ids):
                        continue
                    for i, c in enumerate(p.trip.calls):
                        if (
                            _phys(p.trip, c.stop) != stop
                            or not self.start - 3600 <= c.departure < self.end + 3600
                        ):
                            continue
                        terms, offset = self.dep(p, i)
                        if len(terms) > 1:
                            # CP-SAT takes one variable in an interval's start.
                            d = self.model.new_int(-self.flex(p), self.flex(p))
                            self.model.add_linear(_merge((terms, 1), ([(d, 1)], -1)), 0, 0)
                            terms = [(d, 1)]
                        interval = self.model.new_interval(terms, offset, _u(SPREAD))
                        starts.append((interval, _u(c.departure)))
                if len(starts) <= ROW:
                    continue
                times = sorted(t for _, t in starts)
                most = max(sum(1 for t in times if s <= t < s + _u(SPREAD)) for s in times)
                self.model.add_cumulative([v for v, _ in starts], max(ROW, most))

    def headways(self) -> None:
        """
        The Ms in each direction keep their order and `KEPT` of their gaps, at each platform,
        costing how far short of their scheduled gap they are
        at each junction and the platform before it, so on each stretch between junctions.
        """
        by_direction: dict[bool, list[_Plan]] = defaultdict(list)
        for p in self.plans.values():
            if p.trip.id in self.m_ids:
                by_direction[p.trip.stations()[-1] == ROCKAWAY_PARK].append(p)
        for ps in by_direction.values():
            for stop in {c.stop for p in ps for c in p.trip.calls}:
                at = sorted(
                    (p.trip.calls[i].departure, p, i)
                    for p in ps
                    for i, c in enumerate(p.trip.calls)
                    if c.stop == stop
                )
                for (ta, pa, ia), (tb, pb, ib) in pairwise(at):
                    if pa.trip.service != pb.trip.service or pa.kind == pb.kind == "fixed":
                        continue
                    x, y = self.dep(pb, ib), self.dep(pa, ia)
                    need = max(self.floor(stop), int(KEPT * _u(tb - ta)))
                    self.at_least(x, y, need, [])
                    calls = pb.trip.calls
                    if any(calls[j].stop[:-1] in JUNCTIONS for j in (ib, ib + 1) if j < len(calls)):
                        self.short_of(x, y, _u(tb - ta), UNEVEN_M)

    def own_headways(self) -> None:
        """
        Each train nudged costs how far short it falls of MTA's gap
        to its own route's trains before and after it,
        where it first stops on a track the M shares in the window.
        """
        for p in self.plans.values():
            if p.kind != "nudge":
                continue
            i = next(
                i
                for i, c in enumerate(p.trip.calls)
                if any(_phys(p.trip, c.stop) in t.stops for t in TRACKS)
                and self.start <= c.departure
            )
            trains = self.own[p.trip.service, p.trip.route, p.trip.calls[i].stop]
            at = bisect.bisect_left(trains, (p.trip.calls[i].departure, p.trip.id, i))
            for k in (at - 1, at + 1):
                if not 0 <= k < len(trains):
                    continue
                t, other, j = trains[k]
                gap = _u(abs(t - p.trip.calls[i].departure))
                ours, theirs = self.dep(p, i), self.dep_of(self.timetable.trips[other], j)
                if k > at:
                    self.short_of(theirs, ours, gap, UNEVEN_OWN)
                else:
                    self.short_of(ours, theirs, gap, UNEVEN_OWN)

    def solve(self, held: Counter[tuple[str, int | None]]) -> None:
        """Solve, and keep the trains first on a shared track in the window's first hour."""
        self.plan()
        self.shared()
        self.myrtle_av()
        self.spread()
        self.headways()
        self.own_headways()
        self.model.minimize(self.cost)
        status, _, _, _, values = self.model.solve(CAP, 600, WORKERS)
        if status not in ("OPTIMAL", "FEASIBLE"):
            raise RuntimeError(f"no fit for the M on {self.day} from {self.start}: {status}")
        # Kept are those first on a shared track in the window's first hour,
        # and the trains kept apart from the Ms kept, as the Ms count on where they are;
        # the rest are solved again, with the next hour in view.
        kept = set()
        for p in self.plans.values():
            if p.kind == "fixed":
                continue
            first = min(
                (
                    c.departure
                    for c in p.trip.calls
                    if any(c.stop in t.stops for t in TRACKS) and c.departure >= self.start
                ),
                default=self.end,
            )
            if first < self.start + 3600:
                kept.add(p.trip.id)
                if p.kind == "flex":
                    kept |= self.partners[p.trip.id]
        for trip_id in kept:
            p = self.plans[trip_id]
            self.done[trip_id] = {
                i: sum(values[v] * c for v, c in self.dep(p, i)[0])
                for i in range(len(p.trip.calls))
            }
            if p.kind == "flex":
                held["M moved", values[p.moved] * UNIT] += 1
                waited = sum(values[w] - values[c] for w, c in p.holds.values())
                held["M waits in all", waited * UNIT] += 1
            elif values[p.nudged]:
                held[f"{p.trip.route} nudged", values[p.nudged] * UNIT] += 1
        # Each conflict once, in the window that keeps one of its trains.
        if clashes := sum(values[c] for c, a, b in self.conflicts if a in kept or b in kept):
            held["M in conflict", None] += clashes


def _apply(trip: Trip, offsets: dict[int, int]) -> None:
    """
    Move `trip`'s departures by `offsets`, in units, and each arrival as the departure before,
    or as its own departure, where it makes up time on the way there.
    """
    before = offsets[0]
    for i, call in enumerate(trip.calls):
        dep = offsets[i] * UNIT
        call.arrival = min(call.arrival + before * UNIT, call.departure + dep)
        call.departure += dep
        before = offsets[i]


def fit_m(
    timetable: Timetable, m: list[Trip], cbtc: set[str], fastest: dict[tuple[str, str], int]
) -> Counter[tuple[str, int | None]]:
    """
    Fit `m`, QueensLink's M, among the trains it shares track with,
    with `fastest`, the quickest any train runs each stretch, before anything is added,
    to tell where it can make up time.
    How far each M is moved and waits, and how far other trains are nudged, to report.
    """
    leaving: dict[tuple[str, str, int], int] = defaultdict(int)
    for trip in timetable.trips.values():
        for c in trip.calls:
            leaving[trip.service, _phys(trip, c.stop), c.departure // 3600] += 1
    own: dict[tuple[str, str, str], list[tuple[int, str, int]]] = defaultdict(list)
    for trip in timetable.trips.values():
        if trip.route in NUDGEABLE:
            for i, c in enumerate(trip.calls):
                own[trip.service, trip.route, c.stop].append((c.departure, trip.id, i))
    for trains in own.values():
        trains.sort()
    m_ids = {t.id for t in m}
    held: Counter[tuple[str, int | None]] = Counter()
    for day in sorted({d for t in m for d in ("Weekday", "Saturday", "Sunday") if d in t.service}):
        ms = [t for t in m if day in t.service]
        first = min(c.departure for t in ms for c in t.calls) // 3600
        last = max(c.departure for t in ms for c in t.calls) // 3600
        done: dict[str, dict[int, int]] = {}
        for hour in range(first, last + 1):
            window = _Window(
                timetable,
                cbtc,
                fastest,
                leaving,
                own,
                m_ids,
                day,
                hour * 3600,
                (hour + 2) * 3600,
                done,
            )
            window.solve(held)
        for trip_id, offsets in done.items():
            _apply(timetable.trips[trip_id], offsets)
    return held
