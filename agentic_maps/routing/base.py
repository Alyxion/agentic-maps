"""The contract every routing backend implements.

Two backends exist: Valhalla (`valhalla.py`, primary — self-hosted, gives
alternates and isochrones) and OSRM (`osrm.py`, secondary/legacy — kept for
sites that already run an OSRM container, but cannot do isochrones and maps
`truck` onto its plain driving profile). `rest/maps_api.py` talks to whichever
one `MapsApi.router` holds through this interface only, so the REST layer and
the frontend never need to know which backend actually answered.

Canonical travel-mode vocabulary for the whole product: `car`, `truck`,
`walk`, `bike`. The REST API and the frontend only ever speak these four;
each backend privately maps them onto its own profile/costing names (OSRM:
driving/walking/cycling; Valhalla: auto/truck/pedestrian/bicycle).
"""

from typing import Literal, Protocol

from ..models.isochrone_result import IsochroneResult
from ..models.lat_lon import LatLon
from ..models.map_route import MapRoute
from ..models.optimized_route import OptimizedRoute

TravelMode = Literal["car", "truck", "walk", "bike"]

# One contour request: exactly one of `time_min` / `distance_km` is set,
# mirroring Valhalla's "one metric per contour" rule — a contour that tried to
# be both a time ring and a distance ring would not be a single line on the
# map, so the ambiguity is refused at the type rather than resolved by
# guessing which one the caller meant.
ContourSpec = dict[str, float]


class RoutingBackend(Protocol):
    """What `MapsApi` needs from a routing backend.

    A Protocol rather than an ABC: `OsrmRouter` and `ValhallaRouter` predate
    (and now conform to) this shape without inheriting from a common base,
    and a structural type lets a test double satisfy it with a plain object.
    """

    base_url: str

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

        Any requested alternates come back attached at `MapRoute.alternates`
        (each a full route in its own right, geometry and all) rather than as
        a separate return value — the frontend draws `route` as the
        highlighted line and `route.alternates` as the dimmed ones, and a
        spec that embeds the route embeds the alternates with it for free.
        """
        ...

    async def matrix(
        self,
        points: list[LatLon],
        *,
        mode: str = "car",
        sources: list[int] | None = None,
        targets: list[int] | None = None,
    ) -> list[list[float]]:
        """Travel times in minutes: matrix[i][j] = sources[i] to targets[j].

        `sources`/`targets` are indices into `points`; None means "all of
        them" on that axis, so the default call stays the historical
        all-pairs NxN. An asymmetric ask (`sources=[0]`, `targets=[1..n]`)
        is the one-to-many reachability primitive — both backends support
        it natively (OSRM `table` with `sources`/`destinations` index
        lists, Valhalla `sources_to_targets` with separate location
        arrays), so a 1×N question must never be paid for as N×N.
        """
        ...

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
        """Visit every stop once in the cheapest order (TSP) and route it.

        Returns the optimized visiting order (`order[k]` = index into
        `stops` of the k-th visited stop) plus the full recomputed route in
        that order. `keep_endpoints=True` pins `stops[0]` as the start and
        `stops[-1]` as the end and only reorders the middle; `roundtrip`
        returns to the start instead of ending at `stops[-1]`. Both
        backends solve this natively (OSRM `trip` service, Valhalla
        `optimized_route`) — see each implementation for which parameter
        combinations the backend genuinely honours; unsupported ones raise
        `ValueError` instead of silently degrading.
        """
        ...

    async def isochrone(
        self,
        center: LatLon,
        *,
        mode: str = "car",
        contours: list[ContourSpec],
    ) -> IsochroneResult:
        """Reachable-area contours around `center`.

        Each item in `contours` is `{"time_min": ...}` or `{"distance_km":
        ...}` — never both, matching the backend's own one-metric-per-contour
        rule. Raises `NotImplementedError` on a backend that cannot do this
        (OSRM has no isochrone service); `RoutingCapabilities.isochrone`
        tells the frontend which is which up front so it never has to find
        out by catching the error.
        """
        ...

    async def supported_avoid(self) -> list[str]:
        """Which of `toll` / `motorway` / `ferry` this backend really honours.

        Probed (or otherwise determined) once and remembered, the same
        contract `OsrmRouter.supported_avoid()` already has: a route that
        silently ignores "avoid motorways" is worse than one that admits it
        cannot.
        """
        ...
