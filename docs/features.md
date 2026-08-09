# agentic-maps — Feature Reference

Everything the product does today. Companion documents: `concept.md`
(architecture, offline contract), `imagery-coverage.md` (worldwide licence
survey — the authority on what may ship inside a published package).

---

## 1. Map application (`web/index.html`, `web/map.js`)

The Google-Maps-class landing experience; every capability is also reachable
through `AgenticMap` as an embeddable runtime (`data-agentic-map` + payload).

- **Views:** Hybrid / Satellit / Karte / Dunkel. Map modes are pure
  cartography — imagery layers are structurally absent, not hidden.
- **Aerial-quality auto-fallback:** in Hybrid/Satellit every settled move
  asks `GET /aerial/quality` (pure bbox math over the same band-coverage
  config the `/aerial` dispatcher routes with — no tile fetched, cached per
  rounded bbox) how native the imagery really is. Viewing ≥ 3 zooms past
  the best native band (New York at z12 over the z8 world layer) switches
  the DISPLAY to Karte with a toast and remembers the chosen view; once a
  later move reports a gap ≤ 1 the chosen view returns automatically. The
  3/1 hysteresis plus the settled-move debounce prevent flapping along
  coverage edges, a manual view pick during fallback always wins, and the
  layers control mirrors the displayed view (`tools/
  verify_aerial_fallback.py` proves all of it headlessly).
