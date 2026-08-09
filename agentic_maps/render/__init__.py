"""Server-side screenshot of the live map runtime (docs/rendering.md).

`params.py` is pure and browser-free (bounds checking, the width/height/scale
→ Playwright options mapping) — it is exactly what `models/render_request.py`
validates against, and what `tests/test_render_params.py` exercises without
Playwright installed. `service.py` is where Playwright actually gets
imported, and only inside functions, so importing this package never
requires the `render` extra — only calling `RenderService.render()` does.
"""
