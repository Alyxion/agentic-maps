from pydantic import BaseModel, Field

from .lat_lon import LatLon


class StreetWay(BaseModel):
    """One street segment as the basemap actually draws it.

    The geometry is the tile's own line, clipped to the requested box and
    joined across tile seams where the ends meet — so a caller measuring a
    kerb reads the same course the viewer sees, not an idealised centre line.
    """

    name: str = Field(default="", description="Street name as labelled, empty for unnamed ways.")
    kind: str = Field(default="", description="Road class from the basemap (residential, secondary, service, …).")
    geometry: list[LatLon] = Field(description="Course of the segment, in order.")
    length_m: float = Field(default=0.0, description="Length of the returned segment in metres.")