- **URL state, Google-compatible:** `#@lat,lon,zoomz&view=…&lang=…` (plus
  `,Nh`/`,Nt` for bearing/pitch when non-zero). Written on every settled move
  via `replaceState`; hand-edits and back/forward handled via `hashchange`;
  Google Maps `@lat,lng,Nz` cores paste in directly. The runtime mode
  (offline/mixed/online) is deliberately NOT in the URL (server-side state; a
  shared link must not cut the recipient's network).
- **Search** (see §2 for ranking): magnifier glyph, clear-✕ only when there is
  content, results with live distance chips. A chosen result highlights the
  basemap's own label for that place and suppresses the stock rendering while
  the highlight stands, so the name appears exactly once
  (`AgenticMap.highlightPlace`).
- **Right-click menu:** Route von hier / Als Zwischenstopp / Route hierhin /
  Hierhin zentrieren — with reverse-geocoded names replacing raw coordinates.
  This menu is the path for arbitrary coordinates: a plain left-click opens
  the info card only when it lands on a named POI (small hit box around the
  icon); clicking open ground dismisses whatever is open and drops nothing.
- **Languages:** 25 label languages; countries, cities, streets and ocean
  labels all follow `setLanguage(lang)`.
- **Headline 3-way mode switch:** offline / mixed / online, server-enforced
  (`/mode`, `RuntimeMode`). Offline refuses every network-touching endpoint at
  403; mixed keeps routing/geocoding/live-tiles/glyphs live but refuses bulk
  provisioning (harvest, vector/extract); verified end-to-end.
- **Hold-to-zoom** on +/− (400 ms delay, then continuous).
- **Per-country route shields:** SVG-generated per character count
  (`am-shield-DE-motorway-2char`…), German Autobahn = hexagon (Zeichen 405),
  everything else rounded rectangles; colours follow national signage; the
  `network` tag is parsed (`BAB`, `DE:national`, `e-road`, `FR:A-road`, …).
  On the neutral canvas palette shields are dropped entirely.
- **Street labels from z14** with density via Protomaps' per-road `min_zoom`
  as the collision sort key (measured: 15 → 135 named streets at z14).

### AgenticMap runtime API (host-facing)

| Member | Purpose |
| --- | --- |
| `loadSpec(spec)` | swap the whole scene (locations, routes, decorations) |
| `goTo(i)` / `next()` / `prev()` | camera choreography over spec locations |
| `addRoute(route)` | draw; `animate_ms` = progressive draw with head dot |
| `clearRoutes()` / `clearHighlights()` | stage reset (scene players use both) |
| `addHighlight({kind: radius\|polygon\|ping\|circle,…})` | geo areas & pings |
| `pingAt([lon,lat], label, {ttl_ms, still})` / `pingFeatures(kinds)` | sonar pings |
| `highlightCountry(hit)` / `highlightPlace(name)` | border / label emphasis |
| `setView(v)` / `setLanguage(l)` / `setFlavorOverride(flavor)` | appearance |
| `setVectorDepth(z)` / `reloadVectors()` | street-source management |

Route rendering details: casing + colour line, badge with drawn pictogram
(car/walk/transit — no emoji) plus `min · km`; off-road remainders between the
snapped geometry and the actual stops drawn as dotted connectors (Google
style), destination stub only appearing when the animated head arrives. Pin
labels are sized by circle-chord geometry (`_pinFontSize`) so text can never
touch the rim.

## 2. Search & geocoding

One merged, scored result list — never "countries first":

- **City index** (`/geo/cities/search`): the dense GeoNames `cities15000`
  index (`geo/geonames.py`, ~32k places worldwide with population ≥ 15k,
  960 in Germany; CC BY 4.0, asset built by `tools/make_city_index.py`)
  when its asset is installed, else Natural Earth 10m populated places
  (~7300; the 50m set lacks entire state capitals). Prefix-matched against
  local name, ASCII transliteration AND diacritic-stripped spelling
  ("Köln"/"Koeln"/"koln" all land), scored
  `log10(population)/7 + 1.8·e^(−km/400)·size + 1.6·same-country` — the
  `size` factor (`min(1, log10(pop)/6)`, GeoNames path only) keeps a 57k
  neighbour (Hameln) from burying a megacity (Hamburg) now that small towns
  exist in the index. Viewer's country resolved via bbox
  (`CountryIndex.country_at`); GeoNames' ISO codes are mapped back to
  display names at the REST layer. Offline-capable, ~ms answers. Known
  limit: a handful of GeoNames primary names are English exonyms ("Munich",
  "Nuremberg" — most are local: "Köln", "Hannover"); full local spellings
  for those are caught by Nominatim in the same list.
- **Country search** (`/geo/countries/search`): all 25 languages + ISO;
  substring matching only from 4 characters (two letters match half the
  atlas); exact name always outranks everything.
- **Nominatim** (`/geocode`): viewbox-biased (`bounded=0`), over-fetched and
  re-sorted nearest-first; duplicates within 3 km of an indexed city dropped.
  `/reverse-geocode` names right-click points.
- **Coarse location:** only in `standalone` mode (dev server), never when
  mounted as a module; asked on first search-field focus, low accuracy,
  choice remembered. Map centre wins once the user has navigated (z ≥ 6).

## 3. Routing (Valhalla primary, OSRM secondary)

See `docs/routing.md` for the full backend reference. Summary:

- **Four travel modes** (`car`/`truck`/`walk`/`bike`) — canonical vocabulary
  every backend maps onto its own profile/costing names.
- **Multi-stop** in one request (`via[]`) — one geometry, honest totals,
  per-leg figures (`legs[]`).
- **Alternates**: `POST /route {..., alternates: N}` returns up to N extra
  routes at `MapRoute.alternates`, drawn dimmed/clickable-to-promote
  (both backends, 2-point trips only — neither produces alternates on
  multi-stop routes).
- **Isochrones**: `POST /isochrone` returns GeoJSON reachable-area contour
  polygons (time- or distance-based) — Valhalla only.
- **Via places** (`MapRoute.via_places`): after every `/route`, the offline
  place index is corridor-matched against each candidate's geometry (±7 km,
  endpoints excluded) — "via Pforzheim" vs "via Heilbronn" instead of "one
  is longer". Source: the dense GeoNames index (~32k places — the one that
  actually knows Pforzheim and Heilbronn) when installed, Natural Earth
  (~7300) as fallback; at GeoNames scale the corridor matcher consumes the
  index pre-bucketed by grid cell (`bucket_places`), which keeps the
  per-candidate cost at ~4 ms (vs ~12 ms unbucketed at NE scale). The list
  is **priority-ordered** (population log-scaled +
  capital boost + along-route spacing bonus, `geo/via_places.py`), so any
  consumer truncates by importance: the UI's candidate rows show the brief
  top-1 label (top-2 "X und Y" only when the top place fails the
  uniqueness rule against the other candidates), the summary shows the
  fuller chain (~4), and `along_km` allows geographic re-sorting. The
  road-based label ("über A 8") stays as the secondary line.
- **Turn-by-turn** (`steps=true`): backend maneuver vocabulary normalized to
  `RouteStep`, wording rendered client-side in the UI language; drawn SVG
  maneuver glyphs (roundabout carries its exit number inside the icon);
  clicking a step flies to the junction.
