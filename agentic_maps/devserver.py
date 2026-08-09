"""Isolated dev server for agentic-maps (port 8095).

Serves the demo page (web/), the MapsApi, and a built-in demo MapSpec so the
fly-through can be tested standalone. A host application that embeds MapsApi
via `mount(app, *, prefix)` does not use this module — it mounts MapsApi
directly.

`--mode offline` starts in presentation mode: live proxy/routing/
glyph-fetching are disabled and the demo serves exclusively from harvested
bundles + cached assets — run it to prove a published package works without
internet. `--mode mixed` keeps those live per-request but refuses bulk
provisioning (harvest, vector/extract); see `models/runtime_mode.py`.
"""

import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .models.camera_pose import CameraPose
from .models.lat_lon import LatLon
from .models.map_highlight import MapHighlight
from .models.map_location import MapLocation
from .models.map_payload import MapPayload
from .models.map_pin import MapPin
from .models.map_spec import MapSpec
from .models.runtime_mode import RuntimeMode
from .geo.countries import DEFAULT_LANGUAGE, LANGUAGES
from .mcp_server.http import prepare_http_mcp
from .rest.maps_api import MapsApi
from .rest.setup_api import SetupApi

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BUNDLES_DIR = _ROOT / "var" / "bundles"

DEMO_SOURCE_ID = "de-dop"


def empty_spec() -> MapSpec:
    """The map app's starting point: a map of Germany, nothing on it."""
    return MapSpec(
        id="map-app",
        title="",
        source_id=DEMO_SOURCE_ID,
        overview=CameraPose(center=LatLon(lat=51.0, lon=10.3), zoom=5.4),
        locations=[],
        interactive=True,
    )


def demo_spec() -> MapSpec:
    """Cross-state fly-through mimicking the reference exposé slide."""
    return MapSpec(
        id="demo-metzingen",
        title="TOP LAGE — Metzingen",
        source_id=DEMO_SOURCE_ID,
        overview=CameraPose(center=LatLon(lat=50.6, lon=10.4), zoom=5.8),
        locations=[
            MapLocation(
                id="hq",
                name="Firmenzentrale",
                camera=CameraPose(center=LatLon(lat=48.5386, lon=9.2925), zoom=16.6),
                pin=MapPin(label="HQ", diameter_px=110),
                highlights=[
                    MapHighlight(at=LatLon(lat=48.5405, lon=9.2850), label="Outletcity", radius_px=80),
                    MapHighlight(at=LatLon(lat=48.5369, lon=9.2837), label="Bahnhof", kind="ping"),
                    MapHighlight(
                        at=LatLon(lat=48.5386, lon=9.2925), label="5 Min zu Fuß",
                        kind="radius", radius_m=400, color="#7cc4ff", opacity=0.18,
                    ),
                ],
            ),
            MapLocation(
                id="airport",
                name="Flughafen Stuttgart",
                camera=CameraPose(center=LatLon(lat=48.6899, lon=9.2219), zoom=14.2),
                pin=MapPin(label="STR", diameter_px=85, color="#dfe8f2"),
            ),
            MapLocation(
                id="stuttgart",
                name="Stuttgart Zentrum",
                camera=CameraPose(center=LatLon(lat=48.7784, lon=9.1806), zoom=15.2),
                pin=MapPin(label="STUTTGART", diameter_px=100),
                highlights=[
                    MapHighlight(at=LatLon(lat=48.7838, lon=9.1829), label="Hauptbahnhof", radius_px=70),
                ],
            ),
            MapLocation(
                id="munich",
                name="München",
                camera=CameraPose(center=LatLon(lat=48.1374, lon=11.5755), zoom=15.0),
                pin=MapPin(label="MÜNCHEN", diameter_px=100),
                highlights=[
                    MapHighlight(at=LatLon(lat=48.1402, lon=11.5601), label="Hauptbahnhof", radius_px=70),
                ],
            ),
            MapLocation(
                id="berlin",
                name="Berlin",
                camera=CameraPose(center=LatLon(lat=52.5163, lon=13.3777), zoom=15.3),
                pin=MapPin(label="BERLIN", diameter_px=100),
                highlights=[
                    MapHighlight(at=LatLon(lat=52.5186, lon=13.3762), label="Reichstag", radius_px=70),
                ],
            ),
        ],
    )


