from pydantic import BaseModel

from .bbox_deg import BBoxDeg
from .lat_lon import LatLon


class CountryHit(BaseModel):
    """A country matched by name, with the framing a camera needs.

    `names` carries every language the boundary data ships (24 of them), so a
    map shown in Tokyo and one shown in Stuttgart can name the same country
    correctly; `label` is that map resolved for the requested language.

    `bbox` is the mainland extent, not the full territory: framing France on
    French Guiana or the Netherlands on Curaçao would put the audience in the
    Atlantic.
    """

    iso: str
    label: str
    name: str
    names: dict[str, str] = {}
    continent: str
    bbox: BBoxDeg
    center: LatLon