- **Route options honestly gated:** `/routing/capabilities` probes the active
  backend (`alternates`, `isochrone`, `avoid` list) so the frontend never has
  to find out by catching an error. Avoid-toll/motorway/ferry checkboxes
  reflect what the backend actually honours — against Valhalla that means an
  `exclude_*` costing flag really being applied server-side, not silently
  dropped.
- **Matrix** (`/matrix`): all-pairs minutes for any of the four modes.
- **Backend selection**: `AGENTIC_MAPS_ROUTING_BACKEND` (`valhalla` default,
  or `osrm`); `AGENTIC_MAPS_VALHALLA_URL` / `AGENTIC_MAPS_OSRM_URL` point at
  the self-hosted server.

## 4. Reachability & data overlays (`web/heatmaps.js`, `web/palette-canvas.js`)

- **Canvas palette** (Esri Light-Gray-Canvas idea): chroma stripped via
  Rec.-709 luma, mid-tones left free for data, place names keep contrast,
  shields dropped, roads drained to grey. `setFlavorOverride(agenticCanvasFlavor())`.
- **Overlay kinds** (`agenticHeat`): `radial` (readable rings + labels),
  `isodistance` (street-network travel time via matrix, probe grid
  auto-thinned to the backend cap, reports its resolution — a fuzzy
  approximation kept for the heatmap gallery; `POST /isochrone` is the real,
  exact-polygon version for actual use), `cluster`
  (kernel density, weighted, overlapping sources add), `highlight`
  (categorical wash, no ramp). All draw above fills, below labels.
- **Gallery:** `/overlays.html` + `/api/overlay-samples` — six samples, each
  stating its question and its caveat. Space/arrows to step.
- Open follow-up (agreed, not built): per-pixel raster renderer with the
  volcanic (inferno) ramp for thousands of gradations, high-resolution
  isodistance, elliptic/polygonal falloffs.

## 5. Globe (`web/globe.js`)

- **Seamless 2D↔3D handover** at `HANDOVER_ZOOM = 4.3`: camera distance
  solves `tan(fov/2) = R·sinα/(d−R·cosα)` against the Mercator ground span
  (512-px tile convention, `cos(lat)` clamped ±60°); measured ratio ≈ 1.00.
- **Tessellation from device pixels:** segment count solved from
  `N ≥ π·√(R/2ε)` for ε = 0.2 device px of silhouette sag, re-tessellated on
  resize/DPR change (96…512 segments); renderer runs at the display's real
  pixel ratio (cap 3).
- **Limb shading in the surface material** (`onBeforeCompile`), not a shell —
  a 1.0008-radius shell overhangs >1 px on a 1400-px globe and reads as a
  faceted rim. Starfield with per-star magnitude/size/tint; anisotropic
  texture filtering (8×).
