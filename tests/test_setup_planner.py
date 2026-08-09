"""Unit tests for `agentic_maps/setup/planner.py` — pure logic only.

No Docker, no real network: `preview_pbf`'s "overpass" branch deliberately
never fetches anything (see its docstring), so exercising it here proves the
no-I/O contract rather than assuming it.
"""

import pytest

from agentic_maps.models.bbox_deg import BBoxDeg
from agentic_maps.models.setup_answers import SetupAnswers
from agentic_maps.setup import planner

SMALL_BBOX = BBoxDeg(west=9.70, south=52.35, east=9.78, north=52.40)  # Hannover-ish, small
BIG_BBOX = BBoxDeg(west=5.0, south=47.0, east=15.0, north=55.0)  # ~Germany-sized


def test_slugify_basic():
    assert planner.slugify("Hannover, Germany") == "hannover-germany"
    assert planner.slugify("  Ötztal Alps!! ") == "otztal-alps"
    assert planner.slugify("") == "region"
    assert planner.slugify("   ") == "region"


def test_bbox_from_center_is_ordered_and_small():
    box = planner.bbox_from_center(52.37, 9.73, radius_km=5.0)
    assert box.west < box.east
    assert box.south < box.north
    # A 5 km radius box is comfortably under the Overpass threshold.
    assert box.area_deg2 < planner.OVERPASS_MAX_AREA_DEG2


def test_choose_pbf_strategy_small_bbox_is_overpass():
    answers = SetupAnswers(place="Hannover")
    assert planner.choose_pbf_strategy(SMALL_BBOX, answers) == "overpass"


def test_choose_pbf_strategy_big_bbox_needs_manual():
    answers = SetupAnswers(place="Germany")
    assert planner.choose_pbf_strategy(BIG_BBOX, answers) == "manual-required"


def test_choose_pbf_strategy_explicit_url_wins_even_for_small_bbox():
    answers = SetupAnswers(place="Hannover", pbf_url="https://download.geofabrik.de/europe/germany/niedersachsen-latest.osm.pbf")
    assert planner.choose_pbf_strategy(SMALL_BBOX, answers) == "user-url"


def test_choose_pbf_strategy_explicit_path_wins():
    answers = SetupAnswers(place="Hannover", pbf_path="/data/region.osm.pbf")
    assert planner.choose_pbf_strategy(SMALL_BBOX, answers) == "user-path"


def test_choose_pbf_strategy_url_beats_path_when_both_given():
    answers = SetupAnswers(pbf_url="https://example.test/a.osm.pbf", pbf_path="/data/b.osm.pbf")
    assert planner.choose_pbf_strategy(SMALL_BBOX, answers) == "user-url"


def test_preview_pbf_overpass_case_has_no_url_or_path():
    answers = SetupAnswers(place="Hannover")
    resolution = planner.preview_pbf(SMALL_BBOX, answers)
    assert resolution.method == "overpass"
    assert resolution.url == ""
    assert resolution.path == ""
    assert "apply time" in resolution.note


def test_preview_pbf_manual_required_explains_why():
    answers = SetupAnswers(place="Germany")
    resolution = planner.manual_pbf_resolution(BIG_BBOX)
    assert resolution.method == "manual-required"
    assert "Geofabrik" in resolution.note
    assert "BBBike" in resolution.note


def test_render_env_user_url_sets_both_containers():
    answers = SetupAnswers(place="Hannover", mode="mixed", pbf_url="https://example.test/x.osm.pbf")
    pbf = planner.user_pbf_resolution(answers, SMALL_BBOX)
    env = planner.render_env(answers, region_id="hannover", bbox=SMALL_BBOX, pbf=pbf)
    assert 'VALHALLA_TILE_URLS="https://example.test/x.osm.pbf"' in env
    assert 'NOMINATIM_PBF_URL="https://example.test/x.osm.pbf"' in env
    assert 'NOMINATIM_PBF_PATH=""' in env
    assert 'AGENTIC_MAPS_VALHALLA_URL="http://valhalla:8002"' in env
    assert 'AGENTIC_MAPS_NOMINATIM_URL="http://nominatim:8080"' in env
    assert 'AGENTIC_MAPS_MODE="mixed"' in env


