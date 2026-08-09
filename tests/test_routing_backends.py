import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.models.lat_lon import LatLon
from agentic_maps.rest.maps_api import MapsApi
from agentic_maps.routing.osrm import OsrmRouter
from agentic_maps.routing.valhalla import ValhallaRouter, decode_polyline6

# Valhalla's own worked example from
# https://valhalla.github.io/valhalla/api/decoding/ — ground truth for the
# precision-1e6 decoder, not a value picked to make a test pass.
_EXAMPLE_SHAPE = "e~epoA|jfpOiDaK"
_EXAMPLE_POINTS = [
    LatLon(lat=42.225139, lon=-8.670911),
    LatLon(lat=42.225224, lon=-8.670718),
]

START = LatLon(lat=48.7784, lon=9.1806)
END = LatLon(lat=48.1374, lon=11.5755)


def test_decode_polyline6_matches_valhalla_example():
    assert decode_polyline6(_EXAMPLE_SHAPE) == _EXAMPLE_POINTS


def _trip(shape=_EXAMPLE_SHAPE, time=8040.0, length=233.0, maneuvers=None):
    return {
        "summary": {"time": time, "length": length},
        "legs": [{
            "shape": shape,
            "summary": {"time": time, "length": length},
            "maneuvers": maneuvers or [],
        }],
    }


def _mock_valhalla(handler) -> ValhallaRouter:
    return ValhallaRouter("https://valhalla.test", transport=httpx.MockTransport(handler))


# -- route: costing mapping for all four canonical modes --------------------

@pytest.mark.parametrize("mode,costing", [
    ("car", "auto"), ("truck", "truck"), ("walk", "pedestrian"), ("bike", "bicycle"),
])
async def test_route_maps_all_four_modes_to_costing(mode, costing):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://valhalla.test/route"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"trip": _trip()})

    router = _mock_valhalla(handler)
    route = await router.route(START, END, route_id="r1", mode=mode)

    assert seen["body"]["costing"] == costing
    assert route.mode == mode
    assert route.duration_min == pytest.approx(134.0)
    assert route.distance_km == pytest.approx(233.0)
    assert route.geometry == _EXAMPLE_POINTS
    assert route.legs[0].duration_min == pytest.approx(134.0)


async def test_route_unknown_mode_falls_back_to_car():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["costing"] == "auto"
        return httpx.Response(200, json={"trip": _trip()})

    router = _mock_valhalla(handler)
    route = await router.route(START, END, route_id="r1", mode="spaceship")
    assert route.mode == "car"


# -- route: multi-leg geometry does not duplicate the shared stop -----------

async def test_route_multi_leg_geometry_drops_duplicate_boundary_point():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert len(body["locations"]) == 3  # start, via, end
        trip = _trip()
        trip["legs"] = [
            {"shape": _EXAMPLE_SHAPE, "summary": {"time": 100.0, "length": 1.0}, "maneuvers": []},
            {"shape": _EXAMPLE_SHAPE, "summary": {"time": 200.0, "length": 2.0}, "maneuvers": []},
        ]
        trip["summary"] = {"time": 300.0, "length": 3.0}
        return httpx.Response(200, json={"trip": trip})

    router = _mock_valhalla(handler)
    via = LatLon(lat=48.5, lon=9.3)
    route = await router.route(START, END, route_id="r1", via=[via])

    # Each leg decodes to 2 points; the second leg's first point (== the
    # first leg's last point) must be dropped, not duplicated.
    assert len(route.geometry) == 3
    assert len(route.legs) == 2


# -- route: alternates ------------------------------------------------------

async def test_route_alternates_parsed_onto_map_route():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["alternates"] == 2
        return httpx.Response(200, json={
            "trip": _trip(time=8040.0, length=233.0),
            "alternates": [
                {"trip": _trip(time=9000.0, length=250.0)},
                {"trip": _trip(time=9600.0, length=260.0)},
            ],
        })

    router = _mock_valhalla(handler)
    route = await router.route(START, END, route_id="r1", alternates=2)

    assert route.duration_min == pytest.approx(134.0)
    assert len(route.alternates) == 2
    assert route.alternates[0].duration_min == pytest.approx(150.0)
    assert route.alternates[1].duration_min == pytest.approx(160.0)
    # Alternates do not themselves carry nested alternates.
    assert route.alternates[0].alternates == []
    # IDs stay distinct so drawing all of them never collides on a source id.
    assert route.alternates[0].id != route.alternates[1].id != route.id


