from pydantic import BaseModel, Field

from .geocode_address import GeocodeAddress
from .lat_lon import LatLon


class RouteStop(BaseModel):
    """One named stop of a trip, as an agent describes it.

    Coordinate plus the human label the agent already knows ("HQ",
    "Flughafen Stuttgart") — the label flows into the route's
    `from_location`/`to_location` so a rendered route can name its endpoints
    without a reverse-geocode round trip.

    `address` is the structured detail (road, postcode, locality) the
    geocode pipeline already knows. Filled server-side once at trip/session
    creation (mcp_server/server.py `_enrich_stops`) and cached on the stop;
    `display()` composes the "reasonable details" line every surface shows.
    """

    lat: float = Field(ge=-85.06, le=85.06)
    lon: float = Field(ge=-180.0, le=180.0)
    label: str = ""
    address: GeocodeAddress | None = None

    def latlon(self) -> LatLon:
        return LatLon(lat=self.lat, lon=self.lon)

    def display(self) -> str:
        """Human line with sensible detail, never every field.

        Baseline is locality + ZIP: "Dettingen an der Erms (72581)". The
        street joins only when the stop is street-precise — a bare
        coordinate stop (no label: the road IS its identity) or a
        house-number hit — giving "Frankfurt am Main, Niederräder Ufer
        (60528)". Without any address the label stands alone, and a bare
        coordinate without an address shows the coordinate honestly
        (offline mode must not invent detail).
        """
        if self.address is None:
            return self.label or f"{self.lat:.5f}, {self.lon:.5f}"
        place = self.label or self.address.locality
        street_precise = self.address.road and (not self.label or self.address.house_number)
        if street_precise:
            road = self.address.road
            if self.address.house_number:
                road += f" {self.address.house_number}"
            place = f"{place}, {road}" if place else road
        if self.address.postcode:
            place = f"{place} ({self.address.postcode})" if place else self.address.postcode
        return place or f"{self.lat:.5f}, {self.lon:.5f}"
