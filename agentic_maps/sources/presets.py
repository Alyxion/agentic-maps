"""Licensed tile source presets.

Every entry here must be legal to bulk-download. German state surveys publish
their 20 cm orthophotos (Bremen: 10 cm) as open data — attribution-only or
better — so the `de-dop` composite federates them into one nationwide imagery
source. Each preset declares its `coverage` bbox; the composite routes every
tile to the covering state, smallest bbox first, and falls through to the next
candidate when a WMS answers with a blank out-of-area tile (state borders are
not rectangles). BKG's Germany-wide DOP WMS is open data too but GetMap
requires free registration — the personalized URL goes into
AGENTIC_MAPS_BKG_URL.

Verified 2026-07-25 with real EPSG:3857 GetMap requests against every endpoint
below (keyless, no referrer): BW, BY, BE, NW, HE, NI, RP, SH, MV, BB, ST, SN,
TH, HB, SL — and, since 2026-07-27, HH: the LGV moved its DOP behind
wms_dop_zeitreihe_unbelaubt (GetMap-verified at z13/z17/z19, TIME 2001-2025,
newest served by default). All 16 states answer.

License names are taken from each service's own AccessConstraints where it
names one; services that declare "no restrictions" are mapped to the matching
dl-de variant. We credit every state we actually pull bytes from either way
(see `spec_attribution`), which stays on the safe side of all of these.

Coverage bboxes are the EX_GeographicBoundingBox each service publishes in its
GetCapabilities, so they track the real data extent rather than a guess.
"""

import os

from ..models.bbox_deg import BBoxDeg
from ..models.composite_source import CompositeSource
from ..models.tile_source import TileSource

_DL_DE_BY_2 = "Datenlizenz Deutschland – Namensnennung – 2.0"
_DL_DE_BY_2_URL = "https://www.govdata.de/dl-de/by-2-0"
_DL_DE_ZERO_2 = "Datenlizenz Deutschland – Zero – 2.0"
_DL_DE_ZERO_2_URL = "https://www.govdata.de/dl-de/zero-2-0"
_CC_BY_4 = "Creative Commons Attribution 4.0"
_CC_BY_4_URL = "https://creativecommons.org/licenses/by/4.0/deed.de"

# 20 cm state imagery only takes over at city scale. Below that the federation
# would show its seams: each state flies its own campaign in its own year, so a
# country-wide view of 15 mosaics is a patchwork of greens. Sentinel-2 covers
# that band as one consistent image instead (see `sen2-europe`).
_DOP_MIN_ZOOM = 13
# 20 cm/px ≈ z19 at German latitudes.
_DOP_MAX_ZOOM = 19


def _dop(
    source_id: str,
    name: str,
    url: str,
    layer: str,
    *,
    attribution: str,
    license_name: str,
    license_url: str,
    coverage: tuple[float, float, float, float],
    white_is_nodata: bool = False,
) -> TileSource:
    """One state orthophoto WMS; coverage is (west, south, east, north)."""
    west, south, east, north = coverage
    return TileSource(
        id=source_id,
        name=name,
        kind="wms",
        url=url,
        wms_layers=layer,
        tile_format="jpeg",
        min_zoom=_DOP_MIN_ZOOM,
        max_zoom=_DOP_MAX_ZOOM,
        attribution=attribution,
        license_name=license_name,
        license_url=license_url,
        request_delay_ms=100,
        coverage=BBoxDeg(west=west, south=south, east=east, north=north),
        white_is_nodata=white_is_nodata,
    )


