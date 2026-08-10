"""Headless check: the mid/low-zoom cartography pass (z5–z11), live.

The owner's complaint, verified against screenshots before the pass: at
country zoom every road was the same thin dark-blue hairline, no route
shields showed, land was a washed-out gray-green with no landcover, water a
pale cyan, borders faint and every town label the same size. The tuning
lives in web/road-styles.js (CARTO palette, tuned overview roads, two-band
shields) and web/map.js (_tuneMidZoom, cartoFlavor merge); web/globe.js
reads the same palette for its sphere texture so the z4 handover stays in
one color family.

What this asserts, in a real browser against the running dev server:

  Per theme (map-light AND map-dark):
  1. The palette landed: water/earth fills are the CARTO values (not the
     stock #80deea cyan), the landcover layer carries the sage ramp and is
     opaque at z6.5 (stock faded it to 0 by z7 — the dead-band bug), the
     am-landuse-mid wash exists, the overview motorway is the amber ribbon
     with its casing, the country border is the crisp palette line.
  2. Road hierarchy at z6.5: NOTHING below primary renders (roads_minor,
     roads_major [secondary+tertiary], roads_link all empty in
     queryRenderedFeatures), while the motorway ribbon and the landcover
     masses DO render. At z10.5 the stock road net is back.
  3. Shields: the z6.5–8 band layer (roads-shields-low) exists with
     minzoom 6.5 and actually renders plates at z7.2. (The tiles carry
     shield_text only from their z7 level — below display zoom 7 there is
     nothing to sign; that is a data fact, not a style bug.)
  4. Big-city hierarchy: places_locality text-color is population_rank-
     driven (capitals darker than villages).

  Composition guards:
  5. Hybrid is byte-for-byte untouched: stock water cyan, no
     am-landuse-mid, no roads-shields-low, legacy overview trunk color,
     roads-over present.
  6. A flavor override (canvas-style, __canvas) still wins: the tuning
     gates itself off — drained overview lines, no am-landuse-mid, the
     override's water color.

  Globe handover continuity (tolerance re-derived for the biome raster):
  7. Flat at z4.6 vs globe at z4.0, light and dark. WATER is sampled
     strictly (hue delta < 25 — nothing changed it). LAND cannot be a tight
     hue-delta any more: the globe land is now the Natural Earth II biome
     remap (tools/make_globe_biomes.py) and the flat map at z4.6 shows its
     own landcover variation — so land is asserted as "within the palette
     family": both samples' hues inside the sand→sage arc (30°–140°),
     saturation ≤ 0.45 (simplified, not satellite) and brightness within
     0.2 of each other, plus a coarse 45° hue-delta backstop.

  Biomes on the globe (owner: "i doubt that africa is that green"):
  8. Globe over the Sahara vs the interior-France sample of the Europe
     globe view, light AND dark: the Sahara must be measurably warmer
     (hue at least 12° lower) and brighter (sand, not sage). Measured
     margins at calibration: Δhue ≈ 27° light / 30° dark, ΔV ≈ 0.08 / 0.03.

  Continental road web (owner: "at least show the main roads here like
  google"):
  9. roads-overview-motorway renders features at z4.6 and z4.7 (before the
     pass its minzoom was 5 — nothing below), with minzoom 4. The tiles
     carry the motorway net from their z3 level (tools/probe_lowzoom_roads
     .py), so the web is tile-honest, no extra road dataset.

Requires the dev server (see BASE) and Chromium via playwright or
AGENTIC_MAPS_RENDER_CHROMIUM_PATH, exactly like the other harnesses.
"""
import asyncio
import colorsys
import json
import os
import statistics
import sys
import tempfile

from PIL import Image, ImageChops
from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())
ALLOWED_CONSOLE = ("Failed to load resource",)

# Mirrors road-styles.js CARTO — the harness must fail when someone retunes
# the palette without retuning the checks (they document each other).
CARTO = {
    "map-light": {
        "water": "#96c3e3", "earth": "#f1efe6", "forest": "#b7cbaa",
        "motorway": "#e2a44b", "motorwayFaint": "#d6b481", "casing": "#fbf7ea",
        "border": "rgba(96,89,76,0.85)",
    },
    "map-dark": {
        "water": "#22364a", "earth": "#23271f", "forest": "#28331f",
        "motorway": "#b98a35", "motorwayFaint": "#87703f", "casing": "#12130e",
        "border": "rgba(203,212,203,0.6)",
    },
}

