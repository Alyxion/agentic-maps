from pydantic import BaseModel, model_validator


class BBoxDeg(BaseModel):
    """A WGS84 bounding box in degrees."""

    west: float
    south: float
    east: float
    north: float

    @model_validator(mode="after")
    def _ordered(self) -> "BBoxDeg":
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("bbox must have west<east and south<north")
        return self

    @property
    def area_deg2(self) -> float:
        """Rough size, only used to rank overlapping coverages (smallest wins:
        Berlin before Brandenburg, Bremen before Niedersachsen)."""
        return (self.east - self.west) * (self.north - self.south)

    def contains(self, lat: float, lon: float) -> bool:
        return self.west <= lon <= self.east and self.south <= lat <= self.north

    def covers(self, other: "BBoxDeg") -> bool:
        """True when `other` lies wholly inside this box.

        Distinct from `intersects` on purpose: a vector bundle that merely
        touches the viewport can serve part of the screen, and treating that
        as coverage is what leaves the rest of it blank.
        """
        return (
            self.west <= other.west
            and self.east >= other.east
            and self.south <= other.south
            and self.north >= other.north
        )

    def intersects(self, other: "BBoxDeg") -> bool:
        return not (
            other.east < self.west
            or other.west > self.east
            or other.north < self.south
            or other.south > self.north
        )

    def union(self, other: "BBoxDeg") -> "BBoxDeg":
        return BBoxDeg(
            west=min(self.west, other.west),
            south=min(self.south, other.south),
            east=max(self.east, other.east),
            north=max(self.north, other.north),
        )

    def padded(self, fraction: float) -> "BBoxDeg":
        """Grow the box by `fraction` of its size on every side."""
        dw = (self.east - self.west) * fraction
        dh = (self.north - self.south) * fraction
        return BBoxDeg(
            west=max(-180.0, self.west - dw),
            south=max(-85.06, self.south - dh),
            east=min(180.0, self.east + dw),
            north=min(85.06, self.north + dh),
        )
