from pydantic import BaseModel

from .map_route import MapRoute


class OptimizedRoute(BaseModel):
    """A TSP-optimized visiting order plus the route driven in that order.

    `order[k]` is the index into the ORIGINAL stop list of the k-th stop
    actually visited — `order == [0, 1, ..., n-1]` means the input order was
    already optimal. The route's own `stops` are already reordered to match,
    so a consumer that only draws the line needs nothing but `route`; `order`
    exists for callers that keep their own stop metadata (labels, addresses)
    and need to permute it the same way.
    """

    order: list[int]
    route: MapRoute
