"""Sealer: what travels into a SealedBundle, and how it is packed/rewired."""

import asyncio
import base64
import json

from agentic_maps.models.sealed_bundle import SealedBundle
from agentic_maps.seal.sealer import Sealer, _footprint, sealed_url, trim_worldwide


# -- payload rewiring ---------------------------------------------------


def test_payload_is_pointed_at_the_store_and_told_it_is_offline():
    sealer = Sealer("http://host:8091")
    sealed = sealer._seal_payload({
        "tiles_url_template": "/api/v1/maps/live/de-dop/{z}/{x}/{y}",
        "vector_url_template": "/api/v1/maps/vector/auto/tiles/{z}/{x}/{y}.mvt",
        "glyphs_url_template": "/api/v1/maps/assets/glyphs/{fontstack}/{range}.pbf",
        "sprites_base_url": "/api/v1/maps/assets/sprites/v4",
        "offline": False,
    }, "© Somebody | © OpenStreetMap", {8: "NASA Blue Marble", 16: "© Somebody"})

    assert sealed["tiles_url_template"] == "amap://api/v1/maps/live/de-dop/{z}/{x}/{y}"
    assert sealed["sprites_base_url"] == "amap://api/v1/maps/assets/sprites/v4"
    # The runtime stops asking the federation what is visible when this is
    # set; the answer it would have asked for travels as a per-zoom table.
    assert sealed["offline"] is True
    assert sealed["standalone"] is False
    # JSON object keys are strings — the runtime parses them back.
    assert sealed["attribution_zooms"] == {"8": "NASA Blue Marble", "16": "© Somebody"}


def test_the_credit_is_sealed_per_zoom_because_the_imagery_ladder_is_banded():
    """One line for a whole session names a global provider over a rooftop.

    A world basemap carries the globe, a regional mosaic the mid band, a
    local survey the city zooms. A session that flies between them has
    different rights-holders on screen at different moments, and the live
    map credits them per view — the sealed one has to as well.
    """
    calls = []

    class Response:
        status_code = 200

        def __init__(self, zoom):
            self._zoom = zoom

        def json(self):
            name = ("NASA Blue Marble" if self._zoom <= 7
                    else "Copernicus Sentinel-2" if self._zoom <= 12
                    else "© Freie und Hansestadt Hamburg, LGV")
            return {"sources": [{"attribution": name}]}

    class Client:
        async def get(self, url, params=None):
            calls.append(params["z"])
            return Response(params["z"])

    keys = ["/api/v1/maps/live/de-dop/6/33/20",
            "/api/v1/maps/live/de-dop/11/1075/659",
            "/api/v1/maps/live/de-dop/17/68901/42213"]
    table = asyncio.run(Sealer("http://host")._attribution_by_zoom(Client(), keys))

    assert table == {6: "NASA Blue Marble",
                     11: "Copernicus Sentinel-2",
                     17: "© Freie und Hansestadt Hamburg, LGV"}
    assert sorted(calls) == [6, 11, 17]


def test_sealed_url_keeps_the_path_as_the_store_key():
    assert sealed_url("/api/v1/maps/live/x/1/2/3") == "amap://api/v1/maps/live/x/1/2/3"


def test_sealed_url_scheme_is_configurable():
    assert sealed_url("/x", scheme="custom") == "custom://x"


# -- packing --------------------------------------------------------------


def test_pack_addresses_every_resource_by_offset():
    bundle = SealedBundle()
    Sealer("http://host")._pack(bundle, [
        ("/a", b"aaaa", "image/jpeg"),
        ("/b", b"bb", "image/jpeg"),
        ("/c", b"ccc", "application/x-protobuf"),
    ])

    blob = base64.b64decode(bundle.data)
    for key, expected in (("/a", b"aaaa"), ("/b", b"bb"), ("/c", b"ccc")):
        offset, length, media_id = bundle.index[key]
        assert blob[offset:offset + length] == expected
        assert bundle.media_types[media_id] in ("image/jpeg", "application/x-protobuf")
    assert len(blob) == 9                     # concatenated, no padding between
    assert bundle.byte_size == 9


# -- worldwide layers, cut to the session's world --------------------------


def test_footprint_ignores_continent_wide_tiles():
    """A z2 tile spans a hemisphere and says nothing about where a session looks."""
    box = _footprint([
        "/api/v1/maps/live/de-dop/2/2/1",                  # ignored: too coarse
        "/api/v1/maps/vector/auto/tiles/12/2148/1338.mvt",  # Hamburg
    ])
    assert box is not None
    west, south, east, north = box
    # The z12 tile sits at ~8.8°E / ~53.8°N; the box is that, grown by the
    # margin. Had the z2 tile counted, west would be near the Atlantic.
    assert -4.0 < west < -2.0
    assert 53.0 + 12 - 1 < north < 53.0 + 12 + 2
    assert east - west < 30.0                # one city plus context, not a continent


def test_a_session_about_one_city_does_not_ship_the_whole_planet():
    """Natural Earth is several MB of borders; nearly all of it is off screen."""
    world = {"type": "FeatureCollection", "features": [
        {"properties": {"name": "Germany"},
         "geometry": {"type": "Polygon", "coordinates": [[[6.0, 47.0], [15.0, 47.0],
                                                          [15.0, 55.0], [6.0, 55.0]]]}},
        {"properties": {"name": "New Zealand"},
         "geometry": {"type": "Polygon", "coordinates": [[[166.0, -47.0], [178.0, -47.0],
                                                          [178.0, -34.0], [166.0, -34.0]]]}},
    ]}
    key, data, media = trim_worldwide(
        ("/api/v1/maps/geo/countries", json.dumps(world).encode(), "application/geo+json"),
        ["/api/v1/maps/live/de-dop/12/2148/1338"])

    names = [f["properties"]["name"] for f in json.loads(data)["features"]]
    assert names == ["Germany"]


def test_a_layer_that_is_not_worldwide_json_is_left_alone():
    item = ("/api/v1/maps/live/de-dop/12/1/1", b"\xff\xd8jpegbytes", "image/jpeg")
    assert trim_worldwide(item, ["/api/v1/maps/live/de-dop/12/1/1"]) == item


def test_bundle_json_round_trips_through_model_dump():
    from agentic_maps.seal.sealer import bundle_json

    bundle = SealedBundle(attribution="© Test")
    text = bundle_json(bundle)
    assert json.loads(text)["attribution"] == "© Test"
    assert '": "' not in text and ", " not in text   # compact, no JSON whitespace padding
