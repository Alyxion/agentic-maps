"""GET /vector/features — bounded, selective feature extraction.

The fixture tiles here are REAL Mapbox Vector Tiles, encoded by hand at the
protobuf wire level (the same twenty lines `vector/mvt.py` decodes), not
opaque fake payloads: the endpoint's whole job is decoding, so the tests
must feed it something decodable — including a building height stored as a
protobuf *double*, the encoding that used to be silently dropped.
"""

import struct

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.rest.maps_api import FEATURE_TILES_CAP, MapsApi

# Metzingen — inside the coverage box the fixture archive declares.
_LAT, _LON, _ZOOM = 48.5386, 9.2925, 15


# -- minimal MVT wire-format encoder ----------------------------------------

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _varint_field(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def _zig(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _value_str(text: str) -> bytes:
    return _bytes_field(1, text.encode())


def _value_double(value: float) -> bytes:
    return _key(3, 1) + struct.pack("<d", value)


def _feature(feature_id: int | None, geom_type: int, commands: list[int],
             tags: list[int]) -> bytes:
    msg = b"" if feature_id is None else _varint_field(1, feature_id)
    msg += _bytes_field(2, b"".join(_varint(tag) for tag in tags))
    msg += _varint_field(3, geom_type)
    msg += _bytes_field(4, b"".join(_varint(c) for c in commands))
    return msg


def _layer(name: str, keys: list[str], values: list[bytes],
           features: list[bytes]) -> bytes:
    msg = _varint_field(15, 2) + _bytes_field(1, name.encode())
    for feature in features:
        msg += _bytes_field(2, feature)
    for key in keys:
        msg += _bytes_field(3, key.encode())
    for value in values:
        msg += _bytes_field(4, value)
    msg += _varint_field(5, 4096)
    return _bytes_field(3, msg)          # Tile.layers is field 3


def _square(x: int, y: int, size: int) -> list[int]:
    """MoveTo + 3 LineTo + ClosePath — one building footprint."""
    return [
        (1 << 3) | 1, _zig(x), _zig(y),                       # MoveTo x1
        (3 << 3) | 2, _zig(size), _zig(0), _zig(0), _zig(size), _zig(-size), _zig(0),
        (1 << 3) | 7,                                          # ClosePath
    ]


def _tile_payload(building_id: int = 7) -> bytes:
    """One tile with a building (double height), a road and a POI."""
    buildings = _layer(
        "buildings", ["height", "min_height"],
        [_value_double(11.5), _value_double(0.0)],
        [_feature(building_id, 3, _square(1000, 1000, 200), [0, 0, 1, 1])],
    )
    roads = _layer(
        "roads", ["name", "kind"],
        [_value_str("Fabriciusstraße"), _value_str("residential")],
        [_feature(31, 2, [(1 << 3) | 1, _zig(500), _zig(500),
                          (2 << 3) | 2, _zig(800), _zig(100), _zig(700), _zig(-50)],
                  [0, 0, 1, 1])],
    )
    pois = _layer(
        "pois", ["name", "kind"],
        [_value_str("Bäckerei am Markt"), _value_str("bakery")],
        [_feature(55, 1, [(1 << 3) | 1, _zig(2000), _zig(2000)], [0, 0, 1, 1])],
    )
    return buildings + roads + pois


def _bounds_e7(*coords: TileCoord) -> tuple[int, int, int, int]:
    boxes = [c.bbox_deg() for c in coords]
    return (
        int(min(b[0] for b in boxes) * 1e7) - 10,
        int(min(b[1] for b in boxes) * 1e7) - 10,
        int(max(b[2] for b in boxes) * 1e7) + 10,
        int(max(b[3] for b in boxes) * 1e7) + 10,
    )


@pytest.fixture
def home_tile() -> TileCoord:
    return TileCoord.at(_LAT, _LON, _ZOOM)


@pytest.fixture
def api(tmp_path, wms_source, pmtiles_writer, home_tile):
    east_tile = TileCoord(z=_ZOOM, x=home_tile.x + 1, y=home_tile.y)
    pmtiles_writer(
        tmp_path / "streets-fixture.pmtiles",
        [(home_tile.z, home_tile.x, home_tile.y),
         (east_tile.z, east_tile.x, east_tile.y)],
        _bounds_e7(home_tile, east_tile),
        _tile_payload(),
    )
    # remote_planet off: these tests must not touch the 137 GB archive.
    return MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                   remote_planet=False)


@pytest.fixture
def client(api):
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def _inner_box(tile: TileCoord) -> dict:
    """A bbox strictly inside one tile, so covering-tile math stays at 1."""
    west, south, east, north = tile.bbox_deg()
    dx, dy = (east - west) * 0.1, (north - south) * 0.1
    return {"west": west + dx, "south": south + dy, "east": east - dx, "north": north - dy}


def test_buildings_selective_with_double_height(client, home_tile):
    response = client.get("/api/v1/maps/vector/features",
                          params={**_inner_box(home_tile), "layers": "buildings"})
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["meta"]["zoom"] == 15                # auto zoom picked z15
    assert body["meta"]["layer_counts"] == {"buildings": 1}
    building = body["features"][0]
    assert building["geometry"]["type"] == "Polygon"
    assert building["id"] == 7
    # Heights are protobuf doubles in real tiles — decoded, not dropped.
    assert building["properties"]["height"] == pytest.approx(11.5)
    assert building["properties"]["layer"] == "buildings"
    # Selective means selective: roads/pois were in the tile, not the answer.
    assert all(f["properties"]["layer"] == "buildings" for f in body["features"])


def test_all_layers_and_geometry_types(client, home_tile):
    body = client.get("/api/v1/maps/vector/features",
                      params={**_inner_box(home_tile),
                              "layers": "buildings,roads,pois"}).json()
    kinds = {f["properties"]["layer"]: f["geometry"]["type"] for f in body["features"]}
    assert kinds == {"buildings": "Polygon", "roads": "LineString", "pois": "Point"}
    poi = next(f for f in body["features"] if f["properties"]["layer"] == "pois")
    assert poi["properties"]["name"] == "Bäckerei am Markt"
    assert body["meta"]["tiles_with_data"] == 1


def test_bbox_too_large_is_refused_with_guidance(client):
    response = client.get("/api/v1/maps/vector/features",
                          params={"west": 6.0, "south": 47.0, "east": 15.0,
                                  "north": 55.0, "layers": "buildings", "zoom": 15})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert str(FEATURE_TILES_CAP) in detail
    assert "shrink" in detail


def test_auto_zoom_refusal_names_the_fallback_floor(client):
    # No explicit zoom: auto tries z15 then z14 and refuses below that.
    response = client.get("/api/v1/maps/vector/features",
                          params={"west": 6.0, "south": 47.0, "east": 15.0,
                                  "north": 55.0, "layers": "buildings"})
    assert response.status_code == 400
    assert "z14" in response.json()["detail"]


def test_same_feature_id_across_tile_border_dedupes(client, home_tile):
    west, south, _, north = home_tile.bbox_deg()
    east_tile = TileCoord(z=_ZOOM, x=home_tile.x + 1, y=home_tile.y)
    _, _, east, _ = east_tile.bbox_deg()
    dx = (east - west) * 0.05
    body = client.get("/api/v1/maps/vector/features",
                      params={"west": west + dx, "south": south + dx,
                              "east": east - dx, "north": north - dx,
                              "layers": "buildings"}).json()
    # The same building id sits in both tiles; the answer carries it once.
    assert body["meta"]["tiles_with_data"] == 2
    assert body["meta"]["layer_counts"] == {"buildings": 1}
    assert [f["id"] for f in body["features"]] == [7]


def test_limit_truncates_and_says_so(client, home_tile):
    body = client.get("/api/v1/maps/vector/features",
                      params={**_inner_box(home_tile),
                              "layers": "buildings,roads,pois", "limit": 1}).json()
    assert len(body["features"]) == 1
    assert body["meta"]["truncated"] is True
    assert body["meta"]["limit"] == 1
