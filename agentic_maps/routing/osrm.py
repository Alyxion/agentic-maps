"""Driving/walking routes via any OSRM-compatible HTTP API.

Secondary/legacy backend — Valhalla (`valhalla.py`) is the default (see
`AGENTIC_MAPS_ROUTING_BACKEND` in rest/maps_api.py); this stays for sites that
already run an OSRM container. Used at authoring time only: the resulting
MapRoute (duration + geometry) is embedded in the MapSpec, so the
presentation runtime needs no routing service. Dev default is the public OSRM
demo server (light interactive use only, per its usage policy);
production/batch use should point AGENTIC_MAPS_OSRM_URL at a self-hosted OSRM
container fed with a Geofabrik OSM extract (ODbL).

OSRM has no isochrone service and no truck profile, and does not return
alternates through this client — see `isochrone()` and the `truck` mapping
below.

Transit (GTFS-based) routing is a separate, later provider — see
docs/concept.md §routing.
"""

import os

import httpx

from ..models.isochrone_result import IsochroneResult
from ..models.lane_info import LaneInfo
from ..models.lat_lon import LatLon
from ..models.map_route import MapRoute
from ..models.optimized_route import OptimizedRoute
from ..models.route_leg import RouteLeg
from ..models.route_step import RouteStep
from .base import ContourSpec

# OSRM has no dedicated truck profile; `driving` at least respects car access
# restrictions rather than nothing. A self-hosted OSRM built with the truck
# profile extension would need its own mapping — not attempted here since the
# public demo (the dev default) only ever runs `driving`/`walking`/`cycling`.
_PROFILE_BY_MODE = {"car": "driving", "truck": "driving", "walk": "walking", "bike": "cycling"}


