"""Headless check: navigation mode ("drive a route") + the two control fixes.

Exercises web/index.html + web/nav.js against a real running dev server and a
real routing backend (AGENTIC_MAPS_ROUTING_BACKEND=osrm against the public
demo), no mocking — same conventions as the other tools/verify_*.py scripts.

Fix 1 — instant route redraw on a theme/style switch:
  1. With a route standing, switching hell → dunkel re-adds the route layers
     on the first usable style event, NOT on 'idle': measured click → layer
     present again, asserted < 500 ms (it was seconds while every tile of the
     new style loaded).

Fix 2 — control semantics on the #tilt card:
  2. The compass is a north-lock toggle: clicking animates bearing → 0 and
     never touches the pitch; while locked a user rotate gesture snaps back
     to north; unlocking frees rotation again. The 2D/3D button toggles ONLY
     the pitch — the bearing stays where it was.

Navigation mode (Dettingen an der Erms → Frankfurt am Main):
  3. Entry: a route shows the blue "Start"; clicking sets body.nav-mode and
     hides ALL normal chrome (search, panels, view switch, settings, zoom,
     tilt, locate, dev). Alternates + their light badges leave the map.
  4. HUD: the top banner (navy instruction bar — centered with a sane max
     width on this wide stage) cycles ≥ 3 distinct maneuvers under
     simulation; the countdown strictly decreases within one maneuver; the
     approach bar width changes; the "Danach" preview tab is present.
  4b. Lane guidance: where the upcoming step carries `lanes` (motorway
     junctions on this run do), the row under the banner draws one arrow
     per lane, valid ones bright, the rest dimmed; steps without lanes
     show no row.
  5. Bottom bar: the remaining TIME is the big element and falls, the ETA
     is a clock time, the distance is secondary next to it.
  5b. Voice queue: after a fast run no utterance backlog remains
     (speechSynthesis.pending is false — feature-detected).
  6. Follow camera: 2D↔3D toggles pitch (60 ↔ 0); auto-zoom rides closer at
     a turn than on a long fast straight (sampled deterministically at both).
  7. Straight-road fast-forward measurably shortens the wall-clock for the
     same straight vs. the plain factor (two timed runs).
  8. Traveled path: the grey behind-the-marker layer exists and grows.
  9. URL hash writing is suspended during nav and resumes on exit.
 10. Peek + re-center: a drag suspends the chase cam and shows "Zentrieren";
     clicking it re-follows.
 11. Exit ("Beenden") restores the whole UI with the route still drawn, the
     alternates return, and no nav rAF loop keeps running afterwards.
 12. Arrival: jumping near the destination and letting it drive shows the
     "Ziel erreicht" card, then the normal UI returns on its own.
 13. Dark theme: nav mode runs on the dark cartography too (screenshot).

Navigation polish pass (owner-directed):
 14. Palette: the banner is OUR deep indigo navy + amber (road-styles.js NAV
     tokens) — hue in the blue range, NOT Google's navigation blue (#1a73e8;
     RGB distance > 60 asserted) and not the old green; countdown and
     instruction ink clear full WCAG AA (>= 4.5:1) against the banner. Top
     and bottom are ONE surface system: the bottom bar sits in the same navy
     hue range and its amber remaining-time total clears 4.5:1 on it.
     Checked in both themes.
 15. Bottom bar: two groups — .nb-totals (remaining time large + coloured,
     distance/ETA beneath) and .nb-controls (speed chip, mute, 2D/3D), with
     Beenden visually separated behind the .nb-sep divider.
 16. Tempo = slider over log steps 1..500: programmatic set snaps, keyboard
     arrows walk the steps, the value label follows.
 17. Fast-forward multiplier = second slider ×1(aus)..×32, log-snapped; the
     old checkbox is gone. ×16 vs ×1 measurably shortens the wall-clock for
     the same straight (two timed runs).
 18. Sim card collapses to its title row (chevron), state persisted; on a
     ≤760px viewport with no stored preference it starts collapsed.
 19. Chevron ~1.5× (68px) and the nav route much heavier: line/casing carry
     zoom-interpolated nav widths while nav runs, the map-mode 5/8px numbers
     return on exit; the traveled gray rides the same curve.
 20. Close-zoom streets: the tuned style's width tables now reach z20
     (roads as drivable surfaces) — top anchors extended, z15 anchors
     untouched (the cartography harness's mid-zoom band must not move).
 21. Skip-to-announcement ("→ Ansage"): lands ~2 s of sim-travel before the
     next voice threshold (distance-to-maneuver ∈ [threshold, threshold +
     lead]), works while paused and stays paused.
 22. Dev chrome hidden by default: in a FRESH context (no localStorage) nav
     starts without the SIMULATION card, the "</>" entrance stays visible
     but discreet, and toggling developer mode mid-drive shows/hides the
     card immediately (the dev panel itself never opens over nav). The main
     phases enable dev mode first — the sim card is developer chrome now.

Owner pass 2 (unified aerial + HUD ergonomics):
 23. Lane guidance only when it matters: the row needs DISCRIMINATING lane
     data and shows only inside the distance window (800 m / 2 km at
     motorway speed); far out, all-valid and lane-less steps show nothing.
 24. Paired steppers: « Manöver » and « Ansage » step both directions,
     chain on repeat, round-trip cleanly, stay paused, and no-op at s=0.
 25. Palette tune: navy deeper, the accent a decisive ORANGE (hue 18-35,
     warmer than the old amber ~38); AA re-verified in both themes.
 26. Route colour: vivid brand blue #2e6be6 from MapRoute.color — the
     azure family, measurably not Google's #1a73e8, never the old amber.
 27. Chrome text is non-selectable (user-select none) everywhere except
     inputs.
 28. Side stack (Google-style round buttons): mute moved out of the bar;
     the compass toggles north-up (camera bearing locked 0 mid-drive) vs
     heading-up follow. Bottom bar: round ✕ cancel leftmost, route-options
     rightmost, map-type toggle among the controls.
 29. In-nav map-type toggle Hybrid <-> Karte: the drive, route emphasis and
     traveled layer survive the style swap.
 30. Attribution in nav compacts to the ⓘ badge: standing line hidden, tap
     expands the full per-zoom credit (OSM + imagery strings asserted),
     collapses on outside click; the normal line returns on exit.
 31. In-nav route options: add a stop mid-drive through the shared
     autocomplete — the recomputed route is adopted in place (marker
     re-snapped, emphasis kept, sim still driving); panel closes outside.

Chromium resolution follows tools/verify_render.py's pattern.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time

from playwright.async_api import async_playwright

BASE = os.environ.get("AGENTIC_MAPS_VERIFY_BASE", "http://127.0.0.1:8195")
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

DETTINGEN = (48.5254, 9.3466)
FRANKFURT = (50.1109, 8.6821)

# The "</>" developer entrance deliberately survives nav mode now (discreet
# lower right) — it is no longer part of the hidden chrome list.
NAV_CHROME = ["search-panel", "layers-control", "settings", "locate", "zoom", "tilt"]

# Banner + bottom-bar palette, hue and WCAG contrast, sampled from the
# computed styles. The identity to assert: deep NAVY (hue in the blue range,
# far from Google's #1a73e8) with the amber accent clearing full AA, and the
# SAME surface system top and bottom.
CONTRAST_JS = """() => {
  const rgb = (s) => (s.match(/\\d+(\\.\\d+)?/g) || []).slice(0, 3).map(Number);
  const lum = (c) => {
    const f = (v) => { v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    const [r, g, b] = c.map(f);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const hue = (c) => {
    const [r, g, b] = c.map((v) => v / 255);
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    if (!d) return 0;
    const h = mx === r ? ((g - b) / d + 6) % 6
      : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return h * 60;
  };
  const bannerBg = getComputedStyle(
    document.querySelector('#nav-banner .nb-inner')).backgroundColor;
  const bottomBg = getComputedStyle(
    document.getElementById('nav-bottom')).backgroundColor;
  const countdown = getComputedStyle(document.getElementById('nav-distance')).color;
  const text = getComputedStyle(document.getElementById('nav-text')).color;
  const timeLeft = getComputedStyle(document.getElementById('nav-time-left')).color;
  const [br, bg2, bb] = rgb(bannerBg);
  return {
    bannerBg, bottomBg,
    bannerHue: +hue(rgb(bannerBg)).toFixed(1),
    bottomHue: +hue(rgb(bottomBg)).toFixed(1),
    // The accent must read ORANGE now, not the old amber (~38deg): the
    // countdown's hue is asserted below the amber band.
    accentHue: +hue(rgb(countdown)).toFixed(1),
    countdownRatio: +ratio(rgb(bannerBg), rgb(countdown)).toFixed(2),
    textRatio: +ratio(rgb(bannerBg), rgb(text)).toFixed(2),
    timeRatio: +ratio(rgb(bottomBg), rgb(timeLeft)).toFixed(2),
    googleBlueDist: +Math.hypot(br - 26, bg2 - 115, bb - 232).toFixed(1),
    isOldGoogleGreen: rgb(bannerBg).join(',') === '24,128,56',
  };
}"""


async def wait_map(page) -> None:
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && window.agenticMaps[0].map.getStyle()"
        " && window.agenticNav",
        timeout=30000,
    )
    await page.wait_for_timeout(1200)


async def right_click_set(page, lon, lat, role, zoom=13):
    await page.evaluate(
        "([lon, lat, zoom]) => window.agenticMaps[0].map.jumpTo({ center: [lon, lat], zoom })",
        [lon, lat, zoom],
    )
    await page.wait_for_timeout(300)
    box = await page.evaluate(
        "() => { const r = document.getElementById('map').getBoundingClientRect();"
        " return { x: r.width / 2, y: r.height / 2 }; }"
    )
    await page.mouse.click(box["x"], box["y"], button="right")
    await page.wait_for_selector("#map-menu.open", state="visible", timeout=5000)
    await page.click(f'#map-menu button[data-role="{role}"]')
    await page.wait_for_selector("#map-menu.open", state="hidden", timeout=5000)


async def ctrl_drag(page, dx=90, dy=-60):
    await page.mouse.move(700, 500)
    await page.keyboard.down("Control")
    await page.mouse.down()
    await page.mouse.move(700 + dx, 500 + dy, steps=10)
    await page.mouse.up()
    await page.keyboard.up("Control")


async def debug(page) -> dict:
    return await page.evaluate("() => window.agenticNav.debug()")


async def build_route(page) -> None:
    await right_click_set(page, DETTINGEN[1], DETTINGEN[0], "start")
    await right_click_set(page, FRANKFURT[1], FRANKFURT[0], "end")
    await page.wait_for_selector("#route-result.open", state="visible", timeout=60000)
    await page.wait_for_function(
        "() => { const w = window.agenticMaps[0]; return w.routes.length"
        " && w.map.getLayer('route-' + w.routes[0].id + '-casing'); }",
        timeout=20000,
    )
    await page.wait_for_function(
        "() => !window.agenticMaps[0].map.isMoving()", timeout=15000
    )


async def start_nav(page) -> None:
    # The subtle "Simulieren" link (the old prominent Start pill is gone).
    await page.click("#btn-nav-simulate")
    await page.wait_for_function(
        "() => document.body.classList.contains('nav-mode')", timeout=5000
    )
    await page.wait_for_timeout(600)


async def start_nav_via_dev(page) -> None:
    """The mobile path: the route panel carries NO simulation entry on a
    narrow viewport (by design) — the dev panel's Simulation section is
    the way into the drive there."""
    await page.evaluate(
        "() => { if (!document.getElementById('dev').classList"
        ".contains('open')) document.getElementById('btn-dev').click();"
        " document.querySelector('[data-section=simulation]')"
        ".setAttribute('open', ''); }")
    await page.click("#dev-nav")
    await page.wait_for_function(
        "() => document.body.classList.contains('nav-mode')", timeout=5000
    )
    await page.wait_for_timeout(600)


async def main() -> None:
    out: dict = {}
    errors: list = []
    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    # Headless Chromium suppresses pixels, NOT audio: window.speechSynthesis
    # routes to the OS voice engine, so every harness run used to SPEAK the
    # German announcements through the machine's speakers. This mock keeps the
    # API's observable semantics (pending/speaking/cancel, utterance callbacks)
    # so the voice-queue assertions still test the app's real behavior — it
    # just never reaches the operating system.
    MUTE_TTS = """
        if (window.speechSynthesis) {
            const queue = [];
            const mock = {
                get pending() { return queue.length > 1; },
                get speaking() { return queue.length > 0; },
                speak(u) {
                    queue.push(u);
                    // "utterances" finish quickly, in order, like a real engine.
                    setTimeout(function done() {
                        const i = queue.indexOf(u);
                        if (i !== -1) { queue.splice(i, 1); u.onend && u.onend(); }
                    }, 120);
                },
                cancel() { queue.length = 0; },
                getVoices() { return []; },
                addEventListener() {}, removeEventListener() {},
            };
            Object.defineProperty(window, 'speechSynthesis', { value: mock });
        }
    """

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            permissions=["geolocation"],
            geolocation={"latitude": DETTINGEN[0], "longitude": DETTINGEN[1]},
        )
        await context.add_init_script(MUTE_TTS)
        page = await context.new_page()
        page.on(
            "console",
            lambda m: errors.append({"text": m.text, "url": (m.location or {}).get("url", "")})
            if m.type == "error" else None,
        )
        page.on("pageerror", lambda exc: errors.append(
            {"text": "PAGEERROR: " + str(exc), "url": ""}))

        await page.goto(BASE + "/#@48.9,9.0,9z&view=map-light&lang=de", wait_until="load")
        await wait_map(page)
        await build_route(page)
        out["alternatesPresent"] = await page.evaluate(
            "() => (window.agenticMaps[0].routes[0].alternates || []).length")
        # Nav entry: the quiet "Simulieren" link is visible with its test
        # tooltip, and the old prominent Start pill no longer exists at all.
        out["startVisible"] = await page.evaluate(
            "() => { const el = document.getElementById('btn-nav-simulate');"
            " return el.offsetParent !== null && !el.hidden"
            " && el.title === 'Route simulieren (Testfunktion)'"
            " && !document.getElementById('btn-nav-start')"
            " && !document.getElementById('nav-start-row'); }")

        # ---- FIX 1: theme switch redraws the route within 500 ms ----------
        # Fresh profile: theme 'auto' (headless = light). Click 1 -> hell
        # (same flavor, no style rebuild), click 2 -> dunkel (full rebuild —
        # this is the measured transition).
        await page.click("#btn-theme")
        await page.wait_for_timeout(500)
        out["fix1"] = {"themeToRouteMs": await page.evaluate(
            """() => new Promise((resolve) => {
              const w = window.agenticMaps[0];
              const id = 'route-' + w.routes[0].id + '-casing';
              const t0 = performance.now();
              document.getElementById('btn-theme').click();
              const check = () => {
                try {
                  if (w.view === 'map-dark' && w.map.getLayer(id)) {
                    resolve(Math.round(performance.now() - t0));
                    return;
                  }
                } catch (e) { /* style mid-swap */ }
                if (performance.now() - t0 > 20000) { resolve(-1); return; }
                requestAnimationFrame(check);
              };
              check();
            })""")}
        # Back to light for the nav phases: dunkel -> auto (headless: light).
        await page.click("#btn-theme")
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-light'", timeout=15000)
        await page.wait_for_timeout(800)

        # ---- FIX 2: compass = north-lock, 3D = pitch only ------------------
        await page.evaluate(
            "() => window.agenticMaps[0].map.easeTo({ bearing: 40, pitch: 55, duration: 300 })")
        await page.wait_for_timeout(600)
        await page.click("#btn-compass")
        await page.wait_for_function(
            "() => Math.abs(window.agenticMaps[0].map.getBearing()) < 0.05"
            " && !window.agenticMaps[0].map.isEasing()", timeout=8000)
        out["fix2"] = {"afterLock": await page.evaluate(
            "() => ({ bearing: +window.agenticMaps[0].map.getBearing().toFixed(1),"
            " pitch: +window.agenticMaps[0].map.getPitch().toFixed(1),"
            " active: document.getElementById('btn-compass').classList.contains('active') })")}
        # A user rotate while locked snaps back to north.
        await page.evaluate(
            "() => { window.__maxB = 0; window.agenticMaps[0].map.on('rotate', () =>"
            " { window.__maxB = Math.max(window.__maxB,"
            " Math.abs(window.agenticMaps[0].map.getBearing())); }); }")
        await ctrl_drag(page)
        await page.wait_for_timeout(1200)
        out["fix2"]["lockedDrag"] = await page.evaluate(
            "() => ({ maxBearing: +window.__maxB.toFixed(1),"
            " bearing: +window.agenticMaps[0].map.getBearing().toFixed(1) })")
        # Unlocked: the rotation stays.
        await page.click("#btn-compass")
        await ctrl_drag(page)
        await page.wait_for_timeout(900)
        out["fix2"]["freeDrag"] = await page.evaluate(
            "() => ({ bearing: +window.agenticMaps[0].map.getBearing().toFixed(1),"
            " active: document.getElementById('btn-compass').classList.contains('active') })")
        # 3D button: pitch toggles, bearing untouched.
        bearing_before = out["fix2"]["freeDrag"]["bearing"]
        await page.click("#btn-3d")
        await page.wait_for_function(
            "() => window.agenticMaps[0].map.getPitch() < 0.5"
            " && !window.agenticMaps[0].map.isEasing()", timeout=8000)
        out["fix2"]["after3d"] = await page.evaluate(
            "() => ({ bearing: +window.agenticMaps[0].map.getBearing().toFixed(1),"
            " pitch: +window.agenticMaps[0].map.getPitch().toFixed(1) })")
        out["fix2"]["bearingBefore3d"] = bearing_before
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo({ bearing: 0, pitch: 0 })")

        # ---- nav entry ------------------------------------------------------
        # Developer mode ON for the sim-driven phases: the SIMULATION card is
        # developer chrome now (fresh-visitor default = hidden, checked in its
        # own phase at the end). The panel this opens is itself hidden while
        # nav-mode stands.
        await page.click("#btn-dev")
        await page.wait_for_selector("#dev.open", timeout=5000)
        hash_before = await page.evaluate("() => location.hash")
        await start_nav(page)
        out["entry"] = await page.evaluate(
            """(chrome) => ({
              navMode: document.body.classList.contains('nav-mode'),
              chromeHidden: chrome.every((id) =>
                document.getElementById(id).offsetParent === null),
              // The developer entrance survives nav mode; the panel does not.
              devButtonShown: document.getElementById('btn-dev').offsetParent !== null,
              devPanelHidden: document.getElementById('dev').offsetParent === null,
              routePanelHidden: document.getElementById('route-panel').offsetParent === null,
              bannerShown: document.getElementById('nav-banner').offsetParent !== null,
              bottomShown: document.getElementById('nav-bottom').offsetParent !== null,
              simShown: document.getElementById('nav-sim').offsetParent !== null,
              // Floating side stack (sound + compass) and the compact
              // attribution badge replace the standing credit line.
              sideShown: document.getElementById('nav-side').offsetParent !== null,
              attribBadgeShown: document.getElementById('nav-attrib').offsetParent !== null,
              attribLineHidden: (() => {
                const ctrl = document.querySelector('.maplibregl-ctrl-attrib');
                return !ctrl || ctrl.offsetParent === null;
              })(),
              markerShown: !!document.querySelector('.am-nav-arrow'),
              pitch: +window.agenticMaps[0].map.getPitch().toFixed(1),
              banner: (() => {
                const r = document.getElementById('nav-banner').getBoundingClientRect();
                return { width: Math.round(r.width),
                         centerOffset: Math.round(Math.abs(
                           (r.left + r.width / 2) - window.innerWidth / 2)) };
              })(),
            })""", NAV_CHROME)
        out["entry"]["alternatesHidden"] = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], r = w.routes[0];
              const layersOff = (r._altLayerIds || []).every((id) =>
                w.map.getLayoutProperty(id, 'visibility') === 'none');
              const badgesOff = (r._altBadgeMarkers || []).every((b) =>
                b.getElement().style.display === 'none');
              return { layersOff, badgesOff, altLayers: (r._altLayerIds || []).length };
            }""")

        # ---- palette: OUR pine/amber, not Google's green --------------------
        # (light theme; the dark theme is sampled in the dark-nav phase)
        out["palette"] = await page.evaluate(CONTRAST_JS)
        # Chevron + route emphasis while nav runs.
        out["navWeight"] = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], r = w.routes[0];
              const arrow = document.querySelector('.am-nav-arrow');
              const j = (v) => JSON.stringify(v);
              return {
                chevronPx: Math.round(arrow.getBoundingClientRect().width),
                casing: j(w.map.getPaintProperty('route-' + r.id + '-casing', 'line-width')),
                line: j(w.map.getPaintProperty(
                  (r._legLayerIds || [])[0] || ('route-' + r.id), 'line-width')),
                traveled: j(w.map.getPaintProperty('nav-traveled', 'line-width')),
              };
            }""")
        # Close-zoom street widths: the tuned tables now reach z20; the z15
        # anchors stay put (the cartography harness's band must not move).
        out["closeZoom"] = await page.evaluate(
            """() => {
              const m = window.agenticMaps[0].map;
              const top = (id) => {
                const e = m.getPaintProperty(id, 'line-width');
                return Array.isArray(e)
                  ? { topZoom: e[e.length - 2], topWidth: e[e.length - 1],
                      hasZ15At: e.indexOf(15) } : null;
              };
              const minor = m.getPaintProperty('roads_minor', 'line-width');
              return {
                minor: top('roads_minor'),
                major: top('roads_major'),
                highway: top('roads_highway'),
                minorZ15Value: Array.isArray(minor) ? minor[minor.indexOf(15) + 1] : null,
                minorCasingGapTop: (() => {
                  const g = m.getPaintProperty('roads_minor_casing', 'line-gap-width');
                  return Array.isArray(g) ? g[g.length - 1] : null;
                })(),
              };
            }""")

        # ---- HUD under simulation -------------------------------------------
        await page.evaluate("() => window.agenticNav.setFactor(25)")
        # Countdown strictly decreases within one maneuver.
        countdown = await page.evaluate(
            """() => new Promise((resolve) => {
              let tries = 0;
              const attempt = () => {
                const d0 = window.agenticNav.debug();
                const t0 = document.getElementById('nav-distance').textContent;
                setTimeout(() => {
                  const d1 = window.agenticNav.debug();
                  const t1 = document.getElementById('nav-distance').textContent;
                  const num = (t) => parseFloat(t.replace(',', '.'));
                  if (d0.markIdx === d1.markIdx && d1.distToNext < d0.distToNext
                      && num(t1) <= num(t0)) {
                    resolve({ ok: true, from: t0, to: t1,
                              fromM: Math.round(d0.distToNext), toM: Math.round(d1.distToNext) });
                  } else if (++tries > 40) {
                    resolve({ ok: false, from: t0, to: t1 });
                  } else attempt();
                }, 350);
              };
              attempt();
            })""")
        out["countdown"] = countdown
        # Approach bar fills over time.
        bar0 = await page.evaluate(
            "() => document.getElementById('nav-bar-fill').style.width")
        await page.wait_for_timeout(900)
        bar1 = await page.evaluate(
            "() => document.getElementById('nav-bar-fill').style.width")
        out["bar"] = {"w0": bar0, "w1": bar1, "changed": bar0 != bar1}
        # Danach preview.
        out["danach"] = await page.evaluate(
            "() => ({ shown: document.getElementById('nav-next').offsetParent !== null,"
            " text: document.getElementById('nav-next-text').textContent })")
        # Instruction card cycles distinct maneuvers.
        out["cycle"] = await page.evaluate(
            """() => new Promise((resolve) => {
              const seen = new Set([document.getElementById('nav-text').textContent]);
              const t0 = performance.now();
              const poll = setInterval(() => {
                seen.add(document.getElementById('nav-text').textContent);
                if (seen.size >= 3 || performance.now() - t0 > 60000) {
                  clearInterval(poll);
                  resolve({ distinct: seen.size, texts: [...seen].slice(0, 5) });
                }
              }, 150);
            })""")
        # Bottom bar: remaining decreases, ETA is a clock, speed is live.
        rem0 = await page.evaluate("() => window.agenticNav.debug()")
        await page.wait_for_timeout(1500)
        rem1 = await page.evaluate("() => window.agenticNav.debug()")
        out["bottom"] = {
            "remaining0": round(rem0["total"] - rem0["s"]),
            "remaining1": round(rem1["total"] - rem1["s"]),
            "timeLeft": await page.eval_on_selector("#nav-time-left", "el => el.textContent"),
            "timeLeftPx": await page.evaluate(
                "() => parseFloat(getComputedStyle("
                "document.getElementById('nav-time-left')).fontSize)"),
            "remainingText": await page.eval_on_selector("#nav-remaining", "el => el.textContent"),
            "eta": await page.eval_on_selector("#nav-eta", "el => el.textContent"),
            "speed": await page.eval_on_selector("#nav-speed", "el => el.textContent"),
        }
        # Reworked layout (Google's ergonomics): round ✕ cancel at the LEFT
        # end, totals next, controls (speed chip, 2D/3D, map-type toggle),
        # route-options at the RIGHT end; sound lives in the side stack.
        out["bottom"]["groups"] = await page.evaluate(
            """() => {
              const bar = document.getElementById('nav-bottom');
              const totals = bar.querySelector('.nb-totals');
              const controls = bar.querySelector('.nb-controls');
              const exit = document.getElementById('nav-exit');
              const options = document.getElementById('nav-options');
              const layers = document.getElementById('nav-layers');
              const speed = document.getElementById('nav-speed');
              const chipBg = getComputedStyle(speed).backgroundColor;
              const exitStyle = getComputedStyle(exit);
              const exitRect = exit.getBoundingClientRect();
              return {
                totals: !!totals && totals.contains(
                  document.getElementById('nav-time-left')),
                controls: !!controls && controls.contains(speed)
                  && controls.contains(document.getElementById('nav-camera'))
                  && controls.contains(layers) && controls.contains(options),
                totalsBeforeControls: !!totals && !!controls
                  && totals.getBoundingClientRect().left
                     < controls.getBoundingClientRect().left,
                speedIsChip: chipBg !== 'rgba(0, 0, 0, 0)' && chipBg !== 'transparent',
                // The cancel: round, leftmost, ✕-only, red identity in the glyph.
                exitFirst: bar.firstElementChild === exit
                  && exitRect.left < totals.getBoundingClientRect().left,
                exitRound: Math.abs(exitRect.width - exitRect.height) <= 2
                  && parseFloat(exitStyle.borderRadius) >= exitRect.width / 2 - 1,
                exitGlyphOnly: exit.textContent.trim() === '\\u2715',
                exitRedInk: (() => {
                  const [r, g, b] = (exitStyle.color.match(/\\d+/g) || []).map(Number);
                  return r > g + 40 && r > b + 40;
                })(),
                optionsLast: controls.lastElementChild === options,
                // Sound moved OUT of the bar into the floating side stack.
                muteInBar: bar.contains(document.getElementById('nav-mute')),
                muteInSide: !!document.getElementById('nav-mute')
                  .closest('#nav-side'),
                compassInSide: !!document.getElementById('nav-compass')
                  .closest('#nav-side'),
                timeColored: getComputedStyle(
                  document.getElementById('nav-time-left')).color
                  !== getComputedStyle(document.getElementById('nav-remaining')).color,
              };
            }""")
        # ---- tempo slider: log steps, snapping, keyboard ---------------------
        out["tempoSlider"] = await page.evaluate(
            """() => {
              const nav = window.agenticNav;
              const slider = document.getElementById('nav-sim-factor');
              const res = { isRange: slider.type === 'range',
                            checkboxGone: !document.getElementById('nav-sim-ff') };
              nav.setFactor(30);                 // not a step -> snaps to 25
              res.snap30 = nav.debug().factor;
              const steps = [];
              for (let i = 0; i <= +slider.max; i++) {
                slider.value = String(i);
                slider.dispatchEvent(new Event('input', { bubbles: true }));
                steps.push(nav.debug().factor);
              }
              res.steps = steps;
              res.topLabel = document.getElementById('nav-sim-factor-val').textContent;
              nav.setFactor(1);
              res.bottomLabel = document.getElementById('nav-sim-factor-val').textContent;
              return res;
            }""")
        await page.focus("#nav-sim-factor")
        await page.keyboard.press("ArrowRight")   # 1× -> next log step, 2×
        out["tempoSlider"]["afterArrow"] = (await debug(page))["factor"]
        # ---- fast-forward multiplier slider (×1 = aus, the old checkbox) ----
        out["ffSlider"] = await page.evaluate(
            """() => {
              const nav = window.agenticNav;
              const slider = document.getElementById('nav-sim-ffmult');
              const res = { isRange: slider.type === 'range' };
              nav.setFFMultiplier(5);            // not a step -> snaps to 4
              res.snap5 = nav.debug().ffMax;
              const steps = [];
              for (let i = 0; i <= +slider.max; i++) {
                slider.value = String(i);
                slider.dispatchEvent(new Event('input', { bubbles: true }));
                steps.push(nav.debug().ffMax);
              }
              res.steps = steps;
              nav.setFFMultiplier(1);
              res.ausLabel = document.getElementById('nav-sim-ffmult-val').textContent;
              return res;
            }""")
        await page.evaluate("() => window.agenticNav.setFactor(25)")

        # Hash frozen while nav runs.
        out["hash"] = {"before": hash_before,
                       "during": await page.evaluate("() => location.hash")}
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-3d-light.png")

        # ---- follow camera: 2D/3D + deterministic auto-zoom -----------------
        await page.click("#nav-camera")
        await page.wait_for_timeout(400)
        pitch_2d = await page.evaluate("() => window.agenticMaps[0].map.getPitch()")
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-2d.png")
        await page.click("#nav-camera")
        await page.wait_for_timeout(400)
        pitch_3d = await page.evaluate("() => window.agenticMaps[0].map.getPitch()")
        out["camera"] = {"pitch2d": pitch_2d, "pitch3d": pitch_3d}
        # Auto-zoom: pause, then seek a long fast straight (far band) vs.
        # right before its next maneuver (near band); seek snaps the camera.
        await page.click("#nav-sim-toggle")    # Pause
        straight = await page.evaluate(
            """() => {
              const marks = window.agenticNav.marks();
              for (let k = 0; k + 1 < marks.length; k++) {
                const len = marks[k + 1].at - marks[k].at;
                if (len > 4000 && marks[k].speed > 20) {
                  return { start: marks[k].at, end: marks[k + 1].at, len,
                           speed: marks[k].speed };
                }
              }
              return null;
            }""")
        out["autoZoom"] = {"straight": straight}
        if straight:
            await page.evaluate("(s) => window.agenticNav.seek(s + 100)", straight["start"])
            await page.wait_for_timeout(300)
            far = await page.evaluate(
                "() => ({ zoom: +window.agenticMaps[0].map.getZoom().toFixed(2),"
                " d: Math.round(window.agenticNav.debug().distToNext),"
                " band: window.agenticNav.debug().zoomBand })")
            await page.evaluate("(s) => window.agenticNav.seek(s - 150)", straight["end"])
            await page.wait_for_timeout(300)
            near = await page.evaluate(
                "() => ({ zoom: +window.agenticMaps[0].map.getZoom().toFixed(2),"
                " d: Math.round(window.agenticNav.debug().distToNext),"
                " band: window.agenticNav.debug().zoomBand })")
            out["autoZoom"].update({"far": far, "near": near})

        # ---- lane guidance ONLY when it matters ------------------------------
        # OSRM's first-intersection convention: step N's lanes describe step
        # N's own maneuver point — so approaching mark k shows marks[k].lanes.
        # The row is additionally gated: only DISCRIMINATING lane data (some
        # valid, some not), and only inside the distance window (800 m, 2 km
        # at motorway segment speed).
        lane_mark = await page.evaluate(
            """() => {
              const marks = window.agenticNav.marks();
              for (let k = 2; k < marks.length; k++) {
                if (marks[k].lanes >= 3 && marks[k].lanesValid >= 1
                    && marks[k].lanesValid < marks[k].lanes) {
                  return { k, at: marks[k].at, lanes: marks[k].lanes,
                           prevAt: marks[k - 1].at, prevSpeed: marks[k - 1].speed };
                }
              }
              return null;
            }""")
        out["lanes"] = {"mark": lane_mark}
        if lane_mark:
            # Near the maneuver: the row is there.
            await page.evaluate("(s) => window.agenticNav.seek(s)", lane_mark["at"] - 350)
            await page.wait_for_timeout(400)
            out["lanes"]["row"] = await page.evaluate(
                """() => {
                  const box = document.getElementById('nav-lanes');
                  return {
                    shown: box.classList.contains('open') && box.offsetParent !== null,
                    laneCount: box.querySelectorAll('.lane').length,
                    brightCount: box.querySelectorAll('.lane:not(.off)').length,
                    dimCount: box.querySelectorAll('.lane.off').length,
                    arrows: box.querySelectorAll('.lane svg').length,
                  };
                }""")
            await page.screenshot(path=f"{SCRATCH}/pw-navigation-lanes.png")
            # Far from the maneuver (same segment, beyond the window): hidden —
            # only assertable when the segment is long enough to get outside.
            window_m = 2000 if lane_mark["prevSpeed"] > 22 else 800
            far_s = max(lane_mark["prevAt"] + 10, lane_mark["at"] - window_m - 2500)
            if lane_mark["at"] - far_s > window_m + 150:
                await page.evaluate("(s) => window.agenticNav.seek(s)", far_s)
                await page.wait_for_timeout(300)
                out["lanes"]["hiddenFar"] = await page.evaluate(
                    "() => !document.getElementById('nav-lanes')"
                    ".classList.contains('open')")
            # All-valid fixture: if every lane works there is no instruction —
            # mutate the live step's lanes, re-seek, the row must stay hidden.
            out["lanes"]["hiddenAllValid"] = await page.evaluate(
                """([k, at]) => {
                  const step = window.agenticMaps[0].routes[0].steps[k];
                  const was = step.lanes.map((lane) => lane.valid);
                  step.lanes.forEach((lane) => { lane.valid = true; });
                  window.agenticNav.seek(at - 350);
                  const hidden = !document.getElementById('nav-lanes')
                    .classList.contains('open');
                  step.lanes.forEach((lane, i) => { lane.valid = was[i]; });
                  window.agenticNav.seek(at - 350);
                  const backOn = document.getElementById('nav-lanes')
                    .classList.contains('open');
                  return { hidden, backOn };
                }""", [lane_mark["k"], lane_mark["at"]])
            # …and a step WITHOUT lanes shows no row either.
            no_lane_mark = await page.evaluate(
                """() => {
                  const marks = window.agenticNav.marks();
                  for (let k = 2; k < marks.length; k++) {
                    if (marks[k].lanes === 0) return { k, at: marks[k].at };
                  }
                  return null;
                }""")
            if no_lane_mark:
                await page.evaluate("(s) => window.agenticNav.seek(s)",
                                    no_lane_mark["at"] - 350)
                await page.wait_for_timeout(300)
                out["lanes"]["hiddenWithout"] = await page.evaluate(
                    "() => !document.getElementById('nav-lanes')"
                    ".classList.contains('open')")

        # ---- straight-road fast-forward: two timed runs ----------------------
        # Same start, same distance to cover, same factor — only the FF
        # multiplier slider differs (×1 = aus vs ×16). FF may compress
        # everything beyond 800 m of the maneuver.
        async def timed_run(mult: int) -> float:
            await page.evaluate("(s) => window.agenticNav.seek(s)", straight["start"] + 10)
            await page.evaluate("(m) => window.agenticNav.setFFMultiplier(m)", mult)
            target = straight["start"] + 10 + 3000
            t0 = time.monotonic()
            await page.evaluate("() => window.agenticNav.run()")
            await page.wait_for_function(
                "(t) => window.agenticNav.debug().s >= t", arg=target, timeout=120000)
            elapsed = time.monotonic() - t0
            await page.evaluate("() => window.agenticNav.pause()")
            return round(elapsed, 2)

        await page.evaluate("() => window.agenticNav.setFactor(10)")
        plain_s = await timed_run(1)
        ff_s = await timed_run(16)
        await page.evaluate("() => window.agenticNav.setFFMultiplier(1)")
        out["fastForward"] = {"plainSeconds": plain_s, "ffSeconds": ff_s}

        # ---- skip-to-announcement: lands just before the threshold ----------
        # Paused on purpose: the jump must land and STAY paused (item: skip
        # works while paused). Factor 1 -> lead is ~2 s at segment speed.
        await page.evaluate("() => window.agenticNav.setFactor(1)")
        await page.evaluate("(s) => window.agenticNav.seek(s)", straight["start"] + 10)
        target_pt = await page.evaluate("() => window.agenticNav.announceTarget()")
        await page.click("#nav-sim-announce")
        after_skip = await debug(page)
        out["skipAnnounce"] = {
            "target": target_pt,
            "stayedPaused": after_skip["state"] == "paused",
            "distToManeuver": round(target_pt["maneuverAt"] - after_skip["s"], 1)
            if target_pt else None,
        }
        # A second skip from here targets the NEXT threshold further along.
        target2 = await page.evaluate("() => window.agenticNav.announceTarget()")
        await page.click("#nav-sim-announce")
        after2 = await debug(page)
        out["skipAnnounce"]["second"] = {
            "target": target2,
            "advanced": after2["s"] > after_skip["s"] + 1,
            "distToManeuver": round(target2["maneuverAt"] - after2["s"], 1)
            if target2 else None,
        }

        # ---- backward announcement stepping ("« Ansage") ---------------------
        # Symmetric to the forward skip: land ~2 s before the PREVIOUS
        # announcement point (which then lies AHEAD again and re-fires on
        # resume); repeated presses walk backward announcement by
        # announcement. Still paused throughout.
        back_target = await page.evaluate("() => window.agenticNav.announceBackTarget()")
        await page.click("#nav-sim-announce-prev")
        after_back = await debug(page)
        out["announceBack"] = {
            "target": back_target,
            "movedBack": after_back["s"] < after2["s"] - 1,
            "stayedPaused": after_back["state"] == "paused",
            "distToManeuver": round(back_target["maneuverAt"] - after_back["s"], 1)
            if back_target else None,
        }
        # At the very start a further back-step is a graceful no-op.
        await page.evaluate("() => window.agenticNav.seek(0)")
        await page.click("#nav-sim-announce-prev")
        await page.click("#nav-sim-prev")
        at_zero = await debug(page)
        out["announceBack"]["harmlessAtZero"] = (
            at_zero["s"] < 1 and at_zero["state"] in ("paused", "running"))

        # ---- maneuver stepping, both directions ("« Manöver »") -------------
        # From mid-straight: » lands 150 m before the next maneuver; « walks
        # back to 150 m before the previous one; » again chains forward past
        # the doorstep — the fwd/back/fwd round trip ends exactly where the
        # first » landed.
        await page.evaluate("(s) => window.agenticNav.seek(s)", straight["start"] + 500)
        m0 = await debug(page)
        await page.click("#nav-sim-jump")
        fwd1 = await debug(page)
        await page.click("#nav-sim-prev")
        back1 = await debug(page)
        await page.click("#nav-sim-jump")
        fwd2 = await debug(page)
        out["stepManeuver"] = {
            "startMark": m0["markIdx"],
            "fwdDist": round(fwd1["distToNext"], 1),
            "backDist": round(back1["distToNext"], 1),
            "backMark": back1["markIdx"],
            "roundTrip": abs(fwd2["s"] - fwd1["s"]) < 2
            and fwd2["markIdx"] == fwd1["markIdx"],
            "stayedPaused": fwd2["state"] == "paused",
        }
        await page.evaluate("() => window.agenticNav.setFactor(25)")

        # ---- sim card collapse: one obvious click hides, one restores --------
        # The whole SIMULATION title row is the target (hover feedback,
        # explicit −/+ affordance); collapsed = a slim pill that must not
        # cover the banner's "Danach" tab.
        out["simCollapse"] = {"affordance": await page.evaluate(
            """() => {
              const head = document.getElementById('nav-sim-collapse');
              const sign = head.querySelector('.ns-sign');
              return {
                cursor: getComputedStyle(head).cursor,
                signExpanded: sign
                  ? getComputedStyle(sign, '::before').content : '',
                headSpansCard: head.getBoundingClientRect().width
                  >= document.getElementById('nav-sim')
                       .getBoundingClientRect().width * 0.8,
              };
            }""")}
        await page.click("#nav-sim-collapse")
        out["simCollapse"].update({
            "collapsed": await page.evaluate(
                "() => document.getElementById('nav-sim').classList.contains('collapsed')"),
            "bodyHidden": await page.evaluate(
                "() => document.querySelector('#nav-sim .ns-body').offsetParent === null"),
            "stored": await page.evaluate(
                "() => localStorage.getItem('am-maps-navsim-collapsed')"),
            "titleStillShown": await page.evaluate(
                "() => document.getElementById('nav-sim-collapse').offsetParent !== null"),
            "pill": await page.evaluate(
                """() => {
                  const card = document.getElementById('nav-sim').getBoundingClientRect();
                  const sign = getComputedStyle(document.querySelector(
                    '#nav-sim .ns-sign'), '::before').content;
                  const next = document.getElementById('nav-next').getBoundingClientRect();
                  const overlaps = !(card.left >= next.right || card.right <= next.left
                    || card.top >= next.bottom || card.bottom <= next.top);
                  return { height: Math.round(card.height),
                           width: Math.round(card.width),
                           signCollapsed: sign, overlapsDanach: overlaps };
                }"""),
        })
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-sim-collapsed.png")
        await page.click("#nav-sim-collapse")
        out["simCollapse"]["reopened"] = await page.evaluate(
            "() => !document.getElementById('nav-sim').classList.contains('collapsed')"
            " && localStorage.getItem('am-maps-navsim-collapsed') === '0'")

        # ---- traveled path grows ---------------------------------------------
        await page.evaluate("(s) => window.agenticNav.seek(s)", straight["start"])
        await page.evaluate("() => window.agenticNav.setFactor(25)")
        await page.evaluate("() => window.agenticNav.run()")
        traveled0 = (await debug(page))["traveledCount"]
        await page.wait_for_timeout(1800)
        traveled1 = (await debug(page))["traveledCount"]
        out["traveled"] = {
            "layer": await page.evaluate(
                "() => !!window.agenticMaps[0].map.getLayer('nav-traveled')"),
            "count0": traveled0, "count1": traveled1,
        }
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-sim-window.png")

        # ---- peek + re-center -------------------------------------------------
        await page.mouse.move(700, 420)
        await page.mouse.down()
        await page.mouse.move(860, 560, steps=8)
        await page.mouse.up()
        await page.wait_for_timeout(400)
        out["peek"] = {
            "peeking": (await debug(page))["peeking"],
            "chipShown": await page.evaluate(
                "() => document.getElementById('nav-recenter').offsetParent !== null"),
        }
        await page.click("#nav-recenter")
        await page.wait_for_timeout(400)
        out["peek"]["recentered"] = not (await debug(page))["peeking"]
        out["peek"]["chipGone"] = await page.evaluate(
            "() => document.getElementById('nav-recenter').offsetParent === null")

        # ---- route colour: the vivid brand blue, over every surface ----------
        out["routeColor"] = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], r = w.routes[0];
              const id = (r._legLayerIds || [])[0] || ('route-' + r.id);
              return { color: (r.color || '').toLowerCase(),
                       paint: String(w.map.getPaintProperty(id, 'line-color')).toLowerCase() };
            }""")

        # ---- chrome is UI, not copy: nothing selectable except inputs --------
        out["userSelect"] = await page.evaluate(
            """() => {
              const us = (id) => getComputedStyle(
                document.getElementById(id)).userSelect;
              return {
                banner: us('nav-banner'), bottom: us('nav-bottom'),
                sim: us('nav-sim'), menu: us('map-menu'), dev: us('dev'),
                searchInput: getComputedStyle(document.getElementById('q')).userSelect,
              };
            }""")

        # ---- side stack: mute + compass (north-up toggle) --------------------
        await page.click("#nav-mute")
        muted_on = (await debug(page))["muted"]
        await page.click("#nav-mute")
        out["sideStack"] = {
            "muteToggles": muted_on and not (await debug(page))["muted"],
        }
        # North-up: the camera locks to north while the drive goes on; the
        # chevron (not the camera) carries the direction. Heading-up again
        # on the second tap.
        await page.click("#nav-compass")
        await page.wait_for_timeout(800)
        north = await page.evaluate(
            "() => ({ northUp: window.agenticNav.debug().northUp,"
            " bearing: +window.agenticMaps[0].map.getBearing().toFixed(1),"
            " active: document.getElementById('nav-compass').classList.contains('active') })")
        await page.click("#nav-compass")
        await page.wait_for_timeout(800)
        heading_up = await page.evaluate(
            "() => ({ northUp: window.agenticNav.debug().northUp,"
            " bearing: +window.agenticMaps[0].map.getBearing().toFixed(1) })")
        out["sideStack"]["north"] = north
        out["sideStack"]["headingUp"] = heading_up

        # ---- in-nav map-type toggle: Hybrid <-> Karte, drive continues -------
        await page.click("#nav-layers")
        await page.wait_for_function(
            """() => {
              const w = window.agenticMaps[0];
              return w.view === 'hybrid' && w.routes.length
                && w.map.getLayer('route-' + w.routes[0].id + '-casing')
                && w.map.getLayer('nav-traveled');
            }""", timeout=20000)
        await page.wait_for_timeout(2500)
        out["navLayers"] = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], r = w.routes[0];
              return {
                view: w.view,
                navMode: document.body.classList.contains('nav-mode'),
                running: window.agenticNav.state() === 'running',
                emphasisKept: JSON.stringify(w.map.getPaintProperty(
                  'route-' + r.id + '-casing', 'line-width')).includes('interpolate'),
              };
            }""")
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-hybrid-toggle.png")

        # ---- attribution: compact ⓘ badge, full credits one tap away ---------
        out["navAttrib"] = await page.evaluate(
            """() => new Promise((resolve) => {
              const wrap = document.getElementById('nav-attrib-wrap');
              const badge = document.getElementById('nav-attrib');
              const ctrl = document.querySelector('.maplibregl-ctrl-attrib');
              const visible = badge.offsetParent !== null
                && parseFloat(getComputedStyle(badge).opacity) >= 0.5;
              badge.click();
              setTimeout(() => {
                const text = document.getElementById('nav-attrib-line').textContent;
                const expanded = wrap.classList.contains('open');
                document.getElementById('nav-banner').click();   // outside
                setTimeout(() => resolve({
                  badgeVisible: visible,
                  ctrlHidden: !ctrl || ctrl.offsetParent === null,
                  expanded, text,
                  collapsedAgain: !wrap.classList.contains('open'),
                }), 150);
              }, 150);
            })""")
        await page.click("#nav-layers")   # back to Karte for the later phases
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-light'"
            " && window.agenticMaps[0].map.getLayer('route-'"
            " + window.agenticMaps[0].routes[0].id + '-casing')", timeout=20000)
        await page.wait_for_timeout(800)

        # ---- in-nav route options: add a stop mid-drive ----------------------
        stops_before = await page.evaluate(
            "() => window.agenticMaps[0].routes[0].stops.length")
        await page.click("#nav-options")
        await page.wait_for_selector("#nav-route-options.open", timeout=5000)
        out["navOptions"] = {"stopsBefore": stops_before,
                             "panelStops": await page.evaluate(
                                 "() => document.querySelectorAll('#nro-stops .nro-stop').length")}
        await page.fill("#nro-add-input", "Würzburg")
        await page.wait_for_selector("#nro-suggest .result", timeout=20000)
        await page.click("#nro-suggest .result")
        await page.wait_for_function(
            "(n) => window.agenticMaps[0].routes.length"
            " && window.agenticMaps[0].routes[0].stops.length === n + 1",
            arg=stops_before, timeout=90000)
        await page.wait_for_timeout(1000)
        out["navOptions"].update(await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], r = w.routes[0];
              return {
                stopsAfter: r.stops.length,
                navActive: window.agenticNav.isActive(),
                running: window.agenticNav.state() === 'running',
                markerShown: !!document.querySelector('.am-nav-arrow'),
                traveledLayer: !!w.map.getLayer('nav-traveled'),
                emphasisKept: JSON.stringify(w.map.getPaintProperty(
                  'route-' + r.id + '-casing', 'line-width')).includes('interpolate'),
              };
            }"""))
        # Outside click closes the panel.
        await page.click("#nav-banner")
        await page.wait_for_timeout(200)
        out["navOptions"]["closedOutside"] = await page.evaluate(
            "() => !document.getElementById('nav-route-options')"
            ".classList.contains('open')")
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-options-added-stop.png")

        # ---- exit: UI restored, route kept, no leaked rAF loop -----------------
        await page.click("#nav-exit")
        await page.wait_for_function(
            "() => !document.body.classList.contains('nav-mode')", timeout=5000)
        await page.wait_for_timeout(1200)
        out["exit"] = await page.evaluate(
            """(chrome) => {
              const w = window.agenticMaps[0], r = w.routes[0];
              return {
                chromeBack: chrome.every((id) =>
                  document.getElementById(id).offsetParent !== null),
                routeKept: !!w.map.getLayer('route-' + r.id + '-casing'),
                traveledGone: !w.map.getLayer('nav-traveled'),
                markerGone: !document.querySelector('.am-nav-arrow'),
                alternatesBack: (r._altLayerIds || []).every((id) =>
                  w.map.getLayoutProperty(id, 'visibility') !== 'none'),
                state: window.agenticNav.state(),
                // Nav-only chrome leaves; the standing credit line returns.
                sideGone: document.getElementById('nav-side').offsetParent === null,
                attribBadgeGone: document.getElementById('nav-attrib')
                  .offsetParent === null,
                attribCtrlBack: (() => {
                  const ctrl = document.querySelector('.maplibregl-ctrl-attrib');
                  return !!ctrl && ctrl.offsetParent !== null;
                })(),
              };
            }""", NAV_CHROME)
        # rAF churn: the nav frame counter must stand perfectly still.
        frames0 = await page.evaluate("() => window.__navFrameCount || 0")
        await page.wait_for_timeout(700)
        frames1 = await page.evaluate("() => window.__navFrameCount || 0")
        out["exit"]["framesAfterExit"] = frames1 - frames0
        # Hash writing resumes: a camera move rewrites it.
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo({ center: [9.18, 48.78], zoom: 12 })")
        await page.wait_for_timeout(600)
        out["hash"]["afterExit"] = await page.evaluate("() => location.hash")
        # Route weights back to the map-mode numbers after exit.
        out["navWeight"]["afterExit"] = await page.evaluate(
            """() => {
              const w = window.agenticMaps[0], r = w.routes[0];
              return {
                casing: JSON.stringify(
                  w.map.getPaintProperty('route-' + r.id + '-casing', 'line-width')),
                line: JSON.stringify(w.map.getPaintProperty(
                  (r._legLayerIds || [])[0] || ('route-' + r.id), 'line-width')),
              };
            }""")

        # ---- narrow viewport: sim card starts collapsed (no stored pref) ----
        await page.evaluate(
            "() => localStorage.removeItem('am-maps-navsim-collapsed')")
        await page.set_viewport_size({"width": 700, "height": 900})
        await page.wait_for_timeout(400)
        # Mobile: no "Simulieren" in the route panel — the dev panel's
        # Simulation section is the entry (and must keep working).
        out["simCollapse"]["mobileEntryHidden"] = await page.evaluate(
            "() => getComputedStyle(document.getElementById("
            "'btn-nav-simulate')).display === 'none'")
        await start_nav_via_dev(page)
        out["simCollapse"]["narrowInitial"] = await page.evaluate(
            "() => document.getElementById('nav-sim').classList.contains('collapsed')")
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-narrow.png")
        await page.click("#nav-exit")
        await page.wait_for_function(
            "() => !document.body.classList.contains('nav-mode')", timeout=5000)
        await page.set_viewport_size({"width": 1400, "height": 900})
        await page.evaluate(
            "() => localStorage.removeItem('am-maps-navsim-collapsed')")
        await page.wait_for_timeout(600)
        # start_nav_via_dev left developer mode OFF (the dev-nav handler
        # closes the workbench before starting); the later phases expect
        # the panel standing again.
        await page.click("#btn-dev")
        await page.wait_for_selector("#dev.open", timeout=5000)

        # ---- dark theme nav (screenshot both looks) -----------------------------
        await page.click("#btn-theme")                 # auto -> hell
        await page.wait_for_timeout(400)
        await page.click("#btn-theme")                 # hell -> dunkel
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-dark'"
            " && window.agenticMaps[0].map.getLayer('route-'"
            " + window.agenticMaps[0].routes[0].id + '-casing')", timeout=15000)
        await page.wait_for_timeout(1500)
        await start_nav(page)
        await page.evaluate("() => window.agenticNav.setFactor(25)")
        await page.wait_for_timeout(2500)
        out["darkNav"] = await page.evaluate(
            "() => ({ view: window.agenticMaps[0].view,"
            " navMode: document.body.classList.contains('nav-mode'),"
            " running: window.agenticNav.state() === 'running' })")
        # The dark theme swaps the whole nav token set — banner still ours,
        # contrast still AA.
        out["paletteDark"] = await page.evaluate(CONTRAST_JS)
        out["paletteDark"]["differsFromLight"] = (
            out["paletteDark"]["bannerBg"] != out["palette"]["bannerBg"])
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-3d-dark.png")
        await page.click("#nav-exit")
        await page.wait_for_timeout(600)
        await page.click("#btn-theme")                 # dunkel -> auto (light)
        await page.wait_for_function(
            "() => window.agenticMaps[0].view === 'map-light'", timeout=15000)
        await page.wait_for_timeout(800)

        # ---- arrival: jump near the end, let it drive in ------------------------
        await start_nav(page)
        total = (await debug(page))["total"]
        await page.evaluate("() => window.agenticNav.setFactor(25)")
        await page.evaluate("(s) => window.agenticNav.seek(s)", total - 600)
        await page.wait_for_function(
            "() => document.getElementById('nav-arrival').classList.contains('open')",
            timeout=30000)
        out["arrival"] = {
            "cardShown": True,
            "headline": await page.eval_on_selector("#nav-arrival h3", "el => el.textContent"),
            "state": (await debug(page))["state"],
        }
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-arrival.png")
        # No utterance backlog after the fast run (feature-detected TTS).
        out["voiceQueue"] = await page.evaluate(
            "() => !('speechSynthesis' in window)"
            " || window.speechSynthesis.pending === false")
        # …and the normal UI returns on its own, route still standing.
        await page.wait_for_function(
            "() => !document.body.classList.contains('nav-mode')", timeout=10000)
        out["arrival"]["autoRestored"] = await page.evaluate(
            "(chrome) => chrome.every((id) =>"
            " document.getElementById(id).offsetParent !== null)"
            " && !!window.agenticMaps[0].map.getLayer('route-'"
            " + window.agenticMaps[0].routes[0].id + '-casing')", NAV_CHROME)

        # Developer mode off again — the close-zoom screenshot should show the
        # plain app, not the workbench panel.
        await page.click("#dev-close")
        await page.wait_for_timeout(200)

        # ---- close-zoom street rendering (screenshot, nav already exited) ----
        # Frankfurt old town: dense street grid, so the widened z17.5+ tables
        # actually show as drivable surfaces.
        await page.evaluate(
            "() => window.agenticMaps[0].map.jumpTo("
            "{ center: [8.6835, 50.1125], zoom: 18, pitch: 0, bearing: 0 })")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=f"{SCRATCH}/pw-navigation-close-zoom-z18.png")

        # ---- fresh visitor: sim card hidden until developer mode ------------
        # A brand-new context (no localStorage): nav starts WITHOUT the
        # SIMULATION card; the discreet "</>" entrance is there, and toggling
        # it mid-drive shows/hides the card immediately. The dev panel itself
        # never opens over nav mode.
        context2 = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            permissions=["geolocation"],
            geolocation={"latitude": DETTINGEN[0], "longitude": DETTINGEN[1]},
        )
        await context2.add_init_script(MUTE_TTS)
        page2 = await context2.new_page()
        await page2.goto(BASE + "/#@48.9,9.0,9z&view=map-light&lang=de", wait_until="load")
        await wait_map(page2)
        await build_route(page2)
        await start_nav(page2)
        out["simDefault"] = await page2.evaluate(
            """() => ({
              devStored: localStorage.getItem('am-maps-devmode'),
              navMode: document.body.classList.contains('nav-mode'),
              running: window.agenticNav.state() === 'running',
              simHidden: document.getElementById('nav-sim').offsetParent === null,
              bannerShown: document.getElementById('nav-banner').offsetParent !== null,
              bottomShown: document.getElementById('nav-bottom').offsetParent !== null,
              devBtnShown: document.getElementById('btn-dev').offsetParent !== null,
              devBtnDiscreet: parseFloat(getComputedStyle(
                document.getElementById('devmode')).opacity) <= 0.7,
              devPanelHidden: document.getElementById('dev').offsetParent === null,
            })""")
        await page2.screenshot(path=f"{SCRATCH}/pw-navigation-default-no-sim.png")
        # Flip developer mode ON mid-drive: the card appears immediately …
        await page2.click("#btn-dev")
        await page2.wait_for_timeout(300)
        out["simDefault"]["simShownAfterToggle"] = await page2.evaluate(
            "() => document.getElementById('nav-sim').offsetParent !== null")
        out["simDefault"]["panelStillHiddenInNav"] = await page2.evaluate(
            "() => document.getElementById('dev').offsetParent === null")
        await page2.screenshot(path=f"{SCRATCH}/pw-navigation-devmode-sim.png")
        # … and OFF hides it again.
        await page2.click("#btn-dev")
        await page2.wait_for_timeout(300)
        out["simDefault"]["simHiddenAfterOff"] = await page2.evaluate(
            "() => document.getElementById('nav-sim').offsetParent === null")
        await context2.close()

        out["consoleErrors"] = [
            e for e in errors if "/api/v1/maps/live/" not in e["url"]][:8]
        await browser.close()

    print(json.dumps(out, indent=2, ensure_ascii=False))

    fix1_ok = 0 <= out["fix1"]["themeToRouteMs"] < 500
    f2 = out["fix2"]
    fix2_ok = (
        f2["afterLock"]["bearing"] == 0 and f2["afterLock"]["active"]
        and f2["afterLock"]["pitch"] > 50                      # pitch untouched
        and f2["lockedDrag"]["maxBearing"] > 2                 # gesture rotated…
        and abs(f2["lockedDrag"]["bearing"]) < 1               # …and snapped back
        and abs(f2["freeDrag"]["bearing"]) > 2                 # unlocked: stays
        and not f2["freeDrag"]["active"]
        and f2["after3d"]["pitch"] < 0.5                       # 3D: pitch only…
        and abs(f2["after3d"]["bearing"] - f2["bearingBefore3d"]) < 0.5
    )
    e = out["entry"]
    entry_ok = (
        out["startVisible"] and e["navMode"] and e["chromeHidden"]
        and e["devButtonShown"] and e["devPanelHidden"] and e["routePanelHidden"]
        and e["bannerShown"] and e["bottomShown"] and e["simShown"]
        and e["sideShown"] and e["attribBadgeShown"] and e["attribLineHidden"]
        and e["markerShown"] and e["pitch"] > 55
        # wide stage: whole banner centered at a sane max width
        and e["banner"]["width"] <= 760 and e["banner"]["centerOffset"] <= 4
        and (out["alternatesPresent"] == 0 or (
            e["alternatesHidden"]["layersOff"] and e["alternatesHidden"]["badgesOff"]))
    )
    hud_ok = (
        out["countdown"]["ok"]
        and out["bar"]["changed"]
        and out["danach"]["shown"] and len(out["danach"]["text"]) > 0
        and out["cycle"]["distinct"] >= 3
    )
    bottom_ok = (
        out["bottom"]["remaining1"] < out["bottom"]["remaining0"]
        and bool(re.match(r"^\d{2}:\d{2}$", out["bottom"]["eta"]))
        # the TOTAL leads: remaining time big …
        and bool(re.search(r"\d+ min$", out["bottom"]["timeLeft"]))
        and out["bottom"]["timeLeftPx"] >= 22
        # … distance secondary next to it
        and bool(re.match(r"^\d+(\.\d+)? (km|m|mi|ft)$", out["bottom"]["remainingText"]))
        and bool(re.search(r"\d+ (km/h|mph)", out["bottom"]["speed"]))
    )
    ln = out["lanes"]
    lanes_ok = (
        ln["mark"] is not None
        and ln["row"]["shown"]
        and ln["row"]["laneCount"] == ln["mark"]["lanes"]
        and ln["row"]["arrows"] == ln["row"]["laneCount"]
        and ln["row"]["brightCount"] >= 1
        and ln["row"]["dimCount"] >= 1              # discriminating data only
        and ln.get("hiddenFar", True)               # far out: no lane row
        and ln["hiddenAllValid"]["hidden"]          # all-valid: nothing to say
        and ln["hiddenAllValid"]["backOn"]
        and ln.get("hiddenWithout", True)
    )
    camera_ok = out["camera"]["pitch2d"] < 0.5 and out["camera"]["pitch3d"] > 55
    az = out["autoZoom"]
    autozoom_ok = (
        az["straight"] is not None
        and az["near"]["zoom"] > az["far"]["zoom"] + 0.5
        and az["near"]["band"] == "near" and az["far"]["band"] == "far"
    )
    ff = out["fastForward"]
    ff_ok = ff["ffSeconds"] < ff["plainSeconds"] * 0.7
    traveled_ok = (
        out["traveled"]["layer"] and out["traveled"]["count1"] > out["traveled"]["count0"]
    )
    hash_ok = (
        out["hash"]["during"] == out["hash"]["before"]         # frozen during nav
        and out["hash"]["afterExit"] != out["hash"]["before"]  # resumed after
    )
    peek_ok = (
        out["peek"]["peeking"] and out["peek"]["chipShown"]
        and out["peek"]["recentered"] and out["peek"]["chipGone"]
    )
    x = out["exit"]
    exit_ok = (
        x["chromeBack"] and x["routeKept"] and x["traveledGone"] and x["markerGone"]
        and x["alternatesBack"] and x["state"] == "idle"
        and x["sideGone"] and x["attribBadgeGone"] and x["attribCtrlBack"]
        and x["framesAfterExit"] == 0
    )
    dark_ok = (
        out["darkNav"]["view"] == "map-dark" and out["darkNav"]["navMode"]
        and out["darkNav"]["running"]
    )
    arrival_ok = (
        out["arrival"]["cardShown"]
        and out["arrival"]["headline"] == "Ziel erreicht"
        and out["arrival"]["state"] == "arrived"
        and out["arrival"]["autoRestored"]
    )
    console_ok = not out["consoleErrors"]

    # ---- polish pass verdicts --------------------------------------------
    pal, pald = out["palette"], out["paletteDark"]

    def navy_surface(hue_v, dist_v):
        # Deep blue range (indigo-leaning navy), demonstrably NOT Google's
        # navigation blue (#1a73e8 = rgb 26,115,232).
        return 195 <= hue_v <= 255 and dist_v > 60

    palette_ok = (
        not pal["isOldGoogleGreen"] and not pald["isOldGoogleGreen"]
        and navy_surface(pal["bannerHue"], pal["googleBlueDist"])
        and navy_surface(pald["bannerHue"], pald["googleBlueDist"])
        # ORANGE countdown (clearly warmer than the old amber, whose hue sat
        # at ~38°) and instruction ink both clear FULL AA (4.5:1) against
        # the banner, in both themes.
        and 18 <= pal["accentHue"] <= 35 and 18 <= pald["accentHue"] <= 35
        and pal["countdownRatio"] >= 4.5 and pal["textRatio"] >= 4.5
        and pald["countdownRatio"] >= 4.5 and pald["textRatio"] >= 4.5
        # top and bottom are ONE surface system: the bottom bar sits in the
        # same blue hue range and its orange total clears AA on it.
        and 195 <= pal["bottomHue"] <= 255 and 195 <= pald["bottomHue"] <= 255
        and pal["timeRatio"] >= 4.5 and pald["timeRatio"] >= 4.5
        and pald["differsFromLight"]
    )
    # The route line: the vivid brand blue at its SOURCE (MapRoute.color
    # default) — the azure family is deliberately Google-adjacent in hue but
    # measurably not Google's #1a73e8, and nowhere near the old amber.
    rc = out["routeColor"]
    route_rgb = tuple(int(rc["color"][i:i + 2], 16) for i in (1, 3, 5))
    route_color_ok = (
        rc["color"] == "#2e6be6" and rc["paint"] == rc["color"]
        and (sum((a - b) ** 2 for a, b in zip(route_rgb, (26, 115, 232))) ** 0.5) > 15
    )
    us = out["userSelect"]
    user_select_ok = (
        all(us[key] == "none" for key in ("banner", "bottom", "sim", "menu", "dev"))
        and us["searchInput"] != "none"
    )
    ss = out["sideStack"]
    side_stack_ok = (
        ss["muteToggles"]
        and ss["north"]["northUp"] and ss["north"]["active"]
        and abs(ss["north"]["bearing"]) < 0.5          # camera locked north
        and not ss["headingUp"]["northUp"]
        and abs(ss["headingUp"]["bearing"]) > 1        # chase cam turned back
    )
    nl = out["navLayers"]
    nav_layers_ok = (
        nl["view"] == "hybrid" and nl["navMode"] and nl["running"]
        and nl["emphasisKept"]
    )
    na = out["navAttrib"]
    nav_attrib_ok = (
        na["badgeVisible"] and na["ctrlHidden"] and na["expanded"]
        and "OpenStreetMap" in na["text"]
        and bool(re.search(
            r"LGL|HVBG|Orthophoto|dl-de|Copernicus|Blue Marble|GIBS|LGLN|Bayerische",
            na["text"]))
        and na["collapsedAgain"]
    )
    no = out["navOptions"]
    nav_options_ok = (
        no["panelStops"] >= 2
        and no["stopsAfter"] == no["stopsBefore"] + 1
        and no["navActive"] and no["running"] and no["markerShown"]
        and no["traveledLayer"] and no["emphasisKept"]
        and no["closedOutside"]
    )
    ab = out["announceBack"]

    def back_window_ok(target, dist):
        if not target or dist is None:
            return False
        lead = 2 * target["speed"] + 45
        return target["threshold"] <= dist <= target["threshold"] + lead

    announce_back_ok = (
        ab["movedBack"] and ab["stayedPaused"]
        and back_window_ok(ab["target"], ab["distToManeuver"])
        and ab["harmlessAtZero"]
    )
    sm = out["stepManeuver"]
    step_maneuver_ok = (
        140 <= sm["fwdDist"] <= 160 and 140 <= sm["backDist"] <= 160
        and sm["backMark"] == sm["startMark"] - 1
        and sm["roundTrip"] and sm["stayedPaused"]
    )
    sd = out["simDefault"]
    sim_hidden_by_default_ok = (
        sd["devStored"] is None and sd["navMode"] and sd["running"]
        and sd["simHidden"] and sd["bannerShown"] and sd["bottomShown"]
        and sd["devBtnShown"] and sd["devBtnDiscreet"] and sd["devPanelHidden"]
        and sd["simShownAfterToggle"] and sd["panelStillHiddenInNav"]
        and sd["simHiddenAfterOff"]
    )
    g = out["bottom"]["groups"]
    bottombar_groups_ok = (
        g["totals"] and g["controls"] and g["totalsBeforeControls"]
        and g["speedIsChip"] and g["timeColored"]
        # Google's ergonomics: round ✕ cancel leftmost, options rightmost,
        # sound out of the bar and into the side stack.
        and g["exitFirst"] and g["exitRound"] and g["exitGlyphOnly"]
        and g["exitRedInk"] and g["optionsLast"]
        and not g["muteInBar"] and g["muteInSide"] and g["compassInSide"]
    )
    ts = out["tempoSlider"]
    tempo_slider_ok = (
        ts["isRange"] and ts["checkboxGone"]
        and ts["snap30"] == 25
        and ts["steps"] == [1, 2, 5, 10, 25, 50, 100, 250, 500]
        and ts["topLabel"] == "500×" and ts["bottomLabel"] == "1×"
        and ts["afterArrow"] == 2
    )
    fs = out["ffSlider"]
    ff_slider_ok = (
        fs["isRange"] and fs["snap5"] == 4
        and fs["steps"] == [1, 2, 4, 8, 16, 32]
        and fs["ausLabel"] == "aus"
    )
    sc = out["simCollapse"]
    sim_collapse_ok = (
        sc["collapsed"] and sc["bodyHidden"] and sc["stored"] == "1"
        and sc["titleStillShown"] and sc["reopened"] and sc["narrowInitial"]
        and sc["mobileEntryHidden"]
        # Obvious affordance: whole title row clickable (pointer + spans the
        # card) with an explicit − / + sign …
        and sc["affordance"]["cursor"] == "pointer"
        and sc["affordance"]["headSpansCard"]
        and "−" in sc["affordance"]["signExpanded"]
        and "+" in sc["pill"]["signCollapsed"]
        # … and the collapsed state is a slim pill clear of the Danach tab.
        and sc["pill"]["height"] <= 44 and sc["pill"]["width"] <= 220
        and not sc["pill"]["overlapsDanach"]
    )
    nw = out["navWeight"]
    chevron_route_width_ok = (
        nw["chevronPx"] >= 64
        # nav: zoom-interpolated heavy widths on casing, line and traveled …
        and '"interpolate"' in nw["casing"] and '"interpolate"' in nw["line"]
        and nw["line"] == nw["traveled"]
        # … map-mode numbers back after exit (map.js _drawRoute: 5 in 8).
        and nw["afterExit"]["casing"] == "8" and nw["afterExit"]["line"] == "5"
    )
    cz = out["closeZoom"]
    close_zoom_width_ok = (
        cz["minor"] is not None
        and cz["minor"]["topZoom"] == 20 and cz["minor"]["topWidth"] > 30
        and cz["major"]["topZoom"] == 20 and cz["major"]["topWidth"] > 40
        and cz["highway"]["topZoom"] == 20 and cz["highway"]["topWidth"] > 40
        and cz["minorZ15Value"] == 2                # mid-zoom anchor untouched
        and cz["minorCasingGapTop"] == cz["minor"]["topWidth"]  # casing in lockstep
    )
    sk = out["skipAnnounce"]

    def skip_window_ok(target, dist):
        if not target or dist is None:
            return False
        lead = 2 * target["speed"] + 45      # ~2 s at segment speed + margin
        return target["threshold"] <= dist <= target["threshold"] + lead

    skip_announce_ok = (
        sk["stayedPaused"]
        and skip_window_ok(sk["target"], sk["distToManeuver"])
        and sk["second"]["advanced"]
        and skip_window_ok(sk["second"]["target"], sk["second"]["distToManeuver"])
    )

    verdict = {
        "fix1_instant_restore_ok": fix1_ok,
        "fix2_control_semantics_ok": fix2_ok,
        "nav_entry_ok": entry_ok,
        "hud_ok": hud_ok,
        "lane_guidance_ok": lanes_ok,
        "bottom_bar_ok": bottom_ok,
        "voice_queue_ok": out["voiceQueue"],
        "camera_2d3d_ok": camera_ok,
        "auto_zoom_ok": autozoom_ok,
        "fast_forward_ok": ff_ok,
        "traveled_path_ok": traveled_ok,
        "hash_suspended_ok": hash_ok,
        "peek_recenter_ok": peek_ok,
        "exit_restore_ok": exit_ok,
        "dark_theme_nav_ok": dark_ok,
        "arrival_ok": arrival_ok,
        "console_ok": console_ok,
        "palette_ok": palette_ok,
        "sim_hidden_by_default_ok": sim_hidden_by_default_ok,
        "bottombar_groups_ok": bottombar_groups_ok,
        "tempo_slider_ok": tempo_slider_ok,
        "ff_slider_ok": ff_slider_ok,
        "sim_collapse_ok": sim_collapse_ok,
        "chevron_route_width_ok": chevron_route_width_ok,
        "close_zoom_width_ok": close_zoom_width_ok,
        "skip_announce_ok": skip_announce_ok,
        "announce_back_ok": announce_back_ok,
        "step_maneuver_ok": step_maneuver_ok,
        "route_color_ok": route_color_ok,
        "user_select_ok": user_select_ok,
        "side_stack_ok": side_stack_ok,
        "nav_layers_toggle_ok": nav_layers_ok,
        "nav_attribution_badge_ok": nav_attrib_ok,
        "nav_route_options_ok": nav_options_ok,
    }
    print(json.dumps(verdict, indent=2))
    ok = all(verdict.values())
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
