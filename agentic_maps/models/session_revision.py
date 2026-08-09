from pydantic import BaseModel


class SessionRevision(BaseModel):
    """The lightweight change probe for a browser session.

    Served by `GET /sessions/{token}/revision` (rest/maps_api.py): the
    `?session=` page polls this a few times per second-range interval and
    only re-fetches the full payload when the number moves. For a
    trip-bound session (open_map_view(trip_id=...)) this is the live
    trip's revision counter — bumped by every recompute; a plain frozen
    session answers a constant 1, so a poller simply never sees a change.
    """

    revision: int
