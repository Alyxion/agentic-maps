"""Which major cities does a route pass? — the "via Pforzheim" engine.

Two candidate routes to the same destination differ by *where they go*, not
by their numbers; a person says "via Pforzheim" vs "via Heilbronn". This
matches the offline populated-places index (`CityIndex`, ~7300 Natural Earth
cities) against a route geometry and answers with a PRIORITY-ordered list of
`ViaPlace` — consumers truncate by importance (a summary row shows 1-2, a
detail header a few), and `along_km` lets them re-sort geographically.

Kept framework-free and cheap enough to run on every routed candidate:

- the geometry is pre-simplified to at most `SIMPLIFY_TO_POINTS` vertices
  (uniform stride — corridor membership at ±7 km does not care about
  sub-kilometre wiggles);
- a coarse spatial grid over the simplified line rejects almost every city
  in O(1) before any segment distance is computed (a diagonal route's
  bounding box contains far more cities than its corridor does);
- only the survivors get the exact point-to-segment scan.

Score formula (documented here, implemented in `_rank`):

    score = pop_term + status_boost + spacing_bonus
    pop_term      = log10(population + 1) / 7        # 0..~1 up to a megacity
    status_boost  = 0.60 for a national capital (a prime landmark at
                    any size), 0.10 for a state/province capital (tips
                    near-ties only — it must not beat a 6x bigger city), else 0
    spacing_bonus = +0.25 while the place lies >= SPACING_KM along the route
                    from every already-ranked place (greedy, best first)

The spacing bonus is what keeps two adjacent suburbs from both ranking: once
Pforzheim is taken, Pforzheim's neighbour loses the bonus and a distant
distinct city overtakes it.
"""

import math

from ..models.city_place import CityPlace
from ..models.lat_lon import LatLon
from ..models.via_place import ViaPlace

# A city "on the route": within this distance of the polyline. ~7 km covers
# a city whose centre point sits beside the motorway that carries the route.
CORRIDOR_KM = 7.0
# Places this close to start or end are where the trip IS, not what it goes
# via — Stuttgart is not "via Stuttgart" on a trip leaving Stuttgart.
ENDPOINT_EXCLUDE_KM = 10.0
# Geometry budget for the corridor test (see module docstring).
SIMPLIFY_TO_POINTS = 200
# Greedy spacing rule (see score formula above). Tuned for the dense
# GeoNames index: at 25 km, Pforzheim (23 km along-route from Karlsruhe on
# the A8) counted as Karlsruhe's neighbour and lost its bonus to a 27k town
# — 20 km still suppresses genuine suburbs (within ~10 km) while letting
# distinct mid-size cities on the same motorway both rank.
SPACING_KM = 20.0
SPACING_BONUS = 0.25
# Longest list any consumer needs (detail headers show ~4).
VIA_PLACES_MAX = 8
# Grid cell edge for the coarse reject, in degrees. Must exceed the corridor
# radius (7 km ≈ 0.063°) so a 3x3 neighbourhood is guaranteed to contain
# every cell within corridor distance of a route vertex.
_CELL_DEG = 0.15

_KM_PER_DEG = 111.32


def _simplify(geometry: list[LatLon], budget: int) -> list[LatLon]:
    """Uniform-stride thinning to at most `budget` points, both ends kept."""
    if len(geometry) <= budget:
        return geometry
    stride = (len(geometry) - 1) / (budget - 1)
    picked = [geometry[round(index * stride)] for index in range(budget - 1)]
    picked.append(geometry[-1])
    return picked


