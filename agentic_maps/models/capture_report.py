from pydantic import BaseModel

from .sealed_route import SealedRoute


class CaptureReport(BaseModel):
    """What the authored choreography actually asked for, and nothing else.

    This is the empirical counterpart to `HarvestPlan`. The planner computes
    the tile set from a `MapSpec`; it cannot see a choreography written in a
    page's own JavaScript. So the pages are played instead, and every resource
    the map runtime requests is written down here.

    `keys` are request paths relative to the host (`/api/v1/maps/live/de-dop/
    14/8600/5300`). They are the exact set the sealed store will hold — a tile
    that no reachable state of the presentation displays is never recorded and
    therefore never shipped.
    """

    keys: list[str] = []
    routes: list[SealedRoute] = []
    # Which embeds were played, and in how many viewport variants each. The
    # variants are the screen-size margin: the same choreography on a wider
    # stage or a retina panel reveals tiles the authored box never touches.
    embeds: list[str] = []
    viewport_variants: int = 0
    # Requests that failed. A 404 raster tile is normal (sea, data gap — the
    # basemap shows through), so these are reported rather than raised.
    missing: list[str] = []
