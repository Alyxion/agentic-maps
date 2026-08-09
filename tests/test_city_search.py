"""Search ranks like a person standing on the map, not like a global index.

The bug this pins: typing "ha" in Hannover surfaced Haiti and Hamadan, because
countries were pinned above places and Nominatim ranks by worldwide
importance. The city index scores population + proximity + home country.
"""

from pathlib import Path

from agentic_maps.geo.countries import CityIndex


def _index(features):
    index = CityIndex(Path("/nonexistent"))
    index._collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"name": name, "country": country, "population": pop},
         "geometry": {"type": "Point", "coordinates": [lon, lat]}}
        for name, country, pop, lat, lon in features
    ]}
    return index


CITIES = [
    ("Hanover", "Germany", 722_490, 52.374, 9.739),
    ("Hamburg", "Germany", 1_757_000, 53.552, 9.998),
    ("Hamm", "Germany", 166_000, 51.681, 7.820),
    ("Hanoi", "Vietnam", 4_378_000, 21.035, 105.848),
    ("Harare", "Zimbabwe", 1_572_000, -17.829, 31.054),
    ("Lyon", "France", 1_423_000, 45.764, 4.836),
]


def test_nearby_home_city_beats_bigger_faraway_ones():
    hits = _index(CITIES).search("ha", near=(52.37, 9.73), home_country="Germany")
    names = [h["name"] for h in hits]
    # The city under the cursor first, the big neighbour second; the Asian
    # megacities exist but sit below every German hit.
    assert names[0] == "Hanover"
    assert names[1] == "Hamburg"
    assert names.index("Hamburg") < names.index("Hamm")
    assert names.index("Hamm") < names.index("Hanoi")


def test_population_orders_peers_at_equal_distance():
    # From far away (no proximity signal, no home country), size decides.
    hits = _index(CITIES).search("ha", near=(0.0, -30.0), home_country="")
    names = [h["name"] for h in hits]
    assert names.index("Hanoi") < names.index("Hamburg") < names.index("Hamm")


def test_prefix_only_no_substring_hits():
    assert all(h["name"].lower().startswith("am") is False
               for h in _index(CITIES).search("am"))


def test_without_near_still_answers():
    hits = _index(CITIES).search("ham")
    assert {h["name"] for h in hits} == {"Hamburg", "Hamm"}
    assert hits[0]["distance_km"] is None
