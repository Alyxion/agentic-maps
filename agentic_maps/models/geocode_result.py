from pydantic import BaseModel

from .geocode_address import GeocodeAddress


class GeocodeResult(BaseModel):
    """One candidate from authoring-time geocoding (Nominatim/OSM)."""

    name: str
    lat: float
    lon: float
    kind: str = ""
    # Structured components (postcode, road, locality...) when the geocoder
    # supplied them; None for offline-index hits that only know a point.
    address: GeocodeAddress | None = None