STOCK_WATER = "#80deea"          # the pale cyan the owner rejected

STYLE_STATE = """() => {
  const m = window.agenticMaps[0].map;
  const q = (ids) => {
    try { return m.queryRenderedFeatures({ layers: ids }).length; }
    catch (e) { return -1; }
  };
  const paintStr = (id, prop) => {
    try { return JSON.stringify(m.getPaintProperty(id, prop) || null); }
    catch (e) { return 'null'; }
  };
  return {
    zoom: +m.getZoom().toFixed(2),
    water: paintStr('water', 'fill-color'),
    earth: paintStr('earth', 'fill-color'),
    landcoverColor: paintStr('landcover', 'fill-color'),
    landcoverOpacity: paintStr('landcover', 'fill-opacity'),
    landuseMid: !!m.getLayer('am-landuse-mid'),
    overviewMotorway: paintStr('roads-overview-motorway', 'line-color'),
    overviewCasing: paintStr('roads-overview-motorway-casing', 'line-color'),
    countryBorder: paintStr('country-borders', 'line-color'),
    cityColor: paintStr('places_locality', 'text-color'),
    shieldLow: m.getLayer('roads-shields-low')
      ? { minzoom: m.getLayer('roads-shields-low').minzoom,
          maxzoom: m.getLayer('roads-shields-low').maxzoom } : null,
    renderedSubPrimary: q(['roads_minor', 'roads_major', 'roads_link']),
    renderedMotorway: q(['roads-overview-motorway']),
    renderedLandcover: q(['landcover']),
    roadsOver: !!m.getLayer('roads-over'),
  };
}"""


async def settle(page, timeout=40000):
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && "
        "window.agenticMaps[0].map.isStyleLoaded()", timeout=timeout)
    try:
        await page.wait_for_function(
            "window.agenticMaps[0].map.areTilesLoaded()", timeout=timeout)
    except Exception:
        pass                     # a straggler tile must not fail the style checks
    await page.wait_for_timeout(1200)


async def theme_pass(page, view: str) -> dict:
    out: dict = {"view": view}
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@51.16,10.45,6.5z&view={view}&lang=de",
                    wait_until="load")
    await settle(page)
    out["z65"] = await page.evaluate(STYLE_STATE)
    await page.screenshot(path=f"{SCRATCH}/pw-carto-{view}-z65.png")

    # Shields render in the z6.5–8 band (tile z7 carries the data).
    await page.evaluate(
        "window.agenticMaps[0].map.jumpTo({ center: [8.8, 50.0], zoom: 7.2 })")
    await settle(page)
    out["shieldsAt72"] = await page.evaluate(
        "() => { try { return window.agenticMaps[0].map"
        ".queryRenderedFeatures({ layers: ['roads-shields-low'] }).length; }"
        " catch (e) { return -1; } }")

    # The stock road net returns at city approach.
    await page.evaluate(
        "window.agenticMaps[0].map.jumpTo({ center: [9.18, 48.77], zoom: 10.5 })")
    await settle(page)
    out["subPrimaryAt105"] = await page.evaluate(
        "() => window.agenticMaps[0].map"
        ".queryRenderedFeatures({ layers: ['roads_major'] }).length")
    return out


async def composition_pass(page) -> dict:
    out: dict = {}
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@51.16,10.45,6.5z&view=map-light&lang=de",
                    wait_until="load")
    await settle(page)

    # Hybrid: the imagery view must not inherit ANY of the tuning.
    await page.evaluate("window.agenticMaps[0].setView('hybrid')")
    await settle(page)
    out["hybrid"] = await page.evaluate("""() => {
      const m = window.agenticMaps[0].map;
      const trunk = m.getLayer('roads-overview-trunk');
      return {
        water: JSON.stringify(m.getPaintProperty('water', 'fill-color')),
        landuseMid: !!m.getLayer('am-landuse-mid'),
        shieldLow: !!m.getLayer('roads-shields-low'),
        roadsOver: !!m.getLayer('roads-over'),
        trunkColor: trunk
          ? JSON.stringify(m.getPaintProperty('roads-overview-trunk', 'line-color'))
          : null,
      };
    }""")

    # A flavor override (the canvas idea: drained palette, __canvas marker)
    # must still win over the tuning — the gate turns the whole pass off.
    await page.evaluate("window.agenticMaps[0].setView('map-light')")
    await settle(page)
    await page.evaluate("""() => window.agenticMaps[0].setFlavorOverride(
      { __canvas: true, water: '#dcdedf', earth: '#f2f1ef' })""")
    await settle(page)
    out["override"] = await page.evaluate("""() => {
      const m = window.agenticMaps[0].map;
      return {
        water: JSON.stringify(m.getPaintProperty('water', 'fill-color')),
        landuseMid: !!m.getLayer('am-landuse-mid'),
        motorway: JSON.stringify(
          m.getPaintProperty('roads-overview-motorway', 'line-color')),
        shieldLow: !!m.getLayer('roads-shields-low'),
      };
    }""")
    await page.evaluate("window.agenticMaps[0].setFlavorOverride(null)")
    await settle(page)
    out["restored"] = await page.evaluate(
        "() => JSON.stringify(window.agenticMaps[0].map"
        ".getPaintProperty('water', 'fill-color'))")
    return out


