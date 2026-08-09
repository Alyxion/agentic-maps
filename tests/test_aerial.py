"""The unified aerial endpoint (`GET /aerial/{src}/{z}/{x}/{y}`).

One tile URL for the whole imagery ladder: the dispatcher owns the band
choice per zoom (world z0-8, regional z9-12, fine z13+ — mirroring the real
blue-marble / sen2-europe / de-dop split), reuses the `/live` cache-through
internals byte-for-byte, and falls through to a parent-crop upscale of the
coarsest covering band instead of 204ing where the owning band has no
imagery. `/attribution` follows the same fallthrough.
"""

import io
import random

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from agentic_maps.models.bbox_deg import BBoxDeg
from agentic_maps.models.composite_source import CompositeSource
from agentic_maps.models.tile_source import TileSource
from agentic_maps.rest.maps_api import MapsApi

WORLD_RGB = (20, 40, 90)
MID_RGB = (40, 120, 40)
FINE_RGB = (150, 40, 40)

_NOISE = 0.18   # blend factor toward random noise (see _jpeg)
_TILES: dict[tuple[int, int, int], bytes] = {}


def _jpeg(color: tuple[int, int, int]) -> bytes:
    """A colour-dominated NOISE tile: real imagery heuristics reject
    solid-colour placeholders (`_has_imagery` wants > 16 distinct colours),
    and noise also keeps the JPEG well past the blank-tile byte floor."""
    if color not in _TILES:
        rng = random.Random(sum(color))
        noise = Image.frombytes("RGB", (256, 256), rng.randbytes(256 * 256 * 3))
        image = Image.blend(Image.new("RGB", (256, 256), color), noise, _NOISE)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=95)
        _TILES[color] = out.getvalue()
    return _TILES[color]


