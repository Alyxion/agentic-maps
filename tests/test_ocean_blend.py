"""Ocean-blended world imagery: land mask, compositing, presets, ladder wiring.

The `blue-marble-plus` source keeps NextGeneration's land and takes its ocean
pixels from the GIBS bathymetry layer, separated by the Natural Earth land
polygons rasterized in `geo/landmask.py`. These tests run against synthetic
polygons and mock transports — no network, deterministic pixels.
"""

import io
from pathlib import Path

import httpx
from PIL import Image

from agentic_maps.geo.landmask import LandMask
from agentic_maps.harvest.harvester import Harvester
from agentic_maps.models.map_payload import MapPayload
from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.models.tile_source import TileSource
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.sources.presets import builtin_sources

# A polygon covering the western hemisphere: at tile z0/0/0 the left half of
# the image is land, the right half ocean, with the "coast" on the x=128
# pixel column — a known edge to probe the feather against.
_WEST_HEMISPHERE = [[(-180.0, -85.0), (0.0, -85.0), (0.0, 85.0), (-180.0, 85.0)]]


def _solid_jpeg(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def _xyz(source_id: str, host: str) -> TileSource:
    return TileSource(
        id=source_id, name=source_id, kind="xyz",
        url=f"https://{host}/{{z}}/{{y}}/{{x}}.jpeg",
        tile_format="jpeg", min_zoom=0, max_zoom=8,
        attribution="NASA / EOSDIS GIBS",
        license_name="NASA imagery — public domain",
        license_url="https://example.test/pd",
    )


def _blend_harvester(
    mask: LandMask, *, fail_hosts: set[str] = frozenset(), calls: list[str] | None = None
) -> Harvester:
    land = _xyz("land-test", "land.test")
    ocean = _xyz("ocean-test", "ocean.test")

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.host)
        if request.url.host in fail_hosts:
            return httpx.Response(500, text="boom")
        color = (255, 0, 0) if request.url.host == "land.test" else (0, 0, 255)
        return httpx.Response(200, content=_solid_jpeg(color),
                              headers={"content-type": "image/jpeg"})

    return Harvester(
        land, ocean_source=ocean, land_mask=mask,
        transport=httpx.MockTransport(handler),
    )


async def _fetch(harvester: Harvester, coord: TileCoord) -> bytes:
    async with httpx.AsyncClient(transport=harvester.transport) as client:
        return await harvester.fetch_tile(client, coord)


# -- mask generation ------------------------------------------------------

def test_mask_land_ocean_and_feathered_coast():
    mask = LandMask(polygons=[_WEST_HEMISPHERE]).tile_mask(0, 0, 0)
    assert mask.size == (256, 256)
    assert mask.getpixel((40, 128)) == 255           # deep inside land
    assert mask.getpixel((220, 128)) == 0            # open ocean
    # The coast at x=128 carries a soft ramp, not a hard step: some pixel in
    # the transition band must be genuinely intermediate.
    band = [mask.getpixel((x, 128)) for x in range(125, 132)]
    assert any(0 < v < 255 for v in band)
    # ... and the ramp is tight (~1-2 px feather, not a smear): five pixels
    # away from the edge both sides are fully resolved again.
    assert mask.getpixel((122, 128)) == 255
    assert mask.getpixel((134, 128)) == 0


def test_mask_holes_are_ocean_unless_another_polygon_fills_them():
    # Outer square with a hole; a second polygon (the "Lesotho case") sits
    # inside the hole and must stay land despite the surrounding hole.
    outer = [
        [(-170.0, -80.0), (170.0, -80.0), (170.0, 80.0), (-170.0, 80.0)],
        [(-40.0, -40.0), (40.0, -40.0), (40.0, 40.0), (-40.0, 40.0)],   # hole
    ]
    enclave = [[(-10.0, -12.0), (10.0, -12.0), (10.0, 12.0), (-10.0, 12.0)]]
    mask = LandMask(polygons=[outer, enclave]).tile_mask(0, 0, 0)
    assert mask.getpixel((20, 128)) == 255           # outer polygon body
    assert mask.getpixel((110, 128)) == 0            # inside the hole
    assert mask.getpixel((128, 128)) == 255          # enclave fills the hole
    # Same layout WITHOUT the enclave: the hole stays ocean throughout.
    without = LandMask(polygons=[outer]).tile_mask(0, 0, 0)
    assert without.getpixel((128, 128)) == 0


def test_mask_loads_real_country_polygons_when_present():
    path = Path(__file__).resolve().parent.parent / "var" / "geo" / "countries.geojson"
    if not path.exists():
        return  # optional asset; the synthetic tests above carry the logic
    mask = LandMask(path).tile_mask(5, 16, 10)       # z5 central Europe
    assert mask.getpixel((227, 218)) == 255          # inland Germany (~50N 10E)
    mask_atlantic = LandMask(path).tile_mask(5, 11, 12)  # mid-Atlantic
    assert mask_atlantic.getpixel((128, 128)) == 0


