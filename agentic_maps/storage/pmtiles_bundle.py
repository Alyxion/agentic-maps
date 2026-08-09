"""Read-only access to a PMTiles vector bundle (Protomaps regional extract).

Extracts are produced with the pmtiles CLI (see README) and dropped into the
bundles directory as *.pmtiles. Tiles inside Protomaps builds are
gzip-compressed MVT; we decompress here and serve plain
application/vnd.mapbox-vector-tile.
"""

import gzip
from pathlib import Path

from pmtiles.reader import MmapSource, Reader


class PMTilesBundle:
    def __init__(self, path: Path):
        self.path = path
        self._file = open(path, "rb")
        self._reader = Reader(MmapSource(self._file))

    def get_tile(self, z: int, x: int, y: int) -> bytes | None:
        data = self._reader.get(z, x, y)
        if data is None:
            return None
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data

    def max_zoom(self) -> int:
        return int(self._reader.header()["max_zoom"])

    def min_zoom(self) -> int:
        return int(self._reader.header()["min_zoom"])

    def bounds(self):
        """WGS84 bounds of the extract (BBoxDeg)."""
        from ..models.bbox_deg import BBoxDeg

        header = self._reader.header()
        return BBoxDeg(
            west=header["min_lon_e7"] / 1e7,
            south=header["min_lat_e7"] / 1e7,
            east=header["max_lon_e7"] / 1e7,
            north=header["max_lat_e7"] / 1e7,
        )

    def close(self) -> None:
        self._file.close()