async def test_route_does_not_request_alternates_for_multi_stop():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "alternates" not in body
        return httpx.Response(200, json={"trip": _trip()})

    router = _mock_valhalla(handler)
    via = LatLon(lat=48.5, lon=9.3)
    await router.route(START, END, route_id="r1", via=[via], alternates=2)


# -- route: maneuvers -> RouteStep -------------------------------------------

async def test_route_steps_mapped_from_maneuvers():
    maneuvers = [
        {"type": 1, "begin_shape_index": 0, "length": 0.109, "time": 12.0,
         "street_names": ["Appleton"]},
        {"type": 10, "begin_shape_index": 1, "length": 0.05, "time": 5.0,
         "street_names": ["Main St"]},
        {"type": 26, "begin_shape_index": 1, "length": 0.02, "time": 3.0,
         "roundabout_exit_count": 2, "street_names": []},
        {"type": 4, "begin_shape_index": 1, "length": 0.0, "time": 0.0,
         "street_names": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"trip": _trip(maneuvers=maneuvers)})

    router = _mock_valhalla(handler)
    route = await router.route(START, END, route_id="r1", steps=True)

    assert [s.type for s in route.steps] == ["depart", "turn", "roundabout", "arrive"]
    assert route.steps[1].modifier == "right"
    assert route.steps[1].name == "Main St"
    assert route.steps[1].distance_m == pytest.approx(50.0)
    assert route.steps[2].exit == 2
    assert route.steps[0].location == _EXAMPLE_POINTS[0]


async def test_route_steps_omitted_by_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"trip": _trip(maneuvers=[
            {"type": 1, "begin_shape_index": 0, "length": 0.1, "time": 1.0, "street_names": []},
        ])})

    router = _mock_valhalla(handler)
    route = await router.route(START, END, route_id="r1")
    assert route.steps == []


# -- matrix -------------------------------------------------------------

async def test_matrix_parses_concise_durations():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://valhalla.test/sources_to_targets"
        body = json.loads(request.content)
        assert body["verbose"] is False
        assert body["costing"] == "pedestrian"
        return httpx.Response(200, json={
            "sources_to_targets": {
                "durations": [[0, 1500], [1440, 0]],
                "distances": [[0.0, 25.0], [24.0, 0.0]],
            },
            "units": "kilometers",
            "algorithm": "costmatrix",
        })

    router = _mock_valhalla(handler)
    durations = await router.matrix([START, END], mode="walk")
    assert durations == [[0, 25.0], [24.0, 0]]


async def test_matrix_raises_on_malformed_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "boom"})

    router = _mock_valhalla(handler)
    with pytest.raises(ValueError):
        await router.matrix([START, END])


# -- isochrone ------------------------------------------------------------

async def test_isochrone_parses_time_and_distance_contours():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://valhalla.test/isochrone"
        body = json.loads(request.content)
        assert body["contours"] == [{"time": 15.0}, {"distance": 5.0}]
        assert body["polygons"] is True
        return httpx.Response(200, json={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"contour": 15.0, "metric": "time", "color": "ff0000"},
                    "geometry": {"type": "Polygon", "coordinates": [
                        [[9.0, 48.0], [9.1, 48.0], [9.1, 48.1], [9.0, 48.0]]
                    ]},
                },
                {
                    "type": "Feature",
                    "properties": {"contour": 5.0, "metric": "distance", "color": "00ff00"},
                    "geometry": {"type": "MultiPolygon", "coordinates": [
                        [[[9.0, 48.0], [9.05, 48.0], [9.05, 48.05], [9.0, 48.0]]],
                        [[[9.2, 48.2], [9.25, 48.2], [9.25, 48.25], [9.2, 48.2]]],
                    ]},
                },
            ],
        })

    router = _mock_valhalla(handler)
    result = await router.isochrone(
        START, mode="car",
        contours=[{"time_min": 15.0}, {"distance_km": 5.0}],
    )

    assert result.center == START
    # One ring for the Polygon feature, two for the MultiPolygon's two parts.
    assert len(result.rings) == 3
    by_time = [r for r in result.rings if r.minutes is not None]
    by_distance = [r for r in result.rings if r.km is not None]
    assert len(by_time) == 1 and by_time[0].minutes == pytest.approx(15.0)
    assert by_time[0].color == "#ff0000"
    assert len(by_distance) == 2
    assert all(r.km == pytest.approx(5.0) for r in by_distance)
    # Coordinates come back as [lon, lat] on the wire; LatLon flips them.
    assert by_time[0].polygon[0] == LatLon(lat=48.0, lon=9.0)


