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

- **Baseline**: MTA's latest subway feed, for 2026-11-01 to 2027-05-15
  (the Mobility Database's [`mdb-516-202610290031`](https://mobilitydatabase.org/feeds/gtfs/mdb-516)),
  with the 7 as it'll run once the Flushing Line's work is done,
  which has run the 7X local toward Manhattan, and the 7 past 52 St and 69 St toward Flushing, since 2023:
  the 7 and 7X of MTA's fall 2019 feed, after CBTC and before the work,
  from the Wayback Machine, with as many trains as now,
  quicker west of Queensboro Plaza wherever MTA's schedule now is.
- **QueensLink**: the baseline with QueensLink,
  made from the baseline by `src/subway_sheds/scenarios.py`:
  - The M turns off the Queens Blvd local at 63 Dr-Rego Park onto the old Rockaway Beach Branch,
    to Metropolitan Av-Parkside, Jamaica Ave, Atlantic Av-Woodhaven, and Liberty Av,
    then along the A to Broad Channel and on to Rockaway Park,
    instead of on to Forest Hills.
    It runs its whole route at all hours, with no shuttle between Middle Village and Myrtle Av.
    On weekends, it runs its weekday route, rather than only between Middle Village and Essex St,
    timed as the weekday M at midday, and as often as the F,
    leaving 23 St halfway between two Fs, so it alternates with the F and R.
    On weekdays, MTA's own Ms run as they do, and before and after them,
    late at night and early in the morning, Ms run halfway between two Fs too.
    The new track's times are simulated (see below), and the A's and the Rockaway Park shuttle's are MTA's,
    the A's as it runs in the same hour, as MTA allows it up to 4 minutes more some hours.
  - With the M running on weekends, the F runs via 53 St every day, as on weekdays, rather than via 63 St.
  - The G runs on past Court Sq on weekdays along the Queens Blvd local to Forest Hills, timed as the R.
  - The A's few rush hour trips to Rockaway Park run to Far Rockaway,
    and the Rockaway Park shuttle doesn't run, as the M always does.

  Every train added or moved runs at least 90 s from others on the same track, as MTA's own trains do,
  or 60 s under CBTC, as if MTA's current projects are done:
  the whole G, the E but the Archer Av branch, the F but W 8 St to Coney Island,
  the A from 59 St south, the M from Essex St on to Queens, and QueensLink's new track,
  or else counts as a conflict.
  The M is fitted with CP-SAT, by `src/subway_sheds/fit.py`, two hours at a time:
  each M runs up to 3 minutes earlier or later as a whole,
  and waits up to a minute, or makes up 30 s where it has time to spare,
  at Myrtle Av, Essex St, 21 St-Queensbridge, and Liberty Av,
  and the J, Z, F, R, A and E trains it shares track with run up to 60 s earlier or later,
  for the least cost, with a wait costing most,
  and gaps shorter than even costing too:
  between trains on each shared track, between each M and the M before it,
  where it also keeps at least 80% of its scheduled gap,
  and between each train nudged and its own route's trains before and after it.
  The F and G wait for a gap if they have to, for at most a minute,
  the F at Jackson Hts and 47-50 Sts, and the G at Court Sq.
  Where that isn't enough, the whole trip runs up to 5 minutes earlier or later instead,
  and if that's still not enough, the trains in its way run 30 s or 60 s earlier or later,
  if that keeps them clear of every other train;
  the G runs on only if it fits, and `fetch` says how far they move and how long they wait.
  The feeds are made by `fetch` and not committed.
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
  Changing trains between complexes walks the streets, out one entrance and in another,
  and the page draws those walks along the streets.
- **Platforms**: `mta-od-data`'s physical platforms from OSM, at the commit `route/Cargo.toml` pins,
  which time changing trains within a complex as its `raptor`'s OSM transfers do:
  free on the same platform, up and over to another, a flight a level between stacked ones.
  QueensLink's new stations are taken to have one island platform each.
- **QueensLink's new track**: scheduled times between its new stations,
  in [`data/queenslink/run_times.csv`](data/queenslink/run_times.csv),
  simulated from the track's curves and how trains run, by `src/subway_sheds/run_times/`.
  The curves off the Queens Blvd local at Rego Park are traced from QueensLink's
  [diagram](https://thequeenslink.org/wp-content/uploads/2023/07/QBL_RP.png),
  georeferenced against OSM's streets,
  and the rest from the old Rockaway Beach Branch in OSM,
  trimmed to the ways used in [`data/queenslink/osm.json`](data/queenslink/osm.json)
  (© OpenStreetMap contributors, ODbL).
  Trains accelerate and brake as R188s were recorded doing under CBTC on the elevated 7,
  up to 45 mph and each curve's limit,
  and each run is padded as MTA pads the Queens Blvd local's, with a 30 s dwell,
  fitted in [`data/queenslink/run_time_calibration.csv`](data/queenslink/run_time_calibration.csv):
  as its weekday median, 1.25 times the run, toward Manhattan at weekday rush hours,
  6 to 10 am and 3 to 8 pm, where the M merges into the local at its busiest,
  and otherwise as its quickest on any day, 1.05 times,
  as nothing's merged into toward Rockaway, and little off-peak.
  `subway-sheds run-times` rebuilds both, to review and commit.
- **Land**: `nyc-subway-isochrones`' NYC shoreline, which keeps the grid out of the water.

Travel times are routed from every station to a grid of about 21,500 cells over the city's land,
about 190 m across, on the Wednesday and the Saturday with the most trips scheduled.
Cells more than 250 m from anything to walk on, like marsh islands in Jamaica Bay, are left out.
Every station is routed to as well, to say how far it is itself,
rather than its cell's time, which counts walking to the cell's middle.

## Usage

`mise install` sets up everything else (Node and `uv`) but Rust,
whose `cargo` `uv` runs through `maturin` to build the `route` crate,
and the page builds its travel times in WebAssembly with, from `web/times/`,
which needs `rustup target add wasm32-unknown-unknown`.
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
uv run subway-sheds fetch    # download the MTA feeds, land, streets, and platforms, and make both feeds
uv run subway-sheds route    # route every station, in both scenarios
uv run subway-sheds export   # for the web page, into `web/public/data/`
npm --prefix web install
npm --prefix web run dev
```

`route` takes station IDs to route only those, e.g. `route H04 V02`,
replacing only their times,
and `--scenario`, `--day`, and `--fare` to route only those, e.g. `route --scenario queenslink --day weekday`.
Downloads go in `.cache/`, and what the pipeline computes, like the travel times, in `out/`.
Neither they nor the export are committed:
CI routes, exports, and publishes the page to GitHub Pages on every push to `main`.

The page times walking out of stations itself, at the speed its slider is set to, 1 to 5 mph:
`export` gives it when each station's trains reach every station each minute of the window,
and every station's walks out to the cells, up to an hour at 5 mph,
but only those some cell's quickest trip takes, from any station at any of the slider's speeds,
which `export` finds by running the page's own Rust (`web/times/`) through `route`,
those farther than half an hour at 5 mph loaded after the page first draws,
and those farther than an hour at 3 mph, where the slider starts, only once it's faster,
and it takes each cell's median over the minutes, as R5 does,
in a worker, in WebAssembly with SIMD (`web/times/`).
Walking to the first station and between stations stays at 3 mph.
Pointing at a place draws the walk there along the streets, out of the entrance it takes,
loading each station's walks out, simplified to within a meter, when a place's walk is from it,
and the farther ones only once the slider's faster than 3 mph.

## Development

`pre-commit install` runs the checks on commit, and CI runs them too:
`ruff`, `ty`, `pyrefly` (strict), and `pytest` for the pipeline,
and `tsrs` (with every strictness option on) and `biome` for `web/`.
