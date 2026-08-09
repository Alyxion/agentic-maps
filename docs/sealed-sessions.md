# Sealed Sessions

A **Sealed Session** is one exact-match, offline-replayable recording of a
live map page: every tile, vector, glyph, sprite, geo layer and routing
answer the page asked for while it was played, frozen into a single bundle
with no server behind it. Reopen the bundle later — on a kiosk with no
internet, from a `file://` path, on a laptop with the network entirely
disabled — and it presents *exactly* what was recorded, not a best-effort
approximation of it. A request the recording never saw is refused, out loud,
rather than silently skipped or served from a nearby-enough substitute.

This is the honest use case: a kiosk display that must never depend on a
venue's network, a docs page that embeds one fixed, reproducible map figure
instead of a live embed that can drift, or simply "send someone this one
file and they see precisely what I saw." It is not a general offline mode —
that is what `runtime_mode="offline"` plus a harvested `.mbtiles`/`.pmtiles`
package already gives you (`docs/concept.md` §5, `POST /package`). A sealed
session is smaller and stricter: it holds only what a specific choreography
actually displayed, and it would rather show nothing than show something
that was never proven.

## The readiness/step contract

A page becomes sealable by implementing one contract this product defines
and controls — nothing borrowed from any particular host or presentation
tool:

- **`window.__agenticMapsReady()`** — already exposed by every page that
  mounts `map.js` (`window.agenticMapsSettled`, aliased as
  `window.__agenticMapsReady`; see `web/map.js`). It reports `true` once the
  style is up, every tile is decoded, and the camera has stopped moving. The
  recorder waits for it before considering a page "up", and again after every
  step. A page that layers extra ordered work on top of the map itself — an
  image overlay that must be present in the style, say — wraps this the same
  way `web/view.html` does: its own `window.__agenticMapsReady` calls
  `agenticMapsSettled()` first, then checks its own extra condition.

- **`window.__agenticSealSteps`** — OPTIONAL. An array of functions (sync or
  async) a page defines when it has more than one state worth recording. The
  recorder calls each in order, passing its own index, and waits for
  `__agenticMapsReady()` again after every call — an async function that
  itself `await`s an animation's completion is how a page tells the recorder
  "give this state time to settle" (Playwright awaits whatever a step
  function returns). A page with no such array is simply recorded once, in
  whatever state it reaches on its own after load.

  ```js
  // Recorded as three states: the page's own load state, then two more.
  window.__agenticSealSteps = [
    async () => { map.map.jumpTo({ center: [10.07, 53.60], zoom: 15 }); },
    async () => { await map.map.once('idle'); setOverlayVisible(true); },
  ];
  ```

Both directions of a reversible choreography, or an ambient idle drift after
the last frame, are the PAGE's call to make, not the recorder's — if a state
is worth recording, it is worth one more entry in `__agenticSealSteps`. The
recorder itself carries no opinion about flight direction, animation timing,
or any page-specific concept (there is, deliberately, no view-cone-sweeping
or forward/backward-replay logic built into it — a page that wants either
recorded expresses it as more steps, in its own script).

