"""Browser sessions (`POST /sessions`) and the self-contained page endpoint."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agentic_maps.rest.maps_api as maps_api_module
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_route import MapRoute
from agentic_maps.models.map_spec import MapSpec
from agentic_maps.models.route_step import RouteStep
from agentic_maps.models.via_place import ViaPlace
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.sources.presets import builtin_composites, builtin_sources


def _route(route_id="r1", **overrides) -> MapRoute:
    line = [LatLon(lat=48.5 + i * 0.02, lon=9.0 - i * 0.004) for i in range(60)]
    fields = dict(
        id=route_id, from_location="Dettingen an der Erms (72581)",
        to_location="Frankfurt am Main, Niederräder Ufer (60528)",
        duration_min=161.0, distance_km=214.0, geometry=line,
        steps=[
            RouteStep(type="depart", name="Hauptstraße", distance_m=400, duration_s=60,
                      location=line[0]),
            RouteStep(type="turn", modifier="right", ref="A 8", distance_m=90_000,
                      duration_s=3200, location=line[20]),
            RouteStep(type="arrive", location=line[-1]),
        ],
        via_places=[ViaPlace(name="Pforzheim", lat=48.89, lon=8.7, population=125_000,
                             score=1.0, along_km=60.0)],
        stops=[line[0], line[-1]],
    )
    fields.update(overrides)
    return MapRoute(**fields)


def _spec(**overrides) -> dict:
    primary = _route()
    primary.alternates = [_route(
        route_id="r1-alt0", duration_min=175.0, distance_km=249.0,
        via_places=[ViaPlace(name="Heilbronn", lat=49.14, lon=9.21,
                             population=126_000, score=0.9, along_km=80.0)],
    )]
    spec = MapSpec(id="view-test", source_id="de-dop", routes=[primary],
                   interactive=True)
    return {**spec.model_dump(), **overrides}


@pytest.fixture
def client(tmp_path):
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    api = MapsApi(
        bundles, sources=builtin_sources(), composites=builtin_composites(),
        remote_planet=False, render_base_url="http://maps.test:8195",
    )
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


@pytest.fixture
def api(tmp_path):
    bundles = tmp_path / "bundles-live"
    bundles.mkdir()
    return MapsApi(
        bundles, sources=builtin_sources(), composites=builtin_composites(),
        remote_planet=False, render_base_url="http://maps.test:8195",
    )


def test_session_roundtrip_and_url_composition(client):
    created = client.post("/api/v1/maps/sessions", json=_spec()).json()
    assert created["url"] == f"http://maps.test:8195/?session={created['token']}"
    assert created["expires_in_s"] == 3600
    payload = client.get(f"/api/v1/maps/sessions/{created['token']}").json()
    assert payload["spec"]["id"] == "view-test"
    assert payload["spec"]["routes"][0]["via_places"][0]["name"] == "Pforzheim"
    # The payload is wired like every other one — tiles, glyphs, attribution.
    assert payload["tiles_url_template"].startswith("/api/v1/maps/")
    assert "OpenStreetMap" in payload["attribution"]


def test_session_expiry_and_unknown_token(client, monkeypatch):
    created = client.post("/api/v1/maps/sessions", json=_spec()).json()
    assert client.get("/api/v1/maps/sessions/does-not-exist").status_code == 404
    real_time = maps_api_module.time.time
    monkeypatch.setattr(maps_api_module.time, "time", lambda: real_time() + 3700)
    response = client.get(f"/api/v1/maps/sessions/{created['token']}")
    assert response.status_code == 404
    assert "expired" in response.json()["detail"]


def test_session_payload_cap(client, monkeypatch):
    monkeypatch.setattr(maps_api_module, "SESSION_PAYLOAD_MAX_BYTES", 2000)
    response = client.post("/api/v1/maps/sessions", json=_spec())
    assert response.status_code == 413
    assert "cap" in response.json()["detail"]


def test_session_rejects_unknown_source(client):
    response = client.post("/api/v1/maps/sessions", json=_spec(source_id="nope"))
    assert response.status_code == 404


def test_frozen_session_revision_is_constant_one(client):
    token = client.post("/api/v1/maps/sessions", json=_spec()).json()["token"]
    assert client.get(f"/api/v1/maps/sessions/{token}/revision").json() == {"revision": 1}
    assert client.get("/api/v1/maps/sessions/nope/revision").status_code == 404


def test_trip_bound_session_serves_live_state_and_revision(api):
    """The stable-tab contract: a bound session serves the CURRENT state on
    every GET (not the frozen mint-time copy) and its revision endpoint
    reports the live counter the `?session=` page polls."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC

    app = FastAPI()
    api.mount(app)
    client = _TC(app)

    created = client.post("/api/v1/maps/sessions", json=_spec()).json()
    token = created["token"]

    state = {"spec": MapSpec.model_validate(_spec(id="live-v1")), "revision": 1}
    api.bind_session_source(
        token, lambda: state["spec"], lambda: state["revision"])

    first = client.get(f"/api/v1/maps/sessions/{token}").json()
    assert first["spec"]["id"] == "live-v1"
    assert first["session_revision"] == 1
    assert client.get(f"/api/v1/maps/sessions/{token}/revision").json() == {"revision": 1}

    # The live object mutates (update_trip in real life): the SAME token
    # serves the new state and the bumped revision — no new session minted.
    state["spec"] = MapSpec.model_validate(_spec(id="live-v2"))
    state["revision"] = 2
    assert client.get(f"/api/v1/maps/sessions/{token}/revision").json() == {"revision": 2}
    second = client.get(f"/api/v1/maps/sessions/{token}").json()
    assert second["spec"]["id"] == "live-v2"
    assert second["session_revision"] == 2

    # Live object gone (trip expired/evicted): the last frozen copy still
    # serves — an open tab degrades to a static view instead of breaking.
    state["spec"] = None
    state["revision"] = None
    fallback = client.get(f"/api/v1/maps/sessions/{token}").json()
    assert fallback["spec"]["id"] == "live-v2"
    assert fallback["session_revision"] is None
    assert client.get(f"/api/v1/maps/sessions/{token}/revision").json() == {"revision": 1}


