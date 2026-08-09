"""Headless check: the four-stream UI polish pass.

1. POI thinning in the tilted view: past ~20° pitch only high-rank POIs
   survive (stations, museums, landmark classes, plus the tiles' own
   min_zoom ranking); rendered POI count at the owner's z16 Frankfurt view
   drops by >60% against the same pitched view with the stock filter, and
   station/museum-class POIs are still present. Flat view unchanged.
2. Context menu, Google-informed but ours: iconed card entries (Route von
   hier / Als Zwischenstopp / Route hierhin / Was ist hier? / In der Nähe
   suchen / Ort teilen / Hierhin zentrieren), coordinates header copies on
   click with "Kopiert" feedback, "Ort teilen" copies a centred URL,
   "Was ist hier?" opens the place detail card with street + ZIP,
   "In der Nähe suchen" focuses the biased search box, Escape closes,
   NO report-a-problem/data entries — and the menu keeps working while a
   route stands.
3. Chrome layout: the layers thumbnail grew to 72-80 px; theme toggle +
   settings gear live in the lower-LEFT corner cluster; top right keeps
   only the layers control.
4. Search UX: result rows carry Phosphor kind icons instead of the old
   "ORT"/"STRASSE" chips and show the ZIP in the detail line; open
   suggestions inside the viewport get subtle dot markers; hovering a row
   pings it (throttled) and raises its basemap label (am-place-highlight),
   un-highlighting on leave; the route panel's stop fields get the same
   ranked dropdown (#stop-suggest) — typing into the second stop and
   picking a row fills it and auto-routes.
"""
import asyncio
import json
import os
import re
import sys
import tempfile

from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

FRANKFURT = "#@50.1109,8.6821,16z&view=map-light&lang=de"

MENU_ENTRIES = ["Route von hier", "Als Zwischenstopp", "Route hierhin",
                "Was ist hier?", "In der Nähe suchen", "Ort teilen",
                "Hierhin zentrieren"]


async def mounted(page):
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()",
        timeout=30000)


async def open_menu(page, x=900, y=450):
    await page.mouse.click(x, y, button="right")
    await page.wait_for_selector("#map-menu.open", state="visible", timeout=5000)


async def poi_count(page):
    return await page.evaluate(
        "() => window.agenticMaps[0].map.getLayer('pois')"
        " ? window.agenticMaps[0].map.queryRenderedFeatures(undefined,"
        "     { layers: ['pois'] }).length : 0")


