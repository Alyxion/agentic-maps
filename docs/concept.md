# agentic-maps — Concept

agentic-maps: an offline-capable, legally-licensed mapping platform — tiles,
routing, and rendering. An aerial/satellite (or styled street) map fills the
stage, a photo pin marks the subject, translucent highlight circles call out
nearby POIs, an info panel sits on top — and the camera **flies between
locations**, from city detail up to intercity overviews (e.g. Stuttgart →
München via A8, with the computed drive time on the route). Reference look:
classic real-estate "TOP LAGE" exposé slides; quality bar: **at least as
professional as Google Maps**, in both the imagery view and the styled map
view.

Hard requirements shaping everything:

1. **Offline-complete when published.** While *editing*, live internet access is fine
   and expected. A *published* map is a sealed artifact: every tile that any
   "reasonable movement" (the authored fly-through and its animations) can ever show —
   raster imagery, vector streets/labels, fonts, sprites — is prewarmed and packaged.
   Presenting needs no internet, ever. See §5, the completeness contract.
2. **Legally downloadable sources only.** Bulk download must be permitted; on-slide
   attribution is fine, API keys and "no caching" clauses are not.
3. **Google-compatible mapping.** Everything is Web Mercator (EPSG:3857) on the
   standard XYZ tile grid — identical projection, tile addressing and zoom semantics
   as Google Maps. WGS84 lat/lon in, mercator tiles out; any coordinate/zoom from
   Google Maps transfers 1:1.
4. **Standalone product.** Developed and shipped as its own isolated service with
   its own dev server (:8095); no host-application merge is planned. A `mount(app,
   *, prefix)` integration surface exists for embedders who want it, but nothing
   here assumes a specific host.

---

## 1. Providers & licensing

The offline requirement disqualifies every mainstream commercial imagery API and
selects open-data sources:

| Provider | Offline / bulk download | Verdict |
| --- | --- | --- |
| **German state orthophotos** — all 16 state surveys (table below) | 20 cm color aerials (Bremen 10 cm) via keyless EPSG:3857 WMS; DL-DE/BY-2.0, DL-DE-**Zero**-2.0 or CC BY 4.0 depending on state | ✅ **Primary imagery**, federated nationwide (§3 composite) |
| **BKG `wms_dop`** (Germany-wide 20 cm) | Open data DL-DE/BY-2.0; GetMap needs free registration (personalized URL) | ✅ Nationwide fallback via `AGENTIC_MAPS_BKG_URL` |
| **OpenStreetMap via Protomaps builds** | Planet vector basemap as single PMTiles file, free bulk download + regional `pmtiles extract`; ODbL attribution | ✅ **Street/label/basemap data** |
| **Protomaps basemap styles + assets** (`@protomaps/basemaps`, fonts, sprites) | Styles CC0, fonts OFL, sprites CC0; all vendorable | ✅ **Professional cartography** (§4) |
| OSRM / Geofabrik OSM extracts | Self-hostable, ODbL | ✅ Routing at authoring time (§6) |
| GTFS open data (DELFI Germany-wide, VVS Stuttgart, …) | Official transit lines/stops/schedules, open licenses | ✅ Transit routes/times, roadmap (§7) |
| Natural Earth / NASA Blue Marble | Public domain | ✅ Optional globe zooms |
| **Sentinel-2 Europa** (Copernicus, processed by BKG) | Free of charge under the Copernicus legal notice, **commercial use included**, source note required | ✅ **Regional band z8–12** — one seamless 10 m mosaic (§3) |
| **Natural Earth** admin-0 boundaries | Public domain, no attribution required | ✅ **Worldwide borders + country names**, sealed into every package |
| **GeoNames** `cities15000` | CC BY 4.0 — credit "GeoNames (geonames.org)" travels in payloads, exported pages and package manifests | ✅ **Dense place index** (~32k cities ≥ 15k pop) for via-place route labels + city autocomplete (`tools/make_city_index.py`) |
| Sentinel-2 cloudless (EOX) | Rendered tiles need a **paid** EOX licence for commercial use | ❌ Superseded by the BKG Copernicus layer |
| `tile.openstreetmap.org` raster | OSMF policy **forbids bulk download** | ❌ Never harvest; Protomaps builds instead |
| Google Maps/Earth, Mapbox, Bing/Azure, Esri | ToS forbid offline extraction / tie use to their runtimes | ❌ |

