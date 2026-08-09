import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_highlight import MapHighlight
from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.sources.presets import builtin_sources

# Realistic size: anything under BLANK_TILE_MAX_BYTES is treated as a
# "no imagery here" placeholder by the WMS fetch path.
_JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg" * 700


def test_blue_marble_preset_is_public_domain_xyz():
    source = builtin_sources()["blue-marble"]
    assert source.kind == "xyz"
    assert "{z}/{y}/{x}" in source.url  # GIBS path order
    assert source.max_zoom == 8
    assert "public domain" in source.license_name


def test_harvest_world_global_plus_region(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})

    api = MapsApi(tmp_path, sources=builtin_sources(), composites={})
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)

    import agentic_maps.harvest.harvester as harvester_mod
    original = harvester_mod.Harvester.__init__

    def patched(self, source, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, source, **kwargs)

    harvester_mod.Harvester.__init__ = patched
    try:
        response = client.post("/api/v1/maps/harvest-world", json={
            "global_maxzoom": 1,
            "region_west": 5.6, "region_south": 47.1,
            "region_east": 15.2, "region_north": 55.2,
            "region_maxzoom": 3,
        })
    finally:
        harvester_mod.Harvester.__init__ = original

    assert response.status_code == 200
    report = response.json()
    # z0 (1) + z1 (4) global, plus the small Germany window at z2-z3.
    assert report["planned"] >= 5
    assert report["failed"] == 0
    assert report["bundle_id"] == "world"
    assert all("gibs.earthdata.nasa.gov" in u for u in calls)


def test_matrix_endpoint(tmp_path, wms_source):
    body = {"code": "Ok", "durations": [[0, 1500.0], [1440.0, 0]]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/table/v1/driving/" in str(request.url)
        return httpx.Response(200, content=json.dumps(body).encode())

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={}, router=router)
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)

    response = client.post("/api/v1/maps/matrix", json={
        "points": [{"lat": 48.5, "lon": 9.3}, {"lat": 48.1, "lon": 11.6}],
    })
    assert response.status_code == 200
    assert response.json()["durations_min"] == [[0, 25.0], [24.0, 0]]


def test_highlight_kind_validation():
    with pytest.raises(ValueError):
        MapHighlight(at=LatLon(lat=48.5, lon=9.3), kind="radius")  # radius_m missing
    with pytest.raises(ValueError):
        MapHighlight(at=LatLon(lat=48.5, lon=9.3), kind="polygon", polygon=[])
    ok = MapHighlight(
        at=LatLon(lat=48.5, lon=9.3), kind="polygon",
        polygon=[LatLon(lat=48.5, lon=9.3), LatLon(lat=48.6, lon=9.3), LatLon(lat=48.6, lon=9.4)],
    )
    assert ok.kind == "polygon"
    ping = MapHighlight(at=LatLon(lat=48.5, lon=9.3), kind="ping", label="Bahnhof")
    assert ping.kind == "ping"
