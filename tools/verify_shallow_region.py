"""Headless check: a region whose street data stops at z10 must still render a
map when the view goes deeper — over-zoomed, never a grey hole.

Runs in offline mode so nothing is minted: the point is what the map does with
the data it already has. Berlin is the control — it owns a z15 extract, so its
depth must stay at 15 and it must not 404 once.
"""
import asyncio, json, os, sys, tempfile
import httpx
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8095"
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

# Passau is inside the nationwide z10 extract and has no region of its own.
PLACES = [("passau", 13.4319, 48.5665, 13), ("berlin", 13.4050, 52.5200, 13)]


async def probe(page, name, lon, lat, zoom, misses):
    misses.clear()
    await page.evaluate("([lon, lat, z]) => window.agenticMaps[0].map.jumpTo({center:[lon,lat], zoom:z})",
                        [lon, lat, zoom])
    await page.wait_for_timeout(4000)   # moveend debounce + coverage round trip
    try:
        await page.wait_for_function("window.agenticMaps[0].map.areTilesLoaded()", timeout=20000)
    except Exception:
        pass
    state = await page.evaluate("""() => {
        const m = window.agenticMaps[0].map;
        const ids = m.getStyle().layers
            .filter((l) => l.source === 'streets' && l.type !== 'background').map((l) => l.id);
        const feats = m.queryRenderedFeatures({ layers: ids });
        const kinds = {};
        for (const f of feats) kinds[f.sourceLayer] = (kinds[f.sourceLayer] || 0) + 1;
        return { sourceMaxZoom: m.getSource('streets').maxzoom,
                 rendered: feats.length, byLayer: kinds };
    }""")
    await page.screenshot(path=f"{SCRATCH}/pw-{name}-z{zoom}.png")
    state["vectorTile404s"] = len(misses)
    return state


async def main():
    previous_mode = httpx.get(f"{BASE}/api/v1/maps/mode").json()["mode"]
    httpx.post(f"{BASE}/api/v1/maps/mode", json={"mode": "offline"})
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            misses, errors = [], []
            page.on("response", lambda r: misses.append(r.url)
                    if r.status >= 400 and "/vector/" in r.url else None)
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            await page.goto(BASE + "/", wait_until="load")
            await page.wait_for_function(
                "window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()", timeout=30000)
            await page.get_by_text("Karte", exact=True).click()
            await page.wait_for_timeout(1200)

            out = {name: await probe(page, name, lon, lat, zoom, misses)
                   for name, lon, lat, zoom in PLACES}
            out["consoleErrors"] = errors[:3]
            print(json.dumps(out, indent=2, ensure_ascii=False))
            await browser.close()
    finally:
        httpx.post(f"{BASE}/api/v1/maps/mode", json={"mode": previous_mode})

    # A short 404 burst on entering shallow territory is expected: the style
    # starts at the deepest bundle's zoom and the clamp lands once coverage
    # answers. What must not happen is an empty map.
    ok = (out["passau"]["sourceMaxZoom"] == 10 and out["passau"]["rendered"] > 0
          and out["berlin"]["sourceMaxZoom"] == 15 and out["berlin"]["vectorTile404s"] == 0)
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

asyncio.run(main())
