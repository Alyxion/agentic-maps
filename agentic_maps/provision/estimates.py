"""The single source of size truth for region-bulk provisioning.

Every surface that talks about cost — the MCP tool description, the
confirm-before-download step, the setup wizard's "Offline-Daten" step, the
docs table — calls `estimate_region()` (or prints numbers derived from it),
so the promise a user confirms and the bytes that then flow can never drift
apart silently. The tile math is exact Web-Mercator tile counting over the
region bbox; only the bytes-per-tile figure is an average.

Anchors (all measured, not guessed):
- `TILE_AVG_BYTES`: 12 KB/tile, the observed mean across harvested
  state-DOP/sen2/Blue-Marble bundles (JPEG tiles run ~6-60 KB; mixed
  pyramids average out near 12).
- PBF sizes: HTTP HEAD Content-Length of each Geofabrik extract, measured
  2026-08-09. Geofabrik extracts grow week over week — treat as ~±10%.
- Vector extract: the existing nationwide Germany z0-15 extract weighs
  ~168 MB; other regions scale by their z13 tile count (a fair area proxy
  at constant feature density — Europe is somewhat sparser than Germany,
  so this leans conservative/high).
"""

import math

from ..models.bbox_deg import BBoxDeg
from ..models.provision_estimate import ProvisionEstimate
from ..models.provision_layer_estimate import ProvisionLayerEstimate
from ..models.provision_request import ProvisionRequest
from ..models.region_preset import RegionPreset

TILE_AVG_BYTES = 12 * 1024

# The aerial ladder's fixed bands (mirrors sources/presets.py: blue-marble*
# z0-8, sen2-europe z9-12, the 20 cm federation z13+).
WORLD_MAX_ZOOM = 8
SEN2_MAX_ZOOM = 12
# sen2-europe's own coverage box (sources/presets.py) — the z9-12 band of the
# `earth` preset only exists inside it.
SEN2_COVERAGE = BBoxDeg(west=0.678, south=45.477, east=20.463, north=56.718)

# Germany's z0-15 street extract on disk — the anchor every vector estimate
# scales from (decimal MB, matching how `human_bytes` talks).
DE_VECTOR_BYTES = 168_000_000

# Valhalla graph-build honesty, stated wherever routing presets are offered.
ROUTING_BUILD_NOTE_DE = (
    "Valhalla graph build from this PBF: tens of minutes to ~1 hour on "
    "ordinary hardware.")
ROUTING_BUILD_NOTE_EU = (
    "Valhalla graph build for Europe: MANY HOURS of machine time and a "
    "large tile set on disk — plan for an overnight run.")

REGION_PRESETS: dict[str, RegionPreset] = {
    preset.id: preset
    for preset in (
        RegionPreset(
            id="de", name="Germany",
            bbox=BBoxDeg(west=5.866, south=47.270, east=15.042, north=55.058),
            geofabrik_url="https://download.geofabrik.de/europe/germany-latest.osm.pbf",
            pbf_bytes=4_813_728_854,          # HEAD 2026-08-09
            vector_bytes=DE_VECTOR_BYTES,
        ),
        RegionPreset(
            id="dach", name="Germany + Austria + Switzerland",
            bbox=BBoxDeg(west=5.857, south=45.818, east=17.163, north=55.058),
            geofabrik_url="https://download.geofabrik.de/europe/dach-latest.osm.pbf",
            pbf_bytes=6_190_387_782,          # HEAD 2026-08-09
        ),
        RegionPreset(
            id="eu", name="Europe",
            bbox=BBoxDeg(west=-11.0, south=35.0, east=32.0, north=71.0),
            geofabrik_url="https://download.geofabrik.de/europe-latest.osm.pbf",
            pbf_bytes=34_759_577_819,         # HEAD 2026-08-09
        ),
        RegionPreset(
            id="earth", name="Earth base imagery (z0-12 ladder)",
            bbox=BBoxDeg(west=-180.0, south=-85.05, east=180.0, north=85.05),
            layers=["aerial"],
        ),
    )
}


def _mercator_y_fraction(lat: float) -> float:
    """Web-Mercator y as a fraction of world height (0 = north pole edge)."""
    clamped = max(min(lat, 85.05112878), -85.05112878)
    return (1.0 - math.asinh(math.tan(math.radians(clamped))) / math.pi) / 2.0


def tiles_in_bbox(bbox: BBoxDeg, zoom: int) -> int:
    """Exact count of z/x/y tiles a bbox touches — the same span-inclusive
    counting `TileCoord.at` corner-to-corner iteration produces."""
    n = 1 << zoom
    x0 = max(0, min(n - 1, int((bbox.west + 180.0) / 360.0 * n)))
    x1 = max(0, min(n - 1, int((bbox.east + 180.0) / 360.0 * n)))
    y0 = max(0, min(n - 1, int(_mercator_y_fraction(bbox.north) * n)))
    y1 = max(0, min(n - 1, int(_mercator_y_fraction(bbox.south) * n)))
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def aerial_zooms(region: str, aerial_max_zoom: int | None) -> list[int]:
    """Which zoom levels the aerial layer of a request covers.

    `earth` is the fixed z0-12 base ladder (world band + sen2 band); every
    other region is the 20 cm federation band, z13 up to the required cap.
    """
    if region == "earth":
        return list(range(0, SEN2_MAX_ZOOM + 1))
    return list(range(13, (aerial_max_zoom or 13) + 1))


def aerial_tile_count(bbox: BBoxDeg, region: str, aerial_max_zoom: int | None) -> int:
    total = 0
    for zoom in aerial_zooms(region, aerial_max_zoom):
        if region == "earth":
            if zoom <= WORLD_MAX_ZOOM:
                total += (1 << zoom) * (1 << zoom)
            else:
                total += tiles_in_bbox(SEN2_COVERAGE, zoom)
        else:
            total += tiles_in_bbox(bbox, zoom)
    return total


