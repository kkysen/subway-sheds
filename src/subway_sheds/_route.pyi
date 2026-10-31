"""Travel times routed by RAPTOR, from the `route` crate (`route/src/lib.rs`)."""

from datetime import date
from os import PathLike

type StrPath = str | PathLike[str]

def travel_times(
    feed: StrPath,
    date: date,
    depart: str,
    minutes: int,
    max_rides: int,
    max_minutes: int,
    walks: StrPath,
    origins: list[str],
    destinations: int,
    out: StrPath,
) -> tuple[int, list[tuple[str, str, int]]]:
    """
    Median minutes from each of `origins` to each of `destinations` destinations,
    over a departure each minute of `minutes` from `depart`,
    walking by the tables in `walks` (see `route/src/walks.rs`), written to `out`.

    Returns how many rows were written and the transfers between stops the timetable lacks.
    """

def street_walks(
    osm: StrPath,
    feed: StrPath,
    date: date,
    origins: list[tuple[str, float, float]],
    destinations: list[tuple[float, float]],
    speed: int,
    max_secs: int,
    out: StrPath,
) -> None:
    """
    Writes the walks `travel_times` takes to `out`,
    computed on the streets in `osm`, an `.osm.pbf`, as R5 computes them
    (see `route/src/street_walks.rs`),
    from `origins`, as `(id, latitude, longitude)`, to `destinations`, as `(latitude, longitude)`,
    at `speed` millimeters a second, walking from origins for at most `max_secs`.
    """
