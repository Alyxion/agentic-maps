from pydantic import BaseModel

from .lat_lon import LatLon


class IsochroneRing(BaseModel):
    """One contour ring from an isochrone/isodistance query.

    A single contour value ("15 min", "10 km") is not always one shape — a
    river or a motorway can split reachability into several disjoint blobs,
    each landing here as its own ring sharing the same `minutes`/`km` and
    `color`. Rings come straight from Valhalla's own contour geometry, in
    order from the outermost (largest) contour in: drawing them back-to-front
    layers correctly without needing polygon subtraction.
    """

    # Exactly one of these is set, matching the backend's own "one metric per
    # contour" rule — a ring that was both a time and a distance boundary
    # would not correspond to a single line on the ground.
    minutes: float | None = None
    km: float | None = None
    color: str = "#3388ff"
    polygon: list[LatLon]
