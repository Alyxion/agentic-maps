"""Play a page through every state it can reach and write down what it asked for.

A `MapSpec`-driven package (`package/builder.py`) knows its own tile set: the
planner reads camera stops and fly corridors straight out of the spec. A
page that OWNS its choreography in JavaScript — a kiosk view, a docs figure,
anything driven by `?lat=&lon=&z=` query params or its own internal state
machine — has no such spec to read. Nothing outside the page's own script
knows what it will ask for.

So this module answers the question empirically: it opens the page exactly as
a viewer would, drives it through every state it declares, and records every
same-host resource the map runtime requests. What comes out is the honest
floor — the page's real appetite, with nothing added for a state nobody ever
reaches.

## The readiness/step contract (docs/sealed-sessions.md)

This product defines and controls ONE contract a page implements to become
sealable — no protocol borrowed from any particular host or presentation
tool:

- `window.__agenticMapsReady()` — already exposed by every page that mounts
  `map.js` (`agenticMapsSettled`/`__agenticMapsReady`, see `web/map.js`). The
  recorder waits for this before considering the page "up", and again after
  every step. A page that layers extra ordered work on top (an image overlay
  that must be in the style, say) wraps this the same way `view.html` does —
  see that file's own `window.__agenticMapsReady` override.
- `window.__agenticSealSteps` — OPTIONAL. An array of functions (sync or
  async) a page defines when it has more than one state worth recording. The
  recorder calls each in order, index first, and waits for
  `__agenticMapsReady()` again after every call — an async function that
  `await`s its own animation is exactly how a page expresses "give this
  state time to settle" (Playwright awaits whatever the function returns). A
  page without this array is simply recorded once, in whatever state it
  reaches on its own after load.

Both directions of a reversible choreography, or an ambient idle drift after
the last frame, are the PAGE's call to make, not the recorder's: if a state
is worth recording, it is worth one more entry in `__agenticSealSteps` — the
recorder itself carries no opinion about flight direction, animation timing,
or view cones, unlike the host-specific reference it was ported from.
"""

import asyncio
import json
from typing import Iterable

from ..models.capture_report import CaptureReport
from ..models.embed_shot import EmbedShot
from ..models.sealed_route import SealedRoute

# How long the recorder waits for `__agenticMapsReady()` to turn true, and how
# much slack it gives a page after that before moving on — tiles requested
# during a `flyTo` are captured throughout the wait, not just at the end, so
# this only needs to outlast the slowest realistic transition, not model one.
READY_TIMEOUT_MS = 30000
SETTLE_MS = 1500

# Viewport variants: (scale, aspect_bias, device_scale). The authored box comes
# first; the rest are the margin that keeps a bigger kiosk screen, an
# ultrawide panel, or a retina display from finding a hole the recording never
# saw. Small and explicit on purpose — every variant is tiles in the file.
VIEWPORT_VARIANTS: tuple[tuple[float, float, int], ...] = (
    (1.00, 1.00, 1),   # exactly as authored
    (1.35, 1.00, 1),   # a bigger stage
    (1.15, 1.35, 1),   # a wider aspect (ultrawide / 21:9 kiosk)
    (1.00, 1.00, 2),   # retina: same box, twice the tiles
)

# Live, per-request chatter a sealed page must never need: attribution is
# baked into the bundle's own per-zoom table (see sealer.py) and mode has no
# meaning once there is no server to switch. Suffixes of `map_prefix`.
DEFAULT_SKIP_SUFFIXES = ("attribution", "mode")


