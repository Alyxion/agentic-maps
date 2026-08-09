"""Streets come from the remote planet archive one tile at a time.

The behaviour these pin down is why browsing no longer mints a regional
extract: a tile nobody has locally is read out of the 137 GB archive over a
byte-range request, cached, and served — so looking at a city centre costs the
tiles on screen instead of tens of megabytes.
"""

import gzip

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pmtiles.tile import (
    Compression,
    TileType,
    serialize_directory,
    serialize_header,
    zxy_to_tileid,
)

from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.vector.remote_pmtiles import RemotePMTiles, RemotePMTilesError

_TILE = (14, 8802, 5373)          # Berlin Mitte
_PAYLOAD = b"\x1a\x0bfake-mvt-body"


def _planet_archive() -> bytes:
    """Smallest possible real PMTiles v3 archive holding one tile."""
    body = gzip.compress(_PAYLOAD)
    from pmtiles.tile import Entry

    directory = serialize_directory([Entry(zxy_to_tileid(*_TILE), 0, len(body), 1)])
    header_length = 127
    header = serialize_header({
        "root_offset": header_length,
        "root_length": len(directory),
        "metadata_offset": header_length + len(directory),
        "metadata_length": 0,
        "leaf_directory_offset": header_length + len(directory),
        "leaf_directory_length": 0,
        "tile_data_offset": header_length + len(directory),
        "tile_data_length": len(body),
        "addressed_tiles_count": 1,
        "tile_entries_count": 1,
        "tile_contents_count": 1,
        "clustered": True,
        "internal_compression": Compression.GZIP,
        "tile_compression": Compression.GZIP,
        "tile_type": TileType.MVT,
        "min_zoom": 0,
        "max_zoom": 15,
        "min_lon_e7": -1800000000,
        "min_lat_e7": -850000000,
        "max_lon_e7": 1800000000,
        "max_lat_e7": 850000000,
        "center_zoom": 0,
        "center_lon_e7": 0,
        "center_lat_e7": 0,
    })
    return header + directory + body


def _range_server(archive: bytes, log: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Range")
        log.append(header)
        if not header:
            return httpx.Response(200, content=archive)
        start, end = header.removeprefix("bytes=").split("-")
        chunk = archive[int(start):int(end) + 1]
        return httpx.Response(206, content=chunk)

    return httpx.MockTransport(handler)


@pytest.fixture
def planet(tmp_path):
    log: list = []
    client = httpx.AsyncClient(transport=_range_server(_planet_archive(), log))
    return RemotePMTiles("https://planet.test/x.pmtiles", client), log


def test_reads_a_single_tile_over_range_requests(planet, anyio_backend=None):
    import asyncio

    remote, log = planet
    data = asyncio.run(remote.get_tile(*_TILE))
    assert data == _PAYLOAD                    # gunzipped for us
    # Every read was a range request — never the whole archive.
    assert log and all(entry and entry.startswith("bytes=") for entry in log)


def test_absent_tile_is_none_not_an_error(planet):
    import asyncio

    remote, _ = planet
    assert asyncio.run(remote.get_tile(14, 1, 1)) is None
    assert asyncio.run(remote.get_tile(19, 8802, 5373)) is None   # beyond max_zoom


def test_server_ignoring_range_is_refused(tmp_path):
    """A 200 means the peer is about to hand us the whole planet."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PMTiles" + b"\x00" * 200)

    remote = RemotePMTiles("https://planet.test/x.pmtiles",
                           httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(RemotePMTilesError):
        asyncio.run(remote.header())


def test_tile_route_falls_through_to_the_planet_and_caches(tmp_path):
    log: list = []
    api = MapsApi(tmp_path, sources={}, composites={})
    api._remote = RemotePMTiles(
        "https://planet.test/x.pmtiles",
        httpx.AsyncClient(transport=_range_server(_planet_archive(), log)),
    )
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)

    z, x, y = _TILE
    first = client.get(f"/api/v1/maps/vector/auto/tiles/{z}/{x}/{y}.mvt")
    assert first.status_code == 200 and first.content == _PAYLOAD
    requests_after_first = len(log)

    second = client.get(f"/api/v1/maps/vector/auto/tiles/{z}/{x}/{y}.mvt")
    assert second.status_code == 200 and second.content == _PAYLOAD
    # Served from the disk cache: the planet was not touched again.
    assert len(log) == requests_after_first
    assert api.vector_cache.get(z, x, y) == _PAYLOAD


def test_empty_answer_is_cached_so_ocean_is_not_re_fetched(tmp_path):
    log: list = []
    api = MapsApi(tmp_path, sources={}, composites={})
    api._remote = RemotePMTiles(
        "https://planet.test/x.pmtiles",
        httpx.AsyncClient(transport=_range_server(_planet_archive(), log)),
    )
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)

    assert client.get("/api/v1/maps/vector/auto/tiles/14/1/1.mvt").status_code == 404
    after = len(log)
    assert client.get("/api/v1/maps/vector/auto/tiles/14/1/1.mvt").status_code == 404
    assert len(log) == after


def test_offline_never_reaches_for_the_planet(tmp_path):
    api = MapsApi(tmp_path, sources={}, composites={}, mode="offline")
    api._remote = RemotePMTiles("https://planet.test/x.pmtiles", httpx.AsyncClient())
    app = FastAPI()
    api.mount(app)

    z, x, y = _TILE
    response = TestClient(app).get(f"/api/v1/maps/vector/auto/tiles/{z}/{x}/{y}.mvt")
    assert response.status_code == 404
