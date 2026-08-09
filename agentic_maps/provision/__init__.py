"""Region-bulk offline provisioning: "I want offline routing for all Europe".

`estimates.py` is the single source of size truth (used by the MCP tool
descriptions, the confirm step, the setup wizard and the unit tests);
`engine.py` runs the actual jobs — async, in-process, resumable, honest
about progress. REST surface in `rest/maps_api.py` (`/provision`), MCP
tools in `mcp_server/server.py`.
"""

from .engine import ProvisionEngine
from .estimates import REGION_PRESETS, estimate_region

__all__ = ["ProvisionEngine", "REGION_PRESETS", "estimate_region"]
