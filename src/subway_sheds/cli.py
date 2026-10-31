import hashlib
import itertools
from pathlib import Path
from typing import Annotated

import geopandas as gpd
import httpx
import pandas as pd
from typer import Argument, Option, Typer, echo

from subway_sheds import export, gtfs, osm, paths, raptor_check, scenarios, travel_times
from subway_sheds.grid import land_grid
from subway_sheds.scenarios import Day, Scenario

app = Typer(no_args_is_help=True)

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


@app.command()
def fetch(
    force: Annotated[bool, Option(help="Download again even if already cached.")] = False,
) -> None:
    """Download the MTA feed, shoreline, and streets, and make both scenarios' feeds."""
    for url, dest in [
        (scenarios.mta_feed_url(), scenarios.mta_feed()),
        (SHORELINE_URL, paths.SHORELINE),
    ]:
        if force or not dest.exists():
            echo(f"downloading {url}")
            _download(url, dest)
    _fetch_streets(force)
    gtfs.zip_feed(paths.QUEENSLINK_FEED, Scenario.QUEENSLINK.gtfs)
    gtfs.write_feed(scenarios.baseline_feed(), Scenario.BASELINE.gtfs)


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
) -> None:
    """Route from stations to the grid in each scenario, on each day."""
    destinations = land_grid().points()
    echo(f"{len(destinations)} cells")
    # Every scenario's stations, each routed in every scenario,
    # so that from QueensLink's new ones, the baseline is walking from where they'd be,
    # and there's saying how much they save.
    every_station = {s.id: s for scenario in Scenario for s in gtfs.stations(scenario.gtfs)}
    for scenario, day in itertools.product(scenario_ or list(Scenario), day_ or list(Day)):
        stations = list(every_station.values())
        if stop_ids:
            stations = [s for s in stations if s.id in stop_ids]
        times, station_times = travel_times.from_stations(
            scenario, day, stations, destinations, list(every_station.values())
        )
        routed = {s.id for s in stations} if stop_ids else None
        _save(times, travel_times.output_path(scenario, day), routed)
        _save(station_times, travel_times.stations_output_path(scenario, day), routed)
        echo(
            f"{scenario} {day}: {len(stations)} stations, {len(times)} reachable cells, "
            f"{len(station_times)} reachable stations"
        )


def _differences(name: str, both: pd.DataFrame) -> None:
    off = (both["raptor"] - both["r5"]).abs()
    shares = ", ".join(f"{(off <= m).mean():.1%} within {m}" for m in (0, 1, 2, 5))
    echo(f"  {name}: {len(both)} reached by both: {shares} min")


def _compared_stations() -> tuple[gpd.GeoDataFrame, list[gtfs.Station]]:
    """The cells and every scenario's stations, as `route` routes from and to them."""
    cells = land_grid().points()
    return cells, list({s.id: s for sc in Scenario for s in gtfs.stations(sc.gtfs)}.values())


@app.command(name="r5-walks")
def r5_walks(
    scenario: Annotated[Scenario, Option(help="The scenario whose walks to write.")],
) -> None:
    """
    Write R5's own walks, in seconds, for `compare-raptor`.

    One scenario at a time, since each loads R5's network into the JVM for good.
    """
    cells, stations = _compared_stations()
    destinations = pd.concat(
        [cells, travel_times.station_points(stations, len(cells))], ignore_index=True
    )
    raptor_check.write_walks(scenario, Day.WEEKDAY, stations, destinations)
    echo(f"wrote {raptor_check.walks_dir(scenario).relative_to(paths.ROOT)}")


