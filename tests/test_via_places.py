"""Via-place corridor matching: the "via Pforzheim" engine.

Unit-level against synthetic cities (geometry math, endpoint exclusion,
priority order, spacing suppression) plus the REST integration: /route
annotates the primary AND every alternate from the offline city index.
"""

import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.geo.via_places import find_via_places
from agentic_maps.models.city_place import CityPlace
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.sources.presets import builtin_composites, builtin_sources


def _line(a: tuple, b: tuple, points: int = 50) -> list[LatLon]:
    return [
        LatLon(lat=a[0] + (b[0] - a[0]) * i / (points - 1),
               lon=a[1] + (b[1] - a[1]) * i / (points - 1))
        for i in range(points)
    ]


# A ~180 km south-north test route.
START, END = (48.5, 9.0), (50.1, 8.7)
ROUTE = _line(START, END)


def city(name, lat, lon, population, **kwargs) -> CityPlace:
    return CityPlace(name=name, lat=lat, lon=lon, population=population, **kwargs)


def test_corridor_membership_and_endpoint_exclusion():
    places = [
        city("Korridorstadt", 49.30, 8.88, 120_000),   # ~2 km off the line
        city("Fernstadt", 49.30, 9.15, 500_000),       # ~20 km off: outside
        city("Startville", 48.53, 9.01, 80_000),       # ~3 km from start: excluded
        city("Zielheim", 50.06, 8.72, 80_000),         # ~5 km from end: excluded
    ]
    names = [p.name for p in find_via_places(ROUTE, places)]
    assert names == ["Korridorstadt"]


def test_priority_order_population_and_capital_boost():
    places = [
        city("Kleinstadt", 48.80, 8.95, 20_000),
        city("Grossstadt", 49.30, 8.88, 600_000),
        city("Hauptstadt", 49.70, 8.80, 90_000, capital=True),
        city("Landeshaupt", 49.00, 8.92, 90_000, state_capital=True),
    ]
    result = find_via_places(ROUTE, places)
    names = [p.name for p in result]
    # Capital boost (+0.60) lifts a 90k capital over the 600k city (log10
    # gap ≈ 0.12); the state-capital boost (+0.10) tips near-ties only, so
    # the 600k city stays ahead of the 90k state capital.
    assert names == ["Hauptstadt", "Grossstadt", "Landeshaupt", "Kleinstadt"]
    # Scores are the ordering key, descending.
    scores = [p.score for p in result]
    assert scores == sorted(scores, reverse=True)


def test_spacing_bonus_suppresses_the_second_suburb():
    places = [
        city("Suburb-A", 49.30, 8.88, 50_000),
        city("Suburb-B", 49.32, 8.88, 40_000),   # ~2 km along from A
        city("Ferntal", 48.85, 8.94, 30_000),    # far along-route from both
    ]
    names = [p.name for p in find_via_places(ROUTE, places)]
    # A wins outright; B loses its spacing bonus (adjacent to A) so the
    # smaller but distant Ferntal overtakes it.
    assert names == ["Suburb-A", "Ferntal", "Suburb-B"]


def test_along_km_supports_geographic_resort():
    places = [
        city("Nord", 49.90, 8.73, 50_000),
        city("Mitte", 49.30, 8.88, 60_000),
        city("Sued", 48.80, 8.95, 40_000),
    ]
    result = find_via_places(ROUTE, places)
    by_geography = sorted(result, key=lambda p: p.along_km)
    assert [p.name for p in by_geography] == ["Sued", "Mitte", "Nord"]
    assert all(p.along_km > 0 for p in result)
    assert by_geography[-1].along_km < 200  # sane km scale for a ~180 km route


def test_compute_cost_stays_small_with_full_index_size():
    # ~7300 synthetic places spread over Germany — the real index's shape.
    places = [
        city(f"P{i}", 47.0 + (i % 85) * 0.04, 6.0 + (i // 85) * 0.11, 10_000 + i)
        for i in range(7300)
    ]
    geometry = _line(START, END, points=1200)   # a realistic full geometry
    started = time.perf_counter()
    find_via_places(geometry, places)
    elapsed = time.perf_counter() - started
    # A few ms in practice; generous bound for slow CI.
    assert elapsed < 0.25


def test_short_or_empty_inputs_yield_empty():
    assert find_via_places([], [city("X", 49.0, 8.9, 1000)]) == []
    assert find_via_places(ROUTE, []) == []


# -- REST integration --------------------------------------------------------

def _ne_feature(name, lat, lon, pop, featurecla="Populated place"):
    return {
        "type": "Feature",
        "properties": {"name": name, "adm0name": "Deutschland",
                       "labelrank": 6, "pop_max": pop, "featurecla": featurecla},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    }


def _osrm_with_alternate(request: httpx.Request) -> httpx.Response:
    primary = {
        "duration": 7200.0, "distance": 180_000.0,
        "geometry": {"coordinates": [[p.lon, p.lat] for p in ROUTE]},
        "legs": [{"duration": 7200.0, "distance": 180_000.0, "steps": []}],
    }
    # The alternate swings east through its own corridor.
    east = _line((48.5, 9.0), (50.1, 9.4))
    alternate = {
        **primary,
        "duration": 7800.0,
        "geometry": {"coordinates": [[p.lon, p.lat] for p in east]},
    }
    routes = [primary] + ([alternate] if "alternatives=" in str(request.url) else [])
    return httpx.Response(200, json={"code": "Ok", "routes": routes})


@pytest.fixture
def api_with_cities(tmp_path):
    geo = tmp_path / "geo"
    geo.mkdir()
    (geo / "cities.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            _ne_feature("Korridorstadt", 49.30, 8.88, 120_000),
            _ne_feature("Oststadt", 49.30, 9.25, 90_000),
            _ne_feature("Abseits", 49.30, 10.5, 800_000),
        ],
    }))
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    return MapsApi(
        bundles,
        sources=builtin_sources(), composites=builtin_composites(),
        router=OsrmRouter("https://osrm.test",
                          transport=httpx.MockTransport(_osrm_with_alternate)),
        remote_planet=False,
    )


def test_route_endpoint_annotates_primary_and_alternates(api_with_cities):
    app = FastAPI()
    api_with_cities.mount(app)
    client = TestClient(app)
    body = client.post("/api/v1/maps/route", json={
        "route_id": "r1",
        "start": {"lat": START[0], "lon": START[1]},
        "end": {"lat": END[0], "lon": END[1]},
        "alternates": 1,
    }).json()
    assert [p["name"] for p in body["via_places"]] == ["Korridorstadt"]
    assert len(body["alternates"]) == 1
    assert [p["name"] for p in body["alternates"][0]["via_places"]] == ["Oststadt"]


def test_route_endpoint_survives_missing_city_index(tmp_path):
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    api = MapsApi(
        bundles,
        sources=builtin_sources(), composites=builtin_composites(),
        router=OsrmRouter("https://osrm.test",
                          transport=httpx.MockTransport(_osrm_with_alternate)),
        remote_planet=False,
    )
    app = FastAPI()
    api.mount(app)
    body = TestClient(app).post("/api/v1/maps/route", json={
        "route_id": "r1",
        "start": {"lat": START[0], "lon": START[1]},
        "end": {"lat": END[0], "lon": END[1]},
    }).json()
    assert body["via_places"] == []
