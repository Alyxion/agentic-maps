from pydantic import BaseModel


class EmbedShot(BaseModel):
    """One embedded map page as a host actually shows it — the unit of recording.

    A host does not always hand the recorder a `MapSpec`: some embeds are a
    page (e.g. `/maps/view.html?lat=…`) whose choreography lives in that
    page's own JavaScript. So the recording unit is the embed, described
    exactly as it is embedded: same URL, same box on stage.

    `steps` is a FLOOR on how many step states to drive this embed through —
    the actual count is `max(steps, len(window.__agenticSealSteps))` once the
    recorder can ask the page itself (see `seal/recorder.py`); a page that
    declares no steps at all is simply recorded once, in whatever state it
    reaches on its own.
    """

    label: str
    url: str
    width: int
    height: int
    steps: int = 1