def _median_rgb(image, box):
    pixels = [image.getpixel((x, y))
              for x in range(box[0], box[2], 3) for y in range(box[1], box[3], 3)]
    return tuple(statistics.median(channel[i] for channel in pixels)
                 for i in range(3))


def _hue_deg(rgb):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    return h * 360, s, v


async def globe_pass(page, view: str) -> dict:
    """Flat z4.6 vs globe z4.0: land and water must stay one family."""
    out: dict = {"view": view}
    shots = {}
    for name, zoom in (("flat", 4.6), ("globe", 4.0)):
        await page.goto("about:blank")
        await page.goto(f"{BASE}/#@50.5,9.5,{zoom}z&view={view}&lang=de",
                        wait_until="load")
        await page.wait_for_function(
            "window.agenticGlobe && window.agenticMaps[0].map.isStyleLoaded()",
            timeout=40000)
        await page.wait_for_timeout(2500)
        path = f"{SCRATCH}/pw-carto-globe-{view}-{name}.png"
        await page.screenshot(path=path)
        shots[name] = path
        if name == "flat":
            # The continental road web: motorway features on stage at z4.6
            # (the layer's old minzoom of 5 rendered nothing here).
            out["roadsAtZ46"] = await page.evaluate(
                "() => { try { return window.agenticMaps[0].map"
                ".queryRenderedFeatures({ layers: ['roads-overview-motorway'] })"
                ".length; } catch (e) { return -1; } }")
            out["motorwayMinzoom"] = await page.evaluate(
                "() => { const l = window.agenticMaps[0].map"
                ".getLayer('roads-overview-motorway');"
                " return l ? l.minzoom : null; }")
    # Fixed sample boxes for the fixed camera/viewport: interior France for
    # land (well inside the coastline on both projections), the Atlantic
    # west of Iberia for water.
    land_box, water_box = (405, 520, 455, 560), (120, 620, 180, 680)
    for name, path in shots.items():
        image = Image.open(path).convert("RGB")
        out[name] = {"land": _median_rgb(image, land_box),
                     "water": _median_rgb(image, water_box)}
    for key in ("land", "water"):
        h1, s1, v1 = _hue_deg(out["flat"][key])
        h2, s2, v2 = _hue_deg(out["globe"][key])
        delta = min(abs(h1 - h2), 360 - abs(h1 - h2))
        out[f"{key}HueDelta"] = round(delta, 1)
        out[f"{key}Hues"] = [round(h1, 1), round(h2, 1)]
        out[f"{key}Sats"] = [round(s1, 3), round(s2, 3)]
        out[f"{key}Vals"] = [round(v1, 3), round(v2, 3)]
    return out


async def roadweb_pass(page, view: str) -> dict:
    """Flat Europe at z4.7 — the owner's Google side-by-side view: the
    faint amber motorway web must knit the continent."""
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@50.5,9.5,4.7z&view={view}&lang=de",
                    wait_until="load")
    await settle(page)
    count = await page.evaluate(
        "() => { try { return window.agenticMaps[0].map"
        ".queryRenderedFeatures({ layers: ['roads-overview-motorway'] })"
        ".length; } catch (e) { return -1; } }")
    await page.screenshot(path=f"{SCRATCH}/pw-carto-roadweb-{view}-z47.png")
    return {"view": view, "roadsAtZ47": count}


