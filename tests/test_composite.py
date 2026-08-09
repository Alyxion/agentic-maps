import asyncio
import io

import httpx
import pytest
from PIL import Image

from agentic_maps.harvest.harvester import (
    BLANK_TILE_MAX_BYTES,
    MAX_MOSAIC_MEMBERS,
    Harvester,
    NoImagery,
)
from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.sources.presets import builtin_composites, builtin_sources

_BLANK = b"\xff\xd8\xff\xe0" + b"\x00" * 200          # WMS "outside my state" answer
_IMAGE = b"\xff\xd8\xff\xe0" + b"\x11" * (BLANK_TILE_MAX_BYTES + 500)


def _png(colour, *, left_half_only: bool = False) -> bytes:
    """A 256px RGBA tile that passes for real imagery.

    Noise is not decoration: a tile that quantises to a few flat colours is
    treated as a service placeholder, so a test fixture must look photographic.
    `left_half_only` leaves the right half transparent, which is what a WMS
    returns for a tile straddling its state border.
    """
    base = Image.new("RGB", (256, 256), colour[:3])
    image = Image.blend(base, Image.effect_noise((256, 256), 28).convert("RGB"), 0.15)
    image = image.convert("RGBA")
    if colour[3] == 0:
        image.putalpha(0)
    if left_half_only:
        image.paste((0, 0, 0, 0), (128, 0, 256, 256))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _png_response(colour, **kwargs):
    return httpx.Response(200, content=_png(colour, **kwargs), headers={"content-type": "image/png"})


def _api(tmp_path):
    return MapsApi(tmp_path, sources=builtin_sources(), composites=builtin_composites())


def test_composite_routes_tiles_to_state_sources(tmp_path):
    api = _api(tmp_path)
    composite = api.composites["de-dop"]

    cases = {
        "bw-dop20": (48.5386, 9.2925),      # Metzingen
        "by-dop20": (48.1374, 11.5755),     # München
        "berlin-dop20": (52.5163, 13.3777), # Berlin — inside Brandenburg's bbox, wins on specificity
        "st-dop20": (52.1315, 11.6295),     # Magdeburg
        "he-dop20": (50.1109, 8.6821),      # Frankfurt
        "hb-dop10": (53.0793, 8.8017),      # Bremen — inside Niedersachsen's bbox
        "sn-dop20": (51.0504, 13.7373),     # Dresden
        "sl-dop20": (49.2354, 6.9969),      # Saarbrücken
    }
    for source_id, (lat, lon) in cases.items():
        assert api._resolve_member(composite, TileCoord.at(lat, lon, 14)).id == source_id

    # Outside Germany nobody bids and the basemap shows through.
    assert api._resolve_member(composite, TileCoord.at(47.3769, 8.5417, 14)) is None  # Zürich


def test_zoom_decides_which_imagery_answers(tmp_path):
    """Regional zooms get the seamless Sentinel-2 mosaic; only city zooms pay
    for 20 cm state imagery, whose per-state flights would otherwise show up as
    a patchwork of greens across a country-wide view."""
    api = _api(tmp_path)
    composite = api.composites["de-dop"]

    # Below z8 nobody bids: Blue Marble carries the continental views on its
    # own, so Sentinel's coverage rectangle cannot cut a bright edge across one.
    for z in (5, 7):
        assert api._resolve_members(composite, TileCoord.at(50.3, 10.2, z)) == [], f"z{z}"

    for z in (8, 10, 12):
        ids = [m.id for m in api._resolve_members(composite, TileCoord.at(50.3, 10.2, z))]
        assert ids == ["sen2-europe"], f"z{z}"

    ids = [m.id for m in api._resolve_members(composite, TileCoord.at(50.3, 10.2, 13))]
    assert "sen2-europe" not in ids
    assert {"th-dop20", "by-dop20"} <= set(ids)
    # Most specific first, so the smallest coverage lands on top of the stack.
    assert ids == sorted(ids, key=lambda i: api.sources[i].coverage.area_deg2)


def test_wide_tile_collects_every_state_it_touches(tmp_path):
    """Where state imagery does answer, a tile spanning several of them must
    collect all — otherwise one state's opaque background paints over its
    neighbours."""
    api = _api(tmp_path)
    ids = [m.id for m in api._resolve_members(api.composites["de-dop"], TileCoord.at(50.3, 10.2, 13))]
    assert {"th-dop20", "by-dop20", "he-dop20"} <= set(ids)