def test_trip_bound_session_polling_refreshes_ttl(api, monkeypatch):
    """A tab that keeps polling keeps its session alive past the mint-time
    hour; `session_alive` is what the MCP layer checks before reusing."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC

    app = FastAPI()
    api.mount(app)
    client = _TC(app)
    token = client.post("/api/v1/maps/sessions", json=_spec()).json()["token"]
    spec = MapSpec.model_validate(_spec(id="alive"))
    api.bind_session_source(token, lambda: spec, lambda: 1)

    real_time = maps_api_module.time.time
    offset = {"s": 0.0}
    monkeypatch.setattr(maps_api_module.time, "time", lambda: real_time() + offset["s"])
    # 50 min pass, the tab polls — the TTL slides forward.
    offset["s"] = 3000.0
    assert client.get(f"/api/v1/maps/sessions/{token}/revision").status_code == 200
    # Another 50 min: past the ORIGINAL expiry, alive thanks to the poll.
    offset["s"] = 6000.0
    assert api.session_alive(token)
    assert client.get(f"/api/v1/maps/sessions/{token}").status_code == 200
    # Silence for over an hour ends it — bounded memory keeps its promise.
    offset["s"] = 6000.0 + 3700.0
    assert not api.session_alive(token)
    assert client.get(f"/api/v1/maps/sessions/{token}/revision").status_code == 404


def test_page_bakes_attribution_and_candidates(client):
    token = client.post("/api/v1/maps/sessions", json=_spec()).json()["token"]
    html = client.get(f"/api/v1/maps/sessions/{token}/page").text
    # Attribution footer is part of the document, not an option.
    assert "mp-attribution" in html
    assert "OpenStreetMap" in html
    assert "muss bei jeder Weitergabe erhalten bleiben" in html
    # Header carries the enriched endpoint details (ZIP baseline).
    assert "Dettingen an der Erms (72581)" in html
    assert "Niederräder Ufer (60528)" in html
    # Both candidates are embedded and clickable: lines, rows, steps lists.
    assert html.count('data-cand="1"') >= 3
    assert "über Pforzheim" in html and "über Heilbronn" in html
    assert "Wegbeschreibung — Route" in html
    assert "Wegbeschreibung — Alternative 1" in html
    # German step wording and the summary line.
    assert "Im Kreisverkehr" not in html          # none in fixture
    assert "rechts abbiegen" in html
    # The promote script is inline and fetch-free.
    assert "promote" in html and "fetch(" not in html
    # Tier (a): our own tile endpoints, never a third-party host.
    assert "http://maps.test:8195/api/v1/maps/live/" in html
    assert not re.search(r"https?://(?!maps\.test:8195/)", html)


def test_page_embedded_tier_has_zero_network_references(client):
    token = client.post("/api/v1/maps/sessions", json=_spec()).json()["token"]
    html = client.get(f"/api/v1/maps/sessions/{token}/page",
                      params={"basemap": "embedded"}).text
    assert not re.search(r"https?://", html)
    assert "<img" not in html
    assert "src=" not in html                    # no external assets of any kind
    # Still a complete display: route lines, graticule, scale, attribution.
    assert "mp-line" in html and "mp-grid" in html and "mp-scale" in html
    assert "mp-attribution" in html


def test_page_uniqueness_rule_when_top_place_is_shared(client):
    heidelberg = ViaPlace(name="Heidelberg", lat=49.4, lon=8.69,
                          population=426_000, score=1.05, along_km=150.0)
    karlsruhe = ViaPlace(name="Karlsruhe", lat=49.0, lon=8.4,
                         population=377_000, score=1.04, along_km=100.0)
    primary = _route(via_places=[heidelberg, karlsruhe])
    primary.alternates = [_route(route_id="alt", via_places=[heidelberg])]
    spec = MapSpec(id="shared", source_id="de-dop", routes=[primary]).model_dump()
    token = client.post("/api/v1/maps/sessions", json=spec).json()["token"]
    html = client.get(f"/api/v1/maps/sessions/{token}/page").text
    # Primary's top place (Heidelberg) is shared — the distinguishing
    # Karlsruhe LEADS the two-name form; the alternate has nothing unique
    # and falls back to the shared top place alone.
    assert "über Karlsruhe und Heidelberg" in html
    assert "über Heidelberg" in html


def test_page_truncates_huge_step_lists(client):
    many = [RouteStep(type="turn", modifier="left", name=f"Straße {i}",
                      distance_m=100, duration_s=10) for i in range(80)]
    primary = _route()
    primary.steps = many
    spec = MapSpec(id="big", source_id="de-dop", routes=[primary]).model_dump()
    token = client.post("/api/v1/maps/sessions", json=spec).json()["token"]
    html = client.get(f"/api/v1/maps/sessions/{token}/page").text
    assert "+20 weitere Schritte" in html


def test_page_stays_under_size_cap_with_dense_geometry(client):
    from agentic_maps.export.map_page import MAP_PAGE_MAX_BYTES

    dense = [LatLon(lat=48.5 + i * 0.0001, lon=9.0 + i * 0.0001) for i in range(20_000)]
    primary = _route()
    primary.geometry = dense
    primary.alternates = [_route(route_id="a0", geometry=dense),
                          _route(route_id="a1", geometry=dense)]
    spec = MapSpec(id="dense", source_id="de-dop", routes=[primary]).model_dump()
    token = client.post("/api/v1/maps/sessions", json=spec).json()["token"]
    response = client.get(f"/api/v1/maps/sessions/{token}/page")
    assert response.status_code == 200
    assert len(response.content) < MAP_PAGE_MAX_BYTES
