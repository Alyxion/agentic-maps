"""Pure, browser-free checks for agentic_maps/render/params.py.

No Playwright import happens anywhere in this file or in params.py itself —
that is the whole point of splitting these functions out of service.py, and
this test is what proves it stays true.
"""

import inspect

import pytest

from agentic_maps.render import params
from agentic_maps.render.params import (
    MAX_DIMENSION_PX,
    MIN_DIMENSION_PX,
    VALID_SCALES,
    playwright_context_options,
    screenshot_options,
    validate_render_params,
)


def test_params_module_never_imports_playwright():
    # A static check rather than a `sys.modules` probe: the latter depends on
    # whichever tests happened to run earlier in the same process. Reading
    # back the module's own source is order-independent and directly proves
    # the contract this file's docstring claims — no `import playwright`
    # anywhere in params.py, top-level or inside a function.
    assert "import playwright" not in inspect.getsource(params).lower()


@pytest.mark.parametrize("width,height,scale,fmt,quality", [
    (1280, 720, 1, "png", 85),
    (MIN_DIMENSION_PX, MIN_DIMENSION_PX, 2, "jpeg", 1),
    (MAX_DIMENSION_PX, MAX_DIMENSION_PX, 3, "jpeg", 100),
])
def test_validate_render_params_accepts_valid_combinations(width, height, scale, fmt, quality):
    validate_render_params(width, height, scale, fmt, quality)  # must not raise


@pytest.mark.parametrize("width,height,scale,fmt,quality,needle", [
    (MIN_DIMENSION_PX - 1, 720, 1, "png", 85, "width"),
    (1280, MAX_DIMENSION_PX + 1, 1, "png", 85, "height"),
    (1280, 720, 4, "png", 85, "scale"),
    (1280, 720, 0, "png", 85, "scale"),
    (1280, 720, 1, "gif", 85, "format"),
    (1280, 720, 1, "jpeg", 0, "quality"),
    (1280, 720, 1, "jpeg", 101, "quality"),
])
def test_validate_render_params_rejects_out_of_bounds(width, height, scale, fmt, quality, needle):
    with pytest.raises(ValueError, match=needle):
        validate_render_params(width, height, scale, fmt, quality)


def test_scale_maps_onto_device_scale_factor_one_to_one():
    for scale in VALID_SCALES:
        options = playwright_context_options(1280, 720, scale)
        assert options["device_scale_factor"] == scale
        assert options["viewport"] == {"width": 1280, "height": 720}


def test_screenshot_options_png_has_no_quality():
    options = screenshot_options("png", 85)
    assert options == {"type": "png"}


def test_screenshot_options_jpeg_carries_quality():
    options = screenshot_options("jpeg", 42)
    assert options == {"type": "jpeg", "quality": 42}
