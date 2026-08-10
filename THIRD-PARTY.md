# Third-party software, assets, data & services

Everything agentic-maps depends on — vendored code, runtime-served assets,
Python packages, geodata, deployment container images, and system-level
prerequisites — with the exact license and the obligation it carries.

How this inventory was built: read from the tree (`pyproject.toml`,
`web/vendor/` bundle banners, the provenance headers in `tools/make_*.py`,
`sources/presets.py`, `deploy/`), cross-checked against installed package
metadata (`pip show`) and each upstream project's own license statement.
Anything that could not be verified is **flagged as such** rather than
guessed. For imagery specifically, the authority remains
[`docs/imagery-coverage.md`](docs/imagery-coverage.md) — the worldwide
survey of what may legally ship inside a sealed offline package.

agentic-maps itself is MIT-licensed ([`LICENSE`](LICENSE)).

---

## 1. Vendored frontend libraries (`web/vendor/`)

Checked directly against the shipped files' own license banners. No CDN is
ever called; these are the only copies the frontend runs.

| Component | Version | License (SPDX) | Role | Obligation |
| --- | --- | --- | --- | --- |
| **MapLibre GL JS** (`maplibre-gl.js`) | 5.24.0 (from the bundle's `@license` banner) | BSD-3-Clause | The whole 2D map renderer: style engine, tile loading, camera, symbol collision | Retain the copyright notice + license text in redistributions (the banner in the bundle satisfies this) |
| **MapLibre GL CSS** (`maplibre-gl.css`) | ships with 5.24.0 | BSD-3-Clause | Control/attribution-badge styling for the map canvas | Same as above |
| **@protomaps/basemaps** (`basemaps.js`) | not stated in the minified bundle — **flagged** (upstream latest is 5.x; the bundled API matches the documented flavor/layer-generation contract) | Code: BSD-3-Clause · visual design: CC0-1.0 (by Geraldine Sarmiento) · layer schema: MIT (adapted from Tilezen) — per upstream `LICENSE.md` | The professional basemap cartography: layer definitions, flavors (light/dark/white/black/grayscale), localized label expressions | BSD notice retention for the code; CC0 design and MIT schema impose none beyond notice preservation |

## 2. Third-party assets served at runtime

### Proxied and cached from Protomaps (`/api/v1/maps/assets/…`)

Fetched once from `protomaps.github.io/basemaps-assets` (the upstreams
hard-coded in `rest/maps_api.py`), cached on disk, and shipped inside
offline packages wholesale.

| Asset | License | Role | Obligation |
| --- | --- | --- | --- |
| **Noto Sans glyphs** (Regular / Medium / Italic, PBF ranges) | SIL OFL 1.1 (per `fonts/OFL.txt` in protomaps/basemaps-assets) | Every map label the vector basemap draws | OFL: keep the font license with the fonts; no obligation on rendered text |
| **Sprite sheets** (POI icons, road shields, 1x–3x densities) | MIT — derived from the `tangrams/icons` set, per the basemaps-assets README | Basemap POI icons and shield graphics | MIT notice retention |

> Note: `docs/concept.md` §1 previously recorded the sprites as CC0; the
> upstream repository actually states they are MIT-derived. Corrected here
> and in the concept table — MIT is equally redistributable, only the
> notice-retention obligation differs.

### Served through the shared `llming-stage` mount (`/_stage`, optional extras)

Nothing `llming-stage` ships is vendored a second time in this repo
(`pyproject.toml` states the policy). agentic-maps pages reference exactly
these pieces of the shared bundle; licenses verified from the bundled
files' own banners in the installed package:

| Component | Version | License | Used by | Obligation |
| --- | --- | --- | --- | --- |
| **three.js** (`three.module.min.js`) | r185 (banner: `SPDX-License-Identifier: MIT`) | MIT | `web/globe.js` — the 3D globe | Notice retention |
| **Vue** (`vue.global.prod.js`) | 3.5.13 (banner) | MIT | `web/apps/setup-wizard/` only | Notice retention |
| **Quasar** (`quasar.umd.prod.js` + CSS) | 2.17.7 (banner) | MIT | `web/apps/setup-wizard/` only | Notice retention |
| **Phosphor icons** (`/_stage/icons/regular/*.svg`) | MIT (upstream `phosphor-icons`) | Inline UI icons in `web/index.html` / `web/view.html` (fetched, inlined, `fill="currentColor"`) | Notice retention |

`llming-stage` additionally bundles fonts and other vendor libraries for
its own component system; agentic-maps' map pages use system font stacks
and do not reference them (map labels come from the Noto glyphs above).
Their licensing is the `llming-stage` package's own concern (MIT-licensed
package, published on PyPI).

