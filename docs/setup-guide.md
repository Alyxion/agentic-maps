# The 2-minute geo server

Everything you need to route and geocode against a place of your choosing,
self-hosted, offline-capable: tile serving (this package), routing
(self-hosted Valhalla), geocoding (self-hosted Nominatim) — all scoped to one
region/city, wired together with Docker Compose.

Two front-ends produce the identical `docker-compose.yml` + `.env`:

- **CLI**: `agentic-maps-setup` (`agentic_maps/setup/wizard.py`, the
  `[tool.poetry.scripts]` entry in `pyproject.toml`)
- **Web**: `web/apps/setup-wizard/` (a local page, calling `POST /setup/plan`
  / `POST /setup/apply` — the same planner underneath, see below)

Both call `agentic_maps/setup/planner.py`, the one place the actual
plan/compose/`.env` logic lives. Nothing about the planning is duplicated
between the two.

## What we verified, and where it came from

Before writing any of this, the actual Docker images, env vars and ports were
checked against each project's own docs — not guessed. What follows is a
condensed summary; every claim below traces back to a primary source.

### Valhalla (routing)

**Image**: `ghcr.io/valhalla/valhalla-scripted:latest` — the official image,
part of the `valhalla/valhalla` monorepo
(`github.com/valhalla/valhalla/blob/master/docker/README.md`). The earlier
community image, `gis-ops/docker-valhalla` (`ghcr.io/nilsnolde/docker-valhalla`),
is now **archived** and its code "moved to upstream" per its own README —
we use the current upstream one. The two have the same env var contract with
one notable default flip: upstream now defaults `build_admins=True` and
`build_time_zones=True` (the archived image defaulted both to `False`).

