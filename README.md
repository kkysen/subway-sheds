# subway-sheds

How far you can get on the NYC subway in a given time, with and without [QueensLink](https://thequeenslink.org).

Inspired by Chris Whong's [`nyc-subway-isochrones`](https://github.com/chriswhong/nyc-subway-isochrones),
but routed with [R5](https://github.com/conveyal/r5) (through [`r5py`](https://r5py.readthedocs.io))
over the real street network instead of buffering stations by leftover walking time,
and with waiting for trains counted:
R5 departs every minute of the weekday morning peak and reports the median trip.

## Data

- **QueensLink**: the QueensLink scenario feed, in [`gtfs/queenslink/`](gtfs/queenslink).
  It's MTA's subway feed for 2025-11-02 to 2025-12-07
  (the Mobility Database's [`mdb-516-202511010043`](https://mobilitydatabase.org/feeds/gtfs/mdb-516))
  with QueensLink's M replacing the M and the Rockaway Park shuttle, and the G changed.
  It also assumes Second Avenue Subway Phase 2 is open (the Q to 125 St),
  and runs the 7 on its regular schedule from an older feed,
  rather than this one's, with no regular 7 express during construction.
  It was committed as given, `B1_MVS.zip`, and the repairs R5 needed followed as commits.
- **Baseline**: that same MTA feed,
  with the QueensLink feed's Q and 7 in place of its own,
  so that QueensLink is all that differs.
- **Streets**: BBBike's New York City OpenStreetMap extract.
- **Land**: `nyc-subway-isochrones`' NYC shoreline, which keeps the grid out of the water.

Travel times are routed from every station to a grid of about 22,000 cells over the city's land,
about 190 m across, each feed on its own Wednesday with the most trips scheduled.

## Usage

`mise install` sets up everything else (Java for R5, Node, `uv`).
`jpype1`, which `r5py` runs R5 through, has no wheel for Python 3.15 yet,
so `uv` builds it with the JDK and `ant` from `mise`:
run `uv` through `mise x --`, or from a shell where `mise` is activated.

```sh
uv run subway-sheds fetch    # download the MTA feed, streets, and land, and make both feeds
uv run subway-sheds route    # route every station, in both scenarios
uv run subway-sheds export   # for the web page, into `web/public/data/`
npm --prefix web install
npm --prefix web run dev
```

`route` takes station IDs to route only those, e.g. `route H04 V02`.
Downloads and travel times go in `.cache/`.
Neither they nor the export are committed:
CI routes, exports, and publishes the page to GitHub Pages on every push to `main`.

## Development

`pre-commit install` runs the checks on commit, and CI runs them too:
`ruff`, `ty`, `pyrefly` (strict), and `pytest` for the pipeline,
and `tsrs` (with every strictness option on) and `biome` for `web/`.
