from pydantic import BaseModel

from .isochrone_ring import IsochroneRing
from .lat_lon import LatLon


class IsochroneResult(BaseModel):
    """Reachable-area contours around one center point.

    The typed, backend-agnostic answer `RoutingBackend.isochrone()` returns.
    The REST layer turns this into an actual GeoJSON FeatureCollection on the
    wire (`rest/maps_api.py`'s `/isochrone`) — MapLibre wants a source it can
    add directly, not a pydantic model — but keeping a typed shape here means
    the routing backend itself stays testable without asserting on raw GeoJSON
    dict shapes.
    """

    center: LatLon
    mode: str
    rings: list[IsochroneRing]
