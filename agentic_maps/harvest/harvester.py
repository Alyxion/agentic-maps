"""Politely download a HarvestPlan into an MBTiles bundle.

Bounded concurrency, per-source delay, skip-existing (incremental re-harvest),
one retry per tile. Works for WMS (GetMap with the tile's EPSG:3857 bbox) and
XYZ template sources.

A harvester can be given `alternates`: further sources to try when the primary
has no imagery for a tile. That is how the `de-dop` federation survives state
borders — coverage bboxes are rectangles, actual state borders are not, so the
first candidate regularly answers with a blank out-of-area tile.
"""

import asyncio
import io

import httpx

from ..models.harvest_plan import HarvestPlan
from ..models.harvest_report import HarvestReport
from ..models.tile_coord import TileCoord
from ..models.tile_source import TileSource
from ..storage.mbtiles import MBTilesBundle

# State WMS answer out-of-coverage requests — and requests at a scale their
# layer does not serve — with a flat placeholder image and HTTP 200 rather than
# an error, so size is the signal that separates "no imagery here" from "here
# is the ground". Measured 2026-07-25 across all 15 state services: placeholders
# run 667 B to 3.2 KB, the smallest real orthophoto tile was 6 KB, typical ones
# 15–60 KB. 4.5 KB sits in the empty middle of that gap.
#
# Only applied to WMS sources: an XYZ tile may be legitimately tiny (a
# deep-ocean Blue Marble tile is nearly uniform blue) and there is no second
# provider to fall through to anyway.
BLANK_TILE_MAX_BYTES = 4500

# A mosaic this opaque counts as fully covered and is stored as JPEG.
_OPAQUE_ENOUGH = 0.995

# Hard ceiling on how many source images may go into one displayed tile: the
# 2x2 rule. One tile on stage must never be assembled from a crowd of fetches —
# neither from four-plus states side by side, nor (see storage/fallback.py) from
# more than the four children directly below it.
MAX_MOSAIC_MEMBERS = 4


class NoImagery(Exception):
    """No candidate source had imagery for this tile (all blank)."""


def _key_out_white(image):
    """Make pure-white pixels transparent (see TileSource.white_is_nodata)."""
    from PIL import ImageChops

    red, green, blue = image.convert("RGB").split()
    only_255 = lambda channel: channel.point(lambda v: 255 if v == 255 else 0)  # noqa: E731
    white = ImageChops.multiply(ImageChops.multiply(only_255(red), only_255(green)), only_255(blue))
    keyed = image.copy()
    keyed.putalpha(ImageChops.multiply(image.getchannel("A"), ImageChops.invert(white)))
    return keyed


def _has_imagery(image) -> bool:
    """True when an RGBA tile carries actual ground, not a placeholder.

    Transparency alone is not enough of a test: some services (Thüringen at
    country zoom, for one) answer with an *opaque white* block where their
    layer has nothing to draw. Real orthophotos have thousands of distinct
    colours, so a tile that quantises to a handful is a placeholder — and if
    that one landed on top of the mosaic it would wipe out the states below it.
    """
    if image.getchannel("A").getextrema()[1] == 0:
        return False
    # getcolors returns None once the image exceeds maxcolors distinct values.
    return image.convert("RGB").getcolors(maxcolors=16) is None


