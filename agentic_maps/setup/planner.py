"""Pure planning logic for the 2-minute geo server (docs/setup-guide.md).

No Docker calls, no subprocess, no network — everything here is string/number
math over already-resolved inputs (a bbox, a `PbfResolution`, a `SetupAnswers`)
so it is unit-testable with plain assertions (`tests/test_setup_planner.py`).
The one real I/O side effect the wizard needs — actually cutting a PBF — lives
in `pbf_fetch.py`; resolving a place name to a bbox lives in `wizard.py`
(it needs the geo index / a geocoder). This module only decides *what to do*
with numbers it is handed, and renders the `.env` text from them.

Why profiles don't change the compose file: Valhalla builds one graph with
every costing model (auto/truck/pedestrian/bicycle) regardless of which
`profiles` the wizard was asked to enable — there is no per-profile tile
build. `profiles` only ends up in `.env` as `AGENTIC_MAPS_ENABLED_PROFILES`,
advisory metadata a FUTURE API/UI layer could filter its mode menu by —
nothing reads it today, and it is not wired into the Valhalla/Nominatim
service definitions at all.
"""

import re
from pathlib import Path
from typing import Literal

from ..models.bbox_deg import BBoxDeg
from ..models.pbf_resolution import PbfResolution
from ..models.setup_answers import SetupAnswers
from ..models.setup_plan import SetupPlan

SetupMode = Literal["offline", "mixed", "online"]
MODES: tuple[SetupMode, ...] = ("offline", "mixed", "online")

TRAVEL_PROFILES = ("car", "truck", "walk", "bike")

# The OSM v0.6 API's own documented cap for GET /api/0.6/map — "the maximal
# width and height of the bounding box is 0.25 degree" (wiki.openstreetmap.org
# /wiki/API_v0.6), enforced with an HTTP error past it. overpass-api.de/api/map
# is the read-heavy-friendly mirror of that exact endpoint (same bbox= format,
# same practical ceiling) that OSM's own website "Export" button calls — see
# pbf_fetch.py. 0.25 x 0.25 degrees is comfortably "a city plus a ring of
# suburbs" at most inhabited latitudes (at 50 degrees north, roughly 27 km x
# 28 km) and nowhere close to a state/country. This is a real, documented,
# citable limit — not a guess.
OVERPASS_MAX_AREA_DEG2 = 0.25 * 0.25

DEFAULT_CITY_RADIUS_KM = 6.0

# Compose services (deploy/docker-compose.yml) — kept as constants so the
# planner and the compose file cannot drift on a name typo, and so
# `docker-compose.offline.yml`'s overrides (which key by service name) stay
# valid no matter how `.env` is filled in.
SERVICE_API = "agentic-maps-api"
SERVICE_VALHALLA = "valhalla"
SERVICE_NOMINATIM = "nominatim"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(place: str, *, fallback: str = "region") -> str:
    """A place name into a compose-safe, filesystem-safe id.

    "Hannover, Germany" -> "hannover-germany". Docker Compose project/volume
    names and dotenv values both dislike spaces/punctuation, and this doubles
    as the Nominatim/Valhalla data directory name so it must also be a valid
    path segment.
    """
    import unicodedata

    ascii_only = unicodedata.normalize("NFKD", place).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only.strip().lower()).strip("-")
    return slug or fallback


def bbox_from_center(lat: float, lon: float, radius_km: float = DEFAULT_CITY_RADIUS_KM) -> BBoxDeg:
    """A square-ish box `radius_km` around a point.

    Equirectangular approximation (same one `rest/maps_api.py._rough_distance_km`
    uses) — plenty accurate for sizing a setup-wizard extract, which is itself
    a rough "town plus surroundings" ask, not a survey boundary.
    """
    import math

    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return BBoxDeg(
        west=max(-180.0, lon - dlon), east=min(180.0, lon + dlon),
        south=max(-85.05, lat - dlat), north=min(85.05, lat + dlat),
    )


def choose_pbf_strategy(bbox: BBoxDeg, answers: SetupAnswers) -> str:
    """Which `PbfResolution.method` applies, before any I/O happens.

    Explicit overrides always win (a user who pasted a Geofabrik URL knows
    better than a heuristic). Otherwise: small enough for the Overpass
    `/api/map` bbox export -> fully automatic; too big -> `manual-required`,
    because no on-demand city-scoped extract service with a real scriptable
    API exists (see `pbf_fetch.py`'s module docstring) — the wizard must ask
    for a Geofabrik regional/country URL instead.
    """
    if answers.pbf_url.strip():
        return "user-url"
    if answers.pbf_path.strip():
        return "user-path"
    if bbox.area_deg2 <= OVERPASS_MAX_AREA_DEG2:
        return "overpass"
    return "manual-required"


