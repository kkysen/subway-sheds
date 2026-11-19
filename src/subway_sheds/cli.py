import datetime
import gzip
import hashlib
import itertools
import re
from pathlib import Path
from typing import Annotated

from typer import Argument, Option, Typer, echo
lazy import httpx
lazy import pandas as pd

from subway_sheds import paths, sources
from subway_sheds.scenarios import Day, Fare, Scenario

# Lazily, as most commands use only some of them, and some take long to import.
lazy from subway_sheds import (
    access,
    export,
    gtfs,
    inputs,
    osm,
    street_walks,
    tracks,
    travel_times,
)
lazy from subway_sheds.grid import land_grid

app = Typer(no_args_is_help=True)

# Geofabrik's New York State extract, which they regenerate daily,
# and its `state.txt`, which names the OSM data's version (its replication sequence number).
# The latest extract redirects to that day's, which has its MD5 beside it.
NEW_YORK_OSM_URL = "https://download.geofabrik.de/north-america/us/new-york/new-york-latest.osm.pbf"
NEW_YORK_OSM_STATE_URL = "https://download.geofabrik.de/north-america/us/new-york/updates/state.txt"
# How often Geofabrik is asked whether it has newer OSM data, at most.
OSM_CHECK_EVERY = datetime.timedelta(days=1)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    # Retry failing to connect, which a server sometimes does,
    # over IPv4, which Geofabrik's servers answer more reliably; see `_get_text`.
    transport = httpx.HTTPTransport(retries=5, local_address="0.0.0.0")
    with (
        httpx.Client(transport=transport, timeout=60) as client,
        client.stream("GET", url, follow_redirects=True) as response,
    ):
        response.raise_for_status()
        with partial.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    partial.rename(dest)


def _get(url: str, *, follow_redirects: bool = True) -> httpx.Response:
    """
    A small file, over IPv4, retrying a server slow to answer.

    Geofabrik's servers sometimes take minutes to answer even for a few lines,
    most often over IPv6, though another of them, which a retry may reach, answers at once.
    From CI, though, none of them sometimes answer for minutes.
    """
    # Binding to IPv4's any address is how `httpx` is told to connect over IPv4.
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    for attempt in range(8):
        try:
            with httpx.Client(transport=transport, timeout=10) as client:
                response = client.get(url, follow_redirects=follow_redirects)
        except httpx.TimeoutException, httpx.ConnectError:
            if attempt == 7:
                raise
            continue
        # Not `raise_for_status` alone, which fails on a redirect not followed too.
        if response.is_error:
            response.raise_for_status()
        return response
    raise AssertionError("unreachable")


def _get_text(url: str) -> str:
    """A small file's text; see `_get`."""
    return _get(url).text


def _get_redirect(url: str) -> str:
    """Where `url` redirects to; see `_get`."""
    response = _get(url, follow_redirects=False)
    if not response.is_redirect:
        raise RuntimeError(f"{url} doesn't redirect, but answers {response.status_code}")
    return str(response.url.join(response.headers["location"]))


