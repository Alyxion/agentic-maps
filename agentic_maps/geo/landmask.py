"""Per-tile land/ocean masks from the Natural Earth country polygons.

The ocean-blended world source (`blue-marble-plus` in `sources/presets.py`)
needs to know, per Web-Mercator tile, which pixels are land and which are
ocean. Chroma-keying the imagery would be fragile exactly where it matters
(bright shallow water, dark coniferous coast), so the mask is rasterized from
the admin-0 polygons already shipped in `var/geo/countries.geojson` — vector
data, deterministic, and the same source the border overlay draws, so the mask
can never disagree with the borders the user sees.

Rendering: polygons are projected to world-normalized Web-Mercator once at
load, then each tile draws its intersecting polygons at 4x supersampling,
downsamples (which antialiases the coastline), and applies a small Gaussian
blur. The result is a soft ~1-2 px transition band, so the two imagery layers
blend cleanly along the coast instead of meeting at a hard stair-stepped edge.

Union semantics matter: a polygon's holes must only stay ocean while no OTHER
polygon fills them. Lesotho lives inside a hole of South Africa's polygon —
punching all holes after drawing all outers would sink it. Hole-free polygons
(the vast majority) are drawn straight onto the shared canvas; polygons with
holes are drawn on a scratch tile and merged with a lighter() union.

Lakes are deliberately land here: the admin-0 polygons carry no inland-water
holes, so lake pixels keep the land layer's imagery — which is what a blend
that only replaces *ocean* pixels wants.
"""

import json
import math
from pathlib import Path

# Web-Mercator's latitude limit; the tile grid does not exist beyond it.
_MERC_LAT_LIMIT = 85.05112878

# Rasterize at 4x and downsample: the LANCZOS reduction antialiases the
# polygon edge to a ~1 px ramp before the feather blur widens it.
_SUPERSAMPLE = 4


def _project(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 -> world-normalized Web-Mercator, both axes in [0, 1]."""
    x = (lon + 180.0) / 360.0
    clamped = max(-_MERC_LAT_LIMIT, min(_MERC_LAT_LIMIT, lat))
    radians = math.radians(clamped)
    y = (1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi) / 2.0
    return x, y


class LandMask:
    """Rasterizes land polygons into per-tile alpha masks (255 = land)."""

    def __init__(
        self,
        geojson_path: Path | None = None,
        *,
        polygons: list[list[list[tuple[float, float]]]] | None = None,
    ):
        """Load from a (Multi)Polygon GeoJSON, or take polygons directly.

        `polygons` (tests, synthetic coastlines): a list of polygons, each a
        list of lon/lat rings — first ring the outer boundary, the rest holes.
        """
        self._path = geojson_path
        # Each entry: (bbox in world coords, projected rings, has_holes).
        self._polygons: list[tuple[tuple[float, float, float, float],
                                   list[list[tuple[float, float]]], bool]] | None = None
        if polygons is not None:
            self._polygons = [p for p in map(self._prepare, polygons) if p is not None]

    @property
    def available(self) -> bool:
        if self._polygons is not None:
            return True
        return self._path is not None and self._path.exists()

    def _prepare(self, rings: list[list[tuple[float, float]]]):
        projected = [
            [_project(lon, lat) for lon, lat in ring]
            for ring in rings
            if len(ring) >= 3
        ]
        if not projected or len(projected[0]) < 3:
            return None
        xs = [x for ring in projected for x, _ in ring]
        ys = [y for ring in projected for _, y in ring]
        return (min(xs), min(ys), max(xs), max(ys)), projected, len(projected) > 1

    def _load(self) -> list:
        if self._polygons is None:
            collection = json.loads(Path(self._path).read_text())  # type: ignore[arg-type]
            prepared = []
            for feature in collection.get("features", []):
                geometry = feature.get("geometry") or {}
                kind = geometry.get("type")
                if kind == "Polygon":
                    parts = [geometry["coordinates"]]
                elif kind == "MultiPolygon":
                    parts = geometry["coordinates"]
                else:
                    continue
                for rings in parts:
                    entry = self._prepare(rings)
                    if entry is not None:
                        prepared.append(entry)
            self._polygons = prepared
        return self._polygons

    def tile_mask(self, z: int, x: int, y: int, *, size: int = 256, feather_px: float = 1.0):
        """The land mask for one tile: mode "L", 255 land, 0 ocean, soft edge.

        `feather_px` is the Gaussian radius applied at final resolution, on
        top of the ~1 px antialiasing the supersampled rasterization already
        gives — together the ~1-2 px coastline transition the blend wants.
        """
        from PIL import Image, ImageChops, ImageDraw

        n = 1 << z
        # Candidate selection pads by the feather reach so a polygon whose
        # body ends just outside this tile still contributes its soft edge.
        pad = (feather_px + 2.0) / (size * n)
        west, north = x / n - pad, y / n - pad
        east, south = (x + 1) / n + pad, (y + 1) / n + pad

        raster = size * _SUPERSAMPLE
        scale = raster * n
        canvas = Image.new("L", (raster, raster), 0)
        draw = ImageDraw.Draw(canvas)

        def to_pixels(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
            return [((wx - x / n) * scale, (wy - y / n) * scale) for wx, wy in ring]

        for bbox, rings, has_holes in self._load():
            if bbox[0] > east or bbox[2] < west or bbox[1] > south or bbox[3] < north:
                continue
            if not has_holes:
                draw.polygon(to_pixels(rings[0]), fill=255)
                continue
            # Holes must not punch through OTHER polygons (Lesotho sits in a
            # hole of South Africa's): rasterize holed polygons separately
            # and union them in.
            scratch = Image.new("L", (raster, raster), 0)
            scratch_draw = ImageDraw.Draw(scratch)
            scratch_draw.polygon(to_pixels(rings[0]), fill=255)
            for hole in rings[1:]:
                scratch_draw.polygon(to_pixels(hole), fill=0)
            canvas = ImageChops.lighter(canvas, scratch)
            draw = ImageDraw.Draw(canvas)

        mask = canvas.resize((size, size), Image.LANCZOS)
        if feather_px > 0:
            from PIL import ImageFilter

            mask = mask.filter(ImageFilter.GaussianBlur(feather_px))
        return mask
