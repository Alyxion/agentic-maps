import sqlite3

from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.storage.mbtiles import MBTilesBundle


def test_roundtrip_and_metadata(tmp_path, wms_source):
    bundle = MBTilesBundle.create(tmp_path / "b1.mbtiles", wms_source)
    coord = TileCoord(z=12, x=2181, y=1432)
    assert not bundle.has_tile(coord)

    bundle.put_tile(coord, b"jpegbytes")
    assert bundle.get_tile(coord) == b"jpegbytes"
    assert bundle.tile_count() == 1

    info = bundle.info()
    assert info.source_id == "test-wms"
    assert "dl-de/by-2-0" in info.attribution
    assert info.license_url.endswith("/by-2-0")
    assert info.tile_count == 1
    bundle.close()


def test_rows_stored_in_tms_order(tmp_path, wms_source):
    path = tmp_path / "b2.mbtiles"
    bundle = MBTilesBundle.create(path, wms_source)
    coord = TileCoord(z=3, x=1, y=2)
    bundle.put_tile(coord, b"x")
    bundle.close()

    row = sqlite3.connect(str(path)).execute(
        "SELECT zoom_level, tile_column, tile_row FROM tiles"
    ).fetchone()
    assert row == (3, 1, (1 << 3) - 1 - 2)  # spec-compliant TMS flip


def test_reopen_existing(tmp_path, wms_source):
    path = tmp_path / "b3.mbtiles"
    first = MBTilesBundle.create(path, wms_source)
    first.put_tile(TileCoord(z=5, x=1, y=1), b"d")
    first.close()

    reopened = MBTilesBundle(path)
    assert reopened.get_tile(TileCoord(z=5, x=1, y=1)) == b"d"
    assert reopened.info().attribution == wms_source.attribution
    reopened.close()
