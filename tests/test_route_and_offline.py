import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.rest.maps_api import MapsApi

_OSRM_BODY = {
    "code": "Ok",
    "routes": [
        {
            "duration": 8040.0,
            "distance": 233000.0,
            "geometry": {"coordinates": [[9.18, 48.77], [10.5, 48.5], [11.57, 48.14]]},
        }
    ],
}


def _mock_router() -> OsrmRouter:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/route/v1/driving/" in str(request.url)
        return httpx.Response(200, content=json.dumps(_OSRM_BODY).encode())

    return OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))


def _client(tmp_path, wms_source, *, mode="online"):
    api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                  router=_mock_router(), mode=mode)
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def _route_body():
    return {
        "route_id": "stuttgart-munich",
        "from_location": "stuttgart",
        "to_location": "munich",
        "mode": "car",
        "start": {"lat": 48.7784, "lon": 9.1806},
        "end": {"lat": 48.1374, "lon": 11.5755},
    }


def test_route_endpoint_returns_embedded_route(tmp_path, wms_source):
    response = _client(tmp_path, wms_source).post("/api/v1/maps/route", json=_route_body())
    assert response.status_code == 200
    route = response.json()
    assert route["duration_min"] == pytest.approx(134.0)
    assert route["distance_km"] == pytest.approx(233.0)
    assert route["geometry"][0] == {"lat": 48.77, "lon": 9.18}


def test_offline_mode_blocks_network_endpoints(tmp_path, wms_source):
    client = _client(tmp_path, wms_source, mode="offline")
    assert client.post("/api/v1/maps/route", json=_route_body()).status_code == 403
    assert client.get("/api/v1/maps/live/test-wms/12/2181/1432").status_code == 403
    spec = {
        "id": "s", "source_id": "test-wms",
        "locations": [{"id": "a", "name": "A",
                       "camera": {"center": {"lat": 48.5, "lon": 9.3}, "zoom": 13.0}}],
    }
    assert client.post("/api/v1/maps/harvest", json=spec).status_code == 403
    # Plan preview stays available offline (pure math).
    assert client.post("/api/v1/maps/plan", json=spec).status_code == 200


@pytest.mark.parametrize("mode,expect_ok", [("online", True), ("mixed", True), ("offline", False)])
def test_route_is_a_live_endpoint_allowed_in_mixed(tmp_path, wms_source, mode, expect_ok):
    """`/route` answers the current request live, minting nothing for later
    offline use — per `models/runtime_mode.py` it is only refused in
    "offline", unlike a provisioning endpoint (`/vector/extract`,
    `/harvest`), which is also refused in "mixed"."""
    client = _client(tmp_path, wms_source, mode=mode)
    response = client.post("/api/v1/maps/route", json=_route_body())
    assert (response.status_code == 200) == expect_ok


def test_mode_switch_changes_gating_mid_session(tmp_path, wms_source):
    """`POST /mode` must take effect for the very next request — the mode is
    read per request, not captured at mount time."""
    client = _client(tmp_path, wms_source, mode="online")
    assert client.post("/api/v1/maps/route", json=_route_body()).status_code == 200

    assert client.post("/api/v1/maps/mode", json={"mode": "offline"}).json()["mode"] == "offline"
    assert client.post("/api/v1/maps/route", json=_route_body()).status_code == 403
    assert client.get("/api/v1/maps/mode").json()["mode"] == "offline"

    client.post("/api/v1/maps/mode", json={"mode": "mixed"})
    assert client.post("/api/v1/maps/route", json=_route_body()).status_code == 200
    # ... but "mixed" still refuses provisioning:
    spec = {
        "id": "s", "source_id": "test-wms",
        "locations": [{"id": "a", "name": "A",
                       "camera": {"center": {"lat": 48.5, "lon": 9.3}, "zoom": 13.0}}],
    }
    assert client.post("/api/v1/maps/harvest", json=spec).status_code == 403