def test_render_env_local_path_points_nominatim_at_container_path():
    answers = SetupAnswers(place="Hannover", mode="mixed")
    from agentic_maps.models.pbf_resolution import PbfResolution

    pbf = PbfResolution(method="overpass", path="/tmp/out/hannover.osm.pbf", bbox=SMALL_BBOX, note="fetched")
    env = planner.render_env(answers, region_id="hannover", bbox=SMALL_BBOX, pbf=pbf)
    assert 'NOMINATIM_PBF_PATH="/nominatim/data/hannover.osm.pbf"' in env
    assert 'VALHALLA_TILE_URLS=""' in env


def test_render_env_manual_required_leaves_pbf_vars_empty_with_note():
    answers = SetupAnswers(place="Germany", mode="mixed")
    pbf = planner.manual_pbf_resolution(BIG_BBOX)
    env = planner.render_env(answers, region_id="germany", bbox=BIG_BBOX, pbf=pbf)
    assert 'VALHALLA_TILE_URLS=""' in env
    assert 'NOMINATIM_PBF_URL=""' in env
    assert "# " + pbf.note in env or pbf.note[:40] in env


def test_render_env_offline_mode_flag():
    answers = SetupAnswers(place="Hannover", mode="offline", pbf_url="https://example.test/x.osm.pbf")
    pbf = planner.user_pbf_resolution(answers, SMALL_BBOX)
    env = planner.render_env(answers, region_id="hannover", bbox=SMALL_BBOX, pbf=pbf)
    assert 'AGENTIC_MAPS_MODE="offline"' in env


@pytest.mark.parametrize("mode,expected", [
    ("online", ["docker-compose.yml"]),
    ("mixed", ["docker-compose.yml"]),
    ("offline", ["docker-compose.yml", "docker-compose.offline.yml"]),
])
def test_compose_files_for_mode(mode, expected):
    assert planner.compose_files_for_mode(mode) == expected


def test_plan_stack_rejects_unknown_mode():
    answers = SetupAnswers(place="Hannover", mode="bogus")
    pbf = planner.manual_pbf_resolution(SMALL_BBOX)
    with pytest.raises(ValueError):
        planner.plan_stack(answers, bbox=SMALL_BBOX, pbf=pbf)


def test_plan_stack_derives_region_id_from_place():
    answers = SetupAnswers(place="Hannover, Germany", pbf_url="https://example.test/x.osm.pbf")
    pbf = planner.user_pbf_resolution(answers, SMALL_BBOX)
    plan = planner.plan_stack(answers, bbox=SMALL_BBOX, pbf=pbf)
    assert plan.region_id == "hannover-germany"
    assert plan.mode == "mixed"
    assert plan.profiles == ["car"]
    assert plan.compose_files == ["docker-compose.yml"]
    assert plan.warnings == []
    assert plan.ready_urls[planner.SERVICE_API] == "http://localhost:8095"


def test_plan_stack_explicit_region_id_wins():
    answers = SetupAnswers(place="Hannover", region_id="hq", pbf_url="https://example.test/x.osm.pbf")
    pbf = planner.user_pbf_resolution(answers, SMALL_BBOX)
    plan = planner.plan_stack(answers, bbox=SMALL_BBOX, pbf=pbf)
    assert plan.region_id == "hq"


def test_plan_stack_warns_on_manual_required_pbf():
    answers = SetupAnswers(place="Germany")
    pbf = planner.manual_pbf_resolution(BIG_BBOX)
    plan = planner.plan_stack(answers, bbox=BIG_BBOX, pbf=pbf)
    assert plan.warnings
    assert "Geofabrik" in plan.warnings[0]


def test_plan_stack_filters_unknown_profiles():
    answers = SetupAnswers(place="Hannover", profiles=["car", "spaceship"], pbf_url="https://example.test/x.osm.pbf")
    pbf = planner.user_pbf_resolution(answers, SMALL_BBOX)
    plan = planner.plan_stack(answers, bbox=SMALL_BBOX, pbf=pbf)
    assert plan.profiles == ["car"]
