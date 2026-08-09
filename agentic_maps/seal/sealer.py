"""Turn a recording into the bytes a sealed page carries — and no others.

A `PackageBuilder` package (`package/builder.py`) ships whole artifacts: a
regional `.pmtiles` extract is tens of megabytes of streets for a county when
one page shows four blocks of it, and a glyph directory goes in wholesale.
That is the right trade for a package that sits beside a server-backed
deployment. It is the wrong one for a single sealed HTML file a person mails
or drops on a kiosk's disk, where every byte is carried by every copy.

So this builder is per-resource instead of per-artifact: it holds exactly the
keys a recording proved the page asks for. Streets arrive as the individual
vector tiles that were actually rendered, fonts as the glyph ranges whose
characters actually appear.

What that costs in robustness is bought back at runtime rather than in bytes:
`web/sealed-runtime.js` carries the same parent-crop / children-merge ladder
`storage/fallback.py` uses server-side, so a viewport just outside the
recorded set degrades to a coarser tile instead of showing a hole.
"""

import asyncio
import base64
import json
import math
import re
from typing import Iterable

import httpx

from ..models.capture_report import CaptureReport
from ..models.sealed_bundle import SealedBundle

# The scheme a sealed page answers on. MapLibre routes every style URL with a
# registered protocol through the runtime's handler and never touches the
# network for it. `amap` — this product's own scheme, not the reference
# reference's `dsmap`.
SEALED_SCHEME = "amap"

# `/api/v1/maps/live/{source}/{z}/{x}/{y}` — this product's own raster tile
# route (`MapsApi.mount`'s default `prefix`). A host that mounts `MapsApi`
# at a different prefix, or serves raster tiles some other way, passes its
# own pattern to `Sealer(..., raster_pattern=...)`.
DEFAULT_RASTER_PATTERN = re.compile(r"^/api/v1/maps/live/([^/]+)/(\d+)/(\d+)/(\d+)$")

# Fetching the recorded keys back out of the host is local I/O against a warm
# cache; the ceiling only keeps this from opening a thousand sockets at once.
FETCH_CONCURRENCY = 12


def sealed_url(path: str, *, scheme: str = SEALED_SCHEME) -> str:
    """`/api/v1/maps/...` -> `amap://api/v1/maps/...`

    The path after the scheme is the store key verbatim, so the runtime's
    handler is a lookup and never a translation table.
    """
    return f"{scheme}://{path.lstrip('/')}"


