"""Live trips: computed routes kept alive between MCP tool calls.

The owner's iterative-planning case — "calculate a route, keep it alive, so
we can easily insert stops, add new locations" — needs mutable state the
stateless `route` tool deliberately does not have. A `TripStore` holds
`TripState` objects (stops + options + the latest computed `MapRoute`) so a
mutation is "edit the stop list, recompute, return fresh" instead of the
client re-sending geometry it may no longer have.

Memory discipline ("up to a given memory limit"): the store is bounded BOTH
by trip count and by estimated bytes (geometry dominates — the estimate is
per-coordinate/per-step, see `estimate_trip_bytes`). Eviction is LRU by
last touch; every read or mutation refreshes the TTL. An evicted or expired
id yields a `KeyError` whose message tells the model to recreate the trip —
state here is a cache of convenience, never the only copy of anything.

State lives in this process: over stdio that is one MCP client's own server
(per-session, exactly the Cowork case); over HTTP it is the shared dev
server instance, so trips are visible across clients — acceptable and
documented in docs/mcp.md.
"""

import time
import uuid

from ..models.map_route import MapRoute
from ..models.trip_state import TripState

# Caps, surfaced verbatim in the MCP tool descriptions.
TRIP_STORE_MAX_TRIPS = 24
TRIP_STORE_MAX_BYTES = 16 * 1024 * 1024
TRIP_TTL_S = 3600.0


def estimate_trip_bytes(route: MapRoute) -> int:
    """Rough in-memory weight of a computed result.

    Geometry dominates; a coordinate pair with float objects and list slots
    weighs on the order of 100 bytes, a step (strings, lanes) several times
    that. Precision does not matter — the cap only has to stop unbounded
    growth, not do accounting.
    """
    total = 0
    for candidate in (route, *route.alternates):
        total += 100 * len(candidate.geometry)
        total += 400 * len(candidate.steps)
        total += 200 * len(candidate.via_places)
        total += 300
    return total


class TripStore:
    """Bounded, LRU-evicting, TTL-swept container for live trips."""

    def __init__(
        self,
        *,
        max_trips: int = TRIP_STORE_MAX_TRIPS,
        max_bytes: int = TRIP_STORE_MAX_BYTES,
        ttl_s: float = TRIP_TTL_S,
    ):
        self.max_trips = max_trips
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self._trips: dict[str, TripState] = {}

    def _sweep(self, now: float) -> None:
        for trip_id in [t.id for t in self._trips.values()
                        if t.touched_at + self.ttl_s < now]:
            del self._trips[trip_id]

    def _evict(self) -> None:
        """LRU by last touch until both caps hold again."""
        while self._trips and (
            len(self._trips) > self.max_trips
            or sum(t.size_bytes for t in self._trips.values()) > self.max_bytes
        ):
            oldest = min(self._trips.values(), key=lambda t: t.touched_at)
            del self._trips[oldest.id]

    def mint_id(self) -> str:
        return "trip-" + uuid.uuid4().hex[:12]

    def put(self, trip: TripState) -> None:
        now = time.time()
        self._sweep(now)
        trip.touched_at = now
        if not trip.created_at:
            trip.created_at = now
        trip.size_bytes = estimate_trip_bytes(trip.route)
        self._trips[trip.id] = trip
        self._evict()

    def get(self, trip_id: str) -> TripState:
        """Read + touch. Raises KeyError with recovery guidance."""
        now = time.time()
        self._sweep(now)
        trip = self._trips.get(trip_id)
        if trip is None:
            raise KeyError(
                f"unknown trip '{trip_id}' — it expired ({int(self.ttl_s / 60)} min idle) "
                f"or was evicted (store keeps at most {self.max_trips} trips / "
                f"{self.max_bytes // (1024 * 1024)} MB, least-recently-used first). "
                "Recreate it with create_trip.")
        trip.touched_at = now
        return trip

    def peek(self, trip_id: str) -> TripState | None:
        """Read WITHOUT touching: None instead of KeyError, idle clock kept.

        The trip-bound session machinery polls through this a few times per
        interval while a browser tab is open — a read that refreshed the TTL
        would keep every watched trip alive forever, quietly repealing the
        idle-expiry rule for exactly the trips that hold the most memory.
        """
        now = time.time()
        self._sweep(now)
        return self._trips.get(trip_id)

    def all(self) -> list[TripState]:
        self._sweep(time.time())
        return sorted(self._trips.values(), key=lambda t: -t.touched_at)
