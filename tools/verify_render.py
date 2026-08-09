"""Headless check: `POST /render` produces a real, non-blank PNG at 1x/2x/3x,
with pixel dimensions that scale exactly with `scale`.

Unlike the other tools/verify_*.py scripts, this one does NOT import
Playwright itself — the browser automation it is checking runs INSIDE the
server process, driven by `agentic_maps/render/service.py` when `POST
/render` is called (see docs/rendering.md's design note on why: Playwright
must run in the same process that serves `MapsApi` and `web/`, since
`map.js` fetches tiles/vectors/glyphs relative to that origin). This script
is therefore a plain HTTP client exercising the public contract exactly like
any other caller would, and that IS the verification: if the server-side
Playwright page never came up, or never settled, or screenshotted the wrong
thing, the response fails one of the checks below.

Requires a running dev server (`agentic-maps-dev`, default :8095) with the
`render` extra installed AND a real Chromium binary available to it
(`playwright install chromium`, or `AGENTIC_MAPS_RENDER_CHROMIUM_PATH`
pointed at one already on disk).
"""
import json
import os
import struct
import sys
import tempfile

import httpx

BASE = "http://127.0.0.1:8095"
SCRATCH = os.environ.get("VERIFY_SCRATCH", tempfile.gettempdir())

# Same demo coordinate tools/verify_map.py already exercises (Metzingen HQ),
# so a failure here is directly comparable to that check.
VIEW = {"center": {"lat": 48.5386, "lon": 9.2925}, "zoom": 15.5, "source_id": "de-dop"}
WIDTH, HEIGHT = 640, 400
# A real map screenshot at this size carries real tile/label/road detail; a
# blank or solid-color render compresses to a small fraction of this. Not
# rigorous pixel analysis, but a cheap, effective smoke test for "something
# real got drawn" versus "a grey/white rectangle got screenshotted".
MIN_BYTES_FOR_NOT_BLANK = 15_000


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("response is not a PNG (magic bytes mismatch)")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def main() -> None:
    results = {}
    for scale in (1, 2, 3):
        response = httpx.post(
            f"{BASE}/api/v1/maps/render",
            json={"view": VIEW, "width": WIDTH, "height": HEIGHT, "scale": scale, "format": "png"},
            timeout=90.0,
        )
        key = f"{scale}x"
        if response.status_code != 200:
            results[key] = {"status": response.status_code, "detail": response.text[:400]}
            continue
        data = response.content
        out_path = os.path.join(SCRATCH, f"pw-render-{scale}x.png")
        with open(out_path, "wb") as f:
            f.write(data)
        try:
            width, height = _png_dimensions(data)
        except ValueError as error:
            results[key] = {"status": 200, "error": str(error), "bytes": len(data)}
            continue
        results[key] = {
            "status": 200,
            "bytes": len(data),
            "pixel_width": width,
            "pixel_height": height,
            "expected_width": WIDTH * scale,
            "expected_height": HEIGHT * scale,
            "path": out_path,
        }

    print(json.dumps(results, indent=2))

    ok = all(
        r.get("status") == 200
        and r.get("pixel_width") == r.get("expected_width")
        and r.get("pixel_height") == r.get("expected_height")
        and r.get("bytes", 0) >= MIN_BYTES_FOR_NOT_BLANK
        for r in results.values()
    )
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
