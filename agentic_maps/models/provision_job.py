from pydantic import BaseModel

from .provision_layer_result import ProvisionLayerResult
from .provision_request import ProvisionRequest


class ProvisionJob(BaseModel):
    """Persistent state of one region-provisioning job.

    Serialized as-is to JSON in the engine's state directory on every
    meaningful transition, so a restarted server can report "interrupted,
    resumable" instead of pretending the job never existed. Progress is
    honest counts (tiles done/total, bytes) — no ETA, because upstream tile
    servers do not owe us a steady rate.
    """

    id: str
    request: ProvisionRequest
    # pending | running | done | failed | cancelled | interrupted
    state: str = "pending"
    # Human phase line: "aerial z14: 1234/58000 tiles".
    phase: str = ""
    layers: list[ProvisionLayerResult] = []
    error: str = ""
    note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def tiles_done(self) -> int:
        return sum(layer.tiles_done for layer in self.layers)

    @property
    def tiles_total(self) -> int:
        return sum(layer.tiles_total for layer in self.layers)

    @property
    def bytes_done(self) -> int:
        return sum(layer.bytes_done for layer in self.layers)

    def percent(self) -> float:
        """Tile-weighted progress, 0-100. Byte-based for byte-shaped layers
        (a PBF download reports bytes as tiles_total=0) — the two are never
        mixed into one misleading figure; tile layers dominate when present."""
        if self.tiles_total:
            return round(100.0 * self.tiles_done / self.tiles_total, 1)
        totals = [layer for layer in self.layers if layer.bytes_done]
        return 0.0 if not totals or self.state != "done" else 100.0