class Harvester:
    def __init__(
        self,
        source: TileSource,
        *,
        alternates: list[TileSource] | None = None,
        mosaic: bool = False,
        ocean_source: TileSource | None = None,
        land_mask=None,
        concurrency: int = 6,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.source = source
        self.alternates = list(alternates or [])
        # Federated (composite) sources request transparent PNGs and stack them;
        # see _fetch_mosaic. Single-provider sources fetch their own format.
        self.mosaic = mosaic
        # Ocean-blend compositing (TileSource.ocean_blend_source_id): `source`
        # is the land layer, `ocean_source` fills the ocean pixels, and
        # `land_mask` (geo/landmask.py LandMask) says which are which. Both
        # must be given for blending; otherwise the plain path serves.
        self.ocean_source = ocean_source
        self.land_mask = land_mask
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.transport = transport

    @property
    def candidates(self) -> list[TileSource]:
        return [self.source, *self.alternates]

    def tile_url(
        self, coord: TileCoord, source: TileSource | None = None, *, transparent: bool = False
    ) -> str:
        source = source or self.source
        if source.kind == "xyz":
            return (
                source.url
                .replace("{z}", str(coord.z))
                .replace("{x}", str(coord.x))
                .replace("{y}", str(coord.y))
            )
        west, south, east, north = coord.bbox_3857()
        size = source.tile_size
        return (
            f"{source.url}?SERVICE=WMS&VERSION={source.wms_version}"
            f"&REQUEST=GetMap&LAYERS={source.wms_layers}&STYLES="
            f"&CRS=EPSG:3857&BBOX={west:.4f},{south:.4f},{east:.4f},{north:.4f}"
            f"&WIDTH={size}&HEIGHT={size}&FORMAT={source.media_type}"
            + ("&TRANSPARENT=TRUE" if transparent else "")
        )

    async def _fetch_from(
        self,
        client: httpx.AsyncClient,
        coord: TileCoord,
        source: TileSource,
        *,
        transparent: bool = False,
    ) -> bytes:
        url = self.tile_url(coord, source, transparent=transparent)
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = await client.get(url, timeout=self.timeout_s)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    raise ValueError(f"non-image response ({content_type}) for {coord.z}/{coord.x}/{coord.y}")
                return response.content
            except Exception as error:  # noqa: BLE001 - retried once, then reported per tile
                last_error = error
                await asyncio.sleep(0.5)
        raise last_error  # type: ignore[misc]

    async def _fetch_mosaic(self, client: httpx.AsyncClient, coord: TileCoord) -> bytes:
        """Alpha-composite every candidate state into one tile.

        A tile wide enough to span states (roughly z12 and below) is only
        partly covered by each of them, and a WMS fills the rest with opaque
        background — so picking a single winner paints a white block over the
        neighbours. Requesting transparent PNGs and stacking them instead gives
        a seamless nationwide mosaic. Most specific source goes on top, so
        Berlin's TrueDOP wins over Brandenburg where they overlap.
        """
        from PIL import Image  # optional 'fallback' extra; caller falls back if absent

        # Top-down, most specific first, and stop as soon as the stack is
        # opaque: whatever a fully covering layer sits on cannot be seen, so
        # fetching it would be pure waste. A tile deep inside one state costs
        # exactly one request even though several state rectangles overlap it.
        layers: list[Image.Image] = []
        errors = 0
        for source in self.candidates[:MAX_MOSAIC_MEMBERS]:
            as_png = source.model_copy(update={"tile_format": "png"})
            try:
                data = await self._fetch_from(client, coord, as_png, transparent=True)
                image = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:  # noqa: BLE001 - one state missing must not sink the mosaic
                errors += 1
                continue
            if source.white_is_nodata:
                image = _key_out_white(image)
            if not _has_imagery(image):
                continue  # this state has nothing here
            layers.append(image)
            if image.getchannel("A").histogram()[255] == image.width * image.height:
                break  # fully covers the tile; anything underneath is invisible

        if not layers:
            if errors:
                raise ValueError(f"all mosaic members failed for {coord.z}/{coord.x}/{coord.y}")
            raise NoImagery(f"no candidate has imagery for {coord.z}/{coord.x}/{coord.y}")

        merged = layers[-1]  # widest coverage is the base
        for image in reversed(layers[:-1]):
            merged = Image.alpha_composite(merged, image)

        buffer = io.BytesIO()
        alpha = merged.getchannel("A")
        opaque_fraction = alpha.histogram()[255] / (merged.width * merged.height)
        if opaque_fraction >= _OPAQUE_ENOUGH:
            # Covered — JPEG keeps the package small. The stray transparent
            # specks white-keying leaves in bright roofs are not worth a PNG
            # five times the size.
            merged.convert("RGB").save(buffer, "JPEG", quality=85)
        else:
            # Partly covered: keep the alpha so the basemap shows through the
            # gaps (sea, abroad) instead of a painted rectangle.
            merged.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()

    async def _fetch_blend(self, client: httpx.AsyncClient, coord: TileCoord) -> bytes:
        """Land pixels from `source`, ocean pixels from `ocean_source`.

        The land mask is consulted FIRST: a tile the (feathered) mask calls
        entirely land or entirely ocean costs exactly one upstream fetch and
        returns that layer's bytes untouched — no recompression, and no second
        request aimed at GIBS for the vast majority of the planet's tiles.
        Only genuinely coastal tiles fetch both layers and composite them
        through the mask's soft edge.

        Resilience mirrors the mosaic path: a failing OCEAN fetch degrades to
        the plain land tile (flat navy ocean, the pre-blend look) rather than
        failing the tile; a failing land fetch is a real failure.
        """
        from PIL import Image  # optional extra; fetch_tile falls back if absent

        size = self.source.tile_size
        mask = self.land_mask.tile_mask(coord.z, coord.x, coord.y, size=size)
        low, high = mask.getextrema()
        if low == 255:
            return await self._fetch_from(client, coord, self.source)
        if high == 0:
            return await self._fetch_from(client, coord, self.ocean_source)

        land_data, ocean_result = await asyncio.gather(
            self._fetch_from(client, coord, self.source),
            self._fetch_from(client, coord, self.ocean_source),
            return_exceptions=True,
        )
        if isinstance(land_data, BaseException):
            raise land_data
        if isinstance(ocean_result, BaseException):
            return land_data
        land = Image.open(io.BytesIO(land_data)).convert("RGB")
        ocean = Image.open(io.BytesIO(ocean_result)).convert("RGB")
        blended = Image.composite(land, ocean, mask)
        buffer = io.BytesIO()
        blended.save(buffer, "JPEG", quality=85)
        return buffer.getvalue()

    async def fetch_tile(self, client: httpx.AsyncClient, coord: TileCoord) -> bytes:
        """Fetch one tile from the candidate states.

        Several candidates are mosaicked together (see `_fetch_mosaic`); a
        single one is taken as-is unless it answers blank. When nobody has
        imagery this raises NoImagery, which callers turn into "let the basemap
        show through" rather than painting an empty rectangle over it.
        """
        if self.ocean_source is not None and self.land_mask is not None:
            try:
                return await self._fetch_blend(client, coord)
            except ImportError:
                pass  # no Pillow: the plain path below serves the land layer
        if self.mosaic:
            try:
                return await self._fetch_mosaic(client, coord)
            except NoImagery:
                raise
            except ImportError:
                pass  # no Pillow: fall through to first-non-blank-wins below

        last_error: Exception | None = None
        for source in self.candidates:
            try:
                data = await self._fetch_from(client, coord, source)
            except Exception as error:  # noqa: BLE001 - try the next state, report if none work
                last_error = error
                continue
            if source.kind != "wms" or len(data) > BLANK_TILE_MAX_BYTES:
                return data
        # Everyone who answered answered blank. A transport error on the way is
        # a real failure, not "nobody covers this" — keep it retryable.
        if last_error is not None:
            raise last_error
        raise NoImagery(f"no candidate has imagery for {coord.z}/{coord.x}/{coord.y}")

    async def harvest(self, plan: HarvestPlan, bundle: MBTilesBundle) -> HarvestReport:
        semaphore = asyncio.Semaphore(self.concurrency)
        delay_s = self.source.request_delay_ms / 1000.0
        fetched = skipped = failed = uncovered = total_bytes = 0
        count_lock = asyncio.Lock()

        async with httpx.AsyncClient(transport=self.transport) as client:

            async def worker(coord: TileCoord) -> None:
                nonlocal fetched, skipped, failed, uncovered, total_bytes
                if bundle.has_tile(coord):
                    async with count_lock:
                        skipped += 1
                    return
                async with semaphore:
                    try:
                        data = await self.fetch_tile(client, coord)
                    except NoImagery:
                        # Nobody has imagery here (sea, abroad, data gap) — the
                        # basemap shows through, that is not a failure.
                        async with count_lock:
                            uncovered += 1
                        return
                    except Exception:  # noqa: BLE001 - per-tile failure is a counted outcome
                        async with count_lock:
                            failed += 1
                        return
                    if delay_s:
                        await asyncio.sleep(delay_s)
                bundle.put_tile(coord, data)
                async with count_lock:
                    fetched += 1
                    total_bytes += len(data)

            await asyncio.gather(*(worker(c) for c in plan.tiles))

        return HarvestReport(
            bundle_id=bundle.path.stem,
            planned=plan.tile_count,
            fetched=fetched,
            skipped_existing=skipped,
            failed=failed,
            uncovered=uncovered,
            bytes_fetched=total_bytes,
        )