def human_bytes(count: int) -> str:
    """Decimal units, one decimal place — matches how the size table talks."""
    for unit, factor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if count >= factor:
            return f"{count / factor:.1f} {unit}"
    return f"{count} B"


def _resolve_region(request: ProvisionRequest) -> tuple[str, BBoxDeg, RegionPreset | None]:
    if request.bbox is not None:
        return (request.region_id or "custom", request.bbox, None)
    preset = REGION_PRESETS.get(request.region)
    if preset is None:
        raise ValueError(
            f"unknown region {request.region!r}; presets: {sorted(REGION_PRESETS)} "
            "— or pass a custom bbox")
    return (preset.id, preset.bbox, preset)


def estimate_region(request: ProvisionRequest) -> ProvisionEstimate:
    """The full pre-download cost forecast for a provisioning request.

    Raises ValueError for a region/layer combination that is not offered
    (e.g. routing for `earth`) — refusal at estimate time, so a job can
    never start on a promise the engine cannot keep.
    """
    region, bbox, preset = _resolve_region(request)
    layers: list[ProvisionLayerEstimate] = []
    warnings: list[str] = []

    for layer in request.layers:
        if preset is not None and layer not in preset.layers:
            raise ValueError(f"region {region!r} does not offer the {layer!r} layer")
        if layer == "aerial":
            tiles = aerial_tile_count(bbox, region, request.aerial_max_zoom)
            size = tiles * TILE_AVG_BYTES
            zooms = aerial_zooms(region, request.aerial_max_zoom)
            band = f"z{zooms[0]}-{zooms[-1]}" if len(zooms) > 1 else f"z{zooms[0]}"
            layers.append(ProvisionLayerEstimate(
                layer="aerial", tiles=tiles, bytes_estimate=size,
                display=f"{region} aerial {band}: ~{human_bytes(size)} ({tiles:,} tiles)",
                note=f"average {TILE_AVG_BYTES // 1024} KB/tile; resumable — "
                     "already-cached tiles are skipped on a re-run",
            ))
            if region == "eu":
                warnings.append(
                    f"Europe aerial at {band} is ~{human_bytes(size)} across "
                    f"{tiles:,} tiles — WEEKS of polite downloading against "
                    "public state services. Only start this deliberately, on "
                    "a disk that can take it; consider per-country runs or "
                    "corridor harvests instead.")
        elif layer == "maps":
            anchor = REGION_PRESETS["de"]
            ratio = tiles_in_bbox(bbox, 13) / tiles_in_bbox(anchor.bbox, 13)
            size = (preset.vector_bytes if preset and preset.vector_bytes
                    else int(DE_VECTOR_BYTES * ratio))
            layers.append(ProvisionLayerEstimate(
                layer="maps", bytes_estimate=size,
                display=f"{region} vector streets/labels z0-15: ~{human_bytes(size)}",
                note="pmtiles extract from the Protomaps planet build; scaled "
                     "from the measured Germany extract (168 MB)",
            ))
        elif layer == "routing":
            if preset is not None:
                size = preset.pbf_bytes
                note = ROUTING_BUILD_NOTE_EU if region == "eu" else ROUTING_BUILD_NOTE_DE
                display = f"{region} routing PBF (Geofabrik): ~{human_bytes(size)}"
            elif request.pbf_url:
                size = 0
                note = "size of the supplied PBF URL is not probed at estimate time"
                display = f"{region} routing PBF (supplied URL): size unknown"
            else:
                # Custom bbox: the Overpass /api/map path (setup/pbf_fetch.py)
                # covers up to 0.25x0.25 deg; a city-scale export lands in
                # the tens of MB.
                size = 50 * 1024 * 1024
                note = ("cut via the Overpass /api/map bbox export (bboxes up "
                        "to 0.25x0.25 deg); larger areas need a Geofabrik "
                        "`pbf_url`")
                display = f"{region} routing PBF (Overpass cut): ~{human_bytes(size)} or less"
            layers.append(ProvisionLayerEstimate(
                layer="routing", bytes_estimate=size, display=display, note=note))
            if region == "eu":
                warnings.append(
                    "Europe routing: ~35 GB PBF download, then " + ROUTING_BUILD_NOTE_EU)

    total = sum(layer.bytes_estimate for layer in layers)
    return ProvisionEstimate(
        region=region, bbox=bbox, layers=layers,
        total_bytes=total, total_display=f"~{human_bytes(total)}",
        warnings=warnings,
    )


def preset_size_table() -> list[str]:
    """The human preset table (one line per offer) — shared by the MCP tool
    description, the CLI wizard step and docs/mcp.md, so all three quote the
    same numbers from the same math."""
    lines: list[str] = []
    earth = estimate_region(ProvisionRequest(region="earth", layers=["aerial"]))
    lines.append(f"earth aerial z0-12 base ladder: {earth.total_display}")
    for region in ("de", "dach", "eu"):
        maps_est = estimate_region(ProvisionRequest(region=region, layers=["maps"]))
        lines.append(f"{region} vector streets z0-15: {maps_est.total_display}")
        for cap in (13, 14, 15, 16):
            if region != "de" and cap == 16:
                continue  # the table offers z16 for Germany only
            aerial = estimate_region(ProvisionRequest(
                region=region, layers=["aerial"], aerial_max_zoom=cap))
            band = f"z13-{cap}" if cap > 13 else "z13"
            lines.append(f"{region} aerial {band}: {aerial.total_display}")
        routing = estimate_region(ProvisionRequest(region=region, layers=["routing"]))
        lines.append(f"{region} routing PBF: {routing.total_display}")
    return lines
