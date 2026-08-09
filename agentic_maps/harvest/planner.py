"""Turn a MapSpec into the deduplicated tile pyramid it needs offline.

This is the "reasonable movements" completeness contract (docs/concept.md §5):
everything the camera can show during the authored fly-through must be in the
plan. Strategy (keeps bundles small instead of blanket-downloading a region):
- a shared overview pyramid over the padded union bbox of all locations;
- per location, viewport-sized tile sets from the overview zoom down to that
  location's detail zoom (covers the zoom-in/zoom-out phases of flyTo);
- per consecutive location pair, a corridor along the flight path at the
  zooms between the overview pyramid and the flight's apex zoom (covers the
  cruise phase of flyTo for nearby pairs; distant pairs cruise at overview
  zooms that are already covered).
"""

import math

from ..models.bbox_deg import BBoxDeg
from ..models.composite_source import CompositeSource
from ..models.harvest_plan import HarvestPlan
from ..models.lat_lon import LatLon
from ..models.map_spec import MapSpec
from ..models.tile_coord import TileCoord
from ..models.tile_source import TileSource

# Logical stage is 1920x1080; the margin buffers pans, fly paths and larger
# real screens (1.6 covers ~3000px-wide viewports at the same zoom; pass a
# higher margin when recording for 4K).
_STAGE_W_PX = 1920
_STAGE_H_PX = 1080
_DEFAULT_MARGIN = 1.6
# Above this many tiles per zoom level the overview pyramid stops descending
# and per-location viewports take over.
_OVERVIEW_LEVEL_CAP = 64


class HarvestPlanner:
    def __init__(self, source: TileSource | CompositeSource, *, margin: float = _DEFAULT_MARGIN):
        self.source = source
        self.margin = margin

    def plan(self, spec: MapSpec) -> HarvestPlan:
        tiles: set[TileCoord] = set()
        detail_zooms = [min(int(math.ceil(loc.camera.zoom)), self.source.max_zoom) for loc in spec.locations]
        if not detail_zooms:
            # `MapSpec.locations` may legitimately be empty — a plain map with
            # no choreography is what the map app starts from. Its plan is the
            # overview pyramid down to the overview camera's own zoom; without
            # even an overview there is genuinely nothing to plan, said
            # cleanly instead of `max()` blowing up on an empty sequence.
            if spec.overview is None:
                raise ValueError(
                    f"spec '{spec.id}' has no locations and no overview — nothing to plan"
                )
            detail_zooms = [min(int(math.ceil(spec.overview.zoom)), self.source.max_zoom)]
        union = self._union_bbox(spec)

        overview_z = self.source.min_zoom
        for z in range(self.source.min_zoom, max(detail_zooms) + 1):
            level = self._tiles_for_bbox(union, z)
            if len(level) > _OVERVIEW_LEVEL_CAP:
                break
            tiles.update(level)
            overview_z = z

        for loc, detail_z in zip(spec.locations, detail_zooms):
            for z in range(overview_z + 1, detail_z + 1):
                tiles.update(self._tiles_for_view(loc.camera.center, z))

        for a, b in zip(spec.locations, spec.locations[1:]):
            tiles.update(self._corridor_tiles(a.camera.center, b.camera.center, overview_z))

        ordered = sorted(tiles, key=lambda t: (t.z, t.x, t.y))
        return HarvestPlan(spec_id=spec.id, source_id=self.source.id, tiles=ordered)

    def _corridor_tiles(self, a: LatLon, b: LatLon, overview_z: int) -> set[TileCoord]:
        """Coverage for the cruise phase of a flyTo between two stops.

        The camera arcs out to an apex zoom that roughly fits both endpoints
        in the viewport, cruises, then dives. Endpoint pyramids cover the
        arc's vertical phases; this covers the horizontal phase at the zooms
        the overview pyramid didn't reach.
        """
        apex_z = min(self._fit_zoom(a, b), self.source.max_zoom)
        tiles: set[TileCoord] = set()
        for z in range(overview_z + 1, apex_z + 1):
            # Sample the straight path densely relative to the viewport size.
            meters_per_px = 156543.03392 * math.cos(math.radians((a.lat + b.lat) / 2)) / (1 << z)
            step_deg = _STAGE_W_PX * 0.5 * meters_per_px / 111320.0
            distance_deg = math.hypot(b.lon - a.lon, b.lat - a.lat)
            samples = max(int(distance_deg / step_deg) + 1, 2)
            for i in range(samples + 1):
                t = i / samples
                point = LatLon(lat=a.lat + (b.lat - a.lat) * t, lon=a.lon + (b.lon - a.lon) * t)
                tiles.update(self._tiles_for_view(point, z))
        return tiles

    def _fit_zoom(self, a: LatLon, b: LatLon) -> int:
        """Highest integer zoom at which both points fit the stage viewport."""
        width_deg = max(abs(a.lon - b.lon), 0.0005)
        height_deg = max(abs(a.lat - b.lat), 0.0005)
        fit_x = math.log2(_STAGE_W_PX * 360.0 / (256.0 * width_deg * self.margin))
        # Approximate mercator stretching with the cos factor at mid latitude.
        stretch = 1.0 / max(math.cos(math.radians((a.lat + b.lat) / 2)), 0.1)
        fit_y = math.log2(_STAGE_H_PX * 360.0 / (256.0 * height_deg * stretch * self.margin))
        return max(int(min(fit_x, fit_y)), 0)

    def _union_bbox(self, spec: MapSpec) -> BBoxDeg:
        centers = [loc.camera.center for loc in spec.locations]
        if spec.overview is not None:
            centers.append(spec.overview.center)
        eps = 0.0005
        box = BBoxDeg(
            west=min(c.lon for c in centers) - eps,
            south=min(c.lat for c in centers) - eps,
            east=max(c.lon for c in centers) + eps,
            north=max(c.lat for c in centers) + eps,
        )
        return box.padded(0.3)

    def _tiles_for_view(self, center: LatLon, z: int) -> list[TileCoord]:
        """Tiles covering a stage-sized viewport (plus margin) centered at `center`."""
        meters_per_px = 156543.03392 * math.cos(math.radians(center.lat)) / (1 << z)
        half_w_deg = _STAGE_W_PX * self.margin / 2 * meters_per_px / (111320.0 * math.cos(math.radians(center.lat)))
        half_h_deg = _STAGE_H_PX * self.margin / 2 * meters_per_px / 110540.0
        box = BBoxDeg(
            west=center.lon - half_w_deg,
            south=center.lat - half_h_deg,
            east=center.lon + half_w_deg,
            north=center.lat + half_h_deg,
        )
        return self._tiles_for_bbox(box, z)

    @staticmethod
    def _tiles_for_bbox(box: BBoxDeg, z: int) -> list[TileCoord]:
        nw = TileCoord.at(box.north, box.west, z)
        se = TileCoord.at(box.south, box.east, z)
        return [
            TileCoord(z=z, x=x, y=y)
            for x in range(nw.x, se.x + 1)
            for y in range(nw.y, se.y + 1)
        ]
