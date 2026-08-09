from pydantic import BaseModel


class MapPin(BaseModel):
    """The subject marker: a circular photo bubble with a pointer tip.

    `image_url` is any slide-reachable URL (media library proxy once merged);
    without an image the pin renders as a colored dot bubble with the label.
    """

    image_url: str | None = None
    label: str | None = None
    # Size at the location's detail zoom; the runtime scales pins down as the
    # camera zooms out so they never dominate wide views.
    diameter_px: int = 120
    color: str = "#ffffff"
