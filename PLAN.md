# Plan

## Explainer pop-up

Done, and live on queenslink.org's embed with `embed.js`,
but for checking it in real Safari, on an iPhone or a Mac.
Playwright's WebKit passes on queenslink.org/map,
but doesn't apply Safari's own rules for storage in an iframe on another site.
Safari clears what a site's scripts store after about 7 days without the visitor interacting with it,
so someone back after a while may see it again; that's fine.

## A before/after slider on our own map (not decided)

A swipe between the map without QueensLink and with it, on the live map,
e.g. two synced MapLibre maps, one clipped to the handle, as `maplibre-gl-compare` does.

## Modeling an A and M swap between Far Rockaway and Rockaway Park

QueensLink's M runs to Rockaway Park and the A to Far Rockaway.
Model swapping them: the M to Far Rockaway and the A to Rockaway Park.
Paused for now.

- A full swap, at all hours: every A to the Rockaways runs to Rockaway Park, none to Far Rockaway.
- The M to Far Rockaway is straightforward, as QueensLink's M runs at least as often as the A to Far Rockaway
  in every part of the day, weekdays and Saturdays, e.g. 7.8 vs 6.0 tph 6-10 am on weekdays.

## Fitting every weekday G to Forest Hills

2 of the 298 weekday Gs still end at Court Sq,
those leaving it toward Brooklyn at 7:44 and 8:21 am,
as the greedy fit finds no gap among the Rs, Ms, and other Gs on the Queens Blvd local.
Fit the extended Gs in the M's CP-SAT solve instead, requiring every one to extend,
so the explainer's "The (G) is extended to Forest Hills on weekdays" holds for all of them.

## Additional operating costs per scenario

For each scenario, compute what it costs to run beyond the baseline, from its feed,
over the whole of a weekday, a Saturday, and a Sunday, a week of them, and a year:

- Train-hours and car-hours in service, from each trip's first departure to its last arrival,
  with each route's cars per train.
- Train-miles and car-miles, along each trip's shape.
- Crew hours, a train operator and a conductor per train-hour,
  but one-person operation where MTA runs it.
- Dollars, from MTA's own cost per car-mile or per revenue-hour,
  with the source and year given beside it.

Show them on the page or in the explainer per scenario, beside the time saved.

## Additional fleet capital cost per scenario

Separately from operating costs, compute the capital cost of the cars each scenario needs:

- Peak trains in service at once per route, and so cars, with each route's cars per train,
  plus MTA's spare ratio, against today's fleet by car class
  in MTA's Rail Fleet Asset Management Plan (Tables 12 and 15),
  for how many new cars are needed, if any.
- Where those trains are stored overnight: which yard or lay-up area serves each route,
  against each one's capacity (Tables 35 and 36),
  for where there's room for the extra trains and where there isn't.
- Dollars, from the unit price of MTA's latest car order,
  with the source and year given beside it,
  and yard expansion where there isn't room, if a cost for that can be found.

## Riders who save or lose time, from the OD data

Weight each scenario's station-to-station times by MTA's subway origin-destination ridership estimates,
as `mta-od-data` reads them, by complex, day, and hour, to compute:

- How many riders save time, and how many lose time, each beyond some threshold, e.g. 1 min.
- The rider-minutes saved per ride, over all rides, and over only those that change.
- The same by origin and destination, to show where the gains and losses are.

Total them over the whole day and the whole week, not just the map's hours:
route every hour of a weekday, a Saturday, and a Sunday, as the OD data has them,
rather than only the map's 8 am on weekdays and 12 pm on Saturdays.
Rides only start and end at existing stations in the OD data,
so riders new to QueensLink's stations aren't counted;
say so beside the numbers rather than estimate them.

## Picking From and To

Click or tap a station to pick it as From, the origin, as today.
Double-click or double-tap anywhere, a station or any other point, to lock it in as To, the destination,
showing the trip there and its time.
This replaces the long press, so it works the same with a mouse and a touchscreen.
A single click anywhere but a station still locks that point in as To, as it does today.

- MapLibre's own double-click and double-tap zoom (`doubleClickZoom`) is turned off for this.
  What it does has better ways anyway:
  pinching or the scroll wheel, a trackpad's pinch, `+` and `-` on the keyboard,
  and double-tapping and dragging up or down (`tapDragZoom`, under `touchZoomRotate`), which stays.
  Lost with it are a two-finger tap to zoom out, and Shift-double-click to zoom out.
- A single click waits for a possible second click before picking From,
  so start loading the station's times on the first click (`prefetchTimes`) to hide the wait.
- On desktop, hovering keeps showing the trip to wherever the mouse is, until To is locked in;
  then the trip to To stays shown, and hovering only changes the cursor.
  Clearing To brings hovering back.

Show two fields at the top: From, always, and To, once one is locked in, with a × to clear it.
They're read only: clicking either one flies the map to its point.
Typing a station or an address into them may come later.

## Faster screenshots

`scripts/screenshots.py` takes 72 screenshots in CI,
as SwiftShader draws the map in software.

- Render only at 3×, and scale each shot down to 2× and 1×,
  as the layout is the same at every pixel ratio.
  Compare a scaled-down shot with a natively drawn one first, for thin lines and text.
- Skip the step when nothing it depends on changed (`web/`, the data, and the script),
  reusing the last screenshots from a cache.