async def biome_pass(page, view: str) -> dict:
    """Globe over the Sahara (the owner's 'Africa is not that green' view):
    sampled desert must be sand, not the sage that vegetated Europe gets.
    The Europe half of the comparison is the globe land sample from
    globe_pass — interior France on the exact same camera setup."""
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@23,10,4z&view={view}&lang=de",
                    wait_until="load")
    await page.wait_for_function(
        "window.agenticGlobe && window.agenticMaps[0].map.isStyleLoaded()",
        timeout=40000)
    await page.wait_for_timeout(3000)
    path = f"{SCRATCH}/pw-carto-biome-africa-{view}.png"
    await page.screenshot(path=path)
    # Eastern Algeria: empty desert on this camera, no labels, no borders.
    rgb = _median_rgb(Image.open(path).convert("RGB"), (500, 330, 660, 410))
    h, s, v = _hue_deg(rgb)
    return {"view": view, "sahara": rgb,
            "saharaHue": round(h, 1), "saharaSat": round(s, 3),
            "saharaVal": round(v, 3)}


TILE_COVER = """() => {
  const m = window.agenticMaps[0].map;
  const out = {};
  const managers = m.style.tileManagers || {};
  for (const key of Object.keys(managers)) {
    const tm = managers[key];
    const hist = {};
    for (const id of (tm.getRenderableIds ? tm.getRenderableIds() : [])) {
      const t = tm.getTileByID ? tm.getTileByID(id) : null;
      const z = t && t.tileID ? t.tileID.canonical.z : null;
      if (z !== null) hist[z] = (hist[z] || 0) + 1;
    }
    out[key] = hist;
  }
  return out;
}"""

# Previous minor-road width at z16.5 (the frozen table's exp-1.6 curve
# between the z15 and z17 anchors: 2 + 4.5 * (1.6**1.5 - 1) / (1.6**2 - 1)).
# The close-zoom pass must at least DOUBLE it — hardcoded so a silent
# re-narrowing fails loudly.
OLD_MINOR_WIDTH_Z165 = 4.95


def _eval_zoom_interpolation(expression, zoom: float) -> float:
    """Numeric value of ['interpolate', ['exponential', b]|['linear'], ['zoom'], ...]."""
    kind = expression[1]
    base = kind[1] if kind[0] == "exponential" else 1.0
    stops = expression[3:]
    anchors = [(stops[i], stops[i + 1]) for i in range(0, len(stops), 2)]
    if zoom <= anchors[0][0]:
        return anchors[0][1]
    for (z0, v0), (z1, v1) in zip(anchors, anchors[1:]):
        if zoom <= z1:
            if base == 1.0:
                t = (zoom - z0) / (z1 - z0)
            else:
                t = (base ** (zoom - z0) - 1) / (base ** (z1 - z0) - 1)
            return v0 + t * (v1 - v0)
    return anchors[-1][1]


