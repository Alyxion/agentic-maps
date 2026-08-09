from pydantic import BaseModel


class LaneInfo(BaseModel):
    """One lane at a maneuver point, as OSRM describes it.

    `indications` uses OSRM's own vocabulary ("left", "slight left",
    "straight", "right", "uturn", "none", ...) — a lane can carry several
    (a straight-or-right lane). `valid` says whether taking this lane
    executes the step's maneuver; the lane bar renders valid lanes bold and
    the rest dimmed, exactly Google's lane guidance row.
    """

    indications: list[str] = []
    valid: bool = False
