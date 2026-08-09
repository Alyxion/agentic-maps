from pydantic import BaseModel


class ProvisionLayerEstimate(BaseModel):
    """Size forecast for one layer of a provisioning request."""

    layer: str
    # 0 for layers that are not tile-shaped (routing PBF, vector extract).
    tiles: int = 0
    bytes_estimate: int
    # Human line ready to print: "Germany aerial z13-15: ~15 GB (1.2M tiles)".
    display: str
    note: str = ""