async def render_quality_pass(page) -> dict:
    """Pitched-view LOD, close-zoom widths and the 3D-building fixes."""
    out: dict = {}

    # -- LOD: the Hamburg-harbor reference view (owner's screenshots) -----
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@53.54,10.0,16z&view=hybrid&lang=de",
                    wait_until="load")
    await settle(page)
    out["flatCover"] = await page.evaluate(TILE_COVER)
    await page.evaluate(
        "() => window.agenticMaps[0].map.jumpTo({ pitch: 60 })")
    await settle(page)
    out["pitchedCover"] = await page.evaluate(TILE_COVER)
    await page.screenshot(path=f"{SCRATCH}/pw-carto-lod-harbor.png")

    # -- LOD scoping: the "weird grid" view. Coarse VECTOR tiles at a close
    # display zoom drew simplified far-zoom roads as ruler-straight white
    # hairlines; the streets source must stay single-zoom while pitched.
    await page.evaluate(
        "() => window.agenticMaps[0].map.jumpTo("
        "{ center: [10.02, 53.52], zoom: 14.5, pitch: 60 })")
    await settle(page)
    out["gridViewCover"] = await page.evaluate(TILE_COVER)
    await page.screenshot(path=f"{SCRATCH}/pw-carto-lod-gridview.png")

    # -- close-zoom width tables (map-light; map-dark shares them) --------
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@53.55,10.0,16.5z&view=map-light&lang=de",
                    wait_until="load")
    await settle(page)
    out["minorWidthExpr"] = await page.evaluate(
        "() => window.agenticMaps[0].map.getPaintProperty("
        "'roads_minor', 'line-width')")
    out["minorGapExpr"] = await page.evaluate(
        "() => window.agenticMaps[0].map.getPaintProperty("
        "'roads_minor_casing', 'line-gap-width')")

    # -- 3D buildings at the giant-box / ground-shimmer view --------------
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@53.535,10.02,16.2z&view=map-light&lang=de",
                    wait_until="load")
    await settle(page)
    await page.evaluate(
        "() => window.agenticMaps[0].map.jumpTo({ pitch: 60, bearing: 20 })")
    await settle(page)
    out["buildings"] = await page.evaluate("""() => {
      const m = window.agenticMaps[0].map;
      return {
        filter: JSON.stringify(m.getFilter('buildings-3d')),
        opacity: m.getPaintProperty('buildings-3d', 'fill-extrusion-opacity'),
        base: JSON.stringify(
          m.getPaintProperty('buildings-3d', 'fill-extrusion-base')),
        height: JSON.stringify(
          m.getPaintProperty('buildings-3d', 'fill-extrusion-height')),
        rendered: m.queryRenderedFeatures({ layers: ['buildings-3d'] }).length,
      };
    }""")
    # Flicker pair: two frames, IDENTICAL camera, explicit repaint between.
    frame_a = f"{SCRATCH}/pw-carto-bldg-frame-a.png"
    frame_b = f"{SCRATCH}/pw-carto-bldg-frame-b.png"
    await page.screenshot(path=frame_a)
    await page.evaluate(
        "() => new Promise((done) => {"
        " const m = window.agenticMaps[0].map;"
        " m.once('render', () => requestAnimationFrame(() => done()));"
        " m.triggerRepaint(); })")
    await page.screenshot(path=frame_b)
    image_a = Image.open(frame_a).convert("RGB")
    image_b = Image.open(frame_b).convert("RGB")
    box = (0, 100, image_a.width, image_a.height - 80)
    diff = ImageChops.difference(image_a.crop(box), image_b.crop(box))
    changed = sum(1 for p in diff.getdata() if max(p) > 8)
    out["flicker"] = {"changedPixels": changed,
                      "totalPixels": diff.width * diff.height}
    return out


