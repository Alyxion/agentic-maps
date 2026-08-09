"""Worldwide country boundaries — always available, online or sealed.

The vector basemap is regional by construction (see docs/concept.md §5), so
outside an extract there would be no borders and no country labels at all. This
carries them separately: one small public-domain Natural Earth file that ships
with every package, so "where is this place in the world" works at any zoom,
anywhere, offline.

Public domain (Natural Earth), no attribution required — we credit it anyway.
"""

import json
import math
from pathlib import Path

import httpx

from ..models.bbox_deg import BBoxDeg
from ..models.country_hit import CountryHit
from ..models.lat_lon import LatLon

SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_admin_0_countries.geojson"
)
ATTRIBUTION = "Natural Earth (public domain)"

# Every language the boundary data ships a name in. Cities/streets get the same
# treatment through the vector basemap's `lang` option and through Nominatim's
# accept-language, so a published map reads correctly wherever it is shown.
LANGUAGES = (
    "ar", "bn", "de", "el", "en", "es", "fa", "fr", "he", "hi", "hu", "id",
    "it", "ja", "ko", "nl", "pl", "pt", "ru", "sv", "tr", "uk", "ur", "vi",
    "zh",
)
DEFAULT_LANGUAGE = "de"


def _names(properties: dict) -> dict[str, str]:
    """lang -> country name, from Natural Earth's NAME_XX columns."""
    names = {}
    for lang in LANGUAGES:
        value = properties.get(f"NAME_{lang.upper()}")
        if value:
            names[lang] = value
    if "en" not in names and properties.get("NAME"):
        names["en"] = properties["NAME"]
    return names


def _rings(geometry: dict) -> list[list]:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"][0]]
    if geometry["type"] == "MultiPolygon":
        return [polygon[0] for polygon in geometry["coordinates"]]
    return []


def _bbox(ring: list) -> tuple[float, float, float, float]:
    lons = [point[0] for point in ring]
    lats = [point[1] for point in ring]
    return min(lons), min(lats), max(lons), max(lats)


# 10m, not 50m: the coarse set carries ~1200 places worldwide and is missing
# entire state capitals (Hannover is not in it) — useless for autocomplete.
# The 10m set has ~7300, including the mid-size cities people actually type.
CITY_SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_populated_places_simple.geojson"
)


PHYSICAL_SOURCES = {
    # Natural Earth physical layers, public domain. Without them the globe's
    # map mode is bare land and borders — no seas inland, no rivers, none of
    # the texture that makes a small-scale map readable.
    "lakes": "ne_50m_lakes",
    "rivers": "ne_50m_rivers_lake_centerlines",
    # Urban areas stand in for the road network at globe scale: the 10 m roads
    # file is 50 MB, and at this distance motorways are sub-pixel anyway. Real
    # roads arrive with the vector basemap the moment the flat map takes over.
    "urban": "ne_50m_urban_areas",
    # Admin-1 at 50 m exists only for large federal countries — US, Canada,
    # Brazil, Russia, China, India, Australia — which is exactly the set that
    # should show state lines on a globe. Germany's Länder would be noise here.
    "states": "ne_50m_admin_1_states_provinces_lines",
}


