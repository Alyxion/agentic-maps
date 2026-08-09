from pydantic import BaseModel, Field

from .lat_lon import LatLon


class SitePlanArea(BaseModel):
    """One drawn area of a site plan, in real coordinates."""

    id: str = Field(default="", description="Stable id; the host page uses it to drive step reveals.")
    label: str = Field(default="", description="Headline inside the area.")
    note: str = Field(default="", description="Second line under the label (area, height, …).")
    ring: list[LatLon] = Field(description="Outline, in order; closed implicitly. "
                                           "For role=road this is the CENTRE LINE, not an outline.")
    role: str = Field(default="building",
                      description="building | yard | road — decides the drawn style.")
    width_m: float = Field(default=0.0,
                           description="role=road only: carriageway width. A driveway is one "
                                       "stroked line, not a row of rectangles — drawn as boxes "
                                       "it showed a seam at every bend.")


class SitePlanStreet(BaseModel):
    """A public street, drawn at its real width as the plan's skeleton."""

    name: str = ""
    width_m: float = Field(default=12.0, description="Carriageway plus verge, metres.")
    geometry: list[LatLon]


class SitePlan(BaseModel):
    """Everything a stylised, georeferenced site plan needs.

    Deliberately geometry-only: no colours, no sizes, no fonts. The look lives
    in `siteplan.css`, so one stylesheet governs every plan the platform draws
    instead of each host page growing its own copy.
    """

    plot: list[LatLon] = Field(description="Site boundary — the drawing is framed on it.")
    areas: list[SitePlanArea] = Field(default_factory=list)
    streets: list[SitePlanStreet] = Field(default_factory=list)
    width_px: float = 1180.0
    height_px: float = 634.0
    margin_m: float = Field(default=26.0, description="Breathing space around the plot.")
    scale_bar_m: float = Field(default=50.0, description="Length of the drawn scale bar.")
    north: bool = True
