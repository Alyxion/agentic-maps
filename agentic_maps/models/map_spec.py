from pydantic import BaseModel, Field

from .camera_pose import CameraPose
from .map_location import MapLocation
from .map_route import MapRoute
from .raster_adjust import RasterAdjust


class MapSpec(BaseModel):
    """Everything a map slide needs: source, camera stops, decorations.

    This is the JSON embedded in a `[data-agentic-map]` element and the
    input to harvest planning.
    """

    id: str
    title: str = ""
    source_id: str
    overview: CameraPose | None = None
    # May be empty: a plain map (search/route/browse) is a valid spec with no
    # choreography, which is what the map app starts from.
    locations: list[MapLocation] = Field(default_factory=list)
    routes: list[MapRoute] = []
    imagery: RasterAdjust = RasterAdjust()
    fly_duration_ms: int = 2600
    # Allow viewers to pan/zoom the map INSIDE the presentation. Off by
    # default; when on, offline packages rely on the fallback ladder for tiles
    # beyond the precached choreography.
    interactive: bool = False