**Verified live** with real keyless EPSG:3857 GetMap requests — BW, BY, BE, NW on
2026-07-24, the next eleven on 2026-07-25, Hamburg on 2026-07-27 (see
`docs/imagery-coverage.md` §2):

| State | Endpoint | Layer | License |
| --- | --- | --- | --- |
| Baden-Württemberg | `owsproxy.lgl-bw.de/owsproxy/ows/WMS_LGL-BW_ATKIS_DOP_20_C` | `IMAGES_DOP_20_RGB` | dl-de/by-2-0 |
| Bayern | `geoservices.bayern.de/od/wms/dop/v1/dop20` | `by_dop20c` | CC BY 4.0 |
| Berlin | `gdi.berlin.de/services/wms/truedop_2024` | `truedop_2024` | dl-de/zero-2-0 |
| Nordrhein-Westfalen | `www.wms.nrw.de/geobasis/wms_nw_dop` | `nw_dop_rgb` | dl-de/by-2-0 |
| Hessen | `www.gds-srv.hessen.de/cgi-bin/lika-services/de-viewer/access/ogc-free-images.ows` | `he_dop_rgb` | dl-de/zero-2-0 |
| Niedersachsen | `opendata.lgln.niedersachsen.de/doorman/noauth/dop_wms` | `ni_dop20` | CC BY 4.0 |
| Rheinland-Pfalz | `geo4.service24.rlp.de/wms/rp_dop20.fcgi` | `rp_dop20` | dl-de/by-2-0 |
| Schleswig-Holstein | `dienste.gdi-sh.de/WMS_SH_DOP20col_OpenGBD` | `sh_dop20_rgb` | dl-de/zero-2-0 |
| Mecklenburg-Vorpommern | `www.geodaten-mv.de/dienste/adv_dop` | `mv_dop` | dl-de/by-2-0 |
| Brandenburg | `isk.geobasis-bb.de/mapproxy/dop20c/service/wms` | `bebb_dop20c` | dl-de/by-2-0 |
| Sachsen-Anhalt | `www.geodatenportal.sachsen-anhalt.de/wss/service/ST_LVermGeo_DOP_WMS_OpenData/guest` | `lsa_lvermgeo_dop20_2` | dl-de/by-2-0 |
| Sachsen | `geodienste.sachsen.de/wms_geosn_dop-rgb/guest` | `sn_dop_020` | dl-de/by-2-0 |
| Thüringen | `www.geoproxy.geoportal-th.de/geoproxy/services/DOP` | `th_dop` | dl-de/zero-2-0 |
| Bremen | `geodienste.bremen.de/wms_dop_lb` | `dop10_2025_HB` (10 cm) | CC BY 4.0 |
| Saarland | `geoportal.saarland.de/freewms/dop2021` | `sl_dop2021` | dl-de/zero-2-0 |
| Hamburg | `geodienste.hamburg.de/wms_dop_zeitreihe_unbelaubt` | `dop_zeitreihe_unbelaubt` (TIME 2001–2025, newest by default) | dl-de/by-2-0 |

At ~48.5° latitude 20 cm/px ≈ **z19** — the source out-resolves anything a
1920×1080 stage needs. BKG's Germany-wide GetMap returns `NOACCESS_METHOD`
without registration (tested), hence the per-state federation.

**Worldwide coverage and what may be shipped** — which countries have imagery we
are legally allowed to seal into an offline package, which are candidates, and which
providers are permanently excluded: `docs/imagery-coverage.md`.

**Known quirks.** Hamburg was briefly the one missing state (its old
`HH_WMS_DOP*` hosts were retired); the gap closed 2026-07-27 via the LGV's
`wms_dop_zeitreihe_unbelaubt` service above — out-of-area answers are
~667-byte blank JPEGs, safely under the blank-tile heuristic. Service quirks
that cost real debugging are encoded in the presets rather than rediscovered:
Hessen's
`he_dop20_rgb` renders only from ~z17 (the aggregate `he_dop_rgb` covers all
scales), Mecklenburg-Vorpommern serves nothing below ~z11, and Thüringen fills
everything outside its border with opaque `#FFFFFF` even with
`TRANSPARENT=TRUE` (`TileSource.white_is_nodata` keys it out).

