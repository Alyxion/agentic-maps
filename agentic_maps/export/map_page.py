"""One self-contained HTML document showing a MapSpec professionally.

Why this exists: an MCP client that only gets raw geometry rebuilds the
display itself — the observed failure mode was an ad-hoc Leaflet page pulling
tile.openstreetmap.org (against the OSMF tile-usage policy) with broken
marker placeholders and no attribution. This module emits the display
INSTEAD: our design language, the route(s) drawn with clickable alternates,
the turn-by-turn list, and a mandatory attribution footer baked into the
document. There is no code path that returns the HTML without the credits.

Hard rules:
- ZERO third-party references, ever. Icons/glyphs are inline SVG; fonts are
  system stacks; scripts and styles are inline.
- Two basemap tiers, stated honestly:
  (a) `base_url` given — the document references THAT server's own tile
      endpoints (`/api/v1/maps/live/...`) for a real basemap and silently
      degrades (per-tile `onerror`) to tier (b) when unreachable;
  (b) `base_url=None` — fully self-contained: styled neutral canvas with
      graticule and scale bar under the route. Never blank, never external.
- Alternates are first-class: full geometry + steps for every candidate are
  embedded, and ~40 lines of dependency-free inline JS swap primary/alternate
  styling and the steps list on click — works from file:// with the network
  blocked, because promotion is pure DOM state.

Budget: `MAP_PAGE_MAX_BYTES` for the whole document. Geometry is thinned to
`MAP_PAGE_MAX_POINTS` vertices per candidate (uniform stride — at page scale
the difference is sub-pixel; the interactive app remains the full-fidelity
surface) and steps lists are cut at `MAP_PAGE_MAX_STEPS` with an honest
"+N weitere Schritte" line.
"""

import html as html_escape
import math

from ..models.map_route import MapRoute
from ..models.map_spec import MapSpec

# The whole emitted document must stay under this — a chat/artifact surface
# chokes on multi-MB inline pages long before a browser does.
MAP_PAGE_MAX_BYTES = 1_572_864  # 1.5 MB
# Per-candidate geometry budget (uniform stride; sub-pixel at page scale).
MAP_PAGE_MAX_POINTS = 600
# Steps rendered per candidate before the "+N weitere Schritte" cut.
MAP_PAGE_MAX_STEPS = 60
# Tile <img> budget for tier (a) — a 960x560 canvas never needs more.
_MAX_TILES = 32

_W, _H = 960, 560
_PAD = 48

# Colours: the app's own dark theme (web/map.css / index.html).
_ACCENT = "#2e6be6"
_ALT = "#9aa6b5"

_ESC = html_escape.escape


