"""Live trips: the bounded store (unit) and the MCP tool flow (mocked OSRM).

Store discipline gets unit tests (LRU under both caps, TTL, the helpful
error); the tool flow reuses `test_mcp_server`'s in-memory MCP client
pattern — insert-at-position recomputes the via[] list correctly, options
changes recompute, enrichment happens exactly once per stop, and the
trip-backed view/page tools reflect current state.
"""

import json

import httpx
import pytest
from mcp.client.client import Client

from agentic_maps.mcp_server.trips import TripStore, estimate_trip_bytes
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_route import MapRoute
from agentic_maps.models.route_stop import RouteStop
from agentic_maps.models.trip_state import TripState
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.sources.presets import builtin_composites, builtin_sources

DETTINGEN = {"lat": 48.5254, "lon": 9.3466, "label": "Dettingen"}
STUTTGART = {"lat": 48.7784, "lon": 9.1806, "label": "Stuttgart"}
FRANKFURT = {"lat": 50.1109, "lon": 8.6821, "label": "Frankfurt"}


def _route(points: int = 10) -> MapRoute:
    return MapRoute(
        id="r", from_location="A", to_location="B", duration_min=60.0,
        distance_km=100.0,
        geometry=[LatLon(lat=48.0 + i * 0.01, lon=9.0) for i in range(points)],
    )


def _trip(trip_id: str, points: int = 10) -> TripState:
    return TripState(
        id=trip_id, route=_route(points),
        stops=[RouteStop(**DETTINGEN), RouteStop(**FRANKFURT)],
    )


def test_stop_display_composes_reasonable_detail():
    from agentic_maps.models.geocode_address import GeocodeAddress

    # Labeled stop: locality is the label, ZIP is the baseline detail.
    labeled = RouteStop(lat=48.5, lon=9.3, label="Dettingen an der Erms",
                        address=GeocodeAddress(road="Hauptstraße", postcode="72581",
                                               locality="Dettingen an der Erms"))
    assert labeled.display() == "Dettingen an der Erms (72581)"
    # Bare coordinate stop: the road IS its identity — street joins.
    bare = RouteStop(lat=50.1, lon=8.68,
                     address=GeocodeAddress(road="Niederräder Ufer", postcode="60528",
                                            locality="Frankfurt am Main"))
    assert bare.display() == "Frankfurt am Main, Niederräder Ufer (60528)"
    # House-number hit is street-precise even with a label.
    precise = RouteStop(lat=48.5, lon=9.3, label="HQ",
                        address=GeocodeAddress(road="Keckbronnenweg", house_number="4",
                                               postcode="72581", locality="Dettingen"))
    assert precise.display() == "HQ, Keckbronnenweg 4 (72581)"
    # No address (offline): coordinates shown honestly.
    assert RouteStop(lat=48.5, lon=9.3).display() == "48.50000, 9.30000"
    assert RouteStop(lat=48.5, lon=9.3, label="X").display() == "X"


# -- store unit tests --------------------------------------------------------

def test_store_roundtrip_and_touch_refreshes_ttl():
    store = TripStore(ttl_s=100)
    store.put(_trip("t1"))
    trip = store.get("t1")
    assert trip.id == "t1" and trip.size_bytes == estimate_trip_bytes(trip.route)


def test_store_expires_idle_trips():
    store = TripStore(ttl_s=100)
    store.put(_trip("t1"))
    store._trips["t1"].touched_at -= 200
    with pytest.raises(KeyError, match="create_trip"):
        store.get("t1")


def test_store_evicts_lru_over_trip_count():
    # `put` timestamps monotonically, so creation order IS the LRU order —
    # and a `get` refreshes: touching t1 saves it, dropping t2 instead.
    store = TripStore(max_trips=2)
    store.put(_trip("t1"))
    store.put(_trip("t2"))
    store.get("t1")
    store.put(_trip("t3"))
    assert {t.id for t in store.all()} == {"t1", "t3"}
    with pytest.raises(KeyError, match="evicted|expired"):
        store.get("t2")


def test_store_evicts_under_byte_cap():
    # Each 1000-point trip weighs ~100 KB by the estimate; the byte cap
    # keeps roughly two of them although the count cap would allow ten.
    store = TripStore(max_trips=10, max_bytes=250_000)
    for index in range(4):
        store.put(_trip(f"t{index}", points=1000))
    kept = [t.id for t in store.all()]
    assert set(kept) == {"t2", "t3"}    # least recently used evicted first


# -- MCP tool flow ------------------------------------------------------------

def _osrm_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    coords = url.split("/v1/")[1].split("/", 1)[1].split("?")[0]
    waypoints = [[float(v) for v in pair.split(",")] for pair in coords.split(";")]
    n = len(waypoints)
    route = {
        "duration": 3600.0 * (n - 1), "distance": 100_000.0 * (n - 1),
        # Echo the request's waypoints as the geometry, so a test can PROVE
        # the recomputed route passes an inserted stop.
        "geometry": {"coordinates": waypoints},
        "legs": [{"duration": 3600.0, "distance": 100_000.0, "steps": []}
                 for _ in range(n - 1)],
    }
    routes = [route]
    if "alternatives=" in url:
        routes.append({**route, "duration": route["duration"] + 300.0})
    return httpx.Response(200, json={"code": "Ok", "routes": routes})


