"""Headless check: the three.js globe handover in web/globe.js, live.

The globe shipped with zero live testing (its vendor mount was broken for the
whole build), which is exactly how its bugs survived phase verification. This
drives the real handover in a real browser, in hybrid AND map-dark:

  1. Above HANDOVER_ZOOM the globe canvas is hidden and the flat map visible.
  2. Zooming below the handover shows the globe, hides the flat map canvas
     (no ghost sphere inside a flat map), and places labels (countries at
     least; capped at the label budget).
  3. Dragging the globe rotates it: the map centre longitude follows the
     pointer, so zooming back in lands where the drag left off.
  4. Wheel-zooming on the globe canvas returns to the flat map: globe hidden
     again, flat canvas visible again, and no globe label left floating over
     the flat map (the label layer must be display:none).
  5. The whole pass produces no console errors beyond the allowlisted
     dev-server noise (`/debug/enabled` 404).

Overlay-parity pass (the owner's "ALL kinds of overlays shall ALWAYS work in
both modes"):
  6. A multi-leg route (Dettingen → Hannover → Frankfurt), stop pins, a
     country highlight (Frankreich) and every geo-highlight kind (radius,
     polygon, line, ping) are set up at city zoom, then the camera crosses
     the handover: the globe MUST take the stage (the old route-suppression
     stopgap is gone), the route is on the sphere as ribbons in >= 2 leg
     colours (scene graph AND a readPixels sample), ONE total badge shows,
     the stop pins are mirrored as billboards, the country border is traced
     in the accent colour, and each geo highlight has a ribbon.
  7. Rotating to the far side of the planet limb-culls pins and badge.
  8. Zooming back in restores the flat rendition untouched (leg layers,
     per-leg badges, visible flat canvas).

Tilt pass (close-zoom 3D):
  9. Rotate/pitch gestures are enabled; ctrl+drag actually tilts/rotates.
 10. The 3D button animates to pitch ~55° and ONLY pitch, extruded buildings
     render at z16 Stuttgart (queryRenderedFeatures on 'buildings-3d'
     non-empty), the compass needle mirrors the bearing and the hash carries
     `,Nh,Nt`. The compass is a NORTH-LOCK toggle: clicking it animates the
     bearing to 0 and never touches the pitch; flattening is the 3D button's
     job alone (together they still drop the hash tokens and fade the
     buildings).
 11. The dark theme renders the tilted city too (buildings present).

Requires a running dev server (`agentic-maps-dev`, see BASE below). Chromium
resolution follows tools/verify_render.py's pattern: `playwright install
chromium`, or `AGENTIC_MAPS_RENDER_CHROMIUM_PATH` pointed at a binary already
on disk.
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

# Console noise that is environmental, not a globe bug: the debug bridge
# probes an endpoint the server only mounts with AGENTIC_MAPS_DEBUG=1.
ALLOWED_CONSOLE = ("Failed to load resource",)

STATE = """() => {
  const w = window.agenticMaps[0], m = w.map, g = window.agenticGlobe;
  const c = m.getCenter();
  const layerShown = g && g.labelLayer
    && getComputedStyle(g.labelLayer).display !== 'none';
  const labels = !layerShown ? [] : [...document.querySelectorAll('.am-globe-label')]
    .filter(el => getComputedStyle(el).display !== 'none'
                  && parseFloat(el.style.opacity || '1') > 0.05);
  return {
    zoom: +m.getZoom().toFixed(2),
    lat: +c.lat.toFixed(3), lng: +c.lng.toFixed(3),
    globeVisible: g ? g.visible : null,
    globeCanvasShown: g ? getComputedStyle(g.canvas).display !== 'none' : null,
    flatCanvasVisible: getComputedStyle(m.getCanvas()).visibility === 'visible',
    labelLayerShown: !!layerShown,
    labelCount: labels.length,
  };
}"""


async def globe_pass(page, view: str) -> dict:
    out: dict = {"view": view}
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@48.78,9.18,5.2z&view={view}&lang=de", wait_until="load")
    await page.wait_for_function(
        "window.agenticGlobe && window.agenticMaps[0].map.isStyleLoaded()", timeout=40000
    )
    await page.wait_for_timeout(1500)
    out["flat"] = await page.evaluate(STATE)

    # Cross the handover.
    await page.evaluate("window.agenticMaps[0].map.zoomTo(3.4, { duration: 400 })")
    await page.wait_for_timeout(2500)
    out["globe"] = await page.evaluate(STATE)
    await page.screenshot(path=f"{SCRATCH}/pw-globe-{view}.png")

    # Drag westwards by 300 px: the centre longitude must move east.
    await page.mouse.move(640, 430)
    await page.mouse.down()
    for x in range(640, 340, -30):
        await page.mouse.move(x, 430)
        await page.wait_for_timeout(20)
    await page.mouse.up()
    await page.wait_for_timeout(800)
    out["afterDrag"] = await page.evaluate(STATE)

    # Wheel back in over the globe canvas -> flat map, where the drag ended.
    for _ in range(8):
        await page.mouse.wheel(0, -240)
        await page.wait_for_timeout(150)
    await page.wait_for_timeout(1500)
    out["backFlat"] = await page.evaluate(STATE)
    await page.screenshot(path=f"{SCRATCH}/pw-globe-{view}-back.png")
    return out


STOPS = [
    # Dettingen an der Erms -> Hannover -> Frankfurt, with the app's own
    # stop colours (start green, via amber, destination red).
    (9.2925, 48.5386, "#3fb27f"),
    (9.7386, 52.3745, "#f2a33c"),
    (8.6821, 50.1109, "#e2574c"),
]


async def overlay_pass(page) -> dict:
    out: dict = {}
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@48.6,9.25,10z&view=map-light&lang=de", wait_until="load")
    await page.wait_for_function(
        "window.agenticGlobe && window.agenticMaps[0].map.isStyleLoaded()", timeout=40000
    )
    await page.wait_for_timeout(1200)

    # Build the trip the same way the app does: the real /route API with a
    # via (so the response carries stops+legs), addRoute, and stop pins
    # exactly as drawStopMarkers mints them.
    out["setup"] = await page.evaluate(
        """async (stops) => {
          const map = window.agenticMaps[0];
          const res = await fetch('/api/v1/maps/route', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              route_id: 'verify-globe-trip', mode: 'car',
              start: { lat: stops[0][1], lon: stops[0][0] },
              via: [{ lat: stops[1][1], lon: stops[1][0] }],
              end: { lat: stops[2][1], lon: stops[2][0] },
              from_location: 'Dettingen', to_location: 'Frankfurt', steps: true,
            }),
          });
          if (!res.ok) return { error: await res.text() };
          const route = await res.json();
          map.addRoute(route);
          for (const [lon, lat, color] of stops) {
            const pin = document.createElement('div');
            pin.className = 'am-map-search-pin';
            pin.innerHTML = '<i style="background:' + color + '"></i>';
            new maplibregl.Marker({ element: pin, anchor: 'bottom' })
              .setLngLat([lon, lat]).addTo(map.map);
          }
          // Every other highlight kind, once each.
          map.addHighlight({ at: { lat: 48.55, lon: 9.3 }, kind: 'radius',
            radius_m: 400, label: 'Radius', color: '#7cc4ff', opacity: 0.18 });
          map.addHighlight({ kind: 'polygon', label: 'Areal', color: '#f5d76e',
            opacity: 0.2, polygon: [
              { lat: 48.60, lon: 9.20 }, { lat: 48.60, lon: 9.28 },
              { lat: 48.56, lon: 9.28 }, { lat: 48.56, lon: 9.20 }] });
          map.addHighlight({ kind: 'line', color: '#e2574c', width: 6,
            line: [{ lat: 48.70, lon: 9.10 }, { lat: 48.75, lon: 9.20 }] });
          map.pingAt([9.5, 49.0], 'Ping', {});
          const hits = await (await fetch(
            '/api/v1/maps/geo/countries/search?lang=de&q=Frankreich')).json();
          if (hits.length) map.highlightCountry(hits[0]);
          return {
            legs: (route.legs || []).length,
            stops: (route.stops || []).length,
            legColors: [map.legColor(route, 0), map.legColor(route, 1)],
            geoCount: map._geo.length,
            country: !!map._country,
          };
        }""",
        STOPS,
    )
    await page.wait_for_timeout(2000)   # highlightCountry's fitBounds settles

    # Cross the handover with everything standing.
    await page.evaluate("window.agenticMaps[0].map.zoomTo(3, { duration: 400 })")
    await page.wait_for_timeout(2500)
    out["globeState"] = await page.evaluate(STATE)
    out["overlays"] = await page.evaluate(
        """() => {
          const g = window.agenticGlobe, map = window.agenticMaps[0];
          const colors = g.overlays.children.map(
            (m) => '#' + m.material.color.getHexString());
          const shown = (list) =>
            list.filter((b) => b.el.style.display !== 'none').length;
          return {
            meshCount: g.overlays.children.length,
            distinctColors: [...new Set(colors)],
            badges: g.badgeBillboards.length,
            badgesShown: shown(g.badgeBillboards),
            badgeText: g.badgeBillboards.length
              ? g.badgeBillboards[0].el.textContent : '',
            pins: g.markerBillboards.length,
            pinsShown: shown(g.markerBillboards),
            pinElements: g.markerBillboards.filter(
              (b) => b.el.querySelector('.am-map-search-pin')).length,
          };
        }"""
    )
    # Pixel truth: force a render and sample the WebGL buffer for the two leg
    # colours and the country accent.
    out["pixels"] = await page.evaluate(
        """(legColors) => {
          const g = window.agenticGlobe;
          g.renderer.render(g.scene, g.camera);
          const gl = g.renderer.getContext();
          const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
          const buf = new Uint8Array(w * h * 4);
          gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
          const rgb = (hex) => [1, 3, 5].map(
            (i) => parseInt(hex.slice(i, i + 2), 16));
          const targets = legColors.concat(['#7cc4ff']).map(rgb);
          const counts = targets.map(() => 0);
          for (let i = 0; i < buf.length; i += 16) {
            for (let t = 0; t < targets.length; t++) {
              if (Math.abs(buf[i] - targets[t][0]) <= 32
                  && Math.abs(buf[i + 1] - targets[t][1]) <= 32
                  && Math.abs(buf[i + 2] - targets[t][2]) <= 32) counts[t]++;
            }
          }
          return { legPixelCounts: counts.slice(0, 2), accentPixelCount: counts[2] };
        }""",
        out["setup"]["legColors"],
    )
    await page.screenshot(path=f"{SCRATCH}/pw-globe-overlays.png")

    # Far side of the planet: pins and badge must be limb-culled at once.
    await page.evaluate(
        "() => window.agenticMaps[0].map.jumpTo({ center: [-170.7, -48.5] })"
    )
    await page.wait_for_timeout(600)
    out["farSide"] = await page.evaluate(
        """() => {
          const g = window.agenticGlobe;
          const shown = (list) =>
            list.filter((b) => b.el.style.display !== 'none').length;
          return { pinsShown: shown(g.markerBillboards),
                   badgesShown: shown(g.badgeBillboards) };
        }"""
    )
    await page.evaluate(
        "() => window.agenticMaps[0].map.jumpTo({ center: [9.4, 49.5] })"
    )
    await page.wait_for_timeout(600)

    # Back to the flat map: the original rendition, untouched.
    await page.evaluate("window.agenticMaps[0].map.zoomTo(10, { duration: 400 })")
    await page.wait_for_timeout(2000)
    out["backFlat"] = await page.evaluate(
        """() => {
          const m = window.agenticMaps[0].map;
          return {
            zoom: +m.getZoom().toFixed(1),
            flatCanvasVisible: m.getCanvas().style.visibility !== 'hidden',
            globeShown: getComputedStyle(
              document.querySelector('.am-globe')).display !== 'none',
            legLayers: m.getStyle().layers
              .filter((l) => /^route-.*-leg\\d+$/.test(l.id)).length,
            flatBadges: document.querySelectorAll(
              '.am-map-route-badge:not(.am-globe-route-badge)').length,
            geoLayers: m.getStyle().layers
              .filter((l) => l.id.startsWith('am-geo-')).length,
          };
        }"""
    )
    await page.screenshot(path=f"{SCRATCH}/pw-globe-overlays-back.png")
    return out


async def tilt_pass(page) -> dict:
    out: dict = {}
    await page.goto("about:blank")
    await page.goto(f"{BASE}/#@48.7758,9.1829,16z&view=map-light&lang=de",
                    wait_until="load")
    await page.wait_for_function(
        "window.agenticMaps && window.agenticMaps[0] && "
        "window.agenticMaps[0].map.isStyleLoaded()", timeout=40000
    )
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.areTilesLoaded()", timeout=40000
    )
    await page.wait_for_timeout(1000)

    out["gesturesEnabled"] = await page.evaluate(
        """() => {
          const m = window.agenticMaps[0].map;
          return { dragRotate: m.dragRotate.isEnabled(),
                   touchZoomRotate: m.touchZoomRotate.isEnabled(),
                   maxPitch: m.getMaxPitch() };
        }"""
    )
    # ctrl+drag must rotate and pitch, Google-style.
    await page.mouse.move(700, 500)
    await page.keyboard.down("Control")
    await page.mouse.down()
    await page.mouse.move(780, 420, steps=10)
    await page.mouse.up()
    await page.keyboard.up("Control")
    await page.wait_for_timeout(500)
    out["afterCtrlDrag"] = await page.evaluate(
        "() => ({ bearing: +window.agenticMaps[0].map.getBearing().toFixed(1),"
        " pitch: +window.agenticMaps[0].map.getPitch().toFixed(1) })"
    )
    # Compass = north-lock: the bearing squares up, the PITCH the ctrl+drag
    # produced must survive untouched (the old behaviour dropped to 2D here).
    pitch_before = await page.evaluate(
        "() => +window.agenticMaps[0].map.getPitch().toFixed(1)")
    await page.click("#btn-compass")
    await page.wait_for_function(
        "() => Math.abs(window.agenticMaps[0].map.getBearing()) < 0.05"
        " && !window.agenticMaps[0].map.isEasing()", timeout=8000
    )
    out["compassLock"] = await page.evaluate(
        "() => ({ bearing: +window.agenticMaps[0].map.getBearing().toFixed(1),"
        " pitch: +window.agenticMaps[0].map.getPitch().toFixed(1),"
        " active: document.getElementById('btn-compass').classList.contains('active') })"
    )
    out["compassLock"]["pitchBefore"] = pitch_before
    # Unlock again; if the drag left a tilt, the 3D button (alone) flattens it.
    await page.click("#btn-compass")
    if out["compassLock"]["pitch"] > 0.5:
        await page.click("#btn-3d")
        await page.wait_for_function(
            "() => window.agenticMaps[0].map.getPitch() < 0.5", timeout=8000
        )
    await page.wait_for_timeout(400)
    out["buildingsFlat"] = await page.evaluate(
        "() => window.agenticMaps[0].map.getPaintProperty("
        "'buildings-3d', 'fill-extrusion-opacity')"
    )
    await page.click("#btn-3d")
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.getPitch() > 54", timeout=8000
    )
    # Bearing AND pitch in one ease: a bearing-only easeTo would interrupt
    # the still-running 3D-button pitch animation and freeze it short of 55.
    await page.evaluate(
        "() => window.agenticMaps[0].map.easeTo({ bearing: 30, pitch: 55, duration: 400 })"
    )
    await page.wait_for_function(
        "() => Math.abs(window.agenticMaps[0].map.getBearing() - 30) < 0.5"
        " && window.agenticMaps[0].map.getPitch() > 54.9",
        timeout=8000,
    )
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.areTilesLoaded()", timeout=40000
    )
    await page.wait_for_timeout(1500)   # URL debounce + building fade-in
    out["tilted"] = await page.evaluate(
        """() => {
          const m = window.agenticMaps[0].map;
          return {
            pitch: +m.getPitch().toFixed(1),
            bearing: +m.getBearing().toFixed(1),
            buildingsOpacity: m.getPaintProperty(
              'buildings-3d', 'fill-extrusion-opacity'),
            buildingsRendered: m.queryRenderedFeatures(
              { layers: ['buildings-3d'] }).length,
            needleTransform: document.getElementById('compass-needle').style.transform,
            btn3dLabel: document.getElementById('btn-3d').textContent,
            hash: location.hash,
          };
        }"""
    )
    await page.screenshot(path=f"{SCRATCH}/pw-tilt-light.png")

    # Split reset (the new semantics): the compass squares the bearing —
    # pitch untouched, still ~55 — and only the 3D button drops the tilt.
    await page.click("#btn-compass")
    await page.wait_for_function(
        "() => Math.abs(window.agenticMaps[0].map.getBearing()) < 0.05"
        " && !window.agenticMaps[0].map.isEasing()", timeout=8000
    )
    out["resetBearingOnly"] = await page.evaluate(
        "() => ({ pitch: +window.agenticMaps[0].map.getPitch().toFixed(1),"
        " bearing: +window.agenticMaps[0].map.getBearing().toFixed(1) })"
    )
    await page.click("#btn-3d")
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.getPitch() < 0.5", timeout=8000
    )
    await page.wait_for_timeout(800)
    out["reset"] = await page.evaluate(
        """() => {
          const m = window.agenticMaps[0].map;
          return {
            pitch: +m.getPitch().toFixed(1),
            bearing: +m.getBearing().toFixed(1),
            buildingsOpacity: m.getPaintProperty(
              'buildings-3d', 'fill-extrusion-opacity'),
            hash: location.hash,
          };
        }"""
    )

    # Leave the north-lock off — the state the pass started in.
    await page.click("#btn-compass")

    # Dark theme, tilted: the extrusions must survive the style rebuild.
    await page.evaluate("() => window.agenticMaps[0].setView('map-dark')")
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.isStyleLoaded()", timeout=30000
    )
    await page.click("#btn-3d")
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.getPitch() > 54", timeout=8000
    )
    await page.wait_for_function(
        "() => window.agenticMaps[0].map.areTilesLoaded()", timeout=40000
    )
    await page.wait_for_timeout(1200)
    out["dark"] = await page.evaluate(
        """() => {
          const m = window.agenticMaps[0].map;
          return {
            view: window.agenticMaps[0].view,
            buildingsOpacity: m.getPaintProperty(
              'buildings-3d', 'fill-extrusion-opacity'),
            buildingsRendered: m.queryRenderedFeatures(
              { layers: ['buildings-3d'] }).length,
          };
        }"""
    )
    await page.screenshot(path=f"{SCRATCH}/pw-tilt-dark.png")
    return out


async def main() -> None:
    errors: list[str] = []
    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(viewport={"width": 1280, "height": 860})
        page = await context.new_page()
        page.on(
            "console",
            lambda m: errors.append(m.text)
            if m.type == "error" and not any(a in m.text for a in ALLOWED_CONSOLE)
            else None,
        )
        page.on("pageerror", lambda e: errors.append(str(e)))

        passes = [await globe_pass(page, view) for view in ("hybrid", "map-dark")]
        overlays = await overlay_pass(page)
        tilt = await tilt_pass(page)
        await browser.close()

    out = {"passes": passes, "overlays": overlays, "tilt": tilt,
           "consoleErrors": errors[:8]}
    print(json.dumps(out, indent=2, ensure_ascii=False))

    def pass_ok(p: dict) -> bool:
        return (
            # 1: flat above the handover
            not p["flat"]["globeVisible"] and p["flat"]["flatCanvasVisible"]
            # 2: globe below it — flat hidden, labels placed
            and p["globe"]["globeVisible"] and p["globe"]["globeCanvasShown"]
            and not p["globe"]["flatCanvasVisible"]
            and p["globe"]["labelCount"] >= 10
            # 3: the drag moved the centre by a real amount
            and abs(p["afterDrag"]["lng"] - p["globe"]["lng"]) > 5
            # 4: wheel returned to the flat map, labels off with the globe
            and p["backFlat"]["zoom"] > 4.3
            and not p["backFlat"]["globeVisible"]
            and p["backFlat"]["flatCanvasVisible"]
            and not p["backFlat"]["labelLayerShown"]
        )

    o = overlays
    lower = lambda colors: [c.lower() for c in colors]
    leg_colors = lower(o["setup"]["legColors"])
    overlay_verdict = {
        # trip really is multi-leg, and every highlight kind is standing
        "setup_ok": (o["setup"].get("legs") == 2 and o["setup"].get("stops") == 3
                     and o["setup"].get("geoCount") == 3 and o["setup"].get("country")),
        # the globe took the stage WITH the route (stopgap removed)
        "globe_active_ok": (o["globeState"]["globeVisible"]
                            and o["globeState"]["globeCanvasShown"]
                            and not o["globeState"]["flatCanvasVisible"]),
        # ribbons: casing + 2 legs + 3 geo + country accent, both leg colours
        "route_ribbons_ok": (o["overlays"]["meshCount"] >= 7
                             and all(c in lower(o["overlays"]["distinctColors"])
                                     for c in leg_colors)),
        "leg_pixels_ok": all(n > 3 for n in o["pixels"]["legPixelCounts"]),
        "one_total_badge_ok": (o["overlays"]["badges"] == 1
                               and o["overlays"]["badgesShown"] == 1
                               and "min" in o["overlays"]["badgeText"]),
        "pins_ok": (o["overlays"]["pinElements"] == 3
                    and o["overlays"]["pinsShown"] >= 4),
        "country_accent_ok": o["pixels"]["accentPixelCount"] > 3,
        "limb_culling_ok": (o["farSide"]["pinsShown"] == 0
                            and o["farSide"]["badgesShown"] == 0),
        "back_flat_ok": (o["backFlat"]["flatCanvasVisible"]
                         and not o["backFlat"]["globeShown"]
                         and o["backFlat"]["legLayers"] == 2
                         and o["backFlat"]["flatBadges"] == 2
                         and o["backFlat"]["geoLayers"] >= 6),
    }

    t = tilt
    tilt_verdict = {
        "gestures_ok": (t["gesturesEnabled"]["dragRotate"]
                        and t["gesturesEnabled"]["touchZoomRotate"]
                        and t["gesturesEnabled"]["maxPitch"] >= 60),
        "ctrl_drag_ok": (abs(t["afterCtrlDrag"]["bearing"]) > 2
                         or t["afterCtrlDrag"]["pitch"] > 2),
        "pitch_55_ok": t["tilted"]["pitch"] > 54,
        "buildings_ok": (t["buildingsFlat"] == 0
                         and t["tilted"]["buildingsOpacity"] > 0
                         and t["tilted"]["buildingsRendered"] > 0),
        "compass_ok": ("rotate(-30" in t["tilted"]["needleTransform"]
                       and t["tilted"]["btn3dLabel"] == "2D"
                       # north-lock: bearing squared, pitch untouched, lit
                       and t["compassLock"]["active"]
                       and t["compassLock"]["bearing"] == 0
                       and abs(t["compassLock"]["pitch"]
                               - t["compassLock"]["pitchBefore"]) < 1
                       # split reset: compass squares, 3D button flattens
                       and t["resetBearingOnly"]["bearing"] == 0
                       and t["resetBearingOnly"]["pitch"] > 54
                       and t["reset"]["pitch"] == 0 and t["reset"]["bearing"] == 0
                       and t["reset"]["buildingsOpacity"] == 0),
        "hash_ok": (bool(re.search(r",30(\.\d+)?h,55(\.\d+)?t", t["tilted"]["hash"]))
                    and not re.search(r",-?\d+(\.\d+)?h", t["reset"]["hash"])
                    and not re.search(r",-?\d+(\.\d+)?t", t["reset"]["hash"])),
        "dark_tilt_ok": (t["dark"]["view"] == "map-dark"
                         and t["dark"]["buildingsOpacity"] > 0
                         and t["dark"]["buildingsRendered"] > 0),
    }

    verdict = ({("%s_ok" % p["view"]): pass_ok(p) for p in passes}
               | overlay_verdict | tilt_verdict | {"console_ok": not errors})
    ok = all(verdict.values())
    print(json.dumps(verdict, indent=2))
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
