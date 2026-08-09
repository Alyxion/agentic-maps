from pydantic import BaseModel

from .bbox_deg import BBoxDeg
from .provision_layer_estimate import ProvisionLayerEstimate


class ProvisionEstimate(BaseModel):
    """The full cost forecast for a provisioning request — what the MCP tool
    shows BEFORE anything is downloaded (the confirm-size contract)."""

    region: str
    bbox: BBoxDeg
    layers: list[ProvisionLayerEstimate]
    total_bytes: int
    total_display: str
    warnings: list[str] = []
