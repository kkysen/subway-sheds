"""How fast a train runs a stretch of track, from its curves, acceleration, and braking."""

from collections.abc import Callable

import numpy as np

MPH = 0.44704
FT = 0.3048
G = 9.81

# Recorded trains rarely pass 40-45 mph
# (43.7 mph at most, on the elevated 7 express;
# `ride-measurement-app`'s rides `154951` and `184134`), below CBTC's 50 mph under power.
TOP = 45 * MPH

# The most an R188 motored, by speed, under CBTC on the elevated 7,
# from the same rides' GNSS Doppler
# (`ride-measurement-app`'s `accel_curves.py R188:154951,184134`, 99th percentile per 2 m/s bin):
# speed (mph) and acceleration (m/s^2), already net of running resistance.
ACCEL_MPH = np.array([6.7, 11.2, 15.7, 20.1, 24.6, 29.1, 33.5, 38.0, 42.5])
ACCEL = np.array([1.33, 1.19, 1.07, 0.94, 0.85, 0.71, 0.52, 0.44, 0.40])

# The same rides' braking: their 1st percentile, about 0.95 m/s^2 at every speed.
BRAKE = 2.1 * MPH

# A full-length B Division train, 10 cars of 60 ft.
TRAIN = 600 * FT

# Superelevation plus unbalance on curves, in inches:
# 6 in plus 3 in, the most US practice allows (TCRP Report 57; FRA), as NYCT's own isn't published.
CANT = 9


def acceleration(v: float) -> float:
    """
    The measured acceleration at `v` m/s;
    above what was measured, constant power from the last of it.
    """
    mph = v / MPH
    if mph <= ACCEL_MPH[-1]:
        return float(np.interp(mph, ACCEL_MPH, ACCEL))
    return float(ACCEL[-1] * ACCEL_MPH[-1] / mph)


def curve_limits(radii: np.ndarray, top: float = TOP) -> np.ndarray:
    """
    Speed limits (m/s) for curves of `radii` (m),
    by the US formula V = sqrt(cant / (0.0007 d)) in mph,
    with d the degree of curve, per 100 ft of arc.
    """
    # Straight track, of infinite radius, has no degree of curve, and so no limit but `top`.
    with np.errstate(divide="ignore"):
        degree = 5729.58 / (np.abs(radii) / FT)
        return np.minimum(np.sqrt(CANT / (0.0007 * degree)) * MPH, top)


def head_limits(limits: np.ndarray) -> np.ndarray:
    """
    The limit at each position of the train's head:
    it stays under a curve's limit until its rear is past it.
    """
    n = round(TRAIN)
    return np.array([limits[max(0, i - n) : i + 1].min() for i in range(len(limits))])


def run_time(
    limits: np.ndarray,
    accel: Callable[[float], float] = acceleration,
    grade: np.ndarray | None = None,
) -> float:
    """
    The fastest run (s) from a stop to a stop under `limits` (m/s, every meter),
    as CBTC runs it: right up to each limit, braking on a continuous curve.

    `grade` (rise over run, every meter) takes from acceleration climbing and adds to it descending,
    and the other way around for braking.
    """
    n = len(limits)
    if grade is None:
        grade = np.zeros(n)
    # Backward: the most speed at each point that still brakes in time for what's ahead.
    vb = np.empty(n)
    vb[-1] = 0
    for i in range(n - 2, -1, -1):
        vb[i] = min(limits[i], np.sqrt(vb[i + 1] ** 2 + 2 * (BRAKE + G * grade[i])))
    # Forward: accelerate from the stop as allowed, never above the backward pass.
    v = np.empty(n)
    v[0] = 0
    for i in range(1, n):
        a = accel(v[i - 1]) - G * grade[i]
        v[i] = min(vb[i], np.sqrt(max(v[i - 1] ** 2 + 2 * a, 0)))
    mean = (v[1:] + v[:-1]) / 2
    return float(np.sum(1 / np.maximum(mean, 1e-3)))