class Sealer:
    """Fetches exactly the recorded keys and packs them into a `SealedBundle`.

    `spec_path` is where the LIVE payload (`MapPayload`, the same JSON shape
    `web/map.js` mounts from) is fetched from to be rewritten into the sealed
    one — see `_seal_payload`. It defaults to this product's own dev-server
    demo endpoint (`GET /api/demo-spec`, `devserver.py`); a real deployment
    that serves its payload from a different path (its own `/maps/spec`,
    say) passes that path in instead — there is no single universal spec
    path a host application is required to use.
    """

    def __init__(
        self,
        host: str,
        *,
        spec_path: str = "/api/demo-spec",
        scheme: str = SEALED_SCHEME,
        raster_pattern: re.Pattern = DEFAULT_RASTER_PATTERN,
        attribution_path: str = "/api/v1/maps/attribution",
    ):
        self.host = host.rstrip("/")
        self.spec_path = spec_path
        self.scheme = scheme
        self.raster_pattern = raster_pattern
        self.attribution_path = attribution_path

    async def seal(self, report: CaptureReport) -> SealedBundle:
        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = await self._payload(client)
            blobs = await self._fetch_all(client, report.keys)
            by_zoom = await self._attribution_by_zoom(client, report.keys)

        # Credit follows the ZOOM, because the imagery ladder does: a world
        # basemap carries the globe, a regional mosaic the mid band, a local
        # survey the city zooms. One line for the whole page would name a
        # global provider over a rooftop and a local survey over a continent
        # — wrong in both directions, and the live map does not do that
        # either.
        vector = "© OpenStreetMap" if any(k.endswith(".mvt") for k in report.keys) else ""
        every = dict.fromkeys(part for text in by_zoom.values()
                              for part in text.split(" | ") if part)
        full = " | ".join(part for part in (" | ".join(every), vector) if part)

        bundle = SealedBundle(routes=report.routes, attribution=full)
        self._pack(bundle, blobs)
        bundle.payload = self._seal_payload(payload, full, by_zoom)
        return bundle

    # -- fetching --------------------------------------------------------

    async def _payload(self, client: httpx.AsyncClient) -> dict:
        response = await client.get(self.host + self.spec_path)
        response.raise_for_status()
        return response.json()

    async def _fetch_all(self, client: httpx.AsyncClient,
                         keys: Iterable[str]) -> list[tuple[str, bytes, str]]:
        gate = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(key: str) -> tuple[str, bytes, str] | None:
            async with gate:
                try:
                    response = await client.get(self.host + key)
                except httpx.HTTPError:
                    return None
            if response.status_code != 200 or not response.content:
                # A 404 here is a real answer: sea, a data gap, an empty
                # ocean vector tile. Storing "nothing" would only make the
                # file bigger; the runtime's ladder handles the absence.
                return None
            media = response.headers.get("content-type", "application/octet-stream")
            return key, response.content, media.split(";")[0].strip()

        results = await asyncio.gather(*(one(key) for key in keys))
        found = [item for item in results if item is not None]
        keys = list(keys)
        return [trim_worldwide(item, keys) for item in found]

    async def _attribution_by_zoom(self, client: httpx.AsyncClient,
                                   keys: Iterable[str]) -> dict[int, str]:
        """Which imagery rights apply, PER ZOOM.

        Sealed, we know more than a blanket line, and the extra knowledge
        matters in two directions at once.

        Which services shipped: the recorded raster keys say exactly that,
        so a page over one city credits that city's service and not every
        source the product knows about.

        And WHERE they apply: an imagery ladder is banded by scale — a
        world basemap carries the globe, a regional mosaic the mid band, a
        local survey the city zooms. A single line for the whole page
        therefore names a global provider over a rooftop and a local survey
        over a continent. Both are false, and the live map does not claim
        either — it asks per view. So the answer is sealed per zoom and the
        runtime picks the one for the scale on screen.
        """
        boxes: dict[tuple[str, int], list[float]] = {}
        for key in keys:
            match = self.raster_pattern.match(key)
            if match is None:
                continue
            source, z, x, y = match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))
            west, south, east, north = _tile_bbox(z, x, y)
            box = boxes.get((source, z))
            if box is None:
                boxes[(source, z)] = [west, south, east, north]
            else:
                box[0] = min(box[0], west)
                box[1] = min(box[1], south)
                box[2] = max(box[2], east)
                box[3] = max(box[3], north)

        per_zoom: dict[int, dict[str, None]] = {}
        for (source, z), box in sorted(boxes.items()):
            try:
                response = await client.get(
                    f"{self.host}{self.attribution_path}",
                    params={"src": source, "z": z, "west": box[0], "south": box[1],
                            "east": box[2], "north": box[3]})
                if response.status_code != 200:
                    continue
                for item in response.json().get("sources", []):
                    per_zoom.setdefault(z, {})[item["attribution"]] = None
            except httpx.HTTPError:
                continue
        return {z: " | ".join(texts) for z, texts in sorted(per_zoom.items())}

    # -- packing ---------------------------------------------------------

    def _pack(self, bundle: SealedBundle, blobs: list[tuple[str, bytes, str]]) -> None:
        """One byte string, one offset index.

        Sorted by media type then key so that runs of the same kind sit
        together — irrelevant to correctness, but it keeps the file's own
        compression (transport gzip, zip-in-a-mail) doing useful work.
        """
        media_ids: dict[str, int] = {}
        chunks: list[bytes] = []
        offset = 0
        for key, data, media in sorted(blobs, key=lambda item: (item[2], item[0])):
            media_id = media_ids.setdefault(media, len(media_ids))
            bundle.index[key] = (offset, len(data), media_id)
            chunks.append(data)
            offset += len(data)
        bundle.media_types = [media for media, _ in sorted(media_ids.items(), key=lambda kv: kv[1])]
        bundle.data = base64.b64encode(b"".join(chunks)).decode("ascii")

    def _seal_payload(self, payload: dict, attribution: str,
                      by_zoom: dict[int, str]) -> dict:
        """The runtime payload, pointed at the store instead of the host.

        `offline: true` is not decoration: `map.js` reads it to stop asking
        the federation which sources are visible for the on-map credit line.
        The answer it would have asked for is already computed above and
        travels as a per-zoom table instead.
        """
        sealed = dict(payload)
        for field in ("tiles_url_template", "aerial_url_template",
                      "world_tiles_url_template",
                      "vector_url_template", "glyphs_url_template"):
            value = sealed.get(field)
            if isinstance(value, str) and value.startswith("/"):
                sealed[field] = sealed_url(value, scheme=self.scheme)
        base = sealed.get("sprites_base_url")
        if isinstance(base, str) and base.startswith("/"):
            sealed["sprites_base_url"] = sealed_url(base, scheme=self.scheme)
        # Geo layers (countries/cities/physical/oceans) are fetched (not
        # styled), so they keep host-relative keys and are answered by the
        # runtime's fetch shim rather than the MapLibre protocol handler.
        sealed["offline"] = True
        sealed["standalone"] = False
        sealed["attribution"] = attribution
        # JSON object keys are strings; the runtime parses them back.
        sealed["attribution_zooms"] = {str(z): text for z, text in by_zoom.items()}
        return sealed