**Attribution — what the licenses actually require, and where we put it.**
DL-DE/BY-2.0, CC BY 4.0 and ODbL all require attribution "in a manner reasonable
to the medium" — none requires a permanently visible full-text line. Berlin's
DL-DE-Zero requires none at all. Our placement policy:

1. **On-map**: a compact ⓘ badge (MapLibre compact attribution, Google-style)
   that expands the full line on click; the line itself credits **only the
   sources the map actually uses** (`MapsApi.spec_attribution` resolves the
   composite members covering the spec's stops — a BW-only spec credits only
   LGL BW + OSM).

   **Attribution in navigation mode.** Google shows no visible credit line
   while navigating — but Google is the *licensor* of its own imagery; we are
   *licensees* and may not drop attribution. What our licenses do allow:
   DL-DE/BY-2.0 and CC BY 4.0 require attribution "reasonable to the medium",
   and the OSMF's own attribution guidance explicitly accepts one-tap-away
   attribution in space-constrained UIs. An active navigation HUD is exactly
   that case, so in nav mode the standing credit line compacts to the app's
   existing ⓘ badge pattern (small, corner, genuinely visible over the HUD);
   one tap expands the complete per-zoom credit line, which auto-collapses
   again. Outside navigation the permanent line stays exactly as before.
2. **In-package**: the sealed package's `manifest.json` carries the complete
   attribution + license URLs; at publish the host renders these once into the
   publication's credits/imprint (final page or appendix) — the legally robust
   place for the full text.
3. Every bundle stores its license metadata internally, feeding the host's
   license registry at merge time.

## 2. Architecture

```
agentic_maps/
  models/     one pydantic model per file: LatLon, BBoxDeg, CameraPose, MapPin,
              MapHighlight, MapLocation, MapRoute, RasterAdjust, MapSpec,
              TileSource, CompositeSource, TileCoord, HarvestPlan, HarvestReport,
              BundleInfo, VectorBundleInfo, PackageManifest, EmbedShot,
              CaptureReport, SealedRoute, SealedBundle, SealedPage, SealedWeb
  sources/    licensed presets (state WMS + coverage bboxes) + `de-dop` composite
  harvest/    planner (spec -> tile pyramid incl. fly corridors) + async harvester
  storage/    MBTilesBundle (raster), PMTilesBundle (vector), FallbackTileResolver,
              VectorTileCache (remote-planet tile cache)
  vector/     pmtiles extraction + remote-planet range reads (RemotePMTiles)
  geo/        Natural Earth country/city/ocean indexes + 25-language names;
              GeoNames dense place index (via-places, autocomplete)
  routing/    RoutingBackend protocol; ValhallaRouter (default), OsrmRouter
  render/     RenderService — POST /render via headless Chromium (Playwright)
  seal/       SessionRecorder, Sealer, PageSealer — Sealed Sessions backend
  setup/      planner + CLI wizard + Overpass PBF fetch (2-minute geo server)
  siteplan/   SVG site-plan renderer
  package/    PackageBuilder (sealed zip for publish)
  rest/       MapsApi / SetupApi / FrontendDebug — classes with mount(app, *, prefix)
  devserver.py  isolated FastAPI app (:8095), --mode offline|mixed|online
web/          map.js runtime, map.css, demo page, vendored maplibre-gl + basemaps.js
              sealed-runtime.js / sealed-host.js — the Sealed Sessions frontend
              (amap:// offline replay; see docs/sealed-sessions.md)
tests/        tile math, planner/corridors, bundles, fallback ladder, composite
              routing, REST surface, routing, packaging, offline mode
tools/        verify_*.py harnesses driving the dev server with Playwright;
              seal_page.py — the Sealed Sessions CLI
```

### MapSpec — the authoring model

A map slide is a `MapSpec`: imagery source reference (plain or composite), optional
overview camera, ordered `MapLocation`s (camera pose + `MapPin` photo bubble +
`MapHighlight` circles), embedded `MapRoute`s, and a `RasterAdjust`. The location
order *is* the step order; step transitions are MapLibre `flyTo` animations.

## 3. Imagery: federation, enhancement