# The one shared frontend mount. A host application that already serves this
# asset bundle would skip it; the isolated dev server mounts it itself so the
# same page works standalone too.
STAGE_ASSET_PREFIX = "/_stage"

# The gallery. Each entry names the overlay kind, the question it answers, and
# the honest caveat — several of these look more precise than they are, and a
# sample that hides that teaches the wrong lesson.
OVERLAY_SAMPLES = [
    {
        "id": "canvas",
        "title": "Nur die Leinwand",
        "question": "Wie sieht der Untergrund ohne Daten aus?",
        "note": "Neutrale Graustufen, nur Ortsnamen behalten Kontrast. "
                "Alles Weitere liegt darüber.",
        "kind": "none",
        "zoom": 12.5,
    },
    {
        "id": "radial",
        "question": "Was liegt in 500 m, 1 km, 2 km Luftlinie?",
        "title": "Radien um ein Objekt",
        "note": "Luftlinie — ignoriert Flüsse, Bahntrassen und Autobahnen. "
                "Schnell und ehrlich, aber optimistisch.",
        "kind": "radial",
        "zoom": 13.2,
        "options": {"radii": [500, 1000, 2000, 3000]},
    },
    {
        "id": "isodistance",
        "title": "Fahrzeit über das Straßennetz",
        "question": "Was ist in 5, 10, 15 Minuten wirklich erreichbar?",
        "note": "Über die Routing-Matrix gemessen, zwischen den Messpunkten "
                "interpoliert — eine Reichweiten-Skizze, kein Routing-Ergebnis.",
        "kind": "isodistance",
        "zoom": 11.8,
        "options": {"radiusM": 4000, "samples": 5, "mode": "car"},
    },
    {
        "id": "cluster",
        "title": "Dichte vieler Orte",
        "question": "Wo häuft sich etwas?",
        "note": "Kerndichte über gewichtete Punkte. Überlappende Quellen "
                "addieren sich — das ist hier gewollt.",
        "kind": "cluster",
        "zoom": 12.2,
    },
    {
        "id": "multi",
        "title": "Mehrere Zentren, verschiedene Reichweiten",
        "question": "Wo überlagern sich die Einzugsgebiete?",
        "note": "Drei Standorte mit unterschiedlichen Radien. Die Schnittmengen "
                "sind die Aussage, nicht die einzelnen Kreise.",
        "kind": "multi",
        "zoom": 11.5,
    },
    {
        "id": "highlight",
        "title": "Flächen einfärben",
        "question": "Diese Gebiete, nicht jene.",
        "note": "Kategorial, keine Rampe: die Werte sind nicht geordnet, also "
                "darf die Farbe keine Reihenfolge suggerieren.",
        "kind": "highlight",
        "zoom": 12.0,
    },
]


def mount_stage_assets(app: FastAPI) -> bool:
    """Serve the llming-stage vendor bundle — three.js, Vue, Quasar, fonts.

    agentic-maps deliberately vendors nothing that llming-stage already ships:
    a second copy of three.js is a second version to keep in step, and the
    globe and the rest of the product must not drift apart. MapLibre and the
    Protomaps basemap stay local because they are map-specific and not part of
    the shared bundle.

    Optional at import time so the core engine keeps working without it — the
    globe is the only thing that needs it, and it says so out loud.
    """
    try:
        from llming_stage import mount_assets
    except ImportError:
        print("[maps] llming-stage not installed — the 3D globe will not load. "
              "Install the dev-server extra.")
        return False
    mount_assets(app, asset_prefix=STAGE_ASSET_PREFIX)
    return True


def mount_frontend_debug(app: FastAPI) -> bool:
    """Attach the llming-com-backed remote debug surface, if it is switched on.

    Off unless AGENTIC_MAPS_DEBUG=1, and useless without an API key — see
    rest/frontend_debug.py for why both gates exist.
    """
    from .rest.frontend_debug import FrontendDebug, is_enabled

    if not is_enabled():
        # The probe endpoint stays mounted either way: every page asks
        # `GET debug/enabled` once at load to decide whether to open a
        # socket, and a 404 there put two console errors on every single
        # page view — noise that buried real errors. One boolean, no auth.
        @app.get("/api/v1/maps/debug/enabled")
        async def debug_disabled() -> dict:
            return {"enabled": False}
        return False
    FrontendDebug().mount(app)
    try:
        from llming_com import mount_client_static

        mount_client_static(app)
    except ImportError:
        pass
    print("[maps] frontend debug attached at /api/v1/maps/debug (AGENTIC_MAPS_DEBUG=1)")
    return True


