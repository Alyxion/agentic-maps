"""Headless check: the aerial-quality auto-fallback (the New-York case).

Hybrid over a region whose only "imagery" is upscaled Blue Marble is
silly — the display must switch to the map view on its own and come back
when real imagery returns:

1. Stuttgart at z13 in hybrid stays hybrid — the DOP federation is native
   there (gap 0), no fallback, no toast.
2. Jump to New York at z13: the quality probe reports gap >= 3, the display
   auto-switches to Karte with the "Keine Luftbilder…" toast, and the
   layers control's active tile follows the DISPLAYED view.
3. Back to Stuttgart: the remembered choice (Hybrid) is restored
   automatically (gap <= 1).
4. Rapid pan across the sen2 coverage edge at z12: the settled-moveend
   debounce plus the 3/1 hysteresis allow at most ONE switch per direction
   — no flapping.
5. Manual view choice during fallback wins: picking Karte explicitly while
   fallen back clears the memory, so returning to Stuttgart does NOT flip
   back to Hybrid.

Run against the dev server: `python tools/verify_aerial_fallback.py`
(BASE via AGENTIC_MAPS_VERIFY_BASE, default http://127.0.0.1:8195).
"""
import asyncio
import json
import os
import sys
import tempfile

from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

STUTTGART = "#@48.7838,9.1829,13z&view=hybrid&lang=de"
NEW_YORK = [-74.0060, 40.7128]
# The sen2-europe coverage box starts at lon 0.678: lon 2 sits inside
# (native z12), lon -3 outside (world band only, gap 4 at z12).
SEN2_IN = [2.0, 50.0]
SEN2_OUT = [-3.0, 50.0]


async def current_view(page):
    return await page.evaluate("() => window.agenticMaps[0].view")


async def wait_view(page, prefix, timeout=8000):
    await page.wait_for_function(
        "(p) => window.agenticMaps[0].view.indexOf(p) === 0", arg=prefix,
        timeout=timeout)


async def jump(page, lnglat, zoom):
    await page.evaluate(
        "(a) => window.agenticMaps[0].map.jumpTo({ center: a.c, zoom: a.z })",
        {"c": lnglat, "z": zoom})


async def main():
    out = {}
    checks = {}
    launch_kwargs = {}
    if os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH"):
        launch_kwargs["executable_path"] = os.environ["AGENTIC_MAPS_RENDER_CHROMIUM_PATH"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # Every view transition (auto or manual) dispatches agentic-map:view.
        await page.add_init_script(
            "window.__views = [];"
            "document.addEventListener('agentic-map:view',"
            " (e) => window.__views.push(e.detail.view));")

        await page.goto(f"{BASE}/{STUTTGART}", wait_until="load")
        await page.wait_for_function(
            "() => window.agenticMaps && window.agenticMaps[0]"
            " && window.agenticMaps[0].map.getStyle()", timeout=30000)
        await page.wait_for_timeout(3000)      # settled move + probe round trip

        # ---- 1. native imagery: no fallback at home ----------------------
        out["stuttgart"] = await current_view(page)
        checks["nativeStaysHybrid"] = out["stuttgart"] == "hybrid"

        # ---- 2. New York at z13: auto-switch + toast ---------------------
        await jump(page, NEW_YORK, 13)
        try:
            await wait_view(page, "map-")
        except Exception:
            pass
        out["newYork"] = await page.evaluate("""() => ({
            view: window.agenticMaps[0].view,
            toast: document.getElementById('status').textContent,
            activeTile: (document.querySelector('#layers-grid .lp-tile.active')
                         || { dataset: {} }).dataset.choice,
        })""")
        ny = out["newYork"]
        checks["nyAutoSwitchesToMap"] = ny["view"].startswith("map-")
        checks["nyToastFired"] = "Keine Luftbilder" in ny["toast"]
        checks["layersFollowsDisplayedView"] = ny["activeTile"] == "map"
        await page.screenshot(path=f"{SCRATCH}/pw-aerial-fallback-ny.png")

        # ---- 3. back home: the chosen view returns on its own ------------
        await jump(page, [9.1829, 48.7838], 13)
        try:
            await wait_view(page, "hybrid")
        except Exception:
            pass
        out["restored"] = await current_view(page)
        checks["hybridRestored"] = out["restored"] == "hybrid"

        # ---- 4. rapid pan across the sen2 edge: no flapping ---------------
        await jump(page, SEN2_IN, 12)
        await page.wait_for_timeout(2000)
        await page.evaluate("() => { window.__views = []; }")
        # One synchronous burst — a drag fires moveend continuously and the
        # settled-move debounce must evaluate only the final position.
        await page.evaluate(
            "(pts) => { const m = window.agenticMaps[0].map;"
            " for (const p of pts) m.jumpTo({ center: p, zoom: 12 }); }",
            [SEN2_OUT, SEN2_IN, SEN2_OUT, SEN2_IN, SEN2_OUT])
        await page.wait_for_timeout(3500)      # settle + probe + switch
        out["panOut"] = await page.evaluate("() => window.__views.slice()")
        checks["oneSwitchOutbound"] = (
            len(out["panOut"]) == 1 and out["panOut"][0].startswith("map-"))
        await jump(page, SEN2_IN, 12)
        await page.wait_for_timeout(3500)
        out["panBack"] = await page.evaluate("() => window.__views.slice()")
        checks["oneSwitchInbound"] = (
            len(out["panBack"]) == 2 and out["panBack"][1] == "hybrid")

        # ---- 5. manual choice during fallback wins ------------------------
        await jump(page, NEW_YORK, 13)
        try:
            await wait_view(page, "map-")
        except Exception:
            pass
        await page.click("#layers-btn")
        await page.click('#layers-grid .lp-tile[data-choice="map"]')
        await page.wait_for_timeout(300)
        await jump(page, [9.1829, 48.7838], 13)
        await page.wait_for_timeout(3500)
        out["afterManual"] = await current_view(page)
        checks["manualChoiceSticks"] = out["afterManual"].startswith("map-")

        out["consoleErrors"] = [e for e in errors if "favicon" not in e][:5]
        checks["noConsoleErrors"] = not out["consoleErrors"]
        await browser.close()

    print(json.dumps(out, indent=2, ensure_ascii=False))
    print()
    failed = [k for k, v in checks.items() if not v]
    for k in sorted(checks):
        print(("PASS " if checks[k] else "FAIL ") + k)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(0 if not failed else 1)


asyncio.run(main())
