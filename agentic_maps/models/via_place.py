from pydantic import BaseModel


class ViaPlace(BaseModel):
    """One major city a route passes — the "via Pforzheim" fact.

    Lists of these are PRIORITY-ordered (most important first, see
    `geo/via_places.py` for the score formula): any consumer truncates by
    importance — a summary row takes the first one or two, a detail header
    the first few. `along_km` keeps the geographic order available so a
    consumer can also re-sort the same list by position along the route.
    """

    name: str
    lat: float
    lon: float
    # Natural Earth `pop_max` — 0 where the source has none.
    population: int = 0
    # The importance score this list is ordered by (higher = earlier).
    # Comparable only within one route's list, not across routes.
    score: float = 0.0
    # Distance from the route start to the point where the route passes
    # closest to this place, measured along the route geometry.
    along_km: float = 0.0
