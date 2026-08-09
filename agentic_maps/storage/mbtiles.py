"""MBTiles bundle store (stdlib sqlite3, no external deps).

MBTiles is the portable container for a harvested tile pyramid: one file per
bundle, metadata (source/attribution/license) inside the file so a bundle never
loses its provenance. The MBTiles spec stores rows in TMS order; the flip is
confined to this module — every public method speaks XYZ.
"""

import sqlite3
import threading
from pathlib import Path

from ..models.bundle_info import BundleInfo
from ..models.tile_coord import TileCoord
from ..models.tile_source import TileSource

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (
  zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
  PRIMARY KEY (zoom_level, tile_column, tile_row)
);
"""


def _tms_row(coord: TileCoord) -> int:
    return (1 << coord.z) - 1 - coord.y


class MBTilesBundle:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    @classmethod
    def create(cls, path: Path, source: TileSource) -> "MBTilesBundle":
        bundle = cls(path)
        bundle._set_meta(
            {
                "name": path.stem,
                "format": source.tile_format,
                "type": "baselayer",
                "source_id": source.id,
                "attribution": source.attribution,
                "license_name": source.license_name,
                "license_url": source.license_url,
                "minzoom": str(source.min_zoom),
                "maxzoom": str(source.max_zoom),
            }
        )
        return bundle

    def _set_meta(self, values: dict[str, str]) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
                list(values.items()),
            )
            self._db.commit()

    def _get_meta(self, name: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def put_tile(self, coord: TileCoord, data: bytes) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data)"
                " VALUES (?, ?, ?, ?)",
                (coord.z, coord.x, _tms_row(coord), data),
            )
            self._db.commit()

    def get_tile(self, coord: TileCoord) -> bytes | None:
        row = self._db.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
            (coord.z, coord.x, _tms_row(coord)),
        ).fetchone()
        return bytes(row[0]) if row else None

    def has_tile(self, coord: TileCoord) -> bool:
        return self.get_tile(coord) is not None

    def tile_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM tiles").fetchone()[0])

    def info(self) -> BundleInfo:
        return BundleInfo(
            id=self.path.stem,
            source_id=self._get_meta("source_id"),
            tile_format=self._get_meta("format", "jpeg"),
            attribution=self._get_meta("attribution"),
            license_name=self._get_meta("license_name"),
            license_url=self._get_meta("license_url"),
            tile_count=self.tile_count(),
            size_bytes=self.path.stat().st_size if self.path.exists() else 0,
        )

    def close(self) -> None:
        self._db.close()
