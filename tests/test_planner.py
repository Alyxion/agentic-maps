from agentic_maps.harvest.planner import HarvestPlanner
from agentic_maps.models.tile_coord import TileCoord


def test_plan_covers_detail_and_overview(wms_source, two_stop_spec):
    plan = HarvestPlanner(wms_source).plan(two_stop_spec)

    assert plan.tile_count > 0
    assert len(set(plan.tiles)) == plan.tile_count  # deduplicated
    zooms = {t.z for t in plan.tiles}
    assert min(zooms) == wms_source.min_zoom
    assert max(zooms) == 17  # location zoom 16.5 rounds up, capped by source

    # The detail tile directly over each location must be included.
    hq_detail = TileCoord.at(48.5386, 9.2925, 17)
    assert hq_detail in set(plan.tiles)
    airport_detail = TileCoord.at(48.6899, 9.2219, 14)
    assert airport_detail in set(plan.tiles)


def test_plan_is_viewport_sized_not_blanket(wms_source, two_stop_spec):
    plan = HarvestPlanner(wms_source).plan(two_stop_spec)
    # A blanket download of the union bbox at z17 alone would be tens of
    # thousands of tiles; the corridor strategy keeps the whole pyramid small.
    assert plan.tile_count < 2500
    assert plan.estimated_mb > 0


def test_plan_of_a_spec_with_no_locations_is_the_overview_pyramid(wms_source):
    """`MapSpec.locations` may be empty — the map app's own starting spec is
    exactly that (devserver.empty_spec). Regression: this used to blow up
    with a bare `max() iterable argument is empty` ValueError, turning
    `POST /plan` on the app's default spec into a 500."""
    from agentic_maps.models.camera_pose import CameraPose
    from agentic_maps.models.lat_lon import LatLon
    from agentic_maps.models.map_spec import MapSpec

    spec = MapSpec(
        id="map-app", source_id=wms_source.id,
        overview=CameraPose(center=LatLon(lat=48.6, lon=9.25), zoom=12.4),
        locations=[],
    )
    plan = HarvestPlanner(wms_source).plan(spec)
    assert plan.tile_count > 0
    zooms = {t.z for t in plan.tiles}
    assert min(zooms) == wms_source.min_zoom
    # ceil(12.4) = 13 — the overview camera's own zoom bounds the pyramid.
    assert max(zooms) <= 13


def test_plan_without_locations_or_overview_refuses_cleanly(wms_source):
    import pytest

    from agentic_maps.models.map_spec import MapSpec

    spec = MapSpec(id="void", source_id=wms_source.id, locations=[])
    with pytest.raises(ValueError, match="no locations and no overview"):
        HarvestPlanner(wms_source).plan(spec)
