import hashlib
from pathlib import Path

import httpx
import pandas as pd
import typer

from subway_sheds import export, gtfs, osm, paths, scenarios, travel_times
from subway_sheds.grid import land_grid
from subway_sheds.scenarios import Scenario

app = typer.Typer(no_args_is_help=True)

# Geofabrik's New York State extract, which they regenerate daily, with its MD5 beside it,
# and its `state.txt`, which names the OSM data's version (its replication sequence number).
NEW_YORK_OSM_URL = "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf"
NEW_YORK_OSM_STATE_URL = "https://download.geofabrik.de/north-america/us/new-york-updates/state.txt"
SHORELINE_URL = (
    "https://raw.githubusercontent.com/chriswhong/nyc-subway-isochrones"
    "/master/data/nyc-shoreline-clip-multi.json"
)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    # Retry failing to connect, which a server sometimes does.
    with (
        httpx.Client(transport=httpx.HTTPTransport(retries=5), timeout=60) as client,
        client.stream("GET", url, follow_redirects=True) as response,
    ):
        response.raise_for_status()
        with partial.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    partial.rename(dest)


def _fetch_streets(force: bool) -> None:
    """
    Filter the streets from Geofabrik's latest New York State extract,
    unless they were already filtered from it.

    Only its few-line `state.txt` is downloaded to tell, not the 500 MB extract,
    which is checked against its MD5 and deleted once filtered.
    """
    response = httpx.get(NEW_YORK_OSM_STATE_URL, follow_redirects=True, timeout=60)
    response.raise_for_status()
    latest = response.text
    if (
        not force
        and paths.OSM_PBF.exists()
        and paths.OSM_SOURCE_STATE.exists()
        and paths.OSM_SOURCE_STATE.read_text() == latest
    ):
        return
    extract = paths.CACHE / "new-york-latest.osm.pbf"
    typer.echo(f"downloading {NEW_YORK_OSM_URL}")
    md5 = httpx.get(f"{NEW_YORK_OSM_URL}.md5", follow_redirects=True, timeout=60)
    md5.raise_for_status()
    _download(NEW_YORK_OSM_URL, extract)
    with extract.open("rb") as f:
        actual = hashlib.file_digest(f, "md5").hexdigest()
    expected = md5.text.split()[0]
    if actual != expected:
        extract.unlink()
        raise RuntimeError(f"{NEW_YORK_OSM_URL} has MD5 {actual}, not {expected}")
    kept = osm.filter_streets(extract, paths.OSM_PBF)
    extract.unlink()
    paths.OSM_SOURCE_STATE.write_text(latest)
    typer.echo(f"kept {kept} ways")


@app.command()
def fetch(force: bool = typer.Option(False, help="Download again even if already cached.")) -> None:
    """Download the MTA feed, shoreline, and streets, and make both scenarios' feeds."""
    for url, dest in [
        (scenarios.mta_feed_url(), scenarios.mta_feed()),
        (SHORELINE_URL, paths.SHORELINE),
    ]:
        if force or not dest.exists():
            typer.echo(f"downloading {url}")
            _download(url, dest)
    _fetch_streets(force)
    gtfs.zip_feed(paths.QUEENSLINK_FEED, Scenario.QUEENSLINK.gtfs)
    gtfs.write_feed(scenarios.baseline_feed(), Scenario.BASELINE.gtfs)


@app.command()
def route(
    stop_ids: list[str] | None = typer.Argument(
        None,
        help="Parent station IDs, e.g. H04, whose times to replace; every station if none.",
    ),
) -> None:
    """Route from stations to the grid in each scenario."""
    destinations = land_grid().points()
    typer.echo(f"{len(destinations)} cells")
    for scenario in Scenario:
        stations = gtfs.stations(scenario.gtfs)
        if stop_ids:
            # A station only in one scenario, like QueensLink's new ones, is routed only there.
            stations = [s for s in stations if s.id in stop_ids]
        times = travel_times.from_stations(scenario, stations, destinations)
        dest = travel_times.output_path(scenario)
        if stop_ids and dest.exists():
            # Replace only these stations' times, keeping every other station's.
            routed = {s.id for s in stations}
            kept = pd.read_parquet(dest)
            kept = kept[~kept["station"].astype(str).isin(routed)]
            times = pd.concat(
                [kept.astype({"station": str}), times.astype({"station": str})],
                ignore_index=True,
            ).astype({"station": "category"})
        dest.parent.mkdir(parents=True, exist_ok=True)
        times.to_parquet(dest, index=False)
        typer.echo(f"{scenario}: {len(stations)} stations, {len(times)} reachable cells")


@app.command(name="export")
def export_() -> None:
    """Write the routed travel times for the web page."""
    export.write(land_grid())
    typer.echo(f"wrote {paths.WEB_DATA.relative_to(paths.ROOT)}")
