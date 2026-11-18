"""
Opens the page built in `web/dist/`, by `scripts/e2e.sh` or CI, served as `vite preview` serves it,
under the base path given, if any, as CI builds it for GitHub Pages,
and QueensLink's page, at `queenslink/`,
in headless Chromium, with the station given selected,
and checks that it loads that station's routes and logs no errors.
Its Chromium is Playwright's, installed with `uv run playwright install chromium`.
"""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

# How long the page has to load and work out the station's times, at most.
TIMEOUT_MS = 60_000


# The main page, and QueensLink's (see `web/vite.config.ts`),
# each by the name of the inputs for what the map shows, which only it has:
# the measure and projects on the main page, and one choice of view on QueensLink's.
PAGES = {"": "measure", "queenslink/": "view"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    station = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) > 2 else "/"
    port = free_port()
    # Vite itself, not through `npx`, which would leave it running when stopped,
    # in a process group of its own, to stop with whatever it starts,
    # and not writing where this does, which would keep `scripts/e2e.sh`'s output open.
    server = subprocess.Popen(
        [
            "node_modules/.bin/vite",
            "preview",
            "--port",
            str(port),
            "--strictPort",
            f"--base={base}",
        ],
        cwd="web",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        url = f"http://localhost:{port}{base}"
        for _ in range(100):
            try:
                urllib.request.urlopen(url)
                break
            except OSError:
                time.sleep(0.1)
        errors = [
            e
            for page, control in PAGES.items()
            for e in check(f"{url}{page}#{station}", station, control)
        ]
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait()
    # A file of its own, as GitHub Pages serves, which `vite preview` would serve `index.html` for.
    errors += [
        f"built no web/dist/{page}index.html"
        for page in PAGES
        if not os.path.exists(f"web/dist/{page}index.html")
    ]
    for error in errors:
        print(error, file=sys.stderr)
    sys.exit(1 if errors else 0)


def check(url: str, station: str, control: str) -> list[str]:
    errors: list[str] = []
    routes: list[int] = []
    with sync_playwright() as p:
        # The map draws with WebGL, which headless Chromium has only in software.
        browser = p.chromium.launch(
            args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on(
            "console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None
        )
        page.on("pageerror", lambda e: errors.append(f"page error: {e}"))
        page.on("requestfailed", lambda r: errors.append(f"failed to load {r.url}"))
        page.on(
            "response",
            lambda r: routes.append(r.status) if r.url.endswith(f"/routes/{station}.bin") else None,
        )
        page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
        # Once the map has drawn this URL's times (`markDrawn` in `main.ts`).
        page.wait_for_function(
            "() => document.documentElement.dataset.drawn === location.hash",
            timeout=TIMEOUT_MS,
        )
        # Its routes as first loaded, before the reloads below load them again.
        loaded = list(routes)
        # Opened for the first time, as in a new profile, so the explainer's shown.
        if not page.evaluate("document.querySelector('#explainer').open"):
            errors.append("didn't show the explainer on first opening the page")
        else:
            # Dismissed, it stays hidden on reloading, but only until its text changes,
            # as when only an older version was dismissed.
            page.click("#explainer-dismiss")
            page.reload(wait_until="networkidle", timeout=TIMEOUT_MS)
            if page.evaluate("document.querySelector('#explainer').open"):
                errors.append("showed the explainer again after it was dismissed")
            page.evaluate(
                """() => {
                    for (const key of Object.keys(localStorage)) {
                        if (key.startsWith("subway-sheds:explainer-")) {
                            const older = key.replace(/-[0-9a-f]{8}$/, "-00000000");
                            localStorage.setItem(older, "dismissed");
                            localStorage.removeItem(key);
                        }
                    }
                }"""
            )
            page.reload(wait_until="networkidle", timeout=TIMEOUT_MS)
            if not page.evaluate("document.querySelector('#explainer').open"):
                errors.append("didn't show the explainer again once its text changed")
        for name in set(PAGES.values()):
            has = page.locator(f"input[name={name}]").count() > 0
            if has != (name == control):
                errors.append(f"{'has' if has else 'has no'} {name} inputs on {url}")
        browser.close()
    if loaded != [200]:
        errors.append(f"loaded {station}'s routes with {loaded or 'no'} responses, not one 200")
    return errors


if __name__ == "__main__":
    main()