def manual_pbf_resolution(bbox: BBoxDeg) -> PbfResolution:
    """The honest answer when neither Overpass nor an override applies."""
    return PbfResolution(
        method="manual-required",
        bbox=bbox,
        note=(
            f"This area is {bbox.area_deg2:.3f} square degrees — past the "
            f"{OVERPASS_MAX_AREA_DEG2:.3f} the Overpass API's bbox export "
            "realistically covers (it mirrors the OSM API's own 0.25x0.25 "
            "degree /api/map cap). There is no on-demand city-scoped PBF "
            "service with a real, unauthenticated, scriptable API: "
            "Geofabrik only publishes country/state-level extracts, and "
            "BBBike's extract service is an interactive web form (2-7 "
            "minute queue) with no documented API. Supply a Geofabrik "
            "regional/country .osm.pbf URL yourself (download.geofabrik.de) "
            "as `pbf_url`, or a local file as `pbf_path` — see "
            "docs/setup-guide.md."
        ),
    )


def user_pbf_resolution(answers: SetupAnswers, bbox: BBoxDeg) -> PbfResolution:
    if answers.pbf_url.strip():
        return PbfResolution(
            method="user-url", url=answers.pbf_url.strip(), bbox=bbox,
            note="Supplied directly — not verified reachable until the "
                 "Valhalla/Nominatim containers actually fetch it.",
        )
    return PbfResolution(
        method="user-path", path=answers.pbf_path.strip(), bbox=bbox,
        note="Local file — write_plan() copies it into ./valhalla_data/ and "
             "./nominatim_data/ next to the generated .env.",
    )


def _env_line(key: str, value: str) -> str:
    # dotenv has no real quoting rules; wrapping in double quotes is the one
    # form every reader (docker compose, `source .env`, python-dotenv) agrees
    # on, and it is what protects a value containing spaces (a raw bbox
    # string, a place name echoed back for readability).
    return f'{key}="{value}"'


def render_env(answers: SetupAnswers, *, region_id: str, bbox: BBoxDeg, pbf: PbfResolution) -> str:
    """The full `.env` file `deploy/docker-compose.yml` reads its
    `${AGENTIC_MAPS_...}` / `${VALHALLA_...}` / `${NOMINATIM_...}`
    substitutions from.

    One canonical compose file, parameterised through `.env`, rather than a
    hand-rolled YAML generator that has to be kept byte-for-byte in step with
    `deploy/docker-compose.yml` — see `SetupPlan`'s docstring.
    """
    profiles = ",".join(p for p in answers.profiles if p in TRAVEL_PROFILES) or "car"
    lines = [
        "# Generated by `agentic-maps-setup` / POST /setup/apply — safe to hand-edit.",
        _env_line("AGENTIC_MAPS_REGION_ID", region_id),
        _env_line("AGENTIC_MAPS_REGION_BBOX", f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}"),
        _env_line("AGENTIC_MAPS_MODE", answers.mode),
        _env_line("AGENTIC_MAPS_ENABLED_PROFILES", profiles),
        # Service-to-service URLs inside the compose network (container DNS
        # names, not localhost) — what agentic-maps-api's own
        # AGENTIC_MAPS_VALHALLA_URL / AGENTIC_MAPS_NOMINATIM_URL expect.
        _env_line("AGENTIC_MAPS_VALHALLA_URL", f"http://{SERVICE_VALHALLA}:8002"),
        _env_line("AGENTIC_MAPS_NOMINATIM_URL", f"http://{SERVICE_NOMINATIM}:8080"),
        # Host port bindings — separate from the above so a port clash on the
        # host is a one-line edit, not a hunt through service definitions.
        _env_line("AGENTIC_MAPS_API_PORT", "8095"),
        _env_line("VALHALLA_PORT", "8002"),
        _env_line("NOMINATIM_PORT", "8080"),
    ]
    # Branch on what `pbf` actually carries (a URL vs. a local path vs.
    # neither), not on `pbf.method` — an "overpass" resolution ends up with
    # a local `path` (the file `pbf_fetch.fetch_city_pbf` just wrote), which
    # needs the exact same env wiring a `user-path` override gets: Valhalla
    # auto-detects any PBF dropped into its `/custom_files` volume mount (no
    # env var needed, see deploy/docker-compose.yml's `valhalla_data` mount),
    # Nominatim needs `PBF_PATH` pointed at it inside its own mount.
    if pbf.url:
        # Both containers fetch it independently, so the graph and the
        # geocoder index always describe the identical extract.
        lines.append(_env_line("VALHALLA_TILE_URLS", pbf.url))
        lines.append(_env_line("NOMINATIM_PBF_URL", pbf.url))
        lines.append(_env_line("NOMINATIM_PBF_PATH", ""))
    elif pbf.path:
        lines.append(_env_line("VALHALLA_TILE_URLS", ""))
        lines.append(_env_line("NOMINATIM_PBF_URL", ""))
        # Container-internal path: `write_plan` copies the file into
        # `<out_dir>/nominatim_data/`, which docker-compose.yml mounts at
        # `/nominatim/data`.
        lines.append(_env_line("NOMINATIM_PBF_PATH", f"/nominatim/data/{Path(pbf.path).name}"))
        lines.append(
            "# local PBF: write_plan() copies it into ./valhalla_data/ and "
            "./nominatim_data/ next to this .env — see docs/setup-guide.md."
        )
    else:
        lines.append(_env_line("VALHALLA_TILE_URLS", ""))
        lines.append(_env_line("NOMINATIM_PBF_URL", ""))
        lines.append(_env_line("NOMINATIM_PBF_PATH", ""))
        lines.append(f"# {pbf.note}")
    # `AGENTIC_MAPS_MODE` above already carries the three-way distinction
    # `agentic-maps-api` reads (`RuntimeMode`, rest/maps_api.py) — no
    # separate offline flag is emitted. "offline" means no live upstream
    # ever, including on restart — see docker-compose.offline.yml, which
    # additionally unsets Valhalla/Nominatim's own *_URL fetch variables and
    # relies entirely on the volumes already populated on first boot.
    return "\n".join(lines) + "\n"


