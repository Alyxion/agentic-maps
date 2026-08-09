"""Prove offline mode really blocks every network path."""
import asyncio, json, os, tempfile
from playwright.async_api import async_playwright

SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

EXTERNAL = ("nominatim", "openstreetmap", "gibs.earthdata", "geoservices", "wms", "protomaps",
            "geodienste", "lgl-bw", "router.project-osrm", "geobasis", "githubusercontent")

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        seen = []
        page.on("request", lambda r: seen.append(r.url))
        base = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
        await page.goto(base + "/", wait_until="load")
        await page.wait_for_function("window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()", timeout=30000)

        # Switch to offline mode through the UI, exactly as a user would —
        # the 3-way Netzmodus switch now lives in the settings (gear) menu.
        await page.click("#settings-toggle")
        await page.wait_for_selector("#settings-menu.open", state="visible", timeout=5000)
        await page.click('#mode-switch button[data-mode="offline"]')
        # The page reloads so the style rebuilds against the sealed bundles.
        await page.wait_for_timeout(3000)
        await page.wait_for_function("window.agenticMaps && window.agenticMaps[0]", timeout=30000)
        state = await page.evaluate("() => fetch('/api/v1/maps/mode').then(r => r.json())")

        seen.clear()
        # Try everything that would normally hit the network.
        await page.evaluate("""() => window.agenticMaps[0].map.jumpTo({center:[2.35,48.85], zoom:14})""")
        await page.fill("#q", "Paris") if not await page.is_disabled("#q") else None
        await page.wait_for_timeout(4000)

        proxied = [u for u in seen if "/api/v1/maps/live/" in u or "/geocode" in u or "/route" in u
                   or "/vector/extract" in u]
        direct = [u for u in seen if any(host in u for host in EXTERNAL)]
        blocked = await page.evaluate("""async () => {
            const r = await fetch('/api/v1/maps/geocode', {method:'POST',
              headers:{'Content-Type':'application/json'}, body: JSON.stringify({q:'Berlin'})});
            const t = await fetch('/api/v1/maps/live/de-dop/14/8600/5600');
            return { geocode: r.status, liveTile: t.status,
                     searchDisabled: document.getElementById('q').disabled };
        }""")
        print(json.dumps({"serverMode": state["mode"], "attemptedProxyCalls": proxied[:5],
                          "directExternalCalls": direct[:5], "serverRefuses": blocked}, indent=1))
        await page.screenshot(path=f"{SCRATCH}/pw-offline.png")
        await browser.close()

asyncio.run(main())
