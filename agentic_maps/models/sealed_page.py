from pydantic import BaseModel


class SealedPage(BaseModel):
    """One embeddable map page, taken apart so a sealed host can rebuild it offline.

    A sealed session cannot navigate an iframe to `/view.html` — there is no
    server. The host instead builds the page inside a `srcdoc` frame, which
    needs its pieces separately: the styles, the body, and the scripts in
    order. Library scripts are referenced by id rather than inlined, because a
    sealed file can carry several map embeds and MapLibre must appear in it
    once.
    """

    name: str
    style_refs: list[str] = []
    styles: list[str] = []
    body: str = ""
    # ("lib", "<id>") for a shared library, ("code", "<source>") for the page's
    # own inline script. Order is execution order.
    scripts: list[tuple[str, str]] = []
