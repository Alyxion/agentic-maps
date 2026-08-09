"""PageSealer: decomposing a page under web/ into a SealedWeb."""

from pathlib import Path

import pytest

from agentic_maps.seal.page_seal import PageSealer

_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


# -- the view must be known before the map is built -----------------------


def test_view_html_declares_its_view_before_mounting_map_js():
    """`data-agentic-view` has to be set before `map.js` runs, not after.

    The style builder adds the imagery layers only for hybrid/satellite. A
    map constructed hybrid and *then* told it is a street map has already
    built those layers and asked for aerial tiles — the viewer sees a blink
    of orthophoto on a pure-cartography page, and the credit line briefly
    names imagery rights that are not on screen. Ordering is the whole fix,
    so ordering is what this asserts.
    """
    web = PageSealer(_WEB_ROOT).seal(["view.html"])
    scripts = web.pages["view.html"].scripts

    sets_view = [i for i, (kind, value) in enumerate(scripts)
                 if kind == "code" and "data-agentic-view" in value]
    mounts = [i for i, (kind, value) in enumerate(scripts)
              if kind == "lib" and value == "map.js"]

    assert sets_view, "view.html never declares data-agentic-view"
    assert mounts, "view.html does not load map.js"
    assert min(sets_view) < min(mounts), (
        "view.html sets data-agentic-view after map.js has already built the map")


def test_view_html_decomposes_into_body_styles_and_scripts_with_no_network_call():
    """A smoke test that the real page (not a fixture) survives decomposition
    without raising and produces the shape sealed-host.js expects."""
    web = PageSealer(_WEB_ROOT).seal(["view.html"])
    page = web.pages["view.html"]

    assert 'data-agentic-map' in page.body
    assert "<script" not in page.body           # scripts are replayed, not left in the body
    assert any(kind == "lib" and ref == "map.js" for kind, ref in page.scripts)
    assert "map.js" in web.libraries
    assert "vendor/maplibre-gl.js" in web.libraries
    assert "vendor/maplibre-gl.css" in web.stylesheets


# -- page decomposition, generic fixtures ----------------------------------


def test_pages_share_their_libraries(tmp_path):
    """MapLibre is a megabyte; a session with several map embeds must carry
    it once."""
    (tmp_path / "lib.js").write_text("window.LIB=1;")
    (tmp_path / "a.css").write_text("body{color:red}")
    for name in ("one.html", "two.html"):
        (tmp_path / name).write_text(
            '<html><head><link rel="stylesheet" href="./a.css"></head><body>'
            '<div id="map"></div><script src="./lib.js"></script>'
            '<script>var q=new URLSearchParams(location.search);</script>'
            "</body></html>")

    web = PageSealer(tmp_path).seal(["one.html", "two.html"])

    assert list(web.libraries) == ["lib.js"]
    assert list(web.stylesheets) == ["a.css"]
    assert web.pages["one.html"].scripts[0] == ("lib", "lib.js")
    # A srcdoc frame has no query string of its own, so the page is handed one.
    assert "window.__agenticEmbedParams" in web.pages["one.html"].scripts[1][1]
    assert "location.search" not in web.pages["one.html"].scripts[1][1]
    # Scripts are replayed by the host, so they must not also sit in the body.
    assert "<script" not in web.pages["one.html"].body
    assert 'id="map"' in web.pages["one.html"].body


def test_seal_is_idempotent_over_duplicate_page_names(tmp_path):
    (tmp_path / "one.html").write_text("<html><body><div id='map'></div></body></html>")
    web = PageSealer(tmp_path).seal(["one.html", "one.html"])
    assert list(web.pages) == ["one.html"]


def test_extra_libraries_are_pulled_in_even_without_a_page_referencing_them(tmp_path):
    (tmp_path / "shared.js").write_text("window.SHARED=1;")
    (tmp_path / "one.html").write_text("<html><body></body></html>")
    web = PageSealer(tmp_path).seal(["one.html"], extra_libraries=["shared.js"])
    assert web.libraries["shared.js"] == "window.SHARED=1;"


@pytest.mark.parametrize("missing", ["nope.html"])
def test_a_missing_page_raises_rather_than_silently_skipping(tmp_path, missing):
    with pytest.raises(FileNotFoundError):
        PageSealer(tmp_path).seal([missing])
