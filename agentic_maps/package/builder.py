"""Seal a spec into a self-contained presentation map package (zip).

The package is the publish-time artifact of the offline completeness contract
(docs/concept.md §5): after this, presenting needs NO internet. Contents:

  spec.json                  the MapSpec (locations, routes, imagery adjust)
  manifest.json              PackageManifest: files, attributions, licenses
  raster/<spec_id>.mbtiles   the harvested orthophoto pyramid (must exist)
  vector/*.pmtiles           street/label extracts intersecting the package
  assets/glyphs/**           every font glyph range the style has used

Vector extracts are included whole (they are already regional); slimming them
to the package bbox via `pmtiles extract` is an authoring-time optimization.
Archives inside are stored uncompressed (their tiles are already compressed).
"""

import json
import zipfile
from pathlib import Path

from ..models.map_spec import MapSpec
from ..models.package_manifest import PackageFile, PackageManifest
from ..storage.mbtiles import MBTilesBundle
from ..storage.pmtiles_bundle import PMTilesBundle

_OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
_NATURAL_EARTH_ATTRIBUTION = "Natural Earth (public domain)"
_FONT_LICENSE = "Noto fonts — SIL Open Font License 1.1"
# Via-place names baked into spec.json come from the GeoNames-derived place
# index when it is installed (geo/geonames.py) — CC BY 4.0 requires the
# credit to travel with every redistribution, so it rides in the manifest
# whenever the spec carries such names.
_GEONAMES_ATTRIBUTION = "GeoNames (geonames.org)"
_GEONAMES_LICENSE = (
    "GeoNames cities15000 — CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/"
)


class PackageBuilder:
    def __init__(self, bundles_dir: Path, assets_dir: Path):
        self.bundles_dir = bundles_dir
        self.assets_dir = assets_dir

    def build(self, spec: MapSpec, out_path: Path) -> PackageManifest:
        raster_path = self.bundles_dir / f"{spec.id}.mbtiles"
        if not raster_path.exists():
            raise FileNotFoundError(
                f"no harvested bundle for spec '{spec.id}' — run harvest before packaging"
            )

        files: list[PackageFile] = []
        attributions: set[str] = set()
        licenses: set[str] = set()

        raster = MBTilesBundle(raster_path)
        info = raster.info()
        raster.close()
        attributions.add(info.attribution)
        licenses.add(f"{info.license_name} — {info.license_url}")

        vector_paths = [p for p in sorted(self.bundles_dir.glob("*.pmtiles")) if self._touches_spec(p, spec)]
        if vector_paths:
            attributions.add(_OSM_ATTRIBUTION)
            licenses.add("Open Database License 1.0 — https://opendatacommons.org/licenses/odbl/")

        glyph_files = sorted(self.assets_dir.glob("glyphs/**/*.pbf")) if self.assets_dir.exists() else []
        if glyph_files:
            licenses.add(_FONT_LICENSE)

        geonames_installed = (self.bundles_dir.parent / "geo" / "places-geonames.tsv.gz").exists()
        if geonames_installed and any(
            candidate.via_places
            for route in spec.routes
            for candidate in (route, *route.alternates)
        ):
            attributions.add(_GEONAMES_ATTRIBUTION)
            licenses.add(_GEONAMES_LICENSE)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as archive:

            def add(path: Path, arcname: str) -> None:
                archive.write(path, arcname)
                files.append(PackageFile(name=arcname, size_bytes=path.stat().st_size))

            spec_bytes = spec.model_dump_json(indent=2).encode()
            archive.writestr("spec.json", spec_bytes)
            files.append(PackageFile(name="spec.json", size_bytes=len(spec_bytes)))

            add(raster_path, f"raster/{raster_path.name}")
            world_path = self.bundles_dir / "world.mbtiles"
            if world_path.exists():
                world = MBTilesBundle(world_path)
                world_info = world.info()
                world.close()
                attributions.add(world_info.attribution)
                licenses.add(f"{world_info.license_name} — {world_info.license_url}")
                add(world_path, "raster/world.mbtiles")
            for path in vector_paths:
                add(path, f"vector/{path.name}")
            # Worldwide borders travel with every package: they are what makes a
            # zoomed-out "where in the world is this" view work outside the
            # regional extracts.
            countries_path = self.bundles_dir.parent / "geo" / "countries.geojson"
            if countries_path.exists():
                add(countries_path, "geo/countries.geojson")
                attributions.add(_NATURAL_EARTH_ATTRIBUTION)
                licenses.add("Natural Earth — public domain")
            for path in glyph_files:
                add(path, f"assets/glyphs/{path.relative_to(self.assets_dir / 'glyphs')}")

            manifest = PackageManifest(
                spec_id=spec.id,
                files=files,
                attributions=sorted(attributions),
                licenses=sorted(licenses),
                total_bytes=sum(f.size_bytes for f in files),
            )
            archive.writestr("manifest.json", manifest.model_dump_json(indent=2))
        return manifest

    def _touches_spec(self, pmtiles_path: Path, spec: MapSpec) -> bool:
        """A vector extract belongs in the package if any camera stop is inside it."""
        bundle = PMTilesBundle(pmtiles_path)
        try:
            bounds = bundle.bounds()
            points = [loc.camera.center for loc in spec.locations]
            if spec.overview is not None:
                points.append(spec.overview.center)
            return any(
                bounds.west <= p.lon <= bounds.east and bounds.south <= p.lat <= bounds.north
                for p in points
            )
        finally:
            bundle.close()
