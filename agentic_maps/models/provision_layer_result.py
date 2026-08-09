from pydantic import BaseModel


class ProvisionLayerResult(BaseModel):
    """Outcome/progress of one layer inside a provisioning job."""

    layer: str
    # pending | running | done | failed | cancelled | skipped
    status: str = "pending"
    tiles_done: int = 0
    tiles_total: int = 0
    # Failed tile fetches (upstream errors) — counted, never fatal per tile.
    tiles_failed: int = 0
    bytes_done: int = 0
    detail: str = ""
    # Routing layer without Docker: the exact command to run next, so the
    # job never fails just because Docker is not up.
    next_command: str = ""
