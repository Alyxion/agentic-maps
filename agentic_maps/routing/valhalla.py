"""Turn-by-turn routing, reachability matrices and isochrones via a
self-hosted Valhalla server — the primary routing backend (selected by
default; see `AGENTIC_MAPS_ROUTING_BACKEND` in rest/maps_api.py).

Valhalla (https://github.com/valhalla/valhalla) is a routing engine you build
tiles for from a Geofabrik/OSM extract and run yourself; there is no public
demo server suitable for embedding in a client the way OSRM has one, so
`AGENTIC_MAPS_VALHALLA_URL` must point at a real server for this backend to do
anything. The default assumes a local dev instance on :8002, Valhalla's own
default port.

Every request/response shape below was checked against Valhalla's own docs
(https://valhalla.github.io/valhalla/api/...) rather than guessed. Where the
prose docs were silent on an exact shape — the `alternates` response
structure, the matrix service's concise-mode field names, the warning code
for a rejected hard exclusion — the answer instead comes from the serializer
source in valhalla/src/tyr/ (linked per method below); this is called out
explicitly since it is inference from source, not documentation.
"""

import os

import httpx

from ..models.isochrone_result import IsochroneResult
from ..models.isochrone_ring import IsochroneRing
from ..models.lat_lon import LatLon
from ..models.map_route import MapRoute
from ..models.optimized_route import OptimizedRoute
from ..models.route_leg import RouteLeg
from ..models.route_step import RouteStep
from .base import ContourSpec

_COSTING_BY_MODE = {"car": "auto", "truck": "truck", "walk": "pedestrian", "bike": "bicycle"}

# Valhalla's maneuver `type` enum (turn-by-turn/api-reference.md#maneuver-type)
# mapped onto the (type, modifier) vocabulary RouteStep already speaks —
# mirrored from OSRM's own maneuver vocabulary (route_step.py), so a step
# looks the same regardless of which backend produced it. Best-effort:
# Valhalla distinguishes more cases (ramps, roundabout side, transit) than
# OSRM's vocabulary has room for, so several collapse onto the same pair.
# Anything not listed here (elevators, steps, building entries, transit —
# none of which apply to car/truck/walk/bike routing) falls back to
# ("notification", "").
_MANEUVER_TYPES: dict[int, tuple[str, str]] = {
    0: ("depart", ""),            # kNone
    1: ("depart", ""),            # kStart
    2: ("depart", "right"),       # kStartRight
    3: ("depart", "left"),        # kStartLeft
    4: ("arrive", ""),            # kDestination
    5: ("arrive", "right"),       # kDestinationRight
    6: ("arrive", "left"),        # kDestinationLeft
    7: ("new name", ""),          # kBecomes
    8: ("continue", ""),          # kContinue
    9: ("turn", "slight right"),  # kSlightRight
    10: ("turn", "right"),        # kRight
    11: ("turn", "sharp right"),  # kSharpRight
    12: ("turn", "uturn"),        # kUturnRight
    13: ("turn", "uturn"),        # kUturnLeft
    14: ("turn", "sharp left"),   # kSharpLeft
    15: ("turn", "left"),         # kLeft
    16: ("turn", "slight left"),  # kSlightLeft
    17: ("on ramp", "straight"),  # kRampStraight
    18: ("on ramp", "right"),     # kRampRight
    19: ("on ramp", "left"),      # kRampLeft
    20: ("off ramp", "right"),    # kExitRight
    21: ("off ramp", "left"),     # kExitLeft
    22: ("fork", "straight"),     # kStayStraight
    23: ("fork", "right"),        # kStayRight
    24: ("fork", "left"),         # kStayLeft
    25: ("merge", ""),            # kMerge
    26: ("roundabout", ""),       # kRoundaboutEnter
    27: ("exit roundabout", ""),  # kRoundaboutExit
    28: ("notification", ""),     # kFerryEnter
    29: ("notification", ""),     # kFerryExit
    37: ("merge", "right"),       # kMergeRight
    38: ("merge", "left"),        # kMergeLeft
}


def decode_polyline6(encoded: str) -> list[LatLon]:
    """Decode a Valhalla-encoded route/matrix shape.

    Six digits of decimal precision (1e6) — NOT Google's original polyline
    algorithm's five (1e5). Using the wrong factor places every point off by
    roughly two orders of magnitude ("commonly, in the middle of an ocean",
    per Valhalla's own decoding docs). Algorithm and precision verified
    against https://valhalla.github.io/valhalla/api/decoding/, including its
    own worked example — see `test_decode_polyline6_matches_valhalla_example`
    in tests/test_routing_backends.py.
    """
    inv = 1.0 / 1e6
    coords: list[LatLon] = []
    lat = lon = 0
    index = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lon += delta
        coords.append(LatLon(lat=round(lat * inv, 6), lon=round(lon * inv, 6)))
    return coords


