from typing import Literal

from pydantic import BaseModel, model_validator

from .lat_lon import LatLon


class MapHighlight(BaseModel):
    """A highlight decoration on the map.

    Kinds:
    - ``circle``: translucent labeled screen-space circle (exposé style);
      sized in px at the owning location's detail zoom, scales geographically
      with the camera and fades out far above the location.
    - ``ping``: sonar-style animated attention rings (draws the eye to one or
      many points — e.g. nearby public transport).
    - ``radius``: geographic circle of ``radius_m`` meters (true ground
      footprint, e.g. "500 m walking radius"); rendered as map geometry.
    - ``polygon``: arbitrary area outline+fill from ``polygon`` vertices
      (plots, districts, development areas).
    - ``line``: a WAY traced in colour from ``line`` vertices — a street, a
      frontage, a boundary run. Not a degenerate polygon: it is neither
      closed nor filled, and it exists so that an author can point at the
      road a plot fronts onto (and check that the plot actually sits flush
      with it) without faking a route.
    """

    at: LatLon
    label: str = ""
    kind: Literal["circle", "ping", "radius", "polygon", "line"] = "circle"
    radius_px: int = 90
    radius_m: float | None = None
    polygon: list[LatLon] = []
    line: list[LatLon] = []
    width: float | None = None
    color: str = "#ffffff"
    opacity: float = 0.35

    @model_validator(mode="after")
    def _kind_requirements(self) -> "MapHighlight":
        if self.kind == "radius" and not (self.radius_m and self.radius_m > 0):
            raise ValueError("radius highlight requires radius_m > 0")
        if self.kind == "polygon" and len(self.polygon) < 3:
            raise ValueError("polygon highlight requires >= 3 vertices")
        if self.kind == "line" and len(self.line) < 2:
            raise ValueError("line highlight requires >= 2 vertices")
        return self
