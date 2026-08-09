from typing import Literal

from pydantic import BaseModel

from .route_stop import RouteStop


class TripOp(BaseModel):
    """One edit to a live trip (`update_trip` MCP tool, mcp_server/trips.py).

    A small op vocabulary instead of four near-identical tools: multi-edit
    ("insert two stops") stays ONE recompute, and the roster carries one
    description instead of four that would dilute it. Exactly one op kind
    per entry; the fields the kind does not use are ignored.

    - ``add_stop``: insert ``stop`` at ``position`` (0-based; omitted/None or
      past the end = append as the new destination).
    - ``remove_stop``: delete the stop at ``index``.
    - ``move_stop``: move the stop at ``index`` to ``position``.
    - ``set_options``: change ``mode`` / ``avoid`` / ``alternates`` — only
      the fields given change, the rest keep their current value.
    """

    op: Literal["add_stop", "remove_stop", "move_stop", "set_options"]
    stop: RouteStop | None = None
    index: int | None = None
    position: int | None = None
    mode: Literal["car", "truck", "walk", "bike"] | None = None
    avoid: list[Literal["toll", "motorway", "ferry"]] | None = None
    alternates: int | None = None
