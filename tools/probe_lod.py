"""Instrumentation probe: pitched-view tile LOD (MapLibre v5 variable zoom).

Ground truth on the vendored bundle (v5.24.0):
  - map.setSourceTileLodParams(lodMinRadius, lodScale, sourceId?) exists and
    assigns source.calculateTileZoom = createCalculateTileZoomFunction(...).
  - The default calculateTileZoom (when a source has none) is
    createCalculateTileZoomFunction(9.314, 3).
  - Variable zoom only engages when the mercator covering-tiles provider's
    allowVariableZoom(transform, options) returns true:
        !!options.terrain || pitch > clamp(78.5 - fovLike/2, 0, 60)
    With the default fov (~36.87 deg) the threshold is exactly 60 deg — above
    the 55–62 deg the app actually uses, which is why the cover was
    single-zoom ({z17: 75}) in the owner's instrumentation.

This probe reports the per-source tile cover (zoom -> tile count) for a set
of camera setups, before and after the app-side LOD activation, plus
screenshots. Run with --raw to see the untuned engine (gate patch and
per-source params stripped), e.g. to reproduce the owner's single-zoom
evidence or the predecessor's "weird grid" of coarse vector tiles.

Usage: python tools/probe_lod.py [--raw]
"""
import asyncio
import json
import os
import sys
import tempfile

from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

COVER = """() => {
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

STRIP_LOD = """() => {
  // Reproduce the stock engine: undo the app's LOD activation.
  const m = window.agenticMaps[0].map;
  const provider = m.transform.getCoveringTilesDetailsProvider();
  const proto = Object.getPrototypeOf(provider);
  if (proto.__amOrigAllowVariableZoom) {
    proto.allowVariableZoom = proto.__amOrigAllowVariableZoom;
  }
  for (const key of Object.keys(m.style.tileManagers || {})) {
    const src = m.style.tileManagers[key].getSource();
    if (src) delete src.calculateTileZoom;
  }
  m._update && m._update(true);
}"""

# The predecessor's live experiment: LOD applied to EVERY source, including
# the vector streets — what produced the "weird grid" of white diagonals.
LOD_ALL = """() => {
  const m = window.agenticMaps[0].map;
  const provider = m.transform.getCoveringTilesDetailsProvider();
  const proto = Object.getPrototypeOf(provider);
  if (!proto.__amOrigAllowVariableZoom) {
    proto.__amOrigAllowVariableZoom = proto.allowVariableZoom;
  }
  proto.allowVariableZoom = function (tr, opts) {
    return !!(opts && opts.terrain) || tr.pitch > 45;
  };
  m.setSourceTileLodParams(3, 2);
  m._update && m._update(true);
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


async def scene(page, tag, lat, lon, zoom, pitch, view, mode):
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@{lat},{lon},{zoom}z&view={view}&lang=de",
                    wait_until="load")
    await settle(page, 1500)
    if mode == "raw":
        await page.evaluate(STRIP_LOD)
    elif mode == "lod-all":
        await page.evaluate(LOD_ALL)
    await page.evaluate(
        "([p]) => window.agenticMaps[0].map.jumpTo({ pitch: p })", [pitch])
    await settle(page)
    cover = await page.evaluate(COVER)
    shot = f"{SCRATCH}/pw-lod-{tag}-{mode}.png"
    await page.screenshot(path=shot)
    return {"tag": tag, "mode": mode, "view": view, "pitch": pitch,
            "cover": cover, "shot": shot}


LOD_ORTHO = """([minRadius, scale, gate]) => {
  const m = window.agenticMaps[0].map;
  const provider = m.transform.getCoveringTilesDetailsProvider();
  const proto = Object.getPrototypeOf(provider);
  if (!proto.__amOrigAllowVariableZoom) {
    proto.__amOrigAllowVariableZoom = proto.allowVariableZoom;
  }
  proto.allowVariableZoom = function (tr, opts) {
    return proto.__amOrigAllowVariableZoom.call(this, tr, opts)
      || tr.pitch > gate;
  };
  // Aerial raster only; the vector streets (and every other source) are
  // pinned to the uniform center zoom so coarse vector tiles can never
  // reach a close view.
  m.setSourceTileLodParams(minRadius, scale, 'ortho');
  for (const key of Object.keys(m.style.tileManagers || {})) {
    if (key === 'ortho') continue;
    const src = m.style.tileManagers[key].getSource();
    if (src) src.calculateTileZoom = (requested) => requested;
  }
  m._update && m._update(true);
}"""


async def sweep(page, params):
    results = []
    for (min_radius, scale, gate) in params:
        for (tag, lat, lon, zoom, pitch) in (
                ("hh-harbor", 53.54, 10.0, 16, 60),
                ("hh-grid", 53.52, 10.02, 14.5, 60)):
            await page.goto("about:blank")
            await page.goto(f"{BASE}/#@{lat},{lon},{zoom}z&view=hybrid&lang=de",
                            wait_until="load")
            await settle(page, 1200)
            await page.evaluate(LOD_ORTHO, [min_radius, scale, gate])
            await page.evaluate(
                "([p]) => window.agenticMaps[0].map.jumpTo({ pitch: p })",
                [pitch])
            await settle(page)
            cover = await page.evaluate(COVER)
            shot = (f"{SCRATCH}/pw-lod-sweep-{tag}-r{min_radius}"
                    f"-s{scale}-g{gate}.png")
            await page.screenshot(path=shot)
            results.append({"tag": tag, "minRadius": min_radius,
                            "scale": scale, "gate": gate,
                            "cover": cover, "shot": shot})
    return results


async def main():
    mode = "app"
    if "--raw" in sys.argv:
        mode = "raw"
    if "--lod-all" in sys.argv:
        mode = "lod-all"
    if "--sweep" in sys.argv:
        mode = "sweep"
    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 860})
        page = await context.new_page()
        if mode == "sweep":
            results = await sweep(page, [
                (9.314, 3, 45), (6, 3, 45), (4, 2.5, 45), (9.314, 2, 45),
            ])
            await browser.close()
            print(json.dumps(results, indent=2))
            return
        results = []
        # Hamburg harbor: the owner's fine-grid + dark-seam view.
        results.append(await scene(page, "hh-harbor", 53.54, 10.0, 16, 60,
                                   "hybrid", mode))
        # The "weird grid" reproduction view.
        results.append(await scene(page, "hh-grid", 53.52, 10.02, 14.5, 60,
                                   "hybrid", mode))
        # Flat control: cover must be single-zoom regardless of tuning.
        results.append(await scene(page, "hh-flat", 53.54, 10.0, 16, 0,
                                   "hybrid", mode))
        await browser.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
