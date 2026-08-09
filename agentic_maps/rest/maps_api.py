"""REST surface: a class with mount(app, *, prefix).

A host application injects its own auth/scopes around this; the isolated
dev server mounts it bare. FastAPI is imported here only, so the core engine
stays framework-free.

Three runtime modes (docs/concept.md §5, `models/runtime_mode.py`):
- "online" (default): live cache-through proxy + routing + harvest allowed —
  authoring mode;
- "mixed": live per-request actions stay allowed (routing, geocoding, the
  live tile proxy, one-shot boundary GeoJSON, glyphs/sprites) but bulk
  data-provisioning actions (harvest, harvest-world, vector/extract) refuse —
  authoring against a live map without silently minting gigabytes of new
  offline data;
- "offline": presentation mode — every network-touching endpoint is
  disabled; tiles/glyphs serve exclusively from bundles/cache, with the
  fallback ladder (parent-crop / children-merge) for raster holes.
"""

import asyncio
import json
import math
import os
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from ..harvest.harvester import Harvester, NoImagery
from ..harvest.planner import HarvestPlanner
from ..models.bbox_deg import BBoxDeg
from ..models.bundle_info import BundleInfo
from ..models.composite_source import CompositeSource
from ..models.country_hit import CountryHit
from ..models.feature_extract_meta import FeatureExtractMeta
from ..models.geocode_address import GeocodeAddress
from ..models.geocode_result import GeocodeResult
from ..models.harvest_plan import HarvestPlan
from ..models.harvest_report import HarvestReport
from ..models.lat_lon import LatLon
from ..models.map_payload import MapPayload
from ..models.map_route import MapRoute
from ..models.map_spec import MapSpec
from ..models.map_view_session import MapViewSession
from ..models.aerial_quality import AerialQuality
from ..models.session_revision import SessionRevision
from ..models.optimized_route import OptimizedRoute
from ..models.package_manifest import PackageManifest
from ..models.provision_estimate import ProvisionEstimate
from ..models.provision_job import ProvisionJob
from ..models.provision_request import ProvisionRequest
from ..models.render_request import RenderRequest
from ..models.runtime_mode import RuntimeMode
from ..models.tile_coord import TileCoord
from ..models.site_plan import SitePlan
from ..models.street_survey import StreetSurvey
from ..models.street_way import StreetWay
from ..models.tile_source import TileSource
from ..models.vector_bundle_info import VectorBundleInfo
from ..models.isochrone_result import IsochroneResult
from ..package.builder import PackageBuilder
from ..provision.engine import ProvisionEngine
from ..provision.estimates import estimate_region
from ..routing.base import RoutingBackend
from ..routing.osrm import OsrmRouter
from ..routing.valhalla import ValhallaRouter
from ..sources.presets import builtin_composites, builtin_sources
from ..siteplan import render as render_site_plan
from ..vector.mvt import read_tile
from ..storage.fallback import FallbackTileResolver
from ..storage.mbtiles import MBTilesBundle
from ..storage.pmtiles_bundle import PMTilesBundle
from ..storage.vector_cache import VectorTileCache
from ..vector.remote_pmtiles import RemotePMTiles, RemotePMTilesError
from ..geo.countries import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    CityIndex,
    CountryIndex,
    OceanIndex,
    PhysicalIndex,
)
from ..geo.geonames import GEONAMES_ATTRIBUTION, PlaceIndex
from ..geo.landmask import LandMask
from ..geo.via_places import find_via_places
from ..vector.extractor import ExtractError, VectorExtractor


def _sniff_media_type(data: bytes, declared_format: str = "jpeg") -> str:
    """Content type from the bytes themselves.

    A federated bundle mixes formats: fully covered tiles are stored as JPEG,
    partly covered mosaics as PNG so their alpha survives.
    """
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return f"image/{declared_format}"


# One browser can ask for a hundred tiles at once and each mosaic tile costs up
# to four upstream requests; without a cap that becomes a small flood aimed at
# one state's WMS, which then serves everyone slowly.
LIVE_TILE_CONCURRENCY = 8
# Synthesized aerial tiles (`/aerial` fallthrough) are re-encoded upscales;
# quality mirrors the fallback ladder's own JPEG setting.
AERIAL_SYNTH_JPEG_QUALITY = 82
# Deepest zoom the vector stack deals with: the Protomaps planet builds stop
# at z15 and MapLibre over-zooms from there.
MAX_VECTOR_ZOOM = 15
LIVE_TILE_TIMEOUT_S = 12.0

# /vector/features budget. 64 tiles at z15 is roughly a 6 km x 6 km window —
# generous for "every building around the station" and still cheap enough to
# decode synchronously; past it the request is refused with a message saying
# what to shrink rather than quietly taking half a minute.
FEATURE_TILES_CAP = 64
# Buildings/POIs only exist in the deepest tile levels of the Protomaps
# build; auto zoom selection tries these, deepest first, and refuses rather
# than silently dropping to a zoom where the requested layers are absent.
FEATURE_AUTO_ZOOMS = (15, 14)
# Feature cap per answer. A z15 tile in a dense city carries a few thousand
# features across all layers, so 5000 keeps a typical one-tile answer whole
# while bounding the worst case; `meta.truncated` says when it bit.
FEATURE_DEFAULT_LIMIT = 5000
FEATURE_MAX_LIMIT = 20000

_GLYPHS_UPSTREAM = "https://protomaps.github.io/basemaps-assets/fonts"
_SPRITES_UPSTREAM = "https://protomaps.github.io/basemaps-assets/sprites"

# Where Playwright is told to point itself for `POST /render` (render/service.py):
# this server's own externally-reachable address. Defaults to the isolated
# dev server's own host:port; `devserver.py` overrides it with the real
# host/port it was started with, and a host application embedding `MapsApi`
# should pass its own `render_base_url` at construction time instead of
# relying on the env var.
RENDER_BASE_URL_ENV = "AGENTIC_MAPS_RENDER_BASE_URL"
DEFAULT_RENDER_BASE_URL = "http://127.0.0.1:8095"
# Optional: pin `POST /render` to a specific Chromium binary rather than
# whatever `playwright install` set up. See RenderService.chromium_executable_path.
RENDER_CHROMIUM_PATH_ENV = "AGENTIC_MAPS_RENDER_CHROMIUM_PATH"
# Base URL for the geocoder /geocode and /reverse-geocode proxy to. Unset
# (the default) means the public Nominatim instance (light interactive use
# only, per its usage policy) — set this to a self-hosted Nominatim
# container (e.g. `http://nominatim:8080` inside the docker-compose network
# `setup/planner.py` generates, see docs/setup-guide.md) for anything beyond
# occasional authoring-time lookups.
NOMINATIM_URL_ENV = "AGENTIC_MAPS_NOMINATIM_URL"
DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org"


def _nominatim_base_url() -> str:
    return os.environ.get(NOMINATIM_URL_ENV, "").strip().rstrip("/") or DEFAULT_NOMINATIM_URL


def _address_from_nominatim(address: dict | None) -> GeocodeAddress | None:
    """Normalize Nominatim's `addressdetails` dict into a GeocodeAddress.

    Nominatim spreads the locality across city/town/village/municipality/
    hamlet by settlement size, and the road across road/pedestrian/footway
    for pedestrian zones — a detail card wants one field for each. Returns
    None when there is nothing usable, so callers keep the coordinate-only
    fallback path.
    """
    if not address:
        return None
    locality = next(
        (address[key] for key in ("city", "town", "village", "municipality", "hamlet")
         if address.get(key)), "")
    road = next(
        (address[key] for key in ("road", "pedestrian", "footway", "square")
         if address.get(key)), "")
    result = GeocodeAddress(
        road=road,
        house_number=address.get("house_number", ""),
        postcode=address.get("postcode", ""),
        locality=locality,
        state=address.get("state", ""),
        country=address.get("country", ""),
        country_code=address.get("country_code", ""),
    )
    return result if result != GeocodeAddress() else None
# Browser sessions (`POST /sessions`): a full MapSpec stored under a token so
# the map application can open it with the complete normal UI (`/?session=`).
# An hour outlives any "open this in my browser" flow by a wide margin while
# still bounding a long-running server's memory; the store is swept on every
# mint, exactly like the render-payload store below.
SESSION_TTL_S = 3600.0
# A session spec is routes + decorations, never raster data — 2 MB of JSON is
# several full alternates-and-steps route sets. Beyond that something is
# being smuggled through the wrong channel, and the store refuses loudly.
SESSION_PAYLOAD_MAX_BYTES = 2 * 1024 * 1024

# How long a minted render token stays redeemable. Generous relative to how
# long a single render actually takes (page load + tile settle, seconds) —
# this only needs to survive one Playwright round trip, plus enough slack
# that a slow tile fetch never expires it out from under an in-flight render.
RENDER_TOKEN_TTL_S = 120.0


class PlanPreview(BaseModel):
    spec_id: str
    source_id: str
    tile_count: int
    estimated_mb: float


class RouteRequest(BaseModel):
    route_id: str
    start: LatLon
    end: LatLon
    # Canonical travel-mode vocabulary (routing/base.py): "car" / "truck" /
    # "walk" / "bike". Each backend maps this onto its own profile/costing
    # name; an unrecognised value falls back to "car" rather than erroring.
    mode: str = "car"
    from_location: str = ""
    to_location: str = ""
    # Intermediate stops, in order. One backend request covers all of them,
    # so a multi-stop trip stays one geometry with one honest total.
    via: list[LatLon] = []
    # Turn-by-turn instructions. Off by default: they roughly double the
    # response and an embedded map view only needs the line and the total.
    steps: bool = False
    # "toll" / "motorway" / "ferry". Only actually honoured where the active
    # backend says so (see /routing/capabilities) — a self-hosted OSRM
    # compiled with the matching exclude classes, or a Valhalla server with
    # `service_limits.allow_hard_exclusions` set.
    avoid: list[str] = []
    # How many alternate routes to request in addition to the primary (0 =
    # none). Valhalla only, and only for a plain two-point route — see
    # ValhallaRouter.route(). Ignored (always 0 alternates) on OSRM.
    alternates: int = 0


class GeocodeRequest(BaseModel):
    q: str
    # Nominatim returns localized display names for the languages it has.
    lang: str = DEFAULT_LANGUAGE
    # Where the user is looking. "Bahnhofstraße" exists in a thousand towns,
    # so the one on screen is almost always the one meant — results are biased
    # towards this box and then sorted by distance from `near`.
    near: LatLon | None = None
    viewbox: BBoxDeg | None = None
    limit: int = 6


class ReverseGeocodeRequest(BaseModel):
    lat: float
    lon: float
    lang: str = DEFAULT_LANGUAGE


class ModeState(BaseModel):
    mode: RuntimeMode


class CacheClearReport(BaseModel):
    removed: list[str]


class WorldHarvestRequest(BaseModel):
    """Prewarm the shared Blue Marble world bundle: full globe to
    `global_maxzoom`, plus a refined region (e.g. Europe) to `region_maxzoom`."""

    global_maxzoom: int = 6
    region_west: float | None = None
    region_south: float | None = None
    region_east: float | None = None
    region_north: float | None = None
    region_maxzoom: int = 8


class VectorExtractRequest(BaseModel):
    id: str
    west: float
    south: float
    east: float
    north: float
    maxzoom: int = 15


def _rough_distance_km(origin: LatLon, hit: "GeocodeResult") -> float:
    """Equirectangular approximation — plenty for ranking search results.

    Great-circle accuracy buys nothing here: the question is only which of a
    handful of candidates is closest, and the error over a few hundred km is
    far below the gap between them.
    """
    mean_lat = math.radians((origin.lat + hit.lat) / 2.0)
    dx = math.radians(hit.lon - origin.lon) * math.cos(mean_lat)
    dy = math.radians(hit.lat - origin.lat)
    return math.hypot(dx, dy) * 6371.0


def _probe_range(low: int, high: int, limit: int) -> list[int]:
    """`low..high` inclusive, thinned to at most `limit` evenly spaced values.

    Always keeps both ends: the edges of a view are exactly where a region
    extract stops covering it.
    """
    span = high - low + 1
    if span <= limit:
        return list(range(low, high + 1))
    step = (span - 1) / (limit - 1)
    return sorted({low + round(index * step) for index in range(limit)})


