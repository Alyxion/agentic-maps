"""Headless check: the URL carries the whole view, Google-style.

Also covers the regression it was reported alongside — a view must not go
blank at the edges once an on-demand region finishes minting.
"""
import asyncio, json, os, re, sys, tempfile
from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

# Karlsruhe: the report was "zoomed in, saw all data, then it went grey again".
KARLSRUHE = (8.4037, 49.0069)


async def main():
    out = {}
    launch_kwargs = {}
    if os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH"):
        launch_kwargs["executable_path"] = os.environ["AGENTIC_MAPS_RENDER_CHROMIUM_PATH"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # 1. A shared link restores camera, view and language.
        await page.goto(f"{BASE}/#@49.00690,8.40365,13z&view=map-light&lang=en", wait_until="load")
        await page.wait_for_function(
            "window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()", timeout=30000)
        await page.wait_for_timeout(2500)
        out["restored"] = await page.evaluate("""() => {
            const m = window.agenticMaps[0].map, c = m.getCenter();
            return { lat: +c.lat.toFixed(4), lon: +c.lng.toFixed(4),
                     zoom: +m.getZoom().toFixed(2), view: window.agenticMaps[0].view,
                     lang: document.getElementById('lang').value };
        }""")

        # 2. Moving rewrites it.
        await page.evaluate("() => window.agenticMaps[0].map.jumpTo({center:[13.405,52.52], zoom:11.5})")
        await page.wait_for_timeout(1200)
        out["writtenHash"] = await page.evaluate("() => location.hash")

        # 3. Round trip: what was written must parse back to the same view.
        await page.goto(BASE + "/" + out["writtenHash"], wait_until="load")
        await page.wait_for_function("window.agenticMaps && window.agenticMaps[0]", timeout=30000)
        await page.wait_for_timeout(2500)
        out["roundTrip"] = await page.evaluate("""() => {
            const m = window.agenticMaps[0].map, c = m.getCenter();
            return { lat: +c.lat.toFixed(3), lon: +c.lng.toFixed(3), zoom: +m.getZoom().toFixed(1) };
        }""")

        # 4. Karlsruhe, deep enough to trigger a mint. Sample the whole viewport
        #    (corners included) before and after, and require it stays drawn.
        await page.goto(f"{BASE}/#@49.00690,8.40365,13z&view=map-light&lang=de", wait_until="load")
        await page.wait_for_function("window.agenticMaps && window.agenticMaps[0]", timeout=30000)
        probe = """() => {
            const m = window.agenticMaps[0].map;
            const ids = m.getStyle().layers
                .filter((l) => l.source === 'streets' && l.type !== 'background').map((l) => l.id);
            const pts = [[120, 120], [1280, 120], [700, 450], [120, 780], [1280, 780]];
            return { depth: m.getSource('streets').maxzoom,
                     perCorner: pts.map((p) => m.queryRenderedFeatures(p, { layers: ids }).length) };
        }"""
        await page.wait_for_timeout(6000)
        out["karlsruheBefore"] = await page.evaluate(probe)
        await page.screenshot(path=f"{SCRATCH}/pw-karlsruhe-before.png")
        # Wait for the mint to land: the toast reports it, with a hard ceiling.
        try:
            await page.wait_for_function(
                "document.getElementById('status').textContent.startsWith('Straßendaten geladen')",
                timeout=180000)
            out["mintReported"] = await page.evaluate(
                "() => document.getElementById('status').textContent")
        except Exception:
            out["mintReported"] = None
        await page.wait_for_timeout(6000)
        out["karlsruheAfter"] = await page.evaluate(probe)
        await page.screenshot(path=f"{SCRATCH}/pw-karlsruhe-after.png")

        out["consoleErrors"] = errors[:3]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        await browser.close()

    r = out["restored"]
    ok = (abs(r["lat"] - 49.0069) < 0.01 and abs(r["lon"] - 8.4037) < 0.01
          and abs(r["zoom"] - 13) < 0.01 and r["view"] == "map-light" and r["lang"] == "en"
          and re.match(r"^#@52\.5\d*,13\.40\d*,11\.5z&view=map-light&lang=en$", out["writtenHash"])
          and abs(out["roundTrip"]["lat"] - 52.52) < 0.01
          and abs(out["roundTrip"]["zoom"] - 11.5) < 0.01
          # every sampled point of the viewport still draws something
          and all(n > 0 for n in out["karlsruheAfter"]["perCorner"]))
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

asyncio.run(main())
