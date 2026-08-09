import httpx

from agentic_maps.harvest.harvester import Harvester
from agentic_maps.models.harvest_plan import HarvestPlan
from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.models.tile_source import TileSource
from agentic_maps.storage.mbtiles import MBTilesBundle

# Realistic size: anything under BLANK_TILE_MAX_BYTES is treated as a
# "no imagery here" placeholder by the WMS fetch path.
_JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg" * 700


def _image_transport(fail_paths: set[str] = frozenset()) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if any(marker in str(request.url) for marker in fail_paths):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})

    return httpx.MockTransport(handler)


def test_wms_tile_url_uses_mercator_bbox(wms_source):
    url = Harvester(wms_source).tile_url(TileCoord(z=17, x=68919, y=45267))
    assert "REQUEST=GetMap" in url and "CRS=EPSG:3857" in url
    assert "LAYERS=rgb" in url and "WIDTH=256" in url
    assert "BBOX=1034345.8668" in url


def test_xyz_tile_url_template():
    source = TileSource(
        id="xyz-test", name="XYZ", kind="xyz",
        url="https://tiles.test/{z}/{x}/{y}.png", tile_format="png",
        min_zoom=0, max_zoom=15,
        attribution="© Test", license_name="ODbL", license_url="https://example.test/odbl",
    )
    assert Harvester(source).tile_url(TileCoord(z=3, x=4, y=5)) == "https://tiles.test/3/4/5.png"


async def test_harvest_counts_and_skip_existing(tmp_path, wms_source):
    tiles = [TileCoord(z=12, x=2181, y=1432 + i) for i in range(4)]
    plan = HarvestPlan(spec_id="s", source_id=wms_source.id, tiles=tiles)
    bundle = MBTilesBundle.create(tmp_path / "h.mbtiles", wms_source)
    bundle.put_tile(tiles[0], b"already-there")

    harvester = Harvester(wms_source, transport=_image_transport())
    report = await harvester.harvest(plan, bundle)

    assert report.planned == 4
    assert report.skipped_existing == 1
    assert report.fetched == 3
    assert report.failed == 0
    assert report.complete
    assert bundle.get_tile(tiles[0]) == b"already-there"  # incremental, not overwritten
    assert bundle.get_tile(tiles[1]) == _JPEG

    # Re-harvest is a no-op.
    second = await harvester.harvest(plan, bundle)
    assert second.fetched == 0 and second.skipped_existing == 4


async def test_harvest_counts_failures_without_aborting(tmp_path, wms_source):
    tiles = [TileCoord(z=12, x=2181, y=1432), TileCoord(z=12, x=2181, y=1433)]
    plan = HarvestPlan(spec_id="s", source_id=wms_source.id, tiles=tiles)
    bundle = MBTilesBundle.create(tmp_path / "f.mbtiles", wms_source)

    west, south, _, _ = tiles[0].bbox_3857()
    fail_marker = f"BBOX={west:.4f},{south:.4f}"  # unique to tiles[0] (neighbors share single edges)
    harvester = Harvester(wms_source, transport=_image_transport(fail_paths={fail_marker}))
    report = await harvester.harvest(plan, bundle)

    assert report.failed == 1
    assert report.fetched == 1
    assert not report.complete
