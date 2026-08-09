# MCP server — the whole capability surface as agent tools

agentic-maps exposes everything it can do over the
[Model Context Protocol](https://modelcontextprotocol.io): georeferencing,
routing, map-content extraction, server-side rendering, licensing/attribution,
runtime-mode control and offline-bundle provisioning. Two transports, one
implementation:

- **stdio** — the `agentic-maps-mcp` command, what a Claude Code / Claude
  Desktop config points at;
- **streamable HTTP at `/mcp`** — mounted on the dev server whenever the
  `mcp` extra is importable (feature-detected with a one-line startup notice
  either way, exactly like the stage-assets mount).

Install:

```bash
pip install -e ".[rest,fallback,mcp]"
```

## Architecture

Every MCP tool is a thin adapter over the REST surface, called through an
**in-process ASGI client** (`httpx.ASGITransport`) against a FastAPI app
mounting the same `MapsApi` — one source of truth for validation,
runtime-mode gating and attribution; no logic duplicated into the tool
layer, no self-HTTP over sockets (`agentic_maps/mcp_server/server.py`).

On the dev server the MCP layer wraps the **same `MapsApi` instance** the
browser UI uses: switching the runtime mode via the `set_runtime_mode` tool
switches it for the UI session too — there is one mode, not one per
protocol.

All bytes flow through the server: `render_map` returns the image inline as
MCP content; a client is never handed a third-party URL to fetch.

## Tool roster

| Tool | What it does | Caps | Mode dependency |
| --- | --- | --- | --- |
| `geocode` | Name/address → locations with structured address (postcode!) | limit ≤ 10 | online/mixed |
| `reverse_geocode` | Coordinate → nearest name + structured address | — | online/mixed |
| `search_places` | Offline city-index autocomplete + bounded geocode, merged | limit ≤ 20/source | index: any mode; geocoder half: online/mixed |
| `route` | One-shot multi-stop routing: one geometry, legs, turn-by-turn (`ref`, lanes), `via_places` (priority-ordered "via Pforzheim" cities, per candidate), `stop_details` with structured addresses | 2..100 stops, alternates 0..3 (2-stop trips only), avoid per capabilities | online/mixed |
| `create_trip` / `update_trip` / `get_trip` / `list_trips` | LIVE trips: the computed route stays alive server-side; `update_trip` applies an op list (`add_stop` at position, `remove_stop`, `move_stop`, `set_options`) and recomputes exactly once | store: ≤ 24 trips / ≤ 16 MB estimated, LRU eviction, 60 min idle TTL (refreshed on touch) | online/mixed |
| `open_map_view` | Stores the results as a browser session and opens the FULL map app (route + clickable alternates + via labels + panels) in the local user's default browser; accepts routes, markers, highlights, a raw spec or `trip_id` — trips get ONE stable session whose open tab updates itself via revision polling, later calls answer `already_open` instead of a new tab | session TTL 60 min past last poll, spec ≤ 2 MB | any (page itself obeys the mode) |
| `get_map_page_html` | ONE self-contained HTML document: our design, clickable alternates that swap the steps list (inline JS, works from `file://` offline), via labels, German turn-by-turn, attribution footer **baked in** | ≤ 1.5 MB, steps cut at 60 with "+N weitere Schritte"; `basemap=auto\|embedded` | any |
| `route_matrix` | NxN travel-time matrix in minutes | 2..100 points | online/mixed |
| `optimize_trip` | TSP stop ordering via the backend's NATIVE solver (OSRM `trip` / Valhalla `optimized_route`): pass `stops` or a `trip_id` (the live trip is reordered + recomputed, revision bumped, so every trip view reflects it); returns `order` + the full route in that order | 2..100 stops; `keep_endpoints=false` OSRM-only; Valhalla optimizes `truck` as car | online/mixed |
| `reachability` | One-to-many minutes from an origin — explicit `targets` or a server-generated `grid` {bbox, step_km, cap}; asymmetric matrix underneath, never NxN | public demo ~100 coords/request, no chunking; self-hosted: raise OSRM `--max-table-size` / Valhalla `service_limits.<costing>.max_matrix_locations`, then `AGENTIC_MAPS_MATRIX_MAX` unlocks chunking up to 5000 targets | online/mixed |
| `provision_offline_region` | Whole-region offline data packs (see "Offline regions" below): first call returns the size estimate and starts NOTHING; `confirm_size=true` starts the async job | regions de/dach/eu/earth or custom bbox; aerial zoom cap 13..16 only | **online only** (estimate half is free in any mode) |
| `provisioning_status` / `cancel_provisioning` | Poll job progress (honest tiles/bytes counts, no ETA) / stop a job; cancelled and interrupted jobs are resumable — re-issue the same request, cached data is skipped | — | any |
| `isochrone` | Reachability contours as GeoJSON | ≤ 4 contours; **Valhalla backend only** (clean error on OSRM) | online/mixed |
| `routing_capabilities` | What the active backend really honours | — | any (avoid probe skipped offline) |
| `extract_features` | GeoJSON features from the basemap: buildings (with `height` m), roads, pois, water, landuse, places | ≤ 64 tiles (~6×6 km at z15), ≤ 5000 features default (20000 max) | any; live tile fetch needs online/mixed, offline serves bundled/cached |
| `street_survey` | Decoded street-line geometry in a bbox | 16 tiles | any (same ladder as above) |
| `render_map` | Screenshot of the real map runtime, returned inline | ≤ 1600×1200 px, scale 1/2/3; needs Playwright + Chromium; seconds per call | tiles it draws respect the mode |
| `list_tile_sources` | All sources/composites with license fields verbatim | — | any |
| `map_attribution` | The exact credit line for a view (per-tile winner resolution) | — | any |
| `get_runtime_mode` / `set_runtime_mode` | Read/switch `offline` / `mixed` / `online` for the whole instance | — | any |
| `plan_offline_bundle` | Tile count + MB estimate, downloads nothing | — | any |
| `harvest_offline_bundle` | Download a spec's tiles into a local bundle | can take minutes; polite to upstreams | **online only** (refused in mixed AND offline) |
| `package_offline_bundle` | Zip an already-harvested spec (manifest carries the full license list) | — | any (reads local data only) |
| `list_offline_bundles` | Inventory of local raster bundles + vector extracts | — | any |

Every cap, unit and mode dependency is also stated in the tool description
text itself — the numbers are f-strings over the same named constants that
enforce them (`agentic_maps/mcp_server/server.py`), so description and
behaviour cannot drift apart.

## Offline regions — "I want offline routing for all Europe"

`provision_offline_region` turns that sentence into one confirmed job. Three
layers per region: `maps` (vector streets/labels z0-15, `pmtiles extract`
from the Protomaps planet), `aerial` (the imagery ladder, pulled through the
same band dispatcher and cache-through bundles the live `/aerial` endpoint
serves from), `routing` (the Geofabrik PBF staged into the compose stack's
`valhalla_data/` + `nominatim_data/`; `start_stack=true` additionally runs
`docker compose up -d` when Docker exists — otherwise the job reports the
exact next command instead of failing).

**Two-call contract**: the first call answers with the size table below and
starts nothing; repeating it with `confirm_size=true` starts exactly that
download. One call to see the cost, one to commit.

Sizes (computed by `provision/estimates.py` — the ONE source the tool
description, the wizard and this table all quote; ~12 KB/tile average,
PBF sizes measured 2026-08-09):

| Selection | Size |
| --- | --- |
| `earth` aerial (Blue-Marble ladder z0-8 + Sentinel-2 z9-12) | ~1.7 GB |
| `de` vector streets z0-15 | ~168 MB |
| `de` aerial z13 / z13-14 / z13-15 / z13-16 | ~0.7 / 3.7 / 15 / 62 GB |
| `dach` aerial z13-15 | ~22 GB |
| `eu` aerial z13-15 | ~370 GB — offered, with a strong warning: weeks of polite downloading |
| `de` / `dach` / `eu` routing PBF (Geofabrik) | ~4.8 / 6.2 / 35 GB |

z17+ aerial is deliberately NOT offered as a region preset (Germany z13-19
would be ~3.9 TB) — full-resolution imagery stays a corridor/spec harvest
(`plan_offline_bundle`/`harvest_offline_bundle`).

**Routing-time honesty**: downloading the PBF is the fast part. The first
`docker compose up` then builds the Valhalla graph — tens of minutes to
about an hour for Germany, MANY HOURS plus a large tile set on disk for
Europe; Nominatim's import scales the same way. The tool description says
this out loud so nobody confirms a 35 GB download expecting a 5-minute
Europe router.

**Resumability**: aerial tiles land in the same `live-*` bundles the live
map reads, so an interrupted/cancelled job re-POSTed skips everything
already on disk; PBF downloads resume over HTTP Range. Jobs persist as JSON
under the bundles dir — a restarted server reports them as "interrupted,
resumable" instead of forgetting them.

The setup wizard (CLI `agentic-maps-setup` and `/apps/setup-wizard/`) offers
the same presets with the same table as its final, skippable "Offline-Daten"
step; over REST the surface is `POST /provision/estimate` (free),
`POST /provision` (online mode only), `GET /provision[/{job_id}]`,
`POST /provision/{job_id}/cancel`.

## Show results in the browser

The point of the display tools: the MCP client never has to rebuild a map
from raw geometry. The Cowork flow is three calls:

1. `route` (or `create_trip`) — the data: geometry, legs, steps,
   `via_places`, structured stop addresses.
2. `open_map_view` — mints a browser session (`POST /sessions`, token, TTL
   60 min) and opens `…/?session=<token>` in the user's own default
   browser: the full map application with sage cartography, the route with
   its clickable alternates, brief via-place labels ("über Pforzheim" — the
   road label "über A 8" demoted to the secondary line), badges and the
   turn-by-turn panel. This is a LOCAL server opening the LOCAL user's
   browser at the user's own request; `open_browser=false` returns only the
   URL. URL composition follows the transport: on HTTP it is the dev
   server's public base, over stdio the loopback render host that already
   serves the full app.
3. `get_map_page_html` — when the client wants to show the result inside
   its OWN surface (artifact/preview): one self-contained document instead
   of a link.

`open_map_view(trip_id=…)` keeps ONE stable session per trip: the first
call opens the browser tab; that tab then polls the lightweight
`GET /sessions/{token}/revision` (~2.5 s cadence, visible tabs only) and,
on a bump, re-fetches the payload and applies it IN PLACE — route redrawn
through the normal machinery, a subtle „Route aktualisiert" toast, camera
untouched unless the fresh route is entirely off screen (then one gentle
fit). Trip-bound sessions serve the trip's CURRENT state on every read,
so later `open_map_view(trip_id=…)` calls default to NOT opening another
tab: they return the same URL with `already_open: true`. `open_browser=
true` forces a (re)open, `false` never opens; a polling tab keeps its
session alive 60 min past the last poll, and once the trip itself expires
the session degrades to its last frozen copy instead of breaking the tab.

## Copyright containment

The observed failure mode: an MCP client with raw geometry built an ad-hoc
Leaflet page against `tile.openstreetmap.org` — which violates the OSMF
tile-usage policy and silently drops every attribution obligation this
product exists to honour. That is why the display tools exist and why
clients should use them instead of rebuilding maps from `geometry`:

- `open_map_view` shows OUR surface, which resolves and renders the exact
  credit line for what is on screen;
- `get_map_page_html` bakes the attribution footer into the document — it
  is part of the template, not a parameter, so there is no way to obtain
  the HTML without the credits — and references no third-party host in any
  tier: `basemap=auto` uses THIS server's own tile endpoints (degrading
  per-tile to the embedded rendering when unreachable), `basemap=embedded`
  emits zero network references and draws the route over a styled canvas
  with graticule and scale bar. All icons/glyphs are inline SVG — no
  external asset can ever appear as a broken placeholder.

