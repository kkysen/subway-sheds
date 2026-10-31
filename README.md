# subway-sheds

How far you can get on the NYC subway in a given time, with and without [QueensLink](https://thequeenslink.org).

Inspired by Chris Whong's [`nyc-subway-isochrones`](https://github.com/chriswhong/nyc-subway-isochrones),
but routed over the real street network instead of buffering stations by leftover walking time,
and with waiting for trains counted:
it departs every minute of an hour and takes the median trip:
the weekday morning peak, 7:30 to 8:30, or Saturday midday, 11:30 to 12:30.
Trains are routed by the RAPTOR in [`mta-od-data`](https://github.com/kkysen/mta-od-data)'s `raptor` crate,
and walks on the streets by its `streets` crate,
in and out through stations' entrances,
in the `route` crate here (`route/`), built into the `subway_sheds._route` extension.

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
  It was committed as given, `B1_MVS.zip`, and the repairs routing it needed followed as commits.
- **Baseline**: the QueensLink feed without QueensLink, so that QueensLink is all that differs:
  its M turned at 63 Dr for Forest Hills-71 Av, as the M runs since the swap,
  instead of branching off toward the Rockaways,
  and running as often as MTA's M, hour by hour, rather than QueensLink's a little more often,
  its G cut back to Court Sq,
  and the Rockaway Park shuttle from MTA's feed.
  On weekends, it runs MTA's M instead, which runs only between Middle Village and Essex St.
- **Streets**: what's walked on (streets, sidewalks, footpaths, steps, and platforms)
  within NYC and a 2 km margin,
  filtered from Geofabrik's daily New York State OpenStreetMap extract to about 20 MB.
  `fetch` downloads the 500 MB extract only when Geofabrik's `state.txt` names newer OSM data
  than the streets were filtered from, checks it against its MD5, and deletes it once filtered.
- **Station entrances**: each entrance's time between the street and the platforms,
  in [`data/station_access.csv`](data/station_access.csv),
  from the MTA's [entrances](https://data.ny.gov/d/i9wp-a4ja),
  the heights of its [elevators and escalators](https://data.ny.gov/d/94fv-bak7),
  depths published elsewhere where those are blank,
  and the levels OSM puts platforms on,
  climbed by stairs, escalator, or elevator at the rates in `src/subway_sheds/access.py`.
  `subway-sheds access` rebuilds it to review and commit.
  Changing trains between complexes walks the streets, out one entrance and in another.
- **Platforms**: `mta-od-data`'s physical platforms from OSM, at the commit `route/Cargo.toml` pins,
  which time changing trains within a complex as its `raptor`'s OSM transfers do:
  free on the same platform, up and over to another, a flight a level between stacked ones.
  QueensLink's new stations are taken to have one island platform each.
- **Land**: `nyc-subway-isochrones`' NYC shoreline, which keeps the grid out of the water.

Travel times are routed from every station to a grid of about 21,500 cells over the city's land,
about 190 m across, on the Wednesday and the Saturday with the most trips scheduled.
Cells more than 250 m from anything to walk on, like marsh islands in Jamaica Bay, are left out.
Every station is routed to as well, to say how far it is itself,
rather than its cell's time, which counts walking to the cell's middle.

## Usage

`mise install` sets up everything else (Node and `uv`) but Rust,
whose `cargo` `uv` runs through `maturin` to build the `route` crate.
`raptor` and `streets` come from `mta-od-data` at the commit `route/Cargo.toml` pins.
To build against a local checkout of it beside this one instead,
patch them in a gitignored `.cargo/config.toml`:

```toml
[patch."https://github.com/kkysen/mta-od-data"]
raptor = { path = "../mta-od-data/raptor" }
streets = { path = "../mta-od-data/streets" }
```

`uv` doesn't see changes there, so rebuild after them with `uv sync --reinstall-package subway-sheds`.

```sh
uv run subway-sheds fetch    # download the MTA feed, land, streets, and platforms, and make both feeds
uv run subway-sheds route    # route every station, in both scenarios
uv run subway-sheds export   # for the web page, into `web/public/data/`
npm --prefix web install
npm --prefix web run dev
```

`route` takes station IDs to route only those, e.g. `route H04 V02`,
replacing only their times,
and `--scenario` and `--day` to route only those, e.g. `route --scenario queenslink --day weekday`.
Downloads and travel times go in `.cache/`.
Neither they nor the export are committed:
CI routes, exports, and publishes the page to GitHub Pages on every push to `main`.

The page times walking out of stations itself, at the speed its slider is set to, 2 to 4 mph:
`export` gives it when each station's trains reach every station each minute of the window,
and every station's walks out to the cells,
and it takes each cell's median over the minutes, as routing does.
Walking to the first station and between stations stays at 3 mph.

## Development

`pre-commit install` runs the checks on commit, and CI runs them too:
`ruff`, `ty`, `pyrefly` (strict), and `pytest` for the pipeline,
and `tsrs` (with every strictness option on) and `biome` for `web/`.
