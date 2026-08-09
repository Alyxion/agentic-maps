from pydantic import BaseModel

from .sealed_page import SealedPage


class SealedWeb(BaseModel):
    """The map runtime's own web assets, deduplicated across every embed.

    Shipped once per sealed session no matter how many map pages it embeds:
    `libraries` and `stylesheets` hold the shared source, `pages` only
    reference them.
    """

    libraries: dict[str, str] = {}
    stylesheets: dict[str, str] = {}
    pages: dict[str, SealedPage] = {}