# -- supported_avoid: the code-208 honesty probe -----------------------------

async def test_supported_avoid_all_honoured_when_no_warning():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"trip": _trip()})

    router = _mock_valhalla(handler)
    assert sorted(await router.supported_avoid()) == ["ferry", "motorway", "toll"]


async def test_supported_avoid_none_when_server_rejects_hard_exclusions():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "trip": _trip(),
            "warnings": [{"code": 208, "text": "Hard exclusions are not allowed on this server, "
                                                "ignoring hard excludes"}],
        })

    router = _mock_valhalla(handler)
    assert await router.supported_avoid() == []


async def test_supported_avoid_cached_after_first_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"trip": _trip()})

    router = _mock_valhalla(handler)
    await router.supported_avoid()
    await router.supported_avoid()
    assert len(calls) == 1


# -- OSRM: still conforms after the canonical-mode retrofit -----------------

async def test_osrm_still_has_no_truck_profile_but_accepts_the_mode():
    """OSRM has no truck costing; the client maps it onto `driving` (with a
    code comment explaining the limitation) rather than erroring, since a
    car-shaped truck route is still better than none from a legacy backend."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/route/v1/driving/" in str(request.url)
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{"duration": 600.0, "distance": 10000.0,
                        "geometry": {"coordinates": [[9.18, 48.77], [9.20, 48.78]]}}],
        })

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    route = await router.route(START, END, route_id="r1", mode="truck")
    assert route.mode == "truck"


async def test_osrm_isochrone_not_implemented():
    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})
    ))
    with pytest.raises(NotImplementedError):
        await router.isochrone(START, contours=[{"time_min": 15.0}])


def test_osrm_reports_alternates_but_no_isochrone_support():
    # Alternates via `alternatives=` work on any OSRM (verified live against
    # the public demo); isochrones remain Valhalla-only.
    assert OsrmRouter.supports_alternates is True
    assert OsrmRouter.supports_isochrone is False


async def test_osrm_requests_and_parses_alternates_for_two_point_trips():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["alternatives"] == "2"
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [
                {"duration": 9300.0, "distance": 237000.0,
                 "geometry": {"coordinates": [[9.35, 48.53], [8.68, 50.11]]}},
                {"duration": 9660.0, "distance": 250000.0,
                 "geometry": {"coordinates": [[9.35, 48.53], [9.22, 49.14], [8.68, 50.11]]}},
            ],
        })

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    route = await router.route(START, END, route_id="r1", mode="car", alternates=2)
    assert route.duration_min == 155.0
    assert len(route.alternates) == 1
    assert route.alternates[0].distance_km == 250.0
    assert route.alternates[0].id == "r1-alt0"
    assert route.alternates[0].alternates == []


async def test_osrm_maps_lane_guidance_from_the_maneuver_intersection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{
                "duration": 600.0, "distance": 10000.0,
                "geometry": {"coordinates": [[9.18, 48.77], [9.20, 48.78]]},
                "legs": [{"duration": 600.0, "distance": 10000.0, "steps": [{
                    "name": "Karlstraße", "distance": 400.0, "duration": 60.0,
                    "maneuver": {"type": "turn", "modifier": "right", "location": [9.19, 48.775]},
                    "intersections": [{
                        "location": [9.19, 48.775],
                        "lanes": [
                            {"indications": ["left"], "valid": False},
                            {"indications": ["straight", "right"], "valid": True},
                        ],
                    }],
                }]}],
            }],
        })

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    route = await router.route(START, END, route_id="r1", mode="car", steps=True)
    lanes = route.steps[0].lanes
    assert [lane.valid for lane in lanes] == [False, True]
    assert lanes[1].indications == ["straight", "right"]


async def test_osrm_never_asks_for_alternates_on_multi_stop_trips():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "alternatives" not in request.url.params
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{"duration": 600.0, "distance": 10000.0,
                        "geometry": {"coordinates": [[9.18, 48.77], [9.20, 48.78]]}}],
        })

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    route = await router.route(START, END, route_id="r1", mode="car",
                               via=[LatLon(lat=48.9, lon=9.2)], alternates=2)
    assert route.alternates == []


def test_valhalla_reports_alternates_and_isochrone_support():
    assert ValhallaRouter.supports_alternates is True
    assert ValhallaRouter.supports_isochrone is True


# -- REST wiring --------------------------------------------------------

def _client(tmp_path, wms_source, router) -> TestClient:
    api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={}, router=router)
    app = FastAPI()
    api.mount(app)
    return TestClient(app)


def test_route_endpoint_returns_alternates(tmp_path, wms_source):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "trip": _trip(),
            "alternates": [{"trip": _trip(time=9000.0, length=250.0)}],
        })

    router = _mock_valhalla(handler)
    client = _client(tmp_path, wms_source, router)
    response = client.post("/api/v1/maps/route", json={
        "route_id": "r1", "start": {"lat": 48.7784, "lon": 9.1806},
        "end": {"lat": 48.1374, "lon": 11.5755}, "alternates": 1,
    })
    assert response.status_code == 200
    body = response.json()
    assert len(body["alternates"]) == 1
    assert body["alternates"][0]["duration_min"] == pytest.approx(150.0)


def test_isochrone_endpoint_returns_valid_geojson(tmp_path, wms_source):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"contour": 10.0, "metric": "time", "color": "3388ff"},
                "geometry": {"type": "Polygon", "coordinates": [
                    [[9.0, 48.0], [9.1, 48.0], [9.1, 48.1], [9.0, 48.0]]
                ]},
            }],
        })

    router = _mock_valhalla(handler)
    client = _client(tmp_path, wms_source, router)
    response = client.post("/api/v1/maps/isochrone", json={
        "center": {"lat": 48.0, "lon": 9.0}, "contours": [{"time_min": 10.0}],
    })
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/geo+json"
    geojson = response.json()
    assert geojson["type"] == "FeatureCollection"
    feature = geojson["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["metric"] == "time"
    assert feature["properties"]["contour"] == pytest.approx(10.0)
    # A ring MapLibre can actually render: closed (first == last vertex),
    # [lon, lat] order, at least 4 points.
    ring = feature["geometry"]["coordinates"][0]
    assert len(ring) >= 4
    assert ring[0] == ring[-1]


def test_isochrone_endpoint_501_on_osrm_backend(tmp_path, wms_source):
    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})
    ))
    client = _client(tmp_path, wms_source, router)
    response = client.post("/api/v1/maps/isochrone", json={
        "center": {"lat": 48.0, "lon": 9.0}, "contours": [{"time_min": 10.0}],
    })
    assert response.status_code == 501


def test_isochrone_endpoint_blocked_offline(tmp_path, wms_source):
    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={})
    ))
    api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                  router=router, mode="offline")
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)
    response = client.post("/api/v1/maps/isochrone", json={
        "center": {"lat": 48.0, "lon": 9.0}, "contours": [{"time_min": 10.0}],
    })
    assert response.status_code == 403


def test_routing_capabilities_reflects_backend(tmp_path, wms_source):
    valhalla_router = _mock_valhalla(lambda r: httpx.Response(200, json={"trip": _trip()}))
    response = _client(tmp_path, wms_source, valhalla_router).get(
        "/api/v1/maps/routing/capabilities"
    )
    body = response.json()
    assert body["alternates"] is True
    assert body["isochrone"] is True

    osrm_router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": "Ok"})
    ))
    response = _client(tmp_path, wms_source, osrm_router).get(
        "/api/v1/maps/routing/capabilities"
    )
    body = response.json()
    # OSRM answers alternates too now (`alternatives=` on 2-point trips).
    assert body["alternates"] is True
    assert body["isochrone"] is False


def test_default_router_is_valhalla_unless_env_says_osrm(monkeypatch):
    from agentic_maps.rest.maps_api import _default_router
    from agentic_maps.routing.osrm import OsrmRouter
    from agentic_maps.routing.valhalla import ValhallaRouter

    monkeypatch.delenv("AGENTIC_MAPS_ROUTING_BACKEND", raising=False)
    assert isinstance(_default_router(), ValhallaRouter)

    monkeypatch.setenv("AGENTIC_MAPS_ROUTING_BACKEND", "osrm")
    assert isinstance(_default_router(), OsrmRouter)


def test_routing_capabilities_makes_no_backend_probe_in_offline_mode(tmp_path, wms_source):
    """"offline" blocks EVERY upstream connection — including the avoid
    probe `supported_avoid()` fires at the routing backend. Regression:
    the capabilities endpoint used to probe regardless of mode, and an
    unreachable backend answered while offline was then cached as
    "supports nothing" for the rest of the session."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"trip": _trip()})

    router = _mock_valhalla(handler)
    api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                  router=router, mode="offline")
    app = FastAPI()
    api.mount(app)
    client = TestClient(app)

    body = client.get("/api/v1/maps/routing/capabilities").json()
    assert body["avoid"] == []
    assert calls == []                     # not one byte left the process

    # Back online mid-session, the probe runs fresh — the offline answer was
    # never cached as the backend's real capability.
    client.post("/api/v1/maps/mode", json={"mode": "online"})
    body = client.get("/api/v1/maps/routing/capabilities").json()
    assert sorted(body["avoid"]) == ["ferry", "motorway", "toll"]
    assert len(calls) == 1


