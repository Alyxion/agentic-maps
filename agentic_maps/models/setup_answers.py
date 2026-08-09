from pydantic import BaseModel

from .bbox_deg import BBoxDeg


class SetupAnswers(BaseModel):
    """What the CLI wizard and the web wizard both collect before planning.

    The two front-ends (`setup/wizard.py`'s prompts, `web/apps/setup-wizard/`'s
    form) fill this in different ways but hand the identical shape to
    `setup/planner.py` — one planning brain, two ways to reach it.
    """

    # Free-text place name ("Hannover", "Hannover, Germany"). Resolved to a
    # bbox via the offline CityIndex first, the geocoder second (see
    # `wizard.default_resolve_bbox`). Ignored when `bbox` is set directly.
    place: str = ""
    # Explicit override — wins over `place` unconditionally. What the web
    # wizard sends when the user dragged a box on the map instead of typing
    # a name.
    bbox: BBoxDeg | None = None
    # "offline" | "mixed" | "online" — validated in planner.py rather than
    # typed as Literal here, so a bad value from the web form's JSON body
    # produces the planner's own clear error instead of a generic pydantic
    # one two layers removed from where it matters.
    mode: str = "mixed"
    # Subset of the canonical car/truck/walk/bike vocabulary
    # (routing/base.py's TravelMode). Valhalla builds one graph with every
    # costing model regardless — this only decides what the API/UI *offers*,
    # see planner.py's module docstring for why the graph build itself is
    # unaffected.
    profiles: list[str] = ["car"]
    # Slug for filenames/volumes/service data dirs. Derived from `place` via
    # `planner.slugify` when left blank.
    region_id: str = ""
    # Explicit PBF source overrides. At most one should be set; `pbf_url`
    # wins if both are (see `planner.choose_pbf_strategy`). Anything beyond
    # a small town/city needs one of these — see docs/setup-guide.md's
    # honesty section on why no fully-automatic city-scoped PBF service
    # exists.
    pbf_url: str = ""
    pbf_path: str = ""
