from datetime import datetime, timezone

from pydantic import BaseModel, Field


class PackageFile(BaseModel):
    name: str
    size_bytes: int


class PackageManifest(BaseModel):
    """Contents + provenance of a sealed presentation map package."""

    spec_id: str
    created_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    files: list[PackageFile]
    attributions: list[str]
    licenses: list[str]
    total_bytes: int