**Composite sources.** German orthophotos are state-published; any map that
leaves one state uses the `de-dop` composite, which federates all 16 verified
state services. Every member `TileSource` declares a WGS84 `coverage` bbox, and
each tile collects **every member whose bbox intersects the tile**, ordered
smallest-coverage-first so the most specific service wins (Berlin's TrueDOP over
Brandenburg, Bremen over Niedersachsen).

**Why a mosaic and not a winner.** Coverage bboxes are rectangles; state borders
are not. Below roughly z12 one tile spans several states, and each service fills
the ground it does not own with an opaque background — so picking a single
winner paints a white block across its neighbours. Instead every candidate is
requested as `FORMAT=image/png&TRANSPARENT=TRUE` and the results are
alpha-composited bottom-up (widest coverage as base, most specific on top).
Two guards keep the stack honest:

- a layer whose visible pixels quantise to ≤16 distinct colours is a service
  placeholder, not ground, and is dropped (`_has_imagery`);
- `TileSource.white_is_nodata` keys out pure white for services that ignore
  `TRANSPARENT` (Thüringen).

**The 2×2 rule.** One tile on stage is never assembled from a crowd of source
images. Candidates are fetched most-specific-first and the stack stops the
moment it is opaque — whatever a fully covering layer sits on cannot be seen, so
requesting it would be waste — and `MAX_MOSAIC_MEMBERS = 4` caps the rest.
Measured against the live services: a tile inside one state costs **exactly one
request** (it used to cost two whenever state rectangles overlapped), a border
or country-zoom tile 2–3, never more than 4. The same ceiling governs the
degradation ladder in `storage/fallback.py`, which merges at most the four
children directly below a tile.

The merged tile is stored as **JPEG when ≥99.5 % opaque** (keeps packages small)
and as **PNG when partly covered**, so coast, foreign territory and data gaps
stay transparent and let the styled basemap through instead of a painted
rectangle. Bundles therefore mix formats and the REST layer sniffs the content
type from the bytes. Where no member has imagery the tile is counted
(`HarvestReport.uncovered`) and the basemap shows through.

**The imagery ladder — one consistent source per scale.** 20 cm state imagery
is only used from **z13**. Below that it is the wrong tool: each state flies its
own campaign in its own year, so a regional view of 15 mosaics reads as a
patchwork of greens and browns with hard rectangular seams. The bands are:

| Zoom | Source | Resolution | Why |
| --- | --- | --- | --- |
| 0–8 | NASA Blue Marble, ocean-blended (`blue-marble-plus`, GIBS) | 500 m | public domain, whole globe — bathymetry oceans |
| 8–12 | **Sentinel-2 Europa** (Copernicus, processed by BKG) | 10 m | one seamless mosaic — no state seams |
| 13–19 | state DOP20 federation | 20 cm | the detail a location slide is about |

Sentinel-2 comes from BKG's `sentinel_europe` layer: Copernicus data, supplied
free of charge under the Copernicus legal notice, **commercial use included**,
attribution "© Europäische Union, Copernicus Sentinel-2, verarbeitet vom BKG".
It covers roughly 0.7°E–20.5°E / 45.5°N–56.7°N; outside that window the regional
band falls back to Blue Marble. `_resolve_members` picks by zoom band as well as
coverage, so no tile ever asks a source outside its scale. Single-provider (non-federated) sources keep a byte-size
placeholder check instead of the mosaic — an XYZ tile may be legitimately tiny
(uniform deep ocean) and has no second provider to fall through to.

The world band itself is a blend (`blue-marble-plus`): land pixels come from
`BlueMarble_NextGeneration`, ocean pixels from
`BlueMarble_ShadedRelief_Bathymetry` — same GIBS scheme, both public domain —
so the oceans show real depth structure (shelves, ridges, trenches) instead of
flat navy. The two layers are separated per tile by the Natural Earth admin-0
polygons rasterized in `geo/landmask.py` (the same data the border overlay
draws), with a ~1–2 px feathered coastline so they blend cleanly. Tiles the
mask resolves as all-land or all-ocean cost a single upstream fetch; only
coastal tiles fetch both layers, and the blended result is cached in the live
bundle like any other tile, so it seals into offline packages unchanged. The
plain `blue-marble` and `blue-marble-bathy` presets stay available, and the z8
handoff to Sentinel-2 is untouched.

