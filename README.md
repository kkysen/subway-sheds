# subway-sheds

How far you can get on the NYC subway in a given time, with and without [QueensLink](https://queenslink.org).

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
  - With the M running on weekends, the F runs via 53 St every day, as on weekdays, rather than via 63 St.
  - The G runs on past Court Sq on weekdays along the Queens Blvd local to Forest Hills, timed as the R.
  - The A's few rush hour trips to Rockaway Park run to Far Rockaway,
    and the Rockaway Park shuttle doesn't run, as the M always does.

  How it's scheduled is in [QueensLink schedule](#queenslink-schedule) below.
  The feeds are made by `scenarios` and not committed.
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
- **Tracks**: the OSM track ways each route runs along between each two stations it stops at in a row,
  in [`data/tracks.csv`](data/tracks.csv), from OSM's NYC Subway route relations
  (© OpenStreetMap contributors, ODbL),
  to tell which trains share a track, like the F and M on 6 Av's local, not the B and D's express.
  `subway-sheds tracks` rebuilds it to review and commit.
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

### QueensLink schedule

The QueensLink feed is the baseline with QueensLink's changes added:
MTA's current schedule, but for the full 7X being restored.
`_queenslink` in `src/subway_sheds/scenarios.py` makes it, in this order:

1. The A's rush hour trips to Rockaway Park are run to Far Rockaway instead.
2. The F's weekend trips are run via 53 St, and fitted (see [the F and G](#the-f-and-g)).
3. The M is rerouted and added to, as below.
4. The Rockaway Park shuttle's trips are dropped while the M runs there.
5. The M is fitted with CP-SAT (see [fitting the M](#fitting-the-m)).
6. The G's weekday trips are extended to Forest Hills, and fitted.
7. Trips now leaving before midnight are run the night before (see [past midnight](#past-midnight)).

`scenarios` prints how far trips of each route were moved, how long they wait,
and how many didn't fit or are in conflict.

#### The M

- **Weekdays during the day**, MTA's own M trips keep their times
  (but for how the fit moves them, at most a minute in the current feed),
  running from 63 Dr-Rego Park over the new track and along the A to Rockaway Park.
- **At other times**, the M runs its full route as frequent as the F:
  each new M leaves 23 St (6 Av) halfway between two Fs, so it alternates with the F and R.
  On weekdays, those fill in around MTA's own Ms,
  from midnight to about 6 am and from about 9 pm on.
  On weekends, they run the weekday route, rather than only between Middle Village and Essex St,
  timed as the weekday M at midday.
- **Along the A**, from Liberty Av to Broad Channel, it's timed as the A in the same hour,
  as MTA allows the A up to 4 minutes more some hours.

Downtown trains per hour at 23 St (6 Av), the F / today's M / QueensLink's M:

| Hour | Weekday | Saturday |
|---|---|---|
| Midnight to 5 am | 3–4 / 0 / 2–3 | 3–4 / 0 / 3 |
| 5 am | 4 / 2 / 5 | 4 / 0 / 3 |
| 6 am to 8 pm | 8–15 / 5–9 / the same as today | 4–5 / 0 / 4–5 |
| 9 pm | 7 / 2 / 7 | 5 / 0 / 5 |
| 10 to 11 pm | 6 / 0 / 5–6 | 5 / 0 / 4–5 |

Where the new Ms run, they match the F within one an hour;
an hour has one fewer only for which side of the hour a train falls on.

Franklin Tang's earlier QueensLink feed, routed here before `ec1eeca`,
ran the M at flat frequencies instead.
Trains per hour at Metropolitan Av-Parkside on weekdays, both directions:

| Hours | Franklin's | Ours (MTA's own Ms) |
|---|---|---|
| 5 to 6:30 am | 5 (every 12 min) | 3–5 / 5–6 |
| 6:30 to 9:30 am | 9 (every 6:40) | 6–9 / 7–10 |
| 9:30 am to 3:30 pm | 6 (every 10 min) | 7–8 |
| 3:30 to 8:30 pm | 9 (every 6:40) | 7–9 / 6–8 |
| 8:30 pm to midnight | 6 (every 10 min) | 6–8 / 5–6 |
| Midnight to 5 am | 3 (every 20 min) | one between each two Fs |

So ours runs a little less often at rush hours, as MTA's peak M runs every 7 to 8 minutes,
and more often at midday.
His weekend M ran every 12 minutes at the morning and evening peaks and every 15 otherwise;
ours runs as frequent as the F.
We keep MTA's own trips because they're what the M actually runs today,
and the fit then only has to fit them, not invent new ones.

#### Run times on the new track

The new track's times are simulated from its curves,
in [`data/queenslink/run_times.csv`](data/queenslink/run_times.csv), as under **QueensLink's new track** above:
trains accelerate and brake as R188s were recorded doing under CBTC on the elevated 7,
and each run is padded as MTA pads the Queens Blvd local's, which also runs under CBTC.
The M runs R160s, not R188s,
and a fit to an R160's recorded acceleration and braking was tried, from a ride on the M,
but these stay R188 curves until there's a recording of an F on lower Culver, now under CBTC,
as the M would run under CBTC on the new track.

#### Keeping trains apart

Every train added or moved runs at least 90 s from others on the same track, as MTA's own trains do,
or 60 s under CBTC, as if MTA's current projects are done:
the whole G, the E but the Archer Av branch, the F but W 8 St to Coney Island,
the A from 59 St south, the M from Essex St on to Queens, and QueensLink's new track,
or else counts as a conflict.
Two of MTA's own trains can stay as near as MTA's schedule already runs them, where that's nearer.

`scenarios` fails, writing no QueensLink feed, if any train isn't kept apart from others:
an F via 53 St that doesn't fit, an M in conflict,
or a train run the night before too near that night's trains.
A G that doesn't fit only counts, as it keeps its current run.

#### Fitting the M

The M is fitted by a constraint satisfiability (CP-SAT) solver,
OR-Tools' CP-SAT through `route/src/sat.rs`, from `src/subway_sheds/fit.py`.

The tracks the M shares are:
the Broadway local from Myrtle Av to Marcy Av, the Williamsburg Bridge, the 6 Av local, 63 St,
the Queens Blvd local, and the Rockaway Beach Branch north and south of the new track.

What it may change, in steps of 30 s:

- Each M runs up to 3 minutes earlier or later as a whole,
  and at each of its junctions, Myrtle Av, Essex St, 21 St-Queensbridge, and Liberty Av,
  waits up to a minute, or makes up 30 s,
  only where it runs slower than any train runs that stretch, or dwells there.
  A wait moves the rest of the trip after it toward Rockaway Park,
  and the part before it toward Middle Village.
- Each J, Z, F, R, A, and E train on a track the M shares
  is nudged up to 60 s earlier or later as a whole.
- Every other train stays as it is.

What must hold, or else counts as a conflict, costing far more than anything else:

- Each pair of trains on a shared track that could come near each other
  runs in one order, not overtaking between the platforms they share,
  and stays the minimum headway apart at each of them.
  Two of MTA's own trains are kept no nearer than MTA has them, where that's under the minimum,
  and where MTA's schedule has one pass the other, as the F passes the R around Jackson Hts late on weekends,
  they keep MTA's order at each platform.
- An M toward Middle Village crosses the Manhattan-bound J and Z tracks at grade past Myrtle Av,
  so it stays 90 s from each J and Z there.
- Each train nudged stays clear of every other train at every platform it stops at,
  those coming from the same station before it or going on to the same one after,
  in the same order, by the minimum headway, or by as much as MTA's own schedule does where that's less.

What must always hold:

- On the 6 Av and Queens Blvd locals, at most 4 trains leave each platform in any 8 minutes,
  or as many as MTA's own schedule already does, where that's more.
- The Ms in each direction keep their order,
  and at each platform at least the minimum headway and 80% of their scheduled gap to the M before.

What it costs, for each 30 s:

| What | Cost |
|---|---|
| An M moved as a whole | 1 |
| An M waiting at a junction | 4 |
| An M making up time at a junction | 3 |
| Another train nudged | 4 |
| Two trains on a shared track short of an even gap | 2 |
| An M short of its scheduled gap to the M before, at a junction or the platform before it | 3 |
| A train nudged short of MTA's gap to its own route's trains before and after it | 2 |
| A conflict | 1000, once |

A pair's even gap is an hour over how many trains leave the first platform they share that hour,
at most 10 minutes, and at most three minimum headways but on the 6 Av and Queens Blvd locals;
two of MTA's own trains only count for being nearer than MTA has them.
Among schedules costing the same, it picks by a weight hashed from each trip's ID,
so that solving again picks the same one.

It's solved two hours at a time, an hour apart, for at most 30 s each on 8 workers,
keeping each window's trains first on a shared track in its first hour,
and the trains kept apart from those Ms,
and solving the rest again with the next hour in view.
`scenarios` counts the windows not proven best in the time,
whose schedules may differ from one solve to the next.

#### The F and G

The F's weekend trips via 53 St and the G's extended trips are fitted one at a time, in order,
each among the trains already there, by `_fit` in `src/subway_sheds/scenarios.py`:

1. It waits for a gap where it joins other trains, for at most a minute:
   the F at Jackson Hts and 47-50 Sts, and the G at Court Sq.
2. Where that isn't enough, the whole trip runs up to 5 minutes earlier or later, the least it can.
3. For the G, where that's still not enough,
   the trains in its way run 30 s or 60 s earlier or later,
   if that keeps them clear of every other train.

The F is timed as the E through 53 St, and must fit (see [keeping trains apart](#keeping-trains-apart)).
The G is timed as the R along the Queens Blvd local, and runs on only where it fits;
where it doesn't, it keeps its current run, ending at Court Sq.
In the current feed, that's 2 of the 298 weekday Gs,
those leaving Court Sq toward Brooklyn at 7:44 and 8:21 am,
as the Rs, Ms, and other Gs toward Manhattan on the Queens Blvd local then leave them no gap.

#### Past midnight

GTFS has no times before a service day's start,
so a trip now leaving before midnight,
as the 3 Ms leaving Rockaway Park just before midnight,
and the G from Forest Hills that reaches Court Sq at 12:14 am on weekdays,
is run the night before instead:
past 24:00, on a service of its own, `night-before-` its own,
running each night before its own service runs.
The Ms are fitted only among their own service's trains,
and the G also among the trains of every service running the night before any day it runs, from 9 pm,
as on holidays a Saturday schedule runs some nights before a weekday's.
So `_night_conflicts` then counts the trains running that night each comes nearer than the minimum headway to,
on the same track:
the services running that night, and the next morning's trains before 24:00.

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
uv run subway-sheds fetch      # download the MTA feeds, land, streets, and platforms
uv run subway-sheds scenarios  # make both scenarios' feeds
uv run subway-sheds walks      # walk the streets every station's walks take, and find the grid
uv run subway-sheds route      # route every station, in both scenarios
uv run subway-sheds export     # for the web page, into `web/public/data/`
bun install --cwd web
bun run --cwd web dev
```

`scenarios` makes the feeds again only when the code or data they're made from changes,
as hashed in `out/gtfs/inputs.sha256`, or with `--force`.
`walks` likewise walks the streets again only when they, the stations, or the code walking them change,
and `route` and `export` walk them first if they have to.

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

### Embedding

To embed the map on another site, paste the whole of one of the embeds of QueensLink's page,
in [`embed/queenslink/`](embed/queenslink/):

- [`generic.partial.html`](embed/queenslink/generic.partial.html), for any site.
- [`wordpress.partial.html`](embed/queenslink/wordpress.partial.html),
  always pasted from here into [queenslink.org/map](https://queenslink.org/map/).

Their comments say what they do and how to change them.

## Development

`pre-commit install` runs `scripts/e2e.sh --browser` on push, on each commit pushed,
and the checks on commit, which CI runs too:
`ruff`, `ty`, `pyrefly` (strict), and `pytest` for the pipeline,
and `tsrs` (with every strictness option on) and `biome` for `web/`.
