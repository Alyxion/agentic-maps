"""SessionRecorder: what it records and how it keys a routing request."""

import json

import pytest

from agentic_maps.seal.recorder import SessionRecorder, route_key


# -- request keys -----------------------------------------------------------


def test_route_key_is_printed_the_same_way_javascript_prints_it():
    """`web/sealed-runtime.js` rebuilds this key in JS, so the format must be
    portable: fixed-precision strings and sorted keys, never bare floats —
    `repr(53.6003)` and JavaScript's `String(53.6003)` are not required to
    agree, and a key that differs by one digit finds no route at all."""
    key = route_key({"route_id": "a24", "mode": "car",
                     "start": {"lat": 53.6003, "lon": 10.0689},
                     "end": {"lat": 53.56291, "lon": 10.15277}})
    parsed = json.loads(key)
    assert parsed["start"] == ["53.600300", "10.068900"]
    assert list(parsed) == sorted(parsed)                 # stable ordering
    assert "route_id" not in parsed                       # naming is not identity


def test_route_key_defaults_mode_to_car_not_the_reference_products_drive():
    """agentic-maps' own canonical default (`routing/base.py`,
    `RouteRequest.mode`) is "car" — not the legacy reference's
    "drive", which was never this product's vocabulary."""
    with_mode = route_key({"mode": "car", "start": {}, "end": {}})
    without_mode = route_key({"start": {}, "end": {}})
    assert with_mode == without_mode
    assert json.loads(without_mode)["mode"] == "car"


def test_route_key_ignores_the_name_but_not_the_geometry_or_mode_or_alternates():
    base = {"start": {"lat": 1.0, "lon": 2.0}, "end": {"lat": 3.0, "lon": 4.0}}
    assert route_key({**base, "route_id": "a"}) == route_key({**base, "route_id": "b"})
    assert route_key(base) != route_key({**base, "mode": "walk"})
    assert route_key(base) != route_key({**base, "via": [{"lat": 9.0, "lon": 9.0}]})
    assert route_key(base) != route_key({**base, "alternates": 2})


@pytest.mark.parametrize("path,expected", [
    ("/api/v1/maps/live/de-dop/14/8600/5300", "/api/v1/maps/live/de-dop/14/8600/5300"),
    ("/api/v1/maps/vector/auto/tiles/12/2148/1338.mvt", "/api/v1/maps/vector/auto/tiles/12/2148/1338.mvt"),
    ("/api/v1/maps/assets/glyphs/Noto/0-255.pbf", "/api/v1/maps/assets/glyphs/Noto/0-255.pbf"),
    ("/api/v1/maps/geo/countries", "/api/v1/maps/geo/countries"),
    # Answered from the sealed payload/route store, so recording them would
    # be dead weight or (for routing) is keyed by body, not by path.
    ("/api/v1/maps/attribution?src=de-dop&z=12", None),
    ("/api/v1/maps/mode", None),
    ("/api/v1/maps/route", None),
    ("/favicon.ico", None),
])
def test_only_resources_a_sealed_page_must_answer_are_recorded(path, expected):
    recorder = SessionRecorder("http://host:8091")
    assert recorder._key_of("http://host:8091" + path) == expected


def test_asset_prefixes_default_to_none_and_must_be_opted_into():
    recorder = SessionRecorder("http://host:8091")
    assert recorder._key_of("http://host:8091/_stage/icons/regular/drone.svg") is None

    with_prefix = SessionRecorder("http://host:8091", asset_prefixes=("/_stage/icons/",))
    assert (with_prefix._key_of("http://host:8091/_stage/icons/regular/drone.svg")
            == "/_stage/icons/regular/drone.svg")


def test_requests_to_another_host_are_never_recorded():
    recorder = SessionRecorder("http://host:8091")
    assert recorder._key_of("https://tile.example.com/1/2/3.png") is None


def test_map_prefix_is_configurable_to_match_a_custom_mount_point():
    recorder = SessionRecorder("http://host:8091", map_prefix="/custom/")
    assert recorder._key_of("http://host:8091/custom/live/de-dop/1/2/3") == "/custom/live/de-dop/1/2/3"
    assert recorder._key_of("http://host:8091/api/v1/maps/live/de-dop/1/2/3") is None
