from pathlib import Path

import httpx
import typer

from subway_sheds import export, gtfs, paths, scenarios, travel_times
from subway_sheds.grid import land_grid
from subway_sheds.scenarios import Scenario

app = typer.Typer(no_args_is_help=True)

# BBBike's New York City extract: the five boroughs and a margin, a fraction of the state's size.
NYC_OSM_URL = "https://download.bbbike.org/osm/bbbike/NewYork/NewYork.osm.pbf"
SHORELINE_URL = (
    "https://raw.githubusercontent.com/chriswhong/nyc-subway-isochrones"
    "/master/data/nyc-shoreline-clip-multi.json"
)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with partial.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    partial.rename(dest)


@app.command()
def fetch(force: bool = typer.Option(False, help="Download again even if already cached.")) -> None:
    """Download the MTA feed, streets, and shoreline, and make both scenarios' feeds."""
    for url, dest in [
        (scenarios.mta_feed_url(), scenarios.mta_feed()),
        (NYC_OSM_URL, paths.OSM_PBF),
        (SHORELINE_URL, paths.SHORELINE),
    ]:
        if force or not dest.exists():
            typer.echo(f"downloading {url}")
            _download(url, dest)
    gtfs.zip_feed(paths.QUEENSLINK_FEED, Scenario.QUEENSLINK.gtfs)
    gtfs.splice_routes(
        scenarios.mta_feed(),
        paths.QUEENSLINK_FEED,
        set(scenarios.BASELINE_ROUTES_FROM_QUEENSLINK),
        Scenario.BASELINE.gtfs,
    )


@app.command()
def route(
    stop_ids: list[str] | None = typer.Argument(
        None, help="Parent station IDs, e.g. H04; every station if none."
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
        dest.parent.mkdir(parents=True, exist_ok=True)
        times.to_parquet(dest, index=False)
        typer.echo(f"{scenario}: {len(stations)} stations, {len(times)} reachable cells")


@app.command(name="export")
def export_() -> None:
    """Write the routed travel times for the web page."""
    export.write(land_grid())
    typer.echo(f"wrote {paths.WEB_DATA.relative_to(paths.ROOT)}")
