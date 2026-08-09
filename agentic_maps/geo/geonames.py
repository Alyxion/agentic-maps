"""Dense offline populated-places index — GeoNames `cities15000`.

Natural Earth's populated places (`CityIndex`, geo/countries.py) carry
~7,300 cities worldwide and only 58 German ones — enough for globe labels,
far too sparse for "via Pforzheim" route labels or small-town autocomplete
(Pforzheim and Heilbronn, 120k inhabitants each, are simply not in it).
This loads the GeoNames-derived asset `tools/make_city_index.py` emits
(~32k places worldwide, population ≥ 15k, local spellings + ASCII
transliterations) when it is on disk. Every consumer falls back to the
Natural Earth index when it is not, so existing installs lose nothing and
there is no runtime download here — densifying is an authoring-time step.

Provenance & licence (see also the prep tool's docstring):
GeoNames geographical database, https://www.geonames.org — dump
`cities15000.zip`, licensed **CC BY 4.0**
(https://creativecommons.org/licenses/by/4.0/ per the dump's readme.txt).
Attribution is REQUIRED wherever the data is shown or redistributed —
`GEONAMES_ATTRIBUTION` below is what the REST payloads, exported pages and
package manifests carry (docs/imagery-coverage.md §1).
"""

import csv
import gzip
import math
import unicodedata
from pathlib import Path

from ..models.city_place import CityPlace
from .via_places import bucket_places

GEONAMES_SOURCE_URL = "https://download.geonames.org/export/dump/cities15000.zip"
GEONAMES_ATTRIBUTION = "GeoNames (geonames.org)"
GEONAMES_LICENSE = (
    "GeoNames cities15000 — CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/"
)

# Asset columns (tab-separated; written by tools/make_city_index.py).
_NAME, _ASCII, _LAT, _LON, _POPULATION, _COUNTRY, _FLAG = range(7)


class PlaceIndex:
    """Loads the compact GeoNames asset; answers search and via-place lookups.

    Mirrors `CityIndex`'s consumer-facing surface (`available`, `places()`,
    `search()`) so `rest/maps_api.py` can prefer this index and fall back
    seamlessly. It deliberately has no `collection()`: the globe's label
    layer stays on Natural Earth — 32k labels at globe altitude would be
    noise, and the GeoJSON payload would quadruple for nothing.
    """

    def __init__(self, path: Path):
        self.path = path
        self._places: list[CityPlace] | None = None
        self._buckets: dict[tuple[int, int], list[CityPlace]] | None = None
        # (casefolded spelling variants, row) — prefix search runs over this
        # flat table instead of touching model attributes per query.
        self._search_rows: list[tuple[tuple[str, ...], CityPlace]] | None = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    def places(self) -> list[CityPlace]:
        """The asset as typed rows, hydrated once (same contract as
        `CityIndex.places()` — the corridor matcher runs per routed
        candidate and must not re-parse the TSV)."""
        if self._places is None:
            places: list[CityPlace] = []
            with gzip.open(self.path, "rt", encoding="utf-8", newline="") as handle:
                reader = csv.reader(
                    (line for line in handle if not line.startswith("#")),
                    delimiter="\t", quoting=csv.QUOTE_NONE,
                )
                for row in reader:
                    places.append(CityPlace(
                        name=row[_NAME],
                        ascii_name=row[_ASCII],
                        lat=float(row[_LAT]), lon=float(row[_LON]),
                        population=int(row[_POPULATION] or 0),
                        country=row[_COUNTRY],
                        capital=row[_FLAG] == "C",
                        state_capital=row[_FLAG] == "A",
                    ))
            self._places = places
        return self._places

    def buckets(self) -> dict[tuple[int, int], list[CityPlace]]:
        """Cached `bucket_places` grid — hands the corridor matcher its
        inverted coarse reject (see geo/via_places.py)."""
        if self._buckets is None:
            self._buckets = bucket_places(self.places())
        return self._buckets

    def search(
        self,
        query: str,
        *,
        limit: int = 6,
        near: tuple[float, float] | None = None,
        home_iso: str = "",
    ) -> list[dict]:
        """Prefix search, scored exactly like `CityIndex.search` (see
        docs/features.md §2): population + proximity + home country. Two
        upgrades the denser source affords: names are LOCAL spellings
        ("Hannover", not the exonym "Hanover"), and the ASCII column matches
        accent-free typing ("munch" finds München). `home_iso` is the ISO
        alpha-2 code (GeoNames keys countries by code, not display name);
        the result's `country` field carries that code — the REST layer maps
        it to a display name.
        """
        needle = query.strip().casefold()
        if not needle:
            return []
        if self._search_rows is None:
            # Three spellings per row: local name ("Köln"), GeoNames' ASCII
            # transliteration ("Koeln"), and a diacritic-stripped fold
            # ("Koln") — whichever the user types, the prefix lands.
            rows = []
            for place in self.places():
                name_cf = place.name.casefold()
                ascii_cf = place.ascii_name.casefold()
                folded = unicodedata.normalize("NFD", name_cf)
                folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
                variants = tuple({v for v in (name_cf, ascii_cf, folded) if v})
                rows.append((variants, place))
            self._search_rows = rows
        home = home_iso.strip().upper()
        scored = []
        for variants, place in self._search_rows:
            # Plain loop, not any(generator): this line runs ~40k times per
            # keystroke and the generator frame doubles the latency.
            for variant in variants:
                if variant.startswith(needle):
                    break
            else:
                continue
            # CityIndex.search's formula, with one dense-index correction:
            # the proximity boost is scaled by city size. At Natural Earth
            # density every indexed place was major and raw proximity was
            # safe; at GeoNames density, unscaled proximity lets 57k Hameln
            # bury 1.8M Hamburg on "ha" typed in Hannover. The scale factor
            # reaches 1.0 at ~1M inhabitants, so major-city ranking is
            # unchanged and small towns win on proximity only against peers.
            score = math.log10(place.population + 1) / 7.0
            distance_km = None
            if near is not None:
                mean = math.radians((near[0] + place.lat) / 2.0)
                dx = math.radians(place.lon - near[1]) * math.cos(mean)
                dy = math.radians(place.lat - near[0])
                distance_km = math.hypot(dx, dy) * 6371.0
                size_factor = min(1.0, math.log10(place.population + 1) / 6.0)
                score += 1.8 * math.exp(-distance_km / 400.0) * size_factor
            if home and place.country == home:
                score += 1.6
            scored.append((score, {
                "name": place.name,
                "country": place.country,
                "lat": place.lat,
                "lon": place.lon,
                "population": place.population,
                "distance_km": round(distance_km, 1) if distance_km is not None else None,
                "score": round(score, 3),
            }))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _, entry in scored[:limit]]
