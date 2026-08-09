"""Draw a georeferenced site plan as SVG.

The plan is the map without the basemap: same projection, same coordinates,
so a plan and a map view of the same site can be laid over one another. It
lives here rather than in a host page because every property scenario wants the same
drawing, and the second copy of it is where the two start to disagree.

Geometry in, SVG out. The look is entirely in `web/siteplan.css` — this file
emits classes, never colours.
"""

from __future__ import annotations

import math

from ..models.site_plan import SitePlan

# Roughly the advance width of the label font at 1 px, used to lay out wraps
# and to reserve collision boxes. Measuring text properly needs a font engine;
# for placing a handful of chips an estimate that errs LARGE is enough.
_CHAR_W = 0.62


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap(text: str, max_chars: int) -> list[str]:
    """Greedy word wrap. Long labels used to run out of their own area."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _overlaps(a, b, pad=6.0):
    return (a[0] < b[2] + pad and b[0] < a[2] + pad
            and a[1] < b[3] + pad and b[1] < a[3] + pad)


class _Projection:
    """Equirectangular around the plot, fitted to the drawing box."""

    def __init__(self, plan: SitePlan):
        ring = plan.plot
        lat0 = sum(p.lat for p in ring) / len(ring)
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 111320.0
        xs = [p.lon * self.kx for p in ring]
        ys = [p.lat * self.ky for p in ring]
        self.cx, self.cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        self.w, self.h = plan.width_px, plan.height_px
        self.scale = min(self.w / (max(xs) - min(xs) + 2 * plan.margin_m),
                         self.h / (max(ys) - min(ys) + 2 * plan.margin_m))

    def xy(self, point) -> tuple[float, float]:
        return (self.w / 2 + (point.lon * self.kx - self.cx) * self.scale,
                self.h / 2 - (point.lat * self.ky - self.cy) * self.scale)


def _path(proj, points, close=False) -> str:
    d = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in (proj.xy(p) for p in points))
    return d + ("Z" if close else "")


def _extent(proj, points):
    pts = [proj.xy(p) for p in points]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _rose(x: float, y: float) -> str:
    """A drawn compass rose, not a triangle with an N under it.

    The old arrow was a filled triangle whose letter sat on top of whatever
    street label ran past — it read as a typo rather than as north.
    """
    return (
        f'<g class="sp-rose" transform="translate({x:.0f} {y:.0f})">'
        '<circle class="sp-rose-ring" cx="0" cy="0" r="21"/>'
        '<path class="sp-rose-tick" d="M0 -21v-5M0 21v5M-21 0h-5M21 0h5"/>'
        '<path class="sp-rose-n" d="M0 -17 L5.5 4 L0 0.5 L-5.5 4 Z"/>'
        '<path class="sp-rose-s" d="M0 17 L5.5 -4 L0 -0.5 L-5.5 -4 Z"/>'
        '<text class="sp-rose-label" x="0" y="-27">N</text>'
        '</g>')


def render(plan: SitePlan) -> str:
    proj = _Projection(plan)
    w, h = plan.width_px, plan.height_px
    out: list[str] = [
        f'<svg class="sp-plan" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'preserveAspectRatio="xMidYMid meet" data-ds-notr="1">',
        f'<rect class="sp-bg" x="0" y="0" width="{w:.0f}" height="{h:.0f}" rx="6"/>',
    ]

    # Reserved boxes: the rose first, then every label as it is placed, so a
    # later label can step aside instead of landing on an earlier one.
    rose_x, rose_y = w - 46, 46
    taken: list[tuple[float, float, float, float]] = []
    if plan.north:
        taken.append((rose_x - 34, rose_y - 40, rose_x + 34, rose_y + 34))

    # --- streets: the skeleton, at real width ------------------------------
    out.append('<g class="sp-streets">')
    for street in plan.streets:
        if len(street.geometry) < 2:
            continue
        stroke = street.width_m * proj.scale
        out.append(f'<path class="sp-street" style="stroke-width:{stroke:.1f}" '
                   f'd="{_path(proj, street.geometry)}"/>')
    out.append('</g>')

    # --- street names, collision-aware -------------------------------------
    out.append('<g class="sp-street-labels">')
    named: set[str] = set()
    for street in sorted(plan.streets, key=lambda s: -len(s.geometry)):
        if not street.name or street.name in named:
            continue
        pts = [proj.xy(p) for p in street.geometry]
        segments = sorted(zip(pts, pts[1:]), key=lambda pair: -math.dist(*pair))
        half = len(street.name) * 13.0 * _CHAR_W / 2 + 8
        for (x1, y1), (x2, y2) in segments:
            if math.dist((x1, y1), (x2, y2)) < 2 * half:
                continue
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            if not (12 < mx < w - 12 and 12 < my < h - 12):
                continue
            box = (mx - half, my - 14, mx + half, my + 14)
            if any(_overlaps(box, other) for other in taken):
                continue
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if angle > 90 or angle < -90:
                angle += 180
            taken.append(box)
            named.add(street.name)
            out.append(f'<text class="sp-street-name" x="{mx:.1f}" y="{my - 8:.1f}" '
                       f'transform="rotate({angle:.1f} {mx:.1f} {my:.1f})">'
                       f'{_esc(street.name)}</text>')
            break
    out.append('</g>')

    # --- the plot ----------------------------------------------------------
    out.append(f'<path class="sp-plot" d="{_path(proj, plan.plot, True)}"/>')

    # --- areas: roads under yards under buildings --------------------------
    order = {"road": 0, "yard": 1, "building": 2}
    for area in sorted(plan.areas, key=lambda a: order.get(a.role, 2)):
        cls = f"sp-area sp-{area.role}"
        if area.role == "road":
            # ONE stroked line with round joins. A driveway assembled from
            # overlapping rectangles showed a seam at every bend and stopped
            # dead where the last box ended.
            stroke = (area.width_m or 8.0) * proj.scale
            out.append(f'<g class="{cls}" data-part="{area.id}">'
                       f'<path style="stroke-width:{stroke:.1f}" '
                       f'd="{_path(proj, area.ring)}"/></g>')
            continue
        out.append(f'<g class="{cls}" data-part="{area.id}">'
                   f'<path d="{_path(proj, area.ring, True)}"/></g>')

    # --- area labels, wrapped to their own box -----------------------------
    for area in plan.areas:
        if not area.label or area.role == "road":
            continue
        x0, y0, x1, y1 = _extent(proj, area.ring)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        box_w, box_h = x1 - x0 - 14, y1 - y0 - 12
        size = 19.0 if area.role == "building" else 13.0
        note_size = 14.0 if area.role == "building" else 12.0
        # Shrink to fit rather than overflow: a small yard used to get the same
        # 19 px headline as a hall and wrote its own area outside its outline.
        for _ in range(8):
            max_chars = max(6, int(box_w / (size * _CHAR_W)))
            lines = wrap(area.label, max_chars)
            note_lines = wrap(area.note, max(8, int(box_w / (note_size * _CHAR_W)))) \
                if area.note else []
            total = len(lines) * (size + 3) + len(note_lines) * (note_size + 4)
            if total <= box_h or size <= 10.5:
                break
            size *= 0.88
            note_size *= 0.9
        top = cy - total / 2 + size
        cls = "sp-name" if area.role == "building" else "sp-yard-label"
        for i, line in enumerate(lines):
            out.append(f'<text class="{cls}" style="font-size:{size:.1f}px" '
                       f'x="{cx:.1f}" y="{top + i * (size + 3):.1f}">'
                       f'{_esc(line)}</text>')
        base = top + len(lines) * (size + 3) + 2
        for i, line in enumerate(note_lines):
            out.append(f'<text class="sp-area-note" style="font-size:{note_size:.1f}px" '
                       f'x="{cx:.1f}" y="{base + i * (note_size + 4):.1f}">'
                       f'{_esc(line)}</text>')

    # --- rose + scale bar ---------------------------------------------------
    if plan.north:
        out.append(_rose(rose_x, rose_y))
    bar = plan.scale_bar_m * proj.scale
    x0, y0 = 34, h - 30
    out.append(f'<g class="sp-scale"><path d="M{x0} {y0}h{bar:.1f}M{x0} {y0 - 5}v10'
               f'M{x0 + bar:.1f} {y0 - 5}v10"/>'
               f'<text x="{x0 + bar / 2:.1f}" y="{y0 - 11:.0f}">'
               f'{plan.scale_bar_m:.0f} m</text></g>')
    out.append('</svg>')
    return "".join(out)
