import gzip
import hashlib
import itertools
import re
from pathlib import Path
from typing import Annotated

import httpx
import pandas as pd
from typer import Argument, Option, Typer, echo

from subway_sheds import access, export, gtfs, osm, paths, scenarios, sources, travel_times
from subway_sheds.grid import land_grid
from subway_sheds.scenarios import Day, Fare, Scenario

app = Typer(no_args_is_help=True)

# Geofabrik's New York State extract, which they regenerate daily, with its MD5 beside it,
# and its `state.txt`, which names the OSM data's version (its replication sequence number).
NEW_YORK_OSM_URL = "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf"
NEW_YORK_OSM_STATE_URL = "https://download.geofabrik.de/north-america/us/new-york-updates/state.txt"


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


def _get_text(url: str) -> str:
    """
    A small file's text, over IPv4, retrying a server slow to answer.

    Geofabrik's servers sometimes take minutes to answer even for a few lines,
    most often over IPv6, though another of them, which a retry may reach, answers at once.
    From CI, though, none of them sometimes answer for minutes.
    """
    # Binding to IPv4's any address is how `httpx` is told to connect over IPv4.
    transport = httpx.HTTPTransport(local_address="0.0.0.0")
    for attempt in range(8):
        try:
            with httpx.Client(transport=transport, timeout=10) as client:
                response = client.get(url, follow_redirects=True)
        except httpx.TimeoutException, httpx.ConnectError:
            if attempt == 7:
                raise
            continue
        response.raise_for_status()
        return response.text
    raise AssertionError("unreachable")


def _fetch_streets(force: bool) -> None:
    """
    Filter the streets from Geofabrik's latest New York State extract,
    unless they were already filtered from it.

    Only its few-line `state.txt` is downloaded to tell, not the 500 MB extract,
    which is checked against its MD5 and deleted once filtered.
    If Geofabrik doesn't answer, streets already filtered are kept,
    since a day or so older streets route the same.
    """
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
        return
    extract = paths.CACHE / "new-york-latest.osm.pbf"
    echo(f"downloading {NEW_YORK_OSM_URL}")
    md5 = _get_text(f"{NEW_YORK_OSM_URL}.md5")
    _download(NEW_YORK_OSM_URL, extract)
    with extract.open("rb") as f:
        actual = hashlib.file_digest(f, "md5").hexdigest()
    expected = md5.split()[0]
    if actual != expected:
        extract.unlink()
        raise RuntimeError(f"{NEW_YORK_OSM_URL} has MD5 {actual}, not {expected}")
    kept = osm.filter_streets(extract, paths.OSM_PBF)
    extract.unlink()
    paths.OSM_SOURCE_STATE.write_text(latest)
    echo(f"kept {kept} ways")


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
    """Download the MTA feeds, shoreline, streets, and platforms, and make both scenarios' feeds."""
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
    mta = gtfs.read_feed(sources.mta_feed())
    gtfs.write_feed(scenarios.queenslink_feed(mta), Scenario.QUEENSLINK.gtfs)
    gtfs.write_feed(scenarios.baseline_feed(mta), Scenario.BASELINE.gtfs)


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
) -> None:
    """Route from stations to the grid in each scenario, on each day, with each fare."""
    destinations = land_grid().points()
    echo(f"{len(destinations)} cells")
    # Every scenario's stations, each routed in every scenario,
    # so that from QueensLink's new ones, the baseline is walking from where they'd be,
    # and there's saying how much they save.
    every_station = {s.id: s for scenario in Scenario for s in gtfs.stations(scenario.gtfs)}
    stations = list(every_station.values())
    if stop_ids:
        stations = [s for s in stations if s.id in stop_ids]
    runs = list(
        itertools.product(scenario_ or list(Scenario), day_ or list(Day), fare_ or list(Fare))
    )
    for run in travel_times.from_stations(
        runs, stations, destinations, list(every_station.values())
    ):
        scenario, day, fare, arrivals, routes = run
        routed = {s.id for s in stations} if stop_ids else None
        _save(arrivals, travel_times.arrivals_path(scenario, day, fare), routed)
        _save(routes, travel_times.routes_path(scenario, day, fare), routed)
        pairs = len(arrivals[["station", "to_station"]].drop_duplicates())
        echo(f"{scenario} {day} {fare}: {len(stations)} stations, {pairs} station pairs reached")


@app.command(name="export")
def export_() -> None:
    """Write the routed travel times for the web page."""
    export.write(land_grid())
    echo(f"wrote {paths.WEB_DATA.relative_to(paths.ROOT)}")
