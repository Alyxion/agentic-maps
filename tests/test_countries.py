import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.geo.countries import CountryIndex
from agentic_maps.rest.maps_api import MapsApi

# Two toy countries: one with many language names, one minimal.
_RAW = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "ISO_A2": "DE", "NAME": "Germany", "CONTINENT": "Europe",
                "NAME_DE": "Deutschland", "NAME_EN": "Germany", "NAME_FR": "Allemagne",
                "NAME_ES": "Alemania", "NAME_JA": "ドイツ", "NAME_RU": "Германия",
            },
            "geometry": {"type": "MultiPolygon", "coordinates": [
                # Mainland (many vertices) plus a far-away speck that must not
                # drag the framing out to sea.
                [[[6.0, 47.5], [15.0, 47.5], [15.0, 55.0], [6.0, 55.0], [10.0, 51.0], [6.0, 47.5]]],
                [[[-60.0, 5.0], [-59.0, 5.0], [-59.0, 6.0], [-60.0, 5.0]]],
            ]},
        },
        {
            "type": "Feature",
            "properties": {"ISO_A2": "IT", "NAME": "Italy", "CONTINENT": "Europe", "NAME_IT": "Italia"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[6.6, 36.6], [18.5, 36.6], [18.5, 47.1], [6.6, 47.1], [6.6, 36.6]]
            ]},
        },
    ],
}


@pytest.fixture
def index(tmp_path):
    path = tmp_path / "countries.geojson"
    path.write_text(json.dumps(_RAW))
    return CountryIndex(path)


def test_search_matches_any_language(index):
    for query in ("Deutschland", "Germany", "Allemagne", "Alemania", "ドイツ", "Германия", "de"):
        hits = index.search(query, limit=1)
        assert hits and hits[0].iso == "DE", query


def test_results_are_labelled_in_the_requested_language(index):
    assert index.search("Germany", lang="ja")[0].label == "ドイツ"
    assert index.search("ドイツ", lang="fr")[0].label == "Allemagne"
    # Falls back to English when a language is missing for that country.
    assert index.search("Italia", lang="ja")[0].label == "Italy"


def test_framing_uses_the_mainland_not_an_overseas_speck(index):
    box = index.search("Deutschland")[0].bbox
    assert box.west == pytest.approx(6.0) and box.east == pytest.approx(15.0)


def test_prefix_beats_substring(index):
    assert index.search("Ital")[0].iso == "IT"


def test_collection_carries_one_property_per_language(index):
    feature = next(f for f in index.collection()["features"] if f["properties"]["iso"] == "DE")
    assert feature["properties"]["name_ja"] == "ドイツ"
    assert feature["properties"]["name_de"] == "Deutschland"
    assert feature["geometry"]["type"] == "MultiPolygon"


def test_endpoints_serve_borders_and_search(tmp_path, index):
    api = MapsApi(tmp_path, sources={}, composites={}, countries=index)
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)

    borders = client.get("/api/v1/maps/geo/countries")
    assert borders.status_code == 200
    assert borders.headers["content-type"].startswith("application/geo+json")
    assert len(borders.json()["features"]) == 2

    found = client.get("/api/v1/maps/geo/countries/search", params={"q": "Allemagne", "lang": "en"})
    assert found.json()[0]["label"] == "Germany"


def test_search_is_available_offline(tmp_path, index):
    """Country lookup must keep working in presentation mode — it is local
    data, unlike the geocoder."""
    api = MapsApi(tmp_path, sources={}, composites={}, countries=index, mode="offline")
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)
    assert client.get("/api/v1/maps/geo/countries/search", params={"q": "Deutschland"}).json()
    assert client.get("/api/v1/maps/geo/countries").status_code == 200
