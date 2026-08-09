"""Live verification of the MCP surface against a RUNNING dev server.

Connects to `http://127.0.0.1:8195/mcp` (streamable HTTP) with the official
SDK client and exercises every read-path tool for real — real Nominatim,
real OSRM demo, real Protomaps planet reads. Deliberately NOT part of the
fast suite (network + third-party services); the in-memory equivalents live
in `tests/test_mcp_server.py`.

Run: start the dev server with the mcp extra installed
(`AGENTIC_MAPS_ROUTING_BACKEND=osrm python -m agentic_maps.devserver
--port 8195`), then `python tools/verify_mcp.py [--url URL]`.

Politeness: one call per service, no loops — the public demo endpoints are
shared infrastructure.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []

SCRATCH = Path(os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir()))


def report(tool: str, ok: bool, detail: str) -> None:
    RESULTS.append((tool, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tool}: {detail}")


def payload(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def error_text(result) -> str:
    return " ".join(c.text for c in result.content)


DETTINGEN = {"lat": 48.5254, "lon": 9.3466}
FRANKFURT = {"lat": 50.1109, "lon": 8.6821}
STUTTGART_HBF = {"lat": 48.7838, "lon": 9.1829}
FIVE_STOPS = [
    {**DETTINGEN, "label": "Dettingen an der Erms"},
    {"lat": 48.5369, "lon": 9.2837, "label": "Metzingen"},
    {"lat": 48.7784, "lon": 9.1806, "label": "Stuttgart"},
    {"lat": 49.1427, "lon": 9.2109, "label": "Heilbronn"},
    {**FRANKFURT, "label": "Frankfurt"},
]


def _brief_via(candidate: dict, others: list[dict]) -> str:
    """Mirror of the UI's uniqueness-first brief label, for the live check."""
    places = candidate.get("via_places") or []
    if not places:
        return ""
    elsewhere = {p["name"] for other in others for p in (other.get("via_places") or [])}
    unique = [p for p in places if p["name"] not in elsewhere]
    if not unique:
        return places[0]["name"]
    if places[0]["name"] not in elsewhere:
        return places[0]["name"]
    return f"{unique[0]['name']} und {places[0]['name']}"


async def _verify_in_browser(session_url: str, page_html: str) -> None:
    """Playwright leg: session URL mounts the full app with the alternates
    UI; the exported page renders and promotes from file:// with the network
    blocked (tier b). Browser-open itself is NOT asserted here — headless."""
    from playwright.async_api import async_playwright

    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)

        # 1) The session opens OUR app with the route panel populated.
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto(session_url)
        try:
            await page.wait_for_selector("#route-result.open", timeout=20000)
            await page.wait_for_selector(".alt-row .ar-place", timeout=10000)
            await page.wait_for_timeout(3500)      # tiles/labels settle
            place_labels = await page.eval_on_selector_all(
                ".alt-row .ar-place", "nodes => nodes.map(n => n.textContent)")
            road_lines = await page.eval_on_selector_all(
                ".alt-row .ar-road", "nodes => nodes.map(n => n.textContent)")
            summary = await page.inner_text("#route-result")
            ok = (len(place_labels) >= 2
                  and all(label.startswith("über ") for label in place_labels)
                  and any(line.startswith("über ") for line in road_lines)
                  and "min" in summary)
            await page.screenshot(path=str(SCRATCH / "verify-session-alternates.png"))
            report("session in browser", ok,
                   f"rows={place_labels}, roads={road_lines[:2]}, "
                   f"shot={SCRATCH / 'verify-session-alternates.png'}")
        except Exception as error:  # noqa: BLE001 - report, do not crash the run
            report("session in browser", False, f"{type(error).__name__}: {error}")
        await page.close()

        # 2) The exported page, from file://, network OFF (tier b): renders,
        #    and clicking the alternative swaps colours + the steps list.
        page_path = SCRATCH / "verify-map-page.html"
        page_path.write_text(page_html)
        context = await browser.new_context(offline=True,
                                            viewport={"width": 1200, "height": 1100})
        offline_page = await context.new_page()
        try:
            await offline_page.goto(page_path.as_uri())
            await offline_page.wait_for_selector(".mp-line.active", timeout=5000)
            attribution = await offline_page.inner_text(".mp-attribution")
            steps_before = await offline_page.eval_on_selector_all(
                ".mp-steps.active h3", "nodes => nodes.map(n => n.textContent)")
            await offline_page.click('.mp-row[data-cand="1"]')
            steps_after = await offline_page.eval_on_selector_all(
                ".mp-steps.active h3", "nodes => nodes.map(n => n.textContent)")
            promoted = await offline_page.eval_on_selector_all(
                '.mp-line.active', "nodes => nodes.map(n => n.getAttribute('data-cand'))")
            broken = await offline_page.evaluate(
                "Array.from(document.images).filter(i => !i.complete || !i.naturalWidth).length")
            ok = ("Route" in " ".join(steps_before)
                  and "Alternative 1" in " ".join(steps_after)
                  and promoted == ["1"]
                  and "OpenStreetMap" in attribution
                  and broken == 0)
            await offline_page.screenshot(path=str(SCRATCH / "verify-map-page.png"),
                                          full_page=True)
            report("page offline promote", ok,
                   f"steps {steps_before} -> {steps_after}, active line={promoted}, "
                   f"broken imgs={broken}, shot={SCRATCH / 'verify-map-page.png'}")
        except Exception as error:  # noqa: BLE001
            report("page offline promote", False, f"{type(error).__name__}: {error}")
        await context.close()
        await browser.close()


