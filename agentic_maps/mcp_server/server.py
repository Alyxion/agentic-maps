"""The MCP toolset: every product capability as one crisply-described tool.

Only imported behind feature detection (see package docstring), so the `mcp`
and `fastapi` imports at module level are themselves the optional-extra
boundary — the rest of agentic-maps never imports this module.

Every tool description states its caps, units and runtime-mode dependency in
the text an agent actually reads; the numbers live in the named constants
below so the description and the enforcement can never drift apart
(descriptions are f-strings over the same constants).
"""

import asyncio
import math
import os
import webbrowser
from typing import Any, Literal

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..export.map_page import MAP_PAGE_MAX_BYTES, MAP_PAGE_MAX_STEPS
from ..models.bbox_deg import BBoxDeg
from ..models.camera_pose import CameraPose
from ..models.geocode_address import GeocodeAddress
from ..models.lat_lon import LatLon
from ..models.map_highlight import MapHighlight
from ..models.map_location import MapLocation
from ..models.map_pin import MapPin
from ..models.map_route import MapRoute
from ..models.map_spec import MapSpec
from ..models.provision_request import AERIAL_ZOOM_CAPS
from ..models.reachability_grid import ReachabilityGrid
from ..models.route_stop import RouteStop
from ..models.runtime_mode import RuntimeMode
from ..models.trip_op import TripOp
from ..models.trip_state import TripState
from ..provision.estimates import preset_size_table
from ..rest.maps_api import (
    FEATURE_DEFAULT_LIMIT,
    FEATURE_MAX_LIMIT,
    FEATURE_TILES_CAP,
    SESSION_PAYLOAD_MAX_BYTES,
    SESSION_TTL_S,
    IsochroneContourSpec,
    MapsApi,
)
from .trips import TRIP_STORE_MAX_BYTES, TRIP_STORE_MAX_TRIPS, TRIP_TTL_S, TripStore

# -- caps, all in one place --------------------------------------------------

# Nominatim answers get re-ranked server-side; more than 10 candidates is
# noise for an agent picking one.
GEOCODE_LIMIT_MAX = 10
# Matches the REST/backend contract: one request, one geometry, any number of
# stops up to what the routing backends accept (public OSRM demo caps its
# coordinate list around 100 — same figure the matrix cap uses).
ROUTE_STOPS_MIN = 2
ROUTE_STOPS_MAX = 100
# Valhalla's own ceiling; more alternates than 3 are never distinct routes.
ROUTE_ALTERNATES_MAX = 3
# NxN grows quadratically; 100 points = 10 000 pairs is the public demo's
# table-service cap and a sane fleet-primitive bound.
MATRIX_POINTS_MAX = 100
# More than 4 rings is unreadable on a map and Valhalla's practical limit.
ISOCHRONE_CONTOURS_MAX = 4
# Render output rides base64 inside the MCP response; 1600x1200 at 3x is a
# ~5 MB PNG worst case — big enough for print, small enough for a message.
# (The REST endpoint itself allows 4096px for direct HTTP consumers.)
RENDER_WIDTH_MAX = 1600
RENDER_HEIGHT_MAX = 1200
# Offline city-index autocomplete cap — the index answers instantly, but an
# agent never needs more than a screenful of candidates.
PLACE_SEARCH_LIMIT_MAX = 20
# Street survey inherits the REST endpoint's own budget: 16 tiles.
STREET_SURVEY_TILES_CAP = 16
# Address enrichment reverse-geocodes each un-enriched stop once; against the
# public Nominatim instance that must stay around 1 req/s, so calls within one
# pass are spaced by this much. Tests (and self-hosted Nominatim deployments)
# may zero it.
ENRICH_SPACING_S = 1.0
# One-to-many reachability. The DEFAULT per-request budget is the public-demo
# reality (~100 coordinates total in one table request, the same figure
# MATRIX_POINTS_MAX documents) — origin + 99 targets, no chunking, because
# chunking against shared public infrastructure would just be a polite-looking
# hammer. A SELF-HOSTED backend raises its own service cap (OSRM:
# `--max-table-size`, Valhalla: `service_limits.<costing>.max_matrix_locations`
# in valhalla.json) and then declares the raised per-request budget via this
# env var, which also unlocks chunking up to the hard ceiling below.
MATRIX_MAX_ENV = "AGENTIC_MAPS_MATRIX_MAX"
REACHABILITY_DEMO_BUDGET = 100
# Even self-hosted, one tool call stays bounded: thousands-scale lattices are
# a batch job, not a chat answer.
REACHABILITY_TARGETS_HARD_MAX = 5000

_MODE_NETWORK = "Needs runtime mode 'online' or 'mixed'; refused in 'offline'."
_MODE_PROVISIONING = (
    "PROVISIONING: refused in BOTH 'offline' and 'mixed' runtime modes — only "
    "'online' allows minting new local data."
)

_INSTRUCTIONS = """\
agentic-maps: an offline-capable, legally-licensed mapping platform.
Georeferencing (geocode/reverse_geocode/search_places), routing
(route/route_matrix/isochrone), trip optimization (optimize_trip),
one-to-many reachability (reachability), live trip planning
(create_trip/update_trip), map-content extraction
(extract_features/street_survey), professional display
(open_map_view/get_map_page_html/render_map), offline-bundle provisioning
(plan/harvest/package), and whole-region offline provisioning
(provision_offline_region: size estimate first, confirm_size to download).

Showing results: do NOT rebuild maps from raw geometry with third-party tile
servers — that breaks tile-usage policies and drops attribution. Use the
display trio instead: `route`/`create_trip` (data) → `open_map_view` (the
full map application in the user's own browser) → `get_map_page_html` (a
self-contained, attributed HTML document for your own display surface).

Licensing is a hard product constraint: whenever rendered or extracted map
output is redistributed, the credit line from `map_attribution` MUST
accompany it (open_map_view and get_map_page_html carry it built-in).
Runtime modes gate the tools: 'online' allows everything, 'mixed' allows
live per-request lookups but no bulk provisioning, 'offline' allows only
what is already bundled/cached locally.
"""


def _grid_targets(grid: ReachabilityGrid) -> list[LatLon]:
    """The reachability lattice: cell-centred points every `step_km`,
    row-major west→east / south→north. Refused (with the real count and a
    workable step suggestion) when it would exceed the caller's own `cap` —
    never silently thinned, a thinned grid would quietly answer a different
    question than the one asked."""
    dlat = grid.step_km / 111.32
    mid_lat = (grid.bbox.south + grid.bbox.north) / 2.0
    dlon = grid.step_km / (111.32 * max(math.cos(math.radians(mid_lat)), 0.01))
    rows = max(1, int((grid.bbox.north - grid.bbox.south) / dlat))
    cols = max(1, int((grid.bbox.east - grid.bbox.west) / dlon))
    if rows * cols > grid.cap:
        needed = math.sqrt((rows * cols) / grid.cap) * grid.step_km
        raise ToolError(
            f"grid would be {rows * cols} targets ({cols}x{rows}) — past your "
            f"cap of {grid.cap}. Coarsen step_km to ~{needed:.1f} or shrink "
            "the bbox.")
    return [
        LatLon(
            lat=grid.bbox.south + (row + 0.5) * (grid.bbox.north - grid.bbox.south) / rows,
            lon=grid.bbox.west + (col + 0.5) * (grid.bbox.east - grid.bbox.west) / cols,
        )
        for row in range(rows)
        for col in range(cols)
    ]