def _fetch(harvester, coord, handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await harvester.fetch_tile(client, coord)

    return asyncio.run(run())


def test_mosaic_composites_states_most_specific_on_top(tmp_path):
    api = _api(tmp_path)
    coord = TileCoord.at(52.5163, 13.3777, 14)  # Berlin inside Brandenburg
    members = api._resolve_members(api.composites["de-dop"], coord)
    assert [m.id for m in members][:2] == ["berlin-dop20", "bb-dop20"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "TRANSPARENT=TRUE" in str(request.url)
        if "gdi.berlin.de" in str(request.url):
            return _png_response((255, 0, 0, 255))       # Berlin: red, on top
        return _png_response((0, 0, 255, 255))           # everyone else: blue

    data = _fetch(Harvester(members[0], alternates=members[1:], mosaic=True), coord, handler)
    assert data.startswith(b"\xff\xd8")  # fully covered -> JPEG keeps packages small
    merged = Image.open(io.BytesIO(data)).convert("RGB")
    assert merged.getpixel((128, 128))[0] > 200  # Berlin won the overlap


def test_mosaic_keeps_alpha_where_no_state_covers(tmp_path):
    api = _api(tmp_path)
    coord = TileCoord.at(54.5, 9.5, 13)  # Schleswig-Holstein: half of it is sea/Denmark
    members = api._resolve_members(api.composites["de-dop"], coord)

    def handler(request: httpx.Request) -> httpx.Response:
        return _png_response((0, 128, 0, 255), left_half_only=True)

    data = _fetch(Harvester(members[0], alternates=members[1:], mosaic=True), coord, handler)
    assert data.startswith(b"\x89PNG")  # partial cover -> PNG, basemap shows through
    assert Image.open(io.BytesIO(data)).convert("RGBA").getpixel((200, 128))[3] == 0


def test_one_tile_never_costs_more_than_four_fetches(tmp_path):
    """The 2x2 rule: a displayed tile is assembled from at most four sources,
    and from exactly one when the most specific already covers it."""
    api = _api(tmp_path)
    # Where SH, MV, BB and NI rectangles all overlap — the busiest spot in the
    # federation, and exactly the situation the cap exists for.
    coord = TileCoord.at(53.5, 11.4, 13)
    members = api._resolve_members(api.composites["de-dop"], coord)
    assert len(members) >= MAX_MOSAIC_MEMBERS

    served = []

    def opaque(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        return _png_response((70, 90, 60, 255))

    _fetch(Harvester(members[0], alternates=members[1:], mosaic=True), coord, opaque)
    assert len(served) == 1, "an opaque top layer makes every fetch below it waste"

    served.clear()

    def transparent(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        return _png_response((70, 90, 60, 255), left_half_only=True)

    _fetch(Harvester(members[0], alternates=members[1:], mosaic=True), coord, transparent)
    assert len(served) == MAX_MOSAIC_MEMBERS == 4


def test_all_transparent_members_report_no_imagery(tmp_path):
    api = _api(tmp_path)
    coord = TileCoord.at(53.5511, 9.9937, 14)  # Hamburg: SH/NI bid, neither has HH imagery
    members = api._resolve_members(api.composites["de-dop"], coord)
    assert len(members) >= 2

    def handler(request: httpx.Request) -> httpx.Response:
        return _png_response((0, 0, 0, 0))

    with pytest.raises(NoImagery):
        _fetch(Harvester(members[0], alternates=members[1:], mosaic=True), coord, handler)


def test_single_state_blank_answer_reports_no_imagery(tmp_path):
    """Without a second candidate there is no mosaic, so the byte-size
    heuristic is what separates a placeholder from real ground."""
    api = _api(tmp_path)
    source = api.sources["mv-dop20"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_BLANK, headers={"content-type": "image/jpeg"})

    with pytest.raises(NoImagery):
        _fetch(Harvester(source), TileCoord.at(54.09, 12.13, 13), handler)

    def real(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_IMAGE, headers={"content-type": "image/jpeg"})

    assert _fetch(Harvester(source), TileCoord.at(54.09, 12.13, 14), real) == _IMAGE