def _polygon_rings(geometry: dict) -> list[list[list[float]]]:
    """Exterior ring(s) of a GeoJSON Polygon/MultiPolygon isochrone feature.

    Interior rings (holes — an unreachable pocket inside an otherwise
    reachable area, e.g. a lake) are dropped: `IsochroneRing` has no hole
    concept yet, and a polygon that is slightly too generous around a lake is
    a smaller sin than a keyed model nothing downstream can render correctly.
    `polygons=True` is always requested, so the LineString case is only a
    defensive fallback, not an expected path.
    """
    kind = geometry.get("type")
    if kind == "Polygon":
        rings = geometry.get("coordinates") or []
        return [rings[0]] if rings else []
    if kind == "MultiPolygon":
        return [poly[0] for poly in geometry.get("coordinates") or [] if poly]
    if kind == "LineString":
        return [geometry.get("coordinates") or []]
    return []


class ValhallaRouter:
    supports_alternates = True
    supports_isochrone = True

    # "Hard exclusions" (exclude_tolls/exclude_highways/exclude_ferries) are
    # silently downgraded to no-ops unless the server config sets
    # `service_limits.allow_hard_exclusions` — the request still succeeds,
    # but a warning with this code is added (src/exceptions.cc: "Hard
    # exclusions are not allowed on this server, ignoring hard excludes").
    # Not documented in the prose API reference; found in the serializer/
    # exception source, since honouring "avoid tolls" only where the server
    # actually does is the same principle OsrmRouter.supported_avoid() uses.
    _HARD_EXCLUSION_WARNING_CODE = 208
    _AVOID_TO_COSTING_FLAG = {
        "toll": "exclude_tolls", "motorway": "exclude_highways", "ferry": "exclude_ferries",
    }

    def __init__(self, base_url: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (
            base_url
            or os.environ.get("AGENTIC_MAPS_VALHALLA_URL", "").strip()
            or "http://localhost:8002"
        ).rstrip("/")
        self.transport = transport
        self._avoid_cache: list[str] | None = None

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(transport=self.transport) as client:
            response = await client.post(f"{self.base_url}{path}", json=body, timeout=30.0)
            response.raise_for_status()
            return response.json()

    def _costing_options(self, mode: str, avoid: list[str] | None) -> dict:
        if not avoid:
            return {}
        costing = _COSTING_BY_MODE.get(mode, "auto")
        flags = {
            self._AVOID_TO_COSTING_FLAG[name]: True
            for name in avoid if name in self._AVOID_TO_COSTING_FLAG
        }
        return {"costing_options": {costing: flags}} if flags else {}

    async def route(
        self,
        start: LatLon,
        end: LatLon,
        *,
        route_id: str,
        from_location: str = "",
        to_location: str = "",
        mode: str = "car",
        via: list[LatLon] | None = None,
        steps: bool = False,
        avoid: list[str] | None = None,
        alternates: int = 0,
    ) -> MapRoute:
        """Start to end, optionally through `via` stops in order.

        Request/response verified against
        https://valhalla.github.io/valhalla/api/route/api-reference/.
        Alternates are "not yet supported on multipoint routes", so they are
        only requested when there is no `via` — Valhalla would otherwise just
        not return any, but asking only when it can matter keeps this from
        silently depending on an upstream limitation the caller cannot see.

        The response's `alternates` array (one `{"trip": {...}}` per
        alternate route, sibling to the primary `trip`) is not documented in
        the prose reference; confirmed from
        src/tyr/route_serializer_valhalla.cc, which writes the primary
        route's `trip` at the top level and every subsequent route into an
        `alternates` array in the same shape.
        """
        costing = _COSTING_BY_MODE.get(mode, "auto")
        points = [start, *(via or []), end]
        body: dict = {
            "locations": [{"lat": p.lat, "lon": p.lon} for p in points],
            "costing": costing,
            "units": "kilometers",
            "id": route_id,
        }
        body.update(self._costing_options(mode, avoid))
        if alternates and len(points) == 2:
            body["alternates"] = alternates
        payload = await self._post("/route", body)
        if "trip" not in payload:
            raise ValueError(f"Valhalla returned no route: {payload}")
        canonical_mode = mode if mode in _COSTING_BY_MODE else "car"
        primary = self._map_route(
            payload["trip"], route_id=route_id, from_location=from_location,
            to_location=to_location, mode=canonical_mode, stops=points, steps=steps,
        )
        primary.alternates = [
            self._map_route(
                alt["trip"], route_id=f"{route_id}-alt{index}", from_location=from_location,
                to_location=to_location, mode=canonical_mode, stops=points, steps=steps,
            )
            for index, alt in enumerate(payload.get("alternates", []))
        ]
        return primary

    def _map_route(
        self, trip: dict, *, route_id: str, from_location: str, to_location: str,
        mode: str, stops: list[LatLon], steps: bool,
    ) -> MapRoute:
        geometry: list[LatLon] = []
        legs: list[RouteLeg] = []
        instructions: list[RouteStep] = []
        for leg in trip.get("legs", []):
            leg_shape = decode_polyline6(leg.get("shape", ""))
            # Each leg's shape starts at the point the previous leg ended on;
            # keep that vertex once, not once per leg, or a multi-stop route
            # gets a duplicate point at every intermediate stop.
            geometry.extend(leg_shape[1:] if geometry else leg_shape)
            summary = leg.get("summary", {})
            legs.append(RouteLeg(
                duration_min=round(summary.get("time", 0) / 60.0, 1),
                distance_km=round(summary.get("length", 0), 1),
            ))
            if steps:
                for maneuver in leg.get("maneuvers", []):
                    kind, modifier = _MANEUVER_TYPES.get(maneuver.get("type", 0), ("notification", ""))
                    begin = maneuver.get("begin_shape_index", 0)
                    instructions.append(RouteStep(
                        type=kind,
                        modifier=modifier,
                        exit=maneuver.get("roundabout_exit_count"),
                        name=" / ".join(maneuver.get("street_names", []) or []),
                        distance_m=round(maneuver.get("length", 0.0) * 1000.0, 1),
                        duration_s=maneuver.get("time", 0.0),
                        location=leg_shape[begin] if begin < len(leg_shape) else None,
                    ))
        summary = trip.get("summary", {})
        return MapRoute(
            id=route_id, from_location=from_location, to_location=to_location, mode=mode,
            duration_min=round(summary.get("time", 0) / 60.0, 1),
            distance_km=round(summary.get("length", 0), 1),
            geometry=geometry, legs=legs, steps=instructions, stops=stops,
        )

    async def matrix(
        self,
        points: list[LatLon],
        *,
        mode: str = "car",
        sources: list[int] | None = None,
        targets: list[int] | None = None,
    ) -> list[list[float]]:
        """Travel times in minutes, via `/sources_to_targets`.

        Natively asymmetric: the service takes SEPARATE `sources` and
        `targets` location arrays (that is its whole name), so the optional
        index lists here simply select which of `points` go into each array
        — durations come back row-major, sources × targets, exactly like
        the all-pairs case where both arrays are the full list.

        Requested with `"verbose": false`. The concise-mode response shape —
        `{"sources_to_targets": {"durations": [[seconds, ...], ...],
        "distances": [...]}}`, row-ordered the same way OSRM's `durations`
        table is — is under-specified in the prose docs (which describe
        verbose mode in detail and only say concise mode returns "more
        compact, nested row-major arrays"); confirmed from
        src/tyr/matrix_serializer.cc, whose `serialize_duration` writes raw
        seconds regardless of the request's `units`, so this only ever
        divides by 60 (`distances`, which OSRM's client also drops, would
        need the `units` param honoured before being usable).
        """
        costing = _COSTING_BY_MODE.get(mode, "auto")
        locations = [{"lat": p.lat, "lon": p.lon} for p in points]
        source_locs = locations if sources is None else [locations[i] for i in sources]
        target_locs = locations if targets is None else [locations[i] for i in targets]
        body = {"sources": source_locs, "targets": target_locs, "costing": costing, "verbose": False}
        payload = await self._post("/sources_to_targets", body)
        durations = (payload.get("sources_to_targets") or {}).get("durations")
        if durations is None:
            raise ValueError(f"Valhalla matrix failed: {payload}")
        return [[round((cell or 0) / 60.0, 1) for cell in row] for row in durations]

    async def optimized_route(
        self,
        stops: list[LatLon],
        *,
        route_id: str,
        mode: str = "car",
        roundtrip: bool = False,
        keep_endpoints: bool = True,
        from_location: str = "",
        to_location: str = "",
        steps: bool = False,
    ) -> OptimizedRoute:
        """TSP order + route via `POST /optimized_route`.

        Per the service's own reference (valhalla-docs/optimized/
        api-reference.md): the request is route-shaped (`locations`,
        `costing`); the solver visits every intermediate location once,
        "always starting at the first location in the list and ending at
        the last location" — i.e. endpoints are ALWAYS pinned, which is why
        `keep_endpoints=False` is refused here rather than silently
        ignored (OSRM's trip service can do free endpoints; this backend
        cannot). "Due to the reordering of the intermediate locations, an
        `original_index` is also part of the `locations` object within the
        response" — that field, on `trip.locations` in visited order, is
        the whole order mapping.

        A roundtrip has no native flag either; it is expressed by
        appending the start as the final pinned location, and the
        duplicate is dropped from the returned order.

        Costing: the docs list `auto`, `bicycle` and `pedestrian` for this
        service (multimodal explicitly unsupported; `truck` is not named),
        so `truck` maps to `auto` here — a car-shaped stop order over a
        documented gamble.
        """
        if not keep_endpoints:
            raise ValueError(
                "Valhalla's optimized_route always starts at the first and ends "
                "at the last location — keep_endpoints=false needs the OSRM backend")
        costing = _COSTING_BY_MODE.get(mode, "auto")
        if costing == "truck":
            costing = "auto"
        points = [*stops, stops[0]] if roundtrip else list(stops)
        body: dict = {
            "locations": [{"lat": p.lat, "lon": p.lon} for p in points],
            "costing": costing,
            "units": "kilometers",
            "id": route_id,
        }
        payload = await self._post("/optimized_route", body)
        trip = payload.get("trip")
        if not trip:
            raise ValueError(f"Valhalla returned no optimized route: {payload}")
        locations = trip.get("locations", [])
        if len(locations) != len(points):
            raise ValueError(
                f"Valhalla answered {len(locations)} locations for {len(points)} stops")
        order = [int(loc["original_index"]) for loc in locations]
        if roundtrip:
            # The appended duplicate start comes back as the final location.
            order = [i for i in order if i != len(stops)]
        canonical_mode = mode if mode in _COSTING_BY_MODE else "car"
        route = self._map_route(
            trip, route_id=route_id, from_location=from_location,
            to_location=to_location, mode=canonical_mode,
            stops=[stops[i] for i in order], steps=steps,
        )
        return OptimizedRoute(order=order, route=route)

    async def isochrone(
        self, center: LatLon, *, mode: str = "car", contours: list[ContourSpec],
    ) -> IsochroneResult:
        """Reachable-area contours, via `/isochrone`.

        Request shape (locations/costing/contours/polygons) verified against
        https://valhalla.github.io/valhalla/api/isochrone/. That page
        describes the response only as "GeoJSON" with a `metric` attribute
        per feature; the exact property names — `contour` (the time/distance
        value), `metric` ("time"/"distance"), `color` (plus Leaflet- and
        geojson.io-specific fill keys this client ignores) — come from
        src/tyr/isochrone_serializer.cc. Coordinates are plain `[lon, lat]`
        pairs, unlike route/matrix shapes: the isochrone service never
        polyline-encodes its output.
        """
        costing = _COSTING_BY_MODE.get(mode, "auto")
        body = {
            "locations": [{"lat": center.lat, "lon": center.lon}],
            "costing": costing,
            "polygons": True,
            "contours": [
                {"time": spec["time_min"]} if "time_min" in spec else {"distance": spec["distance_km"]}
                for spec in contours
            ],
        }
        payload = await self._post("/isochrone", body)
        rings: list[IsochroneRing] = []
        for feature in payload.get("features", []):
            geometry = feature.get("geometry", {})
            props = feature.get("properties", {})
            metric = props.get("metric")
            value = props.get("contour")
            color = "#" + str(props.get("color", "3388ff")).lstrip("#")
            for ring_coords in _polygon_rings(geometry):
                rings.append(IsochroneRing(
                    minutes=value if metric == "time" else None,
                    km=value if metric == "distance" else None,
                    color=color,
                    polygon=[LatLon(lat=lat, lon=lon) for lon, lat in ring_coords],
                ))
        return IsochroneResult(center=center, mode=mode if mode in _COSTING_BY_MODE else "car", rings=rings)

    async def supported_avoid(self) -> list[str]:
        """Which of `toll` / `motorway` / `ferry` this server actually honours.

        Sends a real probe route with all three hard-exclusion flags set and
        reads the `warnings` array back, rather than checking for an error:
        an unsupported hard exclusion does not fail the request, it just gets
        silently dropped with warning code 208 (see the class docstring).
        Probed once and remembered, same contract as
        `OsrmRouter.supported_avoid()`.
        """
        if self._avoid_cache is not None:
            return self._avoid_cache
        body = {
            "locations": [{"lat": 49.0069, "lon": 8.4037}, {"lat": 49.0100, "lon": 8.4600}],
            "costing": "auto",
            "costing_options": {"auto": {flag: True for flag in self._AVOID_TO_COSTING_FLAG.values()}},
        }
        try:
            payload = await self._post("/route", body)
        except (httpx.HTTPError, ValueError):
            self._avoid_cache = []
            return []
        codes = {warning.get("code") for warning in payload.get("warnings", [])}
        supported = [] if self._HARD_EXCLUSION_WARNING_CODE in codes else list(self._AVOID_TO_COSTING_FLAG)
        self._avoid_cache = supported
        return supported
