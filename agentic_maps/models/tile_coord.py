import math

from pydantic import BaseModel, Field

_EARTH_RADIUS_M = 6378137.0
_WORLD_M = 2.0 * math.pi * _EARTH_RADIUS_M


class TileCoord(BaseModel):
    """A slippy-map tile address (XYZ scheme, web mercator)."""

    z: int = Field(ge=0, le=22)
    x: int = Field(ge=0)
    y: int = Field(ge=0)

    model_config = {"frozen": True}

    @classmethod
    def at(cls, lat: float, lon: float, z: int) -> "TileCoord":
        n = 1 << z
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return cls(z=z, x=min(max(x, 0), n - 1), y=min(max(y, 0), n - 1))

    def bbox_deg(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in WGS84 degrees."""
        n = 1 << self.z
        west = self.x / n * 360.0 - 180.0
        east = (self.x + 1) / n * 360.0 - 180.0
        north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * self.y / n))))
        south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (self.y + 1) / n))))
        return (west, south, east, north)

    def center_latlon(self) -> tuple[float, float]:
        """(lat, lon) of the tile center — used for coverage routing."""
        n = 1 << self.z
        lon = (self.x + 0.5) / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (self.y + 0.5) / n))))
        return (lat, lon)

    def bbox_3857(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) in EPSG:3857 meters — WMS GetMap bbox."""
        n = 1 << self.z
        tile_m = _WORLD_M / n
        west = self.x * tile_m - _WORLD_M / 2.0
        north = _WORLD_M / 2.0 - self.y * tile_m
        return (west, north - tile_m, west + tile_m, north)
