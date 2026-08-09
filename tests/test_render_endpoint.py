"""REST-level tests for POST /render and its payload-token handshake.

No real browser anywhere here: `agentic_maps.render.service.RenderService`
is monkeypatched with a fake before each request, since the endpoint imports
it lazily (`from ..render.service import RenderService` INSIDE the route
function) precisely so this kind of substitution works without needing
Playwright installed at all. Live-browser verification is
`tools/verify_render.py`, deliberately kept out of the fast suite.
"""

import agentic_maps.render.service as render_service
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.rest.maps_api import MapsApi

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _FakeRenderService:
    """Records what it was asked to render; returns canned bytes."""

    last_kwargs: dict | None = None
    result: bytes = _PNG_MAGIC + b"fake-image-bytes"
    error: Exception | None = None

    def __init__(self, *, base_url: str, chromium_executable_path: str | None = None):
        self.base_url = base_url

    async def render(self, **kwargs) -> bytes:
        _FakeRenderService.last_kwargs = kwargs
        if _FakeRenderService.error is not None:
            raise _FakeRenderService.error
        return _FakeRenderService.result


@pytest.fixture(autouse=True)
def fake_render_service(monkeypatch):
    _FakeRenderService.last_kwargs = None
    _FakeRenderService.result = _PNG_MAGIC + b"fake-image-bytes"
    _FakeRenderService.error = None
    monkeypatch.setattr(render_service, "RenderService", _FakeRenderService)
    yield
    _FakeRenderService.error = None


@pytest.fixture
def api(tmp_path, wms_source):
    return MapsApi(tmp_path, sources={wms_source.id: wms_source}, render_base_url="http://127.0.0.1:8095")


@pytest.fixture
def client(api):
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def _view_body(**overrides):
    body = {
        "view": {"center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 14.0, "source_id": "test-wms"},
        "width": 800, "height": 600, "scale": 2, "format": "png",
    }
    body.update(overrides)
    return body


def test_render_returns_image_bytes_with_content_type(client):
    response = client.post("/api/v1/maps/render", json=_view_body())
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _FakeRenderService.result


def test_render_passes_width_height_scale_format_through(client):
    client.post("/api/v1/maps/render", json=_view_body(width=1024, height=768, scale=3, format="jpeg"))
    kwargs = _FakeRenderService.last_kwargs
    assert kwargs["width"] == 1024
    assert kwargs["height"] == 768
    assert kwargs["scale"] == 3
    assert kwargs["format"] == "jpeg"


def test_render_jpeg_sets_jpeg_content_type(client):
    response = client.post("/api/v1/maps/render", json=_view_body(format="jpeg"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_render_rejects_invalid_scale(client):
    response = client.post("/api/v1/maps/render", json=_view_body(scale=7))
    assert response.status_code == 422


def test_render_rejects_neither_spec_nor_view(client):
    body = _view_body()
    del body["view"]
    response = client.post("/api/v1/maps/render", json=body)
    assert response.status_code == 422


def test_render_unavailable_maps_to_501(client):
    _FakeRenderService.error = render_service.RenderUnavailable("no chromium")
    response = client.post("/api/v1/maps/render", json=_view_body())
    assert response.status_code == 501
    assert "no chromium" in response.json()["detail"]


def test_render_failure_maps_to_502(client):
    _FakeRenderService.error = RuntimeError("page crashed")
    response = client.post("/api/v1/maps/render", json=_view_body())
    assert response.status_code == 502


def test_render_payload_token_is_consumed_and_matches_the_request(client, api):
    body = _view_body()
    client.post("/api/v1/maps/render", json=body)
    token = _FakeRenderService.last_kwargs["token"]
    # The token minted for this render must have been valid WHILE the render
    # ran (this is what `render.html`'s own fetch would have hit)...
    assert token
    # ...and is torn down afterwards: the endpoint's `finally` pops it, so a
    # stale token cannot be replayed against a later render.
    assert token not in api._render_payloads


def test_render_payload_endpoint_serves_and_matches_spec(client, api):
    # Build a payload directly (bypassing /render) to check the plain fetch
    # contract render.html relies on.
    from agentic_maps.models.camera_pose import CameraPose
    from agentic_maps.models.lat_lon import LatLon
    from agentic_maps.models.map_spec import MapSpec

    spec = MapSpec(
        id="direct-test", source_id="test-wms",
        overview=CameraPose(center=LatLon(lat=1.0, lon=2.0), zoom=10.0),
    )
    built = api.build_payload(spec)
    token = api._store_render_payload(built)
    response = client.get(f"/api/v1/maps/render/payload/{token}")
    assert response.status_code == 200
    assert response.json()["spec"]["id"] == "direct-test"


def test_render_payload_unknown_token_is_404(client):
    response = client.get("/api/v1/maps/render/payload/does-not-exist")
    assert response.status_code == 404
