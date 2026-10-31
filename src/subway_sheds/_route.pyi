"""Travel times routed by RAPTOR, from the `route` crate (`route/src/lib.rs`)."""

from datetime import date
from os import PathLike

type StrPath = str | PathLike[str]

def travel_times(
    osm: StrPath,
    max_rides: int,
    max_minutes: int,
    stations: list[tuple[str, str, list[tuple[float, float, int]]]],
    platforms: StrPath,
    islands: list[str],
    free: list[tuple[str, str]],
    origins: list[str],
    destinations: list[tuple[float, float]],
    speed: int,
    runs: list[tuple[StrPath, date, str, int, StrPath, bool]],
    exits_out: StrPath,
    exits_limit: int,
    paths_out: StrPath,
) -> list[int]:
    """
    Median minutes from each of `origins`, by station ID, starting on the street at its entrances,
    to each of `destinations`, as `(latitude, longitude)`, in each of `runs`,
    as `(feed, date, depart, minutes, out, pay_again)`:
    departing each minute of `minutes` from `depart`, written to `out`,
    changing between complexes on the streets if `pay_again`,
    walking on the streets in `osm`, an `.osm.pbf`,
    at `speed` millimeters a second, through `stations`' entrances,
    as `(id, complex, [(latitude, longitude, seconds to the platforms)])`
    (see `route/src/walks.rs`),
    changing trains within a complex by `platforms`, `mta-od-data`'s `platforms.csv`,
    with `islands`' stations' stops all on one island platform,
    and changing free out of the system between the complexes of `free`'s pairs of stations.
    Each run also writes its origins' rides to stations' platforms
    each minute to `{out}-arrivals.parquet`, as `origin`, `station`, `minute`, and `seconds`,
    the journeys to them at the median minute to `{out}-routes.parquet`,
    and its patterns' stations to `{out}-patterns.parquet`,
    and every station's walks out to destinations, up to `exits_limit` millimeters,
    go to `exits_out`, as `station`, `destination`, `exit_seconds`, and `meters`,
    and the street walks between stations of different complexes that changing trains may walk,
    to draw, go to `paths_out`, as `from`, `to`, `latitude`, and `longitude`,
    a row for each point, each walk's in order,
    from the entrance walked out of to the one walked into.
    Returns how many rows each run wrote.

    The streets are read and walked once, for every feed's stations.
    """

def near_streets(osm: StrPath, points: list[tuple[float, float]], meters: float) -> list[bool]:
    """
    Whether each of `points`, as `(latitude, longitude)`,
    links to the streets in `osm`, an `.osm.pbf`, within `meters`, as routing links it.
    """

def used_exits(
    arrivals: bytes,
    cell: bytes,
    exit_seconds: bytes,
    meters: bytes,
    starts: bytes,
    cells: int,
    stations: int,
    mphs: list[float],
) -> list[bool]:
    """
    Which of the walks out of stations to the cells the page ever takes, by their place,
    as `web/times/`'s `used_exits` finds them,
    from every origin, in every run, at every one of `mphs`.
    `arrivals` is each origin's, as the page reads them, one after another,
    and the walks are `cell`, `exit_seconds`, and `meters`, by station from `starts`,
    all little-endian, as `export` writes them.
    """

def exit_trees(
    osm: StrPath,
    stations: list[tuple[str, str, list[tuple[float, float, int]]]],
    destinations: list[tuple[float, float]],
    speed: int,
    limit: int,
    wanted: list[tuple[int, int, bool]],
) -> list[tuple[bytes, bytes, bytes, int, bytes, int]]:
    """
    Each of `stations`' walks out to destinations of `wanted`, `(station, destination, far)`,
    by index into `stations` and `destinations`, as `exits` takes them,
    from the entrance quickest at `speed` millimeters a second, walking at most `limit` millimeters,
    as a tree (see `walks::ExitTree` in `route/src/walks.rs`),
    with the walks to `far` ones after the rest:
    `(parents, latitudes, longitudes, near, ends, near_ends)`,
    the first three little-endian `int32`s,
    and `ends` `(destination, point)` little-endian `uint32`s.
    """

def stations(feed: StrPath) -> list[tuple[str, str, float, float]]:
    """The stations some trip in `feed` stops at, as `(id, name, latitude, longitude)`."""

def station_pairs(feed: StrPath) -> list[tuple[str, str, list[str], list[str]]]:
    """
    Each pair of stations some trip in `feed` runs between without stopping,
    as `(station, station, routes, shapes)`, the smaller station ID first,
    with the routes whose trips run between them and those trips' shapes, each sorted.
    """