# -- optimized_route (TSP): Valhalla /optimized_route ------------------------

VIA = LatLon(lat=48.4011, lon=9.9876)     # Ulm, between Stuttgart and Munich


def _optimized_trip(order: list[int]) -> dict:
    trip = _trip()
    trip["locations"] = [{"original_index": i} for i in order]
    return trip


async def test_valhalla_optimized_route_maps_original_index_to_order():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://valhalla.test/optimized_route"
        seen["body"] = json.loads(request.content)
        # The server visits START, VIA-swap: input [START, VIA, END] comes
        # back visited as locations with original_index [0, 1, 2] — but we
        # feed a swapped middle for a 4-stop case below; here identity.
        return httpx.Response(200, json={"trip": _optimized_trip([0, 2, 1, 3])})

    router = _mock_valhalla(handler)
    stops = [START, VIA, LatLon(lat=48.5, lon=9.5), END]
    result = await router.optimized_route(stops, route_id="opt-1", mode="car")

    assert seen["body"]["costing"] == "auto"
    assert [loc["lat"] for loc in seen["body"]["locations"]] == [s.lat for s in stops]
    # original_index in visited order IS the order mapping.
    assert result.order == [0, 2, 1, 3]
    # The route's stops are the input stops permuted the same way.
    assert result.route.stops == [stops[0], stops[2], stops[1], stops[3]]
    assert result.route.duration_min == pytest.approx(134.0)