async def main():
    out = {}
    checks = {}
    launch_kwargs = {}
    if os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH"):
        launch_kwargs["executable_path"] = os.environ["AGENTIC_MAPS_RENDER_CHROMIUM_PATH"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            geolocation={"latitude": 48.5254, "longitude": 9.3466},
            permissions=["geolocation"])
        # Deterministic clipboard: the dev server is plain HTTP, where the
        # async clipboard API does not exist — mock it and record writes.
        await context.add_init_script("""
            window.__copied = [];
            Object.defineProperty(navigator, 'clipboard', {
              configurable: true,
              value: { writeText: (t) => { window.__copied.push(t);
                                           return Promise.resolve(); } },
            });
        """)
        page = await context.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        await page.goto(f"{BASE}/{FRANKFURT}", wait_until="load")
        await mounted(page)
        await page.wait_for_timeout(4000)

        # ============ 3. chrome layout =====================================
        out["chrome"] = await page.evaluate("""() => {
            const r = (id) => document.getElementById(id).getBoundingClientRect().toJSON();
            return {
              layers: r('layers-btn'),
              theme: r('btn-theme'),
              gear: r('settings-toggle'),
              themeInCluster: !!document.getElementById('btn-theme').closest('#corner-cluster'),
              gearInCluster: !!document.getElementById('settings-toggle').closest('#corner-cluster'),
              topRightChildren: [...document.getElementById('layers-control').children]
                .map((n) => n.id),
              clusterVertical: (() => {
                const c = document.getElementById('corner-cluster');
                return getComputedStyle(c).flexDirection === 'column';
              })(),
            };
        }""")
        ch = out["chrome"]
        checks["layersThumb72to80"] = 72 <= ch["layers"]["width"] <= 80
        checks["clusterLowerLeft"] = (ch["themeInCluster"] and ch["gearInCluster"]
            and ch["clusterVertical"]
            and ch["theme"]["x"] < 200 and ch["theme"]["y"] > 600
            and ch["gear"]["x"] < 200 and ch["gear"]["y"] > 600)
        checks["topRightOnlyLayers"] = ch["topRightChildren"] == ["layers-btn", "layers-picker"]
        vp = page.viewport_size
        await page.screenshot(path=f"{SCRATCH}/pw-ui-chrome-topright.png",
                              clip={"x": vp["width"] - 220, "y": 0, "width": 220, "height": 160})
        await page.screenshot(path=f"{SCRATCH}/pw-ui-chrome-lowerleft.png",
                              clip={"x": 0, "y": vp["height"] - 220, "width": 220, "height": 220})

        # ============ 2. context menu ======================================
        await open_menu(page)
        out["menu"] = await page.evaluate("""() => ({
            entries: [...document.querySelectorAll('#map-menu button[data-role]')]
              .map((b) => ({ role: b.dataset.role, label: b.textContent.trim(),
                             hasIcon: !!b.querySelector('.ph-icon svg') })),
            coord: document.getElementById('menu-coord-text').textContent,
            coordHasIcon: !!document.querySelector('#menu-coord .ph-icon svg'),
            isCard: document.getElementById('map-menu').classList.contains('card'),
        })""")
        m = out["menu"]
        labels = [e["label"] for e in m["entries"]]
        checks["menuEntriesComplete"] = labels == MENU_ENTRIES
        checks["menuAllIconed"] = all(e["hasIcon"] for e in m["entries"]) and m["coordHasIcon"]
        checks["menuIsCard"] = m["isCard"]
        checks["menuNoReportEntries"] = not any(
            re.search(r"(?i)problem|melden|daten|fehler", lbl) for lbl in labels)
        checks["menuCoordShown"] = bool(re.match(r"^\d+\.\d{5}, \d+\.\d{5}$", m["coord"]))
        await page.screenshot(path=f"{SCRATCH}/pw-ui-context-menu.png",
                              clip={"x": 860, "y": 410, "width": 320, "height": 420})

        # coordinates header: click = copy + "Kopiert" feedback
        await page.click("#menu-coord")
        await page.wait_for_timeout(200)
        out["copy"] = await page.evaluate("""() => ({
            copied: window.__copied.slice(-1)[0] || '',
            feedback: document.getElementById('menu-coord-text').textContent,
        })""")
        checks["menuCoordCopies"] = bool(
            re.match(r"^\d+\.\d{5}, \d+\.\d{5}$", out["copy"]["copied"]))
        checks["menuCopyFeedback"] = out["copy"]["feedback"] == "Kopiert"
        await page.wait_for_selector("#map-menu.open", state="hidden", timeout=5000)

        # Escape closes
        await open_menu(page)
        await page.keyboard.press("Escape")
        checks["menuEscapeCloses"] = await page.evaluate(
            "() => !document.getElementById('map-menu').classList.contains('open')")

        # Ort teilen: copies the shareable URL centred there
        await open_menu(page)
        await page.click('#map-menu button[data-role="share"]')
        await page.wait_for_timeout(300)
        out["share"] = await page.evaluate("() => window.__copied.slice(-1)[0] || ''")
        checks["shareCopiesUrl"] = (out["share"].startswith("http")
            and "#@" in out["share"] and "view=" in out["share"] and "lang=" in out["share"])

        # Was ist hier? -> place detail card with street + ZIP
        await open_menu(page)
        await page.click('#map-menu button[data-role="whatishere"]')
        await page.wait_for_selector("#place-card.open", state="visible", timeout=8000)
        try:
            await page.wait_for_function(
                "() => /\\d{5}/.test(document.getElementById('pc-address').textContent)",
                timeout=15000)
        except Exception:
            pass  # geocoder slow/unreachable — recorded below
        out["whatIsHere"] = await page.evaluate("""() => ({
            open: document.getElementById('place-card').classList.contains('open'),
            name: document.getElementById('pc-name').textContent,
            address: document.getElementById('pc-address').textContent,
            coords: document.getElementById('pc-coords').textContent,
        })""")
        w = out["whatIsHere"]
        checks["whatIsHereOpensCard"] = w["open"] and bool(w["coords"])
        checks["whatIsHereStreetZip"] = bool(re.search(r"\d{5}", w["address"]))
        await page.click("#pc-close")

        # In der Nähe suchen: focuses the search box, biased to the point
        await open_menu(page)
        await page.click('#map-menu button[data-role="nearby"]')
        await page.wait_for_timeout(200)
        out["nearby"] = await page.evaluate("""() => ({
            focused: document.activeElement === document.getElementById('q'),
            status: document.getElementById('status').textContent,
        })""")
        checks["nearbyFocusesSearch"] = (out["nearby"]["focused"]
            and out["nearby"]["status"].startswith("Suche bezogen auf"))

        # ============ 4a/4b. search dropdown ===============================
        await page.wait_for_timeout(1200)     # Nominatim politeness
        await page.fill("#q", "")
        await page.type("#q", "braubach", delay=40)
        await page.wait_for_selector("#results .result", state="visible", timeout=20000)
        out["search"] = await page.evaluate("""() => ({
            rows: [...document.querySelectorAll('#results .result')].map((r) => ({
              hasIcon: !!r.querySelector('.ric svg'),
              oldChip: !!r.querySelector('.kind'),
              title: (r.querySelector('b') || {}).textContent || '',
              sub: (r.querySelector('.sub') || {}).textContent || '',
            })),
            dots: document.querySelectorAll('.am-suggest-dot').length,
        })""")
        rows = out["search"]["rows"]
        checks["resultRowsIconed"] = (len(rows) > 0
            and all(r["hasIcon"] and not r["oldChip"] for r in rows))
        checks["resultRowZip"] = any(re.search(r"\d{5}", r["sub"]) for r in rows)
        checks["viewportSuggestDots"] = out["search"]["dots"] >= 1
        await page.screenshot(path=f"{SCRATCH}/pw-ui-search-dropdown.png",
                              clip={"x": 0, "y": 0, "width": 420, "height": 560})

        # hover = ping + raised basemap label; leave = unhighlight
        await page.hover("#results .result:first-child")
        await page.wait_for_timeout(250)
        out["hover"] = await page.evaluate("""() => ({
            ping: !!document.querySelector('.am-map-ping'),
            highlight: !!window.agenticMaps[0].map.getLayer('am-place-highlight'),
        })""")
        checks["hoverPingFires"] = out["hover"]["ping"]
        checks["hoverRaisesLabel"] = out["hover"]["highlight"]
        # scrub every row quickly: the throttle must keep pings from stacking
        for i in range(min(len(rows), 6)):
            await page.hover(f"#results .result:nth-child({i + 1})")
            await page.wait_for_timeout(40)
        out["scrubPings"] = await page.evaluate(
            "() => document.querySelectorAll('.am-map-ping').length")
        checks["hoverPingThrottled"] = out["scrubPings"] <= 2
        await page.hover("#q")
        await page.wait_for_timeout(200)
        checks["unhighlightOnLeave"] = await page.evaluate(
            "() => !window.agenticMaps[0].map.getLayer('am-place-highlight')")
        await page.click("#btn-clear")
        await page.wait_for_timeout(300)

        # ============ 4c. stop-field autocomplete ==========================
        await page.wait_for_timeout(1200)     # Nominatim politeness
        await open_menu(page)
        await page.click('#map-menu button[data-role="start"]')
        await page.wait_for_selector("#route-panel.open", state="visible", timeout=5000)
        second = "#stops-list .route-row:nth-child(2) input"
        await page.click(second)
        await page.type(second, "frankf", delay=60)
        await page.wait_for_selector("#stop-suggest.open .result",
                                     state="visible", timeout=20000)
        out["stopSuggest"] = await page.evaluate("""() => {
            const box = document.getElementById('stop-suggest');
            const input = document.querySelector('#stops-list .route-row:nth-child(2) input');
            const b = box.getBoundingClientRect(), i = input.getBoundingClientRect();
            return {
              rows: [...box.querySelectorAll('.result')].map((r) => ({
                title: (r.querySelector('b') || {}).textContent || '',
                hasIcon: !!r.querySelector('.ric svg'),
              })),
              underField: b.top >= i.bottom - 1 && Math.abs(b.left - i.left) < 40,
            };
        }""")
        ss = out["stopSuggest"]
        checks["stopSuggestOpens"] = (len(ss["rows"]) > 0
            and all(r["hasIcon"] for r in ss["rows"]))
        checks["stopSuggestUnderField"] = ss["underField"]
        checks["stopSuggestHasFrankfurt"] = any(
            "Frankfurt" in r["title"] for r in ss["rows"])
        await page.screenshot(path=f"{SCRATCH}/pw-ui-stop-suggest.png",
                              clip={"x": 0, "y": 0, "width": 420, "height": 560})

        # picking a row fills the stop and triggers the auto-route flow
        await page.click("#stop-suggest .result:first-child")
        await page.wait_for_selector("#route-result.open", state="visible", timeout=45000)
        out["routed"] = await page.evaluate("""() => ({
            endValue: document.querySelector('#stops-list .route-row:nth-child(2) input').value,
            result: document.getElementById('route-result').textContent.slice(0, 80),
            suggestClosed: !document.getElementById('stop-suggest').classList.contains('open'),
            navStart: document.getElementById('nav-start-row').classList.contains('open'),
        })""")
        r = out["routed"]
        checks["stopPickRoutes"] = (bool(r["endValue"]) and "min" in r["result"]
            and r["suggestClosed"] and r["navStart"])

        # the context menu must keep working during an active route
        await open_menu(page, 1000, 300)
        out["menuDuringRoute"] = await page.evaluate(
            "() => document.querySelectorAll('#map-menu button[data-role]').length")
        checks["menuWorksDuringRoute"] = out["menuDuringRoute"] == len(MENU_ENTRIES)
        await page.keyboard.press("Escape")
        await page.click("#btn-clear")
        await page.wait_for_timeout(500)

        # ============ 1. POI thinning, tilted vs flat ======================
        # Back at the owner's view: z16 Frankfurt, flat first.
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo({ center: [8.6821, 50.1109],"
            " zoom: 16, pitch: 0, bearing: 0 })")
        await page.wait_for_timeout(4000)
        flat = await poi_count(page)
        await page.screenshot(path=f"{SCRATCH}/pw-ui-poi-flat.png")

        # Tilt to ~55°; the pitchend listener thins the POI layer.
        await page.evaluate(
            "() => window.agenticMaps[0].map.easeTo({ pitch: 55, duration: 400 })")
        await page.wait_for_timeout(3500)
        thinned = await page.evaluate("""() => {
            const m = window.agenticMaps[0].map;
            const feats = m.queryRenderedFeatures(undefined, { layers: ['pois'] });
            const kinds = new Set();
            for (const f of feats) kinds.add(f.properties.kind);
            return { count: feats.length, kinds: [...kinds],
                     padding: m.getLayoutProperty('pois', 'text-padding') };
        }""")
        await page.screenshot(path=f"{SCRATCH}/pw-ui-poi-pitched-after.png")

        # "Before" for the record: same pitched view with the stock filter.
        # The styledata guard would instantly re-thin (that is the feature),
        # so the instance method is stubbed out for the measurement.
        await page.evaluate("""() => {
            const am = window.agenticMaps[0];
            am._syncPoiDensity = () => {};        // shadow the prototype method
            am.map.setFilter('pois', am._poiStockFilter);
            am.map.setLayoutProperty('pois', 'text-padding',
              am._poiStockPadding === undefined ? 2 : am._poiStockPadding);
        }""")
        await page.wait_for_timeout(2500)
        pitched_stock = await poi_count(page)
        await page.screenshot(path=f"{SCRATCH}/pw-ui-poi-pitched-before.png")
        # un-shadow and re-thin (the normal styledata/pitchend path)
        await page.evaluate("""() => {
            const am = window.agenticMaps[0];
            delete am._syncPoiDensity;            // prototype method is back
            am._poiThinnedFilter = null;          // force a fresh application
            am._syncPoiDensity();
        }""")
        await page.wait_for_timeout(1500)
        await page.evaluate(
            "() => window.agenticMaps[0].map.easeTo({ pitch: 0, duration: 300 })")
        await page.wait_for_timeout(3000)
        restored = await poi_count(page)

        out["poi"] = {"flat": flat, "pitchedThinned": thinned,
                      "pitchedStock": pitched_stock, "flatRestored": restored}
        checks["poiDropsOver60pct"] = (flat > 0
            and thinned["count"] <= 0.4 * flat
            and thinned["count"] <= 0.4 * max(pitched_stock, 1))
        checks["poiLandmarksSurvive"] = any(
            k in thinned["kinds"] for k in ("station", "museum"))
        checks["poiSurvivorsBreathe"] = thinned["padding"] == 14
        checks["poiFlatRestored"] = restored >= 0.8 * flat

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
