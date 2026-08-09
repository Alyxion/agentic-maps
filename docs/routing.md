# Routing — full backend reference

Turn-by-turn routing, all-pairs travel-time matrices and isochrones, behind
one backend-agnostic contract. This is the reference `docs/concept.md` §6
and `docs/features.md` §3 point at; it goes one level deeper than either —
straight into `agentic_maps/routing/base.py`, `valhalla.py` and `osrm.py`,
which carry extensive docstrings citing exactly where each request/response
shape was verified against the upstream project's own docs or source.

Routing runs at **authoring/query time only**. The result of `POST /route`
(a `MapRoute`: mode, duration, distance, full geometry, optional turn-by-turn
`steps`, optional `alternates`) is embedded into a `MapSpec`, so a
published or sealed map re-runs nothing live — see `docs/concept.md` §5-6.

## The `RoutingBackend` protocol

`agentic_maps/routing/base.py` defines a `typing.Protocol` (not an ABC, so a
plain test double or any future backend can satisfy it structurally without
inheriting anything). `rest/maps_api.py`'s `MapsApi.router` holds exactly one
implementation at a time; the REST layer and the frontend only ever talk to
this interface, never to `ValhallaRouter`/`OsrmRouter` directly:

```python
class RoutingBackend(Protocol):
    base_url: str

    async def route(self, start, end, *, route_id, from_location="",
                     to_location="", mode="car", via=None, steps=False,
                     avoid=None, alternates=0) -> MapRoute: ...

    async def matrix(self, points, *, mode="car",
                     sources=None, targets=None) -> list[list[float]]: ...

    async def optimized_route(self, stops, *, route_id, mode="car",
                              roundtrip=False, keep_endpoints=True,
                              from_location="", to_location="",
                              steps=False) -> OptimizedRoute: ...

    async def isochrone(self, center, *, mode="car", contours) -> IsochroneResult: ...

    async def supported_avoid(self) -> list[str]: ...
```

`matrix` grew optional `sources`/`targets` index lists (into `points`) for
asymmetric answers — the one-to-many reachability primitive; `None` keeps
the historical all-pairs NxN. `optimized_route` is the TSP primitive:
returns an `OptimizedRoute` (`order[k]` = index into the input stops of the
k-th visited stop, plus the full route driven in that order). Both are
solved by each backend's own native service — never a homegrown solver.

Two backends implement it today:

| Backend | File | Role | Alternates | Isochrones | Truck |
| --- | --- | --- | --- | --- | --- |
| **Valhalla** | `routing/valhalla.py` | Primary, default | Yes (2-point routes only) | Yes | Real `truck` costing model |
| **OSRM** | `routing/osrm.py` | Secondary/legacy | No (interface parity only — always ignored) | No | Falls back to `driving` |

## Canonical travel-mode vocabulary

The whole product — REST API, frontend, both backends — speaks exactly four
modes, defined once in `routing/base.py`:

```python
TravelMode = Literal["car", "truck", "walk", "bike"]
```

An unrecognised mode string falls back to `"car"` rather than erroring (see
`RouteRequest.mode` in `rest/maps_api.py` and both routers' `mode`
parameters). `MapRoute.mode` additionally allows a fifth literal, `"transit"`
— reserved for a later GTFS-based provider (`docs/concept.md` §7); no backend
routes it yet.

Each backend privately maps the canonical four onto its own profile/costing
name:

| Canonical mode | Valhalla costing | OSRM profile |
| --- | --- | --- |
| `car` | `auto` | `driving` |
| `truck` | `truck` | `driving` (no truck profile exists on the public OSRM demo; access restrictions still apply) |
| `walk` | `pedestrian` | `walking` |
| `bike` | `bicycle` | `cycling` |

## `ValhallaRouter` (`agentic_maps/routing/valhalla.py`)

The default backend (`AGENTIC_MAPS_ROUTING_BACKEND` unset or `valhalla`).
Talks to a self-hosted Valhalla server — there is no public Valhalla demo
suitable for embedding in a client the way OSRM has one, so
`AGENTIC_MAPS_VALHALLA_URL` must point at a real server for this backend to
do anything. Default: `http://localhost:8002` (Valhalla's own default port,
matching the Docker image in `deploy/docker-compose.yml`).

