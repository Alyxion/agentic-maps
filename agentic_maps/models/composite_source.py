from pydantic import BaseModel, Field


class CompositeSource(BaseModel):
    """A virtual imagery source federating per-state WMS members.

    German orthophotos are published per state; a scenario spanning states (e.g.
    Stuttgart + Munich + Berlin) uses a composite whose members each declare a
    `coverage` bbox. Every tile is routed to the members whose coverage
    contains the tile center, smallest bbox first — and since state borders are
    not rectangles, a member that answers with a blank out-of-area tile is
    skipped in favour of the next candidate. Tiles nobody covers 404 and the
    vector basemap beneath shows through.
    """

    id: str
    name: str
    member_ids: list[str] = Field(min_length=1)
    # Imagery starts where the world raster (Blue Marble, z8) ends.
    min_zoom: int = 8
    max_zoom: int = 19
    tile_format: str = "jpeg"