# How far past the recorded footprint a worldwide layer is still kept.
# Borders are context: at the widest recorded view a bigger kiosk window
# shows more of the neighbours, and a missing country reads as a hole in the
# map. Degrees, so it is generous at the scales where these layers are
# visible at all.
_GEO_MARGIN_DEG = 12.0

# Tiles below this zoom are the whole continent and say nothing about where
# the page looks; including them would blow the footprint up to half the
# planet.
_FOOTPRINT_MIN_ZOOM = 4

_ANY_TILE = re.compile(r"/(\d+)/(\d+)/(\d+)(?:\.mvt)?$")


def trim_worldwide(item: tuple[str, bytes, str],
                   keys: Iterable[str]) -> tuple[str, bytes, str]:
    """Cut a worldwide layer down to the world this page shows.

    `/api/v1/maps/geo/countries` is Natural Earth's whole planet — several
    megabytes of borders, shipped so a zoomed-out view knows where it is.
    For a page about one city that is the single largest thing in the file
    and almost all of it is off screen. The recorded tiles say exactly which
    part of the planet was ever displayed, so features outside it (with a
    generous margin, since a viewer's window is wider than the author's) are
    dropped.

    Features are kept whole: clipping geometry would need a geometry library
    and would buy little — the saving is in the countries nobody sees, not
    in the halves of the ones they do.
    """
    key, data, media = item
    if "/geo/" not in key or "json" not in media:
        return item
    footprint = _footprint(keys)
    if footprint is None:
        return item
    try:
        collection = json.loads(data)
    except ValueError:
        return item
    features = collection.get("features")
    if not isinstance(features, list):
        return item
    kept = [f for f in features if _feature_intersects(f, footprint)]
    if len(kept) == len(features):
        return item
    collection["features"] = kept
    return key, json.dumps(collection, separators=(",", ":")).encode(), media


def _footprint(keys: Iterable[str]) -> tuple[float, float, float, float] | None:
    """Bounding box of every tile the recording actually displayed."""
    west = south = east = north = None
    for key in keys:
        if "/live/" not in key and ".mvt" not in key:
            continue
        match = _ANY_TILE.search(key)
        if match is None:
            continue
        z, x, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if z < _FOOTPRINT_MIN_ZOOM:
            continue
        tw, ts, te, tn = _tile_bbox(z, x, y)
        west = tw if west is None else min(west, tw)
        south = ts if south is None else min(south, ts)
        east = te if east is None else max(east, te)
        north = tn if north is None else max(north, tn)
    if west is None:
        return None
    return (west - _GEO_MARGIN_DEG, south - _GEO_MARGIN_DEG,
            east + _GEO_MARGIN_DEG, north + _GEO_MARGIN_DEG)


def _feature_intersects(feature: dict, box: tuple[float, float, float, float]) -> bool:
    west, south, east, north = box
    fw, fs, fe, fn = _geometry_bounds(feature.get("geometry") or {})
    if fw is None:
        return True                                   # unreadable: keep it
    return not (fw > east or fe < west or fs > north or fn < south)


def _geometry_bounds(geometry: dict):
    coords = geometry.get("coordinates")
    if coords is None:
        return (None, None, None, None)
    lons: list[float] = []
    lats: list[float] = []

    def walk(node) -> None:
        if (isinstance(node, (list, tuple)) and len(node) >= 2
                and isinstance(node[0], (int, float)) and isinstance(node[1], (int, float))):
            lons.append(float(node[0]))
            lats.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not lons:
        return (None, None, None, None)
    return (min(lons), min(lats), max(lons), max(lats))


def _tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** z

    def lat(index: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * index / n))))

    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    return west, lat(y + 1), east, lat(y)


def bundle_json(bundle: SealedBundle) -> str:
    """Compact JSON for embedding in a page.

    The index is by far the most repetitive part of the file, so it is
    emitted without whitespace; the base64 payload dwarfs it either way.
    """
    return json.dumps(bundle.model_dump(), separators=(",", ":"))
