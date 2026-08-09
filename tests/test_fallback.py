import io

from PIL import Image

from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.storage.fallback import FallbackTileResolver
from agentic_maps.storage.mbtiles import MBTilesBundle


def _jpeg(color) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(out, format="JPEG")
    return out.getvalue()


def test_exact_hit_passthrough(tmp_path, wms_source):
    bundle = MBTilesBundle.create(tmp_path / "b.mbtiles", wms_source)
    coord = TileCoord(z=14, x=100, y=200)
    bundle.put_tile(coord, b"rawbytes")
    assert FallbackTileResolver(bundle).resolve(coord) == (b"rawbytes", "exact")


def test_parent_quadrant_upscale(tmp_path, wms_source):
    bundle = MBTilesBundle.create(tmp_path / "b.mbtiles", wms_source)
    parent = TileCoord(z=13, x=50, y=100)
    bundle.put_tile(parent, _jpeg((200, 40, 40)))

    child = TileCoord(z=14, x=101, y=200)  # inside that parent
    resolved = FallbackTileResolver(bundle).resolve(child)
    assert resolved is not None
    data, kind = resolved
    assert kind == "parent"
    image = Image.open(io.BytesIO(data))
    assert image.size == (256, 256)
    r, g, b = image.getpixel((128, 128))
    assert r > 150 and g < 90 and b < 90  # reddish parent content survived


def test_children_merge_when_no_parent(tmp_path, wms_source):
    bundle = MBTilesBundle.create(tmp_path / "b.mbtiles", wms_source)
    target = TileCoord(z=12, x=10, y=10)
    bundle.put_tile(TileCoord(z=13, x=20, y=20), _jpeg((30, 160, 30)))
    bundle.put_tile(TileCoord(z=13, x=21, y=21), _jpeg((30, 160, 30)))

    resolved = FallbackTileResolver(bundle, max_parent_levels=0).resolve(target)
    assert resolved is not None
    data, kind = resolved
    assert kind == "children"
    assert Image.open(io.BytesIO(data)).size == (256, 256)


def test_no_data_returns_none(tmp_path, wms_source):
    bundle = MBTilesBundle.create(tmp_path / "b.mbtiles", wms_source)
    assert FallbackTileResolver(bundle).resolve(TileCoord(z=14, x=1, y=1)) is None
