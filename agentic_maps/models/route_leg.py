from pydantic import BaseModel


class RouteLeg(BaseModel):
    """One stop-to-stop section of a multi-stop route.

    A three-stop trip is one OSRM request with two legs; summing legs is what
    lets the UI say "12 min to the airport, 9 min on to the hotel" while the
    drawn line stays a single geometry.
    """

    duration_min: float
    distance_km: float