Every request/response shape in this file was checked against Valhalla's own
prose docs (`valhalla.github.io/valhalla/api/...`); where the prose was
silent — the `alternates` response structure, the matrix service's
concise-mode field names, the warning code for a rejected hard exclusion —
the module's own docstrings cite the exact serializer source file
(`valhalla/src/tyr/...`) the answer came from instead, and call out that it
is inference from source, not documentation.

### `POST /route` → `ValhallaRouter.route()`

Requests `POST {base_url}/route` with:

```json
{
  "locations": [{"lat": ..., "lon": ...}, "...via stops...", {"lat": ..., "lon": ...}],
  "costing": "auto",
  "units": "kilometers",
  "id": "<route_id>",
  "costing_options": {"auto": {"exclude_tolls": true}},
  "alternates": 2
}
```

- `costing_options` is only sent when `avoid` is non-empty (see "Avoid
  gating" below).
- `alternates` is only sent when there is **no** `via` stop — Valhalla does
  not support alternates on multipoint routes, so this backend only asks for
  them where they can actually be honoured rather than silently depending on
  an upstream limitation the caller cannot see.

Response: the primary route lives at the top-level `trip` key; each
alternate is a sibling `{"trip": {...}}` inside an `alternates` array — a
shape confirmed from `src/tyr/route_serializer_valhalla.cc` (not documented
in the prose reference). Geometry comes polyline6-encoded per leg
(`decode_polyline6()` — **six** digits of precision, 1e6, not Google's
original algorithm's five; using the wrong factor places every point off by
roughly two orders of magnitude). Maneuvers map onto `RouteStep` via a
40-entry lookup table (`_MANEUVER_TYPES`) mirrored from Valhalla's own
maneuver `type` enum in the turn-by-turn API reference; cases with no
equivalent in OSRM's `(type, modifier)` vocabulary (elevators, building
entries, transit) fall back to `("notification", "")`.

### `POST /matrix` → `ValhallaRouter.matrix()`

`POST {base_url}/sources_to_targets` with `"verbose": false`; reads
`sources_to_targets.durations` (row-major seconds, divided by 60 here). The
concise-mode shape is under-specified in the prose docs (which describe
verbose mode in detail); confirmed from `src/tyr/matrix_serializer.cc`,
whose `serialize_duration` writes raw seconds regardless of the request's
`units` — so `distances` (not read by this client) would need `units`
honoured separately before being usable.

### Asymmetric matrix — `sources`/`targets`

`/sources_to_targets` is natively asymmetric (that is its whole name): the
request carries SEPARATE `sources` and `targets` location arrays, and the
concise-mode `durations` come back row-major, sources × targets. The index
lists on `RoutingBackend.matrix()` simply select which of `points` go into
each array; all-pairs stays the both-arrays-full case. Raising the ceiling
on a self-hosted server: `service_limits.<costing>.max_matrix_locations` in
`valhalla.json` (the service refuses past it).

### `POST /optimized_route` → `ValhallaRouter.optimized_route()`

Doc-verified against `valhalla-docs/optimized/api-reference.md`:

- Request is route-shaped (`locations`, `costing`, `units`, `id`) against
  the `/optimized_route` action.
- Endpoints are ALWAYS pinned: the solver visits every intermediate
  location once, "always starting at the first location in the list and
  ending at the last location". `keep_endpoints=False` therefore raises
  `ValueError` on this backend instead of silently reordering less than
  promised (use OSRM for free endpoints).
- Order mapping: "Due to the reordering of the intermediate locations, an
  `original_index` is also part of the `locations` object within the
  response" — `trip.locations` comes back in VISITED order, so
  `[loc["original_index"] for loc in trip.locations]` IS the order.
- Costing: the docs list `auto`, `bicycle` and `pedestrian` for this
  service (multimodal explicitly unsupported, `truck` not named) — `truck`
  maps to `auto` here, called out in the client docstring.
- `roundtrip` has no native flag; it is emulated by appending the start as
  the final pinned location and dropping the duplicate from the returned
  order.

### `POST /isochrone` → `ValhallaRouter.isochrone()`

