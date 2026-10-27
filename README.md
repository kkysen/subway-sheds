# subway-sheds

How far you can get on the NYC subway in a given time, with and without [QueensLink](https://thequeenslink.org).

Inspired by Chris Whong's [`nyc-subway-isochrones`](https://github.com/chriswhong/nyc-subway-isochrones),
but routed with [R5](https://github.com/conveyal/r5) (through [`r5py`](https://r5py.readthedocs.io))
over the real street network instead of buffering stations by leftover walking time,
and with waiting for trains counted:
R5 departs every minute of an hour and reports the median trip:
the weekday morning peak, 7:30 to 8:30, or Saturday midday, 11 to noon.

## Data

- **QueensLink**: the QueensLink scenario feed, made by Franklin Tang, in [`gtfs/queenslink/`](gtfs/queenslink).
  It's MTA's subway feed for 2025-11-02 to 2025-12-07
  (the Mobility Database's [`mdb-516-202511010043`](https://mobilitydatabase.org/feeds/gtfs/mdb-516))
  with QueensLink's M replacing the M and the Rockaway Park shuttle,
  and the G run on past Court Sq along the Queens Blvd local to Forest Hills.
  Its F and M have also swapped tunnels, as they did after that feed,
  the F via 53 St and the M via 63 St.
  It also assumes Second Avenue Subway Phase 2 is open (the Q to 125 St),
  and runs the 7 on its regular schedule from an older feed,
  rather than this one's, with no regular 7 express during construction.
  It was committed as given, `B1_MVS.zip`, and the repairs R5 needed followed as commits.
- **Baseline**: the QueensLink feed without QueensLink, so that QueensLink is all that differs:
  its M turned at 63 Dr for Forest Hills-71 Av, as the M runs since the swap,
  instead of branching off toward the Rockaways,
  and running as often as MTA's M, hour by hour, rather than QueensLink's a little more often,
  its G cut back to Court Sq,
  and the Rockaway Park shuttle from MTA's feed.
  On weekends, it runs MTA's M instead, which runs only between Middle Village and Essex St.
- **Streets**: what R5 walks on (streets, sidewalks, footpaths, steps, and platforms)
  within NYC and a 2 km margin,
  filtered from Geofabrik's daily New York State OpenStreetMap extract to about 20 MB.
  `fetch` downloads the 500 MB extract only when Geofabrik's `state.txt` names newer OSM data
  than the streets were filtered from, checks it against its MD5, and deletes it once filtered.
- **Land**: `nyc-subway-isochrones`' NYC shoreline, which keeps the grid out of the water.

Travel times are routed from every station to a grid of about 21,500 cells over the city's land,
about 190 m across, on the Wednesday and the Saturday with the most trips scheduled.
Cells more than 250 m from anything to walk on, like marsh islands in Jamaica Bay, are left out.

## Usage

`mise install` sets up everything else (Java for R5, Node, `uv`).
`jpype1`, which `r5py` runs R5 through, has no wheel for Python 3.15 yet,
so `uv` builds it with the JDK and `ant` from `mise`:
run `uv` through `mise x --`, or from a shell where `mise` is activated.

```sh
uv run subway-sheds fetch    # download the MTA feed, land, and streets, and make both feeds
uv run subway-sheds route    # route every station, in both scenarios
uv run subway-sheds export   # for the web page, into `web/public/data/`
npm --prefix web install
npm --prefix web run dev
```

`route` takes station IDs to route only those, e.g. `route H04 V02`,
replacing only their times.
Downloads and travel times go in `.cache/`.
Neither they nor the export are committed:
CI routes, exports, and publishes the page to GitHub Pages on every push to `main`.

## Development

`pre-commit install` runs the checks on commit, and CI runs them too:
`ruff`, `ty`, `pyrefly` (strict), and `pytest` for the pipeline,
and `tsrs` (with every strictness option on) and `biome` for `web/`.
