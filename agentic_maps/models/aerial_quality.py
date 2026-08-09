from pydantic import BaseModel


class AerialQuality(BaseModel):
    """How native the aerial imagery really is over a viewport.

    Answer of `GET /aerial/quality` (rest/maps_api.py): pure bbox math over
    the SAME band-coverage configuration the `/aerial` dispatcher routes
    tiles with — no tile is fetched. `best_native_zoom` is the deepest zoom
    any covering band serves natively there; past it the dispatcher only
    upscales (`X-Agentic-Maps-Aerial-Synth`). `gap` is how many zoom levels
    the requested view sits past that ceiling (never negative) — the
    frontend's auto-fallback switches the display to cartography when the
    gap says the "imagery" on screen is nothing but Blue-Marble mush.
    """

    best_native_zoom: int
    requested_zoom: int
    gap: int