async def test_valhalla_optimized_route_refuses_free_endpoints():
    """The service's own docs pin endpoints ("always starting at the first
    location ... ending at the last"), so keep_endpoints=False must refuse,
    never silently reorder less than promised."""
    router = _mock_valhalla(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="OSRM"):
        await router.optimized_route([START, VIA, END], route_id="x",
                                     keep_endpoints=False)


async def test_valhalla_optimized_route_truck_downgrades_to_auto():
    """The optimized-route docs list auto/bicycle/pedestrian only — truck is
    not offered, so it optimizes with car costing rather than gambling."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"trip": _optimized_trip([0, 1, 2])})

    router = _mock_valhalla(handler)
    result = await router.optimized_route([START, VIA, END], route_id="x", mode="truck")
    assert seen["body"]["costing"] == "auto"
    assert result.route.mode == "truck"        # canonical mode survives


async def test_valhalla_optimized_route_roundtrip_appends_and_drops_start():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        # 3 input stops + the appended start = 4 locations back.
        return httpx.Response(200, json={"trip": _optimized_trip([0, 2, 1, 3])})

    router = _mock_valhalla(handler)
    result = await router.optimized_route([START, VIA, END], route_id="x",
                                          roundtrip=True)
    assert len(seen["body"]["locations"]) == 4
    assert seen["body"]["locations"][-1] == {"lat": START.lat, "lon": START.lon}
    # The duplicate start (original_index == 3 == len(stops)) is dropped.
    assert result.order == [0, 2, 1]


# -- optimized_route (TSP): OSRM /trip ---------------------------------------

def _osrm_trip_body(waypoint_indices: list[int]) -> dict:
    return {
        "code": "Ok",
        "trips": [{
            "duration": 5400.0, "distance": 150_000.0,
            "geometry": {"coordinates": [[9.18, 48.77], [9.99, 48.40], [11.58, 48.14]]},
            "legs": [{"duration": 2700.0, "distance": 75_000.0, "steps": []}] * 2,
        }],
        # Input order; waypoint_index = position of that input in the trip.
        "waypoints": [{"waypoint_index": w, "trips_index": 0}
                      for w in waypoint_indices],
    }


async def test_osrm_trip_pins_endpoints_and_inverts_waypoint_index():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        # Input [START, VIA, X, END]; the solver visits X before VIA:
        # input 0 -> position 0, input 1 -> position 2, input 2 -> position 1,
        # input 3 -> position 3.
        return httpx.Response(200, json=_osrm_trip_body([0, 2, 1, 3]))

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    stops = [START, VIA, LatLon(lat=48.5, lon=9.5), END]
    result = await router.optimized_route(stops, route_id="opt-1", mode="car")

    # Doc-verified param semantics: roundtrip=false + source=first +
    # destination=last is the pinned-endpoints TSP shape.
    assert "/trip/v1/driving/" in seen["url"]
    assert "roundtrip=false" in seen["url"]
    assert "source=first" in seen["url"] and "destination=last" in seen["url"]
    # order[k] = input index of the k-th visited stop: positions [0,2,1,3]
    # invert to visiting order [0, 2, 1, 3].
    assert result.order == [0, 2, 1, 3]
    assert result.route.stops == [stops[0], stops[2], stops[1], stops[3]]
    assert result.route.duration_min == pytest.approx(90.0)
    assert len(result.route.legs) == 2


async def test_osrm_trip_roundtrip_keeps_loop_and_start_pin():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_osrm_trip_body([0, 1, 2]))

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    await router.optimized_route([START, VIA, END], route_id="x", roundtrip=True)
    assert "roundtrip=true" in seen["url"]
    assert "source=first" in seen["url"]
    assert "destination=" not in seen["url"]   # a loop has no distinct end


async def test_osrm_trip_two_pinned_stops_short_circuits_to_route():
    """Two pinned stops have nothing to optimize — answered via the route
    service, same shape, no trip call."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/route/v1/" in str(request.url)
        return httpx.Response(200, json={
            "code": "Ok",
            "routes": [{"duration": 600.0, "distance": 10_000.0,
                        "geometry": {"coordinates": [[9.18, 48.77], [11.58, 48.14]]}}],
        })

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    result = await router.optimized_route([START, END], route_id="x")
    assert result.order == [0, 1]


