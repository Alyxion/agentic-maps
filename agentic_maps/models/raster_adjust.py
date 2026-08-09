from pydantic import BaseModel, Field


class RasterAdjust(BaseModel):
    """Imagery enhancement applied to the orthophoto layer at render time.

    German survey DOPs are radiometrically flat (overexposed look); the
    defaults add saturation and contrast for a more brilliant appearance.
    Values map 1:1 to MapLibre raster-* paint properties.
    """

    saturation: float = Field(default=0.35, ge=-1.0, le=1.0)
    contrast: float = Field(default=0.18, ge=-1.0, le=1.0)
    brightness_min: float = Field(default=0.02, ge=0.0, le=1.0)
    brightness_max: float = Field(default=0.94, ge=0.0, le=1.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
