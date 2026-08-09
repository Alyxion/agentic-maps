from pydantic import BaseModel


class BundleInfo(BaseModel):
    """Descriptive metadata of an offline tile bundle (from MBTiles metadata)."""

    id: str
    source_id: str
    tile_format: str
    attribution: str
    license_name: str
    license_url: str
    tile_count: int
    size_bytes: int
