"""Headless check: the reworked app chrome.

1. Layers control (top right, Google-style): the old #view-switch button bar
   is gone; a thumbnail tile button previews the view you would switch TO
   (imagery while cartography is up, the vector map on Hybrid/Satellit),
   with a layers glyph + "Ebenen" badge. Clicking opens the "Kartentyp"
   picker; its tiles come from a config array, the current mode is
   highlighted, and picking one switches the view AND the URL hash.
   Narrow viewports degrade the big tile to a plain icon button.
2. Theme toggle: its own small card in the lower-LEFT corner cluster
   (#corner-cluster), stacked with the settings gear; the top right keeps
   only the layers control.
3. Settings gear = user settings only: "Netzwerk" (mode switch) and
   "Sprache" (the #lang select, moved out of the top bar); changing the
   language relabels the map (symbol layers reference name:<lang>). The
   gear lives in the lower-left cluster; its menu opens upward.
4. Developer mode: the "</>" toggle sits lower right below the zoom
   cluster; it opens the structured panel (Szenarien / Simulation /
   Overlays / Debug at minimum), sections individually collapsible, the
   demo scenario loads from it, the nav-sim entry point is wired, and the
   on/off state survives a reload (localStorage).
5. De-branding sweep: zero case-insensitive matches for the banned legacy
   terms anywhere in the repo (the pattern is assembled from fragments below
   so this harness is not itself a match).
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile

from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_SECTIONS = {"szenarien", "simulation", "overlays", "debug"}


async def mounted(page):
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()",
        timeout=30000)


async def top_right_shot(page, name, width=340, height=300):
    vp = page.viewport_size
    await page.screenshot(path=f"{SCRATCH}/{name}",
                          clip={"x": vp["width"] - width, "y": 0,
                                "width": width, "height": height})


async def main():
    out = {}
    checks = {}
    launch_kwargs = {}
    if os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH"):
        launch_kwargs["executable_path"] = os.environ["AGENTIC_MAPS_RENDER_CHROMIUM_PATH"]

    # -- 5. the grep is part of the harness, not an honour-system step ----
    # Assembled from fragments so the harness never contains a banned term
    # itself; whole repo, both legacy names, all suffixed variants.
    banned = "|".join(["d" + "eck", "dynamic" + "slides", "dyn" + "slides", "lech" + "ler"])
    grep = subprocess.run(
        ["grep", "-rniE", banned, ".",
         "--exclude-dir=.git", "--exclude-dir=.venv", "--exclude-dir=__pycache__",
         "--exclude-dir=var", "--exclude-dir=node_modules"],
        cwd=ROOT, capture_output=True, text=True)
    out["brandGrep"] = grep.stdout.strip().splitlines()[:10]
    checks["noLegacyBrandReferences"] = grep.returncode == 1 and not grep.stdout.strip()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # -- 1. layers control --------------------------------------------
        await page.goto(f"{BASE}/#@48.7784,9.1806,12z&view=map-light&lang=de",
                        wait_until="load")
        await mounted(page)
        await page.wait_for_timeout(1500)

        out["control"] = await page.evaluate("""() => {
            const btn = document.getElementById('layers-btn');
            const style = getComputedStyle(btn);
            return {
              oldSwitchGone: !document.getElementById('view-switch'),
              controlPresent: !!document.getElementById('layers-control'),
              btnRect: btn.getBoundingClientRect().toJSON(),
              bg: style.backgroundImage,
              badgeVisible: btn.querySelector('.lb-badge').offsetParent !== null,
              iconFallbackHidden: getComputedStyle(
                btn.querySelector('.lb-icon')).display === 'none',
              pickerClosed: !document.getElementById('layers-picker')
                .classList.contains('open'),
              themeBtn: (() => {
                const t = document.getElementById('btn-theme');
                return { inCluster: !!t.closest('#corner-cluster'),
                         inControl: !!t.closest('#layers-control'),
                         title: t.title, rect: t.getBoundingClientRect().toJSON() };
              })(),
              gearRect: document.getElementById('settings-toggle')
                .getBoundingClientRect().toJSON(),
            };
        }""")
        c = out["control"]
        checks["oldViewSwitchGone"] = c["oldSwitchGone"]
        checks["layersControlTopRight"] = (c["controlPresent"]
            and c["btnRect"]["x"] > 1200 and c["btnRect"]["y"] < 80)
        checks["layersThumbGrown"] = 72 <= c["btnRect"]["width"] <= 80
        checks["thumbnailIsNextView"] = "layers-sat.png" in c["bg"]  # on Karte → previews imagery
        checks["badgeShownWide"] = c["badgeVisible"] and c["iconFallbackHidden"]
        # Theme + gear moved to the lower-LEFT corner cluster (cards,
        # vertical); nothing but the layers control stays top right.
        checks["themeButtonLowerLeft"] = (c["themeBtn"]["inCluster"]
            and not c["themeBtn"]["inControl"]
            and c["themeBtn"]["title"].startswith("Design")
            and c["themeBtn"]["rect"]["x"] < 200 and c["themeBtn"]["rect"]["y"] > 600)
        checks["gearLowerLeft"] = (c["gearRect"]["x"] < 200
            and c["gearRect"]["y"] > 600)
        await top_right_shot(page, "pw-chrome-layers-on-karte.png", 200, 140)

        # picker: open, three tiles, current highlighted
        await page.click("#layers-btn")
        await page.wait_for_selector("#layers-picker.open", timeout=5000)
        out["picker"] = await page.evaluate("""() => ({
            head: document.querySelector('#layers-picker .lp-head').textContent,
            tiles: [...document.querySelectorAll('#layers-grid .lp-tile')]
              .map((t) => ({ choice: t.dataset.choice, label: t.textContent,
                             active: t.classList.contains('active'),
                             hasThumb: t.querySelector('.lp-thumb')
                               .style.backgroundImage.includes('/assets/layers-') })),
        })""")
        tiles = out["picker"]["tiles"]
        checks["pickerOpensWithTiles"] = (out["picker"]["head"] == "Kartentyp"
            and [t["choice"] for t in tiles] == ["hybrid", "satellite", "map"]
            and all(t["hasThumb"] for t in tiles))
        checks["currentModeHighlighted"] = [t["choice"] for t in tiles if t["active"]] == ["map"]
        await top_right_shot(page, "pw-chrome-picker-open.png")

        # switching: every mode works and the URL follows
        state = "() => ({ view: window.agenticMaps[0].view, hash: location.hash })"
        await page.click('#layers-grid .lp-tile[data-choice="hybrid"]')
        await page.wait_for_timeout(700)
        s_hybrid = await page.evaluate(state)
        await top_right_shot(page, "pw-chrome-layers-on-hybrid.png", 200, 120)
        await page.click('#layers-grid .lp-tile[data-choice="satellite"]')
        await page.wait_for_timeout(700)
        s_sat = await page.evaluate(state)
        await page.click('#layers-grid .lp-tile[data-choice="map"]')
        await page.wait_for_timeout(700)
        s_map = await page.evaluate(state)
        out["switching"] = {"hybrid": s_hybrid, "satellite": s_sat, "map": s_map}
        checks["switchHybrid"] = (s_hybrid["view"] == "hybrid"
                                  and "view=hybrid" in s_hybrid["hash"])
        checks["switchSatellit"] = (s_sat["view"] == "satellite"
                                    and "view=satellite" in s_sat["hash"])
        checks["switchKarte"] = (s_map["view"] == "map-light"
                                 and "view=map-light" in s_map["hash"])
        # on Hybrid/Satellit the big tile previews the vector map instead
        out["previewOnHybrid"] = await page.evaluate(
            "() => getComputedStyle(document.getElementById('layers-btn')).backgroundImage")
        # (captured after the round trip back to Karte → sat again)
        checks["previewSwapsWithView"] = "layers-sat.png" in out["previewOnHybrid"]

        # outside click closes the picker
        await page.click("#q")
        await page.wait_for_timeout(300)
        checks["pickerClosesOutside"] = await page.evaluate(
            "() => !document.getElementById('layers-picker').classList.contains('open')")

        # the preview really is the map thumb while imagery is up
        await page.evaluate("() => window.agenticMaps[0].setView('hybrid')")
        await page.evaluate("() => document.getElementById('layers-btn')"
                            ".style.getPropertyValue('--layers-thumb')")
        # setView() on the wrapper does not go through the app's setView —
        # drive the app path instead:
        await page.click("#layers-btn")
        await page.click('#layers-grid .lp-tile[data-choice="hybrid"]')
        await page.wait_for_timeout(400)
        bg_hybrid = await page.evaluate(
            "() => getComputedStyle(document.getElementById('layers-btn')).backgroundImage")
        checks["previewOnHybridIsMap"] = "layers-map.png" in bg_hybrid
        await page.click("#q")

        # -- 2/3. settings gear: Netzwerk + Sprache -------------------------
        await page.click("#settings-toggle")
        await page.wait_for_selector("#settings-menu.open", state="visible", timeout=5000)
        out["settings"] = await page.evaluate("""() => ({
            labels: [...document.querySelectorAll('#settings-menu .settings-label')]
              .map((n) => n.textContent),
            langInMenu: !!document.getElementById('lang').closest('#settings-menu'),
            modeInMenu: !!document.getElementById('mode-switch').closest('#settings-menu'),
            langInTopBar: !!document.getElementById('lang').closest('#layers-control'),
        })""")
        s = out["settings"]
        checks["settingsSections"] = s["labels"] == ["Netzwerk", "Sprache"]
        checks["langLivesInSettings"] = s["langInMenu"] and not s["langInTopBar"]
        checks["modeSwitchStays"] = s["modeInMenu"]
        # The gear menu now opens upward from the lower-left cluster.
        vp = page.viewport_size
        await page.screenshot(path=f"{SCRATCH}/pw-chrome-settings-open.png",
                              clip={"x": 0, "y": vp["height"] - 420,
                                    "width": 340, "height": 420})

        await page.select_option("#lang", "en")
        await page.wait_for_timeout(900)
        out["language"] = await page.evaluate("""() => {
            const m = window.agenticMaps[0].map;
            const fields = JSON.stringify(m.getStyle().layers
              .filter((l) => l.type === 'symbol')
              .map((l) => (l.layout || {})['text-field'] || null));
            return { hash: location.hash, usesNameEn: fields.includes('name:en'),
                     status: document.getElementById('status').textContent };
        }""")
        checks["languageSwitchFromGear"] = (out["language"]["usesNameEn"]
            and "lang=en" in out["language"]["hash"]
            and out["language"]["status"].startswith("Beschriftung"))
        await page.select_option("#lang", "de")
        await page.click("#q")  # close the gear menu

        # -- 1b. narrow viewport: icon fallback ------------------------------
        await page.set_viewport_size({"width": 520, "height": 800})
        await page.wait_for_timeout(400)
        out["narrow"] = await page.evaluate("""() => {
            const btn = document.getElementById('layers-btn');
            const style = getComputedStyle(btn);
            return { width: btn.offsetWidth, bg: style.backgroundImage,
                     iconShown: btn.querySelector('.lb-icon').offsetParent !== null,
                     badgeHidden: btn.querySelector('.lb-badge').offsetParent === null };
        }""")
        n = out["narrow"]
        checks["narrowIconFallback"] = (n["width"] == 38 and n["bg"] == "none"
                                        and n["iconShown"] and n["badgeHidden"])
        await top_right_shot(page, "pw-chrome-narrow-fallback.png", 200, 120)
        await page.set_viewport_size({"width": 1400, "height": 900})
        await page.wait_for_timeout(400)

        # -- 4. developer mode, lower right ---------------------------------
        out["dev"] = await page.evaluate("""() => {
            const btn = document.getElementById('btn-dev');
            const r = btn.getBoundingClientRect();
            return { rect: r.toJSON(), inTopBar: !!btn.closest('#layers-control'),
                     panelOpen: document.getElementById('dev').classList.contains('open') };
        }""")
        d = out["dev"]
        checks["devButtonLowerRight"] = (d["rect"]["x"] > 1200 and d["rect"]["y"] > 600
                                         and not d["inTopBar"] and not d["panelOpen"])
        await page.click("#btn-dev")
        await page.wait_for_selector("#dev.open", timeout=5000)
        # refreshDebugInfo fills the box only after its own fetch of
        # debug/enabled resolves — capturing before that raced a "—".
        await page.wait_for_function(
            "() => document.getElementById('dev-debug-info').textContent !== '—'",
            timeout=5000)
        # The panel is a POPOVER anchored at its own button — grows up-left
        # from the lower-right corner (the gear menu's upward pattern), navy
        # surface family, never a full-height side wall. The button itself
        # carries the same navy treatment.
        out["devPopover"] = await page.evaluate("""() => {
            const dev = document.getElementById('dev');
            const rect = dev.getBoundingClientRect();
            const btn = document.getElementById('devmode').getBoundingClientRect();
            const style = getComputedStyle(dev);
            const navy = (color) => {
              const [r, g, b] = (color.match(/\\d+/g) || []).map(Number);
              return b > r && b > g && r < 90;
            };
            return {
              height: Math.round(rect.height),
              anchoredAboveButton: rect.bottom <= btn.top + 2
                && rect.right >= btn.left,
              rightAligned: window.innerWidth - rect.right < 40,
              rounded: parseFloat(style.borderRadius) >= 10,
              notFullHeight: rect.height < window.innerHeight * 0.8,
              navySurface: navy(style.backgroundColor),
              buttonNavy: navy(getComputedStyle(
                document.getElementById('devmode')).backgroundColor),
            };
        }""")
        dp = out["devPopover"]
        checks["devPanelIsAnchoredPopover"] = (dp["anchoredAboveButton"]
            and dp["rightAligned"] and dp["rounded"] and dp["notFullHeight"])
        checks["devSurfaceNavy"] = dp["navySurface"] and dp["buttonNavy"]
        out["devPanel"] = await page.evaluate("""() => ({
            sections: [...document.querySelectorAll('#dev details.dev-section')]
              .map((s) => ({ name: s.dataset.section, open: s.open,
                             summary: s.querySelector('summary').textContent.trim() })),
            debugInfo: document.getElementById('dev-debug-info').textContent,
            navBtnInSimulation: !!document.getElementById('dev-nav')
              .closest('details[data-section="simulation"]'),
            overlaysBtn: !!document.getElementById('dev-overlays')
              .closest('details[data-section="overlays"]'),
        })""")
        names = {sec["name"] for sec in out["devPanel"]["sections"]}
        checks["devSectionsPresent"] = REQUIRED_SECTIONS.issubset(names)
        checks["devDebugPopulated"] = ("Netzmodus" in out["devPanel"]["debugInfo"]
            and "Debug-Bridge" in out["devPanel"]["debugInfo"])
        checks["devNavReachable"] = out["devPanel"]["navBtnInSimulation"]
        checks["devOverlaysReachable"] = out["devPanel"]["overlaysBtn"]

        # sections collapse individually
        await page.click('details[data-section="szenarien"] summary')
        collapsed = await page.evaluate(
            "() => document.querySelector('details[data-section=\"szenarien\"]').open")
        await page.click('details[data-section="szenarien"] summary')
        reopened = await page.evaluate(
            "() => document.querySelector('details[data-section=\"szenarien\"]').open")
        checks["devSectionsCollapsible"] = (collapsed is False and reopened is True)

        # the demo scenario loads from the panel
        await page.click("#dev-load-scenario")
        await page.wait_for_function(
            "() => document.getElementById('status').textContent"
            ".startsWith('Szenario geladen')", timeout=30000)
        out["scenario"] = await page.evaluate("""() => ({
            status: document.getElementById('status').textContent,
            stopsOpen: document.getElementById('stops').classList.contains('open'),
            stopChips: document.querySelectorAll('#stops button').length,
        })""")
        checks["scenarioLoadsFromPanel"] = (out["scenario"]["stopsOpen"]
            and out["scenario"]["stopChips"] == 6)
        await page.screenshot(path=f"{SCRATCH}/pw-chrome-dev-panel.png")

        # nav-sim entry point answers (no route standing → the honest hint);
        # its section starts collapsed, so expand it the way a user would.
        await page.click('details[data-section="simulation"] summary')
        await page.wait_for_selector("#dev-nav", state="visible", timeout=5000)
        await page.click("#dev-nav")
        await page.wait_for_timeout(300)
        out["devNavStatus"] = await page.evaluate(
            "() => document.getElementById('status').textContent")
        checks["devNavWired"] = out["devNavStatus"] == "Erst eine Route berechnen"

        # dev-mode persists across a reload
        await page.reload(wait_until="load")
        await mounted(page)
        await page.wait_for_timeout(800)
        out["persisted"] = await page.evaluate("""() => ({
            open: document.getElementById('dev').classList.contains('open'),
            btnActive: document.getElementById('btn-dev').classList.contains('active'),
            stored: localStorage.getItem('am-maps-devmode'),
        })""")
        checks["devModePersists"] = (out["persisted"]["open"]
            and out["persisted"]["btnActive"] and out["persisted"]["stored"] == "1")
        await page.click("#dev-close")
        out["storedAfterClose"] = await page.evaluate(
            "() => localStorage.getItem('am-maps-devmode')")
        checks["devModeCloseStored"] = out["storedAfterClose"] == "0"

        out["consoleErrors"] = [e for e in errors if "favicon" not in e][:5]
        await browser.close()

    print(json.dumps(out, indent=2, ensure_ascii=False))
    print()
    failed = [k for k, v in checks.items() if not v]
    for k in sorted(checks):
        print(("PASS " if checks[k] else "FAIL ") + k)
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(0 if not failed else 1)


asyncio.run(main())
