"""MCP surface tests — the official SDK's in-memory client against the real
tool layer, which itself calls the REST surface in-process (ASGI transport).

Nothing upstream is real: Nominatim rides the same `api._client`
MockTransport pattern `test_authoring_controls.py` uses, OSRM is an
`OsrmRouter` with a MockTransport, tiles come from the hand-encoded MVT
fixture in `test_vector_features.py`, and `RenderService` is the fake from
`test_render_endpoint.py`'s pattern. What IS real end-to-end: schema
generation, gating, error mapping, and the shared-instance mode state.
"""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client.client import Client

import agentic_maps.render.service as render_service
from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.sources.presets import builtin_composites, builtin_sources
from tests.test_vector_features import _LAT, _LON, _ZOOM, _bounds_e7, _tile_payload

_EXPECTED_TOOLS = {
    "geocode", "reverse_geocode", "search_places",
    "route", "route_matrix", "isochrone", "routing_capabilities",
    "optimize_trip", "reachability",
    "extract_features", "street_survey",
    "render_map", "open_map_view", "get_map_page_html",
    "create_trip", "update_trip", "get_trip", "list_trips",
    "list_tile_sources", "map_attribution",
    "get_runtime_mode", "set_runtime_mode",
    "plan_offline_bundle", "harvest_offline_bundle", "package_offline_bundle",
    "list_offline_bundles",
    "provision_offline_region", "provisioning_status", "cancel_provisioning",
}

_NOMINATIM_HIT = [{
    "display_name": "Dettingen an der Erms, Reutlingen", "lat": "48.5254",
    "lon": "9.3466", "type": "village",
    "address": {"village": "Dettingen an der Erms", "postcode": "72581",
                "state": "Baden-Württemberg", "country": "Deutschland",
                "country_code": "de"},
}]


def _osrm_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    coords = url.split("/v1/driving/")[1].split("?")[0]
    n = len(coords.split(";"))
    if "/table/v1/" in url:
        params = request.url.params
        if "sources" in params or "destinations" in params:
            # Asymmetric ask: rows × cols with a per-target gradient, so a
            # test can check each target got ITS answer, not a copy.
            rows = len(params["sources"].split(";")) if "sources" in params else n
            cols = len(params["destinations"].split(";")) if "destinations" in params else n
            return httpx.Response(200, json={
                "code": "Ok",
                "durations": [
                    [600.0 + 60.0 * j for j in range(cols)] for _ in range(rows)],
            })
        return httpx.Response(200, json={
            "code": "Ok",
            "durations": [[0 if i == j else 600.0 for j in range(n)] for i in range(n)],
        })
    if "/trip/v1/" in url:
        # The solver "found" that reversing the middle stops is cheaper:
        # first and last keep their positions, the middle comes back
        # mirrored — waypoints ride in INPUT order, waypoint_index is the
        # position of that input coordinate in the trip.
        positions = list(range(n))
        positions[1:-1] = positions[1:-1][::-1]
        return httpx.Response(200, json={
            "code": "Ok",
            "trips": [{
                "duration": 3000.0, "distance": 180_000.0,
                "geometry": {"coordinates": [[9.35, 48.53], [8.68, 50.11]]},
                "legs": [{"duration": 3000.0 / max(n - 1, 1),
                          "distance": 180_000.0 / max(n - 1, 1), "steps": []}
                         for _ in range(max(n - 1, 1))],
            }],
            "waypoints": [{"waypoint_index": position, "trips_index": 0}
                          for position in positions],
        })
    route = {
        "duration": 3600.0, "distance": 210_000.0,
        "geometry": {"coordinates": [[9.35, 48.53], [8.68, 50.11]]},
        "legs": [{"duration": 3600.0 / (n - 1), "distance": 210_000.0 / (n - 1),
                  "steps": []} for _ in range(n - 1)],
    }
    routes = [route]
    if "alternatives=" in url:
        routes.append({**route, "duration": 3900.0})
    return httpx.Response(200, json={"code": "Ok", "routes": routes})