**Enhancement (`RasterAdjust`).** Survey DOPs are radiometrically flat. The spec
carries saturation/contrast/brightness/opacity values mapped 1:1 to MapLibre
`raster-*` paint properties, with defaults tuned for a brilliant, non-overexposed
look (saturation +0.35, contrast +0.18, brightness_max 0.94). Per-project adjustable,
zero cost, applied at render time — originals stay untouched (byte-class
discipline: we never transcode source imagery).

## 4. Cartography: professional by construction

The styled map view uses the **official Protomaps basemap cartography** — the CC0
style layer set from `@protomaps/basemaps` (vendored as `web/vendor/basemaps.js`),
with its fonts (Noto, OFL) and sprite sheets (icons, road shields) served through
our caching asset proxy. This is maintained, cartographer-designed styling: proper
road casings and widths, landuse tints, POI icons, typography — the "not 2005"
guarantee, comparable to Google Maps' visual quality, and restylable (flavors:
light/dark/white/black/grayscale + full color overrides for corporate branding).

**Hybrid stacking** (Google-hybrid semantics): basemap geometry at the bottom,
enhanced orthophoto raster above it, then a subtle white street-net overlay
(ramping in from z ≥ 11 so streets stay readable on mid-zoom city views), then
every label/icon/symbol — so street names and POIs always read on top of imagery.
The runtime (`web/map.js`) splices the raster + overlay directly below the first
symbol layer of the official style. If the style library or vector data are
absent, a minimal built-in dark style keeps the map functional.

**View modes.** Four, switchable at runtime (and per host page via
`payload.default_view`): `hybrid` (imagery + labels), `satellite` (imagery only),
`map-dark` and `map-light` (pure cartography in the official dark/light flavors).
Flavor switches rebuild the style and re-attach routes transparently.

**Corporate theming.** `payload.brand_colors` is a central color override merged
over the flavor before layer generation — any flavor key (`background`, `earth`,
`water`, `park_a`, `major`, `highway`, city label colors, …) can be re-tinted
with a handful of values, so a company palette restyles the whole map without
touching layer definitions. The host's theme system injects this at merge time.

**Decoration scaling.** Pins and highlight circles are authored at their
location's detail zoom and scale geographically as the camera zooms out
(highlights at 2^Δz with a hard fade-out ~2 zooms above their location; pins
more gently, clamped ≥ 0.38) — markers never dominate wide views. Camera poses
default to north-up (bearing 0); bearings remain supported but are opt-in
styling, since mid-flight rotation reads as "broken tiling" on overview maps.

**In-presentation navigation.** `MapSpec.interactive` (default **false**)
enables pan/zoom for viewers inside a published package. Offline safety comes from
the fallback ladder: free navigation beyond the precached choreography degrades
resolution, never fetches.

## 5. The offline completeness contract (publish pipeline)

**Lifecycle.** Three modes, enforced by `MapsApi(mode=...)`
(`RuntimeMode` — `models/runtime_mode.py`):

- **Authoring (online).** The live proxy (`/live/{source}/{z}/{x}/{y}`) is a
  *recording* cache: every tile viewed while editing lands in a bundle. Glyph and
  sprite requests cache the same way. Routing queries OSRM/Valhalla. Harvest/plan/
  package/vector-extract endpoints are available.
- **Mixed.** The live proxy, routing, geocoding, glyph/sprite fetching and
  one-shot boundary GeoJSON stay live and per-request exactly as in "online" —
  but bulk data-provisioning endpoints (harvest, harvest-world, vector/extract)
  refuse (403), so authoring against a live map never silently mints gigabytes of
  new offline data. `/package` still works: it only zips up what an earlier
  "online" harvest already put on disk.
- **Published / presentation (offline).** Live proxy, routing, harvest, and any
  upstream fetching are **disabled (403)**. Tiles serve exclusively from the sealed
  bundle; assets exclusively from the cache shipped in the package. The dev server
  proves this end-to-end via `--mode offline`.

**Coverage model — what "reasonable movements" means, precisely.** The planner
(`harvest/planner.py`) compiles a spec into the exact tile set the authored
choreography can display, on the logical 1920×1080 stage with a ×1.5 margin:

