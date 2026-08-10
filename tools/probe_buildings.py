"""Evidence probe: 3D-building z-fighting at the owner's Hamburg view.

Collects, at 53.535, 10.02 (giant-translucent-box + ground-shimmer report):
  - the `buildings` source-layer inventory: kind histogram, height stats,
    every feature above 400 m with its properties (the giant box suspect),
    and how many features carry min_height 0 (coplanar-base candidates);
  - a flicker pair: two frames at the IDENTICAL camera, forced repaint in
    between, per-pixel diff over the building area of the stage.

Usage: python tools/probe_buildings.py [view] (default map-light)
"""
import asyncio
import json
import os
import sys
import tempfile

from PIL import Image, ImageChops
from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

INVENTORY = """() => {
  const m = window.agenticMaps[0].map;
  const feats = m.querySourceFeatures('streets', { sourceLayer: 'buildings' });
  const kinds = {};
  const tall = [];
  let withMinHeight = 0, zeroBase = 0, total = 0, maxH = 0;
  const seen = new Set();
  for (const f of feats) {
    const p = f.properties || {};
    const key = f.id + '|' + JSON.stringify(p);
    if (seen.has(key)) continue;      // tiles overlap between zooms
    seen.add(key);
    total += 1;
    kinds[p.kind || '(none)'] = (kinds[p.kind || '(none)'] || 0) + 1;
    const h = +p.height || 0;
    if (h > maxH) maxH = h;
    if (p.min_height !== undefined) withMinHeight += 1; else zeroBase += 1;
    if (h > 400) {
      let at = null;
      try {
        const g = f.geometry;
        const c = g.type === 'Polygon' ? g.coordinates[0][0]
          : g.type === 'MultiPolygon' ? g.coordinates[0][0][0] : null;
        if (c) at = [+c[0].toFixed(4), +c[1].toFixed(4)];
      } catch (e) {}
      tall.push({ id: f.id, at, props: p });
    }
  }
  // Top of the height ladder, with footprint size — the giant-box hunt.
  const ranked = [];
  const seen2 = new Set();
  for (const f of feats) {
    const p = f.properties || {};
    const key2 = f.id + '|' + JSON.stringify(p);
    if (seen2.has(key2)) continue;
    seen2.add(key2);
    const h = +p.height || 0;
    if (h < 40) continue;
    let at = null, ringLen = 0;
    try {
      const g = f.geometry;
      const ring = g.type === 'Polygon' ? g.coordinates[0]
        : g.type === 'MultiPolygon' ? g.coordinates[0][0] : null;
      if (ring) {
        at = [+ring[0][0].toFixed(4), +ring[0][1].toFixed(4)];
        ringLen = ring.length;
      }
    } catch (e) {}
    ranked.push({ at, ringLen, props: p });
  }
  ranked.sort((x, y) => (+y.props.height || 0) - (+x.props.height || 0));
  return { total, kinds, maxHeight: maxH, withMinHeight, zeroBase,
           tall: tall.slice(0, 12), tallCount: tall.length,
           tallest: ranked.slice(0, 10) };
}"""


async def settle(page, ms=3500):
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && "
        "window.agenticMaps[0].map.isStyleLoaded()", timeout=40000)
    try:
        await page.wait_for_function(
            "window.agenticMaps[0].map.areTilesLoaded()", timeout=25000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def frame_pair(page, tag):
    """Two frames, identical camera, explicit repaint in between."""
    a = f"{SCRATCH}/pw-bldg-{tag}-frame-a.png"
    b = f"{SCRATCH}/pw-bldg-{tag}-frame-b.png"
    await page.screenshot(path=a)
    await page.evaluate(
        "() => new Promise((done) => {"
        " const m = window.agenticMaps[0].map;"
        " m.once('render', () => requestAnimationFrame(() => done()));"
        " m.triggerRepaint(); })")
    await page.screenshot(path=b)
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    # Building area of this camera: middle band of the stage, away from the
    # HUD chrome at top and the attribution at bottom.
    box = (0, 100, ia.width, ia.height - 80)
    diff = ImageChops.difference(ia.crop(box), ib.crop(box))
    pixels = list(diff.getdata())
    changed = sum(1 for p in pixels if max(p) > 8)
    return {"frameA": a, "frameB": b,
            "changedPixels": changed, "totalPixels": len(pixels),
            "changedShare": round(changed / len(pixels), 6)}


async def main():
    view = sys.argv[1] if len(sys.argv) > 1 else "map-light"
    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 860})
        page = await context.new_page()
        await page.goto(f"{BASE}/#@53.535,10.02,16.2z&view={view}&lang=de",
                        wait_until="load")
        await settle(page, 1500)
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo({ pitch: 60,"
            " bearing: 20 })")
        await settle(page)
        out = {"view": view}
        out["inventory"] = await page.evaluate(INVENTORY)
        out["opacity"] = await page.evaluate(
            "() => window.agenticMaps[0].map.getPaintProperty("
            "'buildings-3d', 'fill-extrusion-opacity')")
        out["flicker"] = await frame_pair(page, view)
        await browser.close()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