`web/view.html` is a working example of a page that needs the readiness half
of the contract but, today, never the step half: nothing in agentic-maps
drives it through more than one live state after load (its `?imgstep=`/
`?step=` image-overlay gate and its `?bearing=` view cone are both read once
at load time, not updated live — see the file's own comments). A caller that
wants either animated and recorded defines `window.__agenticSealSteps`
itself; the dead postMessage protocol that used to drive both from a
host-specific parent frame (`ds-scene-step`, `ds-view-cone`,
`ds-view-ready`) has been removed rather than generalized, since nothing in
this product ever sends those messages.

## Recording and sealing

`agentic_maps/seal/` is the generalized core (framework-free, like the rest
of `agentic_maps/`):

- **`seal/recorder.py`** — `SessionRecorder` drives a page in a headless
  browser (Playwright) across one or more viewport variants (the authored
  box, a bigger stage, a wider aspect, retina — the screen-size margin a
  kiosk or a reader's window adds over the authored one) and writes down
  every same-host request as a `CaptureReport`. `route_key()` derives a
  stable, JS-reproducible identity for a routing request (endpoints, mode,
  vias, alternate count — not the caller-supplied `route_id`), so
  `web/sealed-runtime.js` can look a frozen route up by rebuilding the same
  key in JavaScript.
- **`seal/sealer.py`** — `Sealer` fetches exactly the recorded keys back out
  of the host, computes attribution PER ZOOM (an imagery ladder is banded by
  scale — crediting one provider for the whole session would be wrong at
  most of the zooms it covers), trims worldwide GeoJSON layers down to the
  footprint the recording actually touched, and packs everything into one
  concatenated byte string plus an offset index (`SealedBundle`) — cheaper
  than a `data:` URI per tile, both in bytes and in JSON escaping.
- **`seal/page_seal.py`** — `PageSealer` decomposes a page under `web/`
  (styles, body, scripts, in order) into a `SealedPage`, deduplicating shared
  libraries/stylesheets across every page a session embeds into one
  `SealedWeb`.

**Why a CLI tool (`tools/seal_page.py`) and not a REST endpoint.** Sealing
is a batch job: it needs a real browser, can run for minutes across several
viewport variants, and produces a build artifact (a bundle file) meant for
local inspection or a build pipeline — not something an application calls
mid-request. That is the same shape every other `tools/verify_*.py` script
already has, and it is how the reference implementation this was ported from
worked too (a plain module call from an export pipeline in the reference
monorepo, never wired into that project's own REST API). The *sealer's* own
byte-fetching rides entirely on `MapsApi`'s existing REST surface
(`/api/v1/maps/live/...`, `/vector/...`, the spec endpoint) exactly like a
browser tab would, so no new server route was needed for that half either.

```
python tools/seal_page.py /view.html?lat=53.55&lon=9.99&z=12 \
    --label hamburg-kiosk --out var/sealed/hamburg.json
```

Requires a running dev server and a real Chromium binary
(`playwright install chromium`, or `AGENTIC_MAPS_RENDER_CHROMIUM_PATH`
pointed at one already on disk — the same env var `POST /render` reads).

## Serving a sealed bundle back

`web/sealed-runtime.js` and `web/sealed-host.js` are the frontend half, and
run entirely inside the sealed file — no server, no host application:

- **`sealed-runtime.js`** decodes a `SealedBundle` once and answers every
  road a live map would normally take to the network: a registered MapLibre
  protocol (`amap://…` — this product's own scheme, replacing the
  legacy reference's private scheme) for tiles/glyphs/sprites, a `fetch`/
  `XMLHttpRequest` shim for everything else (routes looked up by
  `route_key()`, geo layers by their recorded path), and closed doors for
  `sendBeacon`/`EventSource`/`WebSocket`. **Anything not in the bundle is
  REFUSED, not forwarded** — a sealed session that quietly worked because the
  author happened to be online would be exactly the bug this exists to
  prevent. Every refusal lands in `store.misses`, so a verification harness
  can fail loudly on a stale recording instead of a viewer noticing a
  silently missing route months later. A raster tile just outside the
  recorded footprint is *not* a miss: the runtime reproduces
  `storage/fallback.py`'s parent-crop ladder, so a kiosk screen slightly
  bigger than the authored one degrades to a softer tile instead of a hole.
- **`sealed-host.js`** builds the map frames from the session's own decoded
  data. Each embed becomes a same-origin `srcdoc` iframe (so every frame can
  share one decoded byte store by direct property access — a `blob:` URL
  loaded from a `file://` session gets an opaque origin and that access is
  denied) via an explicit, caller-driven API:

  ```js
  const host = window.__agenticSealedMaps.boot(bundle, sealedWeb, runtimeSource);
  await host.build(document.getElementById('frame'), 'view.html', 'lat=53.6&lon=10.0&z=14');
  // ... later:
  host.teardown(document.getElementById('frame'));
  ```

  This is a deliberate departure from the reference implementation this
  was ported from, whose `sealed-host.js` watched an iframe's `src` attribute
  via a `MutationObserver` and inferred build/teardown from the host page's
  own scripting setting and clearing it. Nothing in agentic-maps embeds one
  sealed page inside another as nested frames of a larger document — there is
  no such host-authored lifecycle script to defer to — so the explicit
  `build()`/`teardown()` API is the honest generalization: whatever
  assembles the final sealed HTML decides when a frame is populated or torn
  down, in plain calls, not a protocol inferred by watching an attribute.

  The runtime payload rides inline in the frame (`<script type=
  "application/json" data-agentic-inline-spec>`, the SAME convention
  `map.js`'s own `loadPayload()` already supports for a live page) rather
  than through a special "spec" branch in the fetch shim — a sealed map
  therefore never asks the network for its own payload at all, live or
  sealed.

## Honest limits

- **Exact match only.** A route or tile the recording never saw is refused.
  Moving a pin, changing a mode, or widening a kiosk screen past its
  configured viewport variants can all produce a miss — that is the design,
  not a bug to route around with a tolerance.
- **`asset_prefixes` defaults to none.** `SessionRecorder` bakes in no
  assumption about where a host serves its non-map assets; a page that
  fetches something outside `map_prefix` (an icon, a photo) must have that
  prefix passed explicitly. agentic-maps' own pages currently need none —
  everything `view.html`/`index.html` fetch at runtime already lives under
  `/api/v1/maps/`.
- **No ambient/idle capture.** The reference implementation this was ported
  from waited out a fixed "ambient drift" window after the last step,
  because its own pages animated forever once idle. That was content-
  specific and has not been generalized — a page wanting an idle window
  recorded adds one more `__agenticSealSteps` entry that awaits it.
