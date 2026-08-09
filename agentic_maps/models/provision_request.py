from typing import Literal

from pydantic import BaseModel, model_validator

from .bbox_deg import BBoxDeg

ProvisionLayer = Literal["maps", "aerial", "routing"]

# The zoom caps the aerial layer may be asked for. Deliberately closed at 16:
# z17+ over a whole country is hundreds of gigabytes to terabytes (Germany
# z13-19 ≈ 3.9 TB) — full-resolution imagery stays a corridor/spec harvest
# (`/harvest`), never a region bulk download.
AERIAL_ZOOM_CAPS = (13, 14, 15, 16)


class ProvisionRequest(BaseModel):
    """What to provision: a region × a set of layers.

    `region` names a preset (`de`, `dach`, `eu`, `earth` — see
    `provision/estimates.py`) OR `bbox` supplies a custom area; exactly one
    of the two. `aerial_max_zoom` is REQUIRED whenever the aerial layer is
    requested for anything but `earth` (whose ladder is fixed at z0-12): the
    zoom cap is the size dial, and defaulting it would silently pick a
    multi-gigabyte download.
    """

    region: str = ""
    bbox: BBoxDeg | None = None
    # Slug used to name bundles/files for a custom bbox ("metzingen" ->
    # streets-metzingen.pmtiles). Presets use their own id.
    region_id: str = ""
    layers: list[ProvisionLayer]
    aerial_max_zoom: int | None = None
    # Routing layer: where the compose stack lives (or should be staged).
    # Empty means the setup wizard's default, ./agentic-maps-stack.
    routing_stack_dir: str = ""
    # Routing layer for a custom bbox that is too big for the Overpass
    # export: a Geofabrik (or other) PBF URL to download instead.
    pbf_url: str = ""
    # Only when true AND docker is on PATH does the routing layer run
    # `docker compose up -d` after staging the PBF; otherwise the job
    # reports the exact next command instead (never fails just because
    # Docker is not running).
    start_stack: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "ProvisionRequest":
        if bool(self.region) == (self.bbox is not None):
            raise ValueError("pass exactly one of `region` (preset) or `bbox` (custom)")
        if not self.layers:
            raise ValueError("`layers` must name at least one of maps/aerial/routing")
        if "aerial" in self.layers and self.region != "earth":
            if self.aerial_max_zoom not in AERIAL_ZOOM_CAPS:
                raise ValueError(
                    f"aerial layer needs aerial_max_zoom in {AERIAL_ZOOM_CAPS} "
                    "(z17+ is corridor-harvest territory, not a region preset)")
        return self
