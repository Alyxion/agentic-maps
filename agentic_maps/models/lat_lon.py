from pydantic import BaseModel, Field


class LatLon(BaseModel):
    """A WGS84 coordinate."""

    lat: float = Field(ge=-85.06, le=85.06, description="Latitude (web-mercator range)")
    lon: float = Field(ge=-180.0, le=180.0)
