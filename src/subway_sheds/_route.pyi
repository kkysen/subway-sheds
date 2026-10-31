"""Travel times routed by RAPTOR, from the `route` crate (`route/src/lib.rs`)."""

from datetime import date
from os import PathLike

type StrPath = str | PathLike[str]

def travel_times(
    osm: StrPath,
    feed: StrPath,
    date: date,
    depart: str,
    minutes: int,
    max_rides: int,
    max_minutes: int,
    origins: list[tuple[str, float, float]],
    destinations: list[tuple[float, float]],
    speed: int,
    out: StrPath,
) -> int:
    """
    Median minutes from each of `origins`, as `(id, latitude, longitude)`,
    to each of `destinations`, as `(latitude, longitude)`,
    over a departure each minute of `minutes` from `depart`,
    walking on the streets in `osm`, an `.osm.pbf`, at `speed` millimeters a second
    (see `route/src/walks.rs`), written to `out`. Returns how many rows were written.
    """
