from pydantic import BaseModel, Field

from .bbox_deg import BBoxDeg
from .street_way import StreetWay


class StreetSurvey(BaseModel):
    """Every street the basemap knows inside one box.

    Answers the question a geocoder cannot: not "where is Fabriciusstraße"
    but "where does it RUN here" — which is what fitting a plot outline,
    checking a frontage or measuring a setback actually needs.
    """

    bbox: BBoxDeg
    zoom: int = Field(description="Tile zoom the geometry was read at; higher means finer.")
    ways: list[StreetWay] = Field(default_factory=list, description="Segments, longest first.")
    names: list[str] = Field(default_factory=list, description="Distinct street names present, sorted.")