def _expected(color: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(round(c * (1 - _NOISE) + 127.5 * _NOISE) for c in color)


def _wms(source_id: str, host: str, min_zoom: int, max_zoom: int,
         coverage: tuple[float, float, float, float]) -> TileSource:
    west, south, east, north = coverage
    return TileSource(
        id=source_id, name=source_id, kind="wms",
        url=f"https://{host}/wms", wms_layers="rgb", tile_format="jpeg",
        min_zoom=min_zoom, max_zoom=max_zoom,
        attribution=f"© {source_id}", license_name="test", license_url="https://example.test",
        coverage=BBoxDeg(west=west, south=south, east=east, north=north),
    )


@pytest.fixture
def api(tmp_path):
    # Named "blue-marble" so MapsApi._world_source() picks it up, exactly
    # like the shipping preset ladder.
    world = TileSource(
        id="blue-marble", name="world", kind="xyz",
        url="https://world.test/{z}/{x}/{y}.jpg", tile_format="jpeg",
        min_zoom=0, max_zoom=8,
        attribution="© world", license_name="test", license_url="https://example.test",
    )
    mid = _wms("mid", "mid.test", 8, 12, (5.0, 45.0, 15.0, 55.0))
    fine = _wms("fine", "fine.test", 13, 19, (8.0, 47.0, 12.0, 53.0))
    composite = CompositeSource(id="imagery", name="imagery",
                                member_ids=["fine", "mid"], min_zoom=8)
    api = MapsApi(
        tmp_path,
        sources={"blue-marble": world, "mid": mid, "fine": fine},
        composites={"imagery": composite},
    )

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        color = {"world.test": WORLD_RGB, "mid.test": MID_RGB,
                 "fine.test": FINE_RGB}[request.url.host]
        return httpx.Response(200, content=_jpeg(color),
                              headers={"content-type": "image/jpeg"})

    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api._upstream_calls = calls
    return api


@pytest.fixture
def client(api):
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def _dominant(content: bytes) -> tuple[int, int, int]:
    image = Image.open(io.BytesIO(content)).convert("RGB").resize((1, 1))
    return image.getpixel((0, 0))


def _near(a: tuple[int, int, int], b: tuple[int, int, int], tolerance: int = 16) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


# Inside every coverage box: lon 10 / lat 50.
Z5 = (5, 16, 10)           # world band
Z10_IN = (10, 540, 347)    # mid band, inside its coverage
Z10_OUT = (10, 796, 347)   # lon ~100 — outside mid, world still covers
Z15_IN = (15, 17294, 11113)         # fine band, inside its coverage
Z15_MID_ONLY = (15, 16930, 11113)   # lon ~6 — outside fine, inside mid


def test_band_dispatch_per_zoom(client):
    z, x, y = Z5
    world = client.get(f"/api/v1/maps/aerial/imagery/{z}/{x}/{y}")
    assert world.status_code == 200
    assert world.headers["x-agentic-maps-aerial-band"] == "blue-marble"
    assert "x-agentic-maps-aerial-synth" not in world.headers
    assert _near(_dominant(world.content), _expected(WORLD_RGB))

    z, x, y = Z10_IN
    mid = client.get(f"/api/v1/maps/aerial/imagery/{z}/{x}/{y}")
    assert mid.status_code == 200
    assert mid.headers["x-agentic-maps-aerial-band"] == "imagery"
    assert _near(_dominant(mid.content), _expected(MID_RGB))

    z, x, y = Z15_IN
    fine = client.get(f"/api/v1/maps/aerial/imagery/{z}/{x}/{y}")
    assert fine.status_code == 200
    assert _near(_dominant(fine.content), _expected(FINE_RGB))


def test_out_of_range_zoom_is_404(client):
    assert client.get("/api/v1/maps/aerial/imagery/20/0/0").status_code == 404


def test_coverage_fallthrough_upscales_the_coarser_band(client):
    # z10 outside the regional band's coverage: never a 204 at a
    # hybrid-critical zoom — the world band's z8 ancestor is upscaled.
    z, x, y = Z10_OUT
    response = client.get(f"/api/v1/maps/aerial/imagery/{z}/{x}/{y}")
    assert response.status_code == 200
    assert response.headers["x-agentic-maps-aerial-band"] == "blue-marble"
    assert response.headers["x-agentic-maps-aerial-synth"] == "parent-z8"
    assert _near(_dominant(response.content), _expected(WORLD_RGB))

    # z15 outside the fine band but inside the regional band: the NEAREST
    # coarser covering band wins — sen2-analogue at z12, not the world.
    z, x, y = Z15_MID_ONLY
    response = client.get(f"/api/v1/maps/aerial/imagery/{z}/{x}/{y}")
    assert response.status_code == 200
    assert response.headers["x-agentic-maps-aerial-band"] == "mid"
    assert response.headers["x-agentic-maps-aerial-synth"] == "parent-z12"
    assert _near(_dominant(response.content), _expected(MID_RGB))


def test_unified_and_per_band_proxy_serve_identical_bytes(client, api):
    """Pixel parity at ordinary flat zooms: `/aerial` IS `/live` plus
    dispatch — same cache bundle, so the second endpoint re-serves the very
    bytes the first one cached (and costs zero extra upstream calls)."""
    z, x, y = Z15_IN
    live = client.get(f"/api/v1/maps/live/imagery/{z}/{x}/{y}")
    upstream_after_live = len(api._upstream_calls)
    aerial = client.get(f"/api/v1/maps/aerial/imagery/{z}/{x}/{y}")
    assert live.status_code == aerial.status_code == 200
    assert aerial.content == live.content
    assert len(api._upstream_calls) == upstream_after_live


def test_attribution_follows_the_fallthrough(client):
    def sources_at(z: int, west: float, east: float, south=49.0, north=51.0):
        response = client.get(
            "/api/v1/maps/attribution",
            params={"src": "imagery,blue-marble", "z": z,
                    "west": west, "south": south, "east": east, "north": north})
        return [item["id"] for item in response.json()["sources"]]

    # Inside the fine band's coverage the federation credits it as before.
    assert sources_at(15, 9.9, 10.1) == ["fine"]
    # Outside fine but inside mid at z15: the upscaled pixels are mid's.
    assert sources_at(15, 5.9, 6.1) == ["mid"]
    # Outside every regional box: the world imagery is what is on screen.
    assert "blue-marble" in sources_at(10, 99.0, 101.0)


def _quality(client, west, south, east, north, zoom, src="imagery"):
    return client.get("/api/v1/maps/aerial/quality", params={
        "src": src, "west": west, "south": south,
        "east": east, "north": north, "zoom": zoom})


def test_quality_native_inside_fine_coverage(client):
    # Inside the fine band's box at z15: native to z19, gap 0.
    response = _quality(client, 9.9, 49.9, 10.1, 50.1, 15)
    assert response.status_code == 200
    assert response.json() == {
        "best_native_zoom": 19, "requested_zoom": 15, "gap": 0}


def test_quality_new_york_case_reports_the_gap(client):
    # Far outside every regional box (the owner's New York): only the world
    # band covers, so hybrid at z12 is four zooms of Blue-Marble upscale —
    # exactly the signal the frontend's auto-fallback switches on (gap >= 3).
    response = _quality(client, -74.3, 40.5, -73.7, 40.9, 12)
    assert response.json() == {
        "best_native_zoom": 8, "requested_zoom": 12, "gap": 4}


def test_quality_hysteresis_band_at_the_sen2_edge(client):
    # Outside fine but inside mid (lon ~6): native tops out at z12. At z13
    # the gap is 1 (restore territory), at z15 it is 3 (switch territory) —
    # the two frontend thresholds live between these answers.
    assert _quality(client, 5.9, 49.9, 6.1, 50.1, 13).json()["gap"] == 1
    assert _quality(client, 5.9, 49.9, 6.1, 50.1, 15).json()["gap"] == 3
    # A viewport merely TOUCHING the mid box counts as covered — panning
    # along the edge flips the answer only once the box is fully left.
    assert _quality(client, 4.0, 49.9, 5.5, 50.1, 12).json()["gap"] == 0
    assert _quality(client, 2.0, 49.9, 4.0, 50.1, 12).json()["gap"] == 4


def test_quality_validates_and_caches(client, api):
    assert _quality(client, 10, 50, 9, 51, 12).status_code == 400   # west >= east
    assert _quality(client, 9, 50, 10, 51, 12, src="nope").status_code == 404
    api._aerial_quality_cache.clear()
    first = _quality(client, 9.9, 49.9, 10.1, 50.1, 15).json()
    assert len(api._aerial_quality_cache) == 1
    # Same rounded bbox: answered from the cache, byte-identical.
    assert _quality(client, 9.901, 49.901, 10.101, 50.101, 15).json() == first
    assert len(api._aerial_quality_cache) == 1
    # No mode gating — the check is pure config math and must keep working
    # in offline presentations too.
    api.mode = "offline"
    assert _quality(client, 9.9, 49.9, 10.1, 50.1, 15).status_code == 200
    api.mode = "online"


def test_payload_carries_the_unified_template_online_only(api, tmp_path):
    from agentic_maps.models.camera_pose import CameraPose
    from agentic_maps.models.lat_lon import LatLon
    from agentic_maps.models.map_spec import MapSpec

    spec = MapSpec(
        id="aerial-spec", source_id="imagery",
        overview=CameraPose(center=LatLon(lat=50.0, lon=10.0), zoom=6.0),
        locations=[],
    )
    payload = api.build_payload(spec)
    assert payload.aerial_url_template == "/api/v1/maps/aerial/imagery/{z}/{x}/{y}"
    assert payload.aerial_max_zoom == 19

    api.mode = "offline"
    sealed = api.build_payload(spec)
    # Offline keeps the per-band bundle contract; no live dispatcher URL.
    assert sealed.aerial_url_template is None
    assert sealed.tiles_url_template.startswith("/api/v1/maps/bundles/")
