from pydantic import BaseModel

from .map_spec import MapSpec


class MapPayload(BaseModel):
    """Everything `web/map.js` needs to mount one map: the spec plus tile access.

    This is the exact JSON shape a `[data-agentic-map]` element consumes —
    either fetched from `data-agentic-spec-url` or embedded inline via
    `<script type="application/json" data-agentic-inline-spec>`. Built by
    `MapsApi.build_payload()`, which both the dev server's own demo endpoint
    (`GET /api/demo-spec`) and the render pipeline (`POST /render`) call —
    a render must see the identical tile/vector/glyph wiring a live browsing
    session would, or a screenshot could silently differ from what a person
    actually sees.
    """

    spec: MapSpec
    tiles_url_template: str
    tile_min_zoom: int
    tile_max_zoom: int
    # The unified aerial ladder (`GET /aerial/{src}/{z}/{x}/{y}`): one raster
    # source spanning z0..aerial_max_zoom, dispatched server-side to the
    # world/regional/20cm bands. The LIVE display path — set only in
    # online/mixed mode; sealed/offline pages keep the per-band templates
    # below, which also remain populated here for the harvest/package/seal
    # pipeline.
    aerial_url_template: str | None = None
    aerial_max_zoom: int = 19
    attribution: str
    vector_url_template: str | None = None
    vector_max_zoom: int = 15
    glyphs_url_template: str | None = None
    sprites_base_url: str | None = None
    # World imagery (NASA Blue Marble) shown beneath the state orthophotos in
    # hybrid/satellite — covers the globe where 20 cm imagery ends.
    world_tiles_url_template: str | None = None
    world_max_zoom: int = 8
    basemap_flavor: str = "dark"
    # Basemap flavor used UNDER aerial imagery (hybrid/satellite): bright, so
    # labels/uncovered areas stay readable against imagery.
    hybrid_flavor: str = "light"
    # Central brand color overrides merged over the basemap flavor (any
    # Protomaps flavor key: background, earth, water, park_a, major, ...).
    brand_colors: dict[str, str] | None = None
    default_view: str = "hybrid"
    # Worldwide borders + country names, independent of any regional extract.
    countries_url: str | None = None
    country_labels_url: str | None = None
    # Label language for cities, streets and countries alike.
    lang: str = "de"
    languages: list[str] = []
    offline: bool = False
    # Trip-bound browser sessions only (`GET /sessions/{token}`): the live
    # trip's revision at the moment this payload was built. The `?session=`
    # page keeps it as its polling baseline; None for every other payload.
    session_revision: int | None = None
    # Single-user mode: the frontend may ask for a coarse location to rank
    # search results. Never set when mounted as a module in the host.
    standalone: bool = False
    # Where the shared llming-stage vendor bundle lives. three.js, Vue and
    # Quasar are served from there rather than copied in here.
    stage_asset_prefix: str = "/_stage"
