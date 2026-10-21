# subway-sheds

How far you can get on the NYC subway in a given time, today and with [QueensLink](https://thequeenslink.org).

Inspired by Chris Whong's [`nyc-subway-isochrones`](https://github.com/chriswhong/nyc-subway-isochrones),
but routed with [R5](https://github.com/conveyal/r5) (through [`r5py`](https://r5py.readthedocs.io))
over the real street network instead of buffering stations by leftover walking time,
and with waiting for trains counted:
R5 departs every minute of the morning peak and reports the median trip.

## Data

- **Baseline**: MTA's static subway GTFS, downloaded by `subway-sheds fetch`.
- **QueensLink**: the QueensLink scenario feed, `B1_MVS.zip` in the repo root.
  It's the whole subway with QueensLink's M added,
  including its new stations (V01-V04, Metropolitan Av-Parkside to Liberty Av).
  `fetch` repacks it as R5 needs it, repairing what R5 rejects:
  stations left at `location_type` 0 and a `route_url` with no scheme.
- **Streets**: BBBike's New York City OpenStreetMap extract, also downloaded by `fetch`.

The two feeds cover different months,
so each is routed on its own Wednesday with the most trips scheduled.

## Usage

`mise install` sets up everything else (Java for R5, Node, `uv`).
`jpype1`, which `r5py` runs R5 through, has no wheel for Python 3.15 yet,
so `uv` builds it with the JDK and `ant` from `mise`:
run `uv` through `mise x --`, or from a shell where `mise` is activated.

```sh
uv run subway-sheds fetch          # download and repack the feeds and streets
uv run subway-sheds station H04    # travel times from Broad Channel, in both scenarios
uv run subway-sheds export H04     # as GeoJSON for the web page
npm --prefix web install
npm --prefix web run dev
```

Downloads and travel times go in `.cache/`; exports in `web/public/data/`.
Neither is committed.

## Development

`pre-commit install` runs the checks on commit:
`ruff`, `ty`, `pyrefly` (strict), and `pytest` for the pipeline,
and `tsrs` (with every strictness option on) and `biome` for `web/`.
