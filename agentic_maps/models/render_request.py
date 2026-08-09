import uuid

from pydantic import BaseModel, model_validator

from ..render.params import validate_render_params
from .camera_pose import CameraPose
from .map_spec import MapSpec
from .render_view import RenderView


class RenderRequest(BaseModel):
    """Body of `POST /render`: what to draw, and at what pixel size.

    Exactly one of `spec` / `view` must be given:
    - `spec`: a full `MapSpec` — the same thing a live map slide mounts, with
      locations/routes/highlights if it has any;
    - `view`: the shorthand for a plain "screenshot this point at this
      zoom", with no choreography at all.

    `width`/`height` become the Playwright viewport (and therefore the pixel
    size of the screenshot); `scale` is `devicePixelRatio` — 1x/2x/3x, mapped
    onto Chromium's `device_scale_factor`. `format`/`quality` control the
    output encoding: PNG (default, lossless) or JPEG (`quality` 1-100).
    """

    spec: MapSpec | None = None
    view: RenderView | None = None
    width: int
    height: int
    scale: int = 1
    format: str = "png"
    # JPEG only; ignored (but still validated) for PNG.
    quality: int = 85
    # Initial basemap flavor: "hybrid" (imagery + labels), "satellite"
    # (imagery only), "map-light" / "map-dark" (vector cartography only).
    # Applies whichever of `spec` / `view` was given — a `MapSpec` carries no
    # flavor of its own (that is client-side chrome state, not spec data).
    default_view: str = "hybrid"

    @model_validator(mode="after")
    def _validate(self) -> "RenderRequest":
        if (self.spec is None) == (self.view is None):
            raise ValueError("exactly one of `spec` or `view` must be set")
        validate_render_params(self.width, self.height, self.scale, self.format, self.quality)
        return self

    def resolved_spec(self) -> MapSpec:
        """The `MapSpec` to render: `spec` as given, or synthesized from `view`."""
        if self.spec is not None:
            return self.spec
        view = self.view
        assert view is not None  # guaranteed by `_validate`
        return MapSpec(
            id=f"render-{uuid.uuid4().hex[:12]}",
            source_id=view.source_id,
            overview=CameraPose(
                center=view.center, zoom=view.zoom, bearing=view.bearing, pitch=view.pitch,
            ),
            locations=[],
            interactive=False,
        )
