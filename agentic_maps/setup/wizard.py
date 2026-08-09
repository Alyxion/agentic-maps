"""`agentic-maps-setup` — the CLI front-end over `planner.py`.

Split into four separately-testable stages, none of which touch real Docker
in tests (`tests/test_setup_wizard.py` mocks the network client / subprocess
runner at each boundary):

1. `plan_from_answers` — resolve place -> bbox, decide + (if small enough)
   actually fetch the PBF, hand off to `planner.plan_stack`.
2. `write_plan` — materialize `.env` + the compose files + any fetched PBF
   into an output directory. Pure filesystem writes, no subprocess.
3. `run_stack` — if `docker` is on PATH, ask, then `docker compose up -d`
   and poll each service's port. Everything after "is docker present" is
   injectable (`confirm`, `subprocess_run`, `probe_port`) so a test can
   verify the exact command without a real Docker daemon.
4. `main` — wires real `input()` / `shutil.which` / `subprocess.run` /
   `socket` around the above for actual CLI use.
"""

import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from ..models.bbox_deg import BBoxDeg
from ..models.geocode_result import GeocodeResult
from ..models.pbf_resolution import PbfResolution
from ..models.provision_request import AERIAL_ZOOM_CAPS, ProvisionRequest
from ..models.setup_answers import SetupAnswers
from ..models.setup_plan import SetupPlan
from ..models.setup_run_result import SetupRunResult
from ..provision.estimates import estimate_region, preset_size_table
from ..rest.maps_api import DEFAULT_NOMINATIM_URL, NOMINATIM_URL_ENV
from . import pbf_fetch, planner

_TEMPLATE_FILES = ("Dockerfile", "docker-compose.yml", "docker-compose.offline.yml")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_DIR = _REPO_ROOT / "deploy"


# -- stage 1: planning -------------------------------------------------

async def default_resolve_bbox(answers: SetupAnswers, *, client: httpx.AsyncClient) -> BBoxDeg:
    """Explicit bbox wins; otherwise geocode `place` and box a radius around it.

    Deliberately a plain Nominatim `/search` call, not the offline
    `CityIndex` (`geo/countries.py`) — the wizard may run before any dev
    server session ever downloaded that index file, and a fresh checkout
    should not require one just to plan a stack. This is the one live call
    the whole wizard makes before any self-hosted service exists, so it goes
    to whatever `AGENTIC_MAPS_NOMINATIM_URL` already points at (default: the
    public instance, light interactive use) — same env var the running
    stack's own `/geocode` proxy reads, see `rest/maps_api.py`.
    """
    if answers.bbox is not None:
        return answers.bbox
    import os

    place = answers.place.strip()
    if not place:
        raise ValueError("either `place` or `bbox` must be given")
    base = os.environ.get(NOMINATIM_URL_ENV, "").strip().rstrip("/") or DEFAULT_NOMINATIM_URL
    response = await client.get(
        f"{base}/search",
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": "agentic-maps-setup/0.1 (self-hosted geo stack wizard)"},
        timeout=20.0,
    )
    response.raise_for_status()
    hits = response.json()
    if not hits:
        raise ValueError(f"could not geocode {place!r} — try an explicit bbox instead")
    hit = GeocodeResult(
        name=hits[0].get("display_name", place),
        lat=float(hits[0]["lat"]), lon=float(hits[0]["lon"]),
    )
    return planner.bbox_from_center(hit.lat, hit.lon)


