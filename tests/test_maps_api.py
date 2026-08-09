import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.rest.maps_api import MapsApi

# Realistic size: anything under BLANK_TILE_MAX_BYTES is treated as a
# "no imagery here" placeholder by the WMS fetch path.
_JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg" * 700


@pytest.fixture
def api(tmp_path, wms_source):
    return MapsApi(tmp_path, sources={wms_source.id: wms_source})


@pytest.fixture
def client(api):
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def test_sources_listed_with_license(client):
    payload = client.get("/api/v1/maps/sources").json()
    assert payload[0]["id"] == "test-wms"
    assert payload[0]["license_name"].startswith("Datenlizenz")


def test_plan_previews_size(client, two_stop_spec):
    response = client.post("/api/v1/maps/plan", json=two_stop_spec.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["tile_count"] > 0
    assert body["estimated_mb"] > 0


def test_bundle_tile_served_and_missing_404(client, api, wms_source):
    bundle = api._open_bundle("prefilled", source=wms_source)
    coord = TileCoord(z=12, x=2181, y=1432)
    bundle.put_tile(coord, _JPEG)

    ok = client.get(f"/api/v1/maps/bundles/prefilled/tiles/{coord.z}/{coord.x}/{coord.y}")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"
    assert ok.content == _JPEG

    missing = client.get("/api/v1/maps/bundles/prefilled/tiles/12/0/0")
    assert missing.status_code == 404
    assert client.get("/api/v1/maps/bundles/nope/tiles/12/0/0").status_code == 404


def test_live_proxy_caches_into_bundle(client, api):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    first = client.get("/api/v1/maps/live/test-wms/12/2181/1432")
    assert first.status_code == 200 and first.content == _JPEG
    second = client.get("/api/v1/maps/live/test-wms/12/2181/1432")
    assert second.status_code == 200
    assert len(calls) == 1  # second hit came from the cached bundle

    listed = client.get("/api/v1/maps/bundles").json()
    assert any(b["id"] == "live-test-wms" and b["tile_count"] == 1 for b in listed)


def test_live_proxy_rejects_out_of_range_zoom(client):
    assert client.get("/api/v1/maps/live/test-wms/5/1/1").status_code == 404


def test_demo_devserver_payload(tmp_path):
    from agentic_maps.devserver import create_app

    with TestClient(create_app(bundles_dir=tmp_path)) as dev:
        # The landing page is a plain map: no scenario chrome, no stops.
        payload = dev.get("/api/demo-spec").json()
        assert payload["spec"]["locations"] == []
        assert payload["spec"]["interactive"] is True
        assert payload["countries_url"].endswith("/geo/countries")
        assert payload["lang"] == "de" and "en" in payload["languages"]

        # The demo scenario is what the dev panel loads.
        payload = dev.get("/api/demo-spec?scenario=1").json()
        assert payload["spec"]["locations"][0]["id"] == "hq"
        assert "/api/v1/maps/live/de-dop/" in payload["tiles_url_template"]
        assert "dl-de/by-2-0" in payload["attribution"]
        assert "OpenStreetMap" in payload["attribution"]
        assert payload["offline"] is False
        index = dev.get("/")
        assert index.status_code == 200
        assert 'data-agentic-map' in index.text


def test_package_ungated_in_every_mode_for_an_already_harvested_spec(api, client, wms_source, two_stop_spec):
    """`/package` (`package/builder.py`) only zips up what an earlier "online"
    harvest already put on this disk — no network call, no new data minted.
    It is deliberately NOT gated by `_require_provisioning_allowed` or
    `_require_network_allowed` (see its handler's docstring), so it keeps
    working in "mixed" and even "offline", unlike `harvest`/`vector/extract`
    themselves."""
    bundle = api._open_bundle(two_stop_spec.id, source=wms_source)
    bundle.put_tile(TileCoord(z=16, x=1, y=1), _JPEG)

    for mode in ("online", "mixed", "offline"):
        api.mode = mode
        response = client.post("/api/v1/maps/package", json=two_stop_spec.model_dump())
        assert response.status_code == 200, (mode, response.text)
        assert response.json()["spec_id"] == two_stop_spec.id


def test_plan_accepts_the_map_apps_own_empty_spec(client):
    """Regression: `POST /plan` with the choreography-free spec the map app
    starts from (locations=[], only an overview) crashed with an unhandled
    `max() iterable argument is empty` ValueError — a 500 for a spec the
    product itself mints (devserver.empty_spec)."""
    spec = {
        "id": "map-app", "source_id": "test-wms",
        "overview": {"center": {"lat": 48.6, "lon": 9.25}, "zoom": 12.4},
        "locations": [],
    }
    response = client.post("/api/v1/maps/plan", json=spec)
    assert response.status_code == 200, response.text
    assert response.json()["tile_count"] > 0


def test_plan_without_locations_or_overview_is_a_400_not_a_500(client):
    response = client.post("/api/v1/maps/plan", json={"id": "void", "source_id": "test-wms"})
    assert response.status_code == 400
    assert "no locations and no overview" in response.json()["detail"]


def test_harvest_of_an_unplannable_spec_is_a_400_not_a_500(client):
    response = client.post("/api/v1/maps/harvest", json={"id": "void", "source_id": "test-wms"})
    assert response.status_code == 400


def test_package_refuses_a_path_traversal_spec_id(client, two_stop_spec):
    """`spec.id` becomes filesystem paths inside /package (the .mbtiles it
    reads, the .zip it writes) — a traversal id must be refused by the same
    validation the bundle routes use, not resolved outside bundles_dir."""
    evil = two_stop_spec.model_dump()
    evil["id"] = "../../outside/escape"
    response = client.post("/api/v1/maps/package", json=evil)
    assert response.status_code == 400
    assert "invalid bundle id" in response.json()["detail"]
