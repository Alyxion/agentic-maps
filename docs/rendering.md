# Static rendering (`POST /render`)

A server-side screenshot of the live map runtime: the exact same imagery,
vector basemap, routes and highlights `web/map.js` draws interactively,
rendered to a PNG or JPEG at a chosen pixel size and DPI scale. For embedding
map views elsewhere — docs, print, thumbnails, share-card previews, any
non-JS consumer — with pixel parity to what a person sees in the browser.

This is **v1: a Playwright-headless-browser renderer**, not a from-scratch
rendering engine. It mounts `web/render.html` (which mounts the same
`map.js` `index.html`/`view.html` already run) inside a real headless
Chromium tab, waits for the map to report itself fully settled, and
screenshots the map element. The upgrade path to a production-scale renderer
is a known, deliberate next step — see "Upgrade path" below.

## Design: how the browser reaches the map runtime

Rendering happens **inside the same server process** that already serves
`MapsApi` and the static `web/` bundle (`devserver.py`, or any host that
mounts `MapsApi` and points `render_base_url` at itself). `POST /render`
does not spin up a second server and does not point Playwright at a
`file://` URL — `map.js` fetches tiles, vector tiles, glyphs and sprites from
paths relative to its own origin (`/api/v1/maps/...`), so nothing else could
resolve them. Concretely:

1. `MapsApi.build_payload(spec)` assembles the same `MapPayload` (spec +
   tile/vector/glyph URL templates) that `GET /api/demo-spec` already
   returns for the interactive map — one code path, so a render can never
   silently diverge from what browsing produces.
2. `MapsApi._store_render_payload()` mints a short-lived token mapped to
   that payload (kept ~2 minutes, swept opportunistically — not popped on
   first read, so a retried fetch does not turn into a 404).
3. `agentic_maps/render/service.py`'s `RenderService` launches a headless
   Chromium page and navigates it to `{base_url}/render.html?token=...`.
4. `web/render.html` fetches the payload the same way `index.html`/
   `view.html` fetch theirs (`data-agentic-spec-url`), just pointed at the
   per-request URL instead of a fixed one, and mounts the map exactly as any
   other page does.
5. `RenderService` polls `window.__agenticMapsReady()` — the same
   settled-style/tiles-loaded/not-animating contract `tools/verify_*.py`
   already polls — rather than waiting a fixed amount of time, then
   screenshots the `#map` element (not the full page, so nothing outside the
   map can leak into the output).

## Request

```
POST /api/v1/maps/render
Content-Type: application/json
```

Body (`RenderRequest`, `agentic_maps/models/render_request.py`) — exactly
one of `spec` / `view`:

| Field | Type | Notes |
| --- | --- | --- |
| `spec` | `MapSpec` \| omit | Full spec — the same JSON a live map slide mounts, with locations/routes/highlights if it has any. Only its starting camera (`overview`, or the first location) is captured; render does not play a fly-through. |
| `view` | `{center, zoom, source_id, bearing?, pitch?}` \| omit | Convenience path: "just show this point at this zoom", no choreography. `source_id` is required — there is no default imagery source, coverage is geography-specific (see `sources/presets.py`). |
| `width`, `height` | int, 64–4096 | Pixel size of the output (and the Playwright viewport). |
| `scale` | 1 \| 2 \| 3 | `devicePixelRatio` equivalent, mapped 1:1 onto Chromium's `device_scale_factor`. Output pixel dimensions are `width*scale` × `height*scale`. |
| `format` | `"png"` (default) \| `"jpeg"` | PNG is lossless. |
| `quality` | int, 1–100 | JPEG only; ignored for PNG. |
| `default_view` | `"hybrid"` (default) \| `"satellite"` \| `"map-light"` \| `"map-dark"` | Initial basemap flavor. A `MapSpec` carries no flavor of its own — this is client-side chrome state, so it lives on the request. |

Response: raw image bytes, `Content-Type: image/png` or `image/jpeg`.

### Example — convenience `view` path

```json
{
  "view": {"center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 16.2, "source_id": "de-dop"},
  "width": 1280, "height": 800, "scale": 2, "format": "png"
}
```

### Example — full `spec` path (with a pin)