def _fetch_streets(force: bool) -> None:
    """
    Filter the streets from Geofabrik's latest New York State extract,
    unless they were already filtered from it.

    Only its few-line `state.txt` is downloaded to tell, not the 500 MB extract,
    which is checked against its MD5 and deleted once filtered.
    If Geofabrik doesn't answer, streets already filtered are kept,
    since a day or so older streets route the same.
    Geofabrik is asked at most every `OSM_CHECK_EVERY`, as it makes a new extract daily,
    and its servers can take minutes to answer.
    """
    if not force and paths.OSM_PBF.exists() and _checked_within(OSM_CHECK_EVERY):
        return
    try:
        latest = _get_text(NEW_YORK_OSM_STATE_URL)
    except httpx.TimeoutException, httpx.ConnectError:
        if force or not paths.OSM_PBF.exists():
            raise
        echo(f"keeping the streets, since {NEW_YORK_OSM_STATE_URL} didn't answer", err=True)
        return
    if (
        not force
        and paths.OSM_PBF.exists()
        and paths.OSM_SOURCE_STATE.exists()
        and paths.OSM_SOURCE_STATE.read_text() == latest
    ):
        _checked()
        return
    extract = paths.CACHE / "new-york-latest.osm.pbf"
    # That day's extract, not the latest, which may change to the next day's mid-download,
    # and which has no MD5 of its own.
    url = _get_redirect(NEW_YORK_OSM_URL)
    expected = _get_text(f"{url}.md5").split()[0]
    for attempt in range(3):
        echo(f"downloading {url}")
        _download(url, extract)
        with extract.open("rb") as f:
            actual = hashlib.file_digest(f, "md5").hexdigest()
        if actual == expected:
            break
        extract.unlink()
        if attempt == 2:
            raise RuntimeError(f"{url} has MD5 {actual}, not {expected}")
    # The version checked for, before the extract was picked, so if the extract is newer,
    # the next check downloads it again, rather than skipping a newer one.
    kept = osm.filter_streets(extract, paths.OSM_PBF)
    extract.unlink()
    paths.OSM_SOURCE_STATE.write_text(latest)
    _checked()
    echo(f"kept {kept} ways")


def _checked_within(every: datetime.timedelta) -> bool:
    """Whether Geofabrik was last asked for its `state.txt` less than `every` ago."""
    if not paths.OSM_CHECKED.exists():
        return False
    checked = datetime.datetime.fromisoformat(paths.OSM_CHECKED.read_text().strip())
    return datetime.datetime.now(datetime.UTC) - checked < every


def _checked() -> None:
    paths.OSM_CHECKED.write_text(datetime.datetime.now(datetime.UTC).isoformat())


def _fetch_archived(archived: sources.Archived, force: bool) -> None:
    """
    Download `archived` unless already cached, ungzipping it if the archive served it gzipped
    without saying so, and check it against its SHA-256.
    """
    if not force and archived.dest.exists():
        return
    echo(f"downloading {archived.url}")
    partial = archived.dest.with_name(archived.dest.name + ".download")
    _download(archived.url, partial)
    data = partial.read_bytes()
    partial.unlink()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    actual = hashlib.sha256(data).hexdigest()
    if actual != archived.sha256:
        raise RuntimeError(f"{archived.url} has SHA-256 {actual}, not {archived.sha256}")
    archived.dest.write_bytes(data)


MTA_OD_DATA_RAW = "https://raw.githubusercontent.com/kkysen/mta-od-data"


@app.command()
def fetch(
    force: Annotated[bool, Option(help="Download again even if already cached.")] = False,
) -> None:
    """Download the MTA feeds, shoreline, streets, and platforms."""
    for url, dest in [
        (sources.mta_feed_url(), sources.mta_feed()),
        (sources.SHORELINE_URL, paths.SHORELINE),
    ]:
        if force or not dest.exists():
            echo(f"downloading {url}")
            _download(url, dest)
    _fetch_archived(sources.MTA_2019, force)
    _fetch_streets(force)
    rev = re.search(r'rev = "(\w+)"', (paths.ROOT / "route" / "Cargo.toml").read_text())
    assert rev, "`route/Cargo.toml` pins `mta-od-data` by `rev`"
    for name in ["platforms.csv", "stacked_platforms.csv"]:
        dest = paths.PLATFORMS.with_name(name)
        if force or not dest.exists():
            _download(f"{MTA_OD_DATA_RAW}/{rev[1]}/src/mta_od_data/ridership/{name}", dest)


# A hash of what the scenarios' feeds were last made from, beside them, as `inputs` hashes it.
SCENARIOS_INPUTS = paths.GTFS / "inputs.sha256"


def scenarios_digest() -> str:
    """A hash of what the scenarios' feeds are made from, which `scripts/e2e.sh` keeps them by."""
    return inputs.digest("scenarios", [sources.mta_feed(), sources.MTA_2019.dest])


