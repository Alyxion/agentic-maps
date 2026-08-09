from pydantic import BaseModel, Field

from .bbox_deg import BBoxDeg


class ReachabilityGrid(BaseModel):
    """A target lattice for one-to-many reachability, generated server-side.

    `step_km` spaces the grid points; `cap` bounds how many the caller is
    willing to pay for — the request is refused (with the real count) when
    the lattice would exceed it, never silently thinned.
    """

    bbox: BBoxDeg
    step_km: float = Field(gt=0.05, le=500.0)
    cap: int = Field(default=400, ge=1, le=10000)
