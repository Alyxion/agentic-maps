from typing import Literal

from pydantic import BaseModel

from .lat_lon import LatLon
from .route_leg import RouteLeg
from .route_step import RouteStep
from .via_place import ViaPlace


class MapRoute(BaseModel):
    """A precomputed route drawn on the map (e.g. HQ → airport).

    Computed once at authoring time (OSRM or GTFS-based); geometry and
    duration are embedded in the spec so published packages stay offline.
    """

    id: str
    from_location: str
    to_location: str
    # "transit" is reserved for a later GTFS-based provider (see
    # routing/osrm.py); no backend routes it yet. The other four are the
    # product's canonical travel-mode vocabulary (routing/base.py).
    mode: Literal["car", "truck", "walk", "bike", "transit"] = "car"
    duration_min: float
    distance_km: float
    geometry: list[LatLon]
    # Stop-to-stop sections; empty for a plain A→B route.
    legs: list[RouteLeg] = []
    # Turn-by-turn instructions, only fetched when asked for — they roughly
    # double the response size and an embedded map view does not need them.
    steps: list[RouteStep] = []
    # The stops the user actually named, in order, so the panel can redraw the
    # trip without re-geocoding.
    stops: list[LatLon] = []
    # Vivid brand blue (the navy+orange identity's azure): saturated enough
    # to pop over imagery — the old amber default washed out over
    # yellow-green farmland — and deliberately not Google's #1a73e8.
    color: str = "#2e6be6"
    # Progressive draw-in ("racing along the streets") when the route appears;
    # None renders instantly.
    animate_ms: int | None = None
    # Major cities the route passes, PRIORITY-ordered (most important first;
    # see geo/via_places.py for the score) — "über Pforzheim" instead of
    # "the longer one". Consumers truncate by importance: a summary label
    # takes the first 1-2, a detail header a few more; `along_km` allows
    # re-sorting geographically. Filled by the /route handler from the
    # offline city index; empty when the index is not on disk.
    via_places: list[ViaPlace] = []
    # Other paths between the same two points, requested via `alternates` on
    # /route (Valhalla only — see routing/valhalla.py). Each is a full route
    # in its own right (own geometry/duration/steps) so the frontend can
    # promote one to primary without a second request; each alternate's own
    # `alternates` stays empty to keep this from nesting.
    alternates: list["MapRoute"] = []