@app.command()
def scenarios(
    force: Annotated[
        bool, Option(help="Make them again even if what they're made from isn't new.")
    ] = False,
) -> None:
    """
    Make both scenarios' feeds, from the MTA feeds `fetch` downloads,
    unless they were last made from the same code and data.
    """
    digest = scenarios_digest()
    feeds = [scenario.gtfs for scenario in Scenario]
    if not force and inputs.made(SCENARIOS_INPUTS, digest, feeds):
        echo("the scenarios' feeds are already made from the same code and data")
        return
    from subway_sheds import scenarios

    # So that one failing midway isn't taken for made.
    SCENARIOS_INPUTS.unlink(missing_ok=True)
    for line in scenarios.describe(scenarios.write()):
        echo(line)
    inputs.mark(SCENARIOS_INPUTS, digest, feeds)


@app.command()
def run_times() -> None:
    """
    Time QueensLink's new track from its curves,
    calibrated against MTA's schedule on the Queens Blvd local,
    into `data/queenslink/`, which the scenarios' feeds are made with,
    with the track it's timed along, which they draw it with.
    """
    from subway_sheds import run_times as rt

    _fetch_archived(sources.QUEENSLINK_DIAGRAM, False)
    mta = gtfs.read_feed(sources.mta_feed())
    rows, calibration, track = rt.compute(
        mta, paths.QUEENSLINK_OSM, sources.QUEENSLINK_DIAGRAM.dest
    )
    rt.write_csv(rows, paths.QUEENSLINK_RUN_TIMES)
    rt.write_csv(track, paths.QUEENSLINK_TRACK)
    rt.write_csv(calibration, paths.QUEENSLINK_RUN_TIME_CALIBRATION)
    padding = calibration[0]["padding"]
    lean = calibration[0]["lean_padding"]
    echo(f"padding {padding} on runs, or {lean} lean, with {rt.DWELL} s dwells")


