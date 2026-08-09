"""Headless check: the Google-Maps-style directions UX in web/index.html.

Exercises the compact-Google rework against a real running dev server
(`agentic_maps.devserver`) and a real routing backend — no mocking of
`/route`, exactly like the other tools/verify_*.py scripts:

  1. Place detail card: choosing a search result ("Dettingen an der Erms")
     docks a card under the search box whose address line carries a real
     ZIP (structured `address` from the geocoder / reverse geocoder), plus
     a subtle coordinates line and route shortcuts.
  2. Directions panel: the card's "Route" button prefills the destination;
     setting a start fires ALL FOUR mode routes in parallel and each mode
     tab shows its duration once its response lands (em-dash before).
     Clicking a tab draws that mode's route from the cache. Swap reverses
     the stops; "+ Ziel hinzufügen" appends a destination and reroutes.
  3. Turn-by-turn rows carry maneuver glyph + instruction + a secondary
     "41 Sek. (160 m)" line; clicking a step flies to the junction.
  4. Distance units: switching to Meilen in the options reformats the
     summary/steps (and back).
  5. Click semantics: plain click on open ground stays quiet; a named POI
     opens the same detail card; "Route von hier" feeds the trip model;
     a ground click dismisses the card.
  6. Locate-me drops the marker and flies to the (mocked) position.
  7. Offline mode: search disabled, mode tabs disabled, panel closed.
  8. Rework: the "Route"/"Löschen" buttons are GONE — any trip change
     (add/remove/reorder/✕/option) auto-routes, debounced.
  9. Multi-stop routes draw one coloured line + one time badge PER LEG, and
     the steps list heads each section "Abschnitt N · A → B" with a chip.
 10. Regression §0 (reworked with the globe overlay parity pass): at world
     zoom (z4) the globe takes the stage even WITH an active route — the old
     route-suppression stopgap is gone — and the route is on the sphere:
     ribbon meshes present (casing + one per leg) plus one total badge.
 11. Stops are drag-sortable (HTML5 dnd); glyphs re-derive after the drop.
 12. Settings gear replaces the always-visible Netzmodus switcher; theme
     button cycles hell → dunkel → automatisch and re-inks Karte.
 13. Exclusivity: results dropdown, detail card and route panel never stack;
     mid-routing a search pick becomes the destination. Pin-shaped markers
     anchor at the teardrop TIP (same geographic point at every zoom).

Requires a running dev server with a routing backend that actually answers
(AGENTIC_MAPS_ROUTING_BACKEND=osrm against the public demo is fine — truck
rides the car profile there and no alternates come back, which this script
treats as the honest degrade, not a failure).

Chromium resolution follows tools/verify_render.py's pattern:
`playwright install chromium`, or `AGENTIC_MAPS_RENDER_CHROMIUM_PATH`
pointed at a binary already on disk.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import urllib.request

from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

HQ = (48.5386, 9.2925)          # Firmenzentrale, Metzingen
AIRPORT = (48.6899, 9.2219)     # Flughafen Stuttgart
STUTTGART_CENTRE = (48.7829, 9.1815)
DETTINGEN = (48.5254, 9.3466)   # Dettingen an der Erms
FRANKFURT = (50.1109, 8.6821)   # far enough that OSRM offers real alternates

MODES = ["car", "truck", "walk", "bike"]
EMDASH = "—"


def set_server_mode(mode: str) -> None:
    req = urllib.request.Request(
        BASE + "/api/v1/maps/mode",
        data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).read()


async def wait_map(page) -> None:
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()",
        timeout=30000,
    )
    await page.wait_for_timeout(1200)


async def right_click_set(page, lon, lat, role, zoom=14):
    """Centre the camera on the point, right-click screen centre, pick the
    given role from the context menu — the user's own arbitrary-coordinate
    path, unchanged by this rework."""
    await page.evaluate(
        "([lon, lat, zoom]) => window.agenticMaps[0].map.jumpTo({ center: [lon, lat], zoom })",
        [lon, lat, zoom],
    )
    await page.wait_for_timeout(300)
    box = await page.evaluate(
        "() => { const el = document.getElementById('map'); "
        "const r = el.getBoundingClientRect(); return { x: r.width / 2, y: r.height / 2 }; }"
    )
    await page.mouse.click(box["x"], box["y"], button="right")
    await page.wait_for_selector("#map-menu.open", state="visible", timeout=5000)
    await page.click(f'#map-menu button[data-role="{role}"]')
    await page.wait_for_selector("#map-menu.open", state="hidden", timeout=5000)


async def tab_times(page) -> dict:
    return await page.evaluate(
        "() => Object.fromEntries([...document.querySelectorAll('#mode-tabs .mode-tab')]"
        ".map(t => [t.dataset.mode, t.querySelector('.mt-time').textContent]))"
    )


async def wait_all_tab_times(page, timeout=45000) -> dict:
    """All four tabs must leave the em-dash placeholder."""
    await page.wait_for_function(
        "() => [...document.querySelectorAll('#mode-tabs .mt-time')]"
        ".every(n => n.textContent !== '\\u2014')",
        timeout=timeout,
    )
    return await tab_times(page)


async def main() -> None:
    out: dict = {}
    errors: list[str] = []
    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            permissions=["geolocation"],
            geolocation={"latitude": HQ[0], "longitude": HQ[1]},
        )
        page = await context.new_page()
        phase = {"name": "online"}

        def on_console(message):
            if message.type != "error":
                return
            location = message.location or {}
            errors.append({"phase": phase["name"], "text": message.text,
                           "url": location.get("url", "")})

        page.on("console", on_console)
        page.on("pageerror", lambda exc: errors.append(
            {"phase": phase["name"], "text": "PAGEERROR: " + str(exc), "url": ""}))

        await page.goto(BASE + "/", wait_until="load")
        await wait_map(page)

        # -- 1: search result -> detail card with a real ZIP ---------------
        await page.fill("#q", "Dettingen an der Erms")
        await page.wait_for_selector("#results .result", state="visible", timeout=20000)
        await page.click("#results .result")
        await page.wait_for_selector("#place-card.open", state="visible", timeout=5000)
        await page.wait_for_function(
            "() => /\\d{5}/.test(document.getElementById('pc-address').textContent)",
            timeout=20000,
        )
        out["detailCard"] = {
            "name": await page.eval_on_selector("#pc-name", "el => el.textContent"),
            "address": await page.eval_on_selector("#pc-address", "el => el.textContent"),
            "coords": await page.eval_on_selector("#pc-coords", "el => el.textContent"),
        }
        await page.screenshot(path=f"{SCRATCH}/pw-directions-place-card.png")

        # -- 2: "Route" prefills the destination, start via right-click ----
        await page.click("#pc-route")
        await page.wait_for_selector("#route-panel.open", state="visible", timeout=5000)
        out["detailCard"]["cardClosedOnRoute"] = await page.evaluate(
            "() => !document.getElementById('place-card').classList.contains('open')"
        )
        dest = await page.evaluate(
            "() => { const i = document.querySelectorAll('#stops-list input'); "
            "return i[i.length - 1].value; }"
        )
        out["detailCard"]["destinationPrefilled"] = "Dettingen" in dest

        times_before = await tab_times(page)
        out["tabs"] = {"placeholderBeforeStart": all(v == EMDASH for v in times_before.values())}
        await right_click_set(page, HQ[1], HQ[0], "start")
        out["tabs"]["times"] = await wait_all_tab_times(page)
        await page.wait_for_selector("#route-result.open", state="visible", timeout=20000)

        # -- tab switching draws the selected mode from the cache ----------
        await page.click('#mode-tabs .mode-tab[data-mode="walk"]')
        await page.wait_for_function(
            "() => document.querySelectorAll('#route-steps .step').length > 0", timeout=20000
        )
        await page.wait_for_function(
            "() => document.querySelectorAll('.am-map-route-badge').length > 0", timeout=20000
        )
        out["tabs"]["walkActive"] = await page.evaluate(
            "() => document.querySelector('#mode-tabs .mode-tab.active').dataset.mode"
        )
        # Back to car — must be instant (cached), no new /route request.
        requests_before = []
        page.on("request", lambda r: requests_before.append(r.url) if "/maps/route" in r.url else None)
        await page.click('#mode-tabs .mode-tab[data-mode="car"]')
        await page.wait_for_timeout(800)
        out["tabs"]["cachedSwitchRefired"] = len(requests_before)

        # -- 3: step rows carry glyph + instruction + duration (distance) --
        step = await page.evaluate(
            """() => {
              const row = document.querySelector('#route-steps .step');
              return {
                glyph: !!row.querySelector('.glyph svg'),
                main: row.querySelector('.step-main').textContent,
                sub: (row.querySelector('.step-sub') || {}).textContent || '',
                count: document.querySelectorAll('#route-steps .step').length,
              };
            }"""
        )
        out["steps"] = step
        await page.click("#route-steps .step")
        await page.wait_for_timeout(300)
        await page.wait_for_function(
            "() => { const m = window.agenticMaps[0].map; return !m.isMoving(); }", timeout=8000
        )
        out["steps"]["zoomAfterClick"] = await page.evaluate(
            "() => window.agenticMaps[0].map.getZoom()"
        )
        await page.screenshot(path=f"{SCRATCH}/pw-directions-panel.png")

        # -- swap (2-stop case) --------------------------------------------
        values = await page.eval_on_selector_all("#stops-list input", "els => els.map(e => e.value)")
        await page.click("#btn-swap")
        swapped = await page.eval_on_selector_all("#stops-list input", "els => els.map(e => e.value)")
        out["swap"] = {"before": values, "after": swapped,
                       "reversed": swapped == list(reversed(values))}
        await wait_all_tab_times(page)

        # -- second destination --------------------------------------------
        await page.click("#btn-add-stop")
        out["multiStop"] = {
            "inputsAfterAdd": await page.eval_on_selector_all("#stops-list input", "els => els.length"),
            "swapHidden": await page.evaluate("() => document.getElementById('btn-swap').hidden"),
        }
        await right_click_set(page, AIRPORT[1], AIRPORT[0], "end")
        out["multiStop"]["times"] = await wait_all_tab_times(page)
        await page.wait_for_selector("#route-result.open", state="visible", timeout=20000)

        # -- 4: units toggle ------------------------------------------------
        # Asserted on the badge (its text ends with the distance) and on the
        # summary's innerHTML — textContent glues "17 mi" to the following
        # "ohne Verkehrslage" span, so a \b after "mi" can never match there.
        await page.click("#btn-route-options")
        await page.wait_for_selector("#route-options.open", state="visible", timeout=5000)
        await page.check('#units-opt input[value="mi"]')
        await page.wait_for_function(
            "() => /\\d(\\.\\d)? (mi|ft)$/.test("
            "(document.querySelector('.am-map-route-badge span') || {textContent: ''}).textContent)",
            timeout=5000,
        )
        out["units"] = {
            "resultInMiles": await page.eval_on_selector("#route-result", "el => el.innerHTML"),
            "stepSubInMiles": await page.evaluate(
                "() => (document.querySelector('#route-steps .step-sub') || {}).textContent || ''"
            ),
            "badgeInMiles": await page.evaluate(
                "() => (document.querySelector('.am-map-route-badge span') || {}).textContent || ''"
            ),
        }
        await page.check('#units-opt input[value="auto"]')
        await page.wait_for_function(
            "() => /\\d(\\.\\d)? (km|m)$/.test("
            "(document.querySelector('.am-map-route-badge span') || {textContent: ''}).textContent)",
            timeout=5000,
        )
        out["units"]["backToKm"] = True

        # The Löschen button is gone — the search box's Zurücksetzen clears
        # the whole trip (per-field ✕ is covered in the rework section below).
        await page.click("#btn-clear")
        await page.wait_for_selector("#route-panel.open", state="hidden", timeout=5000)
        out["clear"] = {"tabsReset": all(
            v == EMDASH for v in (await tab_times(page)).values())}

        # -- 6: locate me ---------------------------------------------------
        await page.click("#btn-locate")
        await page.wait_for_function(
            "document.querySelector('.am-map-me') !== null", timeout=10000
        )
        await page.wait_for_function(
            "() => { const m = window.agenticMaps[0].map; "
            "return !m.isMoving() && !m.isZooming() && !m.isEasing(); }",
            timeout=8000,
        )
        await page.wait_for_timeout(300)
        center = await page.evaluate(
            "() => { const m = window.agenticMaps[0].map; const c = m.getCenter(); "
            "return { lat: c.lat, lon: c.lng }; }"
        )
        out["locateMe"] = {
            "markerAppeared": True,
            "nearExpected": abs(center["lat"] - HQ[0]) < 0.05 and abs(center["lon"] - HQ[1]) < 0.05,
        }

        # -- 5: click semantics --------------------------------------------
        await page.evaluate(
            "([lon, lat]) => window.agenticMaps[0].map.jumpTo({ center: [lon, lat], zoom: 15.5 })",
            [STUTTGART_CENTRE[1], STUTTGART_CENTRE[0]],
        )
        await page.wait_for_function(
            "() => window.agenticMaps[0].map.areTilesLoaded()", timeout=30000
        )
        await page.wait_for_timeout(1000)

        ground = await page.evaluate(
            """() => {
              const m = window.agenticMaps[0].map;
              for (let x = 450; x < 1100; x += 60) {
                for (let y = 200; y < 700; y += 60) {
                  const f = m.queryRenderedFeatures([[x - 14, y - 14], [x + 14, y + 14]]);
                  if (!f.some(ft => (ft.layer && ft.layer['source-layer']) === 'pois'
                      || (ft.layer.id || '').startsWith('route-'))) return { x, y };
                }
              }
              return null;
            }"""
        )
        pins_before = await page.eval_on_selector_all(".am-map-search-pin", "els => els.length")
        await page.mouse.click(ground["x"], ground["y"])
        await page.wait_for_timeout(1000)
        out["clickSemantics"] = {
            "groundCardShown": await page.evaluate(
                "() => document.getElementById('place-card').classList.contains('open')"
            ),
            "groundPinDelta": (
                await page.eval_on_selector_all(".am-map-search-pin", "els => els.length")
            ) - pins_before,
        }

        poi = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], m = w.map;
              const feats = m.queryRenderedFeatures().filter(f =>
                f.layer && f.layer['source-layer'] === 'pois' && f.properties &&
                (f.properties['name:' + w.lang] || f.properties.name) &&
                f.geometry.type === 'Point');
              for (const f of feats) {
                const p = m.project(f.geometry.coordinates);
                if (p.x > 450 && p.x < 1200 && p.y > 140 && p.y < 760)
                  return { x: p.x, y: p.y,
                           name: f.properties['name:' + w.lang] || f.properties.name };
              }
              return null;
            }"""
        )
        out["clickSemantics"]["poiFound"] = bool(poi)
        if poi:
            await page.mouse.click(poi["x"], poi["y"])
            await page.wait_for_selector("#place-card.open", state="visible", timeout=10000)
            out["clickSemantics"]["poiName"] = poi["name"]
            out["clickSemantics"]["cardName"] = await page.eval_on_selector(
                "#pc-name", "el => el.textContent"
            )
            # Reverse geocode fills the address line in-flight.
            await page.wait_for_function(
                "() => document.getElementById('pc-address').textContent.length > 0",
                timeout=15000,
            )
            out["clickSemantics"]["poiAddress"] = await page.eval_on_selector(
                "#pc-address", "el => el.textContent"
            )
            await page.screenshot(path=f"{SCRATCH}/pw-directions-poi-card.png")
            await page.click("#pc-route-from")
            await page.wait_for_selector("#route-panel.open", state="visible", timeout=5000)
            out["clickSemantics"]["startPrefilled"] = await page.evaluate(
                "() => document.querySelector('#stops-list input').value"
            ) == poi["name"]
            await page.mouse.click(ground["x"], ground["y"])
            await page.wait_for_timeout(800)
            out["clickSemantics"]["dismissedCleanly"] = await page.evaluate(
                "() => !document.getElementById('place-card').classList.contains('open')"
            )
            await page.click("#btn-clear")

        # ==============================================================
        # Rework: auto-route, per-leg colors, zoom-out regression, drag
        # reorder, per-field ✕, gear menu, theme toggle, exclusivity,
        # pin anchoring.
        # ==============================================================
        await page.goto(BASE + "/#@48.61,9.26,10z&view=map-light&lang=de", wait_until="load")
        await wait_map(page)

        # -- the Route / Löschen buttons are GONE ---------------------------
        out["buttons"] = await page.evaluate(
            "() => ({ routeGo: !!document.getElementById('btn-route-go'),"
            " routeClear: !!document.getElementById('btn-route-clear') })"
        )

        # -- auto-route: two right-click stops, NO button press -------------
        await right_click_set(page, HQ[1], HQ[0], "start")
        await right_click_set(page, AIRPORT[1], AIRPORT[0], "end")
        await page.wait_for_selector("#route-result.open", state="visible", timeout=45000)
        out["autoRoute"] = {"routedWithoutButton": True,
                            "times": await wait_all_tab_times(page)}

        # -- third stop (via) => one coloured line + one badge PER LEG ------
        await right_click_set(page, STUTTGART_CENTRE[1], STUTTGART_CENTRE[0], "via")
        await page.wait_for_timeout(600)
        await wait_all_tab_times(page)
        await page.wait_for_function(
            "() => document.querySelectorAll('.am-map-route-badge').length >= 2",
            timeout=45000,
        )
        await page.wait_for_function(
            "() => !window.agenticMaps[0].map.isMoving()", timeout=15000
        )
        out["legs"] = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], m = w.map;
              const layers = m.getStyle().layers.filter((l) => /^route-.*-leg\\d+$/.test(l.id));
              return {
                lineLayers: layers.length,
                colors: layers.map((l) => m.getPaintProperty(l.id, 'line-color')),
                badges: document.querySelectorAll('.am-map-route-badge').length,
                headers: [...document.querySelectorAll('#route-steps .step-leg')]
                  .map((n) => n.textContent.trim()),
                chips: document.querySelectorAll('#route-steps .leg-chip').length,
              };
            }"""
        )
        await page.screenshot(path=f"{SCRATCH}/pw-directions-multileg.png")

        # -- §0: at world zoom the GLOBE carries the route now ---------------
        # The old stopgap (globe suppressed while a route stands) is gone;
        # the route must be on the sphere instead: ribbon meshes (casing +
        # one per leg) and exactly one total-duration badge.
        await page.wait_for_function("() => !!window.agenticGlobe", timeout=20000)
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo({ center: [9.4, 50.0], zoom: 4 })"
        )
        await page.wait_for_timeout(2500)
        out["zoomOut"] = await page.evaluate(
            """() => {
              const m = window.agenticMaps[0].map;
              const g = window.agenticGlobe;
              const globe = document.querySelector('.am-globe');
              return {
                zoom: +m.getZoom().toFixed(1),
                flatCanvasHidden: m.getCanvas().style.visibility === 'hidden',
                globeShown: globe ? getComputedStyle(globe).display !== 'none' : false,
                globeRouteMeshes: g ? g.overlays.children.length : 0,
                globeBadges: g ? g.badgeBillboards.length : 0,
              };
            }"""
        )
        await page.screenshot(path=f"{SCRATCH}/pw-directions-z4-route.png")
        # Back to city zoom: the flat legs/badges must be exactly as left.
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo({ center: [9.2, 48.7], zoom: 10 })"
        )
        await page.wait_for_timeout(1500)
        out["zoomOut"]["flatRestored"] = await page.evaluate(
            """() => {
              const m = window.agenticMaps[0].map;
              return {
                legLayers: m.getStyle().layers
                  .filter((l) => /^route-.*-leg\\d+$/.test(l.id)).length,
                flatBadges: document.querySelectorAll(
                  '.am-map-route-badge:not(.am-globe-route-badge)').length,
                flatCanvasVisible: m.getCanvas().style.visibility !== 'hidden',
              };
            }"""
        )

        # -- drag to reorder ------------------------------------------------
        order_before = await page.eval_on_selector_all(
            "#stops-list input", "els => els.map(e => e.value)")
        glyphs_before = await page.evaluate(
            "() => [...document.querySelectorAll('#stops-list .stop-glyph')]"
            ".map((n) => n.innerHTML)")
        route_calls = []
        page.on("request", lambda r: route_calls.append(r.url)
                if "/maps/route" in r.url else None)
        rows = page.locator("#stops-list .route-row")
        src = await rows.nth(0).locator(".stop-glyph").bounding_box()
        dst = await rows.nth(2).bounding_box()
        await page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
        await page.mouse.down()
        await page.mouse.move(dst["x"] + dst["width"] / 2,
                              dst["y"] + dst["height"] * 0.8, steps=12)
        await page.mouse.move(dst["x"] + dst["width"] / 2,
                              dst["y"] + dst["height"] * 0.85, steps=3)
        await page.mouse.up()
        await page.wait_for_timeout(700)
        order_after = await page.eval_on_selector_all(
            "#stops-list input", "els => els.map(e => e.value)")
        expected = [order_before[1], order_before[2], order_before[0]]
        drag_method = "mouse"
        if order_after != expected:
            # Raw mouse did not produce the HTML5 dnd sequence in this build —
            # Playwright's dedicated dnd is the honest fallback.
            drag_method = "drag_and_drop"
            await page.drag_and_drop(
                "#stops-list .route-row:first-child .stop-glyph",
                "#stops-list .route-row:last-child",
                target_position={"x": 60, "y": 26},
            )
            await page.wait_for_timeout(700)
            order_after = await page.eval_on_selector_all(
                "#stops-list input", "els => els.map(e => e.value)")
        await page.wait_for_timeout(1200)
        glyphs_after = await page.evaluate(
            "() => [...document.querySelectorAll('#stops-list .stop-glyph')]"
            ".map((n) => n.innerHTML)")
        out["drag"] = {
            "method": drag_method,
            "before": order_before, "after": order_after,
            "orderChanged": order_after == expected,
            # First row keeps the circle, last row keeps the map pin — the
            # glyph belongs to the position, not to the travelling row.
            "glyphsRederived": (glyphs_after[0] == glyphs_before[0]
                                and glyphs_after[-1] == glyphs_before[-1]),
            "rerouted": len(route_calls) > 0,
        }

        # -- per-field ✕: remove a via, then empty a 2-stop field -----------
        calls_before = len(route_calls)
        await page.click("#stops-list .route-row:nth-child(2) .drop")
        await page.wait_for_timeout(1500)
        out["fieldClear"] = {
            "rowsAfterRemove": await page.eval_on_selector_all(
                "#stops-list input", "els => els.length"),
            "reroutedAfterRemove": len(route_calls) > calls_before,
        }
        await page.click("#stops-list .route-row:nth-child(2) .drop")
        await page.wait_for_function(
            "() => window.agenticMaps[0].routes.length === 0", timeout=10000)
        out["fieldClear"].update({
            "emptiedValue": await page.evaluate(
                "() => document.querySelectorAll('#stops-list input')[1].value"),
            "rowsKept": await page.eval_on_selector_all(
                "#stops-list input", "els => els.length"),
            "resultClosed": await page.evaluate(
                "() => !document.getElementById('route-result').classList.contains('open')"),
        })
        await page.screenshot(path=f"{SCRATCH}/pw-directions-panel-nobuttons.png")

        # -- settings gear: Netzmodus moved out of the chrome ---------------
        out["gear"] = {
            "menuClosedInitially": await page.evaluate(
                "() => !document.getElementById('settings-menu').classList.contains('open')"),
            "inlineSwitcherGone": await page.evaluate(
                "() => { const ms = document.getElementById('mode-switch');"
                " return !!ms && !!ms.closest('#settings-menu') && ms.offsetParent === null; }"),
        }
        await page.click("#settings-toggle")
        await page.wait_for_selector("#settings-menu.open", state="visible", timeout=5000)
        out["gear"]["modeButtons"] = await page.eval_on_selector_all(
            "#settings-menu #mode-switch button[data-mode]",
            "els => els.map(e => e.dataset.mode)")
        out["gear"]["activeMode"] = await page.evaluate(
            "() => { const b = document.querySelector('#mode-switch button.active');"
            " return b ? b.dataset.mode : ''; }")
        await page.screenshot(path=f"{SCRATCH}/pw-directions-gear.png")
        await page.mouse.click(700, 500)
        await page.wait_for_timeout(300)
        out["gear"]["closesOnOutsideClick"] = await page.evaluate(
            "() => !document.getElementById('settings-menu').classList.contains('open')")

        # -- theme toggle: (auto) → hell → dunkel → auto, follows the OS ----
        async def theme_state():
            return await page.evaluate(
                "() => ({ title: document.getElementById('btn-theme').title,"
                " view: window.agenticMaps[0].view,"
                " flavor: window.agenticMaps[0].currentFlavor,"
                " hash: location.hash })")
        themes = [await theme_state()]                 # fresh profile: auto
        await page.click("#btn-theme")                 # -> hell
        await page.wait_for_timeout(500)
        themes.append(await theme_state())
        await page.click("#btn-theme")                 # -> dunkel
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-dark'", timeout=15000)
        await page.wait_for_function(
            "() => window.agenticMaps[0].map.isStyleLoaded()", timeout=20000)
        await page.wait_for_timeout(1500)
        themes.append(await theme_state())
        await page.screenshot(path=f"{SCRATCH}/pw-directions-dark-karte.png")
        await page.click("#btn-theme")                 # -> auto (headless: hell)
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-light'", timeout=15000)
        await page.wait_for_timeout(500)
        themes.append(await theme_state())
        await page.emulate_media(color_scheme="dark")  # auto follows live
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-dark'", timeout=15000)
        themes.append(await theme_state())
        await page.emulate_media(color_scheme="light")
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-light'", timeout=15000)
        out["theme"] = {"sequence": themes}

        # -- exclusivity + pin-tip anchoring --------------------------------
        # Leave routing mode first: with the panel open a pick would (by
        # design) become the destination instead of opening the detail card.
        await page.click("#btn-clear")
        await page.wait_for_selector("#route-panel.open", state="hidden", timeout=5000)
        await page.fill("#q", "Hannover")
        await page.wait_for_selector("#results .result", state="visible", timeout=20000)
        await page.click("#results .result")
        await page.wait_for_timeout(150)
        out["exclusive"] = {
            "resultsClosedAfterPick": await page.evaluate(
                "() => !document.getElementById('results').classList.contains('open')"),
        }
        await page.wait_for_selector("#place-card.open", state="visible", timeout=10000)
        coords_text = await page.eval_on_selector("#pc-coords", "el => el.textContent")
        pin_lat, pin_lon = [float(x) for x in coords_text.split(",")]
        await page.wait_for_timeout(2500)   # flyTo + label-highlight settle

        async def pin_offset(zoom):
            await page.evaluate(
                "([lon, lat, z]) => window.agenticMaps[0].map.jumpTo("
                "{ center: [lon, lat], zoom: z })",
                [pin_lon, pin_lat, zoom],
            )
            await page.wait_for_timeout(800)
            return await page.evaluate(
                """([lat, lon]) => {
                  const m = window.agenticMaps[0].map;
                  const p = m.project([lon, lat]);
                  const el = document.querySelector('.am-map-search-pin');
                  if (!el) return null;
                  const r = el.getBoundingClientRect();
                  const box = m.getContainer().getBoundingClientRect();
                  return { dx: +((r.left + r.width / 2 - box.left) - p.x).toFixed(2),
                           dy: +((r.bottom - box.top) - p.y).toFixed(2) };
                }""",
                [pin_lat, pin_lon],
            )
        out["pinAnchor"] = {"z12": await pin_offset(12), "z16": await pin_offset(16)}

        # Mid-routing, a search pick becomes the destination — either/or.
        await page.click("#pc-route")
        await page.wait_for_selector("#route-panel.open", state="visible", timeout=5000)
        out["exclusive"]["cardClosedOnRoute"] = await page.evaluate(
            "() => !document.getElementById('place-card').classList.contains('open')")
        await page.fill("#q", "Berlin")
        await page.wait_for_selector("#results .result", state="visible", timeout=20000)
        await page.click("#results .result")
        await page.wait_for_timeout(300)
        out["exclusive"].update({
            "resultsClosedMidRouting": await page.evaluate(
                "() => !document.getElementById('results').classList.contains('open')"),
            "panelStillOpen": await page.evaluate(
                "() => document.getElementById('route-panel').classList.contains('open')"),
            "noCardMidRouting": await page.evaluate(
                "() => !document.getElementById('place-card').classList.contains('open')"),
            "destBecamePick": await page.evaluate(
                "() => { const i = document.querySelectorAll('#stops-list input');"
                " return i[i.length - 1].value; }"),
        })
        await page.click("#btn-clear")
        await page.wait_for_timeout(300)

        # -- alternates: promote round-trips conserve the selectors ---------
        # Owner bug: switch to an alternate and back — after that round-trip
        # the light badge (the map's only clickable selector; the dark
        # primary badge is pointer-events:none) was gone for good and no
        # further switching was possible. Cause: promoteAlternate's shallow
        # copies inherited the live route's _altBadgeMarkers BEFORE
        # _removeRoute nulled them, so two promotes later the resurfacing
        # copy carried a dead marker that fooled _drawAlternates'
        # already-drawn guard. Triple round-trip (A→B→A→B), asserting the
        # FULL selector set after every swap.
        await right_click_set(page, DETTINGEN[1], DETTINGEN[0], "start")
        await right_click_set(page, FRANKFURT[1], FRANKFURT[0], "end")
        await page.wait_for_selector("#route-result.open", state="visible", timeout=45000)
        # Alternates are asserted on the car profile; the tab is only
        # clickable now that the route panel is open (it is the default,
        # so this is usually a no-op).
        if await page.evaluate(
                "() => document.querySelector('#mode-tabs .mode-tab.active').dataset.mode") != "car":
            await page.click('#mode-tabs .mode-tab[data-mode="car"]')
            await page.wait_for_timeout(800)
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('#route-alternates .alt-row').length >= 2",
                timeout=20000)
        except Exception:
            pass    # zero alternates = the backend's honest degrade (see below)
        await page.wait_for_timeout(600)
        alt_state_js = """() => {
          const w = window.agenticMaps[0], m = w.map;
          const r = w.routes[0] || null;
          return {
            alternates: r ? (r.alternates || []).length : -1,
            primary: r ? +r.duration_min.toFixed(2) : null,
            altDurations: r ? (r.alternates || []).map(a => +a.duration_min.toFixed(2)) : [],
            rows: document.querySelectorAll('#route-alternates .alt-row').length,
            lightBadges: [...document.querySelectorAll('.am-map-route-badge-light')]
              .filter((el) => el.isConnected).length,
            altLineLayers: m.getStyle().layers
              .filter((l) => /^route-.*-alt-\\d+$/.test(l.id)).length,
            steps: document.querySelectorAll('#route-steps .step').length,
          };
        }"""
        initial = await page.evaluate(alt_state_js)
        out["alternates"] = {"initial": initial, "swaps": []}
        if initial["alternates"] > 0:
            n = initial["alternates"]
            candidates = sorted([initial["primary"], *initial["altDurations"]])
            steps_by_duration = {initial["primary"]: initial["steps"]}
            for _swap in range(3):
                before = await page.evaluate(alt_state_js)
                # First non-active row == alternates[0]: the one being promoted.
                expected = before["altDurations"][0]
                await page.click("#route-alternates .alt-row:not(.active)")
                await page.wait_for_function(
                    "(d) => { const r = window.agenticMaps[0].routes[0];"
                    " return r && +r.duration_min.toFixed(2) === d; }",
                    arg=expected, timeout=10000)
                await page.wait_for_timeout(1100)    # fitBounds + re-render settle
                state = await page.evaluate(alt_state_js)
                known = steps_by_duration.setdefault(state["primary"], state["steps"])
                state["ok"] = (
                    state["primary"] == expected                        # durations swapped
                    and sorted([state["primary"], *state["altDurations"]]) == candidates
                    and state["rows"] == n + 1                          # panel rows constant
                    and state["lightBadges"] == n                       # map selectors constant
                    and state["altLineLayers"] == n                     # dimmed lines rendered
                    and state["steps"] == known and state["steps"] > 0  # steps follow the primary
                )
                out["alternates"]["swaps"].append(state)
            alternates_ok = (len(out["alternates"]["swaps"]) == 3
                             and all(s["ok"] for s in out["alternates"]["swaps"]))
        else:
            # No alternates from this backend (public OSRM demo/truck): the
            # honest degrade the docstring describes, not a failure.
            out["alternates"]["degraded"] = True
            alternates_ok = True
        await page.screenshot(path=f"{SCRATCH}/pw-directions-alternates.png")
        await page.click("#btn-clear")
        await page.wait_for_timeout(300)

        # Live-tile 404s (blue-marble / de-dop coverage edges) are server tile
        # availability, present before this UI phase — reported, not gated.
        def app_errors(phase_name):
            return [e for e in errors
                    if e["phase"] == phase_name and "/api/v1/maps/live/" not in e["url"]]

        online_errors = app_errors("online")
        out["tileErrorCount"] = len(errors) - len(app_errors("online")) - len(app_errors("offline"))

        # -- 7: offline degradation ----------------------------------------
        phase["name"] = "offline"
        set_server_mode("offline")
        await page.goto(BASE + "/", wait_until="load")
        await wait_map(page)
        out["offline"] = {
            "searchDisabled": await page.evaluate("() => document.getElementById('q').disabled"),
            "tabsDisabled": await page.evaluate(
                "() => [...document.querySelectorAll('#mode-tabs .mode-tab')].every(t => t.disabled)"
            ),
            "panelClosed": await page.evaluate(
                "() => !document.getElementById('route-panel').classList.contains('open')"
            ),
            "addStopDisabled": await page.evaluate(
                "() => document.getElementById('btn-add-stop').disabled"
            ),
        }
        await page.screenshot(path=f"{SCRATCH}/pw-directions-offline.png")

        out["consoleErrorsOnline"] = online_errors[:8]
        out["consoleErrorsOffline"] = app_errors("offline")[:8]
        await browser.close()

    print(json.dumps(out, indent=2, ensure_ascii=False))

    detail_ok = (
        bool(re.search(r"\d{5}", out["detailCard"]["address"]))
        and bool(re.match(r"\d+\.\d{5}, \d+\.\d{5}", out["detailCard"]["coords"]))
        and out["detailCard"]["destinationPrefilled"]
        and out["detailCard"]["cardClosedOnRoute"]
    )
    tabs_ok = (
        out["tabs"]["placeholderBeforeStart"]
        and all(re.search(r"\d", v) for v in out["tabs"]["times"].values())
        and out["tabs"]["walkActive"] == "walk"
        and out["tabs"]["cachedSwitchRefired"] == 0
    )
    steps_ok = (
        out["steps"]["glyph"]
        and out["steps"]["count"] > 0
        and bool(re.search(r"(Sek\.|Min\.)", out["steps"]["sub"]))
        and bool(re.search(r"\((\d+ m|\d+(\.\d+)? km)\)", out["steps"]["sub"]))
        and out["steps"]["zoomAfterClick"] >= 15
    )
    swap_ok = out["swap"]["reversed"]
    multi_ok = (
        out["multiStop"]["inputsAfterAdd"] == 3
        and out["multiStop"]["swapHidden"]
        and all(re.search(r"\d", v) for v in out["multiStop"]["times"].values())
    )
    units_ok = (
        bool(re.search(r"\d(\.\d)? mi<br", out["units"]["resultInMiles"]))
        and bool(re.search(r"\d(\.\d)? (mi|ft)$", out["units"]["badgeInMiles"]))
        and bool(re.search(r"\((\d+ ft|\d+(\.\d+)? mi)\)", out["units"]["stepSubInMiles"]))
        and out["units"]["backToKm"]
        and out["clear"]["tabsReset"]
    )
    locate_ok = out["locateMe"]["markerAppeared"] and out["locateMe"]["nearExpected"]
    cs = out["clickSemantics"]
    click_ok = (
        not cs["groundCardShown"]
        and cs["groundPinDelta"] == 0
        and (not cs["poiFound"] or (
            cs.get("cardName") == cs.get("poiName")
            and cs.get("startPrefilled")
            and cs.get("dismissedCleanly")
        ))
    )
    offline_ok = all(out["offline"].values())
    console_ok = not out["consoleErrorsOnline"]

    buttons_ok = not out["buttons"]["routeGo"] and not out["buttons"]["routeClear"]
    auto_ok = (
        out["autoRoute"]["routedWithoutButton"]
        and all(re.search(r"\d", v) for v in out["autoRoute"]["times"].values())
    )
    legs_ok = (
        out["legs"]["lineLayers"] == 2
        and len(set(out["legs"]["colors"])) == 2
        and out["legs"]["badges"] == 2
        and out["legs"]["chips"] == 2
        and any("Abschnitt 1" in h for h in out["legs"]["headers"])
    )
    zoomout_ok = (
        # Globe on stage WITH the route: suppression stopgap removed.
        out["zoomOut"]["flatCanvasHidden"]
        and out["zoomOut"]["globeShown"]
        # casing + 2 leg ribbons at minimum (3-stop trip)
        and out["zoomOut"]["globeRouteMeshes"] >= 3
        and out["zoomOut"]["globeBadges"] == 1
        # ... and the flat rendition untouched on return
        and out["zoomOut"]["flatRestored"]["legLayers"] == 2
        and out["zoomOut"]["flatRestored"]["flatBadges"] == 2
        and out["zoomOut"]["flatRestored"]["flatCanvasVisible"]
    )
    drag_ok = (out["drag"]["orderChanged"] and out["drag"]["glyphsRederived"]
               and out["drag"]["rerouted"])
    fieldclear_ok = (
        out["fieldClear"]["rowsAfterRemove"] == 2
        and out["fieldClear"]["reroutedAfterRemove"]
        and out["fieldClear"]["emptiedValue"] == ""
        and out["fieldClear"]["rowsKept"] == 2
        and out["fieldClear"]["resultClosed"]
    )
    gear_ok = (
        out["gear"]["menuClosedInitially"]
        and out["gear"]["inlineSwitcherGone"]
        and out["gear"]["modeButtons"] == ["offline", "mixed", "online"]
        and out["gear"]["closesOnOutsideClick"]
    )
    t = out["theme"]["sequence"]
    theme_ok = (
        "Automatisch" in t[0]["title"] and "Hell" in t[1]["title"]
        and "Dunkel" in t[2]["title"] and t[2]["view"] == "map-dark"
        and t[2]["flavor"] == "dark" and "view=map-dark" in t[2]["hash"]
        and t[3]["view"] == "map-light" and t[4]["view"] == "map-dark"
    )
    ex = out["exclusive"]
    exclusive_ok = (
        ex["resultsClosedAfterPick"] and ex["cardClosedOnRoute"]
        and ex["resultsClosedMidRouting"] and ex["panelStillOpen"]
        and ex["noCardMidRouting"] and "Berlin" in ex["destBecamePick"]
    )
    pins = out["pinAnchor"]
    pin_ok = all(
        pins[z] is not None and abs(pins[z][k]) <= 3
        for z in ("z12", "z16") for k in ("dx", "dy")
    )

    verdict = {
        "detail_card_ok": detail_ok, "tabs_ok": tabs_ok, "steps_ok": steps_ok,
        "swap_ok": swap_ok, "multi_stop_ok": multi_ok, "units_ok": units_ok,
        "locate_ok": locate_ok, "click_semantics_ok": click_ok,
        "buttons_gone_ok": buttons_ok, "auto_route_ok": auto_ok,
        "per_leg_colors_ok": legs_ok, "zoomout_route_visible_ok": zoomout_ok,
        "drag_reorder_ok": drag_ok, "field_clear_ok": fieldclear_ok,
        "gear_menu_ok": gear_ok, "theme_toggle_ok": theme_ok,
        "exclusivity_ok": exclusive_ok, "pin_anchor_ok": pin_ok,
        "alternates_roundtrip_ok": alternates_ok,
        "offline_ok": offline_ok, "console_ok": console_ok,
    }
    print(json.dumps(verdict, indent=2))
    ok = all(verdict.values())
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # The offline section flips server-side state — always restore it.
        try:
            set_server_mode("online")
        except Exception:
            pass