### Static assets in this repo (`web/assets/`)

| Asset | Source / generator | License | Notes |
| --- | --- | --- | --- |
| `globe-biomes.webp`, `globe-biomes-dark.webp` | **Natural Earth II** shaded-relief raster (`NE2_50M_SR`), remapped by `tools/make_globe_biomes.py` (full provenance header in the script) | Public domain (Natural Earth terms of use) | The globe's simplified landcover base (sand deserts, sage vegetation, pale tundra). No attribution required; Natural Earth is credited in the app's attribution line anyway |
| `earth.webp` | NASA **Blue Marble**-family equirectangular texture (`web/globe.js` describes the sphere as "a Blue Marble sphere") | NASA imagery — public domain | **Flagged:** unlike the biome asset there is no generator script or provenance header in the repo pinning the exact NASA product/processing; the family and PD status are documented, the precise derivation is not |
| `layers-map.png`, `layers-sat.png` | Thumbnails of this product's own map views (the layers-control preview chips) | Derived views — underlying data © OpenStreetMap contributors (ODbL) / state orthophoto providers | UI-internal previews; on-map attribution machinery already credits the underlying sources |

## 3. Python dependencies

Pins from `pyproject.toml`; versions and licenses read from installed
package metadata (`pip show`). None are copyleft; obligations are notice
retention only (plus Apache-2.0's NOTICE/patent terms where marked).

### Core (always installed)

| Package | Pin | Installed | License | Role |
| --- | --- | --- | --- | --- |
| **pydantic** | `>=2.7,<3.0` | 2.13.4 | MIT | Every domain model (one model per file under `agentic_maps/models/`) |
| **httpx** | `>=0.27,<0.29` | 0.28.1 | BSD-3-Clause | All upstream HTTP: WMS/tile fetching, routing backends, remote-PMTiles range reads; in-process ASGI client for the MCP layer |
| **pmtiles** (python) | `>=3.2,<4.0` | 3.7.0 | BSD-3-Clause | Read-only access to Protomaps regional extracts (vector street/label layer) |

### Optional extras

| Package | Extra(s) | Pin | Installed | License | Role |
| --- | --- | --- | --- | --- | --- |
| **pillow** | `fallback`, `dev-server` | `>=10.0` | 12.3.0 | MIT-CMU | Tile fallback synthesis (parent-crop upscale / children merge), landmask rasterizing, biome asset generation |
| **fastapi** | `rest`, `dev-server`, `render`, `mcp` | `>=0.115` | 0.141.1 | MIT | The REST surface (`rest/`), kept out of the framework-free core |
| **uvicorn[standard]** | `dev-server` | `>=0.30` | 0.52.1 | BSD-3-Clause | ASGI server for the isolated dev server |
| **playwright** | `render` | `>=1.40` | 1.62.0 | Apache-2.0 | Headless-Chromium driver behind `POST /render` and the seal recorder. Apache-2.0: keep license + NOTICE if redistributing |
| **mcp** | `mcp` | `>=2.0,<3.0` | 2.0.0 | MIT | Official Model Context Protocol SDK — the agent-tool surface (`mcp_server/`) |
| **llming-stage** | `dev-server`, `globe` | `>=0.1.4,<0.2.0` | 0.1.5 | MIT | Shared frontend vendor bundle (three.js, Vue, Quasar, icons). Requires Python ≥ 3.14 |
| **llming-com** | `dev-server`, `debug` | `>=0.1.7,<0.2.0` | 0.1.7 | MIT | WebSocket bridge behind the opt-in frontend debug surface. Requires Python ≥ 3.12 |

Notable transitive dependency: **starlette** (BSD-3-Clause), pulled in by
FastAPI.

### Development-only

| Package | Pin | Installed | License |
| --- | --- | --- | --- |
| **pytest** | `>=8.0` | 9.1.1 | MIT |
| **pytest-asyncio** | `>=0.24` | 1.4.0 | Apache-2.0 |

## 4. Data sources

The product's hard constraint applies to every row: bulk download, storage
and redistribution inside a sealed offline package must be permitted.
Attribution strings live as **required pydantic fields** on every
`TileSource` (`sources/presets.py`) — a source without credit cannot be
constructed. Full survey, including candidates and permanent exclusions:
[`docs/imagery-coverage.md`](docs/imagery-coverage.md).

| Data | License | Required credit | Where it flows |
| --- | --- | --- | --- |
| **OpenStreetMap** street/label/POI vectors, via **Protomaps planet builds** (single-file PMTiles, bulk download expressly permitted) | **ODbL 1.0** | "© OpenStreetMap" on-map; full credit in package manifests | Vector basemap, extracts, feature/street extraction. Stance already documented in `docs/imagery-coverage.md` §1: a sealed offline package is a *Produced Work* under ODbL, so share-alike does not reach the package — attribution still does |
| **Natural Earth** (admin-0/1, lakes, rivers, urban areas, populated places, marine polygons) | Public domain | None required (credited anyway) | Country borders + names in 25 languages, ocean labels, landmask for the ocean-blend, fallback place index |
| **Natural Earth II** raster (`NE2_50M_SR`) | Public domain | None required | The globe biome asset (§2 above) |
| **GeoNames** `cities15000` | **CC BY 4.0** (per the dump's own readme.txt) | Exactly: **"GeoNames (geonames.org)"** — wherever the names are shown or redistributed | Dense place index (~32k places ≥ 15k population): via-place route labels, city autocomplete. The credit travels in map payloads, exported pages and package manifests (`tools/make_city_index.py` documents the wiring) |
| **NASA Blue Marble** — `BlueMarble_NextGeneration` (land) + `BlueMarble_ShadedRelief_Bathymetry` (oceans), served via **EOSDIS GIBS** WMTS, blended per tile as `blue-marble-plus` | Public domain (NASA imagery policy) | None required; the courtesy line "NASA Blue Marble / EOSDIS GIBS" is carried in the presets and rendered on-map anyway | World imagery band, z0–8 |
| **Sentinel-2 Europa** (Copernicus, processed by BKG) | Copernicus legal notice — free of charge, **commercial use included**, source note required | "© Europäische Union, Copernicus Sentinel-2, verarbeitet vom BKG" | Regional imagery band z8–12, one seamless 10 m mosaic over central Europe |
| **German state orthophotos** — all 16 state surveys, 20 cm (Bremen 10 cm) | Three families, per state: **DL-DE/BY-2.0** (attribution), **DL-DE-Zero-2.0** (no conditions), **CC BY 4.0** | Per-state attribution strings baked into `sources/presets.py` (e.g. "© LGL Baden-Württemberg, dl-de/by-2-0"); resolved per tile so a view credits exactly the states whose pixels are shown | Detail imagery band z13–19, federated as the `de-dop` composite. Endpoint/layer/license table per state: `docs/concept.md` §1; verification history: `docs/imagery-coverage.md` §2 |
| **Geofabrik** OSM extracts (`download.geofabrik.de`) | ODbL 1.0 (it is OSM data) | "© OpenStreetMap" | Routing-graph source PBFs for the self-hosted Valhalla/Nominatim stack and the `routing` provisioning layer |

Permanently excluded, by policy (no amount of attribution helps):
`tile.openstreetmap.org` raster tiles (OSMF forbids bulk download), Google,
Mapbox, Bing/Azure, Esri, EOX Sentinel-2 cloudless. See
`docs/imagery-coverage.md` §5.

## 5. Deployment container images (`deploy/`)

Used only by the optional "2-minute geo server" compose stack; nothing in
the Python package links against them — they are separate services spoken
to over HTTP.

| Image | License | Role |
| --- | --- | --- |
| `ghcr.io/valhalla/valhalla-scripted:latest` | **MIT** (Valhalla states: "Valhalla, and all of the projects under the Valhalla organization, use the MIT License") | Self-hosted routing: all four travel modes, alternates, isochrones, matrices, TSP |
| `mediagis/nominatim:5.3` | Image packaging: **CC0-1.0** (mediagis/nominatim-docker). Contains **Nominatim** itself: current 5.x states GPL-3.0 for the Python source ("GPL license version 3 or later"), GPLv2 for other files, Apache-2.0 for Lua configs | Self-hosted geocoding (`/geocode`, `/reverse-geocode` via `AGENTIC_MAPS_NOMINATIM_URL`) |
| OSRM (`Project-OSRM/osrm-backend`) — referenced as the secondary routing backend (`routing/osrm.py`), **not shipped** in the compose stack; you self-host it or use the public demo | **BSD-2-Clause** | Alternative routing backend (no isochrones, no truck costing) |

**The Nominatim GPL implication, precisely.** Nominatim runs as an
independent service in its own container and this codebase communicates
with it exclusively over its HTTP API. GPL obligations (source
availability, license preservation) attach to *distributing or modifying
Nominatim itself* — they do **not** propagate across a network API to an
independent client program, so agentic-maps remains MIT. If you
redistribute the deployed stack (e.g. hand a customer a machine image
containing the Nominatim container), you are distributing GPL software and
must pass on its license and source-availability rights — which the
unmodified public images already satisfy; point recipients at the upstream
repositories. Nothing about *running* the service, or shipping map data
produced while using it, is affected.

## 6. System requirements

| Requirement | Needed for | Notes / license where relevant |
| --- | --- | --- |
| **Python 3.11+** | Core + `rest` + `fallback` + `render` + `mcp` extras, full test suite | PSF-2.0 |
| **Python 3.14+** | `dev-server` / `globe` / `debug` extras | `llming-stage` requires ≥ 3.14 (`llming-com` ≥ 3.12); a 3.11 venv silently loses the 3D globe — see `docs/development.md` |
| **node** | Optional: the sealed-runtime JS parity tests (`tests/test_sealed_runtime_js.py`) — they skip cleanly without it | MIT-family |
| **pmtiles CLI** (go-pmtiles) | `POST /vector/extract` and the `maps` provisioning layer shell out to `pmtiles extract` (`brew install pmtiles`) | BSD-3-Clause |
| **osmium-tool** | The setup wizard's automatic small-area PBF path (Overpass XML → `.osm.pbf` via `osmium cat`); not needed for `pbf_url`/`pbf_path` overrides | **GPL-3.0** — invoked as a separate CLI process, never linked; no effect on this codebase's license |
| **Playwright Chromium** | `POST /render`, `tools/seal_page.py`, `tools/verify_*.py` | Separate one-time download after `pip install`: `playwright install chromium` (multi-hundred-MB); or point `AGENTIC_MAPS_RENDER_CHROMIUM_PATH` at an existing binary |
| **Docker + Compose** | Deploy stack only (`deploy/`, setup wizard's launch step, `start_stack=true` provisioning) | Not needed for the library, dev server, tests or MCP stdio |

### Disk / RAM guidance for offline provisioning

From the one estimator all surfaces quote (`provision/estimates.py`; full
table and honesty notes in [`docs/mcp.md`](docs/mcp.md)):

| Selection | Size |
| --- | --- |
| `earth` aerial (Blue-Marble ladder z0–8 + Sentinel-2 z9–12) | ~1.7 GB |
| `de` vector streets z0–15 | ~168 MB |
| `de` aerial z13 / z13–14 / z13–15 / z13–16 | ~0.7 / 3.7 / 15 / 62 GB |
| `dach` aerial z13–15 | ~22 GB |
| `eu` aerial z13–15 | ~370 GB (weeks of polite downloading — offered with a warning) |
| `de` / `dach` / `eu` routing PBF (Geofabrik) | ~4.8 / 6.2 / 35 GB |

z17+ aerial is deliberately not offered as a region preset (Germany z13–19
would be ~3.9 TB); full-resolution imagery stays a corridor/spec harvest.
Beyond disk: Valhalla's Germany graph build takes tens of minutes to ~1 h
(Europe: many hours); Nominatim recommends `shm_size` ≥ 1 GB for imports
past a tiny town, and its own docs put a full-planet import at ~2 days on
64 GB RAM — the compose stack is scoped to a region for a reason.

## 7. Flagged as unverifiable (rather than guessed)

- **`web/vendor/basemaps.js` version** — the minified bundle carries no
  version banner; license verified from upstream, exact bundled version
  not pinnable from the file alone.
- **`web/assets/earth.webp` derivation** — NASA Blue Marble family (public
  domain) per the code comment in `web/globe.js`, but no in-repo generator
  or provenance header pins the exact NASA product/processing chain the
  way `tools/make_globe_biomes.py` does for the biome asset.
- **France IGN terms** — `docs/imagery-coverage.md` already flags that the
  service's `AccessConstraints` points at the cartes.gouv.fr CGU rather
  than naming a license; must be read before that candidate ships. Listed
  here for completeness; it is a candidate, not a shipped source.