async def default_fetch_pbf(
    bbox: BBoxDeg, region_id: str, out_dir: Path, answers: SetupAnswers, *, client: httpx.AsyncClient,
) -> PbfResolution:
    """`planner.choose_pbf_strategy`, then actually fetch when it says `overpass`."""
    strategy = planner.choose_pbf_strategy(bbox, answers)
    if strategy in ("user-url", "user-path"):
        return planner.user_pbf_resolution(answers, bbox)
    if strategy == "manual-required":
        return planner.manual_pbf_resolution(bbox)
    path = await pbf_fetch.fetch_city_pbf(region_id, bbox, out_dir, client=client)
    return PbfResolution(
        method="overpass", path=str(path), bbox=bbox,
        note=f"Cut from OpenStreetMap via the Overpass API's /api/map bbox "
             f"export ({bbox.area_deg2:.4f} sq deg) and converted with "
             f"osmium — see setup/pbf_fetch.py.",
    )


ResolveBbox = Callable[[SetupAnswers], Awaitable[BBoxDeg]]
FetchPbf = Callable[[BBoxDeg, str, Path, SetupAnswers], Awaitable[PbfResolution]]


async def plan_from_answers(
    answers: SetupAnswers,
    *,
    out_dir: Path,
    resolve_bbox: ResolveBbox | None = None,
    fetch_pbf: FetchPbf | None = None,
    client: httpx.AsyncClient | None = None,
) -> SetupPlan:
    """Stage 1: turn answers into a `SetupPlan`, doing whatever I/O that needs.

    `resolve_bbox`/`fetch_pbf` default to the live implementations above but
    are injectable — `tests/test_setup_wizard.py` passes stubs so this can be
    exercised with zero real network/subprocess calls.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        if resolve_bbox is not None:
            bbox = await resolve_bbox(answers)
        else:
            bbox = await default_resolve_bbox(answers, client=client)
        region_id = answers.region_id.strip() or planner.slugify(answers.place)
        if fetch_pbf is not None:
            pbf = await fetch_pbf(bbox, region_id, out_dir, answers)
        else:
            pbf = await default_fetch_pbf(bbox, region_id, out_dir, answers, client=client)
    finally:
        if owns_client:
            await client.aclose()
    return planner.plan_stack(answers, bbox=bbox, pbf=pbf)


# -- stage 2: writing ----------------------------------------------------

def write_plan(plan: SetupPlan, out_dir: Path) -> None:
    """Write `.env`, the compose files, and any locally-fetched PBF.

    Copies (rather than symlinks — portable across filesystems/OSes) the
    three checked-in `deploy/` templates into `out_dir` so the generated
    stack is self-contained and can live anywhere, not just next to a repo
    checkout. `build.context` in `docker-compose.yml` is `${AGENTIC_MAPS_REPO_ROOT:-..}`
    for exactly this reason — this function pins it to the real repo root in
    `.env` rather than assuming `out_dir`'s parent is the checkout.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    env_content = plan.env_content
    if not env_content.rstrip("\n").endswith(f'AGENTIC_MAPS_REPO_ROOT="{_REPO_ROOT}"'):
        env_content = env_content.rstrip("\n") + "\n" + f'AGENTIC_MAPS_REPO_ROOT="{_REPO_ROOT}"\n'
    (out_dir / ".env").write_text(env_content)

    for name in _TEMPLATE_FILES:
        source = _DEPLOY_DIR / name
        if source.exists():
            (out_dir / name).write_text(source.read_text())

    if plan.pbf.path:
        source = Path(plan.pbf.path)
        if source.exists():
            for subdir in ("valhalla_data", "nominatim_data"):
                target_dir = out_dir / subdir
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / source.name
                if source.resolve() != target.resolve():
                    target.write_bytes(source.read_bytes())


# -- stage 3: running ------------------------------------------------------

def docker_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    return which("docker") is not None