**Port**: `8002` (the container's `valhalla_service` HTTP API — matches this
package's own `AGENTIC_MAPS_VALHALLA_URL` default in `routing/valhalla.py`).

**Volume**: `/custom_files` — everything the container generates or reads
(PBFs, the built graph, admin/timezone DBs, `valhalla.json`) lives here.

**Env vars that matter for "give it a PBF, build the graph on first boot"**:

| Var | Default | What it does |
| --- | --- | --- |
| `tile_urls` | (empty) | Space-separated PBF URL(s) to download and build from |
| `use_tiles_ignore_pbf` | `True` | `True`: reuse an existing `tile.tar` and skip building. On a genuinely first boot there is no `tile.tar` yet, so the container builds from `tile_urls`/any PBF already in `/custom_files` regardless — this only matters on *later* restarts (see `docker-compose.offline.yml`, which relies on exactly this to avoid ever re-fetching) |
| `build_admins` | `True` | Admin-area DB (border-crossing penalties, driving side) |
| `build_time_zones` | `True` | Timezone DB (needed for time-dependent routing) |
| `build_elevation` | `False` | Downloads elevation tiles covering the graph |
| `server_threads` | `nproc` | Threads for both the tile build and the running service |

Our `deploy/docker-compose.yml` sets `build_time_zones=False` (this product
never asks for time-dependent routing — `RoutingCapabilities.departure_time_affects_route`
is hard-coded `False` in `rest/maps_api.py`) and `build_elevation=False`
(bandwidth/time we don't need for `/route`, `/matrix`, `/isochrone`).

**Build-time reality** (community-reported, not an official benchmark — Valhalla
publishes no first-party timing table): a ~500 MB regional PBF took about
**30 minutes**; a full-country PBF (USA) took **~4 hours**; a full planet
build **~15 hours** on a quad-core/16 GB machine. A genuinely city-sized PBF
(a few MB — what our own Overpass-based extraction below produces) is several
orders of magnitude smaller than the 500 MB regional case, so in practice it
finishes in low single-digit minutes on ordinary hardware — this is the
scale "2 minutes" is actually true for.

### Nominatim (geocoding)

**Image**: `mediagis/nominatim` (Docker Hub; `github.com/mediagis/nominatim-docker`)
— the standard/most-maintained Nominatim container; tag `5.3` at time of
writing (`mediagis/nominatim:5.3`).

**Port**: `8080` (HTTP search/reverse API). Postgres (`5432`) is internal and
not exposed by our compose file.

**Env vars that matter**:

| Var | Default | What it does |
| --- | --- | --- |
| `PBF_URL` | — | Download-then-import-then-discard a PBF from a URL. Mutually exclusive with `PBF_PATH` |
| `PBF_PATH` | — | Import a PBF already present in the container (bind-mounted) |
| `IMPORT_STYLE` | `full` | `admin` / `street` / `address` / `full` / `extratags` — how much OSM tag data gets imported. We default to `street` (admin boundaries + streets: enough for routing/geocoding place names, without importing every POI) |
| `UPDATE_MODE` | `none` | `continuous`/`once`/`catch-up`/`none` — whether to keep polling for diffs after import |
| `NOMINATIM_PASSWORD` | (random) | DB password — set explicitly so the container is reproducible across restarts |
| `shm-size` (a *run* param, not an env var) | `64M` | Nominatim's own docs recommend "at least 1GB, or half your available RAM" for imports past a tiny town — our compose file sets `1gb` |

**Import-time reality**: Nominatim's own official docs
(`nominatim.org/release-docs/latest/admin/Import/`) publish a real benchmark
for a **full planet** import (2020, 64 GB RAM / 4 CPU / NVMe): `admin` style
4 hours, `street` 22 hours, `address` 36 hours, `full` 54 hours — "even on a
perfectly configured machine, a full planet import takes around 2 days."
There is no equivalent official small-extract benchmark, but the scaling is
roughly proportional to data volume: community reports put **Monaco**
(one of the smallest possible extracts) at a few minutes, and general
"city-level" extracts at up to ~30 minutes on a 4-core machine with an SSD.
A genuinely small town/city PBF (what Overpass extraction below produces)
sits at the fast end of that range.

### Getting an OSM PBF scoped to a city — the honest gap

**There is no on-demand, city-scoped `.osm.pbf` extract service with a real,
unauthenticated, scriptable HTTP API.** Specifically:

- **Geofabrik** (`download.geofabrik.de`) only publishes country/state-level
  extracts on a predictable URL — no per-city granularity. Fine for "give me
  Bavaria", useless for "give me just Hannover".
- **BBBike's extract service** (`extract.bbbike.org`) DOES let you draw an
  arbitrary city-sized box and export `.osm.pbf` — but its own documentation
  describes only an interactive web form with a **2-7 minute queue** and
  email notification, with no documented REST/job-polling API for automated
  use (a paid "Extract Pro" tier exists for that, starting at €120/month).

What DOES have a real, documented, scriptable bbox endpoint is the **OSM
API itself**: `GET https://api.openstreetmap.org/api/0.6/map?bbox=W,S,E,N`
returns raw OSM XML for everything inside the box — but it is capped at
**0.25 x 0.25 degrees and 50,000 nodes**
(`wiki.openstreetmap.org/wiki/API_v0.6`), and it is meant for editors
(JOSM/iD), not bulk consumers. Bulk/scripted readers are pointed at the
**Overpass API's mirror of that same endpoint**, `overpass-api.de/api/map`
(identical `bbox=` format, same practical ceiling) — the exact endpoint
OSM's own website "Export" button calls. Overpass's fair-use policy
(<10,000 queries/day, <1 GB/day) comfortably covers "someone runs the setup
wizard a few times."

**So: `agentic_maps/setup/pbf_fetch.py` fetches `overpass-api.de/api/map`
for the bbox and converts the result to `.osm.pbf` with `osmium cat`
(osmium-tool — the same family of CLI `vector/extractor.py` already shells
out to `pmtiles` for) — fully automatic, for a bbox up to 0.25 x 0.25
degrees (roughly a town/small city plus a ring of suburbs; ~27 km x 28 km at
50°N).** Past that size, `planner.choose_pbf_strategy` returns
`"manual-required"`: the wizard asks you to paste a Geofabrik regional/
country `.osm.pbf` URL yourself, or point at a local file. There is no way
around this honestly — no service exists that turns "Munich metro area" or
"Bavaria" into a PBF on demand without either a slow interactive queue or
you supplying the URL.

**`osmium-tool` is a real prerequisite** for the automatic (small-area) path
— install it (`brew install osmium-tool` / `apt install osmium-tool`) before
running the wizard against a town/city. It is not needed for the
`pbf_url`/`pbf_path` override paths.

## What "2 minutes" honestly means

| Area | PBF source | Valhalla build | Nominatim import | Realistic total |
| --- | --- | --- | --- | --- |
| Small town/city (auto, Overpass) | fully automatic | low single-digit minutes | a few minutes | **roughly 2-5 minutes**, decent bandwidth |
| Larger city / small region (you supply a Geofabrik URL) | manual | tens of minutes | tens of minutes | 30-90 minutes |
| A German state / mid-size country | manual | ~30 min - a few hours | a few hours | half a day |
| Whole country (e.g. USA) / continent | manual | ~4+ hours | many hours - a day+ | a day or more |
| Full planet | manual (planet mirror) | ~15 hours | ~2 days (`full` style) | **not attempted by this wizard** |

**The "2 minutes" framing in the original plan is honest, but only for the
smallest case** — a town or small city, resolved automatically via Overpass.
The wizard states this explicitly (the review step shows the resolved bbox's
area and, past the Overpass threshold, an on-screen warning that a manual
PBF is needed and why) rather than let anyone assume the same 2 minutes
scales to "give me all of Bavaria."

## Using the CLI wizard

```
pip install -e ".[rest,fallback]"
agentic-maps-setup
```

Answers: a place name (or an explicit bbox), a mode (`offline`/`mixed`/
`online`), which travel profiles to enable (advisory — see below), and
optionally a PBF URL override. It writes `.env` + copies of
`deploy/docker-compose.yml`, `deploy/docker-compose.offline.yml` and
`deploy/Dockerfile` into an output directory (`./agentic-maps-stack` by
default), then — only if `docker` is on PATH and you say yes — runs
`docker compose up -d` and polls each service's port.

`--yes` skips the confirmation prompt (still requires `docker` on PATH).

## Using the web wizard

Start the isolated dev server (`agentic-maps-dev`, or any host that mounts
both `MapsApi` and `SetupApi`), then open `/apps/setup-wizard/`. Same four
steps as the CLI (place → mode/profiles → review → launch), backed by
`POST /setup/plan` (review-only — resolves the place to a bbox and reports
what would happen, without fetching a PBF) and `POST /setup/apply` (does the
real work: resolves, fetches if the Overpass path applies, writes files, and
— only if you tick "write + docker compose up" — starts the stack).

The page loads Vue + Quasar from the shared `llming-stage` vendor bundle at
`window.agenticStageVendor` (default `/_stage/vendor`), the same convention
`web/globe.js` uses for three.js — see that file's own comment. `llming-stage`
is a private package that installs via an editable install from the sibling
repo and requires Python 3.14+ (`docs/development.md`'s
`dev-server`/`globe`/`debug` extras section); this page is written against
its documented vendor contract but has not been exercised end-to-end
against a live install.

## The "Offline-Daten" step (optional, skippable)

Both wizards end with an optional offline-data step: the region presets
(`de`, `dach`, `eu`, `earth`) × layers (`maps` vector streets, `aerial`
imagery with a required zoom cap 13-16, `routing` PBF staging) with the
size table from `provision/estimates.py` shown BEFORE anything downloads —
the same estimator, presets and two-step confirm the MCP tool
`provision_offline_region` uses (docs/mcp.md, "Offline regions").

- **CLI**: after the stack launch, `agentic-maps-setup` prints the size
  table and asks for a region (blank = skip). A confirmed selection runs
  inline through the same `ProvisionEngine` the server uses, printing
  honest progress counts until done.
- **Web**: step 5 of `/apps/setup-wizard/` — estimate button (`POST
  /api/v1/maps/provision/estimate`), then a "Download …" button that starts
  the job (`POST /api/v1/maps/provision`) and polls its progress; cancel
  and skip at any time. Cancelled/interrupted jobs are resumable — the
  aerial layer caches into the live tile bundles, so a re-run only pays
  for what is missing.

Honesty carried over from the table: Europe aerial z13-15 is ~370 GB and
weeks of polite downloading; Europe routing is a ~35 GB PBF and MANY HOURS
of Valhalla graph build (Germany: tens of minutes to ~1 h). z17+ aerial is
not offered as a region preset at all — that is corridor-harvest territory.

## Manual compose (skip the wizard)

```
cd deploy
cp docker-compose.yml docker-compose.offline.yml Dockerfile /wherever/you/want
cd /wherever/you/want
cat > .env <<'EOF'
AGENTIC_MAPS_REGION_ID="hannover"
AGENTIC_MAPS_REGION_BBOX="9.70,52.35,9.78,52.40"
AGENTIC_MAPS_MODE="mixed"
AGENTIC_MAPS_ENABLED_PROFILES="car"
AGENTIC_MAPS_VALHALLA_URL="http://valhalla:8002"
AGENTIC_MAPS_NOMINATIM_URL="http://nominatim:8080"
AGENTIC_MAPS_API_PORT="8095"
VALHALLA_PORT="8002"
NOMINATIM_PORT="8080"
# Either a URL both containers fetch independently...
VALHALLA_TILE_URLS="https://download.geofabrik.de/europe/monaco-latest.osm.pbf"
NOMINATIM_PBF_URL="https://download.geofabrik.de/europe/monaco-latest.osm.pbf"
NOMINATIM_PBF_PATH=""
# ...or a local file: leave the URLs empty, set NOMINATIM_PBF_PATH to a
# container path under ./nominatim_data/, and drop the same file into
# ./valhalla_data/ (Valhalla auto-detects any PBF there, no env var needed).
AGENTIC_MAPS_REPO_ROOT="/absolute/path/to/agentic-maps"
EOF
docker compose up -d
```

Add `-f docker-compose.offline.yml` on later restarts once the graph is
built and the geocoder is imported, and set `AGENTIC_MAPS_MODE="offline"`, to
guarantee no container ever reaches the internet again. ("mixed" — the
default above — keeps the API's own live tile proxy/routing/geocoding
allowed while still refusing bulk data provisioning; "online" allows
everything, including harvest/vector-extract; see
`agentic_maps/models/runtime_mode.py`.)

## `AGENTIC_MAPS_NOMINATIM_URL`

`rest/maps_api.py`'s `/geocode` and `/reverse-geocode` endpoints previously
hard-coded the public `nominatim.openstreetmap.org` (light interactive use
only, per its own usage policy — the same caveat that already existed in the
code comments). They now read `AGENTIC_MAPS_NOMINATIM_URL` first, falling
back to the public instance when unset — the same pattern `AGENTIC_MAPS_VALHALLA_URL`
/ `AGENTIC_MAPS_OSRM_URL` already established for routing. Point it at your
self-hosted Nominatim (`http://nominatim:8080` inside the compose network,
or `http://localhost:8080` from the host) to stop hitting the public
instance entirely.

## Why `profiles` doesn't change the compose file

Valhalla builds one graph with every costing model (`auto`/`truck`/
`pedestrian`/`bicycle`) regardless of which profiles you pick — there is no
per-profile tile build. `profiles` only ends up in `.env` as
`AGENTIC_MAPS_ENABLED_PROFILES`, advisory metadata for a future API/UI
filter; it is not wired into the Valhalla/Nominatim service definitions.
