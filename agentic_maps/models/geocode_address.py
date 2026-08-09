from pydantic import BaseModel


class GeocodeAddress(BaseModel):
    """Structured address components for a geocode hit.

    Normalized from Nominatim's `address` dict (requested with
    `addressdetails=1`): Nominatim spreads the locality over
    city/town/village/municipality/hamlet depending on settlement size, and a
    detail card wants one field, not five. `postcode` stays exactly as the
    geocoder returns it — postal-code formats are country-specific
    (5 digits in DE, `NL-1234 AB`, UK outcodes...) and reformatting them
    here would only ever make them wrong somewhere.
    """

    road: str = ""
    house_number: str = ""
    postcode: str = ""
    locality: str = ""
    state: str = ""
    country: str = ""
    # ISO 3166-1 alpha-2, lowercase as Nominatim sends it ("de", "fr").
    country_code: str = ""

    def line(self) -> str:
        """One display line: "Im Schwöllbogen 19, 72581 Dettingen an der Erms".

        German-style ordering (street number after road, postcode before
        locality) — matches the primary market; a fully locale-aware
        formatter is deliberately out of scope here.
        """
        street = " ".join(part for part in (self.road, self.house_number) if part)
        place = " ".join(part for part in (self.postcode, self.locality) if part)
        return ", ".join(part for part in (street, place) if part)