async def main() -> None:
    errors: list[str] = []
    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(viewport={"width": 1280, "height": 860})
        page = await context.new_page()
        page.on(
            "console",
            lambda m: errors.append(m.text)
            if m.type == "error" and not any(a in m.text for a in ALLOWED_CONSOLE)
            else None,
        )
        page.on("pageerror", lambda e: errors.append(str(e)))

        themes = [await theme_pass(page, view)
                  for view in ("map-light", "map-dark")]
        composition = await composition_pass(page)
        quality = await render_quality_pass(page)
        globes = [await globe_pass(page, view)
                  for view in ("map-light", "map-dark")]
        roadwebs = [await roadweb_pass(page, view)
                    for view in ("map-light", "map-dark")]
        biomes = [await biome_pass(page, view)
                  for view in ("map-light", "map-dark")]
        await browser.close()

    out = {"themes": themes, "composition": composition, "globes": globes,
           "roadwebs": roadwebs, "biomes": biomes, "quality": quality,
           "consoleErrors": errors[:8]}
    print(json.dumps(out, indent=2, ensure_ascii=False))

    def theme_ok(t: dict) -> bool:
        c = CARTO[t["view"]]
        z = t["z65"]
        return (
            c["water"] in z["water"]
            and c["earth"] in z["earth"]
            and c["forest"] in z["landcoverColor"]
            # opaque THROUGH the band — the stock z5→7 fade-out is gone
            and '7,1' in z["landcoverOpacity"].replace(" ", "")
            and z["landuseMid"]
            and c["motorway"] in z["overviewMotorway"]
            # z4–5 hairline band runs the desaturated amber (same expression)
            and c["motorwayFaint"] in z["overviewMotorway"]
            and c["casing"] in z["overviewCasing"]
            and c["border"] in z["countryBorder"]
            and "population_rank" in z["cityColor"]
            and z["shieldLow"] == {"minzoom": 6.5, "maxzoom": 8}
            and z["renderedSubPrimary"] == 0
            and z["renderedMotorway"] > 0
            and z["renderedLandcover"] > 0
            and t["shieldsAt72"] > 0
            and t["subPrimaryAt105"] > 0
        )

    comp = composition

    def globe_continuity_ok(g: dict) -> bool:
        # Water strict — nothing touched it. Land re-derived for the biome
        # raster: both sides inside the sand→sage arc, unsaturated, close in
        # brightness; the 45° delta is a coarse backstop (see module doc).
        return (
            g["waterHueDelta"] < 25
            and g["landHueDelta"] < 45
            and all(30 <= h <= 140 for h in g["landHues"])
            and all(s <= 0.45 for s in g["landSats"])
            and abs(g["landVals"][0] - g["landVals"][1]) < 0.2
        )

    def biome_ok(b: dict, g: dict) -> bool:
        # Sahara vs interior France on the SAME globe rendering: sand is
        # warmer (lower hue) and brighter than sage, in both themes.
        # Calibration margins: Δhue 27°/30°, ΔV 0.08/0.03 (light/dark).
        europe_hue = g["landHues"][1]           # globe side of the pair
        europe_val = g["landVals"][1]
        return (
            europe_hue - b["saharaHue"] > 12
            and b["saharaVal"] > europe_val + 0.01
            and b["saharaSat"] <= 0.5
        )

    by_view = {g["view"]: g for g in globes}
    globe_ok = all(globe_continuity_ok(g) for g in globes)

    q = quality
    ortho_pitched = {int(z): n for z, n in
                     q["pitchedCover"].get("ortho", {}).items()}
    ortho_flat = {int(z): n for z, n in
                  q["flatCover"].get("ortho", {}).items()}
    streets_grid = {int(z): n for z, n in
                    q["gridViewCover"].get("streets", {}).items()}
    new_minor_width = _eval_zoom_interpolation(q["minorWidthExpr"], 16.5)
    b = q["buildings"]
    flicker_share = q["flicker"]["changedPixels"] / q["flicker"]["totalPixels"]
    verdict = {
        # Pitched reference view: a real pyramid — at least two distinct
        # aerial zooms, tiles at least two levels coarser than the nominal
        # z17 carrying the far field; the flat cover stays single-zoom.
        "lod_pitched_ok": (
            len(ortho_pitched) >= 2
            and min(ortho_pitched) <= max(ortho_pitched) - 2
            and sum(n for z, n in ortho_pitched.items()
                    if z < max(ortho_pitched)) >= 6
        ),
        "lod_flat_unchanged_ok": list(ortho_flat.keys()) == [17],
        # The grid regression: the vector streets must stay SINGLE-zoom at
        # the pitched close view — a coarse vector tile there re-draws the
        # ruler-straight white diagonals.
        "lod_vector_pinned_ok": len(streets_grid) == 1,
        # Close-zoom band: minor roads at z16.5 at least doubled, casing
        # gap in lockstep with the road width.
        "close_width_ok": (
            new_minor_width >= 2 * OLD_MINOR_WIDTH_Z165
            and q["minorGapExpr"] == q["minorWidthExpr"]
        ),
        # 3D buildings: building class only (no coplanar building_part
        # double-render), epsilon base, clamped height, opaque when up.
        "buildings_zfight_ok": (
            "building_part" not in b["filter"] and '"building"' in b["filter"]
            and b["opacity"] == 1
            and '"max"' in b["base"] and "0.1" in b["base"]
            and '"min"' in b["height"] and "400" in b["height"]
            and b["rendered"] > 0
        ),
        # Two frames, identical camera: per-pixel diff over the building
        # area of the stage stays ≈ 0.
        "buildings_flicker_ok": flicker_share < 0.001,
        **{t["view"] + "_ok": theme_ok(t) for t in themes},
        "hybrid_untouched_ok": (
            STOCK_WATER in comp["hybrid"]["water"]
            and not comp["hybrid"]["landuseMid"]
            and not comp["hybrid"]["shieldLow"]
            and comp["hybrid"]["roadsOver"]
            and comp["hybrid"]["trunkColor"] is not None
            and "#f6c445" in comp["hybrid"]["trunkColor"]
        ),
        "override_wins_ok": (
            "#dcdedf" in comp["override"]["water"]
            and not comp["override"]["landuseMid"]
            and not comp["override"]["shieldLow"]
            and "#c9c8c5" in comp["override"]["motorway"]
        ),
        "restore_ok": CARTO["map-light"]["water"] in comp["restored"],
        "globe_handover_ok": globe_ok,
        "globe_biomes_ok": all(
            biome_ok(b, by_view[b["view"]]) for b in biomes),
        "roadweb_z4_ok": all(
            g["roadsAtZ46"] > 0 and g["motorwayMinzoom"] == 4 for g in globes
        ) and all(r["roadsAtZ47"] > 0 for r in roadwebs),
        "console_ok": not errors,
    }
    ok = all(verdict.values())
    print(json.dumps(verdict, indent=2))
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