1. **Overview pyramid**: all tiles over the padded (30%) union bbox of every
   camera stop, from the source's min zoom down as long as a level stays within a
   64-tile cap. Carries national overviews and the cruise phase of long flights.
2. **Per-location pyramids**: for each stop, viewport-sized tile sets at every zoom
   from the overview pyramid's floor down to that stop's detail zoom. Carries the
   zoom-out/zoom-in phases of every flyTo and local panning slack.
3. **Flight corridors**: for each consecutive pair of stops, the flyTo arc's apex
   zoom is computed (the highest integer zoom fitting both endpoints in the
   viewport, mercator-corrected). At every zoom between the overview floor and the
   apex, stage-sized viewports are sampled along the path at half-viewport spacing.
   Distant pairs (Stuttgart→Berlin) cruise below the overview floor — no extra
   tiles; nearby pairs (HQ→airport) get their mid-flight tiles guaranteed.

The three sets are deduplicated into one `HarvestPlan`; `/plan` previews tile count
and estimated MB before any download (a host editor shows this as the "map
weight" of a page). Reference: the 5-stop, 2-route demo spec (HQ, airport, Stuttgart,
München, Berlin) plans ~2,000 tiles ≈ 60–90 MB.

**Harvest** (`harvest/harvester.py`): bounded concurrency (6), per-source politeness
delay (100 ms), skip-existing (incremental re-harvest after spec edits), one retry,
per-member grouping for composites. Bundles are **MBTiles** (stdlib sqlite) whose
metadata embeds source id, attribution, license name + URL — provenance travels
inside the file.

**Fallback ladder** (`storage/fallback.py`) — degradation, never the network. If a
presentation somehow requests a tile outside the plan (window aspect ratio beyond
margin, manual exploration), the bundle endpoint synthesizes a stand-in:

1. *Parent fallback*: walk up to 4 ancestor zooms; crop the ancestor's matching
   quadrant and upscale — the "temporarily lower resolution" tile.
2. *Children merge*: compose the up-to-4 child tiles one zoom down into one.
3. Only if neither exists: 404 (styled basemap or background shows).

Responses carry `X-Agentic-Maps-Tile-Fallback: exact|parent|children` so
coverage gaps are observable during authoring QA.

**Vector/label completeness.** Street and label data for a sealed package ship
as regional **PMTiles** extracts produced with `pmtiles extract` from the free
Protomaps planet builds — z0–15 regional extracts per package area (~30–80 MB
each; nothing is pre-shipped in the repo). The `/vector/auto` endpoint picks
the most detailed extract intersecting each tile (bbox intersection — low-zoom
tiles are larger than any extract). Vector tiles above an extract's max zoom
**overzoom natively** in MapLibre (z15 data renders crisply at z19 — vectors
scale), so street labels never need per-zoom duplication.

**Live browsing never needs a minted region.** `build_payload()` always
advertises `vector_url_template`, and when no local extract has a tile,
`/vector/auto/tiles` reads that single tile out of the remote Protomaps
planet archive over an HTTP `Range` request and caches it on disk — browsing
an unprepared city costs what is on screen (~0.5 MB), not a whole extract.
Extracts remain the tool for **sealing offline packages**:
`POST /vector/extract` shells out to `pmtiles extract` against the planet
build for a bbox, which costs about **20 MB and 10 seconds** for a city-sized
z0–15 region (measured, Hannover); `GET /vector/coverage?bbox` reports the
best detail available for an area so a client only mints below
`guaranteed_zoom`. Extraction is authoring-time only; offline mode blocks it
(and the remote-planet reads) like routing and geocoding. Pin
`AGENTIC_MAPS_PLANET_URL` to a dated build for reproducible package data,
otherwise the newest daily build is resolved at call time.

Fonts and sprites are finite cached files, included
wholesale.

**The package** (`package/builder.py`, `POST /package`): one zip per spec —
`spec.json` (locations, embedded routes, imagery adjust), `raster/<spec>.mbtiles`,
`vector/*.pmtiles` (extracts containing any camera stop), `assets/glyphs/**`,
`assets/sprites/**` via the shipped cache, and `manifest.json` (file list, sizes,
merged attributions, license URLs). Packaging refuses to run if the spec was never
harvested. At publish time the host embeds/uploads this package alongside its
own static export; the runtime consumes it via the same URL templates (bundle
routes or data: URIs).

