"""`POST /setup/plan` and `POST /setup/apply` — the REST surface the web
wizard (`web/apps/setup-wizard/`) calls, so the CLI wizard (`setup/wizard.py`)
and the web wizard share exactly one planning brain (`setup/planner.py`)
rather than reimplementing it in JS. Same class-with-`mount(app, *, prefix)`
convention as `MapsApi`.

`/setup/plan` is a cheap review step: it resolves the place/bbox (one live
geocode call) and reports what WOULD happen, without fetching a PBF or
touching disk beyond that. `/setup/apply` does the real work: resolves,
fetches (for the small-enough-for-Overpass case), writes `.env` + compose
files, and — only if `run=true` — invokes `docker compose up -d` exactly the
way the CLI's `--yes` flag does.
"""

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..models.setup_answers import SetupAnswers
from ..models.setup_plan import SetupPlan
from ..models.setup_run_result import SetupRunResult
from ..setup import planner, wizard


class SetupApplyRequest(BaseModel):
    answers: SetupAnswers
    out_dir: str
    # Only invokes `docker compose up -d` when explicitly true — mirrors the
    # CLI wizard's own "ask, don't assume" contract (see `wizard.run_stack`);
    # here the REST caller's `run=true` itself IS the confirmation, there is
    # no interactive prompt to show.
    run: bool = False


class SetupApplyResult(BaseModel):
    plan: SetupPlan
    run: SetupRunResult


class SetupApi:
    def __init__(self, *, default_out_dir: Path | None = None):
        self.default_out_dir = default_out_dir or (Path.cwd() / "agentic-maps-stack")

    def mount(self, app: FastAPI, *, prefix: str = "/setup") -> None:
        @app.post(f"{prefix}/plan", response_model=SetupPlan)
        async def plan(answers: SetupAnswers) -> SetupPlan:
            if answers.mode not in planner.MODES:
                raise HTTPException(status_code=400, detail=f"unknown mode {answers.mode!r}")
            async with httpx.AsyncClient() as client:
                try:
                    if answers.bbox is not None:
                        bbox = answers.bbox
                    else:
                        bbox = await wizard.default_resolve_bbox(answers, client=client)
                except (ValueError, httpx.HTTPError) as error:
                    raise HTTPException(status_code=400, detail=str(error))
            pbf = planner.preview_pbf(bbox, answers)
            return planner.plan_stack(answers, bbox=bbox, pbf=pbf)

        @app.post(f"{prefix}/apply", response_model=SetupApplyResult)
        async def apply(request: SetupApplyRequest) -> SetupApplyResult:
            if request.answers.mode not in planner.MODES:
                raise HTTPException(status_code=400, detail=f"unknown mode {request.answers.mode!r}")
            out_dir = Path(request.out_dir)
            try:
                stack_plan = await wizard.plan_from_answers(request.answers, out_dir=out_dir)
            except (ValueError, httpx.HTTPError) as error:
                raise HTTPException(status_code=400, detail=str(error))
            except wizard.pbf_fetch.PbfFetchError as error:
                raise HTTPException(status_code=502, detail=str(error))
            wizard.write_plan(stack_plan, out_dir)
            if request.run:
                run_result = wizard.run_stack(out_dir, stack_plan, confirm=lambda _prompt: True)
            else:
                run_result = SetupRunResult(
                    started=False,
                    message=f"Files written to {out_dir}. Re-call with run=true, or run "
                            f"`docker compose -f {' -f '.join(stack_plan.compose_files)} up -d` "
                            f"there yourself.",
                )
            return SetupApplyResult(plan=stack_plan, run=run_result)
