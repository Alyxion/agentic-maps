from pydantic import BaseModel

from .tile_coord import TileCoord

# Rough average of the 20cm DOP JPEG tiles observed in testing; used only for
# pre-download size estimates shown to the user.
_EST_TILE_BYTES = 45_000


class HarvestPlan(BaseModel):
    """The deduplicated tile pyramid a spec needs, computed before downloading."""

    spec_id: str
    source_id: str
    tiles: list[TileCoord]

    @property
    def tile_count(self) -> int:
        return len(self.tiles)

    @property
    def estimated_mb(self) -> float:
        return round(self.tile_count * _EST_TILE_BYTES / 1_000_000, 1)
