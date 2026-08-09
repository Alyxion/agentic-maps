from pydantic import BaseModel


class SetupRunResult(BaseModel):
    """What `setup/wizard.run_stack` did or didn't do.

    `started=False` with a `message` covers every declined/unavailable path
    (no `docker` on PATH, user said no, `write_plan` was never called) — the
    wizard prints `message` either way rather than branching on a bare bool.
    """

    started: bool
    message: str
    ready_urls: dict[str, str] = {}