async def _verify_live_session_update(client) -> None:
    """The Cowork smooth-update flow, live: ONE stable session per trip; the
    OPEN tab picks update_trip up via revision polling — 3 stops and the
    'Route aktualisiert' toast within ~6 s, no reload, camera untouched."""
    from playwright.async_api import async_playwright

    created = payload(await client.call_tool("create_trip", {
        "stops": [{**DETTINGEN, "label": "Dettingen an der Erms"},
                  {**FRANKFURT, "label": "Frankfurt"}]}))
    view1 = payload(await client.call_tool("open_map_view", {
        "trip_id": created["id"], "open_browser": False}))
    view2 = payload(await client.call_tool("open_map_view", {
        "trip_id": created["id"], "open_browser": False}))
    report("trip session stable", view1["token"] == view2["token"]
           and view1["url"] == view2["url"],
           f"one token per trip, url={view1['url']}")

    launch_kwargs = {}
    chromium_path = os.environ.get("AGENTIC_MAPS_RENDER_CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            await page.goto(view1["url"])
            await page.wait_for_selector("#route-result.open", timeout=20000)
            await page.wait_for_timeout(1500)          # initial fit settles
            await page.evaluate("() => { window.__no_reload = true; }")
            rows_before = await page.eval_on_selector_all(
                "#stops-list .route-row", "nodes => nodes.length")
            center_before = await page.evaluate(
                "() => { const c = window.agenticMaps[0].map.getCenter();"
                " return [c.lng, c.lat]; }")

            await client.call_tool("update_trip", {
                "trip_id": created["id"],
                "operations": [{"op": "add_stop",
                                "stop": {**STUTTGART_HBF, "label": "Stuttgart"},
                                "position": 1}]})
            # WITHOUT reloading: the revision poll (2.5 s cadence) must land
            # the third stop within ~6 s of the edit.
            await page.wait_for_function(
                "() => document.querySelectorAll('#stops-list .route-row')"
                ".length === 3", timeout=6500)
            await page.wait_for_function(
                "() => document.getElementById('status').classList"
                ".contains('open') && document.getElementById('status')"
                ".textContent.indexOf('Route aktualisiert') !== -1",
                timeout=3000)
            no_reload = await page.evaluate("() => window.__no_reload === true")
            center_after = await page.evaluate(
                "() => { const c = window.agenticMaps[0].map.getCenter();"
                " return [c.lng, c.lat]; }")
            camera_kept = (abs(center_after[0] - center_before[0]) < 1e-6
                           and abs(center_after[1] - center_before[1]) < 1e-6)
            await page.screenshot(path=str(SCRATCH / "verify-live-update.png"))
            report("live tab update", rows_before == 2 and no_reload and camera_kept,
                   f"stops 2 -> 3 in place, toast fired, no reload={no_reload}, "
                   f"camera kept={camera_kept}, "
                   f"shot={SCRATCH / 'verify-live-update.png'}")
        except Exception as error:  # noqa: BLE001 - report, do not crash the run
            report("live tab update", False, f"{type(error).__name__}: {error}")
        await browser.close()


async def verify(url: str) -> int:
    from mcp.client.client import Client

    async with Client(url) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        expected = {
            "geocode", "reverse_geocode", "search_places", "route",
            "route_matrix", "isochrone", "routing_capabilities",
            "optimize_trip", "reachability",
            "extract_features", "street_survey", "render_map",
            "open_map_view", "get_map_page_html",
            "create_trip", "update_trip", "get_trip", "list_trips",
            "list_tile_sources", "map_attribution", "get_runtime_mode",
            "set_runtime_mode", "plan_offline_bundle",
            "harvest_offline_bundle", "package_offline_bundle",
            "list_offline_bundles",
            "provision_offline_region", "provisioning_status",
            "cancel_provisioning",
        }
        missing = expected - set(tools)
        report("list_tools", not missing,
               f"{len(tools)} tools" + (f", MISSING {missing}" if missing else ""))

        # -- geocode: postcode must ride along --------------------------------
        result = await client.call_tool(
            "geocode", {"query": "Dettingen an der Erms", "limit": 5})
        hits = payload(result).get("results", [])
        postcodes = {h["address"]["postcode"] for h in hits if h.get("address")}
        report("geocode", "72581" in postcodes,
               f"{len(hits)} hits, postcodes={sorted(postcodes)}")

        # -- reverse geocode ------------------------------------------------
        result = await client.call_tool("reverse_geocode", DETTINGEN)
        hit = payload(result)["result"]
        report("reverse_geocode", bool(hit and hit.get("address")),
               (hit or {}).get("name", "no hit")[:80])

        # -- search_places -----------------------------------------------------
        # "Stutt": the offline index carries major populated places (Stuttgart,
        # not every 20k-inhabitant town) — small places arrive via the geocoder
        # half of the merge instead.
        result = await client.call_tool(
            "search_places", {"query": "Stutt", "near": DETTINGEN})
        body = payload(result)
        names = [c.get("name", "") for c in body.get("city_index", [])]
        report("search_places",
               any("Stuttgart" in n for n in names) and body.get("geocoder"),
               f"city_index={names[:3]}, geocoder={len(body.get('geocoder', []))} hits")

        # -- route with alternates (2 stops) ---------------------------------
        result = await client.call_tool("route", {
            "stops": [{**DETTINGEN, "label": "Dettingen"},
                      {**FRANKFURT, "label": "Frankfurt"}],
            "alternates": 2, "steps": True})
        route = payload(result)
        candidates = 1 + len(route.get("alternates", []))
        refs = {s["ref"] for s in route.get("steps", []) if s.get("ref")}
        lanes = sum(len(s.get("lanes", [])) for s in route.get("steps", []))
        report("route (alternates)",
               candidates >= 2 and refs and lanes > 0,
               f"{candidates} candidates, {route['duration_min']} min / "
               f"{route['distance_km']} km, refs={sorted(refs)[:4]}, "
               f"{lanes} lane entries")

        # -- route with 5 stops -----------------------------------------------
        result = await client.call_tool("route", {"stops": FIVE_STOPS})
        multi = payload(result)
        report("route (5 stops)", len(multi.get("legs", [])) == 4,
               f"{len(multi.get('legs', []))} legs, total "
               f"{multi['duration_min']} min / {multi['distance_km']} km")

        # -- matrix 5x5 -------------------------------------------------------
        points = [{"lat": s["lat"], "lon": s["lon"]} for s in FIVE_STOPS]
        result = await client.call_tool("route_matrix", {"points": points})
        matrix = payload(result)["durations_min"]
        square = len(matrix) == 5 and all(len(row) == 5 for row in matrix)
        report("route_matrix", square and matrix[0][4] > 0,
               f"5x5, Dettingen->Frankfurt {matrix[0][4]:.0f} min")

        # -- extract_features: buildings with real heights --------------------
        result = await client.call_tool("extract_features", {
            "bbox": {"west": STUTTGART_HBF["lon"] - 0.004,
                     "south": STUTTGART_HBF["lat"] - 0.003,
                     "east": STUTTGART_HBF["lon"] + 0.004,
                     "north": STUTTGART_HBF["lat"] + 0.003},
            "layers": "buildings,pois"})
        collection = payload(result)
        heights = [f["properties"]["height"] for f in collection["features"]
                   if isinstance(f["properties"].get("height"), (int, float))]
        report("extract_features",
               bool(heights) and collection["meta"]["layer_counts"].get("buildings", 0) > 0,
               f"counts={collection['meta']['layer_counts']}, "
               f"{len(heights)} heights, sample={heights[:3]}")

        # -- street_survey ---------------------------------------------------
        result = await client.call_tool("street_survey", {
            "bbox": {"west": 9.290, "south": 48.535, "east": 9.296, "north": 48.540}})
        survey = payload(result)
        report("street_survey", bool(survey.get("names")),
               f"{len(survey.get('ways', []))} ways, names={survey.get('names', [])[:4]}")

        # -- sources & attribution --------------------------------------------
        result = await client.call_tool("list_tile_sources", {})
        listed = payload(result)
        unlicensed = [s["id"] for s in listed["sources"] if not s.get("license_name")]
        report("list_tile_sources", not unlicensed,
               f"{len(listed['sources'])} sources, {len(listed['composites'])} "
               f"composites" + (f", UNLICENSED {unlicensed}" if unlicensed else
                                ", every source carries license_name"))

        result = await client.call_tool("map_attribution", {
            "bbox": {"west": 9.17, "south": 48.77, "east": 9.19, "north": 48.79},
            "zoom": 15})
        attribution = payload(result)
        report("map_attribution", bool(attribution.get("text")),
               f"text={attribution.get('text', '')[:80]!r}")

        # -- capabilities & the documented isochrone limitation ----------------
        result = await client.call_tool("routing_capabilities", {})
        caps = payload(result)
        report("routing_capabilities", caps.get("isochrone") is False,
               f"backend={caps.get('backend')}, alternates={caps.get('alternates')}, "
               f"isochrone={caps.get('isochrone')}, avoid={caps.get('avoid')}")

        result = await client.call_tool("isochrone", {
            "center": DETTINGEN, "contours": [{"time_min": 10}]})
        text = error_text(result) if result.is_error else "unexpected success"
        report("isochrone (expected limitation)",
               result.is_error and "Valhalla" in text, text[:100])

        # -- runtime mode (read-only against the live server) ------------------
        result = await client.call_tool("get_runtime_mode", {})
        mode = payload(result)["mode"]
        report("get_runtime_mode", mode in ("online", "mixed", "offline"),
               f"mode={mode}")

        # -- via_places on the Dettingen->Frankfurt candidate pair -------------
        result = await client.call_tool("route", {
            "stops": [{**DETTINGEN, "label": "Dettingen an der Erms"},
                      {**FRANKFURT, "label": "Frankfurt"}],
            "alternates": 2, "steps": True})
        pair = payload(result)
        candidates = [pair] + pair.get("alternates", [])
        labels = [_brief_via(c, [o for o in candidates if o is not c])
                  for c in candidates]
        complete = all(c.get("geometry") and c.get("steps") for c in candidates)
        # Pforzheim (A8 corridor) and Heilbronn (A81 corridor) exist ONLY in
        # the dense GeoNames index — their presence proves the densified
        # place index is live, not the 58-German-city Natural Earth fallback.
        via_names = {p["name"] for c in candidates for p in c.get("via_places", [])}
        report("route (via_places)",
               all(c.get("via_places") for c in candidates) and complete
               and len(set(labels)) == len(labels)
               and {"Pforzheim", "Heilbronn"} <= via_names,
               f"labels={labels}, primary places="
               f"{[p['name'] for p in pair['via_places'][:4]]}, "
               f"dense-index cities present={sorted({'Pforzheim', 'Heilbronn'} & via_names)}")

        # -- live trip: create, then insert Stuttgart mid-route ----------------
        created = payload(await client.call_tool("create_trip", {
            "stops": [{**DETTINGEN, "label": "Dettingen an der Erms"},
                      {**FRANKFURT, "label": "Frankfurt"}],
            "alternates": 2}))
        zips = [((s.get("address") or {}).get("postcode", "")) for s in created["stops"]]
        report("create_trip", created["revision"] == 1 and "72581" in zips,
               f"trip={created['id']}, stop ZIPs={zips}, "
               f"{created['route']['duration_min']} min")

        updated = payload(await client.call_tool("update_trip", {
            "trip_id": created["id"],
            "operations": [{"op": "add_stop",
                            "stop": {**STUTTGART_HBF, "label": "Stuttgart"},
                            "position": 1}]}))
        near_stuttgart = any(
            abs(p["lat"] - STUTTGART_HBF["lat"]) < 0.03
            and abs(p["lon"] - STUTTGART_HBF["lon"]) < 0.04
            for p in updated["route"]["geometry"])
        report("update_trip (insert)",
               updated["revision"] == 2
               and [s["label"] for s in updated["stops"]] == [
                   "Dettingen an der Erms", "Stuttgart", "Frankfurt"]
               and len(updated["route"]["legs"]) == 2 and near_stuttgart,
               f"3 stops, {len(updated['route']['legs'])} legs, "
               f"passes Stuttgart={near_stuttgart}, "
               f"{updated['route']['duration_min']} min")

        # -- optimize_trip: 8 shuffled cities against the OSRM demo ------------
        # Deliberately scrambled geography (south, north, south, ...): the
        # input order zig-zags across Germany, so a working solver MUST both
        # reorder and shorten it.
        shuffled = [
            {"lat": 48.7784, "lon": 9.1806, "label": "Stuttgart"},
            {"lat": 53.5511, "lon": 9.9937, "label": "Hamburg"},
            {"lat": 48.1374, "lon": 11.5755, "label": "München"},
            {"lat": 50.9375, "lon": 6.9603, "label": "Köln"},
            {"lat": 49.4521, "lon": 11.0767, "label": "Nürnberg"},
            {"lat": 51.3397, "lon": 12.3731, "label": "Leipzig"},
            {"lat": 49.0069, "lon": 8.4037, "label": "Karlsruhe"},
            {"lat": 52.5163, "lon": 13.3777, "label": "Berlin"},
        ]
        baseline = payload(await client.call_tool("route", {"stops": shuffled}))
        optimized = payload(await client.call_tool("optimize_trip", {"stops": shuffled}))
        identity = list(range(len(shuffled)))
        saved_km = baseline["distance_km"] - optimized["route"]["distance_km"]
        report("optimize_trip (live TSP)",
               optimized["order"] != identity
               and optimized["route"]["distance_km"] <= baseline["distance_km"],
               f"order={optimized['order']}, input {baseline['distance_km']} km -> "
               f"optimized {optimized['route']['distance_km']} km "
               f"(saved {saved_km:.0f} km), visited="
               f"{[s['label'] for s in optimized['stop_details']]}")

        # -- reachability: one origin, ~80-target grid, one table request ------
        reach = payload(await client.call_tool("reachability", {
            "origin": STUTTGART_HBF,
            "grid": {"bbox": {"west": 8.80, "south": 48.50,
                              "east": 9.60, "north": 49.00},
                     "step_km": 6.0, "cap": 99}}))
        minutes = [t["minutes"] for t in reach["targets"]]
        reachable = [m for m in minutes if m > 0]
        report("reachability (live grid)",
               60 <= reach["meta"]["count"] <= 99
               and reach["meta"]["requests"] == 1
               and len(reachable) > reach["meta"]["count"] * 0.8,
               f"{reach['meta']['count']} targets in {reach['meta']['requests']} "
               f"request, minutes min/median/max = {min(reachable):.0f}/"
               f"{sorted(reachable)[len(reachable) // 2]:.0f}/{max(reachable):.0f}")

        # -- provisioning: the two-call contract, then a TINY real job ---------
        estimate_only = payload(await client.call_tool("provision_offline_region", {
            "region": "de", "layers": ["aerial"], "aerial_max_zoom": 15}))
        jobs_before = payload(await client.call_tool("provisioning_status", {}))
        report("provision (estimate, no start)",
               estimate_only.get("started") is False
               and 13e9 < estimate_only["estimate"]["total_bytes"] < 17e9
               and all(job["state"] != "running" or job.get("created_at", 0) > 0
                       for job in jobs_before.get("jobs", [])),
               f"~{estimate_only['estimate']['total_bytes'] / 1e9:.1f} GB estimated, "
               f"started={estimate_only.get('started')}")

        # A few dozen z13 tiles around Metzingen — real WMS fetches through
        # the real band dispatcher, small enough to be polite.
        tiny = payload(await client.call_tool("provision_offline_region", {
            "bbox": {"west": 9.15, "south": 48.47, "east": 9.40, "north": 48.60},
            "region_id": "verify-metzingen", "layers": ["aerial"],
            "aerial_max_zoom": 13, "confirm_size": True}))
        job_id = tiny["job"]["id"]
        status = tiny["job"]
        for _ in range(120):
            status = payload(await client.call_tool(
                "provisioning_status", {"job_id": job_id}))
            if status["state"] not in ("pending", "running"):
                break
            await asyncio.sleep(2.0)
        aerial = status["layers"][0]
        report("provision (tiny real job)",
               status["state"] == "done"
               and aerial["tiles_done"] == aerial["tiles_total"] > 10
               and aerial["bytes_done"] > 50_000
               and aerial["tiles_failed"] <= aerial["tiles_total"] * 0.2,
               f"{status['state']}: {aerial['tiles_done']}/{aerial['tiles_total']} "
               f"tiles, {aerial['tiles_failed']} failed, "
               f"{aerial['bytes_done']:,} bytes")

        # -- browser session for the 2-stop pair (alternates UI) ---------------
        route_for_view = dict(pair)
        route_for_view.pop("stop_details", None)
        view = payload(await client.call_tool("open_map_view", {
            "routes": [route_for_view], "open_browser": False}))
        report("open_map_view", view["url"].startswith("http") and not view["opened"],
               f"url={view['url']}")

        # -- self-contained page: 2-stop (clickable alternates) + 3-stop trip --
        page2 = payload(await client.call_tool("get_map_page_html", {
            "routes": [route_for_view], "basemap": "embedded"}))
        header_zips = re.findall(r"\((\d{5})\)", page2["html"])
        report("get_map_page_html",
               "mp-attribution" in page2["html"]
               and "http://" not in page2["html"]
               and "https://" not in page2["html"]
               and len(header_zips) >= 2,
               f"{page2['bytes']} bytes, header ZIPs={header_zips[:4]}")

        page3 = payload(await client.call_tool("get_map_page_html", {
            "trip_id": created["id"], "basemap": "embedded"}))
        stops_shown = page3["html"].count("stop-")
        report("trip page (3 stops)",
               all(label in page3["html"] for label in ("Dettingen", "Stuttgart", "Frankfurt")),
               f"labels present, {page3['bytes']} bytes, {stops_shown} stop nodes")

        # -- the Cowork smooth-update flow: open tab, edit trip, no new tab --
        await _verify_live_session_update(client)

    # -- Playwright: the session URL runs the FULL app; the page works offline -
    await _verify_in_browser(view["url"], page2["html"])

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="live MCP surface verification")
    parser.add_argument("--url", default="http://127.0.0.1:8195/mcp")
    args = parser.parse_args()
    sys.exit(asyncio.run(verify(args.url)))


if __name__ == "__main__":
    main()