def _probe_port(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def run_stack(
    out_dir: Path,
    plan: SetupPlan,
    *,
    confirm: Callable[[str], bool] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    subprocess_run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    probe_port: Callable[[str, int], bool] = lambda host, port: _probe_port(host, port),
    poll_attempts: int = 30,
    poll_interval_s: float = 2.0,
) -> SetupRunResult:
    """Stage 3: `docker compose up -d`, if the user says yes and `docker` exists.

    Never runs Docker on its own — the caller must both have `docker` on
    PATH AND say yes via `confirm` (default: none supplied means "assume no",
    so a script that forgets to pass one gets the safe, no-op behaviour
    rather than an accidental launch). Health-polling and the actual
    subprocess call are both injectable so this is fully testable without a
    Docker daemon (`tests/test_setup_wizard.py`).
    """
    if not docker_available(which=which):
        return SetupRunResult(
            started=False,
            message="`docker` was not found on PATH. Install Docker, then run "
                    f"`docker compose -f {' -f '.join(plan.compose_files)} up -d` "
                    f"from {out_dir}.",
        )
    if confirm is None or not confirm(
        f"Run `docker compose -f {' -f '.join(plan.compose_files)} up -d` in {out_dir} now?"
    ):
        return SetupRunResult(
            started=False,
            message=f"Skipped. Run it yourself: `docker compose -f "
                    f"{' -f '.join(plan.compose_files)} up -d` from {out_dir}.",
        )

    args = ["docker", "compose"]
    for name in plan.compose_files:
        args += ["-f", name]
    args += ["up", "-d"]
    result = subprocess_run(args, cwd=str(out_dir), capture_output=True, text=True)
    if result.returncode != 0:
        return SetupRunResult(
            started=False,
            message=f"`docker compose up` failed (exit {result.returncode}): "
                    f"{(result.stderr or '').strip()[-500:]}",
        )

    ready: dict[str, str] = {}
    ports = {
        planner.SERVICE_API: 8095, planner.SERVICE_VALHALLA: 8002, planner.SERVICE_NOMINATIM: 8080,
    }
    for _ in range(poll_attempts):
        for service, port in list(ports.items()):
            if probe_port("localhost", port):
                ready[service] = plan.ready_urls.get(service, f"http://localhost:{port}")
                del ports[service]
        if not ports:
            break
        time.sleep(poll_interval_s)

    message = "Stack is up." if not ports else (
        f"Stack started, but still waiting on: {', '.join(sorted(ports))} "
        "(they may just need more time — check `docker compose logs`)."
    )
    return SetupRunResult(started=True, message=message, ready_urls=ready)


# -- stage 4: Offline-Daten (optional, skippable) ---------------------------

def ask_offline_data(
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> ProvisionRequest | None:
    """The "Offline-Daten" step: same presets and the same size table the
    MCP tool and the web wizard show (`provision/estimates.py` is the one
    source). Returns the confirmed request, or None when skipped — the
    caller decides how to run it (the CLI runs it inline via the same
    ProvisionEngine the server uses)."""
    print_fn("\nOffline-Daten (offline data packs) — optional, Enter to skip.")
    print_fn("Sizes are computed from the same estimator the server enforces "
             "(~12 KB/tile average):")
    for line in preset_size_table():
        print_fn(f"  {line}")
    region = input_fn("Region [de/dach/eu/earth] (blank = skip): ").strip().lower()
    if not region:
        return None
    if region == "earth":
        layers = ["aerial"]
        zoom = None
    else:
        raw = input_fn("Layers, comma-separated [maps,aerial,routing] "
                       "(default: maps,routing): ").strip()
        layers = [l.strip() for l in raw.split(",") if l.strip()] or ["maps", "routing"]
        zoom = None
        if "aerial" in layers:
            zoom_raw = input_fn(f"Aerial zoom cap {AERIAL_ZOOM_CAPS} (the size dial): ").strip()
            zoom = int(zoom_raw) if zoom_raw.isdigit() else None
    try:
        request = ProvisionRequest(region=region, layers=layers, aerial_max_zoom=zoom)
        estimate = estimate_region(request)
    except ValueError as error:
        print_fn(f"! {error}")
        return None
    print_fn("This selection costs:")
    for layer in estimate.layers:
        print_fn(f"  {layer.display}")
    for warning in estimate.warnings:
        print_fn(f"  ! {warning}")
    confirmed = input_fn(
        f"Download {estimate.total_display} now? [y/N] ").strip().lower().startswith("y")
    return request if confirmed else None


async def run_offline_data(request: ProvisionRequest, bundles_dir: Path,
                           *, print_fn: Callable[[str], None] = print) -> None:
    """Run one provisioning job inline, through the SAME engine the server
    uses (`MapsApi.provision`), printing honest progress until it ends —
    what makes the 5-minute promise real without a server running yet."""
    from ..rest.maps_api import MapsApi

    bundles_dir.mkdir(parents=True, exist_ok=True)
    api = MapsApi(bundles_dir)
    job = api.provision.start(request)
    while api.provision.running(job.id):
        await asyncio.sleep(2.0)
        print_fn(f"  [{job.state}] {job.phase} "
                 f"({job.tiles_done}/{job.tiles_total} tiles, {job.bytes_done:,} bytes)")
    print_fn(f"Job {job.state}: " + "; ".join(
        f"{layer.layer}={layer.status} ({layer.detail})" for layer in job.layers))
    for layer in job.layers:
        if layer.next_command:
            print_fn(f"  next: {layer.next_command}")


# -- CLI entry point -------------------------------------------------------

def ask_answers(*, input_fn: Callable[[str], str] = input) -> SetupAnswers:
    place = input_fn("Place (city/town name), or leave blank to enter a bbox: ").strip()
    bbox = None
    if not place:
        raw = input_fn("Bbox as west,south,east,north: ").strip()
        west, south, east, north = (float(part) for part in raw.split(","))
        bbox = BBoxDeg(west=west, south=south, east=east, north=north)
    mode = input_fn("Mode [offline/mixed/online] (default: mixed): ").strip() or "mixed"
    profiles_raw = input_fn("Profiles, comma-separated [car,truck,walk,bike] (default: car): ").strip()
    profiles = [p.strip() for p in profiles_raw.split(",") if p.strip()] or ["car"]
    pbf_url = input_fn("PBF URL override (leave blank to auto-detect): ").strip()
    return SetupAnswers(place=place, bbox=bbox, mode=mode, profiles=profiles, pbf_url=pbf_url)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="agentic-maps setup: the 2-minute geo server")
    parser.add_argument("--out-dir", type=Path, default=Path.cwd() / "agentic-maps-stack")
    parser.add_argument("--yes", action="store_true", help="run docker compose up without asking")
    args = parser.parse_args()

    answers = ask_answers()
    print(f"Planning a {answers.mode} stack for {answers.place or answers.bbox}...")
    plan = asyncio.run(plan_from_answers(answers, out_dir=args.out_dir))
    for warning in plan.warnings:
        print(f"! {warning}")
    write_plan(plan, args.out_dir)
    print(f"Wrote .env + compose files to {args.out_dir}")

    result = run_stack(
        args.out_dir, plan,
        confirm=(lambda _prompt: True) if args.yes else (lambda prompt: input(f"{prompt} [y/N] ").lower().startswith("y")),
    )
    print(result.message)
    for service, url in result.ready_urls.items():
        print(f"  {service}: {url}")

    # Offline-Daten: same presets/size table as the MCP tool and the web
    # wizard, one honest confirmation, skippable with a bare Enter.
    offline_request = ask_offline_data()
    if offline_request is not None:
        asyncio.run(run_offline_data(offline_request, _REPO_ROOT / "var" / "bundles"))

    if result.ready_urls.get(planner.SERVICE_API):
        api_url = result.ready_urls[planner.SERVICE_API]
        print(f"\nTry it: curl '{api_url}/api/v1/maps/geocode' -X POST -H 'content-type: application/json' "
              f"-d '{{\"q\": \"{answers.place or 'town square'}\"}}'")


if __name__ == "__main__":
    main()
