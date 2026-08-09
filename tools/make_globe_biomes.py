"""One-time asset prep: simplified biome raster for the cartographic globe.

Owner feedback ("i doubt that africa is that green"): the globe's land was
one uniform sage tint — the Sahara rendered as green as the Congo. This
script bakes a SIMPLIFIED biome base layer so deserts read as warm sand,
vegetated zones as our sage family and tundra/ice as pale — soft and
low-contrast, in the CARTO palette family (web/road-styles.js), explicitly
NOT a satellite look (that is what the Hybrid view is for).

Source data / provenance / license
----------------------------------
  Natural Earth II, 1:50m shaded-relief raster (NE2_50M_SR, 10800x5400 TIFF),
  https://naciscdn.org/naturalearth/50m/raster/NE2_50M_SR.zip
  (the CDN behind naturalearthdata.com downloads; verified 2026-08).
  Natural Earth is explicitly PUBLIC DOMAIN: "All versions of Natural Earth
  raster + vector map data found on this website are in the public domain."
  — https://www.naturalearthdata.com/about/terms-of-use/
  No attribution is required; we credit Natural Earth in the app's
  attribution line anyway (the geo layers already come from it).

What it produces
----------------
  web/assets/globe-biomes.webp        2048x1024 equirectangular, light theme
  web/assets/globe-biomes-dark.webp   same, remapped to the dark palette

  Both are drawn by web/globe.js as the LAND BASE of the cartographic sphere
  texture, clipped to our own Natural Earth country polygons (so coastlines
  stay our crisp vector shapes); water fill, lakes, rivers, borders and
  urban smudges keep being drawn on top exactly as before. If the asset is
  missing the globe falls back to the flat palette tint.

Remap approach (Pillow channel ops only — no numpy in the venv)
---------------------------------------------------------------
  NE2's own colouring is the classifier; no landcover shapefiles needed:
    vegetation index  = G - R   (tan deserts negative, vegetation +25..+50)
    pale (ice/tundra) = bright AND unsaturated (min channel high, max-min low)
  The index lerps warm sand -> sage; the pale mask overrides toward a pale
  paper tint; NE2's near-white ocean falls into the same pale bucket, which
  keeps any coastal fringe (raster coast vs. our vector coast) quiet. A
  gentle blur first simplifies the source, and NE2's baked relief luminance
  is folded back in at low strength so mountain massifs stay legible.

Run:  .venv/bin/python tools/make_globe_biomes.py [--source path/to/NE2.tif]
      (downloads + caches the 41 MB zip in the system temp dir if no source
      is given; runs fully offline afterwards)
"""
import argparse
import io
import os
import sys
import tempfile
import urllib.request
import zipfile

from PIL import Image, ImageChops, ImageFilter

NE2_URL = "https://naciscdn.org/naturalearth/50m/raster/NE2_50M_SR.zip"
NE2_TIF = "NE2_50M_SR/NE2_50M_SR.tif"
OUT_W, OUT_H = 2048, 1024
MAX_BYTES = 500_000            # hard budget per asset, from the task brief

# Palette anchors, CARTO family (web/road-styles.js). Deserts sit with the
# farmland/barren tints, vegetation with the forest/globeLand sage, pale with
# the glacier tint. Deliberately soft: biome variation must read as texture
# under the vector layers, never compete with them.
LIGHT = {"sand": (230, 220, 187), "sage": (185, 205, 170), "pale": (233, 235, 228)}
DARK = {"sand": (61, 54, 39), "sage": (41, 51, 31), "pale": (54, 59, 57)}


def load_source(source: str | None) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = None            # the TIFF is 58 MP, and trusted
    if source:
        return Image.open(source).convert("RGB")
    cache = os.path.join(tempfile.gettempdir(), "ne2_50m_sr.zip")
    if not os.path.exists(cache):
        print(f"downloading {NE2_URL} -> {cache}")
        urllib.request.urlretrieve(NE2_URL, cache)
    with zipfile.ZipFile(cache) as z:
        data = z.read(NE2_TIF)
    return Image.open(io.BytesIO(data)).convert("RGB")


def masks(im: Image.Image) -> tuple[Image.Image, Image.Image]:
    """(vegetation 0..255, pale 0/255-ish) from NE2's own colouring."""
    r, g, b = im.split()
    # veg01 = clamp((G - R + 10) / 55): Sahara ~0, Congo/Scandinavia ~0.9,
    # France ~0.6, savanna ~0.25 — probed against the source TIFF.
    veg = ImageChops.subtract(g, r, scale=55 / 255, offset=46)
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    # Bright AND unsaturated: Greenland/Antarctica/high Alps yes, bright
    # Arabian sand no (its max-min spread is above the cut).
    bright = mn.point(lambda v: 255 if v > 208 else 0)
    unsat = ImageChops.subtract(mx, mn).point(lambda v: 255 if v < 22 else 0)
    pale = ImageChops.multiply(bright, unsat).filter(ImageFilter.GaussianBlur(1.2))
    return veg, pale


def remap(im: Image.Image, palette: dict, relief_strength: float) -> Image.Image:
    veg, pale = masks(im)
    flat = lambda color: Image.new("RGB", im.size, color)
    out = Image.composite(flat(palette["sage"]), flat(palette["sand"]), veg)
    out = Image.composite(flat(palette["pale"]), out, pale)
    # NE2's baked shaded relief, folded back in gently: overlay() around the
    # 128 midpoint, so lowlands stay put and massifs darken a touch.
    relief = im.convert("L").point(
        lambda v: max(96, min(150, int(128 + (v - 196) * 0.45))))
    shaded = ImageChops.overlay(out, Image.merge("RGB", (relief,) * 3))
    return Image.blend(out, shaded, relief_strength)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="existing NE2_50M_SR TIFF (skips download)")
    parser.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "assets"))
    args = parser.parse_args()

    src = load_source(args.source)
    # Downscale first (all the per-pixel work then runs on 2 MP, not 58 MP),
    # then a soft blur: "really simplified" is the brief — the biome layer is
    # an undertone, not imagery.
    small = src.resize((OUT_W, OUT_H), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(1.0))

    for name, palette, relief in (
        ("globe-biomes.webp", LIGHT, 0.35),
        # Weaker relief in the dark: the dark palette has little headroom and
        # strong shading there reads as blotches, not mountains.
        ("globe-biomes-dark.webp", DARK, 0.22),
    ):
        out = remap(small, palette, relief)
        path = os.path.join(args.out_dir, name)
        out.save(path, "WEBP", quality=76, method=6)
        size = os.path.getsize(path)
        print(f"{path}: {size} bytes")
        if size > MAX_BYTES:
            sys.exit(f"{name} is {size} B — over the {MAX_BYTES} B budget")


if __name__ == "__main__":
    main()