def _tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Web-Mercator tile containing a coordinate."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    clamped = max(min(lat, 85.05112878), -85.05112878)
    radians = math.radians(clamped)
    y = int((1.0 - math.log(math.tan(radians) + 1 / math.cos(radians)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


class VectorCoverage(BaseModel):
    """Two different questions about one area.

    `max_zoom` is the best detail reachable *somewhere* in the box — it decides
    whether minting a region is worth it. `guaranteed_zoom` is the detail
    available across the *whole* box, which is the only figure a single
    MapLibre source maxzoom may be set from: a small deep region touching the
    viewport does not make the rest of the screen drawable.
    """

    max_zoom: int
    guaranteed_zoom: int
    bundle_id: str


class SitePlanSvg(BaseModel):
    """A rendered site plan. Inline it; it carries no external references."""

    svg: str


class RoutingCapabilities(BaseModel):
    backend: str
    avoid: list[str]
    departure_time_affects_route: bool
    turn_by_turn: bool
    multi_stop: bool
    # Whether /route honours `alternates` and /isochrone exists at all for
    # the active backend — the UI offers each only where it will do
    # something, the same honesty-gating principle `avoid` already follows.
    alternates: bool
    isochrone: bool


class MatrixRequest(BaseModel):
    points: list[LatLon]
    mode: str = "car"
    # Optional index lists into `points` for an ASYMMETRIC matrix: rows are
    # `sources`, columns `targets`; None means "all" on that axis, keeping
    # the historical all-pairs default. `sources=[0]` is the one-to-many
    # reachability shape — one row, not NxN.
    sources: list[int] | None = None
    targets: list[int] | None = None


class MatrixResult(BaseModel):
    durations_min: list[list[float]]


class OptimizeRouteRequest(BaseModel):
    """TSP: visit every stop once in the cheapest order.

    `keep_endpoints` pins stops[0]/stops[-1] as start/end (the only shape
    Valhalla's optimized_route supports — passing false there is refused,
    not silently ignored); `roundtrip` returns to the start instead.
    """

    route_id: str = "optimized"
    stops: list[LatLon]
    mode: str = "car"
    roundtrip: bool = False
    keep_endpoints: bool = True
    steps: bool = False
    from_location: str = ""
    to_location: str = ""


class IsochroneContourSpec(BaseModel):
    """One ring to compute: exactly one of the two fields is set."""

    time_min: float | None = None
    distance_km: float | None = None


class IsochroneRequest(BaseModel):
    center: LatLon
    mode: str = "car"
    contours: list[IsochroneContourSpec]


def _geojson_geometry(feature: dict) -> dict | None:
    """One decoded MVT feature (`vector/mvt.py`) as a GeoJSON geometry.

    Point/line features map exactly. Polygons keep the tile's own rings in
    order — MVT ring roles (outer vs hole) are winding-order conventions the
    lightweight reader does not classify, so the first ring is returned as
    the outer and the rest follow as-is; for the "measure this building"
    use case the outer ring and the properties are the payload anyway.
    """
    parts = [part for part in feature["parts"] if part]
    if not parts:
        return None
    kind = feature["type"]
    if kind == 1:                                            # point(s)
        points = [[lon, lat] for part in parts for lon, lat in part]
        if len(points) == 1:
            return {"type": "Point", "coordinates": points[0]}
        return {"type": "MultiPoint", "coordinates": points}
    coordinates = [[[lon, lat] for lon, lat in part] for part in parts]
    if kind == 2:                                            # line(s)
        if len(coordinates) == 1:
            return {"type": "LineString", "coordinates": coordinates[0]}
        return {"type": "MultiLineString", "coordinates": coordinates}
    if kind == 3:                                            # polygon rings
        return {"type": "Polygon", "coordinates": coordinates}
    return None


def _default_router() -> RoutingBackend:
    """Valhalla by default; `AGENTIC_MAPS_ROUTING_BACKEND=osrm` for the
    legacy backend. Mirrors how each backend itself picks up its own
    `AGENTIC_MAPS_VALHALLA_URL` / `AGENTIC_MAPS_OSRM_URL`."""
    import os

    if os.environ.get("AGENTIC_MAPS_ROUTING_BACKEND", "").strip().lower() == "osrm":
        return OsrmRouter()
    return ValhallaRouter()


def _isochrone_geojson(result: IsochroneResult) -> dict:
    """`IsochroneResult` as a GeoJSON FeatureCollection MapLibre can add as a
    source directly — one Polygon feature per ring, properties carrying the
    contour value/metric/colour the same way Valhalla's own response does."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[p.lon, p.lat] for p in ring.polygon]],
                },
                "properties": {
                    "metric": "time" if ring.minutes is not None else "distance",
                    "contour": ring.minutes if ring.minutes is not None else ring.km,
                    "color": ring.color,
                },
            }
            for ring in result.rings
        ],
    }


class MapsApi:
    def __init__(
        self,
        bundles_dir: Path,
        sources: dict[str, TileSource] | None = None,
        composites: dict[str, CompositeSource] | None = None,
        *,
        assets_dir: Path | None = None,
        router: RoutingBackend | None = None,
        extractor: VectorExtractor | None = None,
        countries: CountryIndex | None = None,
        mode: RuntimeMode = "online",
        remote_planet: bool = True,
        standalone: bool = False,
        render_base_url: str | None = None,
    ):
        self.bundles_dir = bundles_dir
        self.extractor = extractor or VectorExtractor(bundles_dir)
        self.countries = countries or CountryIndex(bundles_dir.parent / "geo" / "countries.geojson")
        self.cities = CityIndex(bundles_dir.parent / "geo" / "cities.geojson")
        # The dense GeoNames index (tools/make_city_index.py) — preferred for
        # via-places and city autocomplete when its asset is on disk; the
        # Natural Earth CityIndex above stays the fallback AND the globe's
        # label layer either way.
        self.geonames = PlaceIndex(bundles_dir.parent / "geo" / "places-geonames.tsv.gz")
        self._country_names: dict[str, str] | None = None
        self.physical = PhysicalIndex(bundles_dir.parent / "geo")
        self.oceans = OceanIndex(bundles_dir.parent / "geo" / "oceans.geojson")
        # Land/ocean separation for the blended world source (`blue-marble-
        # plus`): rasterized from the same admin-0 polygons the border overlay
        # draws. Lazy — nothing is loaded until a coastal tile needs a mask.
        self.land_mask = LandMask(bundles_dir.parent / "geo" / "countries.geojson")
        self.assets_dir = assets_dir or bundles_dir.parent / "assets"
        self.sources = sources if sources is not None else builtin_sources()
        self.composites = composites if composites is not None else builtin_composites()
        self.router = router or _default_router()
        self.mode: RuntimeMode = mode
        self._bundles: dict[str, MBTilesBundle] = {}
        self._vector: dict[str, PMTilesBundle] = {}
        self._client: httpx.AsyncClient | None = None
        # Streets straight out of the remote planet archive, one tile at a
        # time (docs/concept.md §5). Regional extracts stay for packaging.
        # Inside the bundles directory, not beside it: siblings of a caller's
        # bundles directory are not ours to write into, and two MapsApi
        # instances pointed at different bundle sets must not share a cache.
        # The bundle glob only matches *.pmtiles, so a subdirectory is inert.
        self.vector_cache = VectorTileCache(bundles_dir / ".vector-cache")
        # Off in tests and in any host that wants purely local data; the whole
        # point is that it is a network dependency, so it must be declinable.
        self.remote_planet_enabled = remote_planet
        # True only for the isolated single-user dev server. Mounted inside the
        # platform host this stays False, and the browser is never asked for a
        # location: one shared deployment must not prompt every visitor for a
        # permission that exists to help one person's own searching.
        self.standalone = standalone
        self._remote: RemotePMTiles | None = None
        self._live_semaphore: asyncio.Semaphore | None = None
        # Where `POST /render` points Playwright — see RENDER_BASE_URL_ENV
        # above. None here means "use the env var, then the dev-server
        # default" (resolved lazily, at render time, not at construction).
        self.render_base_url = render_base_url
        # token -> (expires_at, payload), minted by `_store_render_payload`
        # and consumed by `GET .../render/payload/{token}` (web/render.html's
        # own `data-agentic-spec-url` fetch). Swept opportunistically rather
        # than by a background task: this endpoint is never called often
        # enough for an O(n) scan on each store to matter.
        self._render_payloads: dict[str, tuple[float, MapPayload]] = {}
        # token -> (expires_at, spec): browser sessions (`POST /sessions`).
        # Same sweep-on-store discipline as the render payloads, longer TTL.
        self._sessions: dict[str, tuple[float, MapSpec]] = {}
        # token -> (spec_fn, revision_fn): LIVE session sources (see
        # `bind_session_source`). A bound token serves the CURRENT state of
        # whatever the callables read (the MCP layer binds live trips here)
        # instead of the frozen copy above; the frozen copy stays as the
        # last-known-good fallback once the live object expires.
        self._session_sources: dict[
            str, tuple[Callable[[], MapSpec | None], Callable[[], int | None]]] = {}
        # Rounded-bbox cache for `GET /aerial/quality`: band coverage is
        # static configuration, so an answer never goes stale — the cap only
        # stops a panning browser from growing the dict without bound.
        self._aerial_quality_cache: dict[tuple, AerialQuality] = {}
        # Region-bulk provisioning jobs (docs/mcp.md "Offline regions").
        # State lives INSIDE the bundles dir (like .vector-cache): the bundle
        # glob only matches *.mbtiles/*.pmtiles, so the subdirectory is inert,
        # and two MapsApi instances on different bundle sets never share jobs.
        self.provision = ProvisionEngine(self, bundles_dir / ".provision")

    # -- bundle handling -------------------------------------------------

    def _bundle_path(self, bundle_id: str) -> Path:
        if not bundle_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="invalid bundle id")
        return self.bundles_dir / f"{bundle_id}.mbtiles"

    def _open_bundle(self, bundle_id: str, *, source: TileSource | None = None) -> MBTilesBundle:
        bundle = self._bundles.get(bundle_id)
        if bundle is None:
            path = self._bundle_path(bundle_id)
            if source is not None:
                bundle = MBTilesBundle.create(path, source)
            elif path.exists():
                bundle = MBTilesBundle(path)
            else:
                raise HTTPException(status_code=404, detail=f"unknown bundle {bundle_id}")
            self._bundles[bundle_id] = bundle
        return bundle

    async def _remote_planet(self) -> RemotePMTiles | None:
        """The planet archive, or None when offline or unreachable.

        Never raises: a browsing session must degrade to whatever is on disk
        rather than fail, which is the same contract the raster proxy has.
        """
        if self.mode == "offline" or not self.remote_planet_enabled:
            return None
        if self._remote is None:
            try:
                client = await self._live_client()
                url = await self.extractor.resolve_planet_url(client=client)
                remote = RemotePMTiles(url, client)
                await remote.header()          # proves it answers ranges
                self._remote = remote
            except (RemotePMTilesError, ExtractError, httpx.HTTPError, OSError) as error:
                print(f"[maps] remote planet unavailable: {error}")
                return None
        return self._remote

    def _vector_bundles(self) -> dict[str, PMTilesBundle]:
        for path in sorted(self.bundles_dir.glob("*.pmtiles")):
            if path.stem not in self._vector:
                self._vector[path.stem] = PMTilesBundle(path)
        return self._vector

    async def _vector_tile_bytes(self, z: int, x: int, y: int) -> bytes | None:
        """One vector tile from the best source available, or None.

        Extracted from the tile route so anything that needs to READ a tile
        rather than serve it — the street survey below — resolves it through
        exactly the same ladder: most detailed local extract first, then the
        cache, then a range request into the remote planet.
        """
        coord = TileCoord(z=z, x=x, y=y)
        west, south, east, north = coord.bbox_deg()
        candidates = []
        for bundle in self._vector_bundles().values():
            if z > bundle.max_zoom():
                continue
            bounds = bundle.bounds()
            if bounds.west > east or bounds.east < west or bounds.south > north or bounds.north < south:
                continue
            candidates.append(bundle)
        for bundle in sorted(candidates, key=lambda b: -b.max_zoom()):
            data = bundle.get_tile(z, x, y)
            if data is not None:
                return data

        cached = self.vector_cache.get(z, x, y)
        if cached is not None:
            return cached or None

        remote = await self._remote_planet()
        if remote is None:
            return None
        try:
            data = await remote.get_tile(z, x, y)
        except (RemotePMTilesError, httpx.HTTPError) as error:
            raise HTTPException(status_code=502, detail=str(error))
        self.vector_cache.put(z, x, y, data or b"")
        return data or None

    def _guaranteed_zoom(
        self, area: BBoxDeg, best: int, bundles: dict[str, "PMTilesBundle"]
    ) -> int:
        """Deepest zoom at which EVERY tile over `area` can actually be served.

        Asking "does one bundle's bbox contain the whole area" was too strict
        in both directions. A minted region's stored bounds are tile-aligned
        and can fall a hair inside the box it was asked for, so a region that
        genuinely covers the screen reported as not covering it — the client
        then clamped back to the nationwide z10, drew no streets, and minted
        the neighbouring region again. And two adjacent extracts that together
        cover the view were never credited at all.

        Tiles are the unit the client actually renders, so they are the unit to
        answer in. Walking down from the best available zoom stops at the first
        fully-served level, which is normally the very first check.
        """
        # Start above `best`, not at it: an area may have no extract at all and
        # still be fully usable from tiles cached while browsing, which is
        # exactly the state a laptop is in when the network drops.
        for zoom in range(max(best, MAX_VECTOR_ZOOM), 0, -1):
            if self._tiles_available(area, zoom, bundles):
                return zoom
        return 0

    # How many tiles one level may be probed with. A viewport at z15 is
    # several hundred tiles; rather than give up past the cap (which would
    # silently deny the deepest level that exists), the range is sampled on a
    # grid — a hole big enough to matter visually spans many tiles.
    _COVERAGE_PROBE_LIMIT = 12   # per axis, so at most 144 lookups per level

    def _tiles_available(
        self, area: BBoxDeg, zoom: int, bundles: dict[str, "PMTilesBundle"]
    ) -> bool:
        # Only bundles that reach this zoom AND overlap the area can help.
        # Without the bounds filter every tile was tested against every archive
        # on disk, which took seconds once a few regions had been minted.
        usable = [
            bundle for bundle in bundles.values()
            if bundle.max_zoom() >= zoom and bundle.bounds().intersects(area)
        ]
        x0, y0 = _tile_xy(area.west, area.north, zoom)
        x1, y1 = _tile_xy(area.east, area.south, zoom)
        for x in _probe_range(x0, x1, self._COVERAGE_PROBE_LIMIT):
            for y in _probe_range(y0, y1, self._COVERAGE_PROBE_LIMIT):
                # Tiles pulled from the planet while browsing count as much as
                # tiles in an extract: they are on this disk either way, which
                # is what makes an area usable once the network is gone.
                if self.vector_cache.has(zoom, x, y):
                    continue
                if not any(bundle.get_tile(zoom, x, y) for bundle in usable):
                    return False
        return True

    def _source(self, source_id: str) -> TileSource:
        source = self.sources.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail=f"unknown source {source_id}")
        return source

    def _source_or_composite(self, source_id: str) -> TileSource | CompositeSource:
        if source_id in self.composites:
            return self.composites[source_id]
        return self._source(source_id)

    def _resolve_members(self, composite: CompositeSource, coord: TileCoord) -> list[TileSource]:
        """Members that may hold this tile, most specific first.

        Selection is by bbox *intersection*, not by the tile centre: a wide
        low-zoom tile spans several states and all of them contribute a piece
        of the mosaic. Coverage bboxes also overlap (Berlin sits inside
        Brandenburg's box), so the list is ordered smallest-first — the most
        specific source wins the single-fetch case and lands on top of the
        mosaic stack.
        """
        west, south, east, north = coord.bbox_deg()
        tile_box = BBoxDeg(west=west, south=south, east=east, north=north)
        candidates = [
            member
            for member_id in composite.member_ids
            if (member := self.sources.get(member_id)) is not None
            # Members own a zoom band as well as an area: the 10 m Sentinel
            # mosaic answers regional zooms, the 20 cm state imagery answers
            # city zooms, and neither is asked outside its range.
            and member.min_zoom <= coord.z <= member.max_zoom
            and (member.coverage is None or member.coverage.intersects(tile_box))
        ]
        return sorted(
            candidates,
            key=lambda m: m.coverage.area_deg2 if m.coverage else float("inf"),
        )

    def _resolve_member(self, composite: CompositeSource, coord: TileCoord) -> TileSource | None:
        members = self._resolve_members(composite, coord)
        return members[0] if members else None

    async def _live_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Explicit limits and a short timeout. A state WMS that stalls must
            # fail fast so the tile 404s and the basemap shows through —
            # otherwise slow upstreams pile up and the map looks stuck loading.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(LIVE_TILE_TIMEOUT_S, connect=5.0),
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
            )
        return self._client

    def _require_provisioning_allowed(self, what: str) -> None:
        """Bulk data-provisioning actions: harvest, harvest-world, vector/extract.

        These mint new local data (a whole raster pyramid, a regional
        `.pmtiles`) rather than answer the current request — refused in BOTH
        "mixed" and "offline", not just "offline", so an authoring session
        set to "mixed" (live map, no silent gigabyte downloads) actually
        keeps that promise.
        """
        if self.mode != "online":
            raise HTTPException(status_code=403, detail=f"{what} requires online mode")

    def _require_network_allowed(self, what: str) -> None:
        """Live, per-request, non-provisioning actions: routing, geocoding,
        the live tile proxy, one-shot boundary GeoJSON, glyphs, sprites.

        Refused only in "offline" — "mixed" allows these exactly like
        "online" does, since none of them stockpile data for later offline
        use.
        """
        if self.mode == "offline":
            raise HTTPException(status_code=403, detail=f"{what} is disabled in offline mode")

    async def _harvest_spec(self, spec: MapSpec) -> HarvestReport:
        source = self._source_or_composite(spec.source_id)
        plan = HarvestPlanner(source).plan(spec)
        if isinstance(source, CompositeSource):
            bundle_source = self._source(source.member_ids[0]).model_copy(
                update={"id": source.id, "attribution": self._composite_attribution(source)}
            )
        else:
            bundle_source = source
        bundle = self._open_bundle(spec.id, source=bundle_source)

        if not isinstance(source, CompositeSource):
            return await Harvester(source, **self._blend_kwargs(source)).harvest(plan, bundle)

        # Composite: group tiles by their candidate ladder (tiles over the same
        # set of states share one group), then harvest each group against its
        # primary with the rest as fallbacks for border tiles.
        groups: dict[tuple[str, ...], list[TileCoord]] = {}
        uncovered = 0
        for coord in plan.tiles:
            members = self._resolve_members(source, coord)
            if not members:
                uncovered += 1
            else:
                groups.setdefault(tuple(m.id for m in members), []).append(coord)
        merged = HarvestReport(
            bundle_id=spec.id, planned=plan.tile_count, fetched=0,
            skipped_existing=0, failed=0, uncovered=uncovered, bytes_fetched=0,
        )
        for member_ids, coords in groups.items():
            members = [self._source(member_id) for member_id in member_ids]
            sub_plan = HarvestPlan(spec_id=spec.id, source_id=member_ids[0], tiles=coords)
            harvester = Harvester(members[0], alternates=members[1:], mosaic=True)
            report = await harvester.harvest(sub_plan, bundle)
            merged.fetched += report.fetched
            merged.skipped_existing += report.skipped_existing
            merged.failed += report.failed
            merged.uncovered += report.uncovered
            merged.bytes_fetched += report.bytes_fetched
        return merged

    def _blend_kwargs(self, source: TileSource) -> dict:
        """Harvester kwargs for an ocean-blended source (see `_fetch_blend`).

        Empty when the source does not blend, the named ocean source is not
        configured, or the land-polygon data is missing — the Harvester then
        serves the plain land layer, degradation instead of failure.
        """
        if not source.ocean_blend_source_id:
            return {}
        ocean = self.sources.get(source.ocean_blend_source_id)
        if ocean is None or not self.land_mask.available:
            return {}
        return {"ocean_source": ocean, "land_mask": self.land_mask}

    def _world_source(self) -> TileSource | None:
        """The world-scale imagery layer of the ladder (z0-8).

        The ocean-blended `blue-marble-plus` when configured, plain
        `blue-marble` otherwise; None for hosts that configured neither
        (the world layer is a convention, not a MapsApi requirement).
        """
        return self.sources.get("blue-marble-plus") or self.sources.get("blue-marble")

    async def _live_source_tile(self, source_id: str, coord: TileCoord) -> bytes | None:
        """One tile through the live cache-through path, or None where the
        source genuinely has nothing (out of coverage, blank WMS answer).

        Shared by the per-source proxy (`/live/...`) and the unified aerial
        dispatcher (`/aerial/...`), so both serve — and cache into the same
        `live-{source_id}` bundle — identical bytes for the same tile.
        Raises HTTPException(502) on upstream transport errors.
        """
        source = self._source_or_composite(source_id)
        if isinstance(source, CompositeSource):
            members = self._resolve_members(source, coord)
            if not members:
                return None
            bundle_source = members[0].model_copy(
                update={"id": source.id, "attribution": self._composite_attribution(source)}
            )
        else:
            members = [source]
            bundle_source = source

        bundle = self._open_bundle(f"live-{source_id}", source=bundle_source)
        data = bundle.get_tile(coord)
        if data is None:
            harvester = Harvester(
                members[0],
                alternates=members[1:],
                mosaic=isinstance(source, CompositeSource),
                timeout_s=LIVE_TILE_TIMEOUT_S,
                # Ocean-blended sources (blue-marble-plus) composite two
                # upstream layers through the land mask; empty for all
                # other sources. Cached blended below like any tile, so
                # the two-fetch cost is cold-cache only.
                **self._blend_kwargs(members[0]),
            )
            if self._live_semaphore is None:
                self._live_semaphore = asyncio.Semaphore(LIVE_TILE_CONCURRENCY)
            try:
                async with self._live_semaphore:
                    data = await harvester.fetch_tile(await self._live_client(), coord)
            except NoImagery:
                # Sea, abroad or a state data gap: a real answer, not an error.
                return None
            except Exception as error:  # noqa: BLE001 - surfaced as upstream error
                raise HTTPException(status_code=502, detail=f"upstream tile fetch failed: {error}")
            bundle.put_tile(coord, data)
        return data

    async def fetch_aerial_band_tile(self, source_id: str, coord: TileCoord) -> bytes | None:
        """One tile of the aerial ladder through the SAME cache-through path
        the `/aerial` dispatcher serves from (shared `live-{source_id}`
        bundles, shared politeness limits). Public because the provisioning
        engine walks whole regions through it — a bulk job that used any
        other pipeline would mint a second cache the dispatcher never reads.
        Returns None where the band genuinely has no imagery."""
        return await self._live_source_tile(source_id, coord)

    def refresh_vector_bundle(self, path: Path) -> None:
        """(Re)register a freshly-minted .pmtiles extract with the running
        instance — the same wiring `/vector/extract` performs after minting,
        shared with the provisioning engine's maps layer."""
        self._vector[path.stem] = PMTilesBundle(path)

    def _aerial_fallbacks(
        self, source: TileSource | CompositeSource, world: TileSource | None,
        coord: TileCoord,
    ) -> list[tuple[str, int]]:
        """Coarser ladder bands that may still cover `coord`, finest first.

        Each entry is (source_id, ancestor_zoom): the zoom the band tops out
        at, which is where the dispatcher reads the ancestor tile to upscale
        from. Composite members contribute their own band ceilings (the 20 cm
        states never appear here — asking a state's z18 for a failed z19 would
        fail on the same coverage hole); the world layer is always last.
        """
        west, south, east, north = coord.bbox_deg()
        tile_box = BBoxDeg(west=west, south=south, east=east, north=north)
        candidates: list[tuple[str, int]] = []
        if isinstance(source, CompositeSource):
            for member_id in source.member_ids:
                member = self.sources.get(member_id)
                if member is None or member.max_zoom >= coord.z:
                    continue                       # not a coarser band
                if member.coverage is not None and not member.coverage.intersects(tile_box):
                    continue
                candidates.append((member.id, member.max_zoom))
        if world is not None and world.max_zoom < coord.z:
            candidates.append((world.id, world.max_zoom))
        seen: set[str] = set()
        ordered: list[tuple[str, int]] = []
        for source_id, zoom in sorted(candidates, key=lambda entry: -entry[1]):
            if source_id in seen:
                continue
            seen.add(source_id)
            ordered.append((source_id, zoom))
        return ordered

    def _aerial_best_native_zoom(self, source_id: str, box: BBoxDeg) -> int:
        """Deepest zoom ANY band of the aerial ladder serves natively over `box`.

        The same coverage facts the `/aerial` dispatcher routes tiles with
        (member coverage boxes + zoom ceilings, the world layer beneath
        everything), reduced to pure bbox math — no tile is fetched. Past
        this zoom the dispatcher can only serve parent-crop upscales there;
        -1 when no band covers the box at all (no world layer configured).
        """
        source = self._source_or_composite(source_id)
        world = self._world_source()
        best = world.max_zoom if world is not None else -1
        members = (
            [self.sources[m] for m in source.member_ids if m in self.sources]
            if isinstance(source, CompositeSource) else [source]
        )
        for member in members:
            if member.coverage is None or member.coverage.intersects(box):
                best = max(best, member.max_zoom)
        return best

    async def _upscaled_tile(self, source_id: str, coord: TileCoord, at_zoom: int) -> bytes | None:
        """`coord` synthesized from its ancestor at `at_zoom` of `source_id`.

        The same parent-crop-and-upscale the offline fallback ladder performs
        (storage/fallback.py), but against the live cache-through path — the
        ancestor fetch itself is cached in the source's live bundle, only the
        crop/resize is per-request. Fractional crop boxes keep this working
        even many levels down (the crop simply gets blurrier, never wrong).
        Returns None without Pillow, when the ancestor has no imagery either,
        or when `at_zoom` is not actually coarser.
        """
        try:
            from PIL import Image
        except ImportError:
            return None
        import io

        levels = coord.z - at_zoom
        if levels <= 0:
            return None
        ancestor = TileCoord(z=at_zoom, x=coord.x >> levels, y=coord.y >> levels)
        data = await self._live_source_tile(source_id, ancestor)
        if data is None:
            return None
        image = Image.open(io.BytesIO(data)).convert("RGB")
        scale = 1 << levels
        span = image.width / scale
        offset_x = (coord.x - (ancestor.x << levels)) * span
        offset_y = (coord.y - (ancestor.y << levels)) * span
        crop = image.resize(
            (256, 256), Image.BILINEAR,
            box=(offset_x, offset_y, offset_x + span, offset_y + span),
        )
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=AERIAL_SYNTH_JPEG_QUALITY)
        return out.getvalue()

    def _composite_attribution(self, composite: CompositeSource) -> str:
        parts = [self.sources[m].attribution for m in composite.member_ids if m in self.sources]
        return " | ".join(dict.fromkeys(parts))

    def _country_display_names(self) -> dict[str, str]:
        """ISO alpha-2 → English country name, from the boundary index.

        Cached: the GeoNames search path maps its country codes through this
        on every autocomplete answer. Empty when the boundary file is not on
        disk — the ISO code is then shown as-is rather than failing.
        """
        if self._country_names is None:
            self._country_names = (
                {hit.iso: hit.name for hit in self.countries.hits() if hit.iso}
                if self.countries.available else {}
            )
        return self._country_names

    def _data_attribution(self, spec: MapSpec) -> str:
        """The exported-page / payload credit line: imagery + map data.

        OpenStreetMap and Blue Marble are unconditional (streets and the
        globe are always in play); GeoNames joins when the dense place index
        is the one labelling routes "über Pforzheim" (CC BY requires the
        credit wherever those names are shown).
        """
        text = self.spec_attribution(spec) + " | © OpenStreetMap | NASA Blue Marble"
        if self.geonames.available and any(
            candidate.via_places
            for route in spec.routes
            for candidate in (route, *route.alternates)
        ):
            text += f" | {GEONAMES_ATTRIBUTION}"
        return text

    def spec_attribution(self, spec: MapSpec) -> str:
        """Attribution for the sources a spec actually uses.

        Credits only the composite members that cover one of the spec's camera
        stops — keeps the on-map line short (licenses require attribution
        'reasonable to the medium', not an exhaustive wall of text). Only the
        primary (smallest-coverage) candidate per stop is named here; the
        sealed package's manifest carries the full federation license list, so
        a border tile served by a fallback state is still credited there.
        """
        source = self._source_or_composite(spec.source_id)
        if not isinstance(source, CompositeSource):
            return source.attribution
        used: dict[str, None] = {}
        for loc in spec.locations:
            coord = TileCoord.at(loc.camera.center.lat, loc.camera.center.lon, 14)
            member = self._resolve_member(source, coord)
            if member is not None:
                used[member.attribution] = None
        return " | ".join(used) if used else self._composite_attribution(source)

    def build_payload(
        self, spec: MapSpec, *,
        lang: str = DEFAULT_LANGUAGE,
        default_view: str = "hybrid",
        stage_asset_prefix: str = "/_stage",
    ) -> MapPayload:
        """Assemble everything `web/map.js` needs to mount `spec`.

        Shared by the dev server's own demo endpoint (`GET /api/demo-spec`)
        and the render pipeline (`POST /render`) — both need the identical
        tile/vector/glyph wiring a live browsing session gets, or a render
        could silently show something a person never would.

        URLs are the literal `/api/v1/maps/...` paths, matching how
        `web/view.html` and `web/render.html` already hardcode them: a host
        that mounts `MapsApi` at a different `prefix` would need to adjust
        those pages too, same pre-existing limitation the demo endpoint has
        always had.
        """
        source = self._source_or_composite(spec.source_id)
        vector = self._vector_bundles()
        # The world layer of the ladder: the ocean-blended `blue-marble-plus`
        # when configured, plain `blue-marble` otherwise. Not every host
        # configures one (it is a devserver.py/demo convention, not a hard
        # requirement of MapsApi) — skip the world layer entirely rather than
        # KeyError on a custom `sources` dict that never had one.
        world_source = self._world_source()
        # Live display path: ONE aerial source for the whole zoom range (see
        # the `/aerial` handler) so MapLibre's pitched-view pyramid LOD can
        # pull coarse tiles for the far rows. The per-band templates below
        # stay in the payload regardless: the sealed/offline pipeline
        # (harvest, package, seal/sealer.py) records and rewrites the
        # per-band sources, and offline mode serves bundles per band — the
        # unified source is a LIVE convenience, never the offline contract.
        aerial_template = None
        if self.mode == "offline":
            tiles_template = f"/api/v1/maps/bundles/{spec.id}/tiles/{{z}}/{{x}}/{{y}}"
            world_template = "/api/v1/maps/bundles/world/tiles/{z}/{x}/{y}"
        else:
            aerial_template = f"/api/v1/maps/aerial/{source.id}/{{z}}/{{x}}/{{y}}"
            # "mixed" and "online" both keep the live tile proxy: the live
            # proxy endpoint (`/live/...`) is only refused in "offline"
            # (`_require_network_allowed`), so pointing at bundle-only tiles
            # here for "mixed" would under-serve a mode that is allowed to
            # fetch live.
            tiles_template = f"/api/v1/maps/live/{source.id}/{{z}}/{{x}}/{{y}}"
            world_template = (
                f"/api/v1/maps/live/{world_source.id}/{{z}}/{{x}}/{{y}}"
                if world_source else "/api/v1/maps/live/blue-marble/{z}/{x}/{y}"
            )
        return MapPayload(
            spec=spec,
            tiles_url_template=tiles_template,
            aerial_url_template=aerial_template,
            aerial_max_zoom=max(
                source.max_zoom, world_source.max_zoom if world_source else 0),
            world_tiles_url_template=world_template if world_source else None,
            world_max_zoom=world_source.max_zoom if world_source else 8,
            tile_min_zoom=source.min_zoom,
            tile_max_zoom=source.max_zoom,
            attribution=self._data_attribution(spec),
            # Always offered, even with zero local extracts: `/vector/auto/
            # tiles` already falls back local bundle -> remote-planet range
            # read -> cache on its own (see that handler), so gating this on
            # `vector` being non-empty only defeated that fallback and left
            # every pure-cartography view (Karte/Dunkel) blank whenever no
            # region had been minted yet — which, for a fresh install with no
            # harvested bundles, is every view. Offline mode still degrades
            # correctly on its own: `_remote_planet()` returns None then, so
            # an empty `vector` dict just means every tile 404s, same as
            # `None` did here before.
            vector_url_template="/api/v1/maps/vector/auto/tiles/{z}/{x}/{y}.mvt",
            vector_max_zoom=max((b.max_zoom() for b in vector.values()), default=15),
            glyphs_url_template="/api/v1/maps/assets/glyphs/{fontstack}/{range}.pbf",
            sprites_base_url="/api/v1/maps/assets/sprites/v4",
            basemap_flavor="dark",
            default_view=default_view,
            countries_url="/api/v1/maps/geo/countries",
            country_labels_url="/api/v1/maps/geo/countries/labels",
            lang=lang,
            languages=list(LANGUAGES),
            offline=self.mode == "offline",
            standalone=self.standalone,
            stage_asset_prefix=stage_asset_prefix,
        )

    def public_base_url(self) -> str:
        """The externally-reachable base of THIS server instance.

        Same resolution `POST /render` uses for Playwright: an explicit
        `render_base_url` (the dev server passes its real host:port, the
        stdio MCP entry its loopback render host) wins, then the env var,
        then the dev-server default. Session URLs are composed against this,
        so both transports hand out links that actually open.
        """
        return (
            self.render_base_url
            or os.environ.get(RENDER_BASE_URL_ENV, "").strip()
            or DEFAULT_RENDER_BASE_URL
        ).rstrip("/")

    def _annotate_via_places(self, route: MapRoute) -> None:
        """Attach "via Pforzheim" facts to a fresh routing result.

        Primary and every alternate get their own priority-ordered list —
        the alternates are exactly where the labels earn their keep (two
        candidates told apart by where they go). Skipped silently when the
        offline city index is not on disk: via places are an enrichment,
        never a reason for a route to fail.

        Source preference: the dense GeoNames index (~32k places — the one
        that knows Pforzheim and Heilbronn) when its asset exists, else the
        Natural Earth index (~7,300).
        """
        if self.geonames.available:
            places = self.geonames.places()
            buckets = self.geonames.buckets()
        elif self.cities.available:
            places = self.cities.places()
            buckets = None
        else:
            return
        for candidate in (route, *route.alternates):
            candidate.via_places = find_via_places(candidate.geometry, places, buckets=buckets)

    def _store_session(self, spec: MapSpec) -> str:
        """Mint a browser-session token, sweeping expired entries first."""
        now = time.time()
        for key in [k for k, (expires_at, _) in self._sessions.items() if expires_at < now]:
            del self._sessions[key]
            self._session_sources.pop(key, None)
        token = uuid.uuid4().hex
        self._sessions[token] = (now + SESSION_TTL_S, spec)
        return token

    def bind_session_source(
        self,
        token: str,
        spec_fn: Callable[[], MapSpec | None],
        revision_fn: Callable[[], int | None],
    ) -> None:
        """Make an existing session serve LIVE state instead of its frozen copy.

        The MCP layer binds a live trip here (open_map_view(trip_id=...)):
        every `GET /sessions/{token}` then rebuilds the spec from the trip's
        CURRENT state and `GET /sessions/{token}/revision` answers the trip's
        revision counter — the machinery behind "the open tab updates itself
        instead of a new tab per call". Both callables answer None once the
        live object is gone (trip expired/evicted); the session then falls
        back to the last frozen copy rather than breaking the open tab.
        """
        if token not in self._sessions:
            raise KeyError(f"unknown session token {token!r}")
        self._session_sources[token] = (spec_fn, revision_fn)

    def session_alive(self, token: str) -> bool:
        """Whether a session token is still redeemable (mint-time TTL or the
        poll-refreshed one, see `_session_entry`). The MCP layer asks this
        before REUSING a per-trip token instead of minting a new one."""
        entry = self._sessions.get(token)
        return entry is not None and entry[0] >= time.time()

    def _session_entry(self, token: str) -> tuple[MapSpec, int | None]:
        """(spec, revision) for a token: the live source's current state when
        one is bound (refreshing the TTL — a tab that still polls keeps its
        session alive past the mint-time hour), else the frozen copy with
        revision None (readers then report the constant 1)."""
        entry = self._sessions.get(token)
        if entry is None or entry[0] < time.time():
            self._sessions.pop(token, None)
            self._session_sources.pop(token, None)
            raise HTTPException(status_code=404, detail="session unknown or expired")
        source = self._session_sources.get(token)
        if source is not None:
            spec = source[0]()
            if spec is not None:
                # Keep the frozen copy current too: it is the fallback the
                # tab gets once the live trip expires before the session.
                self._sessions[token] = (time.time() + SESSION_TTL_S, spec)
                return spec, source[1]()
        return entry[1], None

    def _session_spec(self, token: str) -> MapSpec:
        return self._session_entry(token)[0]

    def _store_render_payload(self, payload: MapPayload) -> str:
        """Mint a one-time-ish token `web/render.html` fetches the payload with.

        Not popped on first read: `render.html`'s own fetch could in
        principle retry (a flaky first byte), and popping would turn that
        retry into a 404 instead of a render failure that at least reports
        cleanly. A TTL sweep bounds memory instead.
        """
        now = time.time()
        expired = [key for key, (expires_at, _) in self._render_payloads.items() if expires_at < now]
        for key in expired:
            del self._render_payloads[key]
        token = uuid.uuid4().hex
        self._render_payloads[token] = (now + RENDER_TOKEN_TTL_S, payload)
        return token

    # -- mounting --------------------------------------------------------

    def mount(self, app: FastAPI, *, prefix: str = "/api/v1/maps") -> None:
        @app.get(f"{prefix}/sources", response_model=list[TileSource])
        async def list_sources() -> list[TileSource]:
            return list(self.sources.values())

        @app.get(f"{prefix}/composites", response_model=list[CompositeSource])
        async def list_composites() -> list[CompositeSource]:
            return list(self.composites.values())

        @app.post(f"{prefix}/plan", response_model=PlanPreview)
        async def plan(spec: MapSpec) -> PlanPreview:
            source = self._source_or_composite(spec.source_id)
            try:
                harvest_plan = HarvestPlanner(source).plan(spec)
            except ValueError as error:
                # e.g. a spec with neither locations nor an overview — a
                # caller mistake, not a server fault, so 400 rather than an
                # unhandled 500.
                raise HTTPException(status_code=400, detail=str(error))
            return PlanPreview(
                spec_id=spec.id,
                source_id=source.id,
                tile_count=harvest_plan.tile_count,
                estimated_mb=harvest_plan.estimated_mb,
            )

        @app.post(f"{prefix}/harvest", response_model=HarvestReport)
        async def harvest(spec: MapSpec) -> HarvestReport:
            self._require_provisioning_allowed("harvest")
            try:
                return await self._harvest_spec(spec)
            except ValueError as error:
                # Same contract as /plan: an unplannable spec is the caller's
                # mistake, reported as one.
                raise HTTPException(status_code=400, detail=str(error))

        @app.post(f"{prefix}/harvest-world", response_model=HarvestReport)
        async def harvest_world(request: WorldHarvestRequest) -> HarvestReport:
            self._require_provisioning_allowed("harvest")
            # Prewarm with the blended world layer when it is configured, so
            # a sealed offline package carries the same bathymetry oceans the
            # live map shows; plain blue-marble hosts keep working unchanged.
            source = self._world_source() or self._source("blue-marble")
            tiles: set[TileCoord] = set()
            for z in range(0, min(request.global_maxzoom, source.max_zoom) + 1):
                n = 1 << z
                tiles.update(TileCoord(z=z, x=x, y=y) for x in range(n) for y in range(n))
            if request.region_west is not None:
                for z in range(request.global_maxzoom + 1, min(request.region_maxzoom, source.max_zoom) + 1):
                    nw = TileCoord.at(request.region_north, request.region_west, z)
                    se = TileCoord.at(request.region_south, request.region_east, z)
                    tiles.update(
                        TileCoord(z=z, x=x, y=y)
                        for x in range(nw.x, se.x + 1)
                        for y in range(nw.y, se.y + 1)
                    )
            plan = HarvestPlan(
                spec_id="world", source_id=source.id,
                tiles=sorted(tiles, key=lambda t: (t.z, t.x, t.y)),
            )
            bundle = self._open_bundle("world", source=source)
            return await Harvester(
                source, concurrency=8, **self._blend_kwargs(source)
            ).harvest(plan, bundle)

        @app.post(f"{prefix}/provision/estimate", response_model=ProvisionEstimate)
        async def provision_estimate(request: ProvisionRequest) -> ProvisionEstimate:
            """Size forecast for a region×layers selection — pure math, free
            in every runtime mode (the same planning-is-free contract
            /plan has). This is the number a caller confirms BEFORE
            /provision ever moves a byte."""
            try:
                return estimate_region(request)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error))

        @app.post(f"{prefix}/provision", response_model=ProvisionJob)
        async def provision(request: ProvisionRequest) -> ProvisionJob:
            """Start a region-bulk provisioning job (async; poll GET
            /provision/{job_id}). Bulk data minting, so gated exactly like
            harvest/vector-extract: online mode only."""
            self._require_provisioning_allowed("region provisioning")
            try:
                return self.provision.start(request)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error))

        @app.get(f"{prefix}/provision", response_model=list[ProvisionJob])
        async def provision_list() -> list[ProvisionJob]:
            """All known jobs, newest first — including interrupted ones
            persisted across a server restart."""
            return self.provision.list()

        @app.get(prefix + "/provision/{job_id}", response_model=ProvisionJob)
        async def provision_status(job_id: str) -> ProvisionJob:
            job = self.provision.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"unknown provisioning job {job_id}")
            return job

        @app.post(prefix + "/provision/{job_id}/cancel", response_model=ProvisionJob)
        async def provision_cancel(job_id: str) -> ProvisionJob:
            job = self.provision.cancel(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"unknown provisioning job {job_id}")
            return job

        @app.post(f"{prefix}/matrix", response_model=MatrixResult)
        async def matrix(request: MatrixRequest) -> MatrixResult:
            """Reachability matrix (authoring-time; embed results in slides).

            With `sources`/`targets` index lists the answer is asymmetric —
            rows × columns, exactly what both backends solve natively
            (OSRM `table` `sources`/`destinations`, Valhalla
            `sources_to_targets`) — so one-to-many never pays NxN.
            """
            self._require_network_allowed("routing")
            for name, indices in (("sources", request.sources), ("targets", request.targets)):
                if indices is not None and any(
                        not 0 <= i < len(request.points) for i in indices):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{name} must be indices into points "
                               f"(0..{len(request.points) - 1})")
            try:
                durations = await self.router.matrix(
                    request.points, mode=request.mode,
                    sources=request.sources, targets=request.targets)
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(status_code=502, detail=f"matrix failed: {error}")
            return MatrixResult(durations_min=durations)

        @app.post(f"{prefix}/route/optimize", response_model=OptimizedRoute)
        async def optimize_route(request: OptimizeRouteRequest) -> OptimizedRoute:
            """TSP stop ordering + the route driven in that order.

            Same authoring-time contract as /route; the backend's native
            solver does the ordering (OSRM `trip`, Valhalla
            `optimized_route`).
            """
            self._require_network_allowed("routing")
            if len(request.stops) < 2:
                raise HTTPException(status_code=400, detail="optimize needs at least 2 stops")
            try:
                result = await self.router.optimized_route(
                    request.stops,
                    route_id=request.route_id,
                    mode=request.mode,
                    roundtrip=request.roundtrip,
                    keep_endpoints=request.keep_endpoints,
                    from_location=request.from_location,
                    to_location=request.to_location,
                    steps=request.steps,
                )
            except NotImplementedError as error:
                raise HTTPException(status_code=501, detail=str(error))
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(status_code=502, detail=f"optimize failed: {error}")
            self._annotate_via_places(result.route)
            return result

        @app.post(f"{prefix}/package", response_model=PackageManifest)
        async def package(spec: MapSpec) -> PackageManifest:
            """Zip up an already-harvested spec (`package/builder.py`).

            Deliberately ungated by either `_require_provisioning_allowed` or
            `_require_network_allowed`, in every mode including "offline":
            `PackageBuilder.build` only reads bundles/vector extracts/glyphs
            already on this disk and writes a zip — no network call of its
            own, and no NEW data gets minted (that already happened at
            harvest/extract time, which ARE gated). It 409s on its own,
            honestly, when the raster bundle a spec needs was never
            harvested — see `PackageBuilder.build`'s `FileNotFoundError`.
            """
            # `spec.id` becomes two filesystem paths below (the raster bundle
            # it reads, the zip it writes) — run it through the same id
            # validation every bundle route uses, so "../../…" cannot read an
            # .mbtiles from, or write a zip to, anywhere outside bundles_dir.
            self._bundle_path(spec.id)
            builder = PackageBuilder(self.bundles_dir, self.assets_dir)
            out = self.bundles_dir / "packages" / f"{spec.id}.zip"
            try:
                return builder.build(spec, out)
            except FileNotFoundError as error:
                raise HTTPException(status_code=409, detail=str(error))

        @app.get(f"{prefix}/bundles", response_model=list[BundleInfo])
        async def list_bundles() -> list[BundleInfo]:
            return [
                self._open_bundle(path.stem).info()
                for path in sorted(self.bundles_dir.glob("*.mbtiles"))
            ]

        @app.get(prefix + "/bundles/{bundle_id}/tiles/{z}/{x}/{y}")
        async def bundle_tile(bundle_id: str, z: int, x: int, y: int) -> Response:
            bundle = self._open_bundle(bundle_id)
            resolved = FallbackTileResolver(bundle).resolve(TileCoord(z=z, x=x, y=y))
            if resolved is None:
                raise HTTPException(status_code=404, detail="tile not in bundle")
            data, kind = resolved
            return Response(
                content=data,
                media_type=_sniff_media_type(data, bundle.info().tile_format),
                headers={"X-Agentic-Maps-Tile-Fallback": kind},
            )

        @app.get(prefix + "/vector", response_model=list[VectorBundleInfo])
        async def list_vector() -> list[VectorBundleInfo]:
            return [
                VectorBundleInfo(
                    id=name,
                    min_zoom=bundle.min_zoom(),
                    max_zoom=bundle.max_zoom(),
                    bounds=bundle.bounds(),
                    size_bytes=bundle.path.stat().st_size,
                )
                for name, bundle in self._vector_bundles().items()
            ]

        @app.get(f"{prefix}/geo/countries")
        async def countries() -> Response:
            """Worldwide borders as GeoJSON — the one layer that is never
            regional, so a scenario always knows where in the world it is."""
            if not self.countries.available:
                self._require_network_allowed("country boundaries")
                await self.countries.ensure(await self._live_client())
            return Response(
                content=json.dumps(self.countries.collection()),
                media_type="application/geo+json",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get(f"{prefix}/geo/countries/labels")
        async def country_labels() -> Response:
            """One label point per country — polygons would repeat per tile."""
            if not self.countries.available:
                self._require_network_allowed("country boundaries")
                await self.countries.ensure(await self._live_client())
            return Response(
                content=json.dumps(self.countries.label_points()),
                media_type="application/geo+json",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get(f"{prefix}/geo/physical")
        async def physical() -> Response:
            """Lakes, rivers and major roads — the texture a small-scale map needs."""
            if not self.physical.available:
                self._require_network_allowed("physical geography")
                await self.physical.ensure(await self._live_client())
            return Response(
                content=json.dumps(self.physical.collection()),
                media_type="application/geo+json",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get(f"{prefix}/geo/cities")
        async def cities() -> Response:
            """Major cities worldwide, ranked — label layer for the globe."""
            if not self.cities.available:
                self._require_network_allowed("city labels")
                await self.cities.ensure(await self._live_client())
            return Response(
                content=json.dumps(self.cities.collection()),
                media_type="application/geo+json",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get(f"{prefix}/geo/oceans")
        async def oceans() -> Response:
            """Ocean and sea names as label points — the globe's water layer."""
            if not self.oceans.available:
                self._require_network_allowed("ocean labels")
                await self.oceans.ensure(await self._live_client())
            return Response(
                content=json.dumps(self.oceans.collection()),
                media_type="application/geo+json",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get(f"{prefix}/geo/cities/search")
        async def search_cities(
            q: str,
            limit: int = 6,
            near_lat: float | None = None,
            near_lon: float | None = None,
        ) -> list[dict]:
            """Autocomplete over the offline city index, ranked near-first.

            This exists because Nominatim is an address resolver, not an
            autocomplete: for a two-letter prefix it returns whatever is
            globally "important" and omits the obvious neighbours. The
            populated-places index answers instantly, offline, and is ranked
            by population, proximity and the country the viewer stands in.

            Runs on the dense GeoNames index when its asset exists — small
            towns and LOCAL spellings ("Hannover", not the exonym
            "Hanover") — with the Natural Earth path below as fallback.
            """
            near_point = (
                (near_lat, near_lon)
                if near_lat is not None and near_lon is not None else None
            )
            if self.geonames.available:
                home_iso = ""
                if near_point is not None and self.countries.available:
                    at = self.countries.country_at(near_point[0], near_point[1])
                    home_iso = at.iso if at else ""
                hits = self.geonames.search(
                    q, limit=limit, near=near_point, home_iso=home_iso)
                # GeoNames keys countries by ISO code; the response keeps the
                # display-name contract of the Natural Earth path.
                names = self._country_display_names()
                for hit in hits:
                    hit["country"] = names.get(hit["country"], hit["country"])
                return hits
            if not self.cities.available:
                if self.mode == "offline":
                    return []
                # First call after a fresh checkout: fetch the index once,
                # exactly as /geo/cities does — autocomplete must not stay
                # silently empty just because nobody opened the globe first.
                await self.cities.ensure(await self._live_client())
                self.cities._collection = None
            home = ""
            if near_point is not None and self.countries.available:
                at = self.countries.country_at(near_point[0], near_point[1])
                home = at.name if at else ""
            return self.cities.search(q, limit=limit, near=near_point, home_country=home)

        @app.get(f"{prefix}/geo/countries/search", response_model=list[CountryHit])
        async def search_countries(
            q: str, limit: int = 5, lang: str = DEFAULT_LANGUAGE
        ) -> list[CountryHit]:
            """Name lookup that works offline — typing a country frames it.

            Matches any of the 24 languages the boundary data carries, and
            labels the answer in `lang`.
            """
            if not self.countries.available:
                return []
            return self.countries.search(q, limit=limit, lang=lang)

        @app.get(f"{prefix}/vector/coverage", response_model=VectorCoverage)
        async def vector_coverage(
            west: float, south: float, east: float, north: float
        ) -> VectorCoverage:
            """Best vector detail available for an area.

            The nationwide extract stops at z10, so a view deeper than that
            outside an already-minted region would render empty. The client
            asks here first and mints the region when it comes up short.
            """
            area = BBoxDeg(west=west, south=south, east=east, north=north)
            bundles = self._vector_bundles()
            best, best_id = 0, ""
            for name, bundle in bundles.items():
                if bundle.bounds().intersects(area) and bundle.max_zoom() > best:
                    best, best_id = bundle.max_zoom(), name

            # Online, the planet archive backs every tile anywhere, so detail
            # is guaranteed to its own max zoom and there is nothing to mint.
            # Offline, only what is genuinely on disk counts — which is the
            # whole point of the distinction.
            remote = await self._remote_planet()
            if remote is not None:
                planet = remote.max_zoom
                return VectorCoverage(
                    max_zoom=max(best, planet),
                    guaranteed_zoom=max(self._guaranteed_zoom(area, best, bundles), planet),
                    bundle_id=best_id or "planet",
                )
            guaranteed = self._guaranteed_zoom(area, best, bundles)
            return VectorCoverage(
                max_zoom=max(best, guaranteed),
                guaranteed_zoom=guaranteed,
                bundle_id=best_id or ("cache" if guaranteed else ""),
            )

        @app.post(f"{prefix}/vector/extract", response_model=VectorBundleInfo)
        async def extract_vector(request: VectorExtractRequest) -> VectorBundleInfo:
            """Mint a regional street/label extract from the planet build."""
            self._require_provisioning_allowed("vector extraction")
            try:
                path = await self.extractor.extract(
                    request.id,
                    west=request.west, south=request.south,
                    east=request.east, north=request.north,
                    maxzoom=request.maxzoom,
                )
            except ExtractError as error:
                raise HTTPException(status_code=502, detail=str(error))
            self.refresh_vector_bundle(path)
            bundle = self._vector[path.stem]
            return VectorBundleInfo(
                id=path.stem,
                min_zoom=bundle.min_zoom(),
                max_zoom=bundle.max_zoom(),
                bounds=bundle.bounds(),
                size_bytes=path.stat().st_size,
            )

        @app.get(prefix + "/vector/auto/tiles/{z}/{x}/{y}.mvt")
        async def vector_tile(z: int, x: int, y: int) -> Response:
            """Serve from the most detailed extract intersecting this tile.

            Intersection, not center-containment: low-zoom tiles are far
            larger than any regional extract, yet the extract still carries
            its clipped share of them.
            """
            coord = TileCoord(z=z, x=x, y=y)
            west, south, east, north = coord.bbox_deg()
            candidates = []
            for bundle in self._vector_bundles().values():
                if z > bundle.max_zoom():
                    continue
                bounds = bundle.bounds()
                if bounds.west > east or bounds.east < west or bounds.south > north or bounds.north < south:
                    continue
                candidates.append(bundle)
            # Most detailed extract first; fall through until one has the tile.
            for bundle in sorted(candidates, key=lambda b: -b.max_zoom()):
                data = bundle.get_tile(z, x, y)
                if data is not None:
                    return Response(content=data, media_type="application/x-protobuf")

            # Nothing local. Previously this 404'd, the client noticed the gap
            # and minted a whole 25 MB region to see four tiles. Now the tile
            # itself is read out of the remote planet archive over a range
            # request and cached, so browsing costs what is on screen.
            cached = self.vector_cache.get(z, x, y)
            if cached is not None:
                if not cached:
                    raise HTTPException(status_code=404, detail="no vector tile")
                return Response(content=cached, media_type="application/x-protobuf")

            remote = await self._remote_planet()
            if remote is None:
                raise HTTPException(status_code=404, detail="no vector tile")
            try:
                data = await remote.get_tile(z, x, y)
            except (RemotePMTilesError, httpx.HTTPError) as error:
                raise HTTPException(status_code=502, detail=str(error))
            # Cache the miss too: empty ocean tiles are a real answer and
            # re-asking the planet for them on every pan is pure latency.
            self.vector_cache.put(z, x, y, data or b"")
            if not data:
                raise HTTPException(status_code=404, detail="no vector tile")
            return Response(content=data, media_type="application/x-protobuf")

        @app.get(f"{prefix}/vector/streets", response_model=StreetSurvey)
        async def vector_streets(
            west: float, south: float, east: float, north: float,
            zoom: int = 15, name: str = "", limit: int = 200,
        ) -> StreetSurvey:
            """Where the streets in this box actually RUN.

            The geocoder answers with one point per street and the router only
            traces the streets a trip happens to use — neither tells you where
            a kerb runs. Fitting a plot outline to its block, checking a
            frontage, measuring a setback: all of that needs the line itself,
            and the only place it is written down is the vector tile we already
            serve. So this reads the tile and hands back the geometry.

            `name` filters case-insensitively on the label (substring), which
            is how a caller asks for "the two streets my plot fronts onto"
            instead of every service road in the block.
            """
            area = BBoxDeg(west=west, south=south, east=east, north=north)
            zoom = max(1, min(int(zoom), MAX_VECTOR_ZOOM))
            needle = name.strip().lower()

            x0, y0 = _tile_xy(area.west, area.north, zoom)
            x1, y1 = _tile_xy(area.east, area.south, zoom)
            tiles = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
            # A box this reader is meant for is a block or two. Refusing a
            # continent-sized request beats quietly decoding ten thousand
            # tiles and timing out.
            if len(tiles) > 16:
                raise HTTPException(status_code=400,
                                    detail=f"box spans {len(tiles)} tiles at z{zoom}; "
                                           "ask for a smaller area or a lower zoom")

            def _len_m(points: list[tuple[float, float]]) -> float:
                total = 0.0
                for (alon, alat), (blon, blat) in zip(points, points[1:]):
                    mean = math.radians((alat + blat) / 2)
                    total += math.hypot((blon - alon) * math.cos(mean), blat - alat) * 111320
                return total

            def _inside(point: tuple[float, float]) -> bool:
                return (area.west <= point[0] <= area.east
                        and area.south <= point[1] <= area.north)

            ways: list[StreetWay] = []
            for tile_x, tile_y in tiles:
                data = await self._vector_tile_bytes(zoom, tile_x, tile_y)
                if not data:
                    continue
                for feature in read_tile(data, zoom, tile_x, tile_y, layers={"roads"}):
                    if feature["type"] != 2:            # lines only
                        continue
                    props = feature["props"]
                    label = str(props.get("name") or "")
                    if needle and needle not in label.lower():
                        continue
                    for part in feature["parts"]:
                        # Clip by keeping runs of in-box vertices plus the one
                        # step either side, so a segment that merely passes
                        # through still meets the box edge instead of stopping
                        # at the last vertex that happened to fall inside.
                        keep = [i for i, point in enumerate(part) if _inside(point)]
                        if not keep:
                            continue
                        lo, hi = max(0, keep[0] - 1), min(len(part), keep[-1] + 2)
                        piece = part[lo:hi]
                        if len(piece) < 2:
                            continue
                        ways.append(StreetWay(
                            name=label, kind=str(props.get("kind") or props.get("pmap:kind") or ""),
                            geometry=[LatLon(lat=lat, lon=lon) for lon, lat in piece],
                            length_m=round(_len_m(piece), 1)))

            ways.sort(key=lambda w: -w.length_m)
            del ways[limit:]
            return StreetSurvey(
                bbox=area, zoom=zoom, ways=ways,
                names=sorted({w.name for w in ways if w.name}))

        @app.get(f"{prefix}/vector/features")
        async def vector_features(
            west: float, south: float, east: float, north: float,
            layers: str = "buildings,roads,pois",
            zoom: int | None = None,
            limit: int = FEATURE_DEFAULT_LIMIT,
        ) -> Response:
            """Everything the basemap knows inside a box, selectively.

            The vector tiles carry far more than streets: `buildings` (with
            real `height`/`min_height` metres on most features), `pois`
            (name + kind), `water`, `landuse`, `places`, `roads` — and this
            endpoint hands any of it back as a GeoJSON FeatureCollection with
            ALL properties intact plus a `meta` block (see
            `models/feature_extract_meta.py`).

            Tile resolution reuses the exact ladder the tile route serves
            from (`_vector_tile_bytes`: local extract → cache → remote
            planet), so offline this degrades to whatever is bundled/cached —
            missing tiles are simply absent from the answer, mirroring
            `/vector/auto`'s own offline behaviour, never a 403.

            Geometry comes back tile-clipped (the tiler's own clipping, with
            its small buffer); features spanning a tile border are de-duped
            by their tile feature id where one exists, keeping the first
            occurrence's geometry.
            """
            area = BBoxDeg(west=west, south=south, east=east, north=north)
            requested_layers = {name.strip() for name in layers.split(",") if name.strip()}
            if not requested_layers:
                raise HTTPException(status_code=400, detail="no layers requested")
            limit = max(1, min(int(limit), FEATURE_MAX_LIMIT))

            def _covering(z: int) -> list[tuple[int, int]]:
                x0, y0 = _tile_xy(area.west, area.north, z)
                x1, y1 = _tile_xy(area.east, area.south, z)
                return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]

            if zoom is not None:
                zoom = max(1, min(int(zoom), MAX_VECTOR_ZOOM))
                tiles = _covering(zoom)
                if len(tiles) > FEATURE_TILES_CAP:
                    raise HTTPException(
                        status_code=400,
                        detail=f"box spans {len(tiles)} tiles at z{zoom} "
                               f"(cap {FEATURE_TILES_CAP}); shrink the box or "
                               "lower the zoom (buildings and POIs need z14+)")
            else:
                # Deepest auto zoom that fits the budget. Never drops below
                # z14 on its own: below that the requested detail layers are
                # simply absent from the tiles, and answering "no buildings
                # here" for downtown Stuttgart would be a lie.
                zoom, tiles = FEATURE_AUTO_ZOOMS[0], _covering(FEATURE_AUTO_ZOOMS[0])
                for candidate in FEATURE_AUTO_ZOOMS:
                    zoom, tiles = candidate, _covering(candidate)
                    if len(tiles) <= FEATURE_TILES_CAP:
                        break
                if len(tiles) > FEATURE_TILES_CAP:
                    raise HTTPException(
                        status_code=400,
                        detail=f"box spans {len(tiles)} tiles even at "
                               f"z{FEATURE_AUTO_ZOOMS[-1]} (cap {FEATURE_TILES_CAP}); "
                               "shrink the box, or pass an explicit lower `zoom` "
                               "if coarse layers (water/landuse/places) are enough")

            features: list[dict] = []
            counts: dict[str, int] = {}
            seen: set[tuple[str, int]] = set()
            tiles_with_data = 0
            truncated = False
            for tile_x, tile_y in tiles:
                data = await self._vector_tile_bytes(zoom, tile_x, tile_y)
                if not data:
                    continue
                tiles_with_data += 1
                for feature in read_tile(data, zoom, tile_x, tile_y, layers=requested_layers):
                    feature_id = feature["id"]
                    if feature_id is not None:
                        key = (feature["layer"], feature_id)
                        if key in seen:
                            continue
                        seen.add(key)
                    geometry = _geojson_geometry(feature)
                    if geometry is None:
                        continue
                    if len(features) >= limit:
                        truncated = True
                        break
                    properties = dict(feature["props"])
                    # Which source layer a feature came from is the one fact
                    # GeoJSON has no standard slot for; ride it in properties
                    # (Protomaps layers never carry a "layer" attribute).
                    properties.setdefault("layer", feature["layer"])
                    geo: dict = {"type": "Feature", "geometry": geometry,
                                 "properties": properties}
                    if feature_id is not None:
                        geo["id"] = feature_id
                    features.append(geo)
                    counts[feature["layer"]] = counts.get(feature["layer"], 0) + 1
                if truncated:
                    break
            meta = FeatureExtractMeta(
                zoom=zoom, tiles=len(tiles), tiles_with_data=tiles_with_data,
                layer_counts=counts, limit=limit, truncated=truncated,
            )
            return Response(
                content=json.dumps({
                    "type": "FeatureCollection",
                    "features": features,
                    "meta": meta.model_dump(),
                }),
                media_type="application/geo+json",
            )

        @app.post(f"{prefix}/siteplan", response_model=SitePlanSvg)
        async def siteplan(plan: SitePlan) -> "SitePlanSvg":
            """Render a georeferenced site plan as SVG.

            Same projection as the map, so a plan and a map view of the same
            site overlay exactly. Geometry in, SVG out — the look lives in
            `/maps/siteplan.css`, which is why this returns classes and not
            colours: one stylesheet for every plan the platform draws.
            """
            return SitePlanSvg(svg=render_site_plan(plan))

        @app.get(f"{prefix}/render/payload/{{token}}", response_model=MapPayload)
        async def render_payload(token: str) -> MapPayload:
            """One-shot-ish fetch target for `web/render.html`'s own
            `data-agentic-spec-url` — never called by hand."""
            entry = self._render_payloads.get(token)
            if entry is None:
                raise HTTPException(status_code=404, detail="render token unknown or expired")
            return entry[1]

        @app.post(f"{prefix}/render")
        async def render_map(request: RenderRequest) -> Response:
            """Server-side screenshot of the live map runtime (docs/rendering.md).

            Pixel parity with the interactive map, not a second renderer:
            this mounts the SAME `web/map.js` inside a real headless-Chromium
            page (`agentic_maps/render/service.py`) and screenshots it.

            Deliberately NOT gated by `_require_network_allowed` /
            `_require_provisioning_allowed`: rendering makes no network call
            of its own — the headless page it drives fetches
            tiles/vectors/glyphs through the exact same endpoints (`/bundles`,
            `/live/...`, `/vector/auto/...`, `/assets/...`) a live browsing
            session would, and each of THOSE already enforces its own mode
            policy. A render in offline mode therefore sees exactly what a
            live visitor sees: cached/bundled tiles where they exist, gaps
            where they do not — never a hard failure just because the server
            is offline.
            """
            from ..render.service import RenderService, RenderUnavailable

            spec = request.resolved_spec()
            payload = self.build_payload(spec, default_view=request.default_view)
            token = self._store_render_payload(payload)
            base_url = self.render_base_url or os.environ.get(
                RENDER_BASE_URL_ENV, DEFAULT_RENDER_BASE_URL
            )
            # Ops escape hatch: pin a specific Chromium build instead of
            # whatever `playwright install` most recently set up (e.g. a
            # deployment that manages its own browser binaries). Unset by
            # default — production should let Playwright resolve its own.
            chromium_path = os.environ.get(RENDER_CHROMIUM_PATH_ENV, "").strip() or None
            service = RenderService(base_url=base_url, chromium_executable_path=chromium_path)
            try:
                image_bytes = await service.render(
                    token=token, width=request.width, height=request.height,
                    scale=request.scale, format=request.format, quality=request.quality,
                )
            except RenderUnavailable as error:
                raise HTTPException(status_code=501, detail=str(error))
            except (RuntimeError, ValueError) as error:
                raise HTTPException(status_code=502, detail=f"render failed: {error}")
            finally:
                self._render_payloads.pop(token, None)
            media_type = "image/png" if request.format == "png" else "image/jpeg"
            return Response(content=image_bytes, media_type=media_type)

        @app.get(prefix + "/assets/glyphs/{fontstack}/{range_name}")
        async def glyphs(fontstack: str, range_name: str) -> Response:
            # Same traversal guard the sprites route below has: either
            # segment could otherwise walk out of the glyph cache with "..".
            if ".." in fontstack or ".." in range_name:
                raise HTTPException(status_code=400, detail="invalid glyph path")
            cached = self.assets_dir / "glyphs" / fontstack / range_name
            if not cached.exists():
                self._require_network_allowed("glyph fetching")
                url = f"{_GLYPHS_UPSTREAM}/{urllib.parse.quote(fontstack)}/{range_name}"
                client = await self._live_client()
                upstream = await client.get(url, timeout=30.0)
                if upstream.status_code != 200:
                    raise HTTPException(status_code=404, detail="glyph range not found")
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(upstream.content)
            return Response(content=cached.read_bytes(), media_type="application/x-protobuf")

        @app.post(f"{prefix}/geocode", response_model=list[GeocodeResult])
        async def geocode(request: GeocodeRequest) -> list[GeocodeResult]:
            """Authoring-time geocoding via Nominatim/OSM (light interactive
            use per its policy, unless `AGENTIC_MAPS_NOMINATIM_URL` points at
            a self-hosted instance — see `_nominatim_base_url` above);
            results become MapLocations in the spec."""
            self._require_network_allowed("geocoding")
            client = await self._live_client()
            params: dict = {
                "q": request.q, "format": "json",
                # Over-fetch so there is something to re-rank: Nominatim's own
                # order is importance-first, which puts Paris, France above the
                # Paris you are looking at.
                "limit": max(request.limit * 3, 10),
                "accept-language": request.lang,
                # Structured components (postcode, road, locality) for the
                # detail card — display_name alone cannot answer "what is the
                # ZIP here".
                "addressdetails": 1,
            }
            if request.viewbox is not None:
                box = request.viewbox
                params["viewbox"] = f"{box.west},{box.north},{box.east},{box.south}"
                # bounded=0: prefer the box, do not restrict to it — searching
                # for somewhere off-screen must still work.
                params["bounded"] = 0
            response = await client.get(
                f"{_nominatim_base_url()}/search",
                params=params,
                headers={"User-Agent": "agentic-maps/0.1 (self-hosted geocoding)"},
                timeout=20.0,
            )
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="geocoder unavailable")
            hits = [
                GeocodeResult(
                    name=item.get("display_name", request.q),
                    lat=float(item["lat"]),
                    lon=float(item["lon"]),
                    kind=item.get("type", ""),
                    address=_address_from_nominatim(item.get("address")),
                )
                for item in response.json()
            ]
            if request.near is not None:
                # Nearest first. Nominatim ranks by importance, which is right
                # for "Paris" and wrong for "Bahnhofstraße" — and the second
                # case is the common one while looking at a map.
                hits.sort(key=lambda hit: _rough_distance_km(request.near, hit))
            return hits[: request.limit]

        @app.get(f"{prefix}/mode", response_model=ModeState)
        async def get_mode() -> ModeState:
            return ModeState(mode=self.mode)

        @app.post(f"{prefix}/mode", response_model=ModeState)
        async def set_mode(state: ModeState) -> ModeState:
            """Switch runtime mode at request time — "offline" blocks EVERY
            upstream connection (tiles, glyphs, sprites, routing, geocoding);
            "mixed" additionally blocks bulk provisioning (harvest,
            vector/extract) while keeping those live per-request; "online"
            allows everything."""
            self.mode = state.mode
            return ModeState(mode=self.mode)

        @app.delete(f"{prefix}/cache", response_model=CacheClearReport)
        async def clear_cache() -> CacheClearReport:
            """Remove all recorded/harvested raster bundles and cached assets.
            Vector extracts (*.pmtiles) are source data, not cache — kept."""
            import shutil

            removed: list[str] = []
            for bundle in self._bundles.values():
                bundle.close()
            self._bundles.clear()
            for path in sorted(self.bundles_dir.glob("*.mbtiles")):
                path.unlink()
                removed.append(path.name)
            packages = self.bundles_dir / "packages"
            if packages.exists():
                shutil.rmtree(packages)
                removed.append("packages/")
            if self.assets_dir.exists():
                shutil.rmtree(self.assets_dir)
                removed.append("assets/")
            return CacheClearReport(removed=removed)

        @app.get(prefix + "/assets/sprites/{sprite_path:path}")
        async def sprites(sprite_path: str) -> Response:
            """Cache-through for the basemap sprite sheet (icons/shields)."""
            if ".." in sprite_path:
                raise HTTPException(status_code=400, detail="invalid sprite path")
            cached = self.assets_dir / "sprites" / sprite_path
            if not cached.exists():
                self._require_network_allowed("sprite fetching")
                client = await self._live_client()
                upstream = await client.get(f"{_SPRITES_UPSTREAM}/{sprite_path}", timeout=30.0)
                if upstream.status_code != 200:
                    raise HTTPException(status_code=404, detail="sprite not found")
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(upstream.content)
            media = "image/png" if sprite_path.endswith(".png") else "application/json"
            return Response(content=cached.read_bytes(), media_type=media)

        @app.post(f"{prefix}/route", response_model=MapRoute)
        async def route(request: RouteRequest) -> MapRoute:
            """Authoring-time only; results are embedded into the spec."""
            self._require_network_allowed("routing")
            try:
                result = await self.router.route(
                    request.start,
                    request.end,
                    route_id=request.route_id,
                    from_location=request.from_location,
                    to_location=request.to_location,
                    mode=request.mode,
                    via=request.via,
                    steps=request.steps,
                    avoid=request.avoid,
                    alternates=request.alternates,
                )
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(status_code=502, detail=f"routing failed: {error}")
            self._annotate_via_places(result)
            return result

        @app.post(f"{prefix}/sessions", response_model=MapViewSession)
        async def create_session(spec: MapSpec) -> MapViewSession:
            """Store a full MapSpec for the map application to open.

            The returned `url` mounts `web/index.html` with `?session=` —
            the FULL normal UI over this spec: cartography, routes with
            alternates, via labels, badges, panels. No mode gating: the
            spec is caller-provided data and the page it opens enforces
            every mode rule through the endpoints it draws from.
            """
            # Validate the source now, not when the browser arrives: a typo'd
            # source_id must fail the tool call, not blank the opened page.
            self._source_or_composite(spec.source_id)
            raw = spec.model_dump_json().encode()
            if len(raw) > SESSION_PAYLOAD_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"session spec is {len(raw)} bytes; the cap is "
                           f"{SESSION_PAYLOAD_MAX_BYTES} (2 MB). Trim routes/"
                           "steps — raster data never belongs in a spec.")
            token = self._store_session(spec)
            return MapViewSession(
                token=token,
                url=f"{self.public_base_url()}/?session={token}",
                expires_in_s=int(SESSION_TTL_S),
            )

        @app.get(f"{prefix}/sessions/{{token}}", response_model=MapPayload)
        async def session_payload(token: str) -> MapPayload:
            """The stored spec, wired exactly like every other payload —
            `web/index.html?session=<token>` fetches this on load, and again
            after every revision bump. Trip-bound sessions (see
            `bind_session_source`) serve the trip's CURRENT state here, plus
            `session_revision` as the page's polling baseline."""
            spec, revision = self._session_entry(token)
            payload = self.build_payload(spec)
            payload.session_revision = revision
            return payload

        @app.get(f"{prefix}/sessions/{{token}}/revision", response_model=SessionRevision)
        async def session_revision(token: str) -> SessionRevision:
            """The cheap change probe the `?session=` page polls (~2.5 s,
            visible tabs only): no payload build, no tile anything — just
            the live source's revision counter, or the constant 1 for a
            frozen session, so a poller of one of those never sees a bump."""
            entry = self._sessions.get(token)
            if entry is None or entry[0] < time.time():
                self._sessions.pop(token, None)
                self._session_sources.pop(token, None)
                raise HTTPException(status_code=404, detail="session unknown or expired")
            source = self._session_sources.get(token)
            if source is not None:
                revision = source[1]()
                if revision is not None:
                    # A polling tab is an open tab: keep its session alive.
                    self._sessions[token] = (time.time() + SESSION_TTL_S, entry[1])
                    return SessionRevision(revision=revision)
            return SessionRevision(revision=1)

        @app.get(f"{prefix}/sessions/{{token}}/page")
        async def session_page(token: str, basemap: str = "auto") -> Response:
            """One self-contained HTML document of the session (export/map_page.py).

            `basemap=auto` references THIS server's tile endpoints for a real
            basemap and degrades to the embedded rendering when they cannot
            load; `basemap=embedded` emits zero network references. Either
            way the attribution footer is baked into the document — there is
            no way to get the HTML without the credits.
            """
            from ..export.map_page import MAP_PAGE_MAX_BYTES, build_map_page

            spec = self._session_spec(token)
            html = build_map_page(
                spec,
                attribution=self._data_attribution(spec),
                base_url=self.public_base_url() if basemap == "auto" else None,
            )
            if len(html.encode()) > MAP_PAGE_MAX_BYTES:
                raise HTTPException(
                    status_code=500,
                    detail=f"map page exceeds {MAP_PAGE_MAX_BYTES} bytes even "
                           "after simplification — fewer routes or steps needed")
            return Response(content=html, media_type="text/html; charset=utf-8")

        @app.post(f"{prefix}/isochrone")
        async def isochrone(request: IsochroneRequest) -> Response:
            """Reachable-area contours around one point (authoring-time only).

            Returns actual GeoJSON, not a wrapped model: MapLibre wants a
            source it can add directly (`map.addSource(id, {type: 'geojson',
            data: <this>})`), the same reasoning the `/geo/*` endpoints below
            already follow. 501 on a backend that cannot do this (OSRM) —
            `/routing/capabilities` reports `isochrone: false` for that
            backend so the frontend gates the feature before ever calling
            here.
            """
            self._require_network_allowed("routing")
            contours: list[dict] = [
                {"time_min": c.time_min} if c.time_min is not None else {"distance_km": c.distance_km}
                for c in request.contours
            ]
            try:
                result = await self.router.isochrone(
                    request.center, mode=request.mode, contours=contours,
                )
            except NotImplementedError as error:
                raise HTTPException(status_code=501, detail=str(error))
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(status_code=502, detail=f"isochrone failed: {error}")
            return Response(
                content=json.dumps(_isochrone_geojson(result)),
                media_type="application/geo+json",
            )

        @app.post(prefix + "/reverse-geocode", response_model=GeocodeResult | None)
        async def reverse_geocode(request: ReverseGeocodeRequest) -> GeocodeResult | None:
            """Name for a coordinate — what a right-click on the map needs.

            Returns null rather than erroring when nothing is found: an
            unnamed spot in a field is a legitimate answer, and the caller
            falls back to showing the coordinates.
            """
            self._require_network_allowed("reverse geocoding")
            url = f"{_nominatim_base_url()}/reverse"
            params = {
                "lat": request.lat, "lon": request.lon, "format": "jsonv2",
                "accept-language": request.lang, "zoom": 18,
                "addressdetails": 1,
            }
            try:
                client = await self._live_client()
                response = await client.get(
                    url, params=params,
                    headers={"User-Agent": "agentic-maps/0.1 (self-hosted geocoding)"},
                    timeout=15.0,
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError):
                return None
            if not body or "lat" not in body:
                return None
            return GeocodeResult(
                name=body.get("display_name", ""),
                lat=float(body["lat"]),
                lon=float(body["lon"]),
                address=_address_from_nominatim(body.get("address")),
            )

        @app.get(prefix + "/routing/capabilities", response_model=RoutingCapabilities)
        async def routing_capabilities() -> RoutingCapabilities:
            """What the active routing backend can actually do.

            The UI offers "avoid motorways" only where it will be honoured:
            the public OSRM demo rejects exclude flags outright, and silently
            dropping the option would be worse than not offering it. The same
            principle now covers `alternates` and `isochrone`: OSRM never
            returns alternates through this client and has no isochrone
            service at all, so both report false rather than letting the
            frontend find out by calling and getting a 501. Departure time is
            never routing-relevant for either backend as configured here —
            neither call carries live traffic — so it can only shift an
            arrival clock, never the path.
            """
            # The avoid probe (`supported_avoid`) is itself a live request to
            # the routing backend, and "offline" means NO upstream connection
            # — not even this one. Skipped rather than 403'd: capabilities is
            # informational chrome the frontend reads in every mode, and with
            # routing itself refused offline there is nothing to avoid. This
            # also keeps an unreachable-backend probe made while offline from
            # being cached as "supports nothing" for the rest of the session.
            avoid = [] if self.mode == "offline" else await self.router.supported_avoid()
            return RoutingCapabilities(
                backend=self.router.base_url,
                avoid=avoid,
                departure_time_affects_route=False,
                turn_by_turn=True,
                multi_stop=True,
                alternates=getattr(self.router, "supports_alternates", False),
                isochrone=getattr(self.router, "supports_isochrone", False),
            )

        @app.get(prefix + "/attribution")
        async def visible_attribution(
            src: str, z: int, west: float, south: float, east: float, north: float
        ) -> dict:
            """Which licences apply to the tiles CURRENTLY on screen.

            A map that credits every source it could ever use is wrong in both
            directions: it names rights holders whose data is not shown (Blue
            Marble at street zoom) and buries the one that is. The federation
            resolves per tile anyway, so the same resolution is exposed here —
            the client asks after every settled move and shows this answer.

            Two rules make the answer exact rather than merely plausible:
            only the member that actually WINS a tile is credited (coverage
            boxes overlap — a Hamburg tile intersects the Schleswig-Holstein
            box without ever being served from it), and a source is only
            credited when the current zoom is inside its range.

            `src` may list several layers (imagery + world basemap), comma
            separated, because the map composites them.
            """
            coords = _tiles_for_bbox(z, west, south, east, north)
            seen: dict[str, TileSource] = {}
            for source_id in [s.strip() for s in src.split(",") if s.strip()]:
                try:
                    source = self._source_or_composite(source_id)
                except HTTPException:
                    continue
                if isinstance(source, CompositeSource):
                    world = self._world_source()
                    for coord in coords:
                        members = self._resolve_members(source, coord)
                        # members[0] is the winner for this tile; the rest are
                        # only fallbacks for border mosaics and are not shown.
                        winner = next(
                            (m for m in members[:1]
                             if m.min_zoom <= z <= m.max_zoom), None)
                        if winner is None:
                            # Unified-aerial fallthrough (see the `/aerial`
                            # handler): when no band owns this tile at this
                            # zoom, the pixels actually on screen are an
                            # upscale of the coarsest covering band — credit
                            # THAT rights holder, not nobody.
                            for fallback_id, _ in self._aerial_fallbacks(
                                    source, world, coord):
                                fallback = self.sources.get(fallback_id)
                                if fallback is not None:
                                    winner = fallback
                                    break
                        if winner is not None:
                            seen.setdefault(winner.id, winner)
                elif source.min_zoom <= z <= source.max_zoom:
                    seen.setdefault(source.id, source)
            items = [
                {"id": m.id, "name": m.name, "attribution": m.attribution,
                 "license_name": m.license_name, "license_url": m.license_url}
                for m in seen.values()
            ]
            return {"zoom": z, "sources": items,
                    "text": " | ".join(dict.fromkeys(i["attribution"] for i in items))}

        @app.get(prefix + "/live/{source_id}/{z}/{x}/{y}")
        async def live_tile(source_id: str, z: int, x: int, y: int) -> Response:
            """Cache-through proxy (authoring only): fetch on miss, record into
            the live bundle so browsing progressively prewarms offline data."""
            self._require_network_allowed("live tile proxy")
            source = self._source_or_composite(source_id)
            if z < source.min_zoom or z > source.max_zoom:
                raise HTTPException(status_code=404, detail="zoom outside source range")
            data = await self._live_source_tile(source_id, TileCoord(z=z, x=x, y=y))
            if data is None:
                # 204, not 404: out-of-coverage is the EXPECTED answer for
                # most of the planet (the federation covers Germany), and
                # MapLibre logs every 404 to the console — panning Paris
                # produced an error storm that buried real problems. A 204
                # is "no tile here" without the noise; the basemap shows
                # through exactly as before.
                return Response(status_code=204)
            return Response(content=data, media_type=_sniff_media_type(data, source.tile_format))

        @app.get(prefix + "/aerial/quality", response_model=AerialQuality)
        async def aerial_quality(
            west: float, south: float, east: float, north: float, zoom: int,
            src: str = "de-dop",
        ) -> AerialQuality:
            """Is the imagery at this view native or upscale mush? (New-York case.)

            The dispatcher below never 204s while ANY band covers the ground
            — deliberately — which means hybrid over New York at z12 shows a
            z8 Blue-Marble upscale rather than a hole. This endpoint tells
            the frontend HOW far past native it is viewing, from the same
            band-coverage configuration the dispatcher routes with: a
            few-ms bbox check (no tile fetch, cached per rounded bbox),
            allowed in every runtime mode because it reads only config.
            The frontend auto-switches the display to cartography at
            `gap >= 3` and back at `gap <= 1` (hysteresis against flapping
            at coverage edges).
            """
            if not (west < east and south < north):
                raise HTTPException(
                    status_code=400,
                    detail="bbox must have west<east and south<north")
            key = (src, round(west, 2), round(south, 2),
                   round(east, 2), round(north, 2), zoom)
            cached = self._aerial_quality_cache.get(key)
            if cached is not None:
                return cached
            box = BBoxDeg(west=west, south=south, east=east, north=north)
            best = self._aerial_best_native_zoom(src, box)   # 404 on unknown src
            answer = AerialQuality(
                best_native_zoom=best, requested_zoom=zoom,
                gap=max(0, zoom - best))
            if len(self._aerial_quality_cache) > 4096:
                self._aerial_quality_cache.clear()
            self._aerial_quality_cache[key] = answer
            return answer

        @app.get(prefix + "/aerial/{source_id}/{z}/{x}/{y}")
        async def aerial_tile(source_id: str, z: int, x: int, y: int) -> Response:
            """ONE aerial source for the whole zoom range (docs/imagery-coverage.md).

            The imagery ladder is banded by scale (blue-marble z0-8, sen2
            z9-12, the 20 cm federation z13+), and serving each band as its
            own MapLibre source is what broke pitched 3D views: the far rows
            of a tilted camera ask for COARSER zooms of the SAME source, and
            a source whose minzoom is 13 has nothing coarse to give — the
            horizon degenerated into an upscaled smear. This endpoint is that
            same ladder behind one URL: MapLibre's native pitched-view pyramid
            LOD (coarser tiles for distant rows, clean quadtree seams) then
            works out of the box.

            Dispatch is by zoom, one band per level, through the exact
            `/live` cache-through internals (shared bundles, shared
            politeness limits — a routing wrapper, not a second pipeline):
              z0..8   -> the world layer (blue-marble-plus / blue-marble)
              z9..12  -> the ladder's regional band (sen2 inside `de-dop`)
              z13+    -> the 20 cm state federation
            Where the owning band has no imagery (outside sen2's coverage
            box, abroad at street zoom, a state data gap), the dispatcher
            falls through to the coarsest band that DOES cover the tile and
            serves a parent-crop upscale of its deepest level — deliberately
            never a 204 while any band covers the ground, because a hole in
            a single-source pyramid renders as a blank square, not as the
            basemap showing through. Only a tile no band covers at all
            answers 204. The response says what happened:
            `X-Agentic-Maps-Aerial-Band` names the band that served, and
            `X-Agentic-Maps-Aerial-Synth: parent-z<n>` marks an upscale.
            """
            self._require_network_allowed("live tile proxy")
            source = self._source_or_composite(source_id)
            world = self._world_source()
            top_zoom = max(source.max_zoom, world.max_zoom if world else 0)
            if z < 0 or z > top_zoom:
                raise HTTPException(status_code=404, detail="zoom outside aerial range")
            coord = TileCoord(z=z, x=x, y=y)

            band: str | None = None
            data: bytes | None = None
            if world is not None and z <= world.max_zoom:
                band = world.id
                data = await self._live_source_tile(world.id, coord)
            elif source.min_zoom <= z <= source.max_zoom:
                band = source.id
                data = await self._live_source_tile(source_id, coord)
            if data is not None:
                return Response(
                    content=data,
                    media_type=_sniff_media_type(data, source.tile_format),
                    headers={"X-Agentic-Maps-Aerial-Band": band or source.id},
                )

            for ancestor_id, ancestor_zoom in self._aerial_fallbacks(source, world, coord):
                synth = await self._upscaled_tile(ancestor_id, coord, ancestor_zoom)
                if synth is not None:
                    return Response(
                        content=synth,
                        media_type="image/jpeg",
                        headers={
                            "X-Agentic-Maps-Aerial-Band": ancestor_id,
                            "X-Agentic-Maps-Aerial-Synth": f"parent-z{ancestor_zoom}",
                        },
                    )
            return Response(status_code=204)


def _tiles_for_bbox(z: int, west: float, south: float, east: float,
                    north: float, cap: int = 24) -> list[TileCoord]:
    """Tile coordinates covering a viewport, capped.

    Attribution needs the SET of contributing sources, not every tile — a
    handful of samples across the box names the same set as hundreds would,
    and keeps the answer cheap enough to ask on every move.
    """
    import math

    def xy(lon: float, lat: float) -> tuple[int, int]:
        lat = max(-85.05112878, min(85.05112878, lat))
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        rad = math.radians(lat)
        y = int((1.0 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2.0 * n)
        return max(0, min(n - 1, x)), max(0, min(n - 1, y))

    x0, y0 = xy(west, north)
    x1, y1 = xy(east, south)
    xs = sorted({x0, x1, (x0 + x1) // 2})
    ys = sorted({y0, y1, (y0 + y1) // 2})
    out = [TileCoord(z=z, x=x, y=y) for x in xs for y in ys]
    return out[:cap]