def _fmt_duration(minutes: float) -> str:
    hours, rest = int(minutes // 60), round(minutes % 60)
    return (f"{hours} h {rest} min" if hours else f"{rest} min")


def _fmt_distance(km: float) -> str:
    if km < 1:
        return f"{round(km * 100) * 10} m"
    return (f"{km:.1f}".replace(".", ",") if km < 10 else f"{km:.0f}") + " km"


def _fmt_step_duration(seconds: float) -> str:
    return f"{round(seconds / 60)} Min." if seconds >= 60 else f"{round(seconds)} Sek."


# -- maneuver glyphs (ported from web/index.html, inline-SVG only) -----------

_STROKE = ('fill="none" stroke="currentColor" stroke-width="2" '
           'stroke-linecap="round" stroke-linejoin="round"')


def _arrow(d: str) -> str:
    return f'<svg viewBox="0 0 24 24" {_STROKE}><path d="{d}"/></svg>'


def _maneuver_glyph(step) -> str:
    kind, modifier = step.type or "", step.modifier or ""
    if kind == "depart":
        return _arrow("M12 21V6M12 6l-4 4M12 6l4 4")
    if kind == "arrive":
        return (f'<svg viewBox="0 0 24 24" {_STROKE}><circle cx="12" cy="10" r="3"/>'
                '<path d="M12 21c4-6 6-8.5 6-11a6 6 0 10-12 0c0 2.5 2 5 6 11z"/></svg>')
    if "roundabout" in kind or kind == "rotary":
        label = (f'<text x="12" y="9" text-anchor="middle" font-size="8" fill="currentColor" '
                 f'stroke="none" font-weight="700">{step.exit}</text>') if step.exit else ""
        return (f'<svg viewBox="0 0 24 24" {_STROKE}><circle cx="12" cy="13" r="5"/>'
                f'<path d="M12 22v-4"/><path d="M17 13h4"/>{label}</svg>')
    if kind == "merge":
        return _arrow("M6 21V13c0-3 6-3 6-7V3M12 3l-3 3M12 3l3 3")
    if kind == "fork":
        return _arrow("M12 21v-7L7 8V3M7 3L4 6M7 3l3 3" if "left" in modifier
                      else "M12 21v-7l5-6V3M17 3l-3 3M17 3l3 3")
    if "sharp left" in modifier:
        return _arrow("M12 21V10H6M6 10l4-4M6 10l4 4")
    if "sharp right" in modifier:
        return _arrow("M12 21V10h6M18 10l-4-4M18 10l-4 4")
    if "slight left" in modifier:
        return _arrow("M12 21v-8l-4-4V5M8 5L5 8M8 5l3 3")
    if "slight right" in modifier:
        return _arrow("M12 21v-8l4-4V5M16 5l3 3M16 5l-3 3")
    if "uturn" in modifier:
        return _arrow("M8 21V12a4 4 0 018 0v4M16 16l-3-3M16 16l3-3")
    if "left" in modifier:
        return _arrow("M12 21v-9H6M6 12l4-4M6 12l4 4")
    if "right" in modifier:
        return _arrow("M12 21v-9h6M18 12l-4-4M18 12l-4 4")
    return _arrow("M12 21V6M12 6l-4 4M12 6l4 4")


_MODE_ICON = {
    "car": f'<svg viewBox="0 0 24 24" {_STROKE}><path d="M4 15l1.5-5.5A2 2 0 017.4 8h9.2a2 2 0 011.9 1.5L20 15"/><path d="M3 15h18v4h-2m-14 0H3z"/><circle cx="7.2" cy="17" r="1.4"/><circle cx="16.8" cy="17" r="1.4"/></svg>',
    "truck": f'<svg viewBox="0 0 24 24" {_STROKE}><path d="M3 7h10v9H3z"/><path d="M13 11h4l3 3v2h-7z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/></svg>',
    "walk": f'<svg viewBox="0 0 24 24" {_STROKE}><circle cx="13" cy="4.5" r="1.6"/><path d="M10 20l2-5-1.5-4L8 12l-1 3M12.5 11l2 2 2.8 1M11.5 15l1.5 2 1 4"/></svg>',
    "bike": f'<svg viewBox="0 0 24 24" {_STROKE}><circle cx="6" cy="17" r="3"/><circle cx="18" cy="17" r="3"/><path d="M6 17l4-8h5l3 8M10 9h3M9 17h9"/></svg>',
}


def _instruction_text(step) -> str:
    """German wording for a backend maneuver — mirror of the app's own."""
    onto = f" auf {step.name}" if step.name else ""
    modifier = step.modifier or ""
    side = "links" if "left" in modifier else "rechts" if "right" in modifier else ""
    kind = step.type or ""
    if kind == "depart":
        return "Losfahren" + onto
    if kind == "arrive":
        return "Ziel erreicht"
    if "roundabout" in kind or kind == "rotary":
        if step.exit:
            return f"Im Kreisverkehr die {step.exit}. Ausfahrt nehmen{onto}"
        return "Kreisverkehr" + onto
    if kind == "merge":
        return "Auffahren" + onto
    if kind == "fork":
        return (f"{side} halten" if side else "Weiterfahren") + onto
    if kind == "end of road":
        return (f"{side} abbiegen" if side else "Weiterfahren") + onto
    if kind == "new name":
        return "Weiter" + onto
    if kind == "continue":
        return "Geradeaus weiter" + onto
    if "uturn" in modifier:
        return "Wenden" + onto
    if side:
        return f"{side} abbiegen{onto}"
    return "Weiter" + onto


# -- via / road labels (same semantics as the app) ---------------------------

def _road_of(step) -> str:
    return ((step.ref or "").split(";")[0] or step.name or "").strip()


def road_via_label(route: MapRoute, others: list[MapRoute]) -> str:
    """Longest-held road not shared with any other candidate ("A 81")."""
    named = [s for s in (route.steps or []) if _road_of(s)]
    if not named:
        return ""
    elsewhere = {_road_of(s) for other in others for s in (other.steps or []) if _road_of(s)}
    unique = [s for s in named if _road_of(s) not in elsewhere]
    pool = unique or named
    return _road_of(max(pool, key=lambda s: s.distance_m))


def place_via_label(route: MapRoute, others: list[MapRoute]) -> str:
    """Brief "via" text (max two landmarks), uniqueness-first.

    Prefer the highest-priority place NOT shared with the other candidates;
    when the top place itself is shared, the distinguishing one LEADS and
    the shared landmark follows ("Pforzheim und Heidelberg"); when nothing
    distinguishes at all, fall back to the shared top place.
    """
    places = route.via_places or []
    if not places:
        return ""
    elsewhere = {p.name for other in others for p in (other.via_places or [])}
    unique = [p for p in places if p.name not in elsewhere]
    if not unique:
        return places[0].name
    if places[0].name not in elsewhere:
        return places[0].name
    return f"{unique[0].name} und {places[0].name}"


# -- projection --------------------------------------------------------------

def _mercator(lat: float, lon: float, scale: float) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0 * scale
    clamped = max(min(lat, 85.05112878), -85.05112878)
    radians = math.radians(clamped)
    y = (1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi) / 2.0 * scale
    return x, y


def _simplify(points: list, budget: int) -> list:
    if len(points) <= budget:
        return points
    stride = (len(points) - 1) / (budget - 1)
    thinned = [points[round(i * stride)] for i in range(budget - 1)]
    thinned.append(points[-1])
    return thinned


class _Frame:
    """The page's fixed camera: a Mercator frame fitted around everything."""

    def __init__(self, lats: list[float], lons: list[float]):
        south, north = min(lats), max(lats)
        west, east = min(lons), max(lons)
        # Fractional zoom that fits the bbox into the padded viewport.
        x0, y0 = _mercator(north, west, 256.0)
        x1, y1 = _mercator(south, east, 256.0)
        span_x = max(x1 - x0, 1e-9)
        span_y = max(y1 - y0, 1e-9)
        self.zoom = min(
            math.log2((_W - 2 * _PAD) / span_x),
            math.log2((_H - 2 * _PAD) / span_y),
            17.0,
        )
        self.scale = 256.0 * (2.0 ** self.zoom)
        cx, cy = _mercator((south + north) / 2, (west + east) / 2, self.scale)
        self.ox = cx - _W / 2
        self.oy = cy - _H / 2
        self.mid_lat = (south + north) / 2

    def px(self, lat: float, lon: float) -> tuple[float, float]:
        x, y = _mercator(lat, lon, self.scale)
        return x - self.ox, y - self.oy

    def meters_per_px(self) -> float:
        return 156543.03392 * math.cos(math.radians(self.mid_lat)) / (2.0 ** self.zoom)


def _polyline(frame: _Frame, geometry) -> str:
    pts = []
    for p in _simplify(geometry, MAP_PAGE_MAX_POINTS):
        x, y = frame.px(p.lat, p.lon)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _graticule(frame: _Frame) -> str:
    """Subtle lat/lon lines with edge labels — the neutral canvas's texture."""
    def nice(span: float) -> float:
        for step in (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0):
            if span / step <= 7:
                return step
        return 20.0

    # Invert the frame corners back to degrees.
    def unproject(x: float, y: float) -> tuple[float, float]:
        lon = (x + frame.ox) / frame.scale * 360.0 - 180.0
        n = math.pi - 2.0 * math.pi * (y + frame.oy) / frame.scale
        lat = math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))
        return lat, lon

    north, west = unproject(0, 0)
    south, east = unproject(_W, _H)
    parts = []
    step = nice(max(east - west, 1e-6))
    lon = math.floor(west / step) * step
    while lon <= east:
        x, _ = frame.px(north, lon)
        if 0 <= x <= _W:
            parts.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{_H}"/>')
            parts.append(f'<text x="{x + 4:.1f}" y="14">{lon:.2f}°</text>')
        lon += step
    step = nice(max(north - south, 1e-6))
    lat = math.floor(south / step) * step
    while lat <= north:
        _, y = frame.px(lat, west)
        if 0 <= y <= _H:
            parts.append(f'<line x1="0" y1="{y:.1f}" x2="{_W}" y2="{y:.1f}"/>')
            parts.append(f'<text x="6" y="{y - 4:.1f}">{lat:.2f}°</text>')
        lat += step
    return "".join(parts)