## Live trips & where state lives

`create_trip` keeps the computed route as mutable server-side state so
"insert a stop, get the fresh result" is one call (`update_trip`) instead
of the client resending geometry. The store is bounded (24 trips / 16 MB
estimated, LRU eviction, 60 min idle TTL) and its rules are printed in the
tool descriptions; an evicted id errors with "recreate it with
create_trip". State lives in the server process: over stdio that process
belongs to one MCP client session (the Cowork case); at `/mcp` on the
shared dev server instance trips are visible to every connected client —
acceptable for a single-user dev machine, worth knowing on anything
shared. Stop addresses are enriched once at creation (reverse geocode,
cached on the stop; offline mode shows coordinates honestly instead).

## The three runtime modes, as tools see them

- `online` — everything allowed, including provisioning.
- `mixed` — live per-request lookups (routing, geocoding, live tiles) work;
  bulk provisioning (`harvest_offline_bundle`, vector extraction) is refused:
  authoring against a live map without silently minting gigabytes.
- `offline` — presentation mode. Only bundled/cached data serves;
  every upstream connection is refused. `extract_features`/`street_survey`
  degrade to what is local (missing tiles are absent from the answer, not an
  error), which is exactly how you prove a harvested bundle is complete.

## stdio config (Claude Code / Claude Desktop)

