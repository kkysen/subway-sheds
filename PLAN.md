# Plan

## Binary images

Never commit binary images without explicit approval.

## Explainer pop-up

Done in `d658474`, `c3b94d7`, and `0e4bfa3`, but for checking it in real Safari,
and adding `embed.js` to queenslink.org's embed.
Safari clears what a site's scripts store after about 7 days without the visitor interacting with it,
so someone back after a while may see it again; that's fine.

A near-fullscreen pop-up on the web page
that explains what the site is and how it works.

- Show it the first time someone opens the site,
  including when it's embedded.
- Give it a "Don't show this again" button,
  so it stays closed on later visits.
  Closing it any other way only closes it for now:
  it shows again on the next load.
- Put a small button on the map that reopens it.
- Probably gear it to the page it's on (not decided yet):
  one pop-up component, with the parts about the map and how to use it shared,
  and the parts about what's proposed written for each page.
  The main page explains comparing changes like QueensLink and the IBX;
  `/queenslink` explains just QueensLink.
  Remember "Don't show this again" per page,
  so someone who closed the main page's still sees `/queenslink`'s.
- Remember "Don't show this again" when embedded too,
  as on queenslink.org, in every browser.
  Showing it on every visit there isn't acceptable.

### Remembering it when embedded

The page already keeps its controls in `localStorage`.
In an iframe on another site, browsers either partition that storage by the embedding site,
or, in Safari especially, may block it or clear it.
So don't rely on it alone, and use all three of these (decided):

1. Keep using the iframe's own `localStorage`, guarded with try/catch.
   Where it works, it's enough.
2. Give embedders a small script to paste beside the `<iframe>`.
   It keeps the flag in the embedding site's own `localStorage`, which is first party there,
   so it isn't blocked.
   It hands the flag to the iframe with `postMessage` once the iframe loads,
   and saves it when the iframe sends "Don't show this again" back.
   Both sides check the message's origin.
   We control the queenslink.org embed,
   so it goes in there when the embed is pointed at `/queenslink`.
3. Accept a URL parameter, e.g. `?explainer=off`,
   for an embedder who never wants the pop-up shown.
   Their own page can explain the map instead.

Until the script has answered, the iframe waits briefly before showing the pop-up,
so it doesn't flash open and then close.
Check all three in Chromium, Firefox, and WebKit with Playwright,
in an iframe on another origin.
The station key and legend choices kept in `localStorage` have the same problem,
so they can go through the same channel.

## A general main page and a QueensLink page

Restructure the site so the main page isn't only about QueensLink
and can show other changes too, like the IBX.
Move what's geared just toward QueensLink to its own `/queenslink` page.
The existing embed on queenslink.org should point to `/queenslink`.
`/queenslink` exists as of `00830fd`, the same as the main page for now,
so the embed can move there before the main page changes.
`embed/queenslink/wordpress.partial.html` points there, with `embed.js`, as of `00830fd`;
Khyber pastes it into WordPress once `/queenslink` and `embed.js` are published,
so the old URL needs no redirect.

## Generate the QueensLink icons

The QueensLink favicon and Apple touch icon PNGs in `web/public/` were copied from queenslink.org.
Generate these files during the build instead of committing the PNGs.

## Generated screenshots and a before/after slider for queenslink.org

queenslink.org/the-plan shows three hand-made screenshots of the map
(`ql_ss_1.jpg` to `ql_ss_3.jpg`, 1024×813 JPEGs, already out of date: titled "Subway Sheds"):
times from Steinway St at 8 am on a weekday without QueensLink and with it,
the two in a before/after slider, and the time saved, linking to the map.
The slider is the WordPress plugin `noo-before-after`:
the "before" image laid over the "after" one, CSS-`clip`ped to the handle's position,
dragged with `jquery.event.move`, in a `slick` carousel of two such sliders.

- `scripts/screenshots.py` generates the three Steinway St views for every map page as hi-DPI PNGs
  of the real page with Playwright after each CI build.
  The matching screenshots are served under `/main/screenshots/` and `/queenslink/screenshots/`;
  each view is generated at 1×, 2×, and 3× pixel density with matching dimensions for the slider.
- WordPress keeps doing the slider with its plugin; we only serve the images.

## A before/after slider on our own map (not decided)

A swipe between the map without QueensLink and with it, on the live map,
e.g. two synced MapLibre maps, one clipped to the handle, as `maplibre-gl-compare` does.

## Modeling an A and M swap between Far Rockaway and Rockaway Park

QueensLink's M runs to Rockaway Park and the A to Far Rockaway.
Model swapping them: the M to Far Rockaway and the A to Rockaway Park.

## Diverging `/queenslink` from the main page

Done in `976a844`, not pushed yet.

Once the main page and `/queenslink` are pushed and uploaded in sync,
`/queenslink` gets one control for what the map shows, `Time [Now | with (QL) | Saved]`,
in place of `Minutes [Time | Time Saved]` and the separate QueensLink toggle,
as the three are the only views that mean anything there.
The main page keeps its toggles, which work for several projects.
Update `/queenslink`'s explainer in the same change, as it names the controls as their buttons.

## Fitting every weekday G to Forest Hills

After pushing: 2 of the 298 weekday Gs still end at Court Sq,
those leaving it toward Brooklyn at 7:44 and 8:21 am,
as the greedy fit finds no gap among the Rs, Ms, and other Gs on the Queens Blvd local.
Fit the extended Gs in the M's CP-SAT solve instead, requiring every one to extend,
so the explainer's "The (G) is extended to Forest Hills on weekdays" holds for all of them.
