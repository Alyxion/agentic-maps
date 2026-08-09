from pydantic import BaseModel

from .lane_info import LaneInfo
from .lat_lon import LatLon


class RouteStep(BaseModel):
    """One turn-by-turn instruction, as OSRM describes it.

    Kept close to OSRM's own vocabulary rather than pre-rendered into German
    prose: the map may be shown in any of the 24 label languages, so the
    wording belongs in the frontend where the language is known. What is
    stored here is the *fact* — turn left, take the third exit, keep right.
    """

    # OSRM maneuver type: "turn", "roundabout", "exit roundabout", "merge",
    # "fork", "arrive", "depart", …
    type: str
    # "left" / "sharp right" / "straight" / … — absent on depart/arrive.
    modifier: str = ""
    # Which exit to take. Only meaningful on roundabouts, and the single most
    # useful number in the whole instruction.
    exit: int | None = None
    # Street being entered; empty on unnamed service roads.
    name: str = ""
    # Route number(s) — "A 81; A 6" on a German motorway. Motorways usually
    # have NO `name` at all, so a "via …" label built from names alone can
    # only ever pick shared city streets; the ref is what tells two
    # candidate routes apart ("über A81" vs "über A8").
    ref: str = ""
    distance_m: float = 0.0
    duration_s: float = 0.0
    location: LatLon | None = None
    # Lane layout AT this step's maneuver point (OSRM: the step's first
    # intersection belongs to its own maneuver) — what a navigation banner
    # shows while approaching this step. Empty when the backend has none.
    lanes: list[LaneInfo] = []