# -- compositing ----------------------------------------------------------

async def test_blend_takes_land_and_ocean_from_their_layers():
    mask = LandMask(polygons=[_WEST_HEMISPHERE])
    data = await _fetch(_blend_harvester(mask), TileCoord(z=0, x=0, y=0))
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert data.startswith(b"\xff\xd8")              # cached as JPEG like peers
    r, g, b = image.getpixel((40, 128))
    assert r > 200 and b < 60                        # land pixel: land layer red
    r, g, b = image.getpixel((220, 128))
    assert b > 200 and r < 60                        # ocean pixel: bathy blue
    # The feathered coast mixes both layers instead of jumping.
    edge = [image.getpixel((x, 128)) for x in range(126, 131)]
    assert any(60 < r < 200 and 60 < b < 200 for r, _, b in edge)


async def test_blend_single_fetch_for_all_land_and_all_ocean_tiles():
    calls: list[str] = []
    mask = LandMask(polygons=[_WEST_HEMISPHERE])
    # z2/0/1: entirely inside the western-hemisphere polygon -> land only.
    data = await _fetch(_blend_harvester(mask, calls=calls), TileCoord(z=2, x=0, y=1))
    assert calls == ["land.test"]
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert image.getpixel((128, 128))[0] > 200
    # z2/3/1: entirely ocean -> one fetch against the bathymetry layer.
    calls.clear()
    data = await _fetch(_blend_harvester(mask, calls=calls), TileCoord(z=2, x=3, y=1))
    assert calls == ["ocean.test"]
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert image.getpixel((128, 128))[2] > 200


async def test_blend_degrades_to_land_layer_when_ocean_fetch_fails():
    mask = LandMask(polygons=[_WEST_HEMISPHERE])
    harvester = _blend_harvester(mask, fail_hosts={"ocean.test"})
    data = await _fetch(harvester, TileCoord(z=0, x=0, y=0))
    image = Image.open(io.BytesIO(data)).convert("RGB")
    # Whole tile from the land layer — the pre-blend look, not an error.
    assert image.getpixel((220, 128))[0] > 200


# -- presets & attribution ------------------------------------------------

def test_bathymetry_and_blend_presets():
    sources = builtin_sources()
    bathy = sources["blue-marble-bathy"]
    # Layer identifier + tiling verified against the GIBS WMTS capabilities.
    assert "BlueMarble_ShadedRelief_Bathymetry" in bathy.url
    assert "GoogleMapsCompatible_Level8" in bathy.url
    assert (bathy.min_zoom, bathy.max_zoom) == (0, 8)
    assert "public domain" in bathy.license_name
    assert bathy.attribution

    blend = sources["blue-marble-plus"]
    assert blend.ocean_blend_source_id == "blue-marble-bathy"
    assert "BlueMarble_NextGeneration" in blend.url    # land layer = fallback
    assert (blend.min_zoom, blend.max_zoom) == (0, 8)
    # One combined credit line naming both layers, EOSDIS GIBS exactly once.
    assert "Next Generation" in blend.attribution
    assert "Bathymetry" in blend.attribution
    assert blend.attribution.count("EOSDIS GIBS") == 1
    # The plain preset stays available untouched.
    assert sources["blue-marble"].ocean_blend_source_id is None


# -- ladder wiring --------------------------------------------------------

def _api(tmp_path: Path) -> MapsApi:
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    return MapsApi(bundles, remote_planet=False)


def test_payload_world_layer_is_the_blended_source(tmp_path, two_stop_spec):
    api = _api(tmp_path)
    api.sources["test-wms"] = TileSource(
        id="test-wms", name="t", kind="wms", url="https://example.test/wms",
        wms_layers="rgb", min_zoom=11, max_zoom=17,
        attribution="© Test", license_name="L", license_url="https://example.test/l",
    )
    payload: MapPayload = api.build_payload(two_stop_spec)
    assert payload.world_tiles_url_template == "/api/v1/maps/live/blue-marble-plus/{z}/{x}/{y}"
    # The z8 handoff to Sentinel is unchanged: same max zoom as plain
    # blue-marble, so the ladder bands stay exactly where they were.
    assert payload.world_max_zoom == 8


def test_blend_kwargs_resolve_ocean_source_and_mask(tmp_path):
    api = _api(tmp_path)
    kwargs = api._blend_kwargs(api.sources["blue-marble-plus"])
    if api.land_mask.available:
        assert kwargs["ocean_source"].id == "blue-marble-bathy"
        assert kwargs["land_mask"] is api.land_mask
    else:
        assert kwargs == {}                      # degrade, never half-wire
    # Non-blending sources stay untouched.
    assert api._blend_kwargs(api.sources["blue-marble"]) == {}
