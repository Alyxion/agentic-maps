from agentic_maps.harvest.planner import HarvestPlanner
from agentic_maps.models.camera_pose import CameraPose
from agentic_maps.models.lat_lon import LatLon
from agentic_maps.models.map_location import MapLocation
from agentic_maps.models.map_spec import MapSpec
from agentic_maps.models.tile_coord import TileCoord


def _spec_with(locations):
    return MapSpec(id="corridor", source_id="test-wms", locations=locations)


def _loc(loc_id, lat, lon, zoom):
    return MapLocation(id=loc_id, name=loc_id, camera=CameraPose(center=LatLon(lat=lat, lon=lon), zoom=zoom))


def test_nearby_pair_covers_flight_midpoint(wms_source):
    # HQ -> airport is a short hop: the flyTo cruises at a zoom above the
    # overview pyramid, so the corridor must cover the path midpoint.
    spec = _spec_with([
        _loc("hq", 48.5386, 9.2925, 16.5),
        _loc("airport", 48.6899, 9.2219, 14.0),
    ])
    planner = HarvestPlanner(wms_source)
    plan = planner.plan(spec)
    tiles = set(plan.tiles)

    apex_z = planner._fit_zoom(spec.locations[0].camera.center, spec.locations[1].camera.center)
    assert apex_z >= wms_source.min_zoom  # premise: cruise happens above overview zooms
    mid = TileCoord.at((48.5386 + 48.6899) / 2, (9.2925 + 9.2219) / 2, apex_z)
    assert mid in tiles


def test_distant_pair_adds_no_corridor_beyond_overview(wms_source):
    # Stuttgart -> Berlin: the flight apex fits both cities, i.e. it is below
    # the source's min zoom — corridor must not inflate the plan.
    a = _loc("stuttgart", 48.7784, 9.1806, 15.0)
    b = _loc("berlin", 52.5163, 13.3777, 15.0)
    apex_z = HarvestPlanner(wms_source)._fit_zoom(a.camera.center, b.camera.center)
    assert apex_z < wms_source.min_zoom

    with_corridor = HarvestPlanner(wms_source).plan(_spec_with([a, b]))
    assert with_corridor.tile_count < 1200  # endpoint pyramids only, no blanket