def _save(times: pd.DataFrame, dest: Path, routed: set[str] | None) -> None:
    """Write `times`, replacing only the `routed` stations' times if some are already there."""
    if routed is not None and dest.exists():
        kept = pd.read_parquet(dest)
        kept = kept[~kept["station"].astype(str).isin(routed)]
        times = pd.concat(
            [kept.astype({"station": str}), times.astype({"station": str})],
            ignore_index=True,
        ).astype({"station": "category"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    times.to_parquet(dest, index=False)


@app.command(name="access")
def access_() -> None:
    """
    Rebuild each station entrance's time to the platforms, `data/station_access.csv`,
    from the MTA's and OSM's data now, to review and commit.
    """
    table = access.write()
    echo(f"{len(table)} entrances of {table['station'].nunique()} stations")


@app.command(name="tracks")
def tracks_() -> None:
    """
    Rebuild which OSM track ways each route runs along between stations, `data/tracks.csv`,
    from the MTA's and OSM's data now, to review and commit.
    """
    echo(f"{tracks.write(gtfs.read_feed(sources.mta_feed()))} stretches")


def _walks(force: bool = False) -> None:
    if street_walks.write(force):
        echo(f"walked the streets into {paths.WALKS.relative_to(paths.ROOT)}")


@app.command()
def walks(
    force: Annotated[
        bool, Option(help="Walk them again even if what they're walked on isn't new.")
    ] = False,
) -> None:
    """
    Walk the streets every station's walks take and find the grid of cells near them,
    unless they were last walked from the same streets, stations, and code.
    """
    _walks(force)


@app.command()
def route(
    stop_ids: Annotated[
        list[str] | None,
        Argument(
            help="Parent station IDs, e.g. H04, whose times to replace; every station if none."
        ),
    ] = None,
    scenario_: Annotated[
        list[Scenario] | None,
        Option("--scenario", help="A scenario to route, repeatable; every one if none."),
    ] = None,
    day_: Annotated[
        list[Day] | None,
        Option("--day", help="A day to route, repeatable; every one if none."),
    ] = None,
    fare_: Annotated[
        list[Fare] | None,
        Option("--fare", help="A fare to route with, repeatable; every one if none."),
    ] = None,
    force: Annotated[
        bool, Option(help="Route again even if what it routes from isn't new.")
    ] = False,
) -> None:
    """
    Route from stations to the grid in each scenario, on each day, with each fare,
    unless last routed the same from the same code and data.
    """
    _walks()
    every_station = street_walks.stations()
    stations = every_station
    if stop_ids:
        stations = [s for s in stations if s.id in stop_ids]
    runs = list(
        itertools.product(scenario_ or list(Scenario), day_ or list(Day), fare_ or list(Fare))
    )
    digest = _route_digest(stop_ids, runs)
    outputs = [
        path(*run)
        for run in runs
        for path in (travel_times.arrivals_path, travel_times.routes_path)
    ]
    if not force and inputs.made(ROUTE_INPUTS, digest, outputs):
        echo("already routed the same from the same code and data")
        return
    # So that one failing midway isn't taken for routed.
    ROUTE_INPUTS.unlink(missing_ok=True)
    for run in travel_times.from_stations(runs, stations, every_station, street_walks.STREET_WALKS):
        scenario, day, fare, arrivals, routes = run
        routed = {s.id for s in stations} if stop_ids else None
        _save(arrivals, travel_times.arrivals_path(scenario, day, fare), routed)
        _save(routes, travel_times.routes_path(scenario, day, fare), routed)
        pairs = len(arrivals[["station", "to_station"]].drop_duplicates())
        echo(f"{scenario} {day} {fare}: {len(stations)} stations, {pairs} station pairs reached")
    inputs.mark(ROUTE_INPUTS, digest, outputs)


# A hash of what `route` last routed, and from what, as `_route_digest` hashes it.
ROUTE_INPUTS = paths.TRAVEL_TIMES / "inputs.sha256"


def _route_digest(stop_ids: list[str] | None, runs: list[tuple[Scenario, Day, Fare]]) -> str:
    """
    A hash of what `route` routes, the stations and runs, and what from:
    its code and data, this module's, the scenarios' feeds,
    and what the walks were walked from, whose hash stands for the walks.
    """
    made = [
        Path(__file__),
        *sorted(p for p in paths.GTFS.rglob("*") if p.is_file()),
        street_walks.INPUTS,
    ]
    asked = repr((sorted(stop_ids) if stop_ids else None, [tuple(map(str, run)) for run in runs]))
    return hashlib.sha256((asked + inputs.digest("travel_times", made)).encode()).hexdigest()


# A hash of what the web page's data was last written from, as `_export_digest` hashes it.
EXPORT_INPUTS = paths.OUT / "export.sha256"


def _export_digest() -> str:
    """
    A hash of what `export` writes from: its code and data, the scenarios' feeds,
    the routed travel times, and what the walks were walked from,
    whose hash stands for the walks, much bigger, made from them.
    Not the exit trees' cache's, which `export` writes itself, from these.
    """
    made = [
        *sorted(p for p in paths.GTFS.rglob("*") if p.is_file()),
        *sorted(p for p in paths.TRAVEL_TIMES.rglob("*") if p.is_file()),
        street_walks.INPUTS,
        access.TABLE,
    ]
    return inputs.digest("export", made)


def _exported() -> list[Path]:
    """What `export` writes: the web page's data, or nothing if it hasn't any yet."""
    if not (paths.WEB_DATA / "stations.json").exists():
        return [paths.WEB_DATA / "stations.json"]
    return sorted(p for p in paths.WEB_DATA.rglob("*") if p.is_file())


@app.command(name="export")
def export_(
    force: Annotated[
        bool, Option(help="Write it again even if what it's written from isn't new.")
    ] = False,
) -> None:
    """
    Write the routed travel times for the web page,
    unless it was last written from the same code and data.
    """
    _walks()
    digest = _export_digest()
    if not force and inputs.made(EXPORT_INPUTS, digest, _exported()):
        echo("the web page's data is already written from the same code and data")
        return
    # So that one failing midway isn't taken for written.
    EXPORT_INPUTS.unlink(missing_ok=True)
    export.write(land_grid())
    inputs.mark(EXPORT_INPUTS, digest, _exported())
    echo(f"wrote {paths.WEB_DATA.relative_to(paths.ROOT)}")