- **Labels:** countries + capitals + state capitals + localized ocean names
  (Natural Earth marine polys; label points via pole-of-inaccessibility, not
  centroid — the Pacific's centroid is in the Gulf of California). Density by
  projected screen area, priority collision, multi-frame fades; positions
  always follow lat/lon (fading labels keep moving); labels behind the limb
  vanish instantly (`hideAtOnce`); `display:none` until first placement.
- Cartography texture 4096×2048 (light/dark), borders/rivers/state lines as
  ribbon geometry on the sphere.

## 6. Imagery federation (`sources/presets.py`)

- **Germany complete — all 16 states**, 20 cm (HB 10 cm), each GetMap-verified.
  Hamburg via `wms_dop_zeitreihe_unbelaubt` (TIME 2001–2025, newest by
  default; out-of-area = blank ~667 B, auto-detected). Licences: dl-de/by-2-0,
  dl-de/zero-2-0, CC BY 4.0 per state — all redistributable in sealed offline packages.
- **World:** NASA Blue Marble (z0–8, PD) → Sentinel-2/BKG (z8–12, Copernicus)
  → state DOP (z13–19). Alpha-mosaic compositing, blank detection,
  `white_is_nodata`, bounded live-proxy concurrency.
- **Unified aerial ladder** (`GET /aerial/{src}/{z}/{x}/{y}`): the whole
  ladder behind ONE tile URL, dispatched server-side by zoom (z0–8 world,
  z9–12 Sentinel-2, z13+ state DOP) through the same `/live` cache-through
  internals. This is the LIVE display source (`MapPayload.aerial_url_template`,
  one z0–19 raster source in `web/map.js`), which is what makes MapLibre's
  native pitched-view pyramid LOD work: the far rows of a tilted camera pull
  coarser tiles of the *same* source instead of smearing an overzoomed z13
  band to the horizon. Where the owning band has no imagery, the dispatcher
  falls through to a parent-crop upscale of the coarsest covering band
  (`X-Agentic-Maps-Aerial-Synth` marks it) rather than answering 204 — a hole
  in a single-source pyramid would render as a blank square. `/attribution`
  follows the same fallthrough, so upscaled pixels credit their real rights
  holder. Sealed/offline packages keep the per-band sources unchanged.
- **Verified-but-not-yet-preset** (see imagery-coverage §2b): USGS (US),
  swisstopo, PDOK (NL), IGN (FR), PNOA (ES), Vlaanderen — pending scale/bbox
  steps. Excluded permanently: Google/Bing/Esri/EOX tiles.

## 7. Street vectors

- **Remote planet, range requests** (`vector/remote_pmtiles.py`): single
  tiles read out of the ~137 GB Protomaps build via HTTP `Range` (directory
  LRU-cached, 206-only, refuses a 200), disk-cached per tile including empty
  answers. Browsing a city centre costs ~0.5 MB, mints nothing.
- **`/vector/coverage`:** `max_zoom` (worth minting?) vs `guaranteed_zoom`
  (safe MapLibre source maxzoom), computed from tiles that actually exist
  (sampled grid), planet-aware when online, cache-aware when offline.
- **Extracts** (`pmtiles extract`) remain solely for sealing offline
  packages; the client mints only below `guaranteed_zoom` — never while the
  planet is reachable. `build_payload()` always advertises
  `vector_url_template` (`/vector/auto/tiles/…`), even with zero local
  extracts — that endpoint's own local → remote-planet → cache fallback is
  what keeps a fresh install's pure-cartography views from rendering blank.

## 8. Scene layer

- **MapSpec** (pydantic): locations (camera, pin, highlights), routes
  (colour, `animate_ms`), overview pose, fly duration. A published package
  embeds resolved routes/geometry so playback is offline.
- **Medallion mini-map** (`/view.html`): a single non-interactive view for
  stencil/mask embeds (circular medallions etc.) — `?lat&lon&z&view&dot`;
  all interaction handlers disabled, optional marker dot. Optional
  comparison overlay: `?heat=lon:lat:w;lon:lat:w…` (+`heatr` radius px)
  draws a kernel-density cluster — e.g. purchasing power across a region.
- **Theme flavour** (`palette-theme.js`): `agenticThemeFlavor({paper, ink,
  water, accent})` derives a complete basemap palette from four brand
  tokens — geometry becomes a measured paper→ink lightness ladder
  (palette-canvas idea), water gets its own token (desaturated by default),
  remaining stock keys are neutralised so upstream additions can't leak
  foreign colour, saturated shields are dropped. **Host convention:** the
  embedding page defines `--am-map-paper/-ink/-water/-accent` CSS variables;
  its bridge forwards them to every map iframe as `?th=paper:hex,ink:hex,…`.
  Both embed pages apply the flavour to vector views only — imagery keeps
  its cartography. Adapting maps to a new theme = defining four variables.

> A reference "exposé scene player" (stepwise build-on-each-other scenes
> driven by a postMessage contract from a host application) used to live at
> `web/immodeal.html`. It was removed while extracting this codebase into a
> standalone product, along with the host-specific postMessage
> contract that drove it. `web/view.html`'s own image-overlay step gate and
> view-cone bearing are now read once at load (`?imgstep=`/`?step=`/
> `?bearing=`) rather than live-updated by that protocol — see
> `docs/sealed-sessions.md` for the generalized readiness/step contract that
> replaced it.

## 9. Platform integration & ops

- **Mounting:** `MapsApi(bundles_dir, …).mount(app, prefix)`; a host injects
  its own auth/scopes policy around it. `standalone=False` when
  mounted (no geolocation prompts). Frontend assets under the host's static
  route; `stage_asset_prefix` in the payload tells pages where llming-stage
  serves three.js/Vue/Quasar (`/_stage` default) — nothing llming-stage ships
  is vendored here (MapLibre + Protomaps basemap stay local, map-specific).
- **Remote frontend debug** (llming-com): off unless `AGENTIC_MAPS_DEBUG=1` +
  `AGENTIC_MAPS_DEBUG_KEY`; WebSocket bridge reports console/errors/map+globe
  state and answers `evaluate` — API-key-gated, per-session.
- **Env:** `AGENTIC_MAPS_VALHALLA_URL` / `AGENTIC_MAPS_OSRM_URL` /
  `AGENTIC_MAPS_ROUTING_BACKEND` (docs/routing.md),
  `AGENTIC_MAPS_NOMINATIM_URL` (docs/setup-guide.md),
  `AGENTIC_MAPS_PLANET_URL` (pin a build), `AGENTIC_MAPS_BKG_URL`,
  `AGENTIC_MAPS_MODE` (container equivalent of `--mode`),
  `AGENTIC_MAPS_RENDER_BASE_URL` / `AGENTIC_MAPS_RENDER_CHROMIUM_PATH`
  (docs/rendering.md), `AGENTIC_MAPS_DEBUG(_KEY)`.
- **Verification harnesses** (`tools/verify_*.py`): Playwright end-to-end
  checks for map load, URL state, shallow-region rendering, offline
  behaviour, and static-render output (`verify_render.py`).
- **Static rendering** (`POST /render`, `agentic_maps/render/`, optional
  `render` extra): server-side screenshot of the live map runtime — same
  imagery/vector/routes/highlights, same `map.js`, at a chosen pixel size
  and 1x/2x/3x DPI scale, PNG or JPEG. A Playwright-headless-browser
  renderer (v1); see `docs/rendering.md` for the design, the
  online/offline-gating decision, the honest throughput ceiling, and the
  documented MapLibre Native upgrade path for high-volume production use.

> **Sealed Sessions.** `web/sealed-runtime.js` / `web/sealed-host.js` are an
> in-frame tile/asset store (a registered `amap://` protocol + `fetch`/XHR
> shims that refuse anything unrecorded, plus the parent-crop fallback
> ladder) for playing a sealed, single-file session with no server behind
> it — kiosks, static-site embeds, "send someone this one file and they see
> exactly what I saw offline." Backed by `agentic_maps/seal/`
> (`SessionRecorder`, `Sealer`, `PageSealer`) and driven by
> `tools/seal_page.py`; see `docs/sealed-sessions.md` for the readiness/step
> contract a page implements to become sealable, the exact-match refusal
> behaviour, and why sealing is a CLI tool rather than a REST endpoint.

### Endpoint quick reference (`/api/v1/maps`)

| Area | Endpoints |
| --- | --- |
| Tiles | `live/{src}/{z}/{x}/{y}`, `bundles/{id}/tiles/…`, `vector/auto/tiles/{z}/{x}/{y}.mvt` |
| Geo | `geo/countries[/labels|/search]`, `geo/cities[/search]`, `geo/oceans`, `geo/physical` |
| Search | `geocode`, `reverse-geocode` |
| Routing | `route` (+ `alternates`), `isochrone`, `matrix`, `routing/capabilities` |
| Vector | `vector/coverage`, `vector/extract`, `vector/streets` (street survey for a bbox) |
| Packaging / export | `plan`, `harvest`, `harvest-world`, `package` (sealed mbtiles+pmtiles+manifest zip), `sources`, `composites`, `bundles` |
| Rendering | `render` (server-side screenshot, `render` extra — docs/rendering.md), `siteplan` (SVG site plan) |
| Ops | `mode` (offline/mixed/online switch), `cache` (DELETE — clears bundles/packages/cached assets), `debug/*` (opt-in) |

**Providers are pluggable, not hardcoded.** `MapsApi(bundles_dir, sources=…,
composites=…)` accepts any `dict[str, TileSource]` / `dict[str, CompositeSource]`
in place of the bundled `sources/presets.py` defaults — every field on
`TileSource` that carries a legal claim (`attribution`, `license_name`,
`license_url`) is a required pydantic field, so a provider set literally
cannot be constructed without them; there is no code path that ships a tile
without credit. The bundled defaults are the "EU pack" (Germany DOP federation
+ Sentinel-2 + Blue Marble + Protomaps); `imagery-coverage.md §6` documents the
verification process for adding another region's provider the same way.

**Raw data, not just rendered tiles.** Every dataset behind the map is also
fetchable directly: `geo/countries|cities|oceans|physical` and
`vector/auto/tiles/{z}/{x}/{y}.mvt` serve GeoJSON/MVT for use outside the
map renderer entirely, and `POST /package` bundles a spec's harvested
imagery (`.mbtiles`) and vector extracts (`.pmtiles`) plus a manifest of
every license/attribution involved into one zip — the same offline artifact
the sealed-map pipeline produces, usable as a standalone dataset export.
