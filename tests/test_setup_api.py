"""REST tests for `POST /setup/plan` and `POST /setup/apply`.

Every request supplies an explicit `bbox` (so `default_resolve_bbox` never
makes a real geocode call) and either `pbf_url` or a bbox small enough that
the "manual-required" path is hit — never the `"overpass"` fetch path, which
would need a real network + `osmium` call. `run` is left `false` in the
`/apply` test, matching the constraint that this suite never invokes Docker.
Mirrors `tests/test_maps_api.py`'s TestClient pattern.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.rest.setup_api import SetupApi

BBOX = {"west": 9.70, "south": 52.35, "east": 9.78, "north": 52.40}
BIG_BBOX = {"west": 5.0, "south": 47.0, "east": 15.0, "north": 55.0}


def _client() -> TestClient:
    app = FastAPI()
    SetupApi().mount(app)
    return TestClient(app)


def test_setup_plan_rejects_unknown_mode():
    response = _client().post("/setup/plan", json={"bbox": BBOX, "mode": "bogus"})
    assert response.status_code == 400


def test_setup_plan_with_explicit_bbox_and_pbf_url():
    response = _client().post("/setup/plan", json={
        "bbox": BBOX, "mode": "mixed", "pbf_url": "https://example.test/x.osm.pbf",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["pbf"]["method"] == "user-url"
    assert body["pbf"]["url"] == "https://example.test/x.osm.pbf"
    assert 'VALHALLA_TILE_URLS="https://example.test/x.osm.pbf"' in body["env_content"]
    assert body["compose_files"] == ["docker-compose.yml"]


def test_setup_plan_with_big_bbox_reports_manual_required_warning():
    response = _client().post("/setup/plan", json={"bbox": BIG_BBOX, "mode": "mixed"})
    assert response.status_code == 200
    body = response.json()
    assert body["pbf"]["method"] == "manual-required"
    assert body["warnings"]


def test_setup_plan_small_bbox_previews_overpass_without_fetching():
    response = _client().post("/setup/plan", json={"bbox": BBOX, "mode": "mixed"})
    assert response.status_code == 200
    body = response.json()
    assert body["pbf"]["method"] == "overpass"
    assert body["pbf"]["path"] == ""
    assert body["pbf"]["url"] == ""


def test_setup_plan_offline_mode_layers_compose_files():
    response = _client().post("/setup/plan", json={
        "bbox": BBOX, "mode": "offline", "pbf_url": "https://example.test/x.osm.pbf",
    })
    body = response.json()
    assert body["compose_files"] == ["docker-compose.yml", "docker-compose.offline.yml"]


def test_setup_apply_writes_files_without_running_docker(tmp_path):
    out_dir = tmp_path / "stack"
    response = _client().post("/setup/apply", json={
        "answers": {
            "bbox": BBOX, "mode": "mixed", "region_id": "hannover",
            "pbf_url": "https://example.test/x.osm.pbf",
        },
        "out_dir": str(out_dir),
        "run": False,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["started"] is False
    assert (out_dir / ".env").exists()
    assert (out_dir / "docker-compose.yml").exists()
    env_text = (out_dir / ".env").read_text()
    assert 'AGENTIC_MAPS_REGION_ID="hannover"' in env_text


def test_setup_apply_rejects_unknown_mode(tmp_path):
    response = _client().post("/setup/apply", json={
        "answers": {"bbox": BBOX, "mode": "bogus"},
        "out_dir": str(tmp_path / "stack"),
        "run": False,
    })
    assert response.status_code == 400
