from pydantic import BaseModel

from .camera_pose import CameraPose
from .map_highlight import MapHighlight
from .map_pin import MapPin


class MapLocation(BaseModel):
    """One stop of the fly-through; the location order is the step order."""

    id: str
    name: str
    camera: CameraPose
    pin: MapPin | None = None
    highlights: list[MapHighlight] = []
