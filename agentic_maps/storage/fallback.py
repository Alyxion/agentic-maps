"""Graceful degradation for raster tiles missing from an offline bundle.

The completeness contract is: every tile ever shown is prewarmed into the
bundle. If a hole exists anyway, we still never fetch online at presentation
time — instead we synthesize a stand-in:

1. parent fallback: walk up to `max_parent_levels` ancestors, crop the
   matching quadrant and upscale (temporarily lower resolution);
2. children merge: compose the up-to-4 child tiles one level down into one.

Requires Pillow (optional `fallback` extra); without it only exact hits serve.
"""

import io

from ..models.tile_coord import TileCoord
from .mbtiles import MBTilesBundle

_TILE_PX = 256


class FallbackTileResolver:
    def __init__(self, bundle: MBTilesBundle, *, max_parent_levels: int = 4):
        self.bundle = bundle
        self.max_parent_levels = max_parent_levels

    def resolve(self, coord: TileCoord) -> tuple[bytes, str] | None:
        """Returns (tile bytes, kind) where kind is exact|parent|children."""
        exact = self.bundle.get_tile(coord)
        if exact is not None:
            return (exact, "exact")
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            return None
        parent = self._from_parent(coord)
        if parent is not None:
            return (parent, "parent")
        merged = self._from_children(coord)
        if merged is not None:
            return (merged, "children")
        return None

    def _encode(self, image) -> bytes:
        out = io.BytesIO()
        # Mosaic tiles carry alpha where no state covers the ground; flattening
        # those to JPEG would paint the hole black instead of letting the
        # basemap through, so transparency outvotes the bundle's format.
        has_alpha = image.mode in ("RGBA", "LA") and image.getchannel("A").getextrema()[0] < 255
        if has_alpha or self.bundle.info().tile_format == "png":
            image.save(out, format="PNG")
        else:
            image.convert("RGB").save(out, format="JPEG", quality=82)
        return out.getvalue()

    def _from_parent(self, coord: TileCoord) -> bytes | None:
        from PIL import Image

        for levels_up in range(1, self.max_parent_levels + 1):
            if coord.z - levels_up < 0:
                break
            ancestor = TileCoord(z=coord.z - levels_up, x=coord.x >> levels_up, y=coord.y >> levels_up)
            data = self.bundle.get_tile(ancestor)
            if data is None:
                continue
            image = Image.open(io.BytesIO(data))
            # Quadrant of the ancestor that this tile occupies.
            span = _TILE_PX >> levels_up
            if span < 1:
                break
            offset_x = (coord.x - (ancestor.x << levels_up)) * span
            offset_y = (coord.y - (ancestor.y << levels_up)) * span
            crop = image.crop((offset_x, offset_y, offset_x + span, offset_y + span))
            return self._encode(crop.resize((_TILE_PX, _TILE_PX), Image.BILINEAR))
        return None

    def _from_children(self, coord: TileCoord) -> bytes | None:
        from PIL import Image

        children = [
            (dx, dy, self.bundle.get_tile(TileCoord(z=coord.z + 1, x=coord.x * 2 + dx, y=coord.y * 2 + dy)))
            for dx in (0, 1)
            for dy in (0, 1)
        ]
        if all(data is None for _, _, data in children):
            return None
        # Transparent canvas: a missing quadrant should show the basemap, not a
        # dark square.
        canvas = Image.new("RGBA", (_TILE_PX * 2, _TILE_PX * 2), (0, 0, 0, 0))
        for dx, dy, data in children:
            if data is not None:
                child = Image.open(io.BytesIO(data)).convert("RGBA")
                canvas.paste(child, (dx * _TILE_PX, dy * _TILE_PX), child)
        return self._encode(canvas.resize((_TILE_PX, _TILE_PX), Image.BILINEAR))