class PhysicalIndex:
    """Lakes, rivers and major roads for the small-scale (globe) cartography."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._collection: dict | None = None

    @property
    def available(self) -> bool:
        return all((self.directory / f"{name}.geojson").exists() for name in PHYSICAL_SOURCES)

    async def ensure(self, client: httpx.AsyncClient | None = None) -> None:
        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            for name, source in PHYSICAL_SOURCES.items():
                path = self.directory / f"{name}.geojson"
                if path.exists():
                    continue
                url = (
                    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
                    f"master/geojson/{source}.geojson"
                )
                response = await client.get(url, timeout=180.0, follow_redirects=True)
                response.raise_for_status()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(response.content)
        finally:
            if owns_client:
                await client.aclose()

    def collection(self) -> dict:
        """All three layers in one payload, tagged by `layer`."""
        if self._collection is None:
            features = []
            for name in PHYSICAL_SOURCES:
                path = self.directory / f"{name}.geojson"
                if not path.exists():
                    continue
                for feature in json.loads(path.read_text())["features"]:
                    properties = feature.get("properties", {})
                    features.append({
                        "type": "Feature",
                        "properties": {
                            "layer": name,
                            # Roads carry a scalerank; low = important.
                            "rank": properties.get("scalerank", 10),
                        },
                        "geometry": feature["geometry"],
                    })
            self._collection = {"type": "FeatureCollection", "features": features}
        return self._collection


def _title(text: str) -> str:
    """Normalise the source's inconsistent casing without touching real names."""
    return text.title() if text.isupper() else text


MARINE_SOURCE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_geography_marine_polys.geojson"
)


class OceanIndex:
    """Ocean and sea names for the globe, as label points.

    Natural Earth's marine polygons (public domain) carry the name and a
    scalerank; only the point matters here, because the water itself is
    already drawn by the texture. Google labels the open ocean the same way —
    it is what stops the blue half of the globe from reading as empty.
    """

    def __init__(self, path: Path, *, source_url: str = MARINE_SOURCE_URL):
        self.path = path
        self.source_url = source_url
        self._collection: dict | None = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    async def ensure(self, client: httpx.AsyncClient | None = None) -> Path:
        if self.path.exists():
            return self.path
        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            response = await client.get(self.source_url, timeout=180.0, follow_redirects=True)
            response.raise_for_status()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(response.content)
        finally:
            if owns_client:
                await client.aclose()
        return self.path

    def collection(self) -> dict:
        if self._collection is None:
            features = []
            for feature in json.loads(self.path.read_text())["features"]:
                properties = feature.get("properties", {})
                name = properties.get("name")
                if not name:
                    continue
                point = _label_point(feature["geometry"])
                if point is None:
                    continue
                names = {
                    key: _title(value)
                    for key, value in properties.items()
                    if key.startswith("name_") and value
                }
                features.append({
                    "type": "Feature",
                    "properties": {
                        # Natural Earth shouts some of them ("INDIAN OCEAN");
                        # an atlas does not, and neither does Google.
                        "name": _title(name),
                        **names,
                        # "ocean" for the five great ones, then sea/gulf/bay/…
                        "kind": properties.get("featurecla", "sea"),
                        # Low = shown first. Natural Earth ranks the oceans 0-1
                        # and works down to bays around 5-6.
                        "rank": properties.get("scalerank", 5),
                    },
                    "geometry": {"type": "Point", "coordinates": list(point)},
                })
            self._collection = {"type": "FeatureCollection", "features": features}
        return self._collection


def _label_point(geometry: dict) -> tuple[float, float] | None:
    """Where a sea's name belongs: a point well inside its biggest part.

    Averaging the ring's vertices is not good enough — ocean polygons are
    deeply concave, and the average of the North Pacific's outline lands in
    the Gulf of California. So: take the area-weighted centroid, and when that
    falls outside the water (which is exactly what happens to the concave
    ones), search a coarse grid for the interior point furthest from any
    coastline. That is the "pole of inaccessibility", and it is where an atlas
    puts the word.
    """
    rings: list[list] = []
    kind = geometry.get("type")
    if kind == "Polygon":
        rings = geometry["coordinates"][:1]
    elif kind == "MultiPolygon":
        rings = [polygon[0] for polygon in geometry["coordinates"] if polygon]
    if not rings:
        return None
    ring = max(rings, key=lambda candidate: abs(_ring_area(candidate)))
    if len(ring) < 4:
        return None

    centroid = _ring_centroid(ring)
    if centroid is not None and _contains(ring, *centroid):
        return centroid
    return _pole_of_inaccessibility(ring)


