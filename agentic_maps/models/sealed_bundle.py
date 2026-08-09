from pydantic import BaseModel

from .sealed_route import SealedRoute


class SealedBundle(BaseModel):
    """Every byte an offline map embed needs, addressed by request path.

    Layout is one concatenated byte string plus an offset index, not a map of
    data: URIs. Both carry the same tiles; the difference is that base64 of one
    large blob costs 33 % once, while thousands of individual data: URIs pay
    that overhead per tile and again in JSON string escaping — for a real
    session the gap is tens of megabytes in a file that is meant to be mailed.

    `index[key] = (offset, length, media_type_id)` into `data`.
    """

    index: dict[str, tuple[int, int, int]] = {}
    media_types: list[str] = []
    # base64 of the concatenated payload; decoded once when the page boots.
    data: str = ""
    routes: list[SealedRoute] = []
    # The map runtime's payload (URL templates already rewritten to the sealed
    # protocol), served in place of GET /maps/spec.
    payload: dict = {}
    attribution: str = ""

    @property
    def byte_size(self) -> int:
        return sum(entry[1] for entry in self.index.values())
