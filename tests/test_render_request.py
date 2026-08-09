import pytest
from pydantic import ValidationError

from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_spec import MapSpec
from agentic_maps.models.render_request import RenderRequest
from agentic_maps.models.render_view import RenderView


def _view() -> RenderView:
    return RenderView(center=LatLon(lat=48.5386, lon=9.2925), zoom=14.0, source_id="de-dop")


def test_view_convenience_path_resolves_to_a_one_stop_spec():
    request = RenderRequest(view=_view(), width=800, height=600)
    spec = request.resolved_spec()
    assert isinstance(spec, MapSpec)
    assert spec.source_id == "de-dop"
    assert spec.overview is not None
    assert spec.overview.center.lat == pytest.approx(48.5386)
    assert spec.overview.zoom == pytest.approx(14.0)
    assert spec.locations == []
    assert spec.interactive is False


def test_full_spec_path_is_returned_unchanged():
    spec = MapSpec(id="s1", source_id="de-dop", overview=None, locations=[])
    request = RenderRequest(spec=spec, width=800, height=600)
    assert request.resolved_spec() is spec


def test_neither_spec_nor_view_is_rejected():
    with pytest.raises(ValidationError, match="exactly one"):
        RenderRequest(width=800, height=600)


def test_both_spec_and_view_is_rejected():
    spec = MapSpec(id="s1", source_id="de-dop")
    with pytest.raises(ValidationError, match="exactly one"):
        RenderRequest(spec=spec, view=_view(), width=800, height=600)


@pytest.mark.parametrize("field,value", [
    ("width", 10), ("height", 10_000), ("scale", 5), ("format", "bmp"), ("quality", 0),
])
def test_out_of_bounds_render_params_are_rejected(field, value):
    kwargs = dict(view=_view(), width=800, height=600, scale=1, format="png", quality=85)
    kwargs[field] = value
    with pytest.raises(ValidationError):
        RenderRequest(**kwargs)


def test_defaults_are_a_lossless_1x_png():
    request = RenderRequest(view=_view(), width=800, height=600)
    assert request.scale == 1
    assert request.format == "png"
    assert request.default_view == "hybrid"