def _ring_area(ring: list) -> float:
    """Shoelace area in square degrees — only used to compare and weight rings."""
    total = 0.0
    for index in range(len(ring) - 1):
        x0, y0 = ring[index][0], ring[index][1]
        x1, y1 = ring[index + 1][0], ring[index + 1][1]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _ring_centroid(ring: list) -> tuple[float, float] | None:
    area = _ring_area(ring)
    if abs(area) < 1e-9:
        return None
    cx = cy = 0.0
    for index in range(len(ring) - 1):
        x0, y0 = ring[index][0], ring[index][1]
        x1, y1 = ring[index + 1][0], ring[index + 1][1]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return cx / (6 * area), cy / (6 * area)


def _contains(ring: list, lon: float, lat: float) -> bool:
    """Ray casting, the standard even-odd test."""
    inside = False
    for index in range(len(ring) - 1):
        x0, y0 = ring[index][0], ring[index][1]
        x1, y1 = ring[index + 1][0], ring[index + 1][1]
        if (y0 > lat) != (y1 > lat):
            if lon < x0 + (lat - y0) / (y1 - y0) * (x1 - x0):
                inside = not inside
    return inside


def _pole_of_inaccessibility(ring: list, *, grid: int = 28) -> tuple[float, float] | None:
    """Grid-search the interior point furthest from the outline.

    Coarse on purpose: the label only has to sit convincingly in open water,
    and the ring is thinned first so the search stays linear in grid size
    rather than in coastline detail.
    """
    step = max(1, len(ring) // 240)
    thin = ring[::step]
    lons = [point[0] for point in thin]
    lats = [point[1] for point in thin]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)

    best_point, best_distance = None, -1.0
    for i in range(1, grid):
        lon = west + (east - west) * i / grid
        for j in range(1, grid):
            lat = south + (north - south) * j / grid
            if not _contains(ring, lon, lat):
                continue
            distance = min((lon - x) ** 2 + (lat - y) ** 2 for x, y in zip(lons, lats))
            if distance > best_distance:
                best_point, best_distance = (lon, lat), distance
    return best_point