def _grid_cells(points: list[LatLon]) -> set[tuple[int, int]]:
    """Cells touched by the polyline, segments sampled densely enough that
    no cell along the line is skipped even where vertices are sparse."""
    cells: set[tuple[int, int]] = set()
    for a, b in zip(points, points[1:]):
        steps = 1 + int(max(abs(b.lat - a.lat), abs(b.lon - a.lon)) / (_CELL_DEG / 2))
        for i in range(steps + 1):
            t = i / steps
            lat = a.lat + (b.lat - a.lat) * t
            lon = a.lon + (b.lon - a.lon) * t
            cells.add((int(lat // _CELL_DEG), int(lon // _CELL_DEG)))
    return cells


def bucket_places(places: list[CityPlace]) -> dict[tuple[int, int], list[CityPlace]]:
    """Grid cell → the places inside it, for the coarse reject inverted.

    Scanning every place per route was fine at Natural Earth scale (~7,300);
    the GeoNames index carries ~32k, and even an O(1)-per-place reject is
    32k dict probes on every routed candidate. Bucketing once (cached by the
    index, cells sized `_CELL_DEG` to match `_grid_cells`) turns the
    candidate harvest into "walk the ~200 cells the route touches" instead.
    """
    buckets: dict[tuple[int, int], list[CityPlace]] = {}
    for place in places:
        cell = (int(place.lat // _CELL_DEG), int(place.lon // _CELL_DEG))
        buckets.setdefault(cell, []).append(place)
    return buckets


def _rank(candidates: list[tuple[float, ViaPlace]]) -> list[ViaPlace]:
    """Greedy best-first ordering with the spacing bonus (see formula)."""
    remaining = list(candidates)
    ranked: list[ViaPlace] = []
    taken_along: list[float] = []
    while remaining and len(ranked) < VIA_PLACES_MAX:
        best_index, best_score = 0, -math.inf
        for index, (base, place) in enumerate(remaining):
            spaced = all(abs(place.along_km - at) >= SPACING_KM for at in taken_along)
            score = base + (SPACING_BONUS if spaced else 0.0)
            if score > best_score:
                best_index, best_score = index, score
        _, place = remaining.pop(best_index)
        place.score = round(best_score, 3)
        ranked.append(place)
        taken_along.append(place.along_km)
    return ranked


def find_via_places(
    geometry: list[LatLon],
    places: list[CityPlace],
    *,
    buckets: dict[tuple[int, int], list[CityPlace]] | None = None,
) -> list[ViaPlace]:
    """Cities the route passes, priority-ordered (see module docstring).

    `buckets` (from `bucket_places`, cached by the caller's index) inverts
    the coarse reject: instead of probing the grid once per place, only the
    places bucketed in the route's cells are considered at all. Same result,
    required at GeoNames index scale.
    """
    if len(geometry) < 2 or not (places or buckets):
        return []
    line = _simplify(geometry, SIMPLIFY_TO_POINTS)
    cells = _grid_cells(line)

    if buckets is None:
        # Per-place probe: a place survives if any cell of its 3x3
        # neighbourhood is a route cell.
        survivors = [
            place for place in places
            if any(
                (int(place.lat // _CELL_DEG) + dr, int(place.lon // _CELL_DEG) + dc) in cells
                for dr in (-1, 0, 1) for dc in (-1, 0, 1)
            )
        ]
    else:
        # Inverted: harvest the buckets of every route cell's 3x3
        # neighbourhood — Chebyshev distance is symmetric, so this is the
        # same membership test from the other side.
        seen: set[int] = set()
        survivors = []
        for row, col in cells:
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    for place in buckets.get((row + dr, col + dc), ()):
                        if id(place) not in seen:
                            seen.add(id(place))
                            survivors.append(place)
    if not survivors:
        return []

    # Per-segment precomputation in a local equirectangular frame: x is
    # longitude scaled by cos(mean route latitude) — one factor for the whole
    # route keeps the frame consistent, and route extents are far too small
    # for the residual error to move a city across the 7 km threshold.
    cos_lat = math.cos(math.radians(sum(p.lat for p in line) / len(line)))
    xs = [p.lon * cos_lat for p in line]
    ys = [p.lat for p in line]
    seg_dx = [xs[i + 1] - xs[i] for i in range(len(line) - 1)]
    seg_dy = [ys[i + 1] - ys[i] for i in range(len(line) - 1)]
    seg_len2 = [dx * dx + dy * dy for dx, dy in zip(seg_dx, seg_dy)]
    # Cumulative km from the start at each vertex — along_km lives on this.
    cum_km = [0.0]
    for dx, dy in zip(seg_dx, seg_dy):
        cum_km.append(cum_km[-1] + math.hypot(dx, dy) * _KM_PER_DEG)

    start, end = geometry[0], geometry[-1]
    corridor_deg2 = (CORRIDOR_KM / _KM_PER_DEG) ** 2

    def _near_endpoint(place: CityPlace) -> bool:
        for anchor in (start, end):
            dx = (place.lon - anchor.lon) * cos_lat
            dy = place.lat - anchor.lat
            if math.hypot(dx, dy) * _KM_PER_DEG < ENDPOINT_EXCLUDE_KM:
                return True
        return False

    candidates: list[tuple[float, ViaPlace]] = []
    for place in survivors:
        px = place.lon * cos_lat
        py = place.lat
        best_d2, best_along = math.inf, 0.0
        for i in range(len(line) - 1):
            wx, wy = px - xs[i], py - ys[i]
            t = 0.0 if seg_len2[i] == 0 else max(0.0, min(1.0, (wx * seg_dx[i] + wy * seg_dy[i]) / seg_len2[i]))
            dx = wx - t * seg_dx[i]
            dy = wy - t * seg_dy[i]
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_along = cum_km[i] + t * (cum_km[i + 1] - cum_km[i])
        if best_d2 > corridor_deg2 or _near_endpoint(place):
            continue
        base = math.log10(place.population + 1) / 7.0
        if place.capital:
            base += 0.60
        elif place.state_capital:
            base += 0.10
        candidates.append((base, ViaPlace(
            name=place.name, lat=place.lat, lon=place.lon,
            population=place.population, along_km=round(best_along, 1),
        )))
    return _rank(candidates)
