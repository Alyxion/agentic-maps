import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.rest.maps_api import MapsApi

_MVT = b"\x1a\x2afake-mvt-payload"


@pytest.fixture
def api(tmp_path, wms_source, pmtiles_writer):
    # Two extracts: a "germany" one (z up to 5 here) and a detail region.
    pmtiles_writer(
        tmp_path / "streets-germany.pmtiles",
        [(0, 0, 0), (5, 16, 10)],
        (56_000_000, 471_000_000, 152_000_000, 552_000_000),
        _MVT,
    )
    pmtiles_writer(
        tmp_path / "streets-region.pmtiles",
        [(0, 0, 0), (14, 8617, 5658)],  # z14 tile near Metzingen
        (89_000_000, 484_000_000, 95_000_000, 489_000_000),
        _MVT,
    )
    # remote_planet off: a unit test must not depend on the 137 GB archive.
    return MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                   remote_planet=False)


@pytest.fixture
def client(api):
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def test_vector_listing(client):
    listed = {v["id"]: v for v in client.get("/api/v1/maps/vector").json()}
    assert set(listed) == {"streets-germany", "streets-region"}
    assert listed["streets-region"]["max_zoom"] == 14
    assert listed["streets-region"]["bounds"]["west"] == pytest.approx(8.9)


def test_vector_auto_prefers_detail_extract_and_decompresses(client):
    response = client.get("/api/v1/maps/vector/auto/tiles/14/8617/5658.mvt")
    assert response.status_code == 200
    assert response.content == _MVT  # gzip transparently decompressed
    assert response.headers["content-type"].startswith("application/x-protobuf")


def test_vector_auto_falls_back_to_wide_extract(client):
    assert client.get("/api/v1/maps/vector/auto/tiles/5/16/10.mvt").status_code == 200
    # A tile outside every extract's bounds:
    assert client.get("/api/v1/maps/vector/auto/tiles/5/2/2.mvt").status_code == 404


def test_glyphs_cached_once(client, api):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"glyphdata")

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    url = "/api/v1/maps/assets/glyphs/Noto%20Sans%20Regular/0-255.pbf"
    assert client.get(url).content == b"glyphdata"
    assert client.get(url).content == b"glyphdata"
    assert len(calls) == 1
    assert (api.assets_dir / "glyphs" / "Noto Sans Regular" / "0-255.pbf").exists()


def test_sprites_cached_and_offline_serves_cache(client, api, tmp_path, wms_source):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert client.get("/api/v1/maps/assets/sprites/v4/dark.json").status_code == 200

    offline_api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                          assets_dir=api.assets_dir, mode="offline")
    offline_app = FastAPI()
    offline_api.mount(offline_app)
    offline_client = TestClient(offline_app)
    # Cached asset serves; uncached asset is refused, never fetched.
    assert offline_client.get("/api/v1/maps/assets/sprites/v4/dark.json").status_code == 200
    assert offline_client.get("/api/v1/maps/assets/sprites/v4/light.json").status_code == 403


def test_glyphs_refuse_path_traversal_segments(client):
    """The sprites route always had a ".." guard; glyphs takes two path
    segments into the same cache directory and needs the identical one.
    Percent-encoded so the dots survive client-side URL normalization and
    actually reach the handler as a ".." segment."""
    response = client.get("/api/v1/maps/assets/glyphs/%2E%2E/0-255.pbf")
    assert response.status_code == 400
    assert "invalid glyph path" in response.json()["detail"]
