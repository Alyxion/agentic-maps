"""Server-side screenshot of the live map runtime.

Not a from-scratch renderer: `RenderService` drives the SAME `web/map.js`
that draws the interactive map, inside a real headless-Chromium tab, and
screenshots the `#map` element. Pixel parity with what a person sees is the
entire point — a second, independent rendering code path would drift from
the first the moment either one changes, silently, the first time someone
touched one but not the other.

Design decision — how Playwright reaches the map runtime: this server
process (the same one that already serves `MapsApi` and the static `web/`
bundle, e.g. via `devserver.py`) is pointed to by Playwright over real HTTP
(`{base_url}/render.html?token=...`), NOT a `file://` URL and NOT a second,
separate server. `map.js` fetches tiles/vectors/glyphs/sprites from paths
relative to its own origin (`/api/v1/maps/...`); nothing else can resolve
those, and standing up a second process just to serve one static page would
duplicate the exact routing `MapsApi` already provides. The token in the URL
is minted by `MapsApi.build_payload()` + `MapsApi._store_render_payload()`
(see `rest/maps_api.py`) and resolves, server-side, to the `MapPayload` this
render is for — `render.html` fetches it the same way `index.html`/
`view.html` fetch theirs (`data-agentic-spec-url`), just pointed at a
per-request URL instead of a fixed one.

Playwright is an optional dependency (`pip install agentic-maps[render]`,
plus `playwright install chromium` once): importing this module never
requires it — only calling `render()` does — so the core package stays
importable without a browser installed, matching how `storage/fallback.py`
treats Pillow.
"""

from __future__ import annotations

from .params import playwright_context_options, screenshot_options, validate_render_params

# One page load + tile settle for a fresh view rarely takes anywhere near
# this; a hard ceiling beats a request hanging forever on a style that never
# finishes loading (a broken tile source, a stalled network, an offline spec
# with nothing cached for that area).
READY_TIMEOUT_MS = 45_000


class RenderUnavailable(RuntimeError):
    """Playwright, or a Chromium binary, is not installed.

    Raised instead of letting an `ImportError` or a raw Playwright launch
    error escape — `rest/maps_api.py` turns this into a clean HTTP 501
    ("not implemented on this deployment") rather than a stack trace.
    """


class RenderService:
    """Screenshots one map view via a real headless-Chromium page.

    Launches a fresh browser for every `render()` call. This is a deliberate
    v1 choice, not an oversight: a pooled/reused browser process is a real
    future optimisation (see docs/rendering.md's upgrade path), but it also
    means a crashed or wedged page can poison every render after it, and
    nothing about the current `POST /render` endpoint needs that complexity
    yet — it is built for on-demand, low-volume use (docs/thumbnails/print),
    not a render farm. `tools/verify_render.py` launches the same way, once
    per check, for the same reason.

    `base_url` MUST point at a running instance of the server that mounts
    this same `MapsApi` (and therefore serves `web/render.html` and
    `/api/v1/maps/render/payload/{token}`) — usually
    `http://127.0.0.1:8095` for the isolated dev server, or wherever a host
    application's own instance listens.
    """

    def __init__(self, *, base_url: str, chromium_executable_path: str | None = None):
        self.base_url = base_url.rstrip("/")
        # Advanced/testing knob: pin a specific Chromium build instead of
        # whatever `playwright install` most recently set up. Production
        # deployments should leave this unset. (Also how this feature was
        # verified locally against an already-cached browser build without
        # triggering a new download — see docs/rendering.md.)
        self.chromium_executable_path = chromium_executable_path

    async def render(
        self,
        *,
        token: str,
        width: int,
        height: int,
        scale: int = 1,
        format: str = "png",
        quality: int = 85,
    ) -> bytes:
        """Render the `MapPayload` behind `token` and return the encoded image bytes.

        `token` is minted server-side by `MapsApi` and resolves to a
        `MapPayload` when `web/render.html` fetches it — this method never
        builds or sees a `MapSpec` itself, it only knows the URL to point a
        browser at.
        """
        validate_render_params(width, height, scale, format, quality)

        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise RenderUnavailable(
                "Playwright is not installed — install the 'render' extra "
                "(pip install agentic-maps[render]) and run "
                "`playwright install chromium` once."
            ) from error

        url = f"{self.base_url}/render.html?token={token}"
        launch_kwargs: dict = {}
        if self.chromium_executable_path:
            launch_kwargs["executable_path"] = self.chromium_executable_path

        # Playwright's own `Error`/`TimeoutError` are plain `Exception`
        # subclasses (not `RuntimeError`), so every step below is wrapped —
        # not just `launch()` — and re-raised as one of this module's two
        # exception types. Without this, a mid-render failure (a style that
        # never settles, a page crash) would escape as a raw Playwright
        # exception the REST layer's `except (RuntimeError, ValueError)`
        # never expected, and surface as an unhandled 500 instead of a clean
        # 502/501.
        async with async_playwright() as pw:
            browser = None
            try:
                try:
                    browser = await pw.chromium.launch(**launch_kwargs)
                except PlaywrightError as error:
                    raise RenderUnavailable(
                        f"Chromium browser binary not available: {error}"
                    ) from error
                page = await browser.new_page(**playwright_context_options(width, height, scale))
                await page.goto(url, wait_until="load")
                # Same polling idiom as tools/verify_*.py (`page.wait_for_function`
                # against the settled()/agenticMapsSettled() contract map.js
                # exposes) rather than a fixed sleep — see map.js's own
                # `settled()` docstring for why a fixed wait loses this race.
                await page.wait_for_function(
                    "window.__agenticMapsReady && window.__agenticMapsReady()",
                    timeout=READY_TIMEOUT_MS,
                )
                element = await page.query_selector("#map")
                if element is None:
                    raise RuntimeError("render.html has no #map element to screenshot")
                return await element.screenshot(**screenshot_options(format, quality))
            except RenderUnavailable:
                raise
            except PlaywrightError as error:
                raise RuntimeError(f"render failed: {error}") from error
            finally:
                if browser is not None:
                    await browser.close()