class CityIndex:
    """Major cities, worldwide and offline — the globe's second label layer.

    Natural Earth's populated places (public domain), ranked so the globe can
    show only as many as the current altitude can carry.
    """

    def __init__(self, path: Path, *, source_url: str = CITY_SOURCE_URL):
        self.path = path
        self.source_url = source_url
        self._collection: dict | None = None
        self._places: list | None = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    async def ensure(self, client: httpx.AsyncClient | None = None) -> Path:
        if self.path.exists():
            return self.path
        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            response = await client.get(self.source_url, timeout=120.0, follow_redirects=True)
            response.raise_for_status()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(response.content)
        finally:
            if owns_client:
                await client.aclose()
        return self.path

    def search(
        self,
        query: str,
        *,
        limit: int = 6,
        near: tuple[float, float] | None = None,
        home_country: str = "",
    ) -> list[dict]:
        """Prefix search over the populated-places index, ranked like a human.

        Nominatim ranks by global importance, which is why typing "ha" in
        Hannover surfaced Hamadan. Here the score is what a person standing on
        the map expects: big beats small (population), near beats far, and the
        country you are in beats everywhere else — Hamburg over Hamm, Hannover
        over Haiti.
        """
        needle = query.strip().casefold()
        if not needle:
            return []
        scored = []
        for feature in self.collection()["features"]:
            props = feature["properties"]
            name = props.get("name", "")
            if not name.casefold().startswith(needle):
                continue
            lon, lat = feature["geometry"]["coordinates"]
            population = props.get("population") or 0
            # 0..~1 for anything up to a megacity.
            score = math.log10(population + 1) / 7.0
            distance_km = None
            if near is not None:
                mean = math.radians((near[0] + lat) / 2.0)
                dx = math.radians(lon - near[1]) * math.cos(mean)
                dy = math.radians(lat - near[0])
                distance_km = math.hypot(dx, dy) * 6371.0
                # Proximity fades over a few hundred km — enough that the
                # city you are looking at wins, without hiding the megacity
                # one state over.
                score += 1.8 * math.exp(-distance_km / 400.0)
            if home_country and props.get("country", "") == home_country:
                score += 1.6
            scored.append((score, {
                "name": name,
                "country": props.get("country", ""),
                "lat": lat,
                "lon": lon,
                "population": population,
                "distance_km": round(distance_km, 1) if distance_km is not None else None,
                "score": round(score, 3),
            }))
        scored.sort(key=lambda pair: -pair[0])
        return [entry for _, entry in scored[:limit]]

    def places(self) -> list:
        """The index as typed `CityPlace` lookup rows, hydrated once.

        The via-place corridor matcher (`geo/via_places.py`) runs against
        all ~7300 places on every routed candidate — walking the GeoJSON
        feature dicts each time would dominate its budget.
        """
        from ..models.city_place import CityPlace

        if self._places is None:
            places = []
            for feature in self.collection()["features"]:
                props = feature["properties"]
                lon, lat = feature["geometry"]["coordinates"]
                places.append(CityPlace(
                    name=props.get("name", ""),
                    lat=lat, lon=lon,
                    population=int(props.get("population") or 0),
                    capital=bool(props.get("capital")),
                    state_capital=bool(props.get("state_capital")),
                    rank=int(props.get("rank") or 10),
                ))
            self._places = places
        return self._places

    def collection(self) -> dict:
        if self._collection is None:
            raw = json.loads(self.path.read_text())
            features = []
            for feature in raw["features"]:
                properties = feature.get("properties", {})
                features.append({
                    "type": "Feature",
                    "properties": {
                        "name": properties.get("name", ""),
                        "country": properties.get("adm0name", ""),
                        # Lower rank = more important; the client thins by this.
                        "rank": properties.get("labelrank", 10),
                        "population": properties.get("pop_max", 0),
                        # Globe views show capitals only — every other city is
                        # noise at that altitude.
                        # NATIONAL capitals only: Natural Earth also flags
                        # regional ones ("Admin-1 capital"), which would put
                        # Potenza and Campobasso on a view of Europe.
                        "capital": (properties.get("featurecla") or "").startswith("Admin-0 capital"),
                        # State/province capitals: shown once a single country
                        # fills enough of the screen to carry them.
                        "state_capital": (properties.get("featurecla") or "").startswith("Admin-1 capital"),
                    },
                    "geometry": feature["geometry"],
                })
            self._collection = {"type": "FeatureCollection", "features": features}
        return self._collection


