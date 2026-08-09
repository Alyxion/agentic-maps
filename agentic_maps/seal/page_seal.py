"""Take an embeddable map page apart so a sealed host can rebuild it.

`web/view.html` (and any other page under `web/` that mounts `map.js`) is an
ordinary page: a few stylesheets, a body, a run of scripts. Served by a host
it works; inside a self-contained sealed file there is no host, and the frame
that shows it is a `srcdoc` document `web/sealed-host.js` writes itself.

So the page is decomposed rather than copied. Its own styles and body travel
per page; its libraries (MapLibre alone is a megabyte) are lifted out and
shared, because a sealed file with several map embeds must carry them once.

Two rewrites happen here, both because a `srcdoc` frame has no URL of its own:

- `location.search` becomes an injected parameter string. The frame's
  location is `about:srcdoc`; the query a live embed would have received
  lives wherever the sealed host decided to put it, and the sealed runtime
  hands it over as `window.__agenticEmbedParams`.
- relative asset URLs (`./vendor/...`) would resolve against the sealed
  file's own location, so nothing may keep one — hence the id-based
  references (`web.libraries`/`web.stylesheets`).
"""

import re
from pathlib import Path

from ..models.sealed_page import SealedPage
from ..models.sealed_web import SealedWeb

_LINK = re.compile(r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',
                   re.IGNORECASE)
_SCRIPT = re.compile(r'<script([^>]*)>(.*?)</script>', re.IGNORECASE | re.DOTALL)
_SRC = re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE)
_STYLE = re.compile(r'<style[^>]*>(.*?)</style>', re.IGNORECASE | re.DOTALL)
_BODY = re.compile(r'<body[^>]*>(.*?)</body>', re.IGNORECASE | re.DOTALL)

# The one construct these pages assume a real URL for. Rewritten rather than
# refactored away, because reading the query string is exactly right when the
# page is served normally — it is only the srcdoc frame that has no query.
_LOCATION_SEARCH = re.compile(r'location\.search\b')


class PageSealer:
    """Decomposes pages under `web/` into a `SealedWeb`."""

    def __init__(self, web_root: Path):
        self.web_root = Path(web_root)

    def seal(self, page_names: list[str], *, extra_libraries: list[str] = ()) -> SealedWeb:
        web = SealedWeb()
        for name in dict.fromkeys(page_names):
            web.pages[name] = self._page(name, web)
        for name in extra_libraries:
            self._library(name, web)
        return web

    # -- one page ---------------------------------------------------------

    def _page(self, name: str, web: SealedWeb) -> SealedPage:
        source = (self.web_root / name).read_text(encoding="utf-8")
        page = SealedPage(name=name)

        for href in _LINK.findall(source):
            page.style_refs.append(self._stylesheet(href, web))
        page.styles = [block.strip() for block in _STYLE.findall(source)]

        body = _BODY.search(source)
        body_html = body.group(1) if body else source
        # Scripts are pulled out of the body and replayed in order; leaving
        # the tags in would make the frame try to run them a second time.
        page.scripts = self._scripts(body_html, web)
        page.body = _SCRIPT.sub("", body_html).strip()
        return page

    def _scripts(self, body_html: str, web: SealedWeb) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for attrs, code in _SCRIPT.findall(body_html):
            src = _SRC.search(attrs)
            if src is not None:
                out.append(("lib", self._library(src.group(1), web)))
            elif code.strip():
                out.append(("code", _LOCATION_SEARCH.sub(
                    "(window.__agenticEmbedParams || '')", code)))
        return out

    # -- shared assets ----------------------------------------------------

    def _library(self, ref: str, web: SealedWeb) -> str:
        key = _asset_id(ref)
        if key not in web.libraries:
            web.libraries[key] = (self.web_root / _relative(ref)).read_text(encoding="utf-8")
        return key

    def _stylesheet(self, ref: str, web: SealedWeb) -> str:
        key = _asset_id(ref)
        if key not in web.stylesheets:
            web.stylesheets[key] = (self.web_root / _relative(ref)).read_text(encoding="utf-8")
        return key


def _relative(ref: str) -> str:
    return ref.lstrip("./")


def _asset_id(ref: str) -> str:
    """`./vendor/maplibre-gl.js` -> `vendor/maplibre-gl.js`.

    The directory stays in the id: `map.css` and a future `vendor/map.css`
    must not collide into one entry and silently serve the wrong bytes.
    """
    return _relative(ref)