**The other kind of package: a recording.** The planner above compiles a
`MapSpec`, but a page whose stops, flights, rings and routes live entirely in
its own JavaScript (rather than a spec a planner can read) needs a different
answer: play the page through every reachable state and record every
resource it actually requested, then ship exactly those resources — vector
tiles rendered, glyph ranges used, worldwide layers clipped to the recorded
footprint — instead of whole artifacts. That trade suits a single-file
export, where every byte is carried by every reader.

The original recorder (formerly `capture/`) was built specifically around
a former host product's embed pages and was removed while extracting this
codebase into a standalone product. Its generalized rewrite has since
landed as **Sealed Sessions**: `agentic_maps/seal/` (`SessionRecorder`,
`Sealer`, `PageSealer`) driven by `tools/seal_page.py`, replayed by
`web/sealed-runtime.js`/`web/sealed-host.js` — including the fallback ladder
(a viewport just outside a recorded set degrades rather than shows a hole).
See `docs/sealed-sessions.md`.

**Budgets.** Warn at 25 MB imagery per map slide, hard cap configurable; the
`/plan` preview enforces awareness before download. Slimming levers, in order:
lower detail zoom (z17 ≈ 80 cm/px still looks excellent on stage), fewer stops,
smaller corridor margin, `pmtiles extract` the vector data to the package bbox.

### Authoring controls (dev server toolbar / host editor later)

- **Geolocate & add stops**: `POST /geocode` (Nominatim/OSM, authoring-only,
  proper User-Agent) turns a typed place/address into a `MapLocation`; the
  editor then jumps between stops like any authored step.
- **Record mode**: with the steps set, one action plans (preview: tile count +
  MB) and then harvests the entire choreography — including fly corridors and
  the screen-size buffer (planner margin 1.6, raise for 4K recordings).
- **Offline toggle**: `POST /mode {offline}` flips the runtime into
  no-internet-at-all mode (tiles/glyphs/sprites/routing/geocoding all refuse).
- **Clear cache**: `DELETE /cache` removes every recorded/harvested raster
  bundle, package and cached asset; vector extracts are source data and stay.

## 6. Routing (car / truck / walk / bike)

Routing is backend-agnostic behind `routing/base.py`'s `RoutingBackend` protocol
(see `docs/routing.md` for the full reference). Two backends implement it:

- **Valhalla** (`routing/valhalla.py`, default — `AGENTIC_MAPS_ROUTING_BACKEND`
  unset or `valhalla`) — a self-hosted server (`AGENTIC_MAPS_VALHALLA_URL`) that
  covers all four canonical travel modes through its own costing models
  (`car→auto`, `truck→truck`, `walk→pedestrian`, `bike→bicycle`), returns real
  alternate routes, and answers isochrone (reachable-area polygon) queries —
  the only backend that can.
- **OSRM** (`routing/osrm.py`, secondary/legacy — `AGENTIC_MAPS_ROUTING_BACKEND=osrm`)
  — public demo server for light interactive authoring, or
  `AGENTIC_MAPS_OSRM_URL` for a self-hosted container fed with Geofabrik
  extracts (ODbL). No alternates on multi-stop routes, no isochrones, and
  `truck` maps onto its plain driving profile since OSRM has no truck costing.

`GET /routing/capabilities` reports which of these the active backend actually
supports (`alternates`, `isochrone`, `avoid`) so the frontend never has to
find out by catching an error. The result of `POST /route` is a `MapRoute`
(mode, duration, distance, full geometry, optional `steps`, optional
`alternates: list[MapRoute]`) — drawn as a cased line with a duration badge
under the label layers, alternates dimmed and clickable-to-promote. Routing
happens at authoring/query time; a sealed/published map embeds the computed
result and re-runs nothing live.

## 6b. Storytelling & analytics (marketing a building — or an employer)

The map extension's real product is **location storytelling**: slides that sell a
site (real estate exposé) or a workplace ("we are your best new employer").
Building blocks, all offline-embedded at publish:

**Visual storytelling (implemented)**
- Highlight kinds: `circle` (exposé callout), `ping` (sonar attention rings, one
  or many), `radius` (true ground footprint, "500 m / 5 Min zu Fuß"),
  `polygon` (plots, districts, development areas) — all zoom-scaled/gated.
