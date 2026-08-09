from agentic_maps.models.tile_coord import TileCoord


def test_at_known_location():
    # Metzingen (48.5386, 9.2925) at z17 — verified against slippy-map math.
    coord = TileCoord.at(48.5386, 9.2925, 17)
    assert (coord.z, coord.x, coord.y) == (17, 68919, 45267)


def test_bbox_3857_matches_reference():
    west, south, east, north = TileCoord(z=17, x=68919, y=45267).bbox_3857()
    assert abs(west - 1034345.87) < 0.5
    assert abs(south - 6196902.76) < 0.5
    assert abs(east - 1034651.61) < 0.5
    assert abs(north - 6197208.51) < 0.5
    assert west < east and south < north


def test_clamped_at_poles_and_dateline():
    top = TileCoord.at(85.05, 179.999, 3)
    assert 0 <= top.x < 8 and 0 <= top.y < 8