class CountryIndex:
    """Loads the boundary file, answers name searches, serves the layer data."""

    def __init__(self, path: Path, *, source_url: str = SOURCE_URL):
        self.path = path
        self.source_url = source_url
        self._collection: dict | None = None
        self._hits: list[CountryHit] | None = None

    @property
    def available(self) -> bool:
        return self.path.exists()

    async def ensure(self, client: httpx.AsyncClient | None = None) -> Path:
        """Fetch the boundary file once (authoring time)."""
        if self.path.exists():
            return self.path
        owns_client = client is None
        client = client or httpx.AsyncClient()
        try:
            response = await client.get(self.source_url, timeout=120.0, follow_redirects=True)
            response.raise_for_status()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(response.content)
        finally:
            if owns_client:
                await client.aclose()
        return self.path

    def collection(self) -> dict:
        """Boundaries as a slim FeatureCollection (only the props we render)."""
        if self._collection is None:
            raw = json.loads(self.path.read_text())
            features = []
            for feature in raw["features"]:
                properties = feature.get("properties", {})
                iso = properties.get("ISO_A2") or properties.get("ADM0_A3") or ""
                names = _names(properties)
                slim = {"iso": iso, "name": properties.get("NAME", "")}
                # One property per language so the label layer can switch with
                # a style rebuild instead of a re-download.
                slim.update({f"name_{lang}": value for lang, value in names.items()})
                features.append({
                    "type": "Feature",
                    "id": iso,
                    "properties": slim,
                    "geometry": feature["geometry"],
                })
            self._collection = {"type": "FeatureCollection", "features": features}
        return self._collection

    def label_points(self) -> dict:
        """One point per country, at its mainland centre.

        Labelling the polygons directly makes MapLibre place a label in every
        tile the polygon touches — "DEUTSCHLAND" three times across one view.
        A point geometry gets exactly one label per country.
        """
        features = []
        for hit in self.hits():
            properties = {
                "iso": hit.iso, "name": hit.name,
                # Mainland extent: the client projects these corners to decide
                # whether the country is big enough on screen to be labelled.
                "bbox": [hit.bbox.west, hit.bbox.south, hit.bbox.east, hit.bbox.north],
            }
            properties.update({f"name_{lang}": value for lang, value in hit.names.items()})
            features.append({
                "type": "Feature",
                "id": hit.iso,
                "properties": properties,
                "geometry": {"type": "Point", "coordinates": [hit.center.lon, hit.center.lat]},
            })
        return {"type": "FeatureCollection", "features": features}

    def hits(self) -> list[CountryHit]:
        if self._hits is None:
            raw = json.loads(self.path.read_text())
            hits = []
            for feature in raw["features"]:
                properties = feature.get("properties", {})
                rings = _rings(feature["geometry"])
                if not rings:
                    continue
                # Frame the mainland: the ring with the most vertices is the
                # main landmass, not an overseas island.
                west, south, east, north = _bbox(max(rings, key=len))
                if west >= east or south >= north:
                    continue
                names = _names(properties)
                hits.append(CountryHit(
                    iso=properties.get("ISO_A2") or properties.get("ADM0_A3") or "",
                    label=names.get(DEFAULT_LANGUAGE) or properties.get("NAME", ""),
                    name=properties.get("NAME", ""),
                    names=names,
                    continent=properties.get("CONTINENT", ""),
                    bbox=BBoxDeg(west=west, south=south, east=east, north=north),
                    center=LatLon(lat=(south + north) / 2, lon=(west + east) / 2),
                ))
            self._hits = hits
        return self._hits

    def country_at(self, lat: float, lon: float) -> CountryHit | None:
        """The country a point sits in, by mainland bbox (smallest box wins).

        Deliberately coarse: this feeds search *ranking*, where "Germany-ish"
        is all that is needed — a polygon test would cost more and change
        nothing about which city ends up first.
        """
        best = None
        for hit in self.hits():
            if hit.bbox.contains(lat, lon):
                if best is None or hit.bbox.area_deg2 < best.bbox.area_deg2:
                    best = hit
        return best

    def search(
        self, query: str, *, limit: int = 5, lang: str = DEFAULT_LANGUAGE
    ) -> list[CountryHit]:
        """Match a name in ANY supported language, or the ISO code.

        "Germany", "Deutschland", "Allemagne", "Alemania", "ドイツ" and "DE"
        all find the same country; results come back labelled in `lang`.
        Prefix matches rank ahead of substrings, so typing "Ital" lands on
        Italien rather than on a country that merely contains those letters.
        """
        needle = query.strip().casefold()
        if not needle:
            return []
        prefix, contains = [], []
        for hit in self.hits():
            candidates = [value.casefold() for value in hit.names.values()]
            candidates.append(hit.iso.casefold())
            if any(value.startswith(needle) for value in candidates):
                prefix.append(hit)
            elif len(needle) >= 4 and any(needle in value for value in candidates):
                # Substring matching only once the query can carry it: two
                # letters match the middle of half the atlas ("ha" is inside
                # "Marshallinseln"), and those hits bury the real answer.
                contains.append(hit)
        return [
            hit.model_copy(update={"label": hit.names.get(lang) or hit.names.get("en") or hit.name})
            for hit in (prefix + contains)[:limit]
        ]
