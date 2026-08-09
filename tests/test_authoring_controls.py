import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.models.camera_pose import CameraPose
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_location import MapLocation
from agentic_maps.models.map_spec import MapSpec
from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.sources.presets import builtin_composites, builtin_sources
from agentic_maps.storage.mbtiles import MBTilesBundle

_NOMINATIM = [
    {"display_name": "Metzingen, Reutlingen, Baden-Württemberg", "lat": "48.5369", "lon": "9.2837", "type": "town",
     "address": {"town": "Metzingen", "postcode": "72555", "state": "Baden-Württemberg",
                 "country": "Deutschland", "country_code": "de"}},
]


@pytest.fixture
def api(tmp_path):
    return MapsApi(tmp_path, sources=builtin_sources(), composites=builtin_composites())


@pytest.fixture
def client(api):
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def test_geocode_proxies_nominatim(client, api):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "nominatim.openstreetmap.org" in str(request.url)
        assert "agentic-maps" in request.headers["User-Agent"]
        return httpx.Response(200, content=json.dumps(_NOMINATIM).encode())

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = client.post("/api/v1/maps/geocode", json={"q": "Metzingen"})
    assert response.status_code == 200
    hit = response.json()[0]
    assert hit["lat"] == pytest.approx(48.5369)
    assert hit["kind"] == "town"
    # Structured components ride along for the detail card (addressdetails=1).
    assert hit["address"]["postcode"] == "72555"
    assert hit["address"]["locality"] == "Metzingen"
    assert hit["address"]["country_code"] == "de"


def test_reverse_geocode_carries_structured_address(client, api):
    body = {
        "display_name": "Im Schwöllbogen 19, Dettingen an der Erms",
        "lat": "48.5254", "lon": "9.3466",
        "address": {"road": "Im Schwöllbogen", "house_number": "19", "postcode": "72581",
                    "village": "Dettingen an der Erms", "country": "Deutschland",
                    "country_code": "de"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["addressdetails"] == "1"
        return httpx.Response(200, content=json.dumps(body).encode())

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hit = client.post("/api/v1/maps/reverse-geocode",
                      json={"lat": 48.5254, "lon": 9.3466}).json()
    address = hit["address"]
    assert address["road"] == "Im Schwöllbogen"
    assert address["house_number"] == "19"
    assert address["postcode"] == "72581"
    # village -> the one normalized locality field.
    assert address["locality"] == "Dettingen an der Erms"


def test_geocode_without_address_details_yields_null_address(client, api):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(
            [{"display_name": "Somewhere", "lat": "1.0", "lon": "2.0"}]).encode())

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hit = client.post("/api/v1/maps/geocode", json={"q": "Somewhere"}).json()[0]
    assert hit["address"] is None


def test_mode_toggle_gates_network_endpoints(client):
    assert client.get("/api/v1/maps/mode").json() == {"mode": "online"}
    assert client.post("/api/v1/maps/mode", json={"mode": "offline"}).json() == {"mode": "offline"}
    assert client.post("/api/v1/maps/geocode", json={"q": "x"}).status_code == 403
    assert client.get("/api/v1/maps/live/de-dop/14/8617/5658").status_code == 403
    assert client.post("/api/v1/maps/mode", json={"mode": "online"}).json() == {"mode": "online"}


def test_mixed_mode_allows_live_endpoints_but_blocks_provisioning(client):
    """The whole point of "mixed": live per-request actions stay live, bulk
    data provisioning does not — see `models/runtime_mode.py`."""
    assert client.post("/api/v1/maps/mode", json={"mode": "mixed"}).json() == {"mode": "mixed"}
    # Live tile proxy: allowed in mixed exactly as in online.
    live = client.get("/api/v1/maps/live/de-dop/14/8617/5658")
    assert live.status_code != 403
    # Bulk provisioning: refused in mixed exactly as in offline.
    spec = {
        "id": "s", "source_id": "de-dop",
        "locations": [{"id": "a", "name": "A",
                       "camera": {"center": {"lat": 48.5, "lon": 9.3}, "zoom": 13.0}}],
    }
    assert client.post("/api/v1/maps/harvest", json=spec).status_code == 403
    assert client.post("/api/v1/maps/mode", json={"mode": "online"}).json() == {"mode": "online"}


def test_cache_clear_keeps_vector_extracts(client, api, tmp_path, wms_source, pmtiles_writer):
    bundle = MBTilesBundle.create(tmp_path / "live-de-dop.mbtiles", wms_source)
    bundle.put_tile(TileCoord(z=12, x=1, y=1), b"t")
    pmtiles_writer(tmp_path / "streets-region.pmtiles", [(0, 0, 0)],
                   (89_000_000, 484_000_000, 95_000_000, 489_000_000), b"\x1a\x2amvt")
    (api.assets_dir / "glyphs").mkdir(parents=True)
    (api.assets_dir / "glyphs" / "x.pbf").write_bytes(b"g")

    report = client.delete("/api/v1/maps/cache").json()
    assert "live-de-dop.mbtiles" in report["removed"]
    assert "assets/" in report["removed"]
    assert not (tmp_path / "live-de-dop.mbtiles").exists()
    assert (tmp_path / "streets-region.pmtiles").exists()  # source data, kept


def test_spec_attribution_credits_only_used_states(api):
    spec = MapSpec(
        id="bw-only", source_id="de-dop",
        locations=[
            MapLocation(id="hq", name="HQ",
                        camera=CameraPose(center=LatLon(lat=48.5386, lon=9.2925), zoom=16.0)),
        ],
    )
    attribution = api.spec_attribution(spec)
    assert "LGL Baden-Württemberg" in attribution
    assert "Bayerische" not in attribution
    assert "Berlin" not in attribution