class SessionRecorder:
    """Drives a page in a headless browser and reports what it requested.

    `map_prefix` names the one mount point (`MapsApi.mount(prefix=...)`,
    default `/api/v1/maps/`) whose traffic is recorded wholesale except for
    the routing POST (keyed by body, see `route_key` below) and the
    `skip_suffixes` paths. `asset_prefixes` is deliberately empty by
    default — this module bakes in no assumption about where a host serves
    its non-map assets (icons, photos, a vendored library fetched at
    runtime rather than inlined by `PageSealer`); a caller whose page reaches
    for such assets passes their prefixes explicitly. agentic-maps' own
    pages currently need none: everything `view.html`/`index.html` fetch at
    runtime already lives under `map_prefix`.
    """

    def __init__(
        self,
        host: str,
        *,
        variants: Iterable[tuple[float, float, int]] | None = None,
        concurrency: int = 3,
        map_prefix: str = "/api/v1/maps/",
        asset_prefixes: Iterable[str] = (),
        skip_suffixes: Iterable[str] = DEFAULT_SKIP_SUFFIXES,
        chromium_executable_path: str | None = None,
    ):
        self.host = host.rstrip("/")
        self.variants = tuple(variants) if variants is not None else VIEWPORT_VARIANTS
        self.concurrency = concurrency
        self.map_prefix = map_prefix if map_prefix.endswith("/") else map_prefix + "/"
        self.asset_prefixes = tuple(asset_prefixes)
        self.route_path = self.map_prefix + "route"
        self.skip_paths = tuple(self.map_prefix + suffix for suffix in skip_suffixes)
        # Advanced/testing knob, same shape as `render/service.py`'s own
        # `chromium_executable_path` — pin a specific Chromium build instead
        # of whatever `playwright install` most recently set up. The env var
        # a caller typically reads this from (`AGENTIC_MAPS_RENDER_CHROMIUM_
        # PATH`) is `tools/seal_page.py`'s concern, not this class's — the
        # core stays framework/env-free, same split `MapsApi`/`RenderService`
        # already follow.
        self.chromium_executable_path = chromium_executable_path

    async def record(self, shots: list[EmbedShot]) -> CaptureReport:
        from playwright.async_api import async_playwright

        keys: set[str] = set()
        routes: dict[str, SealedRoute] = {}
        missing: set[str] = set()
        jobs = [(shot, variant) for shot in shots for variant in self.variants]
        gate = asyncio.Semaphore(self.concurrency)

        launch_kwargs: dict = {}
        if self.chromium_executable_path:
            launch_kwargs["executable_path"] = self.chromium_executable_path

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                async def run(shot: EmbedShot, variant: tuple[float, float, int]) -> None:
                    async with gate:
                        await self._play(browser, shot, variant, keys, routes, missing)

                await asyncio.gather(*(run(shot, variant) for shot, variant in jobs))
            finally:
                await browser.close()

        return CaptureReport(
            keys=sorted(keys),
            routes=[routes[k] for k in sorted(routes)],
            embeds=[shot.label for shot in shots],
            viewport_variants=len(self.variants),
            missing=sorted(missing),
        )

    # -- one page, one viewport -------------------------------------------

    async def _play(self, browser, shot: EmbedShot, variant: tuple[float, float, int],
                    keys: set[str], routes: dict[str, SealedRoute], missing: set[str]) -> None:
        scale, aspect, dpr = variant
        context = await browser.new_context(
            viewport={"width": max(120, int(shot.width * scale * aspect)),
                      "height": max(120, int(shot.height * scale))},
            device_scale_factor=dpr,
        )
        page = await context.new_page()
        pending: list = []

        def on_request(request) -> None:
            key = self._key_of(request.url)
            if key:
                keys.add(key)

        async def on_response(response) -> None:
            key = self._key_of(response.url)
            if key and response.status >= 400:
                missing.add(f"{key} -> {response.status}")

        page.on("request", on_request)
        page.on("response", lambda r: pending.append(asyncio.ensure_future(on_response(r))))
        page.on("requestfinished", lambda r: pending.append(
            asyncio.ensure_future(self._capture_route(r, routes))))

        try:
            await page.goto(self.host + shot.url, wait_until="load", timeout=60000)
            await self._wait_ready(page)
            await self._drive(page, shot)
        except Exception as error:                      # noqa: BLE001
            # One page failing to settle must not lose the requests every
            # other page recorded — the run reports it and carries on.
            missing.add(f"{shot.label}: {type(error).__name__}: {error}")
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await context.close()

    async def _wait_ready(self, page) -> None:
        try:
            await page.wait_for_function(
                "window.__agenticMapsReady && window.__agenticMapsReady()",
                timeout=READY_TIMEOUT_MS)
        except Exception:                               # noqa: BLE001
            pass
        await page.wait_for_timeout(SETTLE_MS)

    async def _drive(self, page, shot: EmbedShot) -> None:
        """Every state `window.__agenticSealSteps` declares, in order.

        The page knows its own length; `shot.steps` is only a floor, so
        driving a page whose own step array is longer than what the caller
        happened to ask for still records everything reachable.
        """
        declared = await page.evaluate(
            "() => (window.__agenticSealSteps && window.__agenticSealSteps.length) || 0")
        steps = max(shot.steps, int(declared or 0))

        if not declared:
            return          # nothing to step through — page load already is the state

        for index in range(steps):
            await page.evaluate(
                "i => window.__agenticSealSteps[i] && window.__agenticSealSteps[i](i)", index)
            await self._wait_ready(page)

    # -- request bookkeeping ---------------------------------------------

    def _key_of(self, url: str) -> str | None:
        """Host-relative path of a request the sealed store must answer.

        Only same-host traffic is recorded. Anything reaching a third party
        directly would be a bug in the runtime, not something to seal.
        """
        if not url.startswith(self.host):
            return None
        path = url[len(self.host):] or "/"
        if path.startswith(self.map_prefix):
            # Routing is a POST answered by body, not by path — see route_key.
            if path.startswith(self.route_path):
                return None
            # Authoring-time chatter; the sealed page answers from its own
            # baked-in payload without asking anyone (see sealer.py).
            if any(path.startswith(skip) for skip in self.skip_paths):
                return None
            return path
        if any(path.startswith(prefix) for prefix in self.asset_prefixes):
            return path
        return None

    async def _capture_route(self, request, routes: dict[str, SealedRoute]) -> None:
        """Freeze a routing answer under a canonical key derived from its body."""
        if request.method != "POST" or self.route_path not in request.url:
            return
        try:
            body = json.loads(request.post_data or "{}")
            response = await request.response()
            if response is None or response.status != 200:
                return
            payload = await response.json()
        except Exception:                               # noqa: BLE001
            return
        key = route_key(body)
        routes[key] = SealedRoute(key=key, request=body, response=payload)