class OsrmRouter:
    # What `/routing/capabilities` reports without a network round trip —
    # unlike `avoid`, these do not depend on how the server was compiled.
    # Alternates: OSRM's `alternatives=` works on plain A->B requests (the
    # public demo included — verified live, Dettingen->Frankfurt answers two
    # routes); like Valhalla, none on multi-stop trips.
    supports_alternates = True
    supports_isochrone = False

    def __init__(self, base_url: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (
            base_url
            or os.environ.get("AGENTIC_MAPS_OSRM_URL", "").strip()
            or "https://router.project-osrm.org"
        ).rstrip("/")
        self.transport = transport

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
        """Start to end, optionally through `via` stops in the given order.

        OSRM routes through any number of coordinates in one request, so a
        multi-stop trip is one call and one geometry — not several routes
        stitched together, which would double-count the joins and give a total
        that disagrees with the drawn line.

        Alternates are requested only for a plain A->B trip (no `via`) —
        OSRM does not produce them for multi-waypoint requests, same
        limitation Valhalla has. `routes[0]` is the primary, the rest come
        back attached at `MapRoute.alternates`.
        """
        profile = _PROFILE_BY_MODE.get(mode, "driving")
        points = [start, *(via or []), end]
        coords = ";".join(f"{p.lon},{p.lat}" for p in points)
        query = "overview=full&geometries=geojson"
        if steps:
            query += "&steps=true"
        if alternates and len(points) == 2:
            query += f"&alternatives={alternates}"
        if avoid:
            # Only a self-hosted OSRM built with these exclude classes accepts
            # them; the public demo answers "Exclude flag combination is not
            # supported". The caller surfaces that rather than pretending the
            # option was honoured — a route that silently ignores "avoid
            # motorways" is worse than one that says it cannot.
            query += "&exclude=" + ",".join(sorted(set(avoid)))
        url = f"{self.base_url}/route/v1/{profile}/{coords}?{query}"
        async with httpx.AsyncClient(transport=self.transport) as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            body = response.json()
        if body.get("code") != "Ok" or not body.get("routes"):
            raise ValueError(f"OSRM returned no route: {body.get('code')}")
        primary = self._map_route(
            body["routes"][0], route_id=route_id, from_location=from_location,
            to_location=to_location, mode=mode, stops=points,
        )
        primary.alternates = [
            self._map_route(
                entry, route_id=f"{route_id}-alt{index}", from_location=from_location,
                to_location=to_location, mode=mode, stops=points,
            )
            for index, entry in enumerate(body["routes"][1:])
        ]
        return primary

    def _map_route(
        self, entry: dict, *, route_id: str, from_location: str, to_location: str,
        mode: str, stops: list[LatLon],
    ) -> MapRoute:
        """One OSRM `routes[]` entry -> MapRoute — shared by the primary and
        every alternate, so the speed fixups below apply to all of them."""
        geometry = [LatLon(lat=lat, lon=lon) for lon, lat in entry["geometry"]["coordinates"]]
        duration_min = round(entry["duration"] / 60.0, 1)
        distance_km = round(entry["distance"] / 1000.0, 1)
        # The public OSRM demo runs ONLY the car profile — /walking/ and
        # /cycling/ quietly answer with driving times ("2 min for a
        # kilometre on foot"). If the implied speed is beyond the mode's
        # plausible ceiling, recompute at a typical speed for the mode; a
        # self-hosted server with real foot/bike profiles passes untouched.
        _SPEED_FIXUPS = {"walk": (6.0, 4.7), "bike": (22.0, 16.0)}  # (ceiling, assumed) km/h
        speed_fixup = 1.0
        if mode in _SPEED_FIXUPS and duration_min > 0:
            ceiling_kmh, assumed_kmh = _SPEED_FIXUPS[mode]
            implied_kmh = distance_km / (duration_min / 60.0)
            if implied_kmh > ceiling_kmh:
                fixed = round(distance_km / assumed_kmh * 60.0, 1)
                speed_fixup = fixed / duration_min if duration_min else 1.0
                duration_min = fixed
        # Per-leg figures, so the UI can show "12 min to stop 2, 9 min on to
        # stop 3" the way Google does rather than only a grand total.
        legs = [
            RouteLeg(duration_min=round(leg.get("duration", 0) / 60.0 * speed_fixup, 1),
                     distance_km=round(leg.get("distance", 0) / 1000.0, 1))
            for leg in entry.get("legs", [])
        ]
        instructions: list[RouteStep] = []
        for leg in entry.get("legs", []):
            for step in leg.get("steps", []):
                maneuver = step.get("maneuver", {})
                where = maneuver.get("location") or []
                # OSRM: "the very first [intersection] belonging to the
                # StepManeuver" — its lanes are the lane layout for
                # executing THIS step's maneuver.
                first_intersection = (step.get("intersections") or [{}])[0]
                lanes = [
                    LaneInfo(indications=lane.get("indications", []),
                             valid=bool(lane.get("valid")))
                    for lane in first_intersection.get("lanes") or []
                ]
                instructions.append(RouteStep(
                    type=maneuver.get("type", ""),
                    modifier=maneuver.get("modifier", "") or "",
                    exit=maneuver.get("exit"),
                    name=step.get("name", "") or "",
                    ref=step.get("ref", "") or "",
                    distance_m=step.get("distance", 0.0),
                    duration_s=step.get("duration", 0.0),
                    location=LatLon(lat=where[1], lon=where[0]) if len(where) == 2 else None,
                    lanes=lanes,
                ))
        return MapRoute(
            id=route_id,
            from_location=from_location,
            to_location=to_location,
            mode=mode if mode in ("car", "truck", "walk", "bike") else "car",
            duration_min=duration_min,
            distance_km=distance_km,
            geometry=geometry,
            legs=legs,
            steps=instructions,
            stops=stops,
        )

    _AVOID_CLASSES = ("toll", "motorway", "ferry")

    async def supported_avoid(self) -> list[str]:
        """Which exclude classes this backend really honours.

        Probed once and remembered: the answer depends on how the server was
        compiled, not on the request, and guessing wrong means quietly giving
        someone a motorway route they asked to avoid.
        """
        if getattr(self, "_avoid_cache", None) is not None:
            return self._avoid_cache
        supported: list[str] = []
        probe = "8.4037,49.0069;8.4600,49.0100"
        async with httpx.AsyncClient(transport=self.transport) as client:
            for name in self._AVOID_CLASSES:
                try:
                    response = await client.get(
                        f"{self.base_url}/route/v1/driving/{probe}?exclude={name}", timeout=15.0
                    )
                    if response.json().get("code") == "Ok":
                        supported.append(name)
                except (httpx.HTTPError, ValueError):
                    break
        self._avoid_cache = supported
        return supported

    async def isochrone(
        self, center: LatLon, *, mode: str = "car", contours: list[ContourSpec]
    ) -> IsochroneResult:
        """Not supported: OSRM has no isochrone service.

        `/routing/capabilities` reports `isochrone: false` for this backend
        so the frontend gates the feature before ever calling this.
        """
        raise NotImplementedError("OSRM backend does not support isochrones; use Valhalla")

    async def matrix(
        self,
        points: list[LatLon],
        *,
        mode: str = "car",
        sources: list[int] | None = None,
        targets: list[int] | None = None,
    ) -> list[list[float]]:
        """Travel times in minutes (OSRM table service), optionally asymmetric.

        `sources`/`targets` become the table service's own `sources` /
        `destinations` parameters — "String of `{index};{index}[;{index}
        ...]` or `all` (default)", indices into the coordinate list, and the
        response is row-major over them: "`durations[i][j]` gives the travel
        time from the i-th source to the j-th destination" (OSRM API docs,
        Table service). So a one-to-many ask (`sources=[0]`) costs one row,
        not the full NxN.
        """
        profile = _PROFILE_BY_MODE.get(mode, "driving")
        coords = ";".join(f"{p.lon},{p.lat}" for p in points)
        query = "annotations=duration"
        if sources is not None:
            query += "&sources=" + ";".join(str(i) for i in sources)
        if targets is not None:
            query += "&destinations=" + ";".join(str(i) for i in targets)
        url = f"{self.base_url}/table/v1/{profile}/{coords}?{query}"
        async with httpx.AsyncClient(transport=self.transport) as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            body = response.json()
        if body.get("code") != "Ok" or "durations" not in body:
            raise ValueError(f"OSRM table failed: {body.get('code')}")
        return [
            [round((cell or 0) / 60.0, 1) for cell in row]
            for row in body["durations"]
        ]

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
        """TSP order + route via OSRM's trip service.

        `GET /trip/v1/{profile}/{coords}` — parameters per the OSRM API
        docs (Trip service): `roundtrip` (default true) closes the loop
        back to the start; `source=first` / `destination=last` pin the
        endpoints (`any`, the default, lets the solver pick). The response
        `waypoints` array comes back "representing all waypoints in input
        order", each carrying `waypoint_index` — "Index of the point in the
        trip" — so the visiting order is recovered by inverting that
        mapping: `order[waypoint_index] = input_index`. The `trips[0]`
        entry is a regular Route object, mapped through the same
        `_map_route` the route service uses.
        """
        if len(stops) < 3 and not roundtrip and keep_endpoints:
            # Two PINNED stops have nothing to optimize; answer without a
            # backend round trip so the caller gets a consistent shape.
            # (With free endpoints even two stops may come back reversed,
            # so that case still goes to the solver.)
            route = await self.route(
                stops[0], stops[-1], route_id=route_id, mode=mode,
                from_location=from_location, to_location=to_location, steps=steps,
            )
            return OptimizedRoute(order=list(range(len(stops))), route=route)
        profile = _PROFILE_BY_MODE.get(mode, "driving")
        coords = ";".join(f"{p.lon},{p.lat}" for p in stops)
        query = "overview=full&geometries=geojson"
        if roundtrip:
            # A loop has no distinct end; only the start can be pinned.
            query += "&roundtrip=true"
            if keep_endpoints:
                query += "&source=first"
        else:
            query += "&roundtrip=false"
            if keep_endpoints:
                query += "&source=first&destination=last"
        if steps:
            query += "&steps=true"
        url = f"{self.base_url}/trip/v1/{profile}/{coords}?{query}"
        async with httpx.AsyncClient(transport=self.transport) as client:
            response = await client.get(url, timeout=60.0)
            response.raise_for_status()
            body = response.json()
        if body.get("code") != "Ok" or not body.get("trips"):
            raise ValueError(f"OSRM trip failed: {body.get('code')}")
        waypoints = body.get("waypoints", [])
        if len(waypoints) != len(stops):
            raise ValueError(
                f"OSRM trip answered {len(waypoints)} waypoints for {len(stops)} stops")
        order: list[int] = [0] * len(stops)
        for input_index, waypoint in enumerate(waypoints):
            order[waypoint["waypoint_index"]] = input_index
        visited = [stops[i] for i in order]
        route = self._map_route(
            body["trips"][0], route_id=route_id, from_location=from_location,
            to_location=to_location, mode=mode, stops=visited,
        )
        return OptimizedRoute(order=order, route=route)
