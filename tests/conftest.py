import gzip

import pytest
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from agentic_maps.models.camera_pose import CameraPose
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_location import MapLocation
from agentic_maps.models.map_spec import MapSpec
from agentic_maps.models.tile_source import TileSource


@pytest.fixture
def pmtiles_writer():
    """Injected helper (fixture, not import: the repo root owns `tests.*`)."""
    return _write_pmtiles_archive


def _write_pmtiles_archive(path, tiles, bounds_e7, payload: bytes):
    """Tiny PMTiles archive for tests (gzip tiles, e7 bounds)."""
    with open(path, "wb") as f:
        writer = Writer(f)
        for z, x, y in tiles:
            writer.write_tile(zxy_to_tileid(z, x, y), gzip.compress(payload))
        writer.finalize(
            {
                "tile_type": TileType.MVT,
                "tile_compression": Compression.GZIP,
                "min_lon_e7": bounds_e7[0],
                "min_lat_e7": bounds_e7[1],
                "max_lon_e7": bounds_e7[2],
                "max_lat_e7": bounds_e7[3],
                "center_zoom": 0,
                "center_lon_e7": (bounds_e7[0] + bounds_e7[2]) // 2,
                "center_lat_e7": (bounds_e7[1] + bounds_e7[3]) // 2,
            },
            {"name": path.stem},
        )


@pytest.fixture
def wms_source() -> TileSource:
    return TileSource(
        id="test-wms",
        name="Test WMS",
        kind="wms",
        url="https://example.test/wms",
        wms_layers="rgb",
        tile_format="jpeg",
        min_zoom=11,
        max_zoom=17,
        attribution="© Test, dl-de/by-2-0",
        license_name="Datenlizenz Deutschland – Namensnennung – 2.0",
        license_url="https://www.govdata.de/dl-de/by-2-0",
    )


@pytest.fixture
def two_stop_spec() -> MapSpec:
    return MapSpec(
        id="test-spec",
        source_id="test-wms",
        locations=[
            MapLocation(
                id="hq",
                name="HQ",
                camera=CameraPose(center=LatLon(lat=48.5386, lon=9.2925), zoom=16.5),
            ),
            MapLocation(
                id="airport",
                name="Airport",
                camera=CameraPose(center=LatLon(lat=48.6899, lon=9.2219), zoom=14.0),
            ),
        ],
    )