_REVERSE_HIT = {
    "display_name": "Teststraße, Teststadt", "lat": "48.5254", "lon": "9.3466",
    "address": {"road": "Teststraße", "postcode": "72581", "town": "Teststadt",
                "country": "Deutschland", "country_code": "de"},
}


def _nominatim_handler(request: httpx.Request) -> httpx.Response:
    if "/reverse" in str(request.url):
        return httpx.Response(200, json=_REVERSE_HIT)
    return httpx.Response(200, json=_NOMINATIM_HIT)


@pytest.fixture
def api(tmp_path, pmtiles_writer, monkeypatch):
    import agentic_maps.mcp_server.server as mcp_server_module

    # Address enrichment spaces its geocoder calls for public-Nominatim
    # politeness — pointless against the in-process mock, so zeroed here.
    monkeypatch.setattr(mcp_server_module, "ENRICH_SPACING_S", 0)
    home = TileCoord.at(_LAT, _LON, _ZOOM)
    pmtiles_writer(
        tmp_path / "streets-fixture.pmtiles",
        [(home.z, home.x, home.y)], _bounds_e7(home), _tile_payload(),
    )
    api = MapsApi(
        tmp_path,
        sources=builtin_sources(), composites=builtin_composites(),
        router=OsrmRouter("https://osrm.test", transport=httpx.MockTransport(_osrm_handler)),
        remote_planet=False,
    )
    # Every tool that enriches stop addresses reverse-geocodes through the
    # live client — never let a unit test reach the real Nominatim.
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(_nominatim_handler))
    return api


@pytest.fixture
def mcp_client(api):
    from agentic_maps.mcp_server.server import build_mcp_server

    return Client(build_mcp_server(api))


def _payload(result) -> dict:
    """Structured content when present, else the JSON text content."""
    assert not result.is_error, [c.text for c in result.content]
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    assert result.is_error
    return " ".join(c.text for c in result.content)


async def test_tool_roster_and_caps_in_descriptions(mcp_client):
    async with mcp_client as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == _EXPECTED_TOOLS
    # Every cap an agent must know is stated in the description it reads.
    assert "100" in tools["route"].description            # stop cap
    assert "2" in tools["route"].description              # min stops
    assert "100" in tools["route_matrix"].description     # point cap
    assert "4" in tools["isochrone"].description          # contour cap
    assert "1600" in tools["render_map"].description      # width cap
    assert "1200" in tools["render_map"].description      # height cap
    assert "64" in tools["extract_features"].description  # tile cap
    assert "Valhalla" in tools["isochrone"].description   # backend dependency
    assert "REQUIRED" in tools["map_attribution"].description
    for name in ("harvest_offline_bundle",):
        assert "mixed" in tools[name].description         # provisioning gating
    for name in ("geocode", "route", "route_matrix"):
        assert "offline" in tools[name].description       # network gating
    # The display trio steers away from third-party tile rebuilds.
    assert "PRIORITY-ordered" in tools["route"].description
    assert "open_map_view" in tools["route"].description
    assert "create_trip" in tools["route"].description
    assert "browser" in tools["open_map_view"].description
    assert "attribution" in tools["get_map_page_html"].description
    assert "1536" in tools["get_map_page_html"].description      # page size cap KB
    for name in ("create_trip", "update_trip", "get_trip", "list_trips"):
        assert "24" in tools[name].description            # trip count cap
        assert "16" in tools[name].description            # trip byte cap (MB)


async def test_geocode_returns_structured_address(mcp_client, api):
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps(_NOMINATIM_HIT).encode())))
    async with mcp_client as client:
        result = await client.call_tool(
            "geocode", {"query": "Dettingen an der Erms", "limit": 3})
    hit = _payload(result)["results"][0]
    assert hit["address"]["postcode"] == "72581"
    assert hit["address"]["locality"] == "Dettingen an der Erms"


