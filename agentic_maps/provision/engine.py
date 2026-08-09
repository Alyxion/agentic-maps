"""The job engine behind `POST /provision`: async, in-process, resumable.

A job = one `ProvisionRequest` (region × layers) executed layer by layer:

- `maps`   → one `pmtiles extract` through the existing `VectorExtractor`
  (the exact machinery `/vector/extract` uses — no second extract path);
- `aerial` → the region's tile pyramid pulled tile-by-tile through
  `MapsApi.fetch_aerial_band_tile`, i.e. the SAME band dispatch and
  cache-through bundles the live `/aerial` endpoint serves from. That is
  what makes jobs resumable for free: a tile already in the live bundle is
  answered from disk without an upstream request, so re-POSTing an
  interrupted job only pays for what is still missing;
- `routing`→ the region PBF (Geofabrik for presets, the Overpass cut for a
  small custom bbox) downloaded — with HTTP-Range resume — into the compose
  stack's `valhalla_data/` (+ hardlinked into `nominatim_data/`), the stack
  files written via the setup planner when absent, and `docker compose up
  -d` run ONLY when the request opted in and docker exists; otherwise the
  job reports the exact next command instead of failing.

State: one JSON file per job under the engine's state dir (inside
`var/bundles/.provision` by default). A job found in state "running" at
engine construction time was cut off by a restart — it is re-labelled
"interrupted" with a note saying it is resumable, never silently dropped.

Progress is honest counts (tiles done/total, bytes) and no ETA: upstream
tile servers owe us no steady rate, and a made-up finish time would be the
one number everyone remembers.
"""

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ..models.bbox_deg import BBoxDeg
from ..models.provision_job import ProvisionJob
from ..models.provision_layer_result import ProvisionLayerResult
from ..models.provision_request import ProvisionRequest
from ..models.setup_answers import SetupAnswers
from ..models.tile_coord import TileCoord
from ..setup import planner as setup_planner
from ..setup.pbf_fetch import PbfFetchError, fetch_city_pbf
from .estimates import (
    REGION_PRESETS,
    SEN2_COVERAGE,
    WORLD_MAX_ZOOM,
    aerial_zooms,
    estimate_region,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from ..rest.maps_api import MapsApi

# Aerial workers per job. The live tile path already serializes upstream
# fetches through MapsApi's own LIVE_TILE_CONCURRENCY semaphore (shared with
# any browsing session), so this only bounds how much of that budget one
# bulk job may occupy.
AERIAL_WORKERS = 4
# Per-tile politeness pause after a fetch that actually went upstream (the
# federation presets declare 100 ms request_delay_ms; the live path does not
# apply it itself). Cached tiles skip the pause — a resumed job must not
# crawl through 50k already-downloaded tiles at 10/s.
AERIAL_DELAY_S = 0.1
# A fetch answered faster than this never left this machine (WMS round
# trips run 100 ms+); used to tell cached from fetched for the pause above.
_CACHED_THRESHOLD_S = 0.05
# Persist cadence during the tile loop.
_PERSIST_EVERY_TILES = 50

_STACK_DEFAULT_DIR = "agentic-maps-stack"


class ProvisionEngine:
    def __init__(self, api: "MapsApi", state_dir: Path):
        self.api = api
        self.state_dir = state_dir
        self._jobs: dict[str, ProvisionJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._load_persisted()

    # -- persistence -------------------------------------------------------

    def _job_path(self, job_id: str) -> Path:
        return self.state_dir / f"{job_id}.json"

    def _persist(self, job: ProvisionJob) -> None:
        job.updated_at = time.time()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self._job_path(job.id)
        staging = path.with_suffix(".json.part")
        staging.write_text(job.model_dump_json(indent=2))
        staging.replace(path)

    def _load_persisted(self) -> None:
        if not self.state_dir.exists():
            return
        for path in sorted(self.state_dir.glob("provision-*.json")):
            try:
                job = ProvisionJob.model_validate_json(path.read_text())
            except ValueError:
                continue  # a half-written file from a hard kill; ignore
            if job.state in ("running", "pending"):
                job.state = "interrupted"
                job.note = (
                    "server restarted while this job ran — resumable: POST the "
                    "same request again; tiles/bytes already on disk are skipped.")
                self._persist(job)
            self._jobs[job.id] = job

    # -- public API ----------------------------------------------------------

    def start(self, request: ProvisionRequest) -> ProvisionJob:
        """Validate via the estimator (same refusals), mint the job, run it."""
        estimate_region(request)  # raises ValueError on unknown region/layer
        job = ProvisionJob(
            id="provision-" + uuid.uuid4().hex[:12],
            request=request,
            state="running",
            created_at=time.time(),
            layers=[ProvisionLayerResult(layer=layer) for layer in request.layers],
        )
        self._jobs[job.id] = job
        self._cancels[job.id] = asyncio.Event()
        self._persist(job)
        self._tasks[job.id] = asyncio.get_running_loop().create_task(self._run(job))
        return job

    def get(self, job_id: str) -> ProvisionJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[ProvisionJob]:
        return sorted(self._jobs.values(), key=lambda j: -j.created_at)

    def running(self, job_id: str) -> bool:
        """Whether the job's task is still executing in this process."""
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    def cancel(self, job_id: str) -> ProvisionJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        event = self._cancels.get(job_id)
        if event is not None:
            event.set()
        if job.state in ("pending", "running"):
            job.state = "cancelled"
            job.note = "cancelled — resumable: POST the same request again."
            self._persist(job)
        return job

    # -- job execution -------------------------------------------------------

    def _region(self, request: ProvisionRequest) -> tuple[str, BBoxDeg]:
        if request.bbox is not None:
            return (request.region_id or "custom", request.bbox)
        preset = REGION_PRESETS[request.region]
        return (preset.id, preset.bbox)

    def _cancelled(self, job: ProvisionJob) -> bool:
        event = self._cancels.get(job.id)
        return event is not None and event.is_set()

    async def _run(self, job: ProvisionJob) -> None:
        runners = {"maps": self._run_maps, "aerial": self._run_aerial,
                   "routing": self._run_routing}
        failed = False
        try:
            for result in job.layers:
                if self._cancelled(job):
                    result.status = "cancelled"
                    continue
                result.status = "running"
                self._persist(job)
                try:
                    await runners[result.layer](job, result)
                except asyncio.CancelledError:
                    result.status = "cancelled"
                    raise
                except Exception as error:  # noqa: BLE001 - one layer failing is a job fact
                    result.status = "failed"
                    result.detail = f"{type(error).__name__}: {error}"
                    failed = True
                else:
                    if result.status == "running":
                        result.status = "cancelled" if self._cancelled(job) else "done"
            if self._cancelled(job):
                job.state = "cancelled"
                job.note = "cancelled — resumable: POST the same request again."
            else:
                job.state = "failed" if failed else "done"
                if failed:
                    job.error = "; ".join(
                        f"{r.layer}: {r.detail}" for r in job.layers if r.status == "failed")
            job.phase = "finished" if job.state == "done" else job.state
        except asyncio.CancelledError:
            job.state = "cancelled"
            job.phase = "cancelled"
        finally:
            self._persist(job)

    # -- maps (vector extract) ------------------------------------------------

    async def _run_maps(self, job: ProvisionJob, result: ProvisionLayerResult) -> None:
        region, bbox = self._region(job.request)
        job.phase = f"maps: pmtiles extract for {region} (z0-15)"
        self._persist(job)
        path = await self.api.extractor.extract(
            region, west=bbox.west, south=bbox.south,
            east=bbox.east, north=bbox.north, maxzoom=15,
        )
        result.bytes_done = path.stat().st_size
        result.detail = f"{path.name} ({result.bytes_done:,} bytes)"
        # Register the fresh extract with the running server immediately —
        # the same wiring /vector/extract does after minting.
        self.api.refresh_vector_bundle(path)

    # -- aerial (band-dispatched tile pyramid) --------------------------------

    def _aerial_tiles(self, request: ProvisionRequest) -> list[tuple[str, TileCoord]]:
        """(source_id, coord) pairs, shallow zooms first — exactly the bands
        the `/aerial` dispatcher serves (world ladder for z<=8, the 20 cm
        federation composite above)."""
        region, bbox = self._region(request)
        world = self.api._world_source()
        world_id = world.id if world else "blue-marble"
        pairs: list[tuple[str, TileCoord]] = []
        for zoom in aerial_zooms(region if request.bbox is None else "", request.aerial_max_zoom):
            if request.region == "earth":
                if zoom <= WORLD_MAX_ZOOM:
                    n = 1 << zoom
                    pairs.extend(
                        (world_id, TileCoord(z=zoom, x=x, y=y))
                        for x in range(n) for y in range(n))
                    continue
                box = SEN2_COVERAGE
            else:
                box = bbox
            nw = TileCoord.at(box.north, box.west, zoom)
            se = TileCoord.at(box.south, box.east, zoom)
            source_id = world_id if zoom <= WORLD_MAX_ZOOM else "de-dop"
            pairs.extend(
                (source_id, TileCoord(z=zoom, x=x, y=y))
                for x in range(nw.x, se.x + 1)
                for y in range(nw.y, se.y + 1))
        return pairs

    async def _run_aerial(self, job: ProvisionJob, result: ProvisionLayerResult) -> None:
        pairs = self._aerial_tiles(job.request)
        result.tiles_total = len(pairs)
        queue: asyncio.Queue[tuple[str, TileCoord]] = asyncio.Queue()
        for pair in pairs:
            queue.put_nowait(pair)
        lock = asyncio.Lock()

        async def worker() -> None:
            while not self._cancelled(job):
                try:
                    source_id, coord = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started = time.monotonic()
                try:
                    data = await self.api.fetch_aerial_band_tile(source_id, coord)
                except Exception:  # noqa: BLE001 - a failed tile is a counted outcome
                    data = None
                    async with lock:
                        result.tiles_failed += 1
                fetched_upstream = time.monotonic() - started > _CACHED_THRESHOLD_S
                async with lock:
                    result.tiles_done += 1
                    result.bytes_done += len(data or b"")
                    if result.tiles_done % _PERSIST_EVERY_TILES == 0:
                        job.phase = (f"aerial z{coord.z}: {result.tiles_done}/"
                                     f"{result.tiles_total} tiles")
                        self._persist(job)
                if fetched_upstream:
                    await asyncio.sleep(AERIAL_DELAY_S)

        job.phase = f"aerial: 0/{result.tiles_total} tiles"
        self._persist(job)
        await asyncio.gather(*(worker() for _ in range(AERIAL_WORKERS)))
        result.detail = (
            f"{result.tiles_done}/{result.tiles_total} tiles, "
            f"{result.tiles_failed} failed, {result.bytes_done:,} bytes")

    # -- routing (PBF into the compose stack) ---------------------------------

    def _stack_dir(self, request: ProvisionRequest) -> Path:
        raw = request.routing_stack_dir.strip()
        return Path(raw) if raw else Path.cwd() / _STACK_DEFAULT_DIR

    async def _resolve_pbf(
        self, job: ProvisionJob, result: ProvisionLayerResult, target_dir: Path,
    ) -> Path:
        """The region PBF on local disk, whatever its source.

        Presets download their Geofabrik extract (Range-resumable); a custom
        bbox uses an explicit `pbf_url` or, when small enough, the Overpass
        cut the setup wizard already knows how to make.
        """
        request = job.request
        region, bbox = self._region(request)
        if request.bbox is None:
            url = REGION_PRESETS[request.region].geofabrik_url
            return await self._download_pbf(job, result, url, target_dir / f"{region}.osm.pbf")
        if request.pbf_url.strip():
            return await self._download_pbf(
                job, result, request.pbf_url.strip(), target_dir / f"{region}.osm.pbf")
        if bbox.area_deg2 > setup_planner.OVERPASS_MAX_AREA_DEG2:
            raise PbfFetchError(
                f"custom bbox is {bbox.area_deg2:.3f} sq deg — past the Overpass "
                f"export cap ({setup_planner.OVERPASS_MAX_AREA_DEG2:.3f}); supply a "
                "Geofabrik regional/country PBF via `pbf_url`")
        job.phase = "routing: Overpass export + osmium conversion"
        self._persist(job)
        path = await fetch_city_pbf(region, bbox, target_dir)
        result.bytes_done += path.stat().st_size
        return path

    async def _download_pbf(
        self, job: ProvisionJob, result: ProvisionLayerResult, url: str, target: Path,
    ) -> Path:
        """Streaming download with HTTP-Range resume — a 30 GB Europe PBF
        interrupted at 90% must not start over."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            result.detail = f"{target.name} already present ({target.stat().st_size:,} bytes)"
            result.bytes_done += target.stat().st_size
            return target
        staging = target.with_suffix(target.suffix + ".part")
        position = staging.stat().st_size if staging.exists() else 0
        headers = {"Range": f"bytes={position}-"} if position else {}
        job.phase = f"routing: downloading {url.rsplit('/', 1)[-1]}"
        self._persist(job)
        client = await self.api._live_client()
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=True,
            timeout=httpx.Timeout(600.0, connect=20.0),
        ) as response:
            if position and response.status_code != 206:
                position = 0                      # server ignored the range
            response.raise_for_status()
            mode = "ab" if position else "wb"
            result.bytes_done += position
            last_persist = time.monotonic()
            with open(staging, mode) as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    if self._cancelled(job):
                        raise asyncio.CancelledError()
                    handle.write(chunk)
                    result.bytes_done += len(chunk)
                    if time.monotonic() - last_persist > 3.0:
                        job.phase = (f"routing: {result.bytes_done:,} bytes of "
                                     f"{url.rsplit('/', 1)[-1]}")
                        self._persist(job)
                        last_persist = time.monotonic()
        staging.replace(target)
        return target

    def _write_stack_files(self, stack_dir: Path, pbf_path: Path, region: str,
                           bbox: BBoxDeg) -> None:
        """`.env` + compose templates via the setup planner — never a second
        YAML generator. write_plan's own PBF copy is skipped on purpose (it
        buffers whole files in memory; a Europe PBF would not survive that),
        so the plan is built with the container path and the bytes are
        hardlinked into place by the caller."""
        # Deferred import: setup.wizard imports rest.maps_api for the shared
        # Nominatim env-var constants, and this module is imported BY
        # rest.maps_api — module-level would be a cycle.
        from ..setup import wizard as setup_wizard

        answers = SetupAnswers(
            place=region, region_id=region, mode="mixed",
            pbf_path=str(pbf_path),
        )
        pbf = setup_planner.user_pbf_resolution(answers, bbox)
        plan = setup_planner.plan_stack(answers, bbox=bbox, pbf=pbf)
        stack_dir.mkdir(parents=True, exist_ok=True)
        env_path = stack_dir / ".env"
        if not env_path.exists():
            env_content = plan.env_content.rstrip("\n") + (
                f'\nAGENTIC_MAPS_REPO_ROOT="{Path(__file__).resolve().parents[2]}"\n')
            env_path.write_text(env_content)
        for name in setup_wizard._TEMPLATE_FILES:
            source = setup_wizard._DEPLOY_DIR / name
            if source.exists() and not (stack_dir / name).exists():
                (stack_dir / name).write_text(source.read_text())

    async def _run_routing(self, job: ProvisionJob, result: ProvisionLayerResult) -> None:
        from ..setup import wizard as setup_wizard  # deferred: see _write_stack_files

        region, bbox = self._region(job.request)
        stack_dir = self._stack_dir(job.request)
        valhalla_dir = stack_dir / "valhalla_data"
        pbf_path = await self._resolve_pbf(job, result, valhalla_dir)
        # Nominatim reads its copy from its own mount; hardlink (fall back to
        # copy across filesystems) so 30 GB never exists twice.
        nominatim_dir = stack_dir / "nominatim_data"
        nominatim_dir.mkdir(parents=True, exist_ok=True)
        nominatim_pbf = nominatim_dir / pbf_path.name
        if not nominatim_pbf.exists():
            try:
                os.link(pbf_path, nominatim_pbf)
            except OSError:
                shutil.copyfile(pbf_path, nominatim_pbf)
        self._write_stack_files(stack_dir, pbf_path, region, bbox)

        compose_command = f"docker compose -f docker-compose.yml up -d   (in {stack_dir})"
        build_note = (
            "first boot builds the Valhalla graph from this PBF — tens of "
            "minutes for Germany, MANY HOURS for Europe.")
        if job.request.start_stack and setup_wizard.docker_available():
            answers = SetupAnswers(
                place=region, region_id=region, mode="mixed", pbf_path=str(pbf_path))
            job.phase = "routing: docker compose up -d"
            self._persist(job)
            plan = setup_planner.plan_stack(
                answers, bbox=bbox,
                pbf=setup_planner.user_pbf_resolution(answers, bbox))
            # In a thread: `docker compose up -d` blocks for seconds and the
            # event loop keeps serving tiles meanwhile. One quick poll pass —
            # the graph build takes far longer than any sane poll window; the
            # job reports "started", not "ready".
            run = await asyncio.to_thread(
                setup_wizard.run_stack, stack_dir, plan,
                confirm=lambda _prompt: True,
                poll_attempts=1, poll_interval_s=1.0,
            )
            result.detail = f"PBF staged at {pbf_path}; {run.message} {build_note}"
        else:
            result.next_command = compose_command
            reason = ("start_stack not requested" if not job.request.start_stack
                      else "docker not found on PATH")
            result.detail = (
                f"PBF staged at {pbf_path} ({reason}) — next: {compose_command}. "
                + build_note)
