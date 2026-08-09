import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.vector.extractor import ExtractError, VectorExtractor

_BBOX = {"west": 9.55, "south": 52.28, "east": 9.92, "north": 52.47}  # Hannover


def _client(tmp_path, extractor=None, **kwargs):
    # remote_planet off: these exercise what is on disk, and a unit test must
    # not depend on a 137 GB archive being reachable.
    kwargs.setdefault("remote_planet", False)
    api = MapsApi(tmp_path, sources={}, composites={}, extractor=extractor, **kwargs)
    app = FastAPI()
    api.mount(app)
    return TestClient(app), api


def test_bundle_id_is_validated(tmp_path):
    extractor = VectorExtractor(tmp_path)
    assert extractor.bundle_path("hannover").name == "streets-hannover.pmtiles"
    for bad in ("../etc", "Hannover Mitte", ""):
        with pytest.raises(ExtractError):
            extractor.bundle_path(bad)


def test_coverage_reports_best_available_detail(tmp_path, pmtiles_writer):
    # Nationwide overview archive, plus a deeper city region (stand-ins for the
    # real z10 / z15 extracts).
    pmtiles_writer(tmp_path / "streets-germany.pmtiles", [(0, 0, 0), (1, 1, 0)],
                   (56000000, 471000000, 152000000, 552000000), b"{}")
    pmtiles_writer(tmp_path / "streets-berlin.pmtiles", [(0, 0, 0), (1, 1, 0), (2, 2, 1)],
                   (130500000, 523300000, 138000000, 527000000), b"{}")
    client, api = _client(tmp_path)

    hannover = client.get("/api/v1/maps/vector/coverage", params=_BBOX).json()
    berlin = client.get("/api/v1/maps/vector/coverage", params={
        "west": 13.3, "south": 52.45, "east": 13.5, "north": 52.55}).json()
    abroad = client.get("/api/v1/maps/vector/coverage", params={
        "west": 2.2, "south": 48.8, "east": 2.4, "north": 48.9}).json()

    assert hannover["bundle_id"] == "streets-germany"
    assert berlin["bundle_id"] == "streets-berlin"
    assert abroad["max_zoom"] == 0 and abroad["bundle_id"] == ""


def test_guaranteed_zoom_counts_tiles_not_bounding_boxes(tmp_path, pmtiles_writer):
    """The clamp must follow tiles that actually exist, not a bbox comparison.

    This is the "downloaded 25 MB and still no streets" bug. Comparing a
    minted region's stored bounds against the viewport was too strict — the
    bounds are tile-aligned and can land a hair inside the box that was asked
    for, so a region that really did cover the screen reported as not covering
    it. The client then clamped back to the nationwide archive, drew nothing,
    and minted the neighbour again.
    """
    # Shallow archive over the whole area, deep archive over part of it. Its
    # declared bounds deliberately fall just inside the query box.
    pmtiles_writer(tmp_path / "streets-country.pmtiles",
                   [(0, 0, 0)] + [(1, x, y) for x in range(2) for y in range(2)],
                   (56000000, 471000000, 152000000, 552000000), b"{}")
    pmtiles_writer(tmp_path / "streets-city.pmtiles", [(2, 2, 1)],
                   (95000000, 522000000, 99000000, 525000000), b"{}")
    client, _ = _client(tmp_path)

    # z2 tile (2,1) spans 0..90E / 0..66N, so a box inside it is fully served
    # at z2 even though the deep archive's declared bounds are much smaller.
    inside = client.get("/api/v1/maps/vector/coverage", params={
        "west": 9.5, "south": 52.2, "east": 9.9, "north": 52.5}).json()
    assert inside["max_zoom"] == 2
    assert inside["guaranteed_zoom"] == 2

    # A box reaching into the neighbouring z2 tile, which nobody has, falls
    # back to the depth the shallow archive can guarantee everywhere.
    straddling = client.get("/api/v1/maps/vector/coverage", params={
        "west": -5.0, "south": 52.2, "east": 9.9, "north": 52.5}).json()
    assert straddling["max_zoom"] == 2
    assert straddling["guaranteed_zoom"] == 1


def test_extract_endpoint_mints_and_registers_a_bundle(tmp_path, pmtiles_writer, monkeypatch):
    minted = {}

    class FakeExtractor(VectorExtractor):
        async def extract(self, bundle_id, **kwargs):
            minted.update(kwargs, id=bundle_id)
            path = self.bundle_path(bundle_id)
            pmtiles_writer(path, [(0, 0, 0)], (95500000, 522800000, 99200000, 524700000), b"{}")
            return path

    client, api = _client(tmp_path, extractor=FakeExtractor(tmp_path))
    response = client.post("/api/v1/maps/vector/extract", json={"id": "hannover", **_BBOX})

    assert response.status_code == 200
    assert response.json()["id"] == "streets-hannover"
    assert minted["id"] == "hannover" and minted["maxzoom"] == 15
    # Registered immediately, so the very next tile request can serve it.
    assert "streets-hannover" in api._vector


def test_extract_is_blocked_offline(tmp_path):
    client, _ = _client(tmp_path, mode="offline")
    response = client.post("/api/v1/maps/vector/extract", json={"id": "hannover", **_BBOX})
    assert response.status_code == 403


@pytest.mark.parametrize("mode,expect_ok", [("online", True), ("mixed", False), ("offline", False)])
def test_extract_is_a_provisioning_endpoint_blocked_outside_online(tmp_path, pmtiles_writer, mode, expect_ok):
    """`vector/extract` mints a new regional `.pmtiles` — bulk provisioning,
    per `models/runtime_mode.py`, so it is refused in BOTH "mixed" and
    "offline", not just "offline" (unlike a live/per-request endpoint)."""
    class FakeExtractor(VectorExtractor):
        async def extract(self, bundle_id, **kwargs):
            path = self.bundle_path(bundle_id)
            pmtiles_writer(path, [(0, 0, 0)], (95500000, 522800000, 99200000, 524700000), b"{}")
            return path

    client, _ = _client(tmp_path, extractor=FakeExtractor(tmp_path), mode=mode)
    response = client.post("/api/v1/maps/vector/extract", json={"id": "hannover", **_BBOX})
    if expect_ok:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403


def test_extract_failure_surfaces_as_bad_gateway(tmp_path):
    class Failing(VectorExtractor):
        async def extract(self, bundle_id, **kwargs):
            raise ExtractError("pmtiles not installed")

    client, _ = _client(tmp_path, extractor=Failing(tmp_path))
    response = client.post("/api/v1/maps/vector/extract", json={"id": "hannover", **_BBOX})
    assert response.status_code == 502
    assert "pmtiles" in response.json()["detail"]


def test_missing_binary_is_reported_not_crashed(tmp_path):
    extractor = VectorExtractor(tmp_path, planet_url="https://example.test/planet.pmtiles",
                                binary="definitely-not-installed")
    with pytest.raises(ExtractError):
        asyncio.run(extractor.extract("hannover", **_BBOX))