def route_key(body: dict) -> str:
    """Stable identity of a routing request.

    Keyed on what determines the geometry — endpoints, mode, vias, alternate
    count — and not on `route_id`, so the same road asked for twice under a
    different name is one stored answer.

    Coordinates become FIXED-PRECISION STRINGS, not rounded floats. A sealed
    page rebuilds this key in JavaScript (`web/sealed-runtime.js`) to find
    the frozen answer, and the two languages do not agree on how to print a
    float (or on which way to break a rounding tie). Six decimals is ~10 cm —
    far below anything the routing backend routes around — and `"%.6f"`
    means exactly the same thing on both sides.

    `mode` defaults to `"car"`, this product's own canonical default
    (`rest/maps_api.py`'s `RouteRequest.mode`) — NOT the legacy reference
    reference's `"drive"`, which was never this product's vocabulary
    (`routing/base.py`).
    """
    def point(value: dict | None) -> list[str]:
        value = value or {}
        return [f"{float(value.get('lat', 0.0)):.6f}", f"{float(value.get('lon', 0.0)):.6f}"]

    parts = {
        "mode": body.get("mode") or "car",
        "start": point(body.get("start")),
        "end": point(body.get("end")),
        "via": [point(v) for v in body.get("via") or []],
        "avoid": sorted(body.get("avoid") or []),
        "steps": bool(body.get("steps")),
        "alternates": int(body.get("alternates") or 0),
    }
    return json.dumps(parts, sort_keys=True, separators=(",", ":"))