- Animated routes: progressive draw-in with a racing head dot
  (`MapRoute.animate_ms`) — "this is how fast you're on the A8"; duration badge
  lands when the route arrives.
- Multi-ping from offline data: `pingFeatures(['station','bus_stop',...])`
  queries the vector tiles in view and pings every transit stop — no external
  service, works in sealed packages.
- World context: NASA Blue Marble (public domain, GIBS) under the state
  orthophotos — globe-to-doorstep zooms with imagery at every scale.

**Analytics (implemented → roadmap)**
- **Reachability matrix** (implemented): `POST /matrix` — all-pairs travel
  times between stops for any of the four travel modes, rendered as
  "Erreichbarkeit ab HQ" panels. Authoring-time; results embed in the slide.
- **Isochrones** (implemented): "everything within 15 min" polygons via
  `POST /isochrone` (Valhalla backend only — see §6); GeoJSON contour rings
  are rendered through the existing `polygon` highlight — the classic
  employer-commute story.
- **Sociodemographic overlays — Kaufkraft etc.** (roadmap): choropleth
  `polygon` layers fed from open data (BBSR **INKAR** indicators, **Zensus 2022**
  grid: population, income proxies, age structure — DL-DE licenses) with
  commercial data (GfK Kaufkraft) as a licensed drop-in. Model: a
  `MapDataOverlay` (geojson + value scale + legend) embedded per slide.
- **Transit quality** (roadmap, §7): GTFS-based "next departure / frequency"
  badges at pinged stops.

**Playbooks** (authoring presets the host editor offers)
1. *Employer commute*: HQ pin + transit pings + isochrone + matrix panel
   ("your commute from Stuttgart: 24 min").
2. *Site exposé* (the TOP LAGE classic): aerial detail, photo pin, POI
   callouts, Fahrzeiten panel, walking-radius circle.
3. *Expansion/portfolio*: Blue Marble → Germany fly-through over all sites,
   animated routes between them.
4. *Neighborhood quality*: map-light mode + amenity pings (Kita, Gym,
   Bäcker...) + Kaufkraft choropleth.

## 7. Transit: city rail & subway (public data, staged roadmap)

Publicly available, twice over:

- **Geometry now**: OSM (already in our vector tiles) carries all rail, S-Bahn,
  U-Bahn and tram tracks; the official basemap styles render them, so transit
  infrastructure is visible in every map today.
- **Lines & times next (GTFS)**: Germany publishes official GTFS feeds — DELFI
  nationwide, VVS for Stuttgart, comparable feeds for Berlin/München — with route
  shapes (`shapes.txt`), stops, and schedules under open licenses. Roadmap:
  `transit/` module ingesting a GTFS feed at authoring time into (a) colored line
  overlays per route (U6, S2, …) as embedded geometry, and (b) schedule-based
  transit `MapRoute`s ("Hbf → Flughafen: 27 min S-Bahn"). Same contract: computed
  while editing, embedded, offline at presentation.

## 8. Standalone product — no host-application merge planned

agentic-maps ships and is used on its own: its own dev server, its own REST
surface, its own publish pipeline. The `MapsApi.mount(app, *, prefix)` surface
(§2) exists so any FastAPI application can embed it, but no specific host
integration is assumed or designed for here.

## 9. Out of scope for v1

3D terrain/tilt beyond flat pitch, non-mercator projections, live traffic,
geocoding UI (coordinates entered or picked on map), GTFS ingestion (§7 roadmap).

## 10. Operational notes

- Dev server: `poetry run python -m agentic_maps.devserver` (or the
  `agentic-maps-dev` script) (:8095); `--mode offline` for presentation-mode
  proof, `--mode mixed` for live authoring without bulk provisioning.
- Demo deep links: `/?loc=0..n` jumps to a stop instantly (screenshots, QA).
- Regenerate vector extracts: `pmtiles extract https://build.protomaps.com/<date>.pmtiles
  out.pmtiles --bbox=W,S,E,N [--maxzoom=Z]` (CLI via `brew install pmtiles`).
- BKG registration (free) unlocks nationwide imagery: set `AGENTIC_MAPS_BKG_URL`.
