"""Generate the stations' screenshots for every map page in the built site."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from PIL import Image, features
from playwright.sync_api import ViewportSize, sync_playwright

VIEWPORT: ViewportSize = {"width": 1024, "height": 813}
# Drawn at 3x only, as the map is drawn in software, and scaled down to the others,
# each with the filter that came out closest to drawing it natively:
# box at 1x, averaging each 3x3 block, and Lanczos at 2x, where each pixel spans 1.5 of 3x's.
PIXEL_RATIO = 3
SCALED: dict[int, Image.Resampling] = {
    2: Image.Resampling.LANCZOS,
    1: Image.Resampling.BOX,
}
# Saved as AVIF, with full-resolution color (4:4:4),
# as 4:2:0 smears the text and the thin colored lines.
# At quality 80, over every shot, it came out a fifth to a quarter of the PNGs' size,
# with the controls' text at least 46 dB PSNR against the PNG at each ratio,
# and the faint band outlines keeping at least 99% of their darkness.
FORMAT = "avif"
QUALITY = 80
TIMEOUT_MS = 60_000
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


SHOTS = [
    Shot("G19", "Steinway St", "steinway-st", "weekday"),
    Shot("H15", "Rockaway Park-Beach 116 St", "rockaway-park", "weekday"),
    Shot("G14", "Jackson Hts-Roosevelt Av", "jackson-hts", "weekday"),
    # Saturdays' too, when only QueensLink runs the M along the Queens Blvd local.
    Shot("G14", "Jackson Hts-Roosevelt Av", "jackson-hts-saturday", "saturday"),
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


def save(png: bytes, output: Path, name: str) -> None:
    """Save the shot drawn at 3x, and scaled down to each other ratio, as `FORMAT`."""
    with Image.open(BytesIO(png)) as image:
        image.load()
        path = output / f"{name}-{PIXEL_RATIO}x.{FORMAT}"
        image.save(path, quality=QUALITY, subsampling="4:4:4")
        print(path)
        for ratio, resample in SCALED.items():
            size = (VIEWPORT["width"] * ratio, VIEWPORT["height"] * ratio)
            path = output / f"{name}-{ratio}x.{FORMAT}"
            image.resize(size, resample).save(path, quality=QUALITY, subsampling="4:4:4")
            print(path)


def main() -> None:
    # Before drawing anything, as only some builds of Pillow can write AVIF;
    # the wheels `uv.lock` pins can.
    if not features.check(FORMAT):
        sys.exit(f"this Pillow can't write {FORMAT.upper()}")
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
        # Each shot is encoded in the background while Chromium draws the next one.
        # One thread is enough, as the AVIF encoder runs on every core itself.
        with sync_playwright() as playwright, ThreadPoolExecutor(max_workers=1) as encoder:
            saves: list[Future[None]] = []
            browser = playwright.chromium.launch(
                args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
            )
            context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=PIXEL_RATIO,
                reduced_motion="reduce",
            )
            page = context.new_page()
            for suffix, output_path in PAGES.values():
                page_url = f"{site_url}{suffix}"
                output = output_root / output_path
                output.mkdir(parents=True, exist_ok=True)
                for shot in SHOTS:
                    for filename, view in VIEWS.items():
                        # Stop at the first save that failed, rather than drawing the rest.
                        for saved in saves:
                            if saved.done():
                                saved.result()
                        page.goto(
                            screenshot_url(page_url, shot, view),
                            wait_until="networkidle",
                            timeout=TIMEOUT_MS,
                        )
                        page.get_by_text(shot.name, exact=True).first.wait_for(timeout=TIMEOUT_MS)
                        page.wait_for_function(
                            "name => document.querySelector('#summary-status')"
                            ".textContent.includes(name)",
                            arg=shot.name,
                            timeout=TIMEOUT_MS,
                        )
                        # Once the map has drawn this URL's times (`markDrawn` in `main.ts`).
                        page.wait_for_function(
                            "() => document.documentElement.dataset.drawn === location.hash",
                            timeout=TIMEOUT_MS,
                        )
                        png = page.screenshot(timeout=TIMEOUT_MS)
                        saves.append(encoder.submit(save, png, output, f"{shot.slug}-{filename}"))
            context.close()
            browser.close()
            # Every save, raising the first error, if any.
            for saved in saves:
                saved.result()
    finally:
        if server is not None:
            os.killpg(server.pid, signal.SIGTERM)
            server.wait()


if __name__ == "__main__":
    main()