# -- asymmetric matrix: sources/targets on both backends ----------------------

async def test_valhalla_matrix_selects_source_and_target_locations():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "sources_to_targets": {"durations": [[600, 1200]]}})

    router = _mock_valhalla(handler)
    points = [START, VIA, END]
    durations = await router.matrix(points, sources=[0], targets=[1, 2])

    assert seen["body"]["sources"] == [{"lat": START.lat, "lon": START.lon}]
    assert seen["body"]["targets"] == [
        {"lat": VIA.lat, "lon": VIA.lon}, {"lat": END.lat, "lon": END.lon}]
    assert durations == [[10.0, 20.0]]


async def test_osrm_matrix_passes_sources_and_destinations_indices():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": "Ok", "durations": [[0, 600.0, 900.0]]})

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    durations = await router.matrix([START, VIA, END], sources=[0], targets=[0, 1, 2])

    # Doc-verified: semicolon-separated indices, `sources`/`destinations`.
    assert "sources=0" in seen["url"]
    assert "destinations=0%3B1%3B2" in seen["url"] or "destinations=0;1;2" in seen["url"]
    assert durations == [[0, 10.0, 15.0]]


async def test_matrix_without_sources_stays_all_pairs():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert "sources=" not in url and "destinations=" not in url
        return httpx.Response(200, json={
            "code": "Ok", "durations": [[0, 600.0], [600.0, 0]]})

    router = OsrmRouter("https://osrm.test", transport=httpx.MockTransport(handler))
    assert await router.matrix([START, END]) == [[0, 10.0], [10.0, 0]]


