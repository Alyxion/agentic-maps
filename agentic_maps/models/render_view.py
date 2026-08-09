from pydantic import BaseModel

from .lat_lon import LatLon


class RenderView(BaseModel):
    """The convenience path into `POST /render`: "just show this point at this zoom".

    An alternative to supplying a full `MapSpec` when the caller only wants a
    plain snapshot — no locations, no routes, no choreography.
    `RenderRequest.resolved_spec()` turns this into a one-stop `MapSpec` whose
    `overview` is exactly this camera.

    `source_id` is still required: imagery/vector tiles cannot be resolved
    without knowing which licensed source to draw from (see
    `sources/presets.py`) — there is no sensible product-wide default because
    coverage is geography-specific.
    """

    center: LatLon
    zoom: float
    source_id: str
    bearing: float = 0.0
    pitch: float = 0.0
