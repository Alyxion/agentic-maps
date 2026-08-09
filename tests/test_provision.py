"""Region-bulk provisioning: the estimator against the approved size table,
the job engine against a fake tile world, and the REST surface's gating.

The estimator test pins every preset number to the size table the owner
approved (±10%): the table is the promise users confirm, so the code must
keep computing it — a drive-by change to TILE_AVG_BYTES or a bbox would
fail here first.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.models.bbox_deg import BBoxDeg
from agentic_maps.models.provision_job import ProvisionJob
from agentic_maps.models.provision_request import ProvisionRequest
from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.provision.engine import ProvisionEngine
from agentic_maps.provision.estimates import estimate_region, preset_size_table
from agentic_maps.rest.maps_api import MapsApi

GB = 1e9


def _aerial_bytes(region: str, cap: int) -> int:
    est = estimate_region(ProvisionRequest(
        region=region, layers=["aerial"], aerial_max_zoom=cap))
    return est.total_bytes


# -- estimates vs the approved table (±10%) ---------------------------------

@pytest.mark.parametrize("region,cap,expected_gb", [
    ("de", 13, 0.7), ("de", 14, 3.6), ("de", 15, 15.0), ("de", 16, 60.0),
    ("dach", 15, 22.0),
    ("eu", 15, 356.0),
])
def test_aerial_estimates_match_the_approved_table(region, cap, expected_gb):
    assert _aerial_bytes(region, cap) == pytest.approx(expected_gb * GB, rel=0.10)


def test_earth_base_ladder_estimate_matches_table():
    est = estimate_region(ProvisionRequest(region="earth", layers=["aerial"]))
    assert est.total_bytes == pytest.approx(1.7 * GB, rel=0.10)


def test_germany_vector_estimate_matches_the_existing_extract():
    est = estimate_region(ProvisionRequest(region="de", layers=["maps"]))
    assert est.total_bytes == pytest.approx(168e6, rel=0.10)


@pytest.mark.parametrize("region,expected_gb,tolerance", [
    # PBF constants are measured Content-Lengths (2026-08-09); the approved
    # table's round figures predate this week's extracts, so the band is the
    # honest one: measured value must stay in the table's neighbourhood.
    ("de", 4.0, 0.25), ("dach", 5.5, 0.25), ("eu", 30.0, 0.25),
])
def test_routing_pbf_estimates_stay_near_the_table(region, expected_gb, tolerance):
    est = estimate_region(ProvisionRequest(region=region, layers=["routing"]))
    assert est.total_bytes == pytest.approx(expected_gb * GB, rel=tolerance)


def test_zoom_caps_are_closed_at_16():
    for bad in (12, 17, 19, None):
        with pytest.raises(ValueError):
            ProvisionRequest(region="de", layers=["aerial"], aerial_max_zoom=bad)


def test_earth_offers_only_aerial():
    with pytest.raises(ValueError):
        estimate_region(ProvisionRequest(region="earth", layers=["routing"]))


def test_eu_aerial_carries_a_strong_warning():
    est = estimate_region(ProvisionRequest(
        region="eu", layers=["aerial"], aerial_max_zoom=15))
    assert any("WEEKS" in warning for warning in est.warnings)


def test_custom_bbox_estimates_scale_with_area():
    small = estimate_region(ProvisionRequest(
        bbox=BBoxDeg(west=9.2, south=48.5, east=9.4, north=48.6),
        region_id="metzingen", layers=["maps", "aerial"], aerial_max_zoom=13))
    assert small.region == "metzingen"
    aerial = next(l for l in small.layers if l.layer == "aerial")
    assert 0 < aerial.tiles < 200
    maps_layer = next(l for l in small.layers if l.layer == "maps")
    assert 0 < maps_layer.bytes_estimate < 10e6


def test_request_needs_exactly_one_of_region_and_bbox():
    with pytest.raises(ValueError):
        ProvisionRequest(layers=["maps"])
    with pytest.raises(ValueError):
        ProvisionRequest(region="de",
                         bbox=BBoxDeg(west=9.0, south=48.0, east=10.0, north=49.0),
                         layers=["maps"])


def test_preset_size_table_is_printable_and_complete():
    lines = preset_size_table()
    text = "\n".join(lines)
    for token in ("earth", "de aerial z13-16", "dach aerial z13-15",
                  "eu aerial z13-15", "routing PBF"):
        assert token in text


# -- the engine, against a fake tile world -----------------------------------

class _FakeExtractor:
    def __init__(self, bundles_dir: Path):
        self.bundles_dir = bundles_dir
        self.calls: list[dict] = []

    async def extract(self, bundle_id, *, west, south, east, north, maxzoom=15):
        self.calls.append({"id": bundle_id, "maxzoom": maxzoom})
        path = self.bundles_dir / f"streets-{bundle_id}.pmtiles"
        path.write_bytes(b"pm" * 512)
        return path


class _FakeWorld:
    id = "blue-marble-plus"


class _FakeApi:
    """Just enough MapsApi surface for the engine: band tiles, extractor,
    a live HTTP client, and the vector-refresh hook."""

    def __init__(self, tmp_path: Path, *, tile: bytes = b"\xff\xd8jpeg" * 64,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.extractor = _FakeExtractor(tmp_path)
        self.tile = tile
        self.fetched: list[tuple[str, int, int, int]] = []
        self.refreshed: list[Path] = []
        self.transport = transport
        self._client = None

    def _world_source(self):
        return _FakeWorld()

    async def fetch_aerial_band_tile(self, source_id: str, coord: TileCoord):
        self.fetched.append((source_id, coord.z, coord.x, coord.y))
        return self.tile

    def refresh_vector_bundle(self, path: Path) -> None:
        self.refreshed.append(path)

    async def _live_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self.transport)
        return self._client


METZINGEN = BBoxDeg(west=9.27, south=48.53, east=9.30, north=48.55)


async def _finished(engine: ProvisionEngine, job: ProvisionJob,
                    timeout_s: float = 10.0) -> ProvisionJob:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while engine.running(job.id):
        assert asyncio.get_event_loop().time() < deadline, "job did not finish"
        await asyncio.sleep(0.02)
    return engine.get(job.id)


async def test_engine_runs_maps_and_aerial_to_done(tmp_path):
    api = _FakeApi(tmp_path)
    engine = ProvisionEngine(api, tmp_path / "state")
    job = engine.start(ProvisionRequest(
        bbox=METZINGEN, region_id="metzingen",
        layers=["maps", "aerial"], aerial_max_zoom=13))
    job = await _finished(engine, job)

    assert job.state == "done"
    assert api.extractor.calls == [{"id": "metzingen", "maxzoom": 15}]
    assert api.refreshed and api.refreshed[0].name == "streets-metzingen.pmtiles"
    aerial = next(l for l in job.layers if l.layer == "aerial")
    assert aerial.status == "done"
    assert aerial.tiles_done == aerial.tiles_total > 0
    assert aerial.bytes_done == aerial.tiles_done * len(api.tile)
    # z13 tiles over a city bbox go to the 20 cm federation band, never the
    # world band.
    assert {source for source, *_ in api.fetched} == {"de-dop"}
    # Persisted state survives on disk with the final verdict.
    persisted = ProvisionJob.model_validate_json(
        (tmp_path / "state" / f"{job.id}.json").read_text())
    assert persisted.state == "done"


async def test_engine_counts_failed_tiles_without_failing_the_job(tmp_path):
    api = _FakeApi(tmp_path)

    async def flaky(source_id, coord):
        api.fetched.append((source_id, coord.z, coord.x, coord.y))
        if len(api.fetched) == 1:
            raise RuntimeError("upstream hiccup")
        return api.tile

    api.fetch_aerial_band_tile = flaky
    engine = ProvisionEngine(api, tmp_path / "state")
    job = engine.start(ProvisionRequest(
        bbox=METZINGEN, region_id="metzingen", layers=["aerial"], aerial_max_zoom=13))
    job = await _finished(engine, job)
    aerial = job.layers[0]
    assert job.state == "done" and aerial.tiles_failed == 1
    assert aerial.tiles_done == aerial.tiles_total


async def test_engine_cancel_stops_the_tile_loop(tmp_path):
    api = _FakeApi(tmp_path)
    started = asyncio.Event()

    async def slow(source_id, coord):
        started.set()
        await asyncio.sleep(0.05)
        return api.tile

    api.fetch_aerial_band_tile = slow
    engine = ProvisionEngine(api, tmp_path / "state")
    # A wider bbox so there are enough tiles that cancel lands mid-loop.
    job = engine.start(ProvisionRequest(
        bbox=BBoxDeg(west=9.0, south=48.3, east=9.8, north=48.8),
        region_id="cancelme", layers=["aerial"], aerial_max_zoom=14))
    await started.wait()
    engine.cancel(job.id)
    job = await _finished(engine, job)
    assert job.state == "cancelled"
    assert "resumable" in job.note
    aerial = job.layers[0]
    assert aerial.tiles_done < aerial.tiles_total


async def test_engine_marks_running_jobs_interrupted_after_restart(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    orphan = ProvisionJob(
        id="provision-orphan000001",
        request=ProvisionRequest(region="de", layers=["maps"]),
        state="running", created_at=1.0,
    )
    (state_dir / f"{orphan.id}.json").write_text(orphan.model_dump_json())

    engine = ProvisionEngine(_FakeApi(tmp_path), state_dir)
    reloaded = engine.get(orphan.id)
    assert reloaded is not None and reloaded.state == "interrupted"
    assert "resumable" in reloaded.note


async def test_engine_routing_stages_pbf_and_reports_next_command(tmp_path):
    payload = b"PBF" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("metzingen.osm.pbf")
        return httpx.Response(200, content=payload)

    api = _FakeApi(tmp_path, transport=httpx.MockTransport(handler))
    engine = ProvisionEngine(api, tmp_path / "state")
    stack_dir = tmp_path / "stack"
    job = engine.start(ProvisionRequest(
        bbox=METZINGEN, region_id="metzingen", layers=["routing"],
        pbf_url="https://pbf.test/metzingen.osm.pbf",
        routing_stack_dir=str(stack_dir)))
    job = await _finished(engine, job)

    assert job.state == "done"
    routing = job.layers[0]
    assert routing.status == "done"
    # Staged for Valhalla, hardlinked for Nominatim — the bytes exist once.
    valhalla_pbf = stack_dir / "valhalla_data" / "metzingen.osm.pbf"
    nominatim_pbf = stack_dir / "nominatim_data" / "metzingen.osm.pbf"
    assert valhalla_pbf.read_bytes() == payload
    assert nominatim_pbf.stat().st_ino == valhalla_pbf.stat().st_ino
    # Stack files exist and the .env names the PBF for the Nominatim mount.
    env = (stack_dir / ".env").read_text()
    assert 'NOMINATIM_PBF_PATH="/nominatim/data/metzingen.osm.pbf"' in env
    assert (stack_dir / "docker-compose.yml").exists()
    # start_stack was not requested: the exact next command is reported.
    assert "docker compose" in routing.next_command
    assert routing.bytes_done == len(payload)


async def test_engine_routing_download_resumes_with_range(tmp_path):
    payload = b"0123456789" * 100
    seen_ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Range", "")
        seen_ranges.append(header)
        if header:
            position = int(header.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=payload[position:])
        return httpx.Response(200, content=payload)

    api = _FakeApi(tmp_path, transport=httpx.MockTransport(handler))
    engine = ProvisionEngine(api, tmp_path / "state")
    stack_dir = tmp_path / "stack"
    # A previous run left half the file behind.
    part = stack_dir / "valhalla_data" / "metzingen.osm.pbf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(payload[:400])

    job = engine.start(ProvisionRequest(
        bbox=METZINGEN, region_id="metzingen", layers=["routing"],
        pbf_url="https://pbf.test/metzingen.osm.pbf",
        routing_stack_dir=str(stack_dir)))
    job = await _finished(engine, job)

    assert job.state == "done"
    assert seen_ranges == ["bytes=400-"]
    assert (stack_dir / "valhalla_data" / "metzingen.osm.pbf").read_bytes() == payload


async def test_engine_refuses_oversized_custom_bbox_routing_without_url(tmp_path):
    api = _FakeApi(tmp_path)
    engine = ProvisionEngine(api, tmp_path / "state")
    job = engine.start(ProvisionRequest(
        bbox=BBoxDeg(west=6.0, south=47.5, east=14.9, north=55.0),
        region_id="toobig", layers=["routing"]))
    job = await _finished(engine, job)
    assert job.state == "failed"
    assert "pbf_url" in job.error


# -- REST surface -------------------------------------------------------------

@pytest.fixture
def rest(tmp_path):
    api = MapsApi(tmp_path, remote_planet=False)
    app = FastAPI()
    api.mount(app)
    # Context-managed on purpose: the engine's background task lives on the
    # client's portal event loop, which only persists across requests inside
    # the `with` block.
    with TestClient(app) as client:
        yield api, client


def test_provision_estimate_is_free_in_every_mode(rest):
    api, client = rest
    api.mode = "mixed"
    response = client.post("/api/v1/maps/provision/estimate", json={
        "region": "de", "layers": ["aerial"], "aerial_max_zoom": 14})
    assert response.status_code == 200
    body = response.json()
    assert body["total_bytes"] == pytest.approx(3.6e9, rel=0.10)
    assert "tiles" in json.dumps(body)


def test_provision_start_is_gated_to_online_mode(rest):
    api, client = rest
    api.mode = "mixed"
    response = client.post("/api/v1/maps/provision", json={
        "region": "de", "layers": ["maps"]})
    assert response.status_code == 403
    assert "online mode" in response.json()["detail"]


def test_provision_rejects_unknown_region_cleanly(rest):
    _, client = rest
    response = client.post("/api/v1/maps/provision/estimate", json={
        "region": "atlantis", "layers": ["maps"]})
    assert response.status_code == 400
    assert "atlantis" in response.json()["detail"]


def test_provision_job_lifecycle_over_rest(rest, monkeypatch):
    api, client = rest

    async def fake_tile(source_id, coord):
        return b"\xff\xd8tile"

    monkeypatch.setattr(api, "fetch_aerial_band_tile", fake_tile)
    started = client.post("/api/v1/maps/provision", json={
        "bbox": {"west": 9.27, "south": 48.53, "east": 9.30, "north": 48.55},
        "region_id": "metzingen", "layers": ["aerial"], "aerial_max_zoom": 13})
    assert started.status_code == 200
    job_id = started.json()["id"]

    listed = client.get("/api/v1/maps/provision").json()
    assert [j["id"] for j in listed] == [job_id]

    import time as _time

    for _ in range(200):
        status = client.get(f"/api/v1/maps/provision/{job_id}").json()
        if status["state"] not in ("pending", "running"):
            break
        _time.sleep(0.02)
    assert status["state"] == "done"
    aerial = status["layers"][0]
    assert aerial["tiles_done"] == aerial["tiles_total"] > 0

    assert client.get("/api/v1/maps/provision/provision-missing").status_code == 404
    assert client.post("/api/v1/maps/provision/provision-missing/cancel").status_code == 404