# -- REST wiring for optimize + asymmetric matrix -----------------------------

def test_optimize_endpoint_returns_order_and_route(tmp_path, wms_source):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"trip": _optimized_trip([0, 2, 1])})

    client = _client(tmp_path, wms_source, _mock_valhalla(handler))
    response = client.post("/api/v1/maps/route/optimize", json={
        "stops": [{"lat": 48.7784, "lon": 9.1806},
                  {"lat": 48.4011, "lon": 9.9876},
                  {"lat": 48.1374, "lon": 11.5755}],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["order"] == [0, 2, 1]
    assert body["route"]["duration_min"] == pytest.approx(134.0)


def test_optimize_endpoint_maps_keep_endpoints_refusal_to_502(tmp_path, wms_source):
    client = _client(tmp_path, wms_source,
                     _mock_valhalla(lambda r: httpx.Response(200, json={})))
    response = client.post("/api/v1/maps/route/optimize", json={
        "stops": [{"lat": 48.0, "lon": 9.0}, {"lat": 48.1, "lon": 9.1},
                  {"lat": 48.2, "lon": 9.2}],
        "keep_endpoints": False,
    })
    assert response.status_code == 502
    assert "OSRM" in response.json()["detail"]


def test_optimize_endpoint_blocked_offline(tmp_path, wms_source):
    api = MapsApi(tmp_path, sources={wms_source.id: wms_source}, composites={},
                  router=_mock_valhalla(lambda r: httpx.Response(200, json={})),
                  mode="offline")
    app = FastAPI()
    api.mount(app)
    response = TestClient(app).post("/api/v1/maps/route/optimize", json={
        "stops": [{"lat": 48.0, "lon": 9.0}, {"lat": 48.2, "lon": 9.2}]})
    assert response.status_code == 403


def test_matrix_endpoint_accepts_source_and_target_indices(tmp_path, wms_source):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert len(body["sources"]) == 1 and len(body["targets"]) == 2
        return httpx.Response(200, json={
            "sources_to_targets": {"durations": [[300, 600]]}})

    client = _client(tmp_path, wms_source, _mock_valhalla(handler))
    response = client.post("/api/v1/maps/matrix", json={
        "points": [{"lat": 48.0, "lon": 9.0}, {"lat": 48.1, "lon": 9.1},
                   {"lat": 48.2, "lon": 9.2}],
        "sources": [0], "targets": [1, 2],
    })
    assert response.status_code == 200
    assert response.json()["durations_min"] == [[5.0, 10.0]]


def test_matrix_endpoint_rejects_out_of_range_indices(tmp_path, wms_source):
    client = _client(tmp_path, wms_source,
                     _mock_valhalla(lambda r: httpx.Response(200, json={})))
    response = client.post("/api/v1/maps/matrix", json={
        "points": [{"lat": 48.0, "lon": 9.0}, {"lat": 48.1, "lon": 9.1}],
        "sources": [5],
    })
    assert response.status_code == 400
    assert "indices" in response.json()["detail"]
