import json
import zipfile

import pytest

from agentic_maps.models.tile_coord import TileCoord
from agentic_maps.package.builder import PackageBuilder
from agentic_maps.storage.mbtiles import MBTilesBundle


def test_package_contains_everything(tmp_path, wms_source, two_stop_spec, pmtiles_writer):
    bundles = tmp_path / "bundles"
    assets = tmp_path / "assets"
    (assets / "glyphs" / "Noto Sans Regular").mkdir(parents=True)
    (assets / "glyphs" / "Noto Sans Regular" / "0-255.pbf").write_bytes(b"glyph")

    bundle = MBTilesBundle.create(bundles / f"{two_stop_spec.id}.mbtiles", wms_source)
    bundle.put_tile(TileCoord(z=12, x=1, y=1), b"tile")
    bundle.close()

    # One extract containing the spec, one far away (must be excluded).
    pmtiles_writer(bundles / "streets-region.pmtiles", [(0, 0, 0)],
                   (89_000_000, 484_000_000, 95_000_000, 489_000_000), b"\x1a\x2amvt")
    pmtiles_writer(bundles / "streets-elsewhere.pmtiles", [(0, 0, 0)],
                   (130_000_000, 520_000_000, 140_000_000, 530_000_000), b"\x1a\x2amvt")

    out = tmp_path / "pkg.zip"
    manifest = PackageBuilder(bundles, assets).build(two_stop_spec, out)

    names = {f.name for f in manifest.files}
    assert "spec.json" in names
    assert f"raster/{two_stop_spec.id}.mbtiles" in names
    assert "vector/streets-region.pmtiles" in names
    assert "vector/streets-elsewhere.pmtiles" not in names
    assert "assets/glyphs/Noto Sans Regular/0-255.pbf" in names
    assert any("dl-de/by-2-0" in a for a in manifest.attributions)
    assert any("OpenStreetMap" in a for a in manifest.attributions)

    with zipfile.ZipFile(out) as archive:
        stored = set(archive.namelist())
        assert "manifest.json" in stored
        spec_json = json.loads(archive.read("spec.json"))
        assert spec_json["id"] == two_stop_spec.id


def test_package_credits_geonames_when_via_places_ship(tmp_path, wms_source, two_stop_spec):
    """Via-place names in spec.json are CC-BY GeoNames data when the dense
    index is installed — the credit must travel in the manifest."""
    from agentic_maps.models.lat_lon import LatLon
    from agentic_maps.models.map_route import MapRoute
    from agentic_maps.models.via_place import ViaPlace

    bundles = tmp_path / "bundles"
    bundle = MBTilesBundle.create(bundles / f"{two_stop_spec.id}.mbtiles", wms_source)
    bundle.put_tile(TileCoord(z=12, x=1, y=1), b"tile")
    bundle.close()
    (tmp_path / "geo").mkdir()
    (tmp_path / "geo" / "places-geonames.tsv.gz").write_bytes(b"\x1f\x8b\x08\x00stub")
    spec = two_stop_spec.model_copy(update={"routes": [MapRoute(
        id="r1", from_location="hq", to_location="airport",
        duration_min=42.0, distance_km=61.0,
        geometry=[LatLon(lat=48.54, lon=9.29), LatLon(lat=48.69, lon=9.22)],
        via_places=[ViaPlace(name="Pforzheim", lat=48.884, lon=8.699,
                             population=119_313, along_km=30.0)],
    )]})

    manifest = PackageBuilder(bundles, tmp_path / "assets").build(spec, tmp_path / "pkg.zip")
    assert any("GeoNames" in a for a in manifest.attributions)
    assert any("CC BY 4.0" in lic and "GeoNames" in lic for lic in manifest.licenses)

    # Without the index installed the same spec stays uncredited — the names
    # then came from public-domain Natural Earth.
    (tmp_path / "geo" / "places-geonames.tsv.gz").unlink()
    manifest = PackageBuilder(bundles, tmp_path / "assets").build(spec, tmp_path / "pkg2.zip")
    assert not any("GeoNames" in a for a in manifest.attributions)


def test_package_requires_harvest_first(tmp_path, two_stop_spec):
    builder = PackageBuilder(tmp_path / "bundles", tmp_path / "assets")
    (tmp_path / "bundles").mkdir()
    with pytest.raises(FileNotFoundError):
        builder.build(two_stop_spec, tmp_path / "pkg.zip")