```json
{
  "mcpServers": {
    "agentic-maps": {
      "command": "agentic-maps-mcp",
      "args": ["--mode", "mixed"]
    }
  }
}
```

`--bundles-dir` overrides the default (`var/bundles` in the repo, the dev
server's convention); `AGENTIC_MAPS_MODE` is the env equivalent of `--mode`.
The usual backend env vars apply (`AGENTIC_MAPS_ROUTING_BACKEND`,
`AGENTIC_MAPS_OSRM_URL`, `AGENTIC_MAPS_VALHALLA_URL`,
`AGENTIC_MAPS_NOMINATIM_URL`).

`render_map` over stdio: the headless-Chromium page needs a real HTTP origin,
so the stdio server starts a loopback helper (ephemeral port, uvicorn,
feature-detected) serving the same `MapsApi` + `web/`; set
`AGENTIC_MAPS_RENDER_BASE_URL` instead to reuse an already-running instance.

## HTTP transport at `/mcp`

Start the dev server with the `mcp` extra installed and the endpoint is
there:

```bash
AGENTIC_MAPS_ROUTING_BACKEND=osrm python -m agentic_maps.devserver --port 8095
# → [maps] MCP endpoint mounted at /mcp (streamable HTTP)
```

Any streamable-HTTP MCP client connects to `http://127.0.0.1:8095/mcp`.
Live end-to-end verification against a running server:
`python tools/verify_mcp.py --url http://127.0.0.1:8195/mcp`.

**Remote use** follows the established pattern: put a cloudflared tunnel in
front of `/mcp` and hand the tunnel URL to the remote MCP client — the
server itself keeps listening on loopback and all bytes keep flowing through
it. The SDK's DNS-rebinding protection admits only localhost `Host` headers
by default; a tunneled deployment sets `AGENTIC_MAPS_MCP_ALLOWED_HOSTS` to a
comma-separated allowlist of public hostnames (or `*` to disable the check,
the tunnel then being the access boundary).

## Licensing & attribution obligation

Licensing is this product's hard constraint, and it rides through the MCP
surface unchanged: whenever rendered (`render_map`) or extracted
(`extract_features`, `street_survey`) output is redistributed, the credit
line from `map_attribution` for that exact view **must accompany it** — the
tool resolves per tile, so it credits precisely the sources whose data is in
the view, and `list_tile_sources` carries every source's
`license_name`/`license_url` verbatim for anything beyond the one-line
credit.
