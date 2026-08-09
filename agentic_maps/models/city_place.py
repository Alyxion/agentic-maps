from pydantic import BaseModel


class CityPlace(BaseModel):
    """One populated place from the offline city index, in lookup form.

    `CityIndex.places()` (Natural Earth) and `PlaceIndex.places()` (GeoNames,
    `geo/geonames.py`) hydrate their sources into these once and cache the
    list — the via-place corridor matcher runs on every routed candidate and
    must not re-walk raw feature dicts each time.
    """

    name: str
    lat: float
    lon: float
    population: int = 0
    # National capital (Natural Earth "Admin-0 capital…" featurecla /
    # GeoNames PPLC).
    capital: bool = False
    # State/province capital ("Admin-1 capital…" / GeoNames PPLA).
    state_capital: bool = False
    # Natural Earth labelrank — lower = more important. GeoNames rows keep
    # the default; nothing GeoNames-fed renders globe labels.
    rank: int = 10
    # ISO 3166-1 alpha-2 country code (GeoNames rows; Natural Earth rows
    # carry the country as a display name in the GeoJSON instead).
    country: str = ""
    # ASCII transliteration when the name needs one ("München" → "Munchen");
    # empty when the name is already ASCII. Lets autocomplete match
    # accent-free typing against local spellings.
    ascii_name: str = ""
