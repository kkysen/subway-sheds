from pathlib import Path

import httpx
import typer

from subway_sheds import gtfs, paths, scenarios, travel_times
from subway_sheds.scenarios import Scenario

app = typer.Typer(no_args_is_help=True)

# BBBike's New York City extract: the five boroughs and a margin, a fraction of the state's size.
NYC_OSM_URL = "https://download.bbbike.org/osm/bbbike/NewYork/NewYork.osm.pbf"


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
    """Download the MTA feed and streets, and make both scenarios' feeds."""
    for url, dest in [
        (scenarios.mta_feed_url(), scenarios.mta_feed()),
        (NYC_OSM_URL, paths.OSM_PBF),
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
def station(stop_ids: list[str] = typer.Argument(help="Parent station IDs, e.g. G08.")) -> None:
    """Compute travel times from stations to the grid under every scenario."""
    for scenario in Scenario:
        stations = gtfs.stations(scenario.gtfs)
        by_id = {s.id: s for s in stations}
        missing = [stop_id for stop_id in stop_ids if stop_id not in by_id]
        if missing:
            raise typer.BadParameter(f"not stations in {scenario}: {', '.join(missing)}")
        # The grid comes from the QueensLink feed's stations so both scenarios share it.
        grid = travel_times.grid(gtfs.stations(Scenario.QUEENSLINK.gtfs))
        times = travel_times.from_stations(scenario, [by_id[i] for i in stop_ids], grid)
        for stop_id, group in times.groupby("station"):
            dest = travel_times.output_path(scenario, str(stop_id))
            dest.parent.mkdir(parents=True, exist_ok=True)
            group.to_parquet(dest, index=False)
            reached = group["minutes"].notna().sum()
            typer.echo(f"{scenario} {stop_id}: {reached} of {len(group)} cells within reach")


@app.command()
def export(stop_ids: list[str] = typer.Argument(help="Stations already computed.")) -> None:
    """Write stations' travel times as GeoJSON for the web page."""
    paths.WEB_DATA.mkdir(parents=True, exist_ok=True)
    for stop_id in stop_ids:
        dest = paths.WEB_DATA / f"{stop_id}.geojson"
        dest.write_text(travel_times.cells_geojson(stop_id))
        typer.echo(f"wrote {dest.relative_to(paths.ROOT)}")