async def test_route_with_three_stops_and_legs(mcp_client):
    stops = [
        {"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"},
        {"lat": 48.7784, "lon": 9.1806, "label": "Stuttgart"},
        {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"},
    ]
    async with mcp_client as client:
        result = await client.call_tool("route", {"stops": stops, "steps": False})
        too_few = await client.call_tool("route", {"stops": stops[:1]})
    body = _payload(result)
    # Labels are enriched with "reasonable details" (ZIP baseline) from the
    # reverse geocoder, and the structured addresses ride along.
    assert body["from_location"] == "Dettingen (72581)"
    assert body["to_location"] == "Frankfurt (72581)"
    assert [s["label"] for s in body["stop_details"]] == [
        "Dettingen", "Stuttgart", "Frankfurt"]
    assert all(s["address"]["postcode"] == "72581" for s in body["stop_details"])
    assert len(body["legs"]) == 2
    assert body["duration_min"] == pytest.approx(60.0)
    # Geometry is the documented coordinate-list form: ordered {lat, lon}.
    assert body["geometry"] and set(body["geometry"][0]) == {"lat", "lon"}
    assert "2..100 stops" in _error_text(too_few)


async def test_route_alternates_on_two_stop_trip(mcp_client):
    stops = [{"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"},
             {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"}]
    async with mcp_client as client:
        result = await client.call_tool("route", {"stops": stops, "alternates": 2})
    body = _payload(result)
    assert len(body["alternates"]) == 1        # the mock returns one extra route
    assert body["alternates"][0]["duration_min"] == pytest.approx(65.0)


async def test_matrix_returns_minutes(mcp_client):
    points = [{"lat": 48.5, "lon": 9.3}, {"lat": 48.7, "lon": 9.1},
              {"lat": 48.9, "lon": 9.2}]
    async with mcp_client as client:
        result = await client.call_tool("route_matrix", {"points": points})
    matrix = _payload(result)["durations_min"]
    assert len(matrix) == 3 and len(matrix[0]) == 3
    assert matrix[0][1] == pytest.approx(10.0)   # 600 s -> minutes
    assert matrix[0][0] == 0


async def test_extract_features_plumbs_through_with_heights(mcp_client):
    home = TileCoord.at(_LAT, _LON, _ZOOM)
    west, south, east, north = home.bbox_deg()
    dx, dy = (east - west) * 0.1, (north - south) * 0.1
    async with mcp_client as client:
        result = await client.call_tool("extract_features", {
            "bbox": {"west": west + dx, "south": south + dy,
                     "east": east - dx, "north": north - dy},
            "layers": "buildings",
        })
    body = _payload(result)
    assert body["meta"]["layer_counts"] == {"buildings": 1}
    assert body["features"][0]["properties"]["height"] == pytest.approx(11.5)


async def test_street_survey_reads_fixture_roads(mcp_client):
    home = TileCoord.at(_LAT, _LON, _ZOOM)
    west, south, east, north = home.bbox_deg()
    async with mcp_client as client:
        result = await client.call_tool("street_survey", {
            "bbox": {"west": west, "south": south, "east": east, "north": north}})
    body = _payload(result)
    assert "Fabriciusstraße" in body["names"]


async def test_attribution_defaults_to_composites_and_carries_licenses(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool("map_attribution", {
            "bbox": {"west": 9.28, "south": 48.53, "east": 9.30, "north": 48.55},
            "zoom": 15,
        })
        sources = await client.call_tool("list_tile_sources", {})
    body = _payload(result)
    assert "LGL" in body["text"]                     # BW imagery credited
    assert all(s["license_name"] for s in body["sources"])
    listed = _payload(sources)
    assert all(s["license_name"] and s["attribution"] for s in listed["sources"])


async def test_mode_roundtrip_gates_provisioning_and_is_shared(mcp_client, api):
    spec = {"id": "mcp-spec", "source_id": "de-dop",
            "locations": [{"id": "a", "name": "A",
                           "camera": {"center": {"lat": 48.5, "lon": 9.3},
                                      "zoom": 13.0}}]}
    rest_app = FastAPI()
    api.mount(rest_app)
    rest = TestClient(rest_app)
    async with mcp_client as client:
        assert _payload(await client.call_tool("get_runtime_mode", {})) == {"mode": "online"}
        assert _payload(await client.call_tool(
            "set_runtime_mode", {"mode": "mixed"})) == {"mode": "mixed"}
        # One instance, one mode: the REST/UI surface sees the switch too.
        assert rest.get("/api/v1/maps/mode").json() == {"mode": "mixed"}
        harvest = await client.call_tool("harvest_offline_bundle", {"spec": spec})
        assert "requires online mode" in _error_text(harvest)
        # Live per-request lookups stay allowed in mixed; offline refuses.
        assert _payload(await client.call_tool(
            "set_runtime_mode", {"mode": "offline"})) == {"mode": "offline"}
        geocode = await client.call_tool("geocode", {"query": "x"})
        assert "disabled in offline mode" in _error_text(geocode)
        await client.call_tool("set_runtime_mode", {"mode": "online"})


async def test_isochrone_reports_backend_limitation_cleanly(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool("isochrone", {
            "center": {"lat": 48.5, "lon": 9.3},
            "contours": [{"time_min": 10}],
        })
    text = _error_text(result)
    assert "Valhalla" in text and "isochrone" in text.lower()


async def test_render_map_returns_image_bytes_and_enforces_caps(mcp_client, monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"mcp-fake-render"

    class _Fake:
        def __init__(self, *, base_url, chromium_executable_path=None):
            pass

        async def render(self, **kwargs):
            return png

    monkeypatch.setattr(render_service, "RenderService", _Fake)
    async with mcp_client as client:
        result = await client.call_tool("render_map", {
            "center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 15.0,
            "width": 800, "height": 600})
        too_big = await client.call_tool("render_map", {
            "center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 15.0,
            "width": 3000, "height": 600})
    image = result.content[0]
    assert image.type == "image"
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data) == png       # bytes THROUGH the server
    assert "1600" in _error_text(too_big)


async def test_open_map_view_composes_url_and_opens_browser(mcp_client, api, monkeypatch):
    import agentic_maps.mcp_server.server as mcp_server_module

    opened = []
    monkeypatch.setattr(mcp_server_module.webbrowser, "open",
                        lambda url: opened.append(url) or True)
    api.render_base_url = "http://transport.test:9999"
    async with mcp_client as client:
        route = _payload(await client.call_tool("route", {
            "stops": [{"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"},
                      {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"}],
            "alternates": 1}))
        route.pop("stop_details")
        view = _payload(await client.call_tool("open_map_view", {"routes": [route]}))
        # URL composed against THIS instance's public base (per transport:
        # the dev server's real host:port, or the stdio loopback host).
        assert view["url"].startswith("http://transport.test:9999/?session=")
        assert view["opened"] is True and opened == [view["url"]]
        # The stored session serves the spec back with the routes intact.
        session_payload = await client.call_tool("get_map_page_html", {
            "routes": [route], "basemap": "embedded"})
        html = _payload(session_payload)["html"]
        assert "mp-attribution" in html
        assert "http://" not in html and "https://" not in html

        nothing = await client.call_tool("open_map_view", {})
        assert "nothing to show" in _error_text(nothing)


async def test_trip_view_keeps_one_session_and_opens_the_browser_once(
        mcp_client, api, monkeypatch):
    """The Cowork flow: open_map_view(trip_id) mints ONE stable session, the
    session serves the trip's CURRENT state after update_trip (revision
    included), and the browser is opened exactly once — later calls default
    to already_open instead of a new tab; open_browser=true forces."""
    import agentic_maps.mcp_server.server as mcp_server_module

    opened = []
    monkeypatch.setattr(mcp_server_module.webbrowser, "open",
                        lambda url: opened.append(url) or True)
    rest_app = FastAPI()
    api.mount(rest_app)
    rest = TestClient(rest_app)

    async with mcp_client as client:
        created = _payload(await client.call_tool("create_trip", {
            "stops": [{"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"},
                      {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"}]}))

        first = _payload(await client.call_tool(
            "open_map_view", {"trip_id": created["id"]}))
        assert first["opened"] is True and opened == [first["url"]]
        token = first["token"]

        # The session is trip-bound: current state + revision baseline.
        before = rest.get(f"/api/v1/maps/sessions/{token}").json()
        assert len(before["spec"]["locations"]) == 2
        assert before["session_revision"] == 1
        assert rest.get(f"/api/v1/maps/sessions/{token}/revision").json() == {
            "revision": 1}

        # Insert Stuttgart: the OPEN tab's token now serves 3 stops — no
        # re-open, no new session, just the bumped revision to poll.
        await client.call_tool("update_trip", {
            "trip_id": created["id"],
            "operations": [{"op": "add_stop",
                            "stop": {"lat": 48.7838, "lon": 9.1829,
                                     "label": "Stuttgart"},
                            "position": 1}]})
        assert rest.get(f"/api/v1/maps/sessions/{token}/revision").json() == {
            "revision": 2}
        after = rest.get(f"/api/v1/maps/sessions/{token}").json()
        names = [l["name"] for l in after["spec"]["locations"]]
        # display() appends the enriched ZIP — match on the leading label.
        assert len(names) == 3
        for name, label in zip(names, ["Dettingen", "Stuttgart", "Frankfurt"]):
            assert name.startswith(label)
        assert after["session_revision"] == 2

        # Open-once discipline: same URL back, browser NOT reopened.
        second = _payload(await client.call_tool(
            "open_map_view", {"trip_id": created["id"]}))
        assert second["token"] == token and second["url"] == first["url"]
        assert second["already_open"] is True and second["opened"] is False
        assert len(opened) == 1 and "updates itself" in second["note"]

        # open_browser=true forces a fresh window on the SAME session.
        forced = _payload(await client.call_tool(
            "open_map_view", {"trip_id": created["id"], "open_browser": True}))
        assert forced["token"] == token and forced["opened"] is True
        assert opened == [first["url"], first["url"]]

        # A non-trip call still opens per call — the discipline is per trip.
        route = _payload(await client.call_tool("route", {
            "stops": [{"lat": 48.5254, "lon": 9.3466, "label": "A"},
                      {"lat": 50.1109, "lon": 8.6821, "label": "B"}]}))
        route.pop("stop_details")
        plain = _payload(await client.call_tool(
            "open_map_view", {"routes": [route]}))
        assert plain["opened"] is True and plain["token"] != token


async def test_plan_and_list_bundles_work_in_any_mode(mcp_client):
    spec = {"id": "plan-spec", "source_id": "de-dop",
            "locations": [{"id": "a", "name": "A",
                           "camera": {"center": {"lat": 48.5, "lon": 9.3},
                                      "zoom": 13.0}}]}
    async with mcp_client as client:
        plan = _payload(await client.call_tool("plan_offline_bundle", {"spec": spec}))
        bundles = _payload(await client.call_tool("list_offline_bundles", {}))
    assert plan["tile_count"] > 0 and plan["estimated_mb"] > 0
    assert [v["id"] for v in bundles["vector"]] == ["streets-fixture"]


# -- optimize_trip -----------------------------------------------------------

async def test_optimize_trip_with_stops_returns_order_and_reordered_details(mcp_client):
    stops = [
        {"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"},
        {"lat": 49.1427, "lon": 9.2109, "label": "Heilbronn"},
        {"lat": 48.7784, "lon": 9.1806, "label": "Stuttgart"},
        {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"},
    ]
    async with mcp_client as client:
        result = await client.call_tool("optimize_trip", {"stops": stops})
        both = await client.call_tool("optimize_trip", {
            "stops": stops, "trip_id": "trip-x"})
        neither = await client.call_tool("optimize_trip", {})
    body = _payload(result)
    # The mock solver mirrors the middle stops: Stuttgart before Heilbronn.
    assert body["order"] == [0, 2, 1, 3]
    assert body["improved"] is True
    assert [s["label"] for s in body["stop_details"]] == [
        "Dettingen", "Stuttgart", "Heilbronn", "Frankfurt"]
    assert body["route"]["duration_min"] == pytest.approx(50.0)
    assert "exactly one" in _error_text(both)
    assert "exactly one" in _error_text(neither)


async def test_optimize_trip_rewrites_a_live_trip_through_the_normal_recompute(mcp_client):
    stops = [
        {"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"},
        {"lat": 49.1427, "lon": 9.2109, "label": "Heilbronn"},
        {"lat": 48.7784, "lon": 9.1806, "label": "Stuttgart"},
        {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"},
    ]
    async with mcp_client as client:
        created = _payload(await client.call_tool("create_trip", {"stops": stops}))
        optimized = _payload(await client.call_tool(
            "optimize_trip", {"trip_id": created["id"]}))
        fetched = _payload(await client.call_tool("get_trip", {"trip_id": created["id"]}))
    assert optimized["order"] == [0, 2, 1, 3]
    trip = optimized["trip"]
    assert trip["revision"] == 2
    assert [s["label"] for s in trip["stops"]] == [
        "Dettingen", "Stuttgart", "Heilbronn", "Frankfurt"]
    # The store holds the optimized order — every later view reflects it.
    assert [s["label"] for s in fetched["stops"]] == [
        "Dettingen", "Stuttgart", "Heilbronn", "Frankfurt"]
    assert fetched["route"]["legs"] and len(fetched["route"]["legs"]) == 3


# -- reachability -------------------------------------------------------------

async def test_reachability_with_explicit_targets_is_one_row(mcp_client):
    origin = {"lat": 48.5254, "lon": 9.3466}
    targets = [{"lat": 48.7784, "lon": 9.1806}, {"lat": 49.1427, "lon": 9.2109}]
    async with mcp_client as client:
        result = await client.call_tool("reachability", {
            "origin": origin, "targets": targets})
    body = _payload(result)
    assert body["metric"] == "minutes"
    assert [t["minutes"] for t in body["targets"]] == [10.0, 11.0]
    assert body["meta"] == {"count": 2, "requests": 1, "grid": None}


async def test_reachability_grid_generates_lattice_and_refuses_over_cap(mcp_client):
    async with mcp_client as client:
        result = await client.call_tool("reachability", {
            "origin": {"lat": 48.5254, "lon": 9.3466},
            "grid": {"bbox": {"west": 9.0, "south": 48.4, "east": 9.4, "north": 48.7},
                     "step_km": 10.0, "cap": 50}})
        refused = await client.call_tool("reachability", {
            "origin": {"lat": 48.5254, "lon": 9.3466},
            "grid": {"bbox": {"west": 9.0, "south": 48.4, "east": 9.4, "north": 48.7},
                     "step_km": 0.5, "cap": 50}})
    body = _payload(result)
    assert 4 <= body["meta"]["count"] <= 12          # ~3x3 lattice at 10 km
    assert all(t["minutes"] > 0 for t in body["targets"])
    assert all(9.0 < t["lon"] < 9.4 and 48.4 < t["lat"] < 48.7
               for t in body["targets"])
    text = _error_text(refused)
    assert "cap" in text and "step_km" in text


async def test_reachability_demo_budget_refuses_chunking_and_env_unlocks_it(
        mcp_client, monkeypatch):
    import agentic_maps.mcp_server.server as mcp_server_module

    origin = {"lat": 48.5254, "lon": 9.3466}
    many = [{"lat": 48.0 + i * 0.001, "lon": 9.0} for i in range(120)]
    monkeypatch.delenv(mcp_server_module.MATRIX_MAX_ENV, raising=False)
    async with mcp_client as client:
        refused = await client.call_tool("reachability", {
            "origin": origin, "targets": many})
        text = _error_text(refused)
        # The refusal names the real knobs, not just "too many".
        assert "--max-table-size" in text and "service_limits" in text
        monkeypatch.setenv(mcp_server_module.MATRIX_MAX_ENV, "51")
        chunked = await client.call_tool("reachability", {
            "origin": origin, "targets": many})
    body = _payload(chunked)
    assert body["meta"]["count"] == 120
    # 120 targets at 50 per request (51 coords incl. origin) = 3 chunks.
    assert body["meta"]["requests"] == 3
    assert len(body["targets"]) == 120


# -- region-bulk provisioning --------------------------------------------------

async def test_provision_first_call_estimates_and_starts_nothing(mcp_client, api):
    async with mcp_client as client:
        result = await client.call_tool("provision_offline_region", {
            "region": "de", "layers": ["aerial"], "aerial_max_zoom": 14})
        jobs = await client.call_tool("provisioning_status", {})
    body = _payload(result)
    assert body["started"] is False
    assert body["estimate"]["total_bytes"] == pytest.approx(3.6e9, rel=0.10)
    assert "confirm_size" in body["note"]
    assert _payload(jobs) == {"jobs": []}          # nothing was started
    assert api.provision.list() == []


async def test_provision_validation_errors_are_clean(mcp_client):
    async with mcp_client as client:
        no_zoom = await client.call_tool("provision_offline_region", {
            "region": "de", "layers": ["aerial"]})
        no_layers = await client.call_tool("provision_offline_region", {
            "region": "de"})
        both = await client.call_tool("provision_offline_region", {
            "region": "de", "bbox": {"west": 9.0, "south": 48.0,
                                     "east": 10.0, "north": 49.0},
            "layers": ["maps"]})
    assert "aerial_max_zoom" in _error_text(no_zoom)
    assert "layers" in _error_text(no_layers)
    assert "exactly one" in _error_text(both)


async def test_provision_confirmed_job_runs_and_reports_status(mcp_client, api, monkeypatch):
    async def fake_tile(source_id, coord):
        return b"\xff\xd8tile-bytes"

    monkeypatch.setattr(api, "fetch_aerial_band_tile", fake_tile)
    async with mcp_client as client:
        started = _payload(await client.call_tool("provision_offline_region", {
            "bbox": {"west": 9.27, "south": 48.53, "east": 9.30, "north": 48.55},
            "region_id": "metzingen", "layers": ["aerial"],
            "aerial_max_zoom": 13, "confirm_size": True}))
        assert started["started"] is True
        job_id = started["job"]["id"]
        for _ in range(300):
            status = _payload(await client.call_tool(
                "provisioning_status", {"job_id": job_id}))
            if status["state"] not in ("pending", "running"):
                break
            await asyncio.sleep(0.01)
        missing = await client.call_tool("cancel_provisioning",
                                         {"job_id": "provision-nope"})
    assert status["state"] == "done"
    aerial = status["layers"][0]
    assert aerial["status"] == "done"
    assert aerial["tiles_done"] == aerial["tiles_total"] > 0
    assert aerial["bytes_done"] == aerial["tiles_done"] * len(b"\xff\xd8tile-bytes")
    assert "unknown provisioning job" in _error_text(missing)


async def test_provision_gating_follows_runtime_mode(mcp_client):
    async with mcp_client as client:
        await client.call_tool("set_runtime_mode", {"mode": "mixed"})
        refused = await client.call_tool("provision_offline_region", {
            "region": "de", "layers": ["maps"], "confirm_size": True})
        # The estimate half stays free in mixed — planning is pure math.
        estimate = await client.call_tool("provision_offline_region", {
            "region": "de", "layers": ["maps"]})
        await client.call_tool("set_runtime_mode", {"mode": "online"})
    assert "requires online mode" in _error_text(refused)
    assert _payload(estimate)["started"] is False


async def test_new_tool_descriptions_carry_their_contracts(mcp_client):
    async with mcp_client as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    provision = tools["provision_offline_region"].description
    assert "confirm_size" in provision
    assert "eu aerial z13-15" in provision           # the size table rides along
    assert "MANY HOURS" in provision                 # EU routing honesty
    assert "mixed" in provision                      # provisioning gating
    reach = tools["reachability"].description
    assert "--max-table-size" in reach and "service_limits" in reach
    assert "100" in reach
    optimize = tools["optimize_trip"].description
    assert "100" in optimize                         # same stop ceiling as route
    assert "Valhalla" in optimize and "OSRM" in optimize