def create_app(
    bundles_dir: Path | None = None, *,
    mode: RuntimeMode = "online",
    host: str = "127.0.0.1",
    port: int = 8095,
) -> FastAPI:
    # The dev server is one person on their own machine — the one place
    # where asking for a location is reasonable.
    api = MapsApi(
        bundles_dir or _DEFAULT_BUNDLES_DIR, mode=mode, standalone=True,
        # POST /render (agentic_maps/render/service.py) points Playwright
        # back at THIS server — host/port must match what `main()` actually
        # binds to below, not the MapsApi default (which assumes the
        # isolated dev server's own default port).
        render_base_url=f"http://{host}:{port}",
    )
    # Built BEFORE the FastAPI app: the MCP session manager must run inside
    # the app's lifespan (a mounted sub-app's own lifespan never runs), so
    # the lifespan has to exist at construction time. Feature-detected like
    # mount_stage_assets — None (with a one-line notice) without the extra.
    # Same MapsApi INSTANCE as the REST/UI surface: switching the runtime
    # mode over MCP switches it for the browser session too, deliberately.
    mcp_http = prepare_http_mcp(api)
    app = FastAPI(
        title="agentic-maps dev server",
        lifespan=mcp_http.lifespan if mcp_http else None,
    )
    api.mount(app)
    if mcp_http:
        mcp_http.mount_on(app)
    mount_stage_assets(app)
    mount_frontend_debug(app)
    # The setup wizard's REST surface (web/apps/setup-wizard/) — only makes
    # sense on the isolated single-user dev server, same reasoning
    # `standalone=True` above already documents for geolocation.
    SetupApi().mount(app)

    @app.get("/api/demo-spec", response_model=MapPayload)
    async def get_demo_spec(scenario: bool = False) -> MapPayload:
        """Payload for the map app.

        The landing page is a plain map — search, route, zoom, no scenario
        chrome — so it asks for an empty spec. `?scenario=1` returns the demo
        scenario (stops, pins, highlights, routes) that the dev panel loads.
        """
        spec = demo_spec() if scenario else empty_spec()
        return api.build_payload(spec, lang=DEFAULT_LANGUAGE, stage_asset_prefix=STAGE_ASSET_PREFIX)

    @app.get("/api/overlay-samples")
    async def overlay_samples() -> dict:
        """The sample gallery, as data.

        Kept server-side rather than hard-coded in the page so the viewer and
        any client can step through the same set, and so a new sample is one
        entry rather than a code change in two places.
        """
        # Karlsruhe: a compact city with a river, a motorway ring and a clear
        # centre — everything a reachability overlay needs to be judged on.
        centre = [8.4037, 49.0069]
        return {"centre": centre, "samples": OVERLAY_SAMPLES}

    class NoCacheStatic(StaticFiles):
        """Never cache the demo assets.

        Browsers hold on to map.js/map.css across restarts, which silently
        serves an old runtime against a new API — an hour-eating class of
        phantom bug. Production ships hashed URLs instead.
        """

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            return response

    app.mount("/", NoCacheStatic(directory=_ROOT / "web", html=True), name="web")
    return app


def main() -> None:
    import os
    import uvicorn

    from .models.runtime_mode import RUNTIME_MODES

    # AGENTIC_MAPS_MODE is the container-friendly equivalent of --mode
    # (docker-compose.yml/docker-compose.offline.yml set it rather than
    # overriding the image's own CMD args) — the flag still wins if both are
    # given. An unset or unrecognised value falls back to "online", the
    # historical default.
    env_mode = os.environ.get("AGENTIC_MAPS_MODE", "").strip().lower()
    default_mode: RuntimeMode = env_mode if env_mode in RUNTIME_MODES else "online"

    parser = argparse.ArgumentParser(description="agentic-maps isolated dev server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument(
        "--mode",
        choices=RUNTIME_MODES,
        default=default_mode,
        help="runtime mode: 'offline' (presentation, no internet access at all), "
             "'mixed' (live routing/geocoding/tiles, no bulk data provisioning), "
             "'online' (everything allowed) — default: AGENTIC_MAPS_MODE if set, "
             "else 'online'",
    )
    args = parser.parse_args()
    app = create_app(mode=args.mode, host=args.host, port=args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
