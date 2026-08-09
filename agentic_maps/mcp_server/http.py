"""Mount the MCP surface at `/mcp` on a host FastAPI app (streamable HTTP).

Feature-detected exactly like `devserver.mount_stage_assets`: the host calls
`prepare_http_mcp(api)` BEFORE constructing its FastAPI app (the MCP session
manager must run inside the app's lifespan, and a mounted sub-app's own
lifespan never runs — Starlette only runs the outermost one), gets None with
a one-line notice when the `mcp` extra is missing, and otherwise passes
`.lifespan` to `FastAPI(...)` and calls `.mount_on(app)` afterwards.

The MCP server wraps the SAME `MapsApi` instance the host serves: switching
the runtime mode through an MCP tool switches it for the UI session too,
which is correct — there is one mode, not one per protocol.

Remote use follows the established pattern: a cloudflared tunnel in front of
`/mcp`. The SDK's DNS-rebinding protection only admits localhost Host
headers by default; a tunneled deployment sets
`AGENTIC_MAPS_MCP_ALLOWED_HOSTS` to a comma-separated Host allowlist, or
`*` to disable the check (the tunnel then being the access boundary).
"""

import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only, never a runtime import
    from fastapi import FastAPI

    from ..rest.maps_api import MapsApi

MCP_ALLOWED_HOSTS_ENV = "AGENTIC_MAPS_MCP_ALLOWED_HOSTS"
MCP_HTTP_PATH = "/mcp"


@dataclass
class HttpMcp:
    """A prepared /mcp mount: the ready-made routes plus the lifespan that
    runs their session manager. Not a pydantic model — infrastructure
    wiring, not domain data."""

    routes: list
    lifespan: Callable[[Any], contextlib.AbstractAsyncContextManager[None]]

    def mount_on(self, app: "FastAPI") -> None:
        # The SDK's own Route objects go straight onto the host router — NOT
        # `app.mount("/mcp", sub_app)`: a Mount only matches "/mcp/" and
        # relies on a trailing-slash redirect for the bare path, and on a
        # host with a catch-all static mount at "/" that redirect never
        # happens (the static app wins the bare "/mcp" and answers 405).
        app.router.routes.extend(self.routes)


def _transport_security():
    """None (SDK default: localhost-only Host headers) unless the env knob
    opens it up for tunneled deployments."""
    raw = os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").strip()
    if not raw:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    if raw == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [host.strip() for host in raw.split(",") if host.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts + ["127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"https://{host}" for host in hosts]
        + ["http://127.0.0.1:*", "http://localhost:*"],
    )


def prepare_http_mcp(api: "MapsApi") -> HttpMcp | None:
    """Build the /mcp mount, or None (with a notice) without the extra."""
    try:
        import mcp  # noqa: F401 - feature probe only
    except ImportError:
        print("[maps] mcp SDK not installed — MCP endpoint not mounted at "
              "/mcp. Install the mcp extra (pip install 'agentic-maps[mcp]').")
        return None

    from .server import build_mcp_server

    server = build_mcp_server(api)
    # The Starlette app the SDK builds is only a carrier here: its routes
    # (one endpoint at exactly /mcp) are grafted onto the host app, and its
    # lifespan (the session manager) is re-run by the host's own lifespan —
    # a mounted sub-app's lifespan would never fire.
    carrier = server.streamable_http_app(
        streamable_http_path=MCP_HTTP_PATH,
        transport_security=_transport_security(),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    print(f"[maps] MCP endpoint mounted at {MCP_HTTP_PATH} (streamable HTTP)")
    return HttpMcp(routes=list(carrier.routes), lifespan=lifespan)