def compose_files_for_mode(mode: str) -> list[str]:
    """Which `deploy/docker-compose*.yml` files to layer, in order.

    "online"/"mixed" run the base file alone (containers may still reach the
    internet to fetch their PBF / talk to upstream). "offline" adds the
    override, which is `docker-compose.offline.yml`'s entire reason to
    exist: same service names, internet-requiring bits stripped, so it only
    works once the volumes are already populated from an earlier "mixed"/
    "online" run.
    """
    base = ["docker-compose.yml"]
    if mode == "offline":
        return base + ["docker-compose.offline.yml"]
    return base


def preview_pbf(bbox: BBoxDeg, answers: SetupAnswers) -> PbfResolution:
    """`choose_pbf_strategy`, without ever fetching bytes.

    Used by `POST /setup/plan` (a cheap review step — see `rest/setup_api.py`)
    and by `tests/test_setup_planner.py`: the `"overpass"` case is reported
    with an empty `path` and a note that the real fetch happens at apply
    time, rather than actually calling Overpass/osmium. `"user-url"` /
    `"user-path"` / `"manual-required"` need no I/O either way, so those
    three are identical to what the real apply-time resolution produces.
    """
    strategy = choose_pbf_strategy(bbox, answers)
    if strategy in ("user-url", "user-path"):
        return user_pbf_resolution(answers, bbox)
    if strategy == "manual-required":
        return manual_pbf_resolution(bbox)
    return PbfResolution(
        method="overpass", bbox=bbox,
        note=f"Will be cut from OpenStreetMap via the Overpass API's "
             f"/api/map bbox export ({bbox.area_deg2:.4f} sq deg, under the "
             f"{OVERPASS_MAX_AREA_DEG2:.4f} sq deg threshold) at apply time.",
    )


def plan_stack(answers: SetupAnswers, *, bbox: BBoxDeg, pbf: PbfResolution) -> SetupPlan:
    """Assemble the `SetupPlan` from already-resolved inputs.

    Pure: everything here is string formatting and list building over `bbox`
    and `pbf`, which the caller (`wizard.plan_from_answers` /
    `rest/setup_api.py`) resolved beforehand — that is the actual I/O
    boundary, kept out of this function on purpose (see module docstring).
    """
    if answers.mode not in MODES:
        raise ValueError(f"unknown mode {answers.mode!r}; expected one of {MODES}")
    region_id = answers.region_id.strip() or slugify(answers.place)
    warnings: list[str] = []
    if pbf.method == "manual-required":
        warnings.append(pbf.note)
    if answers.mode == "offline" and pbf.method == "manual-required":
        warnings.append(
            "offline mode was requested but no PBF source is resolved yet — "
            "the first `docker compose up` still needs internet to build the "
            "graph/import the geocoder once; offline only governs *later* "
            "restarts."
        )
    env_content = render_env(answers, region_id=region_id, bbox=bbox, pbf=pbf)
    return SetupPlan(
        region_id=region_id,
        bbox=bbox,
        mode=answers.mode,
        profiles=[p for p in answers.profiles if p in TRAVEL_PROFILES] or ["car"],
        pbf=pbf,
        env_content=env_content,
        compose_files=compose_files_for_mode(answers.mode),
        warnings=warnings,
        ready_urls={
            SERVICE_API: "http://localhost:8095",
            SERVICE_VALHALLA: "http://localhost:8002",
            SERVICE_NOMINATIM: "http://localhost:8080",
        },
    )
