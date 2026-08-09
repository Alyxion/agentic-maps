from pydantic import BaseModel, Field


class FeatureExtractMeta(BaseModel):
    """The `meta` block riding along with a `/vector/features` answer.

    The FeatureCollection itself stays plain GeoJSON (any GIS consumer can
    take it as-is, same reasoning as `/isochrone`); everything a caller needs
    to judge the answer's completeness lives here instead of being implied.
    """

    zoom: int = Field(description="Tile zoom the features were read at.")
    tiles: int = Field(description="Covering tiles computed for the bbox.")
    tiles_with_data: int = Field(
        description="How many of those tiles actually held data — fewer than "
                    "`tiles` over ocean, abroad, or offline with a cold cache."
    )
    layer_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Features returned per source layer.",
    )
    limit: int = Field(description="Feature cap that was applied.")
    truncated: bool = Field(
        default=False,
        description="True when the cap cut the answer short — shrink the bbox "
                    "or raise `limit` for the rest.",
    )
    # Geometry honesty: features are returned exactly as the tiles carry
    # them, which means clipped at tile borders (with a small buffer the
    # tiler adds). De-duplication merges the same feature *id* seen in
    # several tiles but does not re-stitch its geometry.
    tile_clipped: bool = True