class _CountingNominatim:
    def __init__(self):
        self.reverse_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert "/reverse" in str(request.url)
        self.reverse_calls += 1
        return httpx.Response(200, json={
            "lat": str(request.url.params["lat"]), "lon": str(request.url.params["lon"]),
            "display_name": "irgendwo",
            "address": {"town": "Teststadt", "postcode": "70000"},
        })


@pytest.fixture
def world(tmp_path, monkeypatch):
    import agentic_maps.mcp_server.server as mcp_server_module
    from agentic_maps.mcp_server.server import build_mcp_server

    monkeypatch.setattr(mcp_server_module, "ENRICH_SPACING_S", 0)
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    api = MapsApi(
        bundles, sources=builtin_sources(), composites=builtin_composites(),
        router=OsrmRouter("https://osrm.test", transport=httpx.MockTransport(_osrm_handler)),
        remote_planet=False, render_base_url="http://maps.test:8195",
    )
    nominatim = _CountingNominatim()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(nominatim.handler))
    return Client(build_mcp_server(api)), nominatim


def _payload(result) -> dict:
    assert not result.is_error, [c.text for c in result.content]
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _error_text(result) -> str:
    assert result.is_error
    return " ".join(c.text for c in result.content)


async def test_create_insert_and_recompute(world):
    mcp_client, nominatim = world
    async with mcp_client as client:
        created = _payload(await client.call_tool("create_trip", {
            "stops": [DETTINGEN, FRANKFURT]}))
        assert created["revision"] == 1
        assert len(created["route"]["stops"]) == 2
        # Both stops enriched exactly once at creation.
        assert nominatim.reverse_calls == 2
        assert created["stops"][0]["address"]["postcode"] == "70000"
        assert "(70000)" in created["route"]["from_location"]

        # The owner's case: insert a stop mid-route.
        updated = _payload(await client.call_tool("update_trip", {
            "trip_id": created["id"],
            "operations": [{"op": "add_stop", "stop": STUTTGART, "position": 1}],
        }))
        assert updated["revision"] == 2
        assert [s["label"] for s in updated["stops"]] == [
            "Dettingen", "Stuttgart", "Frankfurt"]
        # The mocked backend echoes waypoints: the recomputed geometry now
        # actually passes Stuttgart, in order.
        geometry = updated["route"]["geometry"]
        assert [round(p["lat"], 4) for p in geometry] == [48.5254, 48.7784, 50.1109]
        assert len(updated["route"]["legs"]) == 2
        # Only the NEW stop hit the geocoder; the others kept their cache.
        assert nominatim.reverse_calls == 3

        # Reading mutates nothing.
        read = _payload(await client.call_tool("get_trip", {"trip_id": created["id"]}))
        assert read["revision"] == 2
        assert nominatim.reverse_calls == 3

        listed = _payload(await client.call_tool("list_trips", {}))
        assert [t["trip_id"] for t in listed["trips"]] == [created["id"]]
        assert listed["trips"][0]["revision"] == 2


async def test_set_options_recomputes_with_new_mode(world):
    mcp_client, _ = world
    async with mcp_client as client:
        created = _payload(await client.call_tool("create_trip", {
            "stops": [DETTINGEN, FRANKFURT], "alternates": 1}))
        assert created["route"]["alternates"]          # alternates requested
        updated = _payload(await client.call_tool("update_trip", {
            "trip_id": created["id"],
            "operations": [{"op": "set_options", "mode": "walk", "alternates": 0}],
        }))
        assert updated["mode"] == "walk"
        assert updated["route"]["mode"] == "walk"
        assert updated["route"]["alternates"] == []
        assert updated["revision"] == 2


async def test_unknown_trip_yields_recreate_guidance(world):
    mcp_client, _ = world
    async with mcp_client as client:
        result = await client.call_tool("get_trip", {"trip_id": "trip-gone"})
        text = _error_text(result)
        assert "create_trip" in text and "evicted" in text


async def test_trip_backed_view_and_page(world, monkeypatch):
    import agentic_maps.mcp_server.server as mcp_server_module

    opened = []
    monkeypatch.setattr(mcp_server_module.webbrowser, "open",
                        lambda url: opened.append(url) or True)
    mcp_client, _ = world
    async with mcp_client as client:
        created = _payload(await client.call_tool("create_trip", {
            "stops": [DETTINGEN, STUTTGART, FRANKFURT]}))

        view = _payload(await client.call_tool("open_map_view", {
            "trip_id": created["id"]}))
        assert view["url"].startswith("http://maps.test:8195/?session=")
        assert view["opened"] is True and opened == [view["url"]]

        quiet = _payload(await client.call_tool("open_map_view", {
            "trip_id": created["id"], "open_browser": False}))
        assert quiet["opened"] is False and len(opened) == 1

        page = _payload(await client.call_tool("get_map_page_html", {
            "trip_id": created["id"], "basemap": "embedded"}))
        html = page["html"]
        # All three stops appear with their enriched labels (label + ZIP
        # baseline), and the attribution footer is baked in.
        for label in ("Dettingen (70000)", "Stuttgart (70000)", "Frankfurt (70000)"):
            assert label in html
        assert "mp-attribution" in html
        assert "https://" not in html and "http://" not in html

        gone = await client.call_tool("open_map_view", {"trip_id": "trip-x"})
        assert "create_trip" in _error_text(gone)
