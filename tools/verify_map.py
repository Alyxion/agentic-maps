"""Headless check: does the map finish loading, and does search highlight work?"""
import asyncio, json, os, tempfile
from playwright.async_api import async_playwright

SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        pending, failed = set(), []
        page.on("request", lambda r: pending.add(r.url))
        page.on("requestfinished", lambda r: pending.discard(r.url))
        page.on("requestfailed", lambda r: (pending.discard(r.url), failed.append(r.url)))
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        await page.goto("http://127.0.0.1:8095/", wait_until="load")
        await page.wait_for_function("window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()", timeout=30000)
        await page.evaluate("""() => window.agenticMaps[0].map.jumpTo({center:[9.2925,48.5386], zoom:13})""")
        # Wait for MapLibre to report everything loaded, with a hard ceiling.
        try:
            await page.wait_for_function("window.agenticMaps[0].map.areTilesLoaded() && window.agenticMaps[0].map.isStyleLoaded()", timeout=45000)
            settled = True
        except Exception:
            settled = False
        highlighted = await page.evaluate("""() => {
            const ds = window.agenticMaps[0];
            const found = ds.highlightPlace('Metzingen');
            return { found, layer: !!ds.map.getLayer('am-place-highlight'),
                     rendered: ds.map.queryRenderedFeatures({layers:['am-place-highlight']}).length };
        }""")
        await page.screenshot(path=f"{SCRATCH}/pw-metzingen.png")
        print(json.dumps({"tilesSettled": settled, "stillPending": len(pending),
                          "failed": failed[:3], "highlight": highlighted,
                          "consoleErrors": errors[:3]}, ensure_ascii=False))
        await browser.close()

asyncio.run(main())