def build_internal_app(api: MapsApi) -> FastAPI:
    """The in-process REST app every tool calls — same MapsApi instance the
    host serves, minus static/web chrome (tools never fetch pages)."""
    app = FastAPI(title="agentic-maps mcp internal")
    api.mount(app)
    return app


def build_mcp_server(api: MapsApi) -> MCPServer:
    app = build_internal_app(api)
    # In-process ASGI: no sockets, no self-HTTP. The long timeout exists for
    # exactly one caller — harvest_offline_bundle, which legitimately runs
    # minutes; everything else answers in milliseconds to seconds.
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://agentic-maps.internal/api/v1/maps",
        timeout=httpx.Timeout(3600.0),
    )

    async def _call(method: str, path: str, *, params: dict | None = None,
                    body: Any | None = None) -> Any:
        response = await client.request(method, path, params=params, json=body)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ToolError(f"{detail}")
        return response.json()

    server = MCPServer(
        name="agentic-maps",
        title="Agentic Maps",
        instructions=_INSTRUCTIONS,
        version="0.1.0",
    )

    # Live trips (see trips.py): state of THIS process — per MCP session over
    # stdio, shared across clients on the HTTP devserver instance.
    trips = TripStore()
    # Stable per-trip browser sessions: ONE token per trip (re-minted only
    # after the session expired), bound to the live trip via
    # MapsApi.bind_session_source so the OPEN tab serves/polls current state.
    trip_sessions: dict[str, str] = {}
    # Browser-opening discipline: trips a tab was already opened for. Later
    # open_map_view(trip_id=...) calls default to NOT spawning another tab —
    # the open one updates itself. In-process on purpose, like the trips.
    trip_opened: set[str] = set()

    # -- shared helpers ----------------------------------------------------

    async def _enrich_stops(stops: list[RouteStop]) -> None:
        """Fill `RouteStop.address` once per stop via reverse geocoding.

        Cached on the stop object — a trip carries the enrichment forever,
        so repeated page/view generations never re-hit the geocoder. In
        offline mode (or with the geocoder down) the first failure aborts
        the pass and labels/coordinates stand honestly unenriched.
        """
        first = True
        for stop in stops:
            if stop.address is not None:
                continue
            if not first:
                await asyncio.sleep(ENRICH_SPACING_S)
            first = False
            try:
                result = await _call("POST", "/reverse-geocode",
                                     body={"lat": stop.lat, "lon": stop.lon, "lang": "de"})
            except ToolError:
                return
            if result and result.get("address"):
                stop.address = GeocodeAddress.model_validate(result["address"])

    async def _compute_route(
        stops: list[RouteStop], mode: str, avoid: list[str],
        alternates: int, steps: bool, route_id: str = "mcp-route",
    ) -> dict[str, Any]:
        return await _call("POST", "/route", body={
            "route_id": route_id,
            "start": stops[0].latlon().model_dump(),
            "end": stops[-1].latlon().model_dump(),
            "via": [stop.latlon().model_dump() for stop in stops[1:-1]],
            "mode": mode,
            "from_location": stops[0].display(),
            "to_location": stops[-1].display(),
            "steps": steps,
            "avoid": avoid,
            "alternates": alternates,
        })

    def _fit_camera(lats: list[float], lons: list[float]) -> CameraPose:
        """A camera that frames everything — the session page refines with a
        client-side fitBounds, but the mount needs a sane starting pose."""
        south, north, west, east = min(lats), max(lats), min(lons), max(lons)
        span = max((east - west) * 1.3, (north - south) * 2.1, 0.005)
        zoom = max(3.0, min(16.0, math.log2(360.0 / span)))
        return CameraPose(
            center=LatLon(lat=(south + north) / 2, lon=(west + east) / 2),
            zoom=round(zoom, 1),
        )

    def _assemble_view_spec(
        routes: list[MapRoute] | None,
        markers: list[RouteStop] | None,
        highlights: list[MapHighlight] | None,
        title: str,
        source_id: str,
    ) -> MapSpec:
        """The sync tail of _build_view_spec: pure spec assembly from
        already-enriched inputs. Split out so the trip-bound session source
        below can rebuild the trip's CURRENT spec on every `GET /sessions/
        {token}` — trip stops carry their cached addresses, so no async
        enrichment pass is needed there."""
        routes = routes or []
        markers = list(markers or [])
        locations = [
            MapLocation(
                id=f"stop-{index}",
                name=stop.display(),
                camera=CameraPose(center=stop.latlon(), zoom=13.0),
                pin=MapPin(label=stop.label or stop.display(), diameter_px=90),
                highlights=list(highlights or []) if index == 0 else [],
            )
            for index, stop in enumerate(markers)
        ]
        if highlights and not locations:
            locations = [MapLocation(
                id="focus", name=title or "Karte",
                camera=CameraPose(center=highlights[0].at, zoom=13.0),
                highlights=list(highlights),
            )]
        if not routes and not locations:
            raise ToolError("nothing to show: pass routes, markers/stops, "
                            "highlights, a raw spec, or a trip_id")
        lats = [p.lat for r in routes for p in r.geometry] + [l.camera.center.lat for l in locations]
        lons = [p.lon for r in routes for p in r.geometry] + [l.camera.center.lon for l in locations]
        return MapSpec(
            id="mcp-view",
            title=title,
            source_id=source_id,
            overview=_fit_camera(lats, lons),
            locations=locations,
            routes=routes,
            interactive=True,
        )

    async def _build_view_spec(
        routes: list[MapRoute] | None,
        markers: list[RouteStop] | None,
        highlights: list[MapHighlight] | None,
        spec: MapSpec | None,
        trip_id: str | None,
        title: str,
        source_id: str,
    ) -> MapSpec:
        """One spec from whichever shape the caller has (raw spec wins;
        a trip reference resolves to its CURRENT state)."""
        if spec is not None:
            return spec
        if trip_id is not None:
            try:
                trip = trips.get(trip_id)
            except KeyError as error:
                raise ToolError(str(error))
            routes = [trip.route]
            markers = trip.stops
            title = title or f"{trip.stops[0].display()} → {trip.stops[-1].display()}"
        if markers:
            await _enrich_stops(list(markers))
        return _assemble_view_spec(routes, markers, highlights, title, source_id)

    def _trip_session_source(trip_id: str, source_id: str):
        """(spec_fn, revision_fn) for MapsApi.bind_session_source: the live
        trip's CURRENT state, rebuilt per read — how an already-open tab
        shows every update_trip result without a reload. Both answer None
        once the trip expired/was evicted; the session then serves its last
        frozen copy instead of breaking the tab."""
        def spec_fn() -> MapSpec | None:
            trip = trips.peek(trip_id)
            if trip is None:
                return None
            return _assemble_view_spec(
                [trip.route], list(trip.stops), None,
                f"{trip.stops[0].display()} → {trip.stops[-1].display()}",
                source_id)

        def revision_fn() -> int | None:
            trip = trips.peek(trip_id)
            return None if trip is None else trip.revision

        return spec_fn, revision_fn

    async def _create_session(spec: MapSpec) -> dict[str, Any]:
        return await _call("POST", "/sessions", body=spec.model_dump())

    # -- georeferencing ------------------------------------------------------

    @server.tool(description=(
        "Forward georeferencing: free-text place/address query -> candidate "
        "locations, each with lat/lon and a structured address (road, "
        "house_number, postcode, locality, state, country, country_code). "
        "Bias the ranking with `near` (results sorted nearest-first) and/or "
        "`viewbox` (a WGS84 bbox that is preferred, not enforced). "
        f"`limit` <= {GEOCODE_LIMIT_MAX}. Backed by OSM/Nominatim "
        "server-side. Default lang 'de' (German market). " + _MODE_NETWORK))
    async def geocode(
        query: str,
        lang: str = "de",
        near: LatLon | None = None,
        viewbox: BBoxDeg | None = None,
        limit: int = 6,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, GEOCODE_LIMIT_MAX))
        body: dict[str, Any] = {"q": query, "lang": lang, "limit": limit}
        if near is not None:
            body["near"] = near.model_dump()
        if viewbox is not None:
            body["viewbox"] = viewbox.model_dump()
        return {"results": await _call("POST", "/geocode", body=body)}

    @server.tool(description=(
        "Backward georeferencing: coordinate -> nearest named place with a "
        "structured address incl. postcode. Returns {\"result\": null} for a "
        "genuinely unnamed spot (open field) — that is an answer, not an "
        "error. Default lang 'de'. " + _MODE_NETWORK))
    async def reverse_geocode(lat: float, lon: float, lang: str = "de") -> dict[str, Any]:
        result = await _call("POST", "/reverse-geocode",
                             body={"lat": lat, "lon": lon, "lang": lang})
        return {"result": result}

    @server.tool(description=(
        "Place/POI search combining two sources: the OFFLINE populated-places "
        "index (instant prefix autocomplete, ranked by population and "
        "proximity — works in every runtime mode) and the live geocoder "
        "(bounded to `bbox` when given; needs mode online/mixed, silently "
        "skipped offline with a note). "
        f"`limit` <= {PLACE_SEARCH_LIMIT_MAX} per source. Default lang 'de'."))
    async def search_places(
        query: str,
        bbox: BBoxDeg | None = None,
        near: LatLon | None = None,
        limit: int = 6,
        lang: str = "de",
    ) -> dict[str, Any]:
        limit = max(1, min(limit, PLACE_SEARCH_LIMIT_MAX))
        params: dict[str, Any] = {"q": query, "limit": limit}
        if near is not None:
            params["near_lat"], params["near_lon"] = near.lat, near.lon
        cities = await _call("GET", "/geo/cities/search", params=params)
        note = ""
        geocoder_hits: list = []
        body: dict[str, Any] = {"q": query, "lang": lang, "limit": limit}
        if near is not None:
            body["near"] = near.model_dump()
        if bbox is not None:
            body["viewbox"] = bbox.model_dump()
        try:
            geocoder_hits = await _call("POST", "/geocode", body=body)
        except ToolError as error:
            note = f"geocoder skipped: {error}"
        return {"city_index": cities, "geocoder": geocoder_hits,
                **({"note": note} if note else {})}

    # -- routing ---------------------------------------------------------

    @server.tool(description=(
        f"One-shot route along real streets through {ROUTE_STOPS_MIN}.."
        f"{ROUTE_STOPS_MAX} stops in the given order (one request, one "
        "geometry, one honest total). Returns everything an external "
        "renderer needs: duration_min, distance_km, per-leg figures, "
        "`geometry` as an ordered list of {lat, lon} WGS84 points ready to "
        "draw, `via_places` (major cities the route passes, PRIORITY-ordered "
        "— take the first 1-2 for a brief 'via Pforzheim' label; `along_km` "
        "lets you re-sort geographically), and `stop_details` with the "
        "structured address (road, postcode, locality) per stop. "
        "`steps=true` adds turn-by-turn instructions incl. road `ref` "
        "('A 8') and lane layout per maneuver. "
        f"`alternates` (0..{ROUTE_ALTERNATES_MAX}) requests additional "
        "distinct routes — honoured ONLY on plain 2-stop trips (multi-stop "
        "always returns 0 alternates), attached under `alternates` each with "
        "full geometry, steps and via_places of its own. `avoid` "
        "(toll/motorway/ferry) is honoured only where "
        "routing_capabilities() lists it — otherwise the backend rejects the "
        "request rather than silently ignoring the wish. To show results "
        "professionally in the user's browser, follow with open_map_view "
        "(or get_map_page_html for an embeddable page — both carry the "
        "required map attribution built-in; redistributing raw geometry on "
        "third-party tiles does not). For iterative planning (inserting/"
        "reordering stops), prefer create_trip, which keeps the route alive "
        "server-side. " + _MODE_NETWORK))
    async def route(
        stops: list[RouteStop],
        mode: Literal["car", "truck", "walk", "bike"] = "car",
        alternates: int = 0,
        avoid: list[Literal["toll", "motorway", "ferry"]] | None = None,
        steps: bool = False,
    ) -> dict[str, Any]:
        if not ROUTE_STOPS_MIN <= len(stops) <= ROUTE_STOPS_MAX:
            raise ToolError(
                f"route needs {ROUTE_STOPS_MIN}..{ROUTE_STOPS_MAX} stops, got {len(stops)}")
        alternates = max(0, min(alternates, ROUTE_ALTERNATES_MAX))
        await _enrich_stops(stops)
        result = await _compute_route(stops, mode, avoid or [], alternates, steps)
        # The structured addresses ride alongside the route — labels alone
        # ("Frankfurt") hide the detail (street, ZIP) every display needs.
        result["stop_details"] = [stop.model_dump() for stop in stops]
        return result

    @server.tool(description=(
        "All-pairs travel-time matrix: durations_min[i][j] = minutes from "
        f"points[i] to points[j]. 2..{MATRIX_POINTS_MAX} points (hard cap — "
        "the reachability/fleet primitive, not a routing loop). "
        + _MODE_NETWORK))
    async def route_matrix(
        points: list[LatLon],
        mode: Literal["car", "truck", "walk", "bike"] = "car",
    ) -> dict[str, Any]:
        if not 2 <= len(points) <= MATRIX_POINTS_MAX:
            raise ToolError(f"matrix needs 2..{MATRIX_POINTS_MAX} points, got {len(points)}")
        return await _call("POST", "/matrix", body={
            "points": [p.model_dump() for p in points], "mode": mode})

    @server.tool(description=(
        "Reachable-area contours around one point as GeoJSON polygons. Each "
        "contour is EITHER {\"time_min\": N} OR {\"distance_km\": N}, max "
        f"{ISOCHRONE_CONTOURS_MAX} contours. REQUIRES the Valhalla routing "
        "backend: on OSRM deployments this fails with a clear "
        "backend-limitation error — check routing_capabilities().isochrone "
        "first. " + _MODE_NETWORK))
    async def isochrone(
        center: LatLon,
        contours: list[IsochroneContourSpec],
        mode: Literal["car", "truck", "walk", "bike"] = "car",
    ) -> dict[str, Any]:
        if not 1 <= len(contours) <= ISOCHRONE_CONTOURS_MAX:
            raise ToolError(
                f"isochrone needs 1..{ISOCHRONE_CONTOURS_MAX} contours, got {len(contours)}")
        return await _call("POST", "/isochrone", body={
            "center": center.model_dump(), "mode": mode,
            "contours": [c.model_dump(exclude_none=True) for c in contours]})

    @server.tool(description=(
        "TSP stop ordering: visit every stop exactly once in the cheapest "
        "order, using the routing backend's NATIVE solver (OSRM `trip` "
        "service / Valhalla `optimized_route`). Pass EITHER `stops` "
        f"({ROUTE_STOPS_MIN}..{ROUTE_STOPS_MAX}, same public-demo ceiling as "
        "route) OR `trip_id` — a live trip gets its stop order rewritten and "
        "its route recomputed through the normal trip pipeline (revision "
        "bumped), so open_map_view/get_map_page_html(trip_id=...) show the "
        "optimized order afterwards. Returns `order` (order[k] = index into "
        "the ORIGINAL stops of the k-th visited stop) plus the full "
        "recomputed route. `keep_endpoints=true` (default) pins first/last "
        "as start/end; `roundtrip=true` returns to the start. "
        "keep_endpoints=false needs the OSRM backend (Valhalla always pins "
        "endpoints — refused honestly, never silently ignored); on Valhalla "
        "`truck` optimizes with car costing (its optimized_route documents "
        "auto/bicycle/pedestrian only). " + _MODE_NETWORK))
    async def optimize_trip(
        trip_id: str | None = None,
        stops: list[RouteStop] | None = None,
        mode: Literal["car", "truck", "walk", "bike"] = "car",
        roundtrip: bool = False,
        keep_endpoints: bool = True,
    ) -> dict[str, Any]:
        if (trip_id is None) == (stops is None):
            raise ToolError("pass exactly one of `trip_id` or `stops`")
        if trip_id is not None:
            try:
                trip = trips.get(trip_id)
            except KeyError as error:
                raise ToolError(str(error))
            stop_list, opt_mode = list(trip.stops), trip.mode
        else:
            stop_list, opt_mode = list(stops), mode
        if not ROUTE_STOPS_MIN <= len(stop_list) <= ROUTE_STOPS_MAX:
            raise ToolError(
                f"optimize needs {ROUTE_STOPS_MIN}..{ROUTE_STOPS_MAX} stops, "
                f"got {len(stop_list)}")
        result = await _call("POST", "/route/optimize", body={
            "route_id": "optimized",
            "stops": [stop.latlon().model_dump() for stop in stop_list],
            "mode": opt_mode,
            "roundtrip": roundtrip,
            "keep_endpoints": keep_endpoints,
            "from_location": stop_list[0].display(),
            "to_location": stop_list[-1].display(),
        })
        order = result["order"]
        ordered_stops = [stop_list[i] for i in order]
        if trip_id is not None:
            # Route the live trip through its NORMAL recompute (enrichment,
            # steps, alternates) so every trip surface reflects the new order.
            trip.stops = ordered_stops
            await _enrich_stops(trip.stops)
            recomputed = await _compute_route(
                trip.stops, trip.mode, trip.avoid, trip.alternates,
                steps=True, route_id="trip")
            trip.route = MapRoute.model_validate(recomputed)
            trip.revision += 1
            trips.put(trip)
            return {"order": order, "improved": order != sorted(order),
                    "trip": trip.model_dump()}
        return {
            "order": order,
            "improved": order != sorted(order),
            "route": result["route"],
            "stop_details": [stop.model_dump() for stop in ordered_stops],
        }

    def _matrix_budget() -> tuple[int, bool]:
        """(coords per request, chunking allowed). Demo budget unless the
        self-hosted env knob raises it — see MATRIX_MAX_ENV above."""
        raw = os.environ.get(MATRIX_MAX_ENV, "").strip()
        if raw.isdigit() and int(raw) > 2:
            return int(raw), True
        return REACHABILITY_DEMO_BUDGET, False

    @server.tool(description=(
        "One-to-many reachability: minutes from `origin` to many targets in "
        "one call (asymmetric matrix — never pays NxN). Targets come EITHER "
        "as an explicit `targets` list OR as `grid` {bbox, step_km, cap}: a "
        "lattice generated server-side, refused (with the real count) when "
        "it would exceed `cap`. Returns per-target {lat, lon, minutes}; "
        "minutes 0.0 can mean unreachable (backend convention). LIMITS: "
        f"against the public demo backend ~{REACHABILITY_DEMO_BUDGET} "
        "coordinates total per request (origin + 99 targets) and no "
        "chunking. Self-hosted backends can be raised — OSRM "
        "`--max-table-size`, Valhalla `service_limits.<costing>."
        "max_matrix_locations` — then set AGENTIC_MAPS_MATRIX_MAX to the "
        "raised per-request budget, which also unlocks chunked grids up to "
        f"{REACHABILITY_TARGETS_HARD_MAX} targets. Thousands-scale lattices "
        "need self-hosting; this tool says so instead of hammering the "
        "demo. " + _MODE_NETWORK))
    async def reachability(
        origin: LatLon,
        targets: list[LatLon] | None = None,
        grid: ReachabilityGrid | None = None,
        mode: Literal["car", "truck", "walk", "bike"] = "car",
        metric: Literal["minutes"] = "minutes",
    ) -> dict[str, Any]:
        if (targets is None) == (grid is None):
            raise ToolError("pass exactly one of `targets` or `grid`")
        if grid is not None:
            targets = _grid_targets(grid)
        if not targets:
            raise ToolError("no targets — empty list or a grid smaller than one step")
        budget, chunking = _matrix_budget()
        per_request = budget - 1                     # the origin rides along
        if len(targets) > REACHABILITY_TARGETS_HARD_MAX:
            raise ToolError(
                f"{len(targets)} targets exceeds the hard ceiling "
                f"{REACHABILITY_TARGETS_HARD_MAX} — split the area or coarsen the grid")
        if len(targets) > per_request and not chunking:
            raise ToolError(
                f"{len(targets)} targets need chunked requests, which the "
                f"public-demo budget (~{REACHABILITY_DEMO_BUDGET} coordinates "
                "per table request) does not allow. Self-host the backend, "
                "raise its cap (OSRM --max-table-size / Valhalla "
                "service_limits.<costing>.max_matrix_locations) and set "
                f"{MATRIX_MAX_ENV} to the raised budget.")
        minutes: list[float] = []
        chunks = 0
        for start in range(0, len(targets), per_request):
            chunk = targets[start:start + per_request]
            chunks += 1
            body = {
                "points": [origin.model_dump()] + [t.model_dump() for t in chunk],
                "mode": mode,
                "sources": [0],
                "targets": list(range(1, len(chunk) + 1)),
            }
            answer = await _call("POST", "/matrix", body=body)
            minutes.extend(answer["durations_min"][0])
        return {
            "origin": origin.model_dump(),
            "metric": metric,
            "targets": [
                {"lat": t.lat, "lon": t.lon, "minutes": m}
                for t, m in zip(targets, minutes)
            ],
            "meta": {"count": len(targets), "requests": chunks,
                     "grid": grid.model_dump() if grid else None},
        }

    @server.tool(description=(
        "What the ACTIVE routing backend really supports: which `avoid` "
        "classes are honoured, whether `alternates` and `isochrone` work at "
        "all, multi-stop and turn-by-turn support. Ask this before offering "
        "avoid/alternates/isochrone options — the answer differs between "
        "Valhalla and OSRM deployments. Works in every runtime mode (the "
        "avoid probe is skipped offline and reports empty)."))
    async def routing_capabilities() -> dict[str, Any]:
        return await _call("GET", "/routing/capabilities")

    # -- map content -------------------------------------------------------

    @server.tool(description=(
        "Extract map features inside a WGS84 bbox as GeoJSON with ALL "
        "properties: layers from 'buildings' (real height/min_height metres "
        "on most), 'roads', 'pois' (name+kind), 'water', 'landuse', 'places' "
        "— comma-select what you need. Auto zoom picks z15/z14 (buildings "
        f"and POIs need z14+); hard cap {FEATURE_TILES_CAP} covering tiles "
        "(~6x6 km at z15) — a too-large bbox is refused with guidance, so "
        "shrink the box rather than retry. Default feature limit "
        f"{FEATURE_DEFAULT_LIMIT} (max {FEATURE_MAX_LIMIT}); `meta.truncated` "
        "says when it bit. Geometry is tile-clipped; the same feature id "
        "crossing a tile border is returned once. Works offline for areas "
        "already bundled/cached; otherwise fetches through the tile ladder "
        "(mode online/mixed)."))
    async def extract_features(
        bbox: BBoxDeg,
        layers: str = "buildings,roads,pois",
        zoom: int | None = None,
        limit: int = FEATURE_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {**bbox.model_dump(), "layers": layers, "limit": limit}
        if zoom is not None:
            params["zoom"] = zoom
        return await _call("GET", "/vector/features", params=params)

    @server.tool(description=(
        "Where the streets in a bbox actually RUN: decoded street-line "
        "geometry ({lat,lon} courses), name, road kind and per-segment "
        "length_m — what the geocoder (one point) and the router (only the "
        "trip's streets) cannot answer. Optional `name` filters "
        "case-insensitively (substring). Box budget: "
        f"{STREET_SURVEY_TILES_CAP} tiles at the chosen zoom (a block or "
        "two, not a city) — refused with guidance beyond that. Works "
        "offline for bundled/cached areas."))
    async def street_survey(
        bbox: BBoxDeg,
        name: str = "",
        zoom: int = 15,
        limit: int = 200,
    ) -> dict[str, Any]:
        return await _call("GET", "/vector/streets", params={
            **bbox.model_dump(), "name": name, "zoom": zoom, "limit": limit})

    # -- rendering -----------------------------------------------------------

    @server.tool(description=(
        "Server-side screenshot of the real map runtime (the SAME renderer a "
        "person sees, pixel-for-pixel) returned inline as image bytes — "
        "never a URL. `view`: 'hybrid' (imagery+labels), 'satellite', "
        "'map-light', 'map-dark'. Caps: width <= "
        f"{RENDER_WIDTH_MAX}px, height <= {RENDER_HEIGHT_MAX}px, scale "
        "1|2|3 (devicePixelRatio). Needs Playwright + Chromium installed "
        "server-side and takes a few SECONDS per call (real page load + tile "
        "settle). Respects the runtime mode through the tile endpoints it "
        "draws from: offline renders show exactly the bundled/cached state. "
        "When redistributing the image, attach the map_attribution() credit "
        "line — that is a license obligation, not a courtesy."))
    async def render_map(
        center: LatLon,
        zoom: float,
        width: int = 1280,
        height: int = 800,
        scale: Literal[1, 2, 3] = 1,
        view: Literal["hybrid", "satellite", "map-light", "map-dark"] = "hybrid",
        format: Literal["png", "jpeg"] = "png",
        source_id: str = "de-dop",
        bearing: float = 0.0,
        pitch: float = 0.0,
        quality: int = 85,
    ) -> Image:
        if width > RENDER_WIDTH_MAX or height > RENDER_HEIGHT_MAX:
            raise ToolError(
                f"render caps: width <= {RENDER_WIDTH_MAX}, height <= "
                f"{RENDER_HEIGHT_MAX} (got {width}x{height})")
        body = {
            "view": {"center": center.model_dump(), "zoom": zoom,
                     "source_id": source_id, "bearing": bearing, "pitch": pitch},
            "width": width, "height": height, "scale": scale,
            "format": format, "quality": quality, "default_view": view,
        }
        response = await client.post("/render", json=body)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ToolError(f"{detail}")
        return Image(data=response.content, format=format)

    # -- sources, licensing, attribution ---------------------------------

    @server.tool(description=(
        "Every configured tile source and composite federation, with their "
        "license fields verbatim (attribution, license_name, license_url). "
        "Only sources whose license allows the use are configured at all — "
        "that is the product's hard constraint. Works in every runtime "
        "mode."))
    async def list_tile_sources() -> dict[str, Any]:
        return {
            "sources": await _call("GET", "/sources"),
            "composites": await _call("GET", "/composites"),
        }

    @server.tool(description=(
        "The exact credit line for a given map view: which licenses apply to "
        "the tiles actually shown in `bbox` at `zoom` (per-tile resolution — "
        "only the source that WINS a tile is credited). Returns the "
        "ready-to-print `text` plus per-source license details. This text is "
        "REQUIRED alongside any rendered or extracted output you "
        "redistribute — licensing is this product's hard constraint. "
        "`sources` defaults to the configured composites + world basemap. "
        "Works in every runtime mode."))
    async def map_attribution(
        bbox: BBoxDeg,
        zoom: int,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        if not sources:
            sources = list(api.composites)
            # The world basemap actually served: the ocean-blended
            # blue-marble-plus when configured, plain blue-marble otherwise.
            for world_id in ("blue-marble-plus", "blue-marble"):
                if world_id in api.sources:
                    sources.append(world_id)
                    break
            if not sources:
                sources = list(api.sources)
        return await _call("GET", "/attribution", params={
            "src": ",".join(sources), "z": zoom, **bbox.model_dump()})

    # -- runtime mode ------------------------------------------------------

    @server.tool(description=(
        "Current runtime mode. Three states gate every tool here: 'online' = "
        "everything allowed (authoring + provisioning); 'mixed' = live "
        "per-request lookups (routing, geocoding, live tiles) allowed but "
        "bulk provisioning (harvest/extract) refused — authoring without "
        "silently minting gigabytes; 'offline' = presentation mode, only "
        "bundled/cached data serves, every upstream connection refused."))
    async def get_runtime_mode() -> dict[str, Any]:
        return await _call("GET", "/mode")

    @server.tool(description=(
        "Switch the runtime mode ('offline' | 'mixed' | 'online') for the "
        "WHOLE server instance — a UI session sharing this instance sees the "
        "switch too. See get_runtime_mode for what each state gates. "
        "Switching to 'offline' is how you PROVE a harvested bundle really "
        "works without internet."))
    async def set_runtime_mode(mode: RuntimeMode) -> dict[str, Any]:
        return await _call("POST", "/mode", body={"mode": mode})

    # -- offline provisioning ---------------------------------------------

    @server.tool(description=(
        "Preview what harvesting a MapSpec would fetch: tile count and "
        "estimated MB, WITHOUT downloading anything. Free to call in every "
        "runtime mode — planning is pure math. Use before "
        "harvest_offline_bundle to sanity-check the size."))
    async def plan_offline_bundle(spec: MapSpec) -> dict[str, Any]:
        return await _call("POST", "/plan", body=spec.model_dump())

    @server.tool(description=(
        "Harvest a MapSpec's raster tiles into a local offline bundle. "
        + _MODE_PROVISIONING + " Can take MINUTES for a deep spec and hits "
        "upstream providers politely (bounded concurrency, per-source "
        "delays) — call plan_offline_bundle first and keep specs focused. "
        "Returns fetched/skipped/failed/uncovered counts and bytes_fetched."))
    async def harvest_offline_bundle(spec: MapSpec) -> dict[str, Any]:
        return await _call("POST", "/harvest", body=spec.model_dump())

    @server.tool(description=(
        "Zip an already-harvested spec into a self-contained offline package "
        "(raster bundle + vector extracts + glyphs + manifest with the full "
        "license list). Reads only local data — allowed in every runtime "
        "mode, including offline. Fails with a clear conflict error when the "
        "spec was never harvested."))
    async def package_offline_bundle(spec: MapSpec) -> dict[str, Any]:
        return await _call("POST", "/package", body=spec.model_dump())

    @server.tool(description=(
        "Inventory of local offline data: harvested raster bundles "
        "(.mbtiles) and vector street extracts (.pmtiles) with zoom ranges, "
        "bounds and sizes — what would actually serve in offline mode. "
        "Works in every runtime mode."))
    async def list_offline_bundles() -> dict[str, Any]:
        return {
            "raster": await _call("GET", "/bundles"),
            "vector": await _call("GET", "/vector"),
        }

    # -- region-bulk provisioning ("offline routing for all Europe") --------

    _SIZE_TABLE = " | ".join(preset_size_table())

    @server.tool(description=(
        "Provision a WHOLE REGION for offline use in one call: region "
        "presets 'de' (Germany), 'dach' (DE+AT+CH), 'eu' (Europe), 'earth' "
        "(the global z0-12 imagery base ladder), or a custom `bbox` (+ "
        "`region_id` slug). Layers: 'maps' (vector streets/labels z0-15 via "
        "pmtiles extract), 'aerial' (imagery pyramid — REQUIRES "
        f"`aerial_max_zoom` in {AERIAL_ZOOM_CAPS}; z17+ is deliberately not "
        "offered, full-resolution stays a corridor harvest), 'routing' (the "
        "Geofabrik region PBF downloaded and staged into the compose "
        "stack's valhalla_data/ + nominatim_data/; `start_stack=true` "
        "additionally runs `docker compose up -d` when Docker exists — "
        "otherwise the job reports the exact next command instead of "
        "failing). TWO-CALL CONTRACT: the first call WITHOUT "
        "`confirm_size=true` only returns the size estimate and starts "
        "NOTHING; repeat with confirm_size=true to start exactly that "
        "download. Jobs run async and survive restarts as 'interrupted, "
        "resumable' (re-POST the same request; cached tiles/bytes are "
        "skipped). SIZES (computed, ~12 KB/tile): " + _SIZE_TABLE + ". "
        "HONESTY: EU aerial z13-15 is ~370 GB against public state services "
        "— weeks of polite downloading; EU routing means a ~35 GB PBF and "
        "MANY HOURS of Valhalla graph build (Germany: tens of minutes to "
        "~1 h). " + _MODE_PROVISIONING))
    async def provision_offline_region(
        region: str = "",
        bbox: BBoxDeg | None = None,
        layers: list[Literal["maps", "aerial", "routing"]] | None = None,
        aerial_max_zoom: int | None = None,
        confirm_size: bool = False,
        region_id: str = "",
        pbf_url: str = "",
        routing_stack_dir: str = "",
        start_stack: bool = False,
    ) -> dict[str, Any]:
        if not layers:
            raise ToolError("pass `layers`: any of 'maps', 'aerial', 'routing'")
        if bool(region) == (bbox is not None):
            raise ToolError("pass exactly one of `region` (de/dach/eu/earth) or `bbox`")
        if "aerial" in layers and region != "earth" and aerial_max_zoom not in AERIAL_ZOOM_CAPS:
            raise ToolError(
                f"aerial layer needs aerial_max_zoom in {AERIAL_ZOOM_CAPS} — the zoom "
                "cap IS the size dial (z17+ stays corridor-harvest territory)")
        body: dict[str, Any] = {
            "region": region,
            "region_id": region_id,
            "layers": layers,
            "aerial_max_zoom": aerial_max_zoom,
            "pbf_url": pbf_url,
            "routing_stack_dir": routing_stack_dir,
            "start_stack": start_stack,
        }
        if bbox is not None:
            body["bbox"] = bbox.model_dump()
        estimate = await _call("POST", "/provision/estimate", body=body)
        if not confirm_size:
            return {
                "started": False,
                "estimate": estimate,
                "note": ("Nothing downloaded yet — this is the cost. Re-call "
                         "with confirm_size=true to start exactly this job."),
            }
        job = await _call("POST", "/provision", body=body)
        return {"started": True, "estimate": estimate, "job": job,
                "note": "Poll provisioning_status(job_id) for progress."}

    @server.tool(description=(
        "Progress of region-provisioning jobs: pass `job_id` for one job "
        "(state, phase, tiles done/total, bytes — honest counts, no ETA), "
        "omit it to list all known jobs newest-first, including "
        "'interrupted' ones persisted across a server restart (those are "
        "resumable: re-issue the same provision_offline_region request). "
        "Works in every runtime mode."))
    async def provisioning_status(job_id: str | None = None) -> dict[str, Any]:
        if job_id is None:
            return {"jobs": await _call("GET", "/provision")}
        return await _call("GET", f"/provision/{job_id}")

    @server.tool(description=(
        "Cancel a running region-provisioning job. Already-downloaded "
        "tiles/bytes stay on disk, so a later identical "
        "provision_offline_region call resumes instead of restarting. "
        "Works in every runtime mode."))
    async def cancel_provisioning(job_id: str) -> dict[str, Any]:
        return await _call("POST", f"/provision/{job_id}/cancel")

    # -- professional display (browser sessions & embeddable pages) --------

    @server.tool(description=(
        "Open the results in the FULL map application on this server — the "
        "professional display surface (sage cartography, routes with "
        "clickable alternates, via-place labels, turn-by-turn panel, "
        "attribution). Pass routes (as returned by route/create_trip), "
        "markers (lat/lon/label stops), highlights, a raw MapSpec, or "
        "`trip_id` for a live trip. TRIP FLOW: one STABLE session per trip "
        "— the FIRST call opens the user's browser; the open tab then polls "
        "the trip revision and applies every update_trip/optimize_trip "
        "change IN PLACE (route redrawn, subtle toast, camera kept), so "
        "LATER calls for the same trip default to NOT opening another tab "
        "and return the same URL with already_open=true. `open_browser`: "
        "omit for that default, true forces a (re)open, false never opens. "
        "Non-trip calls store a frozen view and open the browser per call. "
        f"Sessions live {int(SESSION_TTL_S / 60)} min past the last poll "
        f"(payload cap {SESSION_PAYLOAD_MAX_BYTES // (1024 * 1024)} MB). "
        "Browser opening is appropriate exactly because this LOCAL server "
        "and the user share a machine at the user's own request."))
    async def open_map_view(
        routes: list[MapRoute] | None = None,
        markers: list[RouteStop] | None = None,
        highlights: list[MapHighlight] | None = None,
        spec: MapSpec | None = None,
        trip_id: str | None = None,
        title: str = "",
        source_id: str = "de-dop",
        open_browser: bool | None = None,
    ) -> dict[str, Any]:
        view_spec = await _build_view_spec(
            routes, markers, highlights, spec, trip_id, title, source_id)
        session: dict[str, Any] | None = None
        if trip_id is not None:
            token = trip_sessions.get(trip_id)
            if token is not None and api.session_alive(token):
                # The stable per-trip session: same token, same URL, same
                # tab — it serves the trip's CURRENT state on every read.
                session = {
                    "token": token,
                    "url": f"{api.public_base_url()}/?session={token}",
                    "expires_in_s": int(SESSION_TTL_S),
                }
            else:
                # Expired/never minted: the fresh token is a fresh tab too.
                trip_opened.discard(trip_id)
        if session is None:
            session = await _create_session(view_spec)
            if trip_id is not None:
                api.bind_session_source(
                    session["token"], *_trip_session_source(trip_id, source_id))
                trip_sessions[trip_id] = session["token"]
        already_open = trip_id is not None and trip_id in trip_opened
        should_open = open_browser if open_browser is not None else not already_open
        opened = False
        note = ""
        if should_open:
            try:
                opened = bool(webbrowser.open(session["url"]))
            except Exception as error:  # noqa: BLE001 - headless env, no browser
                note = f"browser could not be opened here: {error}"
            if opened and trip_id is not None:
                trip_opened.add(trip_id)
        result: dict[str, Any] = {**session, "opened": opened}
        if already_open:
            result["already_open"] = True
            if not opened:
                note = note or (
                    "a browser tab for this trip is already open and updates "
                    "itself via revision polling — no new tab; pass "
                    "open_browser=true to force one")
        if note:
            result["note"] = note
        return result

    @server.tool(description=(
        "One SELF-CONTAINED HTML document of the route(s) for your own "
        "display surface (artifact/preview): our design language, the "
        "primary route plus CLICKABLE alternates (clicking swaps colours "
        "and the turn-by-turn list — inline JS, works from file:// with "
        "network blocked), via-place labels, German step list, and a "
        "MANDATORY attribution footer baked into the document — the HTML "
        "cannot be obtained without the credits. Never references "
        "third-party hosts. Basemap tiers: `basemap='auto'` references THIS "
        "server's own tile endpoints and degrades cleanly when unreachable; "
        "`basemap='embedded'` emits zero network references (styled canvas "
        "+ graticule instead of imagery). Size cap "
        f"{MAP_PAGE_MAX_BYTES // 1024} KB; step lists cut at "
        f"{MAP_PAGE_MAX_STEPS} with an honest '+N weitere Schritte'. "
        "Accepts routes, a raw spec, or `trip_id` (current live-trip "
        "state). Use this INSTEAD of rebuilding a map from raw geometry on "
        "third-party tile servers — that violates tile-usage policies and "
        "loses attribution. Positioning: route/create_trip = data, "
        "open_map_view = full app in the user's browser, get_map_page_html "
        "= embeddable page for the client's own display."))
    async def get_map_page_html(
        routes: list[MapRoute] | None = None,
        markers: list[RouteStop] | None = None,
        spec: MapSpec | None = None,
        trip_id: str | None = None,
        title: str = "",
        source_id: str = "de-dop",
        basemap: Literal["auto", "embedded"] = "auto",
    ) -> dict[str, Any]:
        view_spec = await _build_view_spec(
            routes, markers, None, spec, trip_id, title, source_id)
        session = await _create_session(view_spec)
        response = await client.get(
            f"/sessions/{session['token']}/page", params={"basemap": basemap})
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ToolError(f"{detail}")
        html = response.text
        return {
            "html": html,
            "bytes": len(html.encode()),
            "session_url": session["url"],
            "note": ("Die Herkunftsangabe im Dokumentfuß ist Bestandteil des "
                     "Dokuments und muss bei jeder Weitergabe erhalten bleiben."),
        }

    # -- live trips --------------------------------------------------------

    _TRIP_LIMITS = (
        f"Store keeps at most {TRIP_STORE_MAX_TRIPS} trips / "
        f"{TRIP_STORE_MAX_BYTES // (1024 * 1024)} MB estimated geometry, "
        "evicting least-recently-used first; a trip idle for "
        f"{int(TRIP_TTL_S / 60)} min expires. Every read/mutation refreshes "
        "the clock. An unknown/evicted trip_id errors with instructions to "
        "recreate the trip."
    )

    @server.tool(description=(
        "Start a LIVE trip: computes the route and keeps it alive "
        "server-side so stops can be inserted/removed/reordered afterwards "
        "without resending anything (update_trip). Returns the trip state: "
        "trip_id, enriched stops (structured address per stop), and the "
        "full computed route (geometry, legs, steps, via_places, "
        f"alternates). {ROUTE_STOPS_MIN}..{ROUTE_STOPS_MAX} stops; "
        f"`alternates` 0..{ROUTE_ALTERNATES_MAX} applies to every "
        "recompute (2-stop trips only, like route). Show it with "
        "open_map_view(trip_id=...) or get_map_page_html(trip_id=...). "
        + _TRIP_LIMITS + " " + _MODE_NETWORK))
    async def create_trip(
        stops: list[RouteStop],
        mode: Literal["car", "truck", "walk", "bike"] = "car",
        avoid: list[Literal["toll", "motorway", "ferry"]] | None = None,
        alternates: int = 2,
    ) -> dict[str, Any]:
        if not ROUTE_STOPS_MIN <= len(stops) <= ROUTE_STOPS_MAX:
            raise ToolError(
                f"a trip needs {ROUTE_STOPS_MIN}..{ROUTE_STOPS_MAX} stops, got {len(stops)}")
        alternates = max(0, min(alternates, ROUTE_ALTERNATES_MAX))
        await _enrich_stops(stops)
        result = await _compute_route(stops, mode, avoid or [], alternates,
                                      steps=True, route_id="trip")
        trip = TripState(
            id=trips.mint_id(), stops=stops, mode=mode, avoid=avoid or [],
            alternates=alternates, route=MapRoute.model_validate(result),
        )
        trips.put(trip)
        return trip.model_dump()

    @server.tool(description=(
        "Edit a live trip and get the FRESH result: applies the operations "
        "in order, then recomputes the route EXACTLY ONCE (multi-edit = one "
        "recompute) and returns the updated trip state (revision bumped). "
        "Ops: {op:'add_stop', stop:{lat,lon,label}, position?} inserts at "
        "0-based position (omit = append as new destination); "
        "{op:'remove_stop', index}; {op:'move_stop', index, position}; "
        "{op:'set_options', mode?, avoid?, alternates?} — only given fields "
        "change. One tool instead of four: the op list keeps multi-edits "
        "atomic and the roster readable. New stops are address-enriched "
        "once; existing enrichment is cached. A map tab opened via "
        "open_map_view(trip_id=...) picks the change up LIVE within a few "
        "seconds — do not open a new view per edit. " + _TRIP_LIMITS + " "
        + _MODE_NETWORK))
    async def update_trip(trip_id: str, operations: list[TripOp]) -> dict[str, Any]:
        try:
            trip = trips.get(trip_id)
        except KeyError as error:
            raise ToolError(str(error))
        if not operations:
            raise ToolError("no operations given — use get_trip to just read")
        stops = list(trip.stops)
        for op in operations:
            if op.op == "add_stop":
                if op.stop is None:
                    raise ToolError("add_stop needs `stop`")
                position = len(stops) if op.position is None else max(0, min(op.position, len(stops)))
                stops.insert(position, op.stop)
            elif op.op == "remove_stop":
                if op.index is None or not 0 <= op.index < len(stops):
                    raise ToolError(f"remove_stop: index must be 0..{len(stops) - 1}")
                stops.pop(op.index)
            elif op.op == "move_stop":
                if op.index is None or not 0 <= op.index < len(stops):
                    raise ToolError(f"move_stop: index must be 0..{len(stops) - 1}")
                if op.position is None:
                    raise ToolError("move_stop needs `position`")
                stop = stops.pop(op.index)
                stops.insert(max(0, min(op.position, len(stops))), stop)
            elif op.op == "set_options":
                if op.mode is not None:
                    trip.mode = op.mode
                if op.avoid is not None:
                    trip.avoid = list(op.avoid)
                if op.alternates is not None:
                    trip.alternates = max(0, min(op.alternates, ROUTE_ALTERNATES_MAX))
        if not ROUTE_STOPS_MIN <= len(stops) <= ROUTE_STOPS_MAX:
            raise ToolError(
                f"a trip needs {ROUTE_STOPS_MIN}..{ROUTE_STOPS_MAX} stops after "
                f"editing, got {len(stops)}")
        await _enrich_stops(stops)
        result = await _compute_route(stops, trip.mode, trip.avoid,
                                      trip.alternates, steps=True, route_id="trip")
        trip.stops = stops
        trip.route = MapRoute.model_validate(result)
        trip.revision += 1
        trips.put(trip)
        return trip.model_dump()

    @server.tool(description=(
        "Read a live trip's current state WITHOUT recomputing or mutating "
        "anything (refreshes its idle clock). " + _TRIP_LIMITS))
    async def get_trip(trip_id: str) -> dict[str, Any]:
        try:
            return trips.get(trip_id).model_dump()
        except KeyError as error:
            raise ToolError(str(error))

    @server.tool(description=(
        "All live trips in this server process (most recently used first): "
        "id, stop labels, mode, revision, age — how you rediscover state "
        "mid-conversation. Over stdio this process belongs to your session; "
        "over the shared HTTP devserver, trips from other clients are "
        "visible too. " + _TRIP_LIMITS))
    async def list_trips() -> dict[str, Any]:
        import time as _time

        now = _time.time()
        return {"trips": [
            {
                "trip_id": trip.id,
                "stops": [stop.display() for stop in trip.stops],
                "mode": trip.mode,
                "revision": trip.revision,
                "age_s": round(now - trip.created_at, 1),
                "idle_s": round(now - trip.touched_at, 1),
                "size_bytes": trip.size_bytes,
            }
            for trip in trips.all()
        ]}

    return server