`POST {base_url}/isochrone` with `locations` (single center point),
`costing`, `polygons: true`, and one `contours` entry per requested ring
(`{"time": N}` or `{"distance": N}` — never both, mirroring Valhalla's own
one-metric-per-contour rule). The request shape is documented; the response
property names — `contour` (the ring's value), `metric` (`"time"` or
`"distance"`), `color` — are not, and come from
`src/tyr/isochrone_serializer.cc`. Isochrone coordinates are plain
`[lon, lat]` pairs (unlike route/matrix shapes, this service never
polyline-encodes). Interior rings (holes — an unreachable pocket like a lake
inside an otherwise reachable area) are dropped: `IsochroneRing` has no hole
concept, and a slightly-too-generous polygon around a lake is a smaller sin
than a keyed model nothing downstream can render correctly.

### Avoid gating — the honesty mechanism

`"toll"` / `"motorway"` / `"ferry"` map onto Valhalla's *hard exclusions*
(`exclude_tolls` / `exclude_highways` / `exclude_ferries` costing options).
Hard exclusions are silently downgraded to no-ops by the server unless its
config sets `service_limits.allow_hard_exclusions` — the request still
succeeds, but a warning with code `208` ("Hard exclusions are not allowed on
this server, ignoring hard excludes") is added to the response. This is not
in the prose API reference either; it comes from `src/exceptions.cc`.

`ValhallaRouter.supported_avoid()` therefore never assumes support — it
sends a real two-point probe route with **all three** hard-exclusion flags
set and inspects the `warnings` array: code `208` present means the server
supports none of them (Valhalla applies the flag atomically per request, so
one warning covers all three); no warning means all three are honoured.
Probed once per process and cached (`_avoid_cache`), the same contract
`OsrmRouter.supported_avoid()` follows — a route that silently ignores
"avoid motorways" is worse than one that admits it cannot.

## `OsrmRouter` (`agentic_maps/routing/osrm.py`)

Secondary/legacy backend (`AGENTIC_MAPS_ROUTING_BACKEND=osrm`) for sites that
already run an OSRM container. Dev default is the **public OSRM demo server**
(`https://router.project-osrm.org`) — light interactive use only, per its own
usage policy; `AGENTIC_MAPS_OSRM_URL` should point production/batch use at a
self-hosted OSRM container fed with a Geofabrik OSM extract (ODbL).

Known, deliberate limitations (all surfaced through `/routing/capabilities`
rather than discovered by the caller catching an error):

- **No isochrones.** `OsrmRouter.isochrone()` raises `NotImplementedError`
  unconditionally — OSRM has no isochrone service.
- **No alternates through this client.** `alternates` is accepted on
  `route()` for interface parity with `ValhallaRouter` but never honoured:
  only `routes[0]` from the OSRM response is ever parsed.
- **No truck profile.** `truck` maps onto `driving` — "at least respects car
  access restrictions rather than nothing," per the module's own comment. A
  self-hosted OSRM built with a truck profile extension would need its own
  mapping; not attempted, since the public demo (the dev default) only ever
  runs `driving`/`walking`/`cycling`.
- **Walk-speed fixup for the public demo.** The public OSRM demo runs *only*
  the car profile — `/walking/` quietly answers with driving times ("2
  minutes for a kilometre"). If the implied speed on a `walk` request exceeds
  6 km/h, `OsrmRouter.route()` recomputes the duration at 4.7 km/h and scales
  every leg's duration by the same ratio; a self-hosted server with a real
  foot profile passes through untouched.
- **Multi-stop is one request.** `via` stops are appended to the same
  `route/v1/{profile}/{coords}` call OSRM already supports natively — one
  geometry, one honest total, not several routes stitched together (which
  would double-count the joins).

### `GET /trip/v1/{profile}/…` → `OsrmRouter.optimized_route()`

Doc-verified against the OSRM API reference (Trip service):

- Parameters: `roundtrip` (default `true`) closes the loop back to the
  start; `source=first` / `destination=last` pin the endpoints (`any`, the
  default, lets the solver pick — which is what `keep_endpoints=False`
  sends). A pinned non-roundtrip TSP is therefore
  `roundtrip=false&source=first&destination=last`; a roundtrip keeps only
  `source=first` (a loop has no distinct end).
- Order mapping: the response `waypoints` array represents "all waypoints
  in input order", each carrying `waypoint_index` — "Index of the point in
  the trip". The visiting order is that mapping inverted:
  `order[waypoint_index] = input_index`.
- `trips[0]` is a regular Route object and goes through the same
  `_map_route` the route service uses (legs, steps, walk-speed fixup and
  all).
- Two pinned stops short-circuit to the plain route service — nothing to
  optimize, same response shape.

### `GET /table/v1/…` — asymmetric matrix

`sources`/`destinations` are "String of `{index};{index}[;{index} ...]` or
`all` (default)" — semicolon-separated indices into the coordinate list —
and "`durations[i][j]` gives the travel time from the i-th source to the
j-th destination". The public demo caps a table request around 100
coordinates; a self-hosted `osrm-routed` raises that with
`--max-table-size`.

### Avoid gating

`OsrmRouter.supported_avoid()` probes each of `toll`/`motorway`/`ferry`
individually against a fixed two-point route with `&exclude=<name>` and
keeps the ones that come back `"code": "Ok"` — support depends on how the
server was *compiled* (which exclude classes it was built with), not on the
request, so guessing wrong means quietly handing someone a motorway route
they asked to avoid. The public demo answers "Exclude flag combination is
not supported" for all three; production use needs a self-hosted OSRM built
with the matching profile exclude classes.

## Backend selection — environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENTIC_MAPS_ROUTING_BACKEND` | unset (→ Valhalla) | `osrm` selects `OsrmRouter`; anything else (including unset) selects `ValhallaRouter`. Read once, in `rest/maps_api.py`'s `_default_router()`. |
| `AGENTIC_MAPS_VALHALLA_URL` | `http://localhost:8002` | Base URL of a self-hosted Valhalla server. Read by `ValhallaRouter.__init__`. |
| `AGENTIC_MAPS_OSRM_URL` | `https://router.project-osrm.org` | Base URL of an OSRM-compatible server (public demo by default). Read by `OsrmRouter.__init__`. |

A host embedding `MapsApi` can also skip the environment entirely and pass a
constructed router straight in: `MapsApi(bundles_dir, router=ValhallaRouter("http://valhalla:8002"))`.

## REST endpoints (`agentic_maps/rest/maps_api.py`, mounted under `/api/v1/maps`)

All four routing endpoints call `self._require_network_allowed("routing")`
first, so each returns **403** in `offline` runtime mode; `mixed` and
`online` both allow them (routing is a live, per-request action, not bulk
data provisioning — see `docs/concept.md` §5).

### `POST /route`

Request body (`RouteRequest`):

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `route_id` | `str` | — | Caller-chosen id, echoed onto the response and used to name alternates (`"{route_id}-alt0"`, …). |
| `start`, `end` | `LatLon` | — | `{lat, lon}`. |
| `mode` | `str` | `"car"` | Canonical vocabulary; unrecognised values fall back to `"car"`. |
| `from_location`, `to_location` | `str` | `""` | Display names, echoed back untouched. |
| `via` | `list[LatLon]` | `[]` | Intermediate stops, in order — one backend request, one geometry. |
| `steps` | `bool` | `False` | Turn-by-turn instructions roughly double the response; off unless asked for. |
| `avoid` | `list[str]` | `[]` | Any of `"toll"` / `"motorway"` / `"ferry"` — only actually honoured where `/routing/capabilities` says so. |
| `alternates` | `int` | `0` | Extra routes to request (Valhalla, two-point routes only; always 0 alternates back from OSRM). |

Response: a `MapRoute` —

```python
class MapRoute(BaseModel):
    id: str
    from_location: str
    to_location: str
    mode: Literal["car", "truck", "walk", "bike", "transit"] = "car"
    duration_min: float
    distance_km: float
    geometry: list[LatLon]
    legs: list[RouteLeg] = []          # per stop-to-stop section
    steps: list[RouteStep] = []        # only populated when steps=True
    stops: list[LatLon] = []           # the named stops, in order
    color: str = "#2e6be6"
    animate_ms: int | None = None
    alternates: list["MapRoute"] = []  # each a full MapRoute; own `alternates` stays empty
```

`RouteStep` (OSRM's own maneuver vocabulary, which Valhalla's richer maneuver
set is normalized down onto):

```python
class RouteStep(BaseModel):
    type: str            # "turn", "roundabout", "exit roundabout", "merge", "fork", "arrive", "depart", …
    modifier: str = ""   # "left", "sharp right", "straight", … — absent on depart/arrive
    exit: int | None = None   # roundabout exit number
    name: str = ""
    distance_m: float = 0.0
    duration_s: float = 0.0
    location: LatLon | None = None
```

502 on any `httpx.HTTPError`/`ValueError` from the backend (upstream
unreachable or an empty/malformed response).

### `POST /isochrone`

Request body (`IsochroneRequest`):

```python
class IsochroneContourSpec(BaseModel):
    time_min: float | None = None
    distance_km: float | None = None      # exactly one of the two is set

class IsochroneRequest(BaseModel):
    center: LatLon
    mode: str = "car"
    contours: list[IsochroneContourSpec]
```

Response: **raw GeoJSON** (`Content-Type: application/geo+json`), not a
wrapped pydantic model — MapLibre wants a source it can add directly
(`map.addSource(id, {type: 'geojson', data: <this>})`). One `Polygon`
feature per ring, in outermost-to-innermost order, `properties` carrying
`contour` (the minutes/km value), `metric` (`"time"`/`"distance"`) and
`color` (hex) — the typed intermediate is `IsochroneResult`/`IsochroneRing`
in `agentic_maps/models/`.

**501** on a backend that cannot do this (OSRM — `NotImplementedError` from
`OsrmRouter.isochrone()`); **502** on any other backend failure.
`/routing/capabilities` reports `isochrone: false` for OSRM up front so the
frontend gates the feature before ever calling here and hitting the 501.

### `POST /matrix`

Request body (`MatrixRequest`): `{"points": [LatLon, ...], "mode": "car",
"sources": [0], "targets": [1, 2, ...]}` — `sources`/`targets` are OPTIONAL
index lists into `points` for an asymmetric answer (rows × columns); out-of-
range indices are a 400. Omitting both keeps the historical all-pairs shape.

Response (`MatrixResult`): `{"durations_min": [[float, ...], ...]}` —
`durations_min[i][j]` is the travel time in minutes from the i-th source to
the j-th target (with the full point list on both axes when no index lists
were given). 502 on backend failure.

### `POST /route/optimize`

TSP stop ordering + the route driven in that order. Request body
(`OptimizeRouteRequest`):

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `stops` | `list[LatLon]` | — | 2+ stops; two pinned stops short-circuit to a plain route. |
| `mode` | `str` | `"car"` | Canonical vocabulary. Valhalla optimizes `truck` with `auto` costing (its optimized-route docs list auto/bicycle/pedestrian only). |
| `roundtrip` | `bool` | `false` | Return to the start instead of ending at `stops[-1]`. |
| `keep_endpoints` | `bool` | `true` | Pin first/last. `false` needs OSRM — Valhalla always pins and the request is refused (502 with the reason), never silently degraded. |
| `steps` | `bool` | `false` | Turn-by-turn on the optimized route. |
| `route_id`, `from_location`, `to_location` | `str` | | Same echo contract as `/route`. |

Response (`OptimizedRoute`): `{"order": [0, 2, 1, 3], "route": MapRoute}` —
`order[k]` is the index into the INPUT stops of the k-th visited stop; the
route's own `stops` are already permuted to match and `via_places` are
annotated exactly like `/route`. 403 offline, 502 on backend failure.

### `GET /routing/capabilities`

No request body. Response (`RoutingCapabilities`):

```python
class RoutingCapabilities(BaseModel):
    backend: str                          # the active router's base_url
    avoid: list[str]                      # from router.supported_avoid() — probed live
    departure_time_affects_route: bool    # always False; neither backend carries live traffic
    turn_by_turn: bool                    # always True
    multi_stop: bool                      # always True
    alternates: bool                      # router.supports_alternates (True: both — 2-point trips only)
    isochrone: bool                       # router.supports_isochrone (True: Valhalla, False: OSRM)
```

This is the single source of truth the frontend consults before offering
"avoid motorways", alternates or isochrones in the UI — the same
honesty-gating principle applies to all three: never let the user pick an
option that will be silently ignored or fail with a raw error.

## Testing

`tests/test_routing_backends.py` exercises both routers entirely against
`httpx.MockTransport` fixtures built from real Valhalla/OSRM response shapes
(including `test_decode_polyline6_matches_valhalla_example`, which checks
the polyline decoder against Valhalla's own worked example from its decoding
docs) — no network call, no real Valhalla/OSRM server involved. See
"Roadmap" in the top-level `README.md` for what routing behavior has **not**
been exercised against a live backend in this environment.
