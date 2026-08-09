"""Mint a regional vector basemap extract from the Protomaps planet build.

The planet PMTiles archive is range-requestable, so `pmtiles extract` pulls only
the tiles inside a bbox — a city-sized z0-15 region costs about 20 MB and ten
seconds. That is what makes "author anywhere in Germany" practical: the shipped
nationwide extract stops at z10 (streets and place labels at overview scale),
and the moment a scenario actually visits a place we mint the detail for exactly
that area, which the packager then seals into the offline package.

Authoring-time only, like routing and geocoding: it needs the network, and
offline mode blocks it.
"""

import asyncio
import os
import re
from datetime import date, timedelta
from pathlib import Path

import httpx

_BUILD_URL = "https://build.protomaps.com/{date}.pmtiles"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")


class ExtractError(RuntimeError):
    """pmtiles extract failed or is unavailable."""


class VectorExtractor:
    def __init__(
        self,
        bundles_dir: Path,
        *,
        planet_url: str | None = None,
        binary: str = "pmtiles",
        timeout_s: float = 900.0,
    ):
        self.bundles_dir = bundles_dir
        # Pin a build with AGENTIC_MAPS_PLANET_URL for reproducible package data;
        # otherwise the most recent daily build is discovered at call time.
        self.planet_url = planet_url or os.environ.get("AGENTIC_MAPS_PLANET_URL", "").strip() or None
        self.binary = binary
        self.timeout_s = timeout_s

    async def resolve_planet_url(self, *, client: httpx.AsyncClient | None = None) -> str:
        """Newest daily planet build (they appear a day or two behind)."""
        if self.planet_url:
            return self.planet_url
        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            for days_back in range(1, 8):
                url = _BUILD_URL.format(date=(date.today() - timedelta(days=days_back)).strftime("%Y%m%d"))
                try:
                    response = await client.get(url, headers={"Range": "bytes=0-99"}, timeout=20.0)
                except httpx.HTTPError:
                    continue
                if response.status_code in (200, 206):
                    self.planet_url = url
                    return url
        finally:
            if owns_client:
                await client.aclose()
        raise ExtractError("no recent Protomaps planet build reachable")

    def bundle_path(self, bundle_id: str) -> Path:
        if not _ID_RE.match(bundle_id):
            raise ExtractError(f"invalid bundle id {bundle_id!r}")
        return self.bundles_dir / f"streets-{bundle_id}.pmtiles"

    async def extract(
        self,
        bundle_id: str,
        *,
        west: float,
        south: float,
        east: float,
        north: float,
        maxzoom: int = 15,
    ) -> Path:
        target = self.bundle_path(bundle_id)
        planet = await self.resolve_planet_url()
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        # Write to a temp name so a crashed run cannot leave a half archive
        # that the tile server would happily serve holes from.
        staging = target.with_suffix(".pmtiles.part")
        staging.unlink(missing_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, "extract", planet, str(staging),
                f"--bbox={west},{south},{east},{north}",
                f"--maxzoom={maxzoom}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError):
            raise ExtractError(f"{self.binary} not installed (brew install pmtiles)")
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            process.kill()
            staging.unlink(missing_ok=True)
            raise ExtractError(f"pmtiles extract timed out after {self.timeout_s:.0f}s")
        if process.returncode != 0 or not staging.exists():
            staging.unlink(missing_ok=True)
            raise ExtractError(stderr.decode()[-400:].strip() or "pmtiles extract failed")
        staging.replace(target)
        return target
