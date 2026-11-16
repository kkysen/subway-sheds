"""Generate the stations' screenshots for every map page in the built site."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.sync_api import ViewportSize, sync_playwright

VIEWPORT: ViewportSize = {"width": 1024, "height": 813}
PIXEL_RATIOS = (1, 2, 3)
TIMEOUT_MS = 60_000
QUIET_MS = 2_000
VIEWS = {
    "now": "baseline",
    "queenslink": "queenslink",
    "saved": "saved",
}


@dataclass(frozen=True)
class Shot:
    # The station's stop ID and name, as the page's summary says it.
    station: str
    name: str
    # The start of each screenshot's file name.
    slug: str
    day: str
    views: tuple[str, ...] = tuple(VIEWS)


SHOTS = [
    Shot("G19", "Steinway St", "steinway-st", "weekday"),
    Shot("H15", "Rockaway Park-Beach 116 St", "rockaway-park", "weekday"),
    Shot("G14", "Jackson Hts-Roosevelt Av", "jackson-hts", "weekday"),
    # Saturdays' too, when only QueensLink runs the M along the Queens Blvd local.
    Shot("G14", "Jackson Hts-Roosevelt Av", "jackson-hts-saturday", "saturday", ("saved",)),
]
PAGES = {
    "main": ("", Path("screenshots")),
    "queenslink": ("queenslink/", Path("queenslink/screenshots")),
}


def screenshot_url(page_url: str, shot: Shot, view: str) -> str:
    # The page reads station and controls from its URL fragment, independent of saved browser state.
    params = urlencode(
        [
            ("day", shot.day),
            ("measure", "saved" if view == "saved" else "time"),
            ("with", "" if view == "baseline" else "queenslink"),
            ("walking_speed_mph", "3"),
            ("fare", "single"),
            ("legend", "open"),
        ]
    )
    return f"{page_url}?explainer=off#{shot.station}&{params}"


def root_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/queenslink"):
        path = path[: -len("queenslink")]
    else:
        path = f"{path}/" if path else "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    # Given a page URL, use its existing dev server; otherwise preview the built site as CI does.
    argument = sys.argv[1] if len(sys.argv) > 1 else "/"
    existing_url = argument if argument.startswith(("http://", "https://")) else None
    base = argument if existing_url is None else "/"
    server: subprocess.Popen[bytes] | None = None
    try:
        if existing_url is None:
            port = free_port()
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
            base = f"{base.rstrip('/')}/"
            site_url = f"http://localhost:{port}{base}"
            output_root = Path("web/dist")
        else:
            site_url = root_url(existing_url)
            output_root = Path("web/public")
        for attempt in range(100):
            try:
                with urllib.request.urlopen(site_url):
                    break
            except OSError:
                if server is None or attempt == 99:
                    raise
                time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
            )
            for pixel_ratio in PIXEL_RATIOS:
                context = browser.new_context(
                    viewport=VIEWPORT,
                    device_scale_factor=pixel_ratio,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                for suffix, output_path in PAGES.values():
                    page_url = f"{site_url}{suffix}"
                    output = output_root / output_path
                    output.mkdir(parents=True, exist_ok=True)
                    for shot in SHOTS:
                        for filename in shot.views:
                            page.goto(
                                screenshot_url(page_url, shot, VIEWS[filename]),
                                wait_until="networkidle",
                                timeout=TIMEOUT_MS,
                            )
                            page.get_by_text(shot.name, exact=True).first.wait_for(
                                timeout=TIMEOUT_MS
                            )
                            page.wait_for_function(
                                "name => document.querySelector('#summary-status')"
                                ".textContent.includes(name)",
                                arg=shot.name,
                                timeout=TIMEOUT_MS,
                            )
                            page.wait_for_timeout(QUIET_MS)
                            path = output / f"{shot.slug}-{filename}-{pixel_ratio}x.png"
                            page.screenshot(path=str(path))
                            print(path)
                context.close()
            browser.close()
    finally:
        if server is not None:
            os.killpg(server.pid, signal.SIGTERM)
            server.wait()


if __name__ == "__main__":
    main()
