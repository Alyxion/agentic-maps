from typing import Literal

from pydantic import BaseModel, Field

from .bbox_deg import BBoxDeg


class TileSource(BaseModel):
    """A licensed tile origin we are allowed to harvest.

    Only sources whose license permits bulk download become presets; the
    attribution/license fields are mandatory and rendered on-map and stored in
    every bundle produced from the source.
    """

    id: str
    name: str
    kind: Literal["wms", "xyz"]
    # wms: base endpoint URL; xyz: template with {z}/{x}/{y} placeholders.
    url: str
    wms_layers: str | None = None
    wms_version: str = "1.3.0"
    tile_format: Literal["jpeg", "png"] = "jpeg"
    tile_size: int = 256
    min_zoom: int = Field(default=0, ge=0, le=22)
    max_zoom: int = Field(default=19, ge=0, le=22)
    attribution: str
    license_name: str
    license_url: str
    request_delay_ms: int = Field(default=0, ge=0, description="Politeness delay between requests")
    # WGS84 area this source can serve (state boundary bbox); None = unbounded.
    # Used by composite sources to route tiles to the right member.
    coverage: BBoxDeg | None = None
    # Some services honour TRANSPARENT only partially and fill the ground they
    # do not own with opaque pure white. In a federated mosaic that white would
    # cover the neighbouring state, so it gets keyed out as no-data. Costs a
    # few pixels on genuinely pure-white ground (fresh snow, bright roofs).
    white_is_nodata: bool = False
    # Ocean-blend compositing (harvest/harvester.py `_fetch_blend`): when set,
    # this source's own kind/url describe the LAND layer, and every ocean
    # pixel is taken from the source named here instead — separated by the
    # Natural Earth land-polygon mask (geo/landmask.py) with a feathered
    # coastline. Callers that cannot blend (no Pillow, no mask data) serve
    # the plain land layer, so the field is an upgrade, never a dependency.
    ocean_blend_source_id: str | None = None

    @property
    def media_type(self) -> str:
        return "image/jpeg" if self.tile_format == "jpeg" else "image/png"
