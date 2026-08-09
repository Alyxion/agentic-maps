from pydantic import BaseModel


class SealedRoute(BaseModel):
    """One authoring-time routing answer, frozen for presentation.

    Routes are computed against OSRM while editing and must never be computed
    again (docs/concept.md §6). The embedded pages ask for them by POSTing a
    body, so the body — canonicalised — is the key the sealed page looks up.
    """

    key: str
    request: dict
    response: dict
