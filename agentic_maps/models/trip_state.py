from typing import Literal

from pydantic import BaseModel

from .map_route import MapRoute
from .route_stop import RouteStop


class TripState(BaseModel):
    """One live trip the MCP server keeps alive between tool calls.

    The owner's iterative-planning case: compute once, then insert/remove/
    reorder stops or flip options and get the fresh result each time without
    resending geometry. Held in `mcp_server/trips.py`'s bounded store (LRU +
    byte cap); everything here is state, the routing result included, so a
    `get_trip` read needs no recompute.
    """

    id: str
    # Ordered; first = start, last = destination. Stops carry the structured
    # address (`RouteStop.address`) once enriched — cached here so repeated
    # page/view generations never re-hit the geocoder.
    stops: list[RouteStop]
    mode: Literal["car", "truck", "walk", "bike"] = "car"
    avoid: list[str] = []
    # How many alternates each recompute requests (honoured on 2-stop trips).
    alternates: int = 2
    # Bumped on every mutation that recomputed — a compact history marker.
    revision: int = 1
    # The latest computed result, alternates/via_places/steps included.
    route: MapRoute
    created_at: float = 0.0
    touched_at: float = 0.0
    # Rough in-memory weight (see trips.py `estimate_trip_bytes`) — what the
    # store's byte cap sums over.
    size_bytes: int = 0
