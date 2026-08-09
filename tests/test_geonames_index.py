"""The dense GeoNames place index: prep → asset → loader → consumers.

Covers the prep tool's parsing/filtering (fixture TSV, never the real
download), the loader round-trip, the bucketed corridor fast path, search
ranking on the densified data (the documented Hannover examples), the REST
preference-plus-fallback wiring, and the CC-BY credit in package manifests.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.geo.geonames import PlaceIndex
from agentic_maps.geo.via_places import bucket_places, find_via_places
from agentic_maps.models.city_place import CityPlace
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.sources.presets import builtin_composites, builtin_sources

_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "make_city_index.py"


def _prep_tool():
    """tools/ is not a package — load the prep module from its file."""
    spec = importlib.util.spec_from_file_location("make_city_index", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("make_city_index", module)
    spec.loader.exec_module(module)
    return module


def _geoname_line(geonameid, name, ascii_name, lat, lon, code, country, population):
    """One row in the real cities15000.txt column layout (19 columns)."""
    return "\t".join([
        str(geonameid), name, ascii_name, "alt1,alt2", str(lat), str(lon),
        "P", code, country, "", "01", "082", "", "", str(population), "",
        "300", "Europe/Berlin", "2026-01-01",
    ])


SOURCE_TEXT = "\n".join([
    _geoname_line(1, "Pforzheim", "Pforzheim", 48.88436, 8.69892, "PPLA3", "DE", 119313),
    _geoname_line(2, "Heilbronn", "Heilbronn", 49.13995, 9.22054, "PPLA3", "DE", 120733),
    _geoname_line(3, "Stuttgart", "Stuttgart", 48.78232, 9.17702, "PPLA", "DE", 632865),
    _geoname_line(4, "Berlin", "Berlin", 52.52437, 13.41053, "PPLC", "DE", 3644826),
    # A borough — must be dropped (PPLX), or Berlin routes read "via Neukölln".
    _geoname_line(5, "Neukölln", "Neukoelln", 52.47719, 13.43126, "PPLX", "DE", 310283),
    # Diacritics: ascii differs from name, so the column is kept.
    _geoname_line(6, "Köln", "Koeln", 50.93333, 6.95, "PPLA2", "DE", 1024621),
    _geoname_line(7, "Hanoi", "Hanoi", 21.02888, 105.85416, "PPLC", "VN", 8053663),
]) + "\n"


@pytest.fixture
def asset_path(tmp_path) -> Path:
    out = tmp_path / "geo" / "places-geonames.tsv.gz"
    total, germany = _prep_tool().build(SOURCE_TEXT, out)
    assert (total, germany) == (6, 5)   # the PPLX borough is gone
    return out


def test_loader_round_trip_types_and_flags(asset_path):
    index = PlaceIndex(asset_path)
    assert index.available
    places = {p.name: p for p in index.places()}
    assert "Neukölln" not in places
    assert places["Berlin"].capital and not places["Berlin"].state_capital
    assert places["Stuttgart"].state_capital and not places["Stuttgart"].capital
    assert not places["Pforzheim"].capital and not places["Pforzheim"].state_capital
    assert places["Köln"].ascii_name == "Koeln"
    assert places["Pforzheim"].ascii_name == ""   # identical → stored blank
    assert places["Heilbronn"].population == 120733
    assert places["Hanoi"].country == "VN"
    assert abs(places["Pforzheim"].lat - 48.88436) < 1e-4
    # The provenance comment lines are skipped, not parsed as rows.
    assert len(index.places()) == 6


def test_missing_asset_reports_unavailable(tmp_path):
    assert not PlaceIndex(tmp_path / "nope.tsv.gz").available


def test_bucketed_corridor_matches_flat_scan(asset_path):
    places = PlaceIndex(asset_path).places()
    # Dettingen → Frankfurt-ish south-north line passing Pforzheim/Stuttgart.
    geometry = [
        LatLon(lat=48.5 + 1.6 * i / 99, lon=9.3 - 0.6 * i / 99) for i in range(100)
    ]
    flat = find_via_places(geometry, places)
    bucketed = find_via_places(geometry, places, buckets=bucket_places(places))
    assert [p.name for p in flat] == [p.name for p in bucketed]
    assert flat  # the corridor does hit at least one of the fixture towns


def test_bucketed_corridor_cost_at_geonames_scale():
    # ~32k synthetic places over Europe — the real index's shape.
    places = [
        CityPlace(name=f"P{i}", lat=36.0 + (i % 180) * 0.12,
                  lon=-10.0 + (i // 180) * 0.25, population=15_000 + i)
        for i in range(32_000)
    ]
    buckets = bucket_places(places)
    geometry = [
        LatLon(lat=48.5 + 1.6 * i / 1199, lon=9.35 - 0.66 * i / 1199)
        for i in range(1200)
    ]
    find_via_places(geometry, places, buckets=buckets)  # warm
    started = time.perf_counter()
    find_via_places(geometry, places, buckets=buckets)
    elapsed = time.perf_counter() - started
    # ~4 ms in practice; generous bound for slow CI — the point is that the
    # 4.4x larger index must not scale the per-route cost linearly.
    assert elapsed < 0.1


# -- search ranking on the dense data ----------------------------------------

def _search_index(tmp_path, rows) -> PlaceIndex:
    out = tmp_path / "places.tsv.gz"
    _prep_tool().build("\n".join(rows) + "\n", out)
    return PlaceIndex(out)


def test_search_home_city_first_and_megacity_over_near_town(tmp_path):
    index = _search_index(tmp_path, [
        _geoname_line(1, "Hannover", "Hannover", 52.37052, 9.73322, "PPLA", "DE", 515140),
        _geoname_line(2, "Hamburg", "Hamburg", 53.55073, 9.99302, "PPLA", "DE", 1973896),
        _geoname_line(3, "Hameln", "Hameln", 52.10397, 9.35623, "PPL", "DE", 58666),
        _geoname_line(4, "Hamm", "Hamm", 51.68033, 7.82089, "PPL", "DE", 178967),
        _geoname_line(5, "Hanoi", "Hanoi", 21.02888, 105.85416, "PPLC", "VN", 8053663),
    ])
    names = [h["name"] for h in index.search("ha", near=(52.37, 9.73), home_iso="DE")]
    # The city under the cursor first; the megacity one state over must beat
    # the 57k town next door (the size-scaled proximity term) — and every
    # German hit outranks the Asian megacity.
    assert names[0] == "Hannover"
    assert names[1] == "Hamburg"
    assert names.index("Hamburg") < names.index("Hameln")
    assert names.index("Hamm") < names.index("Hanoi")


def test_search_population_orders_peers_from_far_away(tmp_path):
    index = _search_index(tmp_path, [
        _geoname_line(1, "Hamburg", "Hamburg", 53.55073, 9.99302, "PPLA", "DE", 1973896),
        _geoname_line(2, "Hamm", "Hamm", 51.68033, 7.82089, "PPL", "DE", 178967),
        _geoname_line(3, "Hanoi", "Hanoi", 21.02888, 105.85416, "PPLC", "VN", 8053663),
    ])
    names = [h["name"] for h in index.search("ha", near=(0.0, -30.0))]
    assert names.index("Hanoi") < names.index("Hamburg") < names.index("Hamm")


def test_search_matches_local_ascii_and_stripped_spellings(tmp_path):
    index = _search_index(tmp_path, [
        _geoname_line(1, "Köln", "Koeln", 50.93333, 6.95, "PPLA2", "DE", 1024621),
    ])
    for query in ("köln", "koeln", "koln"):
        assert [h["name"] for h in index.search(query)] == ["Köln"], query
    assert index.search("") == []
    assert index.search("x") == []


# -- REST wiring: prefer GeoNames, fall back to Natural Earth -----------------

# A ~180 km south-north corridor; GeoNames-only towns beside it.
_START, _END = (48.5, 9.0), (50.1, 8.7)


def _osrm_transport() -> httpx.MockTransport:
    line = [
        [_START[1] + (_END[1] - _START[1]) * i / 49,
         _START[0] + (_END[0] - _START[0]) * i / 49]
        for i in range(50)
    ]
    def handler(request):
        return httpx.Response(200, json={"code": "Ok", "routes": [{
            "duration": 7200.0, "distance": 180_000.0,
            "geometry": {"coordinates": line},
            "legs": [{"duration": 7200.0, "distance": 180_000.0, "steps": []}],
        }]})
    return httpx.MockTransport(handler)


def _api(tmp_path) -> MapsApi:
    bundles = tmp_path / "bundles"
    bundles.mkdir(exist_ok=True)
    return MapsApi(
        bundles,
        sources=builtin_sources(), composites=builtin_composites(),
        router=OsrmRouter("https://osrm.test", transport=_osrm_transport()),
        remote_planet=False,
    )


def _ne_cities_geojson(entries) -> str:
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"name": name, "adm0name": "Germany", "labelrank": 6,
                        "pop_max": pop, "featurecla": "Populated place"},
         "geometry": {"type": "Point", "coordinates": [lon, lat]}}
        for name, lat, lon, pop in entries
    ]})


def test_route_prefers_geonames_index_over_natural_earth(tmp_path):
    geo = tmp_path / "geo"
    geo.mkdir()
    # NE knows only a town OFF the corridor; GeoNames knows the one ON it.
    (geo / "cities.geojson").write_text(
        _ne_cities_geojson([("Abseits", 49.3, 10.5, 800_000)]))
    _prep_tool().build(
        _geoname_line(1, "Korridorstadt", "Korridorstadt", 49.30, 8.88, "PPL", "DE", 120_000)
        + "\n",
        geo / "places-geonames.tsv.gz")
    app = FastAPI()
    _api(tmp_path).mount(app)
    body = TestClient(app).post("/api/v1/maps/route", json={
        "route_id": "r1",
        "start": {"lat": _START[0], "lon": _START[1]},
        "end": {"lat": _END[0], "lon": _END[1]},
    }).json()
    assert [p["name"] for p in body["via_places"]] == ["Korridorstadt"]


def test_route_falls_back_to_natural_earth_without_asset(tmp_path):
    geo = tmp_path / "geo"
    geo.mkdir()
    (geo / "cities.geojson").write_text(
        _ne_cities_geojson([("NE-Stadt", 49.30, 8.88, 120_000)]))
    app = FastAPI()
    _api(tmp_path).mount(app)
    body = TestClient(app).post("/api/v1/maps/route", json={
        "route_id": "r1",
        "start": {"lat": _START[0], "lon": _START[1]},
        "end": {"lat": _END[0], "lon": _END[1]},
    }).json()
    assert [p["name"] for p in body["via_places"]] == ["NE-Stadt"]


def test_search_endpoint_serves_geonames_with_display_country(tmp_path):
    geo = tmp_path / "geo"
    geo.mkdir()
    _prep_tool().build(
        _geoname_line(1, "Pforzheim", "Pforzheim", 48.88436, 8.69892, "PPLA3", "DE", 119313)
        + "\n",
        geo / "places-geonames.tsv.gz")
    app = FastAPI()
    _api(tmp_path).mount(app)
    hits = TestClient(app).get(
        "/api/v1/maps/geo/cities/search",
        params={"q": "pforz", "near_lat": 48.5, "near_lon": 9.0}).json()
    assert [h["name"] for h in hits] == ["Pforzheim"]
    # No boundary file in this tmp dir → the ISO code passes through rather
    # than failing; with countries.geojson present it becomes "Germany".
    assert hits[0]["country"] == "DE"
    assert hits[0]["distance_km"] is not None
