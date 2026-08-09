from pydantic import BaseModel, Field

from .lat_lon import LatLon


class CameraPose(BaseModel):
    """A MapLibre camera position; the unit of the fly-through sequence."""

    center: LatLon
    zoom: float = Field(ge=0.0, le=22.0)
    bearing: float = 0.0
    pitch: float = Field(default=0.0, ge=0.0, le=85.0)
