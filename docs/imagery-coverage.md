# Imagery coverage & shipping licence — worldwide survey

**The rule this project applies:** a source may only be used if we are allowed to
**bulk-download** its tiles, **store** them, and **redistribute** them inside a
sealed offline package handed to a third party — commercially. Attribution
is fine. An API key, a "no caching / no derivative storage" clause, or a
prohibition on use outside the provider's own runtime disqualifies a source
outright, however good the imagery is.

Everything below is classified against that single test.

## Status vocabulary

| Mark | Meaning |
| --- | --- |
| ✅ **shipped** | In `sources/presets.py`, endpoint verified with a real GetMap/tile request, licence read from the service's own metadata |
| 🟡 **candidate** | Programme and licence family known to permit redistribution, **endpoint and exact terms not yet verified by us** — must be probed before use |
| ❓ **unknown** | Open data likely exists but neither endpoint nor terms established |
| ❌ **excluded** | Terms forbid offline extraction or redistribution |

Only ✅ rows are safe to put in a sealed offline package today. 🟡 rows are the work queue, in the
order a customer is likely to need them.

---

## 1. Worldwide base layers (always available)

| Layer | Source | Resolution | Licence | Status |
| --- | --- | --- | --- | --- |
| Globe imagery | **NASA Blue Marble** via GIBS WMTS (`gibs.earthdata.nasa.gov`) — served as `blue-marble-plus`: land from `BlueMarble_NextGeneration`, oceans from `BlueMarble_ShadedRelief_Bathymetry` (depth structure instead of flat navy), blended per tile along the Natural Earth land-polygon mask with a ~1–2 px feathered coastline (`geo/landmask.py`); both plain layers remain as presets | ~500 m, z0–8 | NASA imagery, both layers — **public domain**, no attribution required (we credit anyway, one combined line) | ✅ shipped |
| Regional Europe | **Sentinel-2**, Copernicus, processed by BKG (`isk.geobasis-bb.de/mapproxy/dop20c_sentinel`) | 10 m, z8–12 | Copernicus legal notice — free of charge, **commercial use included**, source note required | ✅ shipped |
| Borders, names, physical | **Natural Earth** (admin-0, admin-1 lines, lakes, rivers, urban, populated places) | 1:50 m (populated places 1:10 m) | **Public domain** | ✅ shipped |
| Dense place index (via-places, autocomplete) | **GeoNames** `cities15000` dump, prepped by `tools/make_city_index.py` into `var/geo/places-geonames.tsv.gz` (~32k places ≥ 15k pop, 0.6 MB) | point data | **CC BY 4.0** (per the dump's readme.txt) — credit "GeoNames (geonames.org)" wherever the names are shown or redistributed; wired into map payloads, exported pages and package manifests | ✅ shipped |
| Streets, labels, POIs | **OpenStreetMap** via Protomaps planet builds | z0–15 | **ODbL** — attribution; a sealed offline package is a *Produced Work*, so share-alike does not reach it | ✅ shipped |

**Coverage reality check.** The Sentinel band covers roughly **0.7°E–20.5°E,
45.5°N–56.7°N** — central Europe only. It excludes the UK and Ireland, most of
Spain and Italy, Scandinavia above 56.7°N, and everything outside Europe. Above
z12 there is *no* imagery anywhere except Germany. Outside those windows the map
falls back to upscaled Blue Marble, which is honest but blurry.

---

## 2. Germany — complete (all 16 states)

The federation the product actually runs on. Each state publishes 20 cm
orthophotos (Bremen 10 cm) as open data; endpoints, layers and licences are in
`docs/concept.md` §1 and verified live.

| Licence family | States |
| --- | --- |
| dl-de/by-2-0 (attribution) | BW, NW, RP, MV, BB, ST, SN, **HH** |
| dl-de/zero-2-0 (no conditions) | BE, HE, SH, TH, SL |
| CC BY 4.0 | BY, NI, HB |

**Hamburg (closed 2026-07-27):** the LGV retired its old `HH_WMS_DOP*` hosts;
the live service is `wms_dop_zeitreihe_unbelaubt` (layer
`dop_zeitreihe_unbelaubt`, TIME 2001-2025, newest year served when TIME is
omitted). GetMap-verified at z13/z17/z19; out-of-area answers are ~667-byte
blank JPEGs, safely below the blank-tile heuristic. Licence dl-de/by-2-0 per
the Transparenzportal; bulk GeoTIFF downloads there remain the archival
fallback should the host move again.

**BKG nationwide DOP20** exists as one seamless mosaic under dl-de/by-2-0, but
GetMap requires registration and third parties are charged, so it stays behind
`AGENTIC_MAPS_BKG_URL` rather than shipping.

---

## 2b. Verification run — 2026-07-26

Probed live: `GetCapabilities`, then a real keyless **EPSG:3857 GetMap** at
**z9 / z13 / z17**. "IMG" means a real photographic tile came back at that zoom.

| Service | Endpoint | Layer | z9 / z13 / z17 | Verdict |
| --- | --- | --- | --- | --- |
| **US** USGS Imagery Only | `basemap.nationalmap.gov/arcgis/services/USGSImageryOnly/MapServer/WMSServer` | `0` | 29k / 27k / 16k | ✅ works, keyless, bbox is worldwide |
| **US** USGS NAIPPlus | `imagery.nationalmap.gov/arcgis/services/USGSNAIPPlus/ImageServer/WMSServer` | `USGSNAIPPlus:NaturalColor` | 12k / 22k / 18k | ✅ works, keyless |
| **CH** swisstopo | `wms.geo.admin.ch/` | `ch.swisstopo.swissimage` | 26k / 24k / 24k | ✅ works, keyless |
| **NL** PDOK | `service.pdok.nl/hwh/luchtfotorgb/wms/v1_0` | `Actueel_orthoHR` | 27k / 31k / 23k | ✅ works; bbox 3.171,50.579,7.489,53.638; Fees/AccessConstraints **None** |
| **FR** IGN Géoplateforme | `data.geopf.fr/wms-r/wms` | `HR.ORTHOIMAGERY.ORTHOPHOTOS` | 18k / 22k / 16k | ✅ works; terms = cartes.gouv.fr CGU (read before shipping) |
| **ES** IGN PNOA | `www.ign.es/wms-inspire/pnoa-ma` | `OI.OrthoimageCoverage` | 13k / 16k / 19k | ✅ works, keyless |
| **BE-VL** Vlaanderen | `geo.api.vlaanderen.be/OMWRGBMRVL/wms` | `Ortho` | 20k / 21k / 21k | ✅ works (Flanders only) |
| **EE** Maa-amet | `kaart.maaamet.ee/wms/fotokaart` | `EESTIFOTO` | non-image at all three | ⚠️ needs different format/params |
| **LU** Geoportail | `wms.geoportail.lu/opendata/service` | `ortho` | HTTP 500 | ⚠️ layer name wrong (caps list `Ortho`) |
| **CA** NRCan | `maps.geogratis.gc.ca/wms/imagery_imagerie` | — | caps 404 | ❓ endpoint moved |
| **CZ** ČÚZK | `ags.cuzk.cz/arcgis1/services/ortofoto_wm/…` | — | caps HTTP 400 | ❓ endpoint wrong |
| **NO** Geonorge | `wms.geonorge.no/skwms1/wms.nib` | — | 200 but no caps | ❓ likely needs a key |
| **PL** GUGiK | `mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/StandardResolution` | caps OK, layers generic | not yet GetMap-tested | 🟡 |

**Caveats found in this run, not yet resolved:**

- **USGS advertises a worldwide bbox** (-180,-89,180,89) although the data is
  US-only. The coverage bbox must be set by hand to the US, otherwise the
  federation would route the whole planet to it.
- **France's `AccessConstraints` points at the cartes.gouv.fr CGU** rather than
  naming a licence. IGN moved to the Etalab Open Licence in 2021, but the CGU
  text must be read before this ships.
- **Belgium is regional** — this endpoint is Flanders; Wallonia and Brussels are
  separate services.
- The two ⚠️ rows are parameter problems, not licence problems, and should
  succeed on a second attempt.

None of these are in `sources/presets.py` yet: verifying that a service answers
is step 2 of 5 (see §6). Still outstanding for each: scale limits, out-of-area
behaviour, and the real coverage bbox.

## 3. Europe

| Country | Programme | Licence family | Status |
| --- | --- | --- | --- |
| **Austria** | basemap.at orthophoto; Geoland | Open Government Data AT (CC BY 4.0) | 🟡 candidate |
| **Switzerland** | swisstopo SWISSIMAGE (10 cm) | swisstopo open data — free incl. commercial, attribution | 🟡 candidate |
| **Netherlands** | PDOK Luchtfoto (8 cm) | CC BY 4.0 | 🟡 candidate |
| **Belgium** | Regional: AGIV/Informatie Vlaanderen, SPW Wallonie | Regional open licences (mostly attribution) | 🟡 candidate |
| **Luxembourg** | Geoportail.lu orthophoto | CC BY 4.0 | 🟡 candidate |
| **France** | IGN Géoplateforme / BD ORTHO | Open Licence (Etalab 2.0) since 2021 | 🟡 candidate |
| **Spain** | IGN PNOA orthophoto | CC BY 4.0 | 🟡 candidate |
| **Italy** | Geoportale Nazionale; regional (Lombardia, Bolzano…) | Mixed — national CC BY, some regional restricted | 🟡 candidate |
| **Poland** | Geoportal / GUGiK orthophoto | Open since 2020, attribution | 🟡 candidate |
| **Czechia** | ČÚZK orthophoto | Open data, attribution | 🟡 candidate |
| **Denmark** | Datafordeler / SDFE ortofoto | Free, registration for services | 🟡 candidate |
| **Sweden** | Lantmäteriet ortofoto | CC0 for open products | 🟡 candidate |
| **Norway** | Norge i bilder / Kartverket | NLOD (attribution) | 🟡 candidate |
| **Finland** | NLS ortokuva | CC BY 4.0 | 🟡 candidate |
| **UK / Ireland** | OS / Tailte Éireann aerial | **Licensed products** — open products are vector/raster maps, not aerial | ❓ / likely ❌ for aerial |
| **Rest of Europe** | Varies | — | ❓ |

**EU-wide fallback if a country has no open aerial:** Copernicus Sentinel-2 at
10 m, free including commercial use with the source note. Obtaining a *wider*
mosaic than the BKG window means the Copernicus Data Space (free registration)
rather than the current proxy.

---

## 4. Rest of world

| Area | Programme | Licence | Status |
| --- | --- | --- | --- |
| **United States** | **USGS The National Map / NAIP** (`USGSImageryOnly`) | US federal works — **public domain**, no key, no attribution required | 🟡 candidate — *highest value, likely simplest* |
| **Canada** | NRCan / provincial imagery | Open Government Licence — Canada (attribution) | 🟡 candidate |
| **Australia** | Geoscience Australia; state imagery (NSW, VIC…) | CC BY 4.0 | 🟡 candidate |
| **New Zealand** | LINZ Data Service aerial | CC BY 4.0 (key required for some services) | 🟡 candidate |
| **Japan** | GSI seamless photo | GSI terms — attribution, redistribution permitted with conditions | 🟡 candidate |
| **Brazil, India, China, Africa, Middle East, SE Asia** | No general open aerial programme found | — | ❓ — expect Sentinel-2 only |

For everything in the ❓ rows the practical answer is **Sentinel-2 at 10 m**
(Copernicus, global, free including commercial) plus Blue Marble below z8. That
is enough for country and regional views; it is not enough for a building.

---

## 5. Permanently excluded

| Provider | Why |
| --- | --- |
| **Google** Maps/Earth/Satellite | ToS forbid extraction, caching and use outside Google's runtime |
| **Mapbox Satellite** | Licensed to the account; no redistribution of tiles |
| **Bing / Azure Maps Aerial** | Same |
| **Esri World Imagery** | Redistribution restricted; underlying imagery is commercially licensed |
| **EOX Sentinel-2 cloudless** | Rendered tiles need a **paid** EOX licence for commercial use |
| `tile.openstreetmap.org` raster | OSMF policy forbids bulk download (the *data* is fine — we take it via Protomaps) |

No amount of attribution makes any of these shippable. They are not "use with
care" — they are out.

---

## 6. How to add a country

The federation does the routing; a new source is a preset plus verification.

1. Find the national/regional WMS or WMTS and its licence statement — the
   service's own `GetCapabilities` `AccessConstraints` is the authority, not a
   portal page.
2. Confirm it answers a **keyless EPSG:3857 GetMap** and check for scale limits
   (Hessen renders only above ~z17 on one layer; MV serves nothing below ~z11).
3. Check what it returns **outside its territory** — several services answer
   with an opaque white or flat-colour tile rather than an error; that needs
   `white_is_nodata` or the blank-size heuristic.
4. Take the coverage bbox from `EX_GeographicBoundingBox`, not from a guess.
5. Add the preset with attribution and licence URL, add it to the composite,
   and add a row to §1 of `docs/concept.md`.

Steps 2–3 are the slow part and cannot be skipped: of the 16 German states, one
had an unreachable host, one served blank below z17, one served nothing below
z11, and one painted its neighbours white.
