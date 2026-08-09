from pydantic import BaseModel

from .bbox_deg import BBoxDeg


class VectorBundleInfo(BaseModel):
    """Descriptive metadata of a PMTiles vector extract."""

    id: str
    min_zoom: int
    max_zoom: int
    bounds: BBoxDeg
    size_bytes: int
    attribution: str = "© OpenStreetMap"
