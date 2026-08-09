"""Read single tiles out of a remote PMTiles archive over HTTP range requests.

The Protomaps planet build is ~137 GB and answers `Range:` with 206, which is
the whole point of the format: a header, a directory tree, and tile data laid
out so a reader can binary-search the directory and then fetch the byte range
of exactly one tile.

That is what this does, and it is why browsing no longer needs a regional
extract. Minting a 25 MB region to look at four tiles was the wrong unit of
work; extracts remain what they were designed for — sealing an offline package
(docs/concept.md §5).

Two round trips at most for a cold tile (root directory, then a leaf), one for
a warm one, because directories are cached. The tile bytes themselves land in
the normal local bundle cache above this, so a second visit costs nothing.
"""

import asyncio
import gzip
from collections import OrderedDict

import httpx
from pmtiles.tile import deserialize_directory, deserialize_header, find_tile, zxy_to_tileid

# The header is 127 bytes and the root directory almost always follows inside
# the first 16 KB, so one range request usually yields both.
_HEADER_PROBE_BYTES = 16384
_MAX_DIRECTORY_DEPTH = 4      # per the PMTiles v3 spec
_DIRECTORY_CACHE_ENTRIES = 128


class RemotePMTilesError(RuntimeError):
    pass


class RemotePMTiles:
    """One remote archive. Safe to share: directory reads are serialised."""

    def __init__(self, url: str, client: httpx.AsyncClient, *, timeout: float = 20.0):
        self.url = url
        self._client = client
        self._timeout = timeout
        self._header: dict | None = None
        self._root: list | None = None
        self._directories: OrderedDict[tuple[int, int], list] = OrderedDict()
        self._lock = asyncio.Lock()

    async def _range(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        response = await self._client.get(
            self.url,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
            timeout=self._timeout,
        )
        # 206 is the expected answer; a 200 means the server ignored the range
        # and is about to hand us 137 GB, which we must not accept.
        if response.status_code == 200:
            raise RemotePMTilesError(f"{self.url} ignored Range (served the whole file)")
        if response.status_code != 206:
            raise RemotePMTilesError(f"{self.url} range request failed: HTTP {response.status_code}")
        return response.content

    async def header(self) -> dict:
        if self._header is None:
            async with self._lock:
                if self._header is None:
                    probe = await self._range(0, _HEADER_PROBE_BYTES)
                    if not probe.startswith(b"PMTiles"):
                        raise RemotePMTilesError(f"{self.url} is not a PMTiles archive")
                    header = deserialize_header(probe[:127])
                    root_offset = header["root_offset"]
                    root_length = header["root_length"]
                    if root_offset + root_length <= len(probe):
                        root = probe[root_offset:root_offset + root_length]
                    else:
                        root = await self._range(root_offset, root_length)
                    self._root = deserialize_directory(root)
                    self._header = header
        return self._header

    async def _directory(self, offset: int, length: int) -> list:
        key = (offset, length)
        cached = self._directories.get(key)
        if cached is not None:
            self._directories.move_to_end(key)
            return cached
        entries = deserialize_directory(await self._range(offset, length))
        self._directories[key] = entries
        self._directories.move_to_end(key)
        while len(self._directories) > _DIRECTORY_CACHE_ENTRIES:
            self._directories.popitem(last=False)
        return entries

    async def get_tile(self, z: int, x: int, y: int) -> bytes | None:
        """Decompressed MVT for one tile, or None when the archive has no such
        tile (ocean, out of bounds, above max zoom)."""
        header = await self.header()
        if z < header["min_zoom"] or z > header["max_zoom"]:
            return None
        tile_id = zxy_to_tileid(z, x, y)

        entries = self._root
        for _ in range(_MAX_DIRECTORY_DEPTH):
            entry = find_tile(entries, tile_id)
            if entry is None:
                return None
            if entry.run_length == 0:
                # A leaf directory rather than a tile: descend.
                entries = await self._directory(
                    header["leaf_directory_offset"] + entry.offset, entry.length
                )
                continue
            data = await self._range(header["tile_data_offset"] + entry.offset, entry.length)
            # Protomaps stores MVT gzip-compressed; the rest of the stack
            # expects plain vector tiles, same as PMTilesBundle.
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        raise RemotePMTilesError(f"{self.url}: directory deeper than {_MAX_DIRECTORY_DEPTH}")

    @property
    def max_zoom(self) -> int:
        if self._header is None:
            raise RemotePMTilesError("header not read yet")
        return int(self._header["max_zoom"])
