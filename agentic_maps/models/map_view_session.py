from pydantic import BaseModel


class MapViewSession(BaseModel):
    """A stored browser-session spec, addressable by token.

    Minted by `POST /sessions` (rest/maps_api.py): the full map application
    (`web/index.html`) opens `url`, fetches the payload back via
    `GET /sessions/{token}` and mounts the spec with the normal UI — routes,
    alternates, via labels, panels. Sessions expire after `expires_in_s`
    seconds (sweep on store, checked on read).
    """

    token: str
    # Ready-to-open absolute URL against the serving instance's public base.
    url: str
    expires_in_s: int
