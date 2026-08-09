from pydantic import BaseModel

from .bbox_deg import BBoxDeg
from .pbf_resolution import PbfResolution


class SetupPlan(BaseModel):
    """Everything `setup/wizard.write_plan` needs to materialize a stack.

    `env_content` is the full `.env` file text; `compose_files` names the
    `deploy/docker-compose*.yml` files to layer with `docker compose -f a -f
    b up -d`, in order. The compose YAML itself is NOT regenerated per
    answers — it is the one checked-in `deploy/docker-compose.yml` template,
    parameterised entirely through `.env` (`${AGENTIC_MAPS_...}` /
    `${VALHALLA_...}` / `${NOMINATIM_...}` substitutions), so there is one
    canonical compose file rather than a hand-rolled YAML generator to keep
    in sync with it.
    """

    region_id: str
    bbox: BBoxDeg
    mode: str
    profiles: list[str]
    pbf: PbfResolution
    env_content: str
    compose_files: list[str]
    warnings: list[str] = []
    # service name -> local URL, printed by the wizard once the stack is up.
    ready_urls: dict[str, str] = {}