def builtin_sources() -> dict[str, TileSource]:
    sources: dict[str, TileSource] = {}
    for source in (
        _dop(
            "bw-dop20",
            "Baden-Württemberg Orthophotos 20 cm (LGL)",
            "https://owsproxy.lgl-bw.de/owsproxy/ows/WMS_LGL-BW_ATKIS_DOP_20_C",
            "IMAGES_DOP_20_RGB",
            attribution="© LGL Baden-Württemberg, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(7.45, 47.50, 10.55, 49.85),
        ),
        _dop(
            "by-dop20",
            "Bayern Orthophotos 20 cm (Bayerische Vermessungsverwaltung)",
            "https://geoservices.bayern.de/od/wms/dop/v1/dop20",
            "by_dop20c",
            attribution="© Bayerische Vermessungsverwaltung – geodaten.bayern.de, CC BY 4.0",
            license_name=_CC_BY_4,
            license_url=_CC_BY_4_URL,
            coverage=(8.95, 47.25, 13.90, 50.60),
        ),
        _dop(
            "berlin-dop20",
            "Berlin TrueDOP 20 cm (Geoportal Berlin)",
            "https://gdi.berlin.de/services/wms/truedop_2024",
            "truedop_2024",
            attribution="© Geoportal Berlin / TrueDOP 2024, dl-de/zero-2-0",
            license_name=_DL_DE_ZERO_2,
            license_url=_DL_DE_ZERO_2_URL,
            coverage=(13.05, 52.33, 13.80, 52.70),
        ),
        _dop(
            "nrw-dop",
            "Nordrhein-Westfalen Orthophotos (Geobasis NRW)",
            "https://www.wms.nrw.de/geobasis/wms_nw_dop",
            "nw_dop_rgb",
            attribution="© Geobasis NRW, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(5.85, 50.30, 9.50, 52.55),
        ),
        _dop(
            "he-dop20",
            "Hessen Orthophotos 20 cm (HVBG)",
            "https://www.gds-srv.hessen.de/cgi-bin/lika-services/de-viewer/access/ogc-free-images.ows",
            # The scale-aware aggregate: he_dop20_rgb alone renders only from
            # ~z17 and returns a blank tile at every smaller scale.
            "he_dop_rgb",
            attribution="© Hessische Verwaltung für Bodenmanagement und Geoinformation (HVBG)",
            license_name=_DL_DE_ZERO_2,
            license_url=_DL_DE_ZERO_2_URL,
            coverage=(7.69883, 49.3761, 10.2723, 51.667),
        ),
        _dop(
            "hh-dop",
            "Hamburg Orthophotos DOP Zeitreihe unbelaubt (LGV)",
            # The Zeitreihe service carries 2001-2025 behind a TIME dimension
            # and serves the NEWEST year when TIME is omitted — which is what
            # a live map wants. Out-of-area answers are ~667-byte blank JPEGs,
            # well under the blank-tile heuristic, so the federation treats
            # them as "no imagery" without any special casing.
            "https://geodienste.hamburg.de/wms_dop_zeitreihe_unbelaubt",
            "dop_zeitreihe_unbelaubt",
            attribution="© Freie und Hansestadt Hamburg, LGV — dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            # EX_GeographicBoundingBox of the service, i.e. the state proper.
            coverage=(9.5, 53.35, 10.45, 53.78),
        ),
        _dop(
            "ni-dop20",
            "Niedersachsen Orthophotos 20 cm (LGLN)",
            "https://opendata.lgln.niedersachsen.de/doorman/noauth/dop_wms",
            "ni_dop20",
            attribution="© GeoBasis-DE / LGLN, CC BY 4.0",
            license_name=_CC_BY_4,
            license_url=_CC_BY_4_URL,
            coverage=(6.505772, 51.153098, 11.754046, 54.148101),
        ),
        _dop(
            "rp-dop20",
            "Rheinland-Pfalz Orthophotos 20 cm (LVermGeo RP)",
            "https://geo4.service24.rlp.de/wms/rp_dop20.fcgi",
            "rp_dop20",
            attribution="© GeoBasis-DE / LVermGeoRP, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(6.037773, 48.897996, 8.617703, 51.000893),
        ),
        _dop(
            "sh-dop20",
            "Schleswig-Holstein Orthophotos 20 cm (LVermGeo SH)",
            "https://dienste.gdi-sh.de/WMS_SH_DOP20col_OpenGBD",
            "sh_dop20_rgb",
            attribution="© GeoBasis-DE / LVermGeo SH",
            license_name=_DL_DE_ZERO_2,
            license_url=_DL_DE_ZERO_2_URL,
            coverage=(7.823156, 53.298791, 11.415539, 55.154323),
        ),
        _dop(
            "mv-dop20",
            "Mecklenburg-Vorpommern Orthophotos 20 cm (LAiV M-V)",
            "https://www.geodaten-mv.de/dienste/adv_dop",
            "mv_dop",
            attribution="© GeoBasis-DE / M-V, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(10.33942, 53.039183, 14.47699, 54.820921),
        ),
        _dop(
            "bb-dop20",
            "Brandenburg Orthophotos 20 cm (LGB)",
            "https://isk.geobasis-bb.de/mapproxy/dop20c/service/wms",
            "bebb_dop20c",
            attribution="© GeoBasis-DE / LGB, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(11.229126, 51.306310, 14.770193, 53.572620),
        ),
        _dop(
            "st-dop20",
            "Sachsen-Anhalt Orthophotos 20 cm (LVermGeo LSA)",
            "https://www.geodatenportal.sachsen-anhalt.de/wss/service/ST_LVermGeo_DOP_WMS_OpenData/guest",
            "lsa_lvermgeo_dop20_2",
            attribution="© GeoBasis-DE / LVermGeo LSA, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(10.50923, 50.892719, 13.323255, 53.076941),
        ),
        _dop(
            "sn-dop20",
            "Sachsen Orthophotos 20 cm (GeoSN)",
            "https://geodienste.sachsen.de/wms_geosn_dop-rgb/guest",
            "sn_dop_020",
            attribution="© Staatsbetrieb Geobasisinformation und Vermessung Sachsen (GeoSN)",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            coverage=(11.788898, 50.150604, 15.08686, 51.72093),
        ),
        _dop(
            "th-dop20",
            "Thüringen Orthophotos 20 cm (TLBG)",
            "https://www.geoproxy.geoportal-th.de/geoproxy/services/DOP",
            "th_dop",
            attribution="© GDI-Th / TLBG",
            license_name=_DL_DE_ZERO_2,
            license_url=_DL_DE_ZERO_2_URL,
            coverage=(9.85484, 50.154427, 12.711884, 51.66367),
            # Measured 2026-07-25: fills everything outside Thüringen with
            # opaque #FFFFFF even with TRANSPARENT=TRUE.
            white_is_nodata=True,
        ),
        _dop(
            "hb-dop10",
            "Land Bremen Orthophotos 10 cm (LGeo Bremen)",
            "https://geodienste.bremen.de/wms_dop_lb",
            "dop10_2025_HB",
            attribution="© Landesamt GeoInformation Bremen, CC BY 4.0",
            license_name=_CC_BY_4,
            license_url=_CC_BY_4_URL,
            coverage=(8.45982, 53.0062, 9.0139, 53.232),
        ),
        _dop(
            "sl-dop20",
            "Saarland Orthophotos 20 cm (LVGL Saarland)",
            "https://geoportal.saarland.de/freewms/dop2021",
            "sl_dop2021",
            attribution="© LVGL Saarland",
            license_name=_DL_DE_ZERO_2,
            license_url=_DL_DE_ZERO_2_URL,
            coverage=(6.340597, 49.082884, 7.438171, 49.659757),
        ),
    ):
        sources[source.id] = source

    # The regional/national band: one seamless Copernicus mosaic instead of 15
    # state mosaics. It starts only at z8 — above that Blue Marble is still
    # sharp enough, and Sentinel's coverage rectangle would cut a hard-edged
    # bright patch across a continental view. Hands over to the 20 cm
    # orthophotos at z13, where their detail starts to pay.
    sources["sen2-europe"] = TileSource(
        id="sen2-europe",
        name="Sentinel-2 Europa (Copernicus, aufbereitet vom BKG)",
        kind="wms",
        url="https://isk.geobasis-bb.de/mapproxy/dop20c_sentinel/service/wms",
        wms_layers="sentinel_europe",
        tile_format="jpeg",
        min_zoom=8,
        max_zoom=12,
        attribution="© Europäische Union, Copernicus Sentinel-2, verarbeitet vom BKG",
        license_name="Copernicus Sentinel Data — entgeltfrei, Quellenvermerk erforderlich",
        license_url="https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice",
        request_delay_ms=100,
        coverage=BBoxDeg(west=0.678, south=45.477, east=20.463, north=56.718),
    )
    sources["blue-marble"] = TileSource(
        id="blue-marble",
        name="NASA Blue Marble Next Generation (world imagery, 500 m)",
        kind="xyz",
        # NASA GIBS WMTS in GoogleMapsCompatible tiling — path order {z}/{y}/{x}.
        url=(
            "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
            "BlueMarble_NextGeneration/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg"
        ),
        tile_format="jpeg",
        min_zoom=0,
        max_zoom=8,
        attribution="NASA Blue Marble / EOSDIS GIBS",
        license_name="NASA imagery — public domain",
        license_url="https://www.earthdata.nasa.gov/engage/open-data-services-software-policies",
        request_delay_ms=50,
    )
    # NextGeneration's companion layer: same Blue Marble land, but the oceans
    # carry shaded-relief bathymetry — continental shelves, ridges, trenches —
    # instead of flat navy. Layer identifier and tiling verified against the
    # GIBS WMTS GetCapabilities 2026-08-09: `BlueMarble_ShadedRelief_Bathymetry`,
    # GoogleMapsCompatible_Level8 (z0-8), image/jpeg, {z}/{y}/{x} path order.
    sources["blue-marble-bathy"] = TileSource(
        id="blue-marble-bathy",
        name="NASA Blue Marble Shaded Relief & Bathymetry (world imagery, 500 m)",
        kind="xyz",
        url=(
            "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
            "BlueMarble_ShadedRelief_Bathymetry/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg"
        ),
        tile_format="jpeg",
        min_zoom=0,
        max_zoom=8,
        attribution="NASA Blue Marble Shaded Relief & Bathymetry / EOSDIS GIBS",
        license_name="NASA imagery — public domain",
        license_url="https://www.earthdata.nasa.gov/engage/open-data-services-software-policies",
        request_delay_ms=50,
    )
    # The blended world layer the imagery ladder actually serves: land pixels
    # from NextGeneration (the praised look), ocean pixels from the bathymetry
    # layer, separated by the Natural Earth land-polygon mask with a feathered
    # (~1-2 px) coastline — see geo/landmask.py and Harvester._fetch_blend.
    # Cost: a cold MIXED (coastal) tile is two upstream GIBS fetches; tiles
    # the mask resolves as all-land or all-ocean stay a single fetch, and the
    # existing politeness limits (request_delay_ms, LIVE_TILE_CONCURRENCY)
    # apply unchanged. Cached blended in the live bundle, so warm tiles and
    # sealed offline packages cost GIBS nothing. Both inputs are NASA public
    # domain; the combined attribution names EOSDIS GIBS once.
    sources["blue-marble-plus"] = TileSource(
        id="blue-marble-plus",
        name="NASA Blue Marble Next Generation + Bathymetrie-Ozeane (world imagery, 500 m)",
        kind="xyz",
        # The LAND layer's template — also the graceful degradation: a caller
        # that cannot blend serves plain NextGeneration from this URL.
        url=(
            "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
            "BlueMarble_NextGeneration/default/GoogleMapsCompatible_Level8/{z}/{y}/{x}.jpeg"
        ),
        ocean_blend_source_id="blue-marble-bathy",
        tile_format="jpeg",
        min_zoom=0,
        max_zoom=8,
        attribution="NASA Blue Marble Next Generation & Shaded Relief Bathymetry / EOSDIS GIBS",
        license_name="NASA imagery — public domain",
        license_url="https://www.earthdata.nasa.gov/engage/open-data-services-software-policies",
        request_delay_ms=50,
    )
    bkg_url = os.environ.get("AGENTIC_MAPS_BKG_URL", "").strip()
    if bkg_url:
        sources["bkg-dop"] = TileSource(
            id="bkg-dop",
            name="Deutschland Orthophotos 20 cm (BKG, registered)",
            kind="wms",
            url=bkg_url,
            wms_layers="rgb",
            tile_format="jpeg",
            min_zoom=_DOP_MIN_ZOOM,
            max_zoom=_DOP_MAX_ZOOM,
            attribution="© GeoBasis-DE / BKG, dl-de/by-2-0",
            license_name=_DL_DE_BY_2,
            license_url=_DL_DE_BY_2_URL,
            request_delay_ms=100,
        )
    return sources


# Everything the `de-dop` federation pulls from: the 20 cm state mosaics for
# city zooms, plus the Sentinel-2 mosaic that carries the regional band. Which
# one answers a tile is decided by zoom (see MapsApi._resolve_members).
DE_DOP_MEMBER_IDS = [
    "berlin-dop20", "hb-dop10", "sl-dop20", "th-dop20", "sn-dop20", "st-dop20",
    "rp-dop20", "he-dop20", "sh-dop20", "mv-dop20", "bw-dop20", "bb-dop20",
    "nrw-dop", "ni-dop20", "by-dop20", "hh-dop", "sen2-europe",
]


def builtin_composites() -> dict[str, CompositeSource]:
    """Cross-state federations; members are keys of builtin_sources()."""
    return {
        "de-dop": CompositeSource(
            id="de-dop",
            name="Deutschland Orthophotos (state open-data federation)",
            member_ids=list(DE_DOP_MEMBER_IDS),
            min_zoom=8,
        )
    }
