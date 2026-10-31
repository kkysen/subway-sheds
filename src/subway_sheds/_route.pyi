"""Travel times routed by RAPTOR, from the `route` crate (`route/src/lib.rs`)."""

from datetime import date
from os import PathLike

type StrPath = str | PathLike[str]

def travel_times(
    osm: StrPath,
    max_rides: int,
    max_minutes: int,
    origins: list[tuple[str, float, float]],
    destinations: list[tuple[float, float]],
    speed: int,
    runs: list[tuple[StrPath, date, str, int, StrPath]],
) -> list[int]:
    """
    Median minutes from each of `origins`, as `(id, latitude, longitude)`,
    to each of `destinations`, as `(latitude, longitude)`, in each of `runs`,
    as `(feed, date, depart, minutes, out)`: departing each minute of `minutes` from `depart`,
    written to `out`, walking on the streets in `osm`, an `.osm.pbf`,
    at `speed` millimeters a second (see `route/src/walks.rs`).
    Returns how many rows each run wrote.

    The streets are read and walked once, for every feed's stops.
    """

def near_streets(osm: StrPath, points: list[tuple[float, float]], meters: float) -> list[bool]:
    """
    Whether each of `points`, as `(latitude, longitude)`,
    links to the streets in `osm`, an `.osm.pbf`, within `meters`, as routing links it.
    """

def stations(feed: StrPath) -> list[tuple[str, str, float, float]]:
    """The stations some trip in `feed` stops at, as `(id, name, latitude, longitude)`."""

def station_pairs(feed: StrPath) -> list[tuple[str, str, list[str], list[str]]]:
    """
    Each pair of stations some trip in `feed` runs between without stopping,
    as `(station, station, routes, shapes)`, the smaller station ID first,
    with the routes whose trips run between them and those trips' shapes, each sorted.
    """