def _scale_bar(frame: _Frame) -> str:
    mpp = frame.meters_per_px()
    target = mpp * 110
    nice = 10 ** math.floor(math.log10(max(target, 1)))
    for factor in (1, 2, 5, 10):
        if nice * factor >= target:
            nice *= factor
            break
    width = nice / mpp
    label = f"{nice / 1000:.0f} km" if nice >= 1000 else f"{nice:.0f} m"
    x, y = 20, _H - 22
    return (f'<g class="mp-scale"><line x1="{x}" y1="{y}" x2="{x + width:.0f}" y2="{y}"/>'
            f'<line x1="{x}" y1="{y - 5}" x2="{x}" y2="{y + 5}"/>'
            f'<line x1="{x + width:.0f}" y1="{y - 5}" x2="{x + width:.0f}" y2="{y + 5}"/>'
            f'<text x="{x + width / 2:.0f}" y="{y - 8}" text-anchor="middle">{label}</text></g>')


def _tile_imgs(frame: _Frame, spec: MapSpec, base_url: str) -> str:
    """Tier (a): this server's own raster tiles behind the SVG overlay.

    Never a third-party host — `base_url` IS our instance. Each tile removes
    itself on error, so an unreachable server (file:// offline, or runtime
    mode refusing the live proxy) degrades to the styled canvas of tier (b)
    without a single broken-image placeholder.
    """
    z_img = max(0, min(19, round(frame.zoom)))
    # World band: the ocean-blended Blue Marble (bathymetry oceans), matching
    # what the live map's imagery ladder serves at these zooms.
    source = "blue-marble-plus" if z_img <= 8 else spec.source_id
    factor = 2.0 ** (frame.zoom - z_img)  # displayed px per tile px
    size = 256.0 * factor
    first_x = int(frame.ox // size)
    first_y = int(frame.oy // size)
    tiles = []
    ty = first_y
    while ty * size - frame.oy < _H and len(tiles) < _MAX_TILES:
        tx = first_x
        while tx * size - frame.ox < _W and len(tiles) < _MAX_TILES:
            n = 2 ** z_img
            if 0 <= tx < n and 0 <= ty < n:
                left = tx * size - frame.ox
                top = ty * size - frame.oy
                tiles.append(
                    f'<img src="{base_url}/api/v1/maps/live/{source}/{z_img}/{tx}/{ty}" '
                    f'alt="" style="left:{left / _W * 100:.3f}%;top:{top / _H * 100:.3f}%;'
                    f'width:{size / _W * 100:.3f}%;height:{size / _H * 100:.3f}%" '
                    'onerror="this.remove()">')
            tx += 1
        ty += 1
    return "".join(tiles)


# -- the document ------------------------------------------------------------

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body { background: #10151c; color: #dfe8f2; font: 14px/1.45 -apple-system,
  'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; padding: 18px; }
.mp-shell { max-width: 1060px; margin: 0 auto; }
.mp-head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
  padding: 4px 2px 12px; }
.mp-head .glyph { width: 22px; height: 22px; display: inline-block;
  vertical-align: -5px; color: #2e6be6; }
.mp-head .glyph svg { width: 100%; height: 100%; }
.mp-title { font-size: 19px; font-weight: 650; letter-spacing: 0.01em; }
.mp-title .sep { color: #8fa0b3; margin: 0 6px; }
.mp-sum { color: #9fdcb4; font-weight: 700; }
.mp-sum small { color: #8fa0b3; font-weight: 400; }
.mp-stops { color: #8fa0b3; font-size: 13px; padding: 0 2px 10px; }
.mp-map { position: relative; width: 100%; aspect-ratio: 960/560;
  border-radius: 12px; overflow: hidden; background:
  radial-gradient(120% 90% at 30% 20%, #1a2330 0%, #131a24 55%, #0e141d 100%);
  border: 1px solid #26303d; }
.mp-map img { position: absolute; display: block; }
.mp-map svg.overlay { position: absolute; inset: 0; width: 100%; height: 100%; }
.mp-grid line { stroke: #35414f; stroke-width: 0.6; stroke-dasharray: 2 5; }
.mp-grid text { fill: #55657a; font-size: 10px; }
.mp-scale line { stroke: #c8d4e0; stroke-width: 1.5; }
.mp-scale text { fill: #c8d4e0; font-size: 11px; }
.mp-line { fill: none; stroke-linecap: round; stroke-linejoin: round; cursor: pointer; }
.mp-line.casing { stroke: #10151c; stroke-width: 9; opacity: 0.85; pointer-events: none; }
.mp-line.active { stroke: #2e6be6; stroke-width: 5; }
.mp-line.alt { stroke: #9aa6b5; stroke-width: 4; opacity: 0.65; }
.mp-badge { cursor: pointer; }
.mp-badge rect { fill: #ffffff; rx: 7; }
.mp-badge.active rect { fill: #222b36; }
.mp-badge text { font-size: 11.5px; font-weight: 650; fill: #222b36; }
.mp-badge.active text { fill: #f2f6fa; }
.mp-marker text { fill: #eef4fa; font-size: 12px; font-weight: 650;
  paint-order: stroke; stroke: #10151c; stroke-width: 3px; }
.mp-via-dot { fill: #cdd8e4; }
.mp-via-label { fill: #b9c6d4; font-size: 11px; paint-order: stroke;
  stroke: #10151c; stroke-width: 3px; }
.mp-panel { display: grid; grid-template-columns: 300px 1fr; gap: 16px;
  padding-top: 16px; }
@media (max-width: 760px) { .mp-panel { grid-template-columns: 1fr; } }
.mp-rows { display: flex; flex-direction: column; gap: 8px; }
.mp-row { display: grid; grid-template-columns: 26px 1fr auto; gap: 10px;
  align-items: center; padding: 10px 12px; border-radius: 10px;
  background: #161d27; border: 1px solid #26303d; cursor: pointer; }
.mp-row.active { border-color: #2e6be6; background: #1b2330; }
.mp-row .icon { width: 22px; height: 22px; color: #c8d4e0; }
.mp-row .icon svg { width: 100%; height: 100%; }
.mp-row .via b { display: block; font-size: 13.5px; }
.mp-row .via span { color: #8fa0b3; font-size: 12px; }
.mp-row .metrics { text-align: right; }
.mp-row .metrics b { color: #9fdcb4; display: block; }
.mp-row .metrics span { color: #8fa0b3; font-size: 12px; }
.mp-steps-box { background: #161d27; border: 1px solid #26303d;
  border-radius: 10px; padding: 6px 2px; max-height: 420px; overflow: auto; }
.mp-steps { display: none; }
.mp-steps.active { display: block; }
.mp-steps h3 { font-size: 13px; color: #8fa0b3; font-weight: 600;
  padding: 8px 14px 4px; }
.mp-step { display: grid; grid-template-columns: 30px 1fr; gap: 10px;
  padding: 7px 14px; align-items: start; }
.mp-step + .mp-step { border-top: 1px solid #1d2530; }
.mp-step .glyph { width: 22px; height: 22px; color: #c8d4e0; }
.mp-step .glyph svg { width: 100%; height: 100%; }
.mp-step .sub { color: #8fa0b3; font-size: 12px; display: block; }
.mp-more { color: #8fa0b3; font-size: 12.5px; padding: 8px 14px 10px; }
.mp-attribution { margin-top: 16px; padding: 10px 12px; border-radius: 8px;
  background: #161d27; border: 1px solid #26303d; color: #8fa0b3;
  font-size: 12px; }
.mp-attribution b { color: #b9c6d4; }
"""

_JS = """
(function () {
  function promote(i) {
    document.querySelectorAll('[data-cand]').forEach(function (node) {
      if (node.classList.contains('casing')) return;   // casing styling is fixed
      var active = node.getAttribute('data-cand') === String(i);
      node.classList.toggle('active', active);
      if (node.classList.contains('mp-line'))
        node.classList.toggle('alt', !active);
    });
    // SVG paints in document order: the active line must sit on top.
    var lines = document.getElementById('mp-lines');
    var casing = lines.querySelector('.mp-line.casing[data-cand="' + i + '"]');
    var line = lines.querySelector('.mp-line:not(.casing)[data-cand="' + i + '"]');
    if (casing) lines.appendChild(casing);
    if (line) lines.appendChild(line);
    var badges = document.getElementById('mp-badges');
    var badge = badges && badges.querySelector('.mp-badge[data-cand="' + i + '"]');
    if (badge) badges.appendChild(badge);
    var sum = document.getElementById('mp-sum');
    var row = document.querySelector('.mp-row[data-cand="' + i + '"]');
    if (sum && row) sum.innerHTML = row.getAttribute('data-sum');
  }
  document.addEventListener('click', function (event) {
    var node = event.target.closest('[data-cand]');
    if (node) promote(node.getAttribute('data-cand'));
  });
})();
"""


def _steps_html(route: MapRoute, index: int, active: bool) -> str:
    rows = []
    steps = route.steps or []
    for step in steps[:MAP_PAGE_MAX_STEPS]:
        sub = ""
        if step.distance_m:
            sub = (f'<span class="sub">{_fmt_step_duration(step.duration_s or 0)} '
                   f'({_fmt_distance(step.distance_m / 1000.0)})</span>')
        rows.append(
            f'<div class="mp-step"><span class="glyph">{_maneuver_glyph(step)}</span>'
            f'<span>{_ESC(_instruction_text(step))}{sub}</span></div>')
    if len(steps) > MAP_PAGE_MAX_STEPS:
        rows.append(f'<div class="mp-more">+{len(steps) - MAP_PAGE_MAX_STEPS} weitere Schritte</div>')
    if not rows:
        rows.append('<div class="mp-more">Keine Schrittliste für diese Route.</div>')
    title = "Route" if index == 0 else f"Alternative {index}"
    return (f'<div class="mp-steps{" active" if active else ""}" data-cand="{index}">'
            f'<h3>Wegbeschreibung — {_ESC(title)}</h3>{"".join(rows)}</div>')


def _via_chain(route: MapRoute, limit: int = 4) -> str:
    return " · ".join(p.name for p in (route.via_places or [])[:limit])


def build_map_page(spec: MapSpec, *, attribution: str, base_url: str | None = None) -> str:
    """The document (see module docstring). `attribution` is mandatory by
    signature — the footer is part of the template, not an option."""
    routes = spec.routes or []
    primary = routes[0] if routes else None
    candidates: list[MapRoute] = ([primary, *primary.alternates] if primary else [])

    # Frame around everything that will be drawn.
    lats = [p.lat for cand in candidates for p in cand.geometry]
    lons = [p.lon for cand in candidates for p in cand.geometry]
    for location in spec.locations:
        lats.append(location.camera.center.lat)
        lons.append(location.camera.center.lon)
    if not lats:
        lats, lons = [47.2, 55.1], [5.8, 15.1]  # Germany, the empty-spec frame
    frame = _Frame(lats, lons)

    # -- SVG overlay ---------------------------------------------------------
    lines, badges = [], []
    for index, cand in reversed(list(enumerate(candidates))):
        points = _polyline(frame, cand.geometry)
        active = index == 0
        lines.append(f'<polyline class="mp-line casing" data-cand="{index}" points="{points}"/>')
        lines.append(f'<polyline class="mp-line {"active" if active else "alt"}" '
                     f'data-cand="{index}" points="{points}"/>')
        mid = cand.geometry[len(cand.geometry) // 2]
        mx, my = frame.px(mid.lat, mid.lon)
        text = f"{_fmt_duration(cand.duration_min)} · {_fmt_distance(cand.distance_km)}"
        width = 7.2 * len(text) + 16
        badges.append(
            f'<g class="mp-badge{" active" if active else ""}" data-cand="{index}" '
            f'transform="translate({mx - width / 2:.0f},{my - 26:.0f})">'
            f'<rect width="{width:.0f}" height="24" rx="7"/>'
            f'<text x="{width / 2:.0f}" y="16" text-anchor="middle">{_ESC(text)}</text></g>')

    markers = []
    if primary is not None and primary.geometry:
        sx, sy = frame.px(primary.geometry[0].lat, primary.geometry[0].lon)
        ex, ey = frame.px(primary.geometry[-1].lat, primary.geometry[-1].lon)
        markers.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="6" fill="#3fb27f" '
                       'stroke="#10151c" stroke-width="2"/>')
        markers.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="6" fill="#e2574c" '
                       'stroke="#10151c" stroke-width="2"/>')
        for stop in (primary.stops or [])[1:-1]:
            x, y = frame.px(stop.lat, stop.lon)
            markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#f2a33c" '
                           'stroke="#10151c" stroke-width="2"/>')
        for place in (primary.via_places or [])[:3]:
            x, y = frame.px(place.lat, place.lon)
            markers.append(f'<circle class="mp-via-dot" cx="{x:.1f}" cy="{y:.1f}" r="2.5"/>'
                           f'<text class="mp-via-label" x="{x + 6:.1f}" y="{y + 4:.1f}">'
                           f'{_ESC(place.name)}</text>')
    for location in spec.locations:
        x, y = frame.px(location.camera.center.lat, location.camera.center.lon)
        markers.append(f'<g class="mp-marker"><circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" '
                       f'fill="#7cc4ff" stroke="#10151c" stroke-width="2"/>'
                       f'<text x="{x + 8:.1f}" y="{y + 4:.1f}">{_ESC(location.name)}</text></g>')

    # -- panel ---------------------------------------------------------------
    row_html, steps_html = [], []
    for index, cand in enumerate(candidates):
        others = [c for c in candidates if c is not cand]
        via = place_via_label(cand, others)
        road = road_via_label(cand, others)
        summary = (f"<b>{_ESC(_fmt_duration(cand.duration_min))}</b> · "
                   f"{_ESC(_fmt_distance(cand.distance_km))}"
                   + (f" · über {_ESC(via)}" if via else ""))
        row_html.append(
            f'<div class="mp-row{" active" if index == 0 else ""}" data-cand="{index}" '
            f'data-sum="{_ESC(summary, quote=True)}">'
            f'<span class="icon">{_MODE_ICON.get(cand.mode, _MODE_ICON["car"])}</span>'
            f'<span class="via"><b>{("über " + _ESC(via)) if via else "Route"}</b>'
            f'<span>{("über " + _ESC(road)) if road else "&nbsp;"}</span></span>'
            f'<span class="metrics"><b>{_ESC(_fmt_duration(cand.duration_min))}</b>'
            f'<span>{_ESC(_fmt_distance(cand.distance_km))}</span></span></div>')
        steps_html.append(_steps_html(cand, index, active=index == 0))

    # -- header --------------------------------------------------------------
    if primary is not None:
        title = (f'{_ESC(primary.from_location or "Start")}'
                 f'<span class="sep">→</span>{_ESC(primary.to_location or "Ziel")}')
        chain = _via_chain(primary)
        head_sum = (f"<b>{_ESC(_fmt_duration(primary.duration_min))}</b> · "
                    f"{_ESC(_fmt_distance(primary.distance_km))}"
                    + (f" · über {_ESC(place_via_label(primary, candidates[1:]))}"
                       if primary.via_places else ""))
        mode_icon = _MODE_ICON.get(primary.mode, _MODE_ICON["car"])
    else:
        title = _ESC(spec.title or "Karte")
        chain, head_sum, mode_icon = "", "", ""

    stops_line = ""
    names = [location.name for location in spec.locations if location.name]
    if len(names) >= 2:
        stops_line = f'<div class="mp-stops">{_ESC(" → ".join(names))}</div>'
    elif chain:
        stops_line = f'<div class="mp-stops">über {_ESC(chain)}</div>'

    tiles = _tile_imgs(frame, spec, base_url.rstrip("/")) if base_url else ""

    doc = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_ESC(spec.title or "Route")} — agentic-maps</title>
<style>{_CSS}</style>
</head>
<body>
<div class="mp-shell">
  <div class="mp-head">
    <span class="glyph">{mode_icon}</span>
    <span class="mp-title">{title}</span>
    <span class="mp-sum" id="mp-sum">{head_sum}</span>
    <small style="color:#55657a">ohne Verkehrslage (freie Fahrt)</small>
  </div>
  {stops_line}
  <div class="mp-map">
    {tiles}
    <svg class="overlay" viewBox="0 0 {_W} {_H}" preserveAspectRatio="xMidYMid slice">
      <g class="mp-grid">{_graticule(frame)}</g>
      <g id="mp-lines">{"".join(lines)}</g>
      <g>{"".join(markers)}</g>
      <g id="mp-badges">{"".join(badges)}</g>
      {_scale_bar(frame)}
    </svg>
  </div>
  <div class="mp-panel">
    <div class="mp-rows">{"".join(row_html)}</div>
    <div class="mp-steps-box">{"".join(steps_html)}</div>
  </div>
  <div class="mp-attribution"><b>Kartendaten &amp; Bilddaten:</b> {_ESC(attribution)} — Diese Herkunftsangabe ist Bestandteil des Dokuments und muss bei jeder Weitergabe erhalten bleiben.</div>
</div>
<script>{_JS}</script>
</body>
</html>"""
    return doc
