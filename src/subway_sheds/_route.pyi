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
