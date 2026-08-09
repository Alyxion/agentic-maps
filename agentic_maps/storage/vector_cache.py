"""Disk cache for single vector tiles pulled from the remote planet archive.

Plain files rather than an archive format on purpose: tiles arrive one at a
time and in no particular order, which is exactly what PMTiles and MBTiles are
bad at appending to. A directory is trivially inspectable, trivially prunable,
and the OS page cache does the hot-path work for us.

An empty tile is a real answer (ocean, or a zoom the planet has nothing at) and
is cached too — as a zero-byte file — so a blank area is not re-fetched over
and over.
"""

from pathlib import Path


class VectorTileCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, z: int, x: int, y: int) -> Path:
        return self.directory / str(z) / str(x) / f"{y}.mvt"

    def get(self, z: int, x: int, y: int) -> bytes | None:
        path = self._path(z, x, y)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def has(self, z: int, x: int, y: int) -> bool:
        """Cheap existence check — coverage asks this for hundreds of tiles and
        must not read them all."""
        path = self._path(z, x, y)
        try:
            return path.stat().st_size > 0
        except FileNotFoundError:
            return False

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        path = self._path(z, x, y)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a half-written tile that another request picks up
        # would be a corrupt protobuf, and MapLibre renders that as a hole.
        staging = path.with_suffix(".part")
        staging.write_bytes(data)
        staging.replace(path)

    def stats(self) -> tuple[int, int]:
        """(tile count, bytes on disk)."""
        count = total = 0
        for path in self.directory.rglob("*.mvt"):
            count += 1
            total += path.stat().st_size
        return count, total