```json
{
  "spec": {
    "id": "hq-shot", "source_id": "de-dop",
    "overview": {"center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 16.2},
    "locations": [{
      "id": "hq", "name": "HQ",
      "camera": {"center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 16.2},
      "pin": {"label": "HQ", "diameter_px": 90}
    }]
  },
  "width": 500, "height": 350, "scale": 1, "format": "jpeg", "quality": 90
}
```

## Online/offline gating

`POST /render` itself makes **no network call** — building the payload is
pure local computation, and launching a browser is a local subprocess. The
headless page it drives fetches tiles/vectors/glyphs through the exact same
endpoints (`/bundles/...`, `/live/...`, `/vector/auto/...`, `/assets/...`) a
live browsing session would, and each of *those* already enforces
`MapsApi._require_network_allowed()` where it applies. A render therefore behaves
exactly like a live visitor: in offline mode it draws whatever is cached or
bundled and leaves gaps where nothing is, never a hard failure just because
the server has no internet. `POST /render` deliberately does not duplicate
that gate.

## Throughput — be honest about it

`RenderService` launches a **fresh Chromium browser for every render**. This
is deliberate for v1: no shared-state failure modes (one wedged page cannot
poison the next request), and it matches the low-volume, on-demand use this
is built for — docs, print, thumbnails. It is **not a render farm**. A
browser launch plus a real page settling (style load, tile fetch, decode)
realistically costs low seconds per request; do not point a high-traffic
production path at this endpoint directly, and do not expect it to scale by
adding concurrent requests — every one of them pays its own full Chromium
launch.

## Setup

```
pip install -e ".[rest,fallback,render]"
playwright install chromium   # one-time; downloads the browser binary
```

`playwright` (the Python package) is declared in the `render` extra, same as
`fastapi` is in `rest` and `pillow` is in `fallback` — the core engine and
even the REST surface stay importable without it. The Chromium **binary**
is a separate, one-time step (`playwright install chromium`) that the
`pip install` alone does not perform.

Advanced/ops knobs (env vars, mirroring how `AGENTIC_MAPS_VALHALLA_URL` /
`AGENTIC_MAPS_OSRM_URL` configure the routing backends):

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENTIC_MAPS_RENDER_BASE_URL` | `http://127.0.0.1:8095` | Where Playwright is told to navigate. `devserver.py` sets this itself from `--host`/`--port`; a host embedding `MapsApi` should pass `render_base_url=` to its constructor instead. |
| `AGENTIC_MAPS_RENDER_CHROMIUM_PATH` | unset (Playwright's own resolution) | Pin a specific Chromium binary instead of whatever `playwright install` last set up. |

If Playwright or a Chromium binary is missing, `POST /render` returns
**HTTP 501** with a message naming what to install, rather than a stack
trace.

## Verification

`tools/verify_render.py` is the live check: it calls `POST /render` at
1x/2x/3x against a running dev server and asserts the PNG's own pixel
dimensions equal `width*scale` × `height*scale`, and that the response is
large enough to be a real drawn map rather than a blank/solid-color
rectangle. It intentionally does **not** import Playwright itself — the
browser automation under test runs *inside* the server process, so a plain
HTTP client exercises the real public contract. Not part of `pytest tests
-q` (a real browser is too heavy for the fast unit suite), same split the
other `tools/verify_*.py` scripts already use.

Fast unit tests (`tests/test_render_params.py`, `tests/test_render_request.py`,
`tests/test_render_endpoint.py`) cover parameter validation, the
`RenderRequest` spec/view exclusivity rule, and the REST endpoint's
success/error mapping with `RenderService` mocked out — no browser, no
network, part of the normal suite.

## Upgrade path: MapLibre Native

A one-Chromium-tab-per-request renderer will not scale to high request
volume — every render pays a full browser launch and a real style/tile
load. The documented next step is a **MapLibre Native** renderer: the same
MapLibre GL style/rendering engine, compiled for server-side use with no
browser and no DOM, rendering a style + camera straight to pixels. The goal
is for that renderer to be **API-compatible with this endpoint** — same
`POST /render` request/response shape — so callers would not need to change
anything; only the backend implementation swaps out from underneath them.
This is not yet implemented; `RenderService`'s constructor and the
`RenderRequest`/`MapPayload` models were kept free of anything
Playwright-specific in their public shape specifically so that swap stays
plausible.
