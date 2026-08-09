"""Cut a small OSM PBF extract for a bbox — the one real I/O side effect
`choose_pbf_strategy() == "overpass"` needs.

There is no on-demand, city-scoped `.osm.pbf` extract service with a real,
unauthenticated, scriptable HTTP API:

- Geofabrik (https://download.geofabrik.de) publishes only country/state-level
  extracts on a stable URL pattern — no city granularity.
- BBBike's extract service (https://extract.bbbike.org) DOES do city-sized
  custom areas, but it is an interactive web form with a 2-7 minute queue and
  email notification; its own docs describe no REST/job-polling API, only the
  web UI (`docs/setup-guide.md` cites this).

What DOES have a real, documented, scriptable bbox endpoint is the OSM API
itself: `GET https://api.openstreetmap.org/api/0.6/map?bbox=W,S,E,N` returns
raw OSM XML for everything inside the box, capped at 0.25 x 0.25 degrees and
50,000 nodes (wiki.openstreetmap.org/wiki/API_v0.6). That endpoint is meant
for editors (JOSM/iD) and is rate-limited accordingly, so bulk/scripted
readers are asked to use the Overpass API's mirror of the exact same
interface instead: `overpass-api.de/api/map` (same `bbox=` format, same
practical ceiling) — the same endpoint OSM's own website "Export" button
calls under the hood. Overpass's fair-use policy (<10,000 queries/day,
<1 GB/day) comfortably covers "someone runs the setup wizard a few times".

The result is OSM XML, not PBF — `osmium` (osmium-tool, BSD-2, the same
family of CLI the rest of this codebase already shells out to for `pmtiles
extract` in `vector/extractor.py`) converts it losslessly with `osmium cat`.
"""

import asyncio
import re
from pathlib import Path

import httpx

from ..models.bbox_deg import BBoxDeg

_MAP_URL = "https://overpass-api.de/api/map"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")


class PbfFetchError(RuntimeError):
    """The Overpass export or the osmium conversion failed."""


async def fetch_city_pbf(
    region_id: str,
    bbox: BBoxDeg,
    out_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    binary: str = "osmium",
    timeout_s: float = 180.0,
) -> Path:
    """Overpass `/api/map` bbox export -> `osmium cat` -> `<region_id>.osm.pbf`.

    Only called for bboxes `choose_pbf_strategy` already judged small enough
    (<= `planner.OVERPASS_MAX_AREA_DEG2`) — this function does not re-check
    the size itself, matching `vector/extractor.py`'s division of labour
    (the caller decides whether to call it at all).
    """
    if not _ID_RE.match(region_id):
        raise PbfFetchError(f"invalid region id {region_id!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    xml_path = out_dir / f"{region_id}.osm"
    pbf_path = out_dir / f"{region_id}.osm.pbf"

    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        params = {"bbox": f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}"}
        try:
            response = await client.get(_MAP_URL, params=params, timeout=timeout_s, follow_redirects=True)
        except httpx.HTTPError as error:
            raise PbfFetchError(f"Overpass /api/map request failed: {error}")
        if response.status_code != 200:
            raise PbfFetchError(
                f"Overpass /api/map returned {response.status_code}: {response.text[:300]}"
            )
        xml_path.write_bytes(response.content)
    finally:
        if owns_client:
            await client.aclose()

    staging = pbf_path.with_suffix(".pbf.part")
    staging.unlink(missing_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "cat", str(xml_path), "-o", str(staging), "--overwrite",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError):
        raise PbfFetchError(f"{binary} not installed (brew install osmium-tool / apt install osmium-tool)")
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        process.kill()
        staging.unlink(missing_ok=True)
        raise PbfFetchError(f"osmium cat timed out after {timeout_s:.0f}s")
    finally:
        xml_path.unlink(missing_ok=True)
    if process.returncode != 0 or not staging.exists():
        staging.unlink(missing_ok=True)
        raise PbfFetchError(stderr.decode()[-400:].strip() or "osmium cat failed")
    staging.replace(pbf_path)
    return pbf_path
