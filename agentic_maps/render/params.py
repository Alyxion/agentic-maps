"""Pure, browser-free rendering parameters.

Split out from `service.py` so the width/height/scale → Playwright mapping
and the bounds every `/render` request is checked against can be unit-tested
without importing Playwright at all — this module has no dependency on it,
directly or indirectly, and never will.

`scale` mirrors `devicePixelRatio`/Playwright's `device_scale_factor`
one-for-one: both mean "how many device pixels per CSS pixel", so 1/2/3 here
is exactly what a browser's own `srcset` 1x/2x/3x asks for.
"""

MIN_DIMENSION_PX = 64
MAX_DIMENSION_PX = 4096
VALID_SCALES = (1, 2, 3)
VALID_FORMATS = ("png", "jpeg")


def validate_render_params(width: int, height: int, scale: int, format: str, quality: int) -> None:
    """Raises `ValueError` describing the first thing wrong; otherwise returns None."""
    if not (MIN_DIMENSION_PX <= width <= MAX_DIMENSION_PX):
        raise ValueError(
            f"width must be between {MIN_DIMENSION_PX} and {MAX_DIMENSION_PX}px, got {width}"
        )
    if not (MIN_DIMENSION_PX <= height <= MAX_DIMENSION_PX):
        raise ValueError(
            f"height must be between {MIN_DIMENSION_PX} and {MAX_DIMENSION_PX}px, got {height}"
        )
    if scale not in VALID_SCALES:
        raise ValueError(f"scale must be one of {VALID_SCALES}, got {scale}")
    if format not in VALID_FORMATS:
        raise ValueError(f"format must be one of {VALID_FORMATS}, got {format!r}")
    if not (1 <= quality <= 100):
        raise ValueError(f"quality must be between 1 and 100, got {quality}")


def playwright_context_options(width: int, height: int, scale: int) -> dict:
    """kwargs for `browser.new_page(**this)`: viewport size + device_scale_factor.

    Written once and tested here instead of inline at the call site, so the
    mapping cannot silently drift between `RenderService` and anything else
    (e.g. `tools/verify_render.py`) that needs to reason about it.
    """
    return {"viewport": {"width": width, "height": height}, "device_scale_factor": scale}


def screenshot_options(format: str, quality: int) -> dict:
    """kwargs for `element_handle.screenshot(**this)`.

    `quality` only applies to JPEG — PNG is lossless and Playwright rejects
    the argument outright if it is passed for a PNG screenshot.
    """
    options: dict = {"type": format}
    if format == "jpeg":
        options["quality"] = quality
    return options
