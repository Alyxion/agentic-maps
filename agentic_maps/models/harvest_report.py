from pydantic import BaseModel


class HarvestReport(BaseModel):
    """Outcome of downloading a HarvestPlan into a bundle."""

    bundle_id: str
    planned: int
    fetched: int
    skipped_existing: int
    failed: int
    # Tiles no source covers (composite scenarios near coverage borders); the
    # fallback ladder serves these from ancestors at presentation time.
    uncovered: int = 0
    bytes_fetched: int

    @property
    def complete(self) -> bool:
        return self.failed == 0