@app.command(name="compare-raptor")
def compare_raptor(
    scenario_: Annotated[
        list[Scenario] | None,
        Option("--scenario", help="A scenario to compare, repeatable; every one if none."),
    ] = None,
    day_: Annotated[
        list[Day] | None,
        Option("--day", help="A day to compare, repeatable; every one if none."),
    ] = None,
) -> None:
    """Route with RAPTOR walking R5's own walks from `r5-walks`, and compare with R5's `route`."""
    cells, stations = _compared_stations()
    destinations = len(cells) + len(stations)
    for scenario, day in itertools.product(scenario_ or list(Scenario), day_ or list(Day)):
        rows, unknown = raptor_check.route(
            scenario, day, stations, destinations, raptor_check.walks_dir(scenario)
        )
        echo(
            f"{scenario} {day}: RAPTOR wrote {rows} rows; "
            f"{len(unknown)} of R5's transfers are between stops it doesn't serve"
        )
        times = raptor_check.compare(scenario, day, len(cells), stations)
        both = times.dropna(subset=["r5", "raptor"])
        to_station = both["destination"] >= len(cells)
        _differences("to stations", both[to_station])
        _differences("to cells", both[~to_station])
        echo(
            f"  reached by R5 only: {times['raptor'].isna().sum()}, "
            f"by RAPTOR only: {times['r5'].isna().sum()}"
        )
        worst = both[to_station].assign(off=lambda t: t["raptor"] - t["r5"])
        worst = worst.reindex(worst["off"].abs().sort_values(ascending=False).index).head(10)
        for origin, destination, r5, raptor in zip(
            worst["origin"].tolist(),
            worst["destination"].tolist(),
            worst["r5"].tolist(),
            worst["raptor"].tolist(),
            strict=True,
        ):
            from_, to = stations[origin], stations[destination - len(cells)]
            echo(
                f"    {from_.name} ({from_.id}) to {to.name} ({to.id}): "
                f"R5 {r5:.0f}, RAPTOR {raptor:.0f} min"
            )


@app.command(name="street-walks")
def street_walks(
    scenario_: Annotated[
        list[Scenario] | None,
        Option(
            "--scenario", help="A scenario whose walks to write, repeatable; every one if none."
        ),
    ] = None,
) -> None:
    """Write the walks `r5-walks` reads from R5, computed by the `streets` crate instead."""
    cells, stations = _compared_stations()
    destinations = pd.concat(
        [cells, travel_times.station_points(stations, len(cells))], ignore_index=True
    )
    for scenario in scenario_ or list(Scenario):
        raptor_check.write_street_walks(scenario, Day.WEEKDAY, stations, destinations)
        echo(f"wrote {raptor_check.street_walks_dir(scenario).relative_to(paths.ROOT)}")


def _seconds_off(name: str, t: pd.DataFrame, a: str, b: str, unit: str, within: list[int]) -> None:
    both = t.dropna(subset=[a, b])
    off = (both[b] - both[a]).abs()
    shares = ", ".join(f"{(off <= w).mean():.1%} within {w}" for w in within)
    echo(
        f"  {name}: {len(both)} in both: {shares} {unit}; "
        f"mean {(both[b] - both[a]).mean():+.2f}; "
        f"only {a}: {t[b].isna().sum()}, only {b}: {t[a].isna().sum()}"
    )


@app.command(name="compare-walks")
def compare_walks(
    scenario_: Annotated[
        list[Scenario] | None,
        Option("--scenario", help="A scenario to compare, repeatable; every one if none."),
    ] = None,
    day_: Annotated[
        list[Day] | None,
        Option("--day", help="A day to compare, repeatable; every one if none."),
    ] = None,
) -> None:
    """
    Compare the `streets` crate's walks from `street-walks` with R5's from `r5-walks`,
    table by table, and RAPTOR's times walking each.
    """
    cells, stations = _compared_stations()
    destinations = len(cells) + len(stations)
    for scenario in scenario_ or list(Scenario):
        echo(f"{scenario} walks, in seconds:")
        for table, t in raptor_check.compare_walks(scenario).items():
            _seconds_off(table, t, "r5", "streets", "s", [0, 1, 5, 30])
        for day in day_ or list(Day):
            raptor_check.route(
                scenario, day, stations, destinations, raptor_check.street_walks_dir(scenario)
            )
            t = raptor_check.compare_raptors(scenario, day)
            to_station = t["destination"] >= len(cells)
            echo(f"{scenario} {day}, RAPTOR's minutes on each's walks:")
            _seconds_off("to stations", t[to_station], "r5", "streets", "min", [0, 1, 2, 5])
            _seconds_off("to cells", t[~to_station], "r5", "streets", "min", [0, 1, 2, 5])


@app.command(name="export")
def export_() -> None:
    """Write the routed travel times for the web page."""
    export.write(land_grid())
    echo(f"wrote {paths.WEB_DATA.relative_to(paths.ROOT)}")
