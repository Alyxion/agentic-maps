"""stdio entry point: `agentic-maps-mcp` — what a claude-code/desktop MCP
config points at.

Constructs the same world the dev server would (bundles under the repo's
`var/bundles` by default, runtime mode from `AGENTIC_MAPS_MODE`) and serves
the toolset over stdio. Safe against stray prints: the SDK's stdio transport
claims fd 1 and diverts non-protocol output to stderr while serving.

`render_map` needs a real HTTP origin for the headless-Chromium page it
drives (`render/service.py` — the page fetches `/render.html` and its tiles
over HTTP; there is no file:// mode). Over stdio no host server exists, so
when uvicorn is importable a loopback helper server is started on an
ephemeral port serving the SAME MapsApi + the static `web/` bundle, and the
render base URL points at it. Without uvicorn (or when
`AGENTIC_MAPS_RENDER_BASE_URL` names an already-running instance) the env
var/default applies unchanged.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path


def _notice(text: str) -> None:
    """Operator notices go to stderr, always: before `run_stdio_async` claims
    fd 1, stdout IS the protocol wire and a single stray line would land in
    the client's JSON-RPC parser."""
    print(text, file=sys.stderr)


def _build_api(bundles_dir: Path, mode: str):
    from ..models.runtime_mode import RUNTIME_MODES
    from ..rest.maps_api import MapsApi

    runtime_mode = mode if mode in RUNTIME_MODES else "online"
    # standalone=False: no browser session belongs to a stdio server, so
    # nothing should ever ask a visitor for geolocation.
    return MapsApi(bundles_dir, mode=runtime_mode, standalone=False)


async def _start_render_host(api) -> "tuple[object, asyncio.Task] | None":
    """Loopback HTTP origin for render_map, when uvicorn is available.

    Ephemeral port on 127.0.0.1 — the OS picks it, we read it back and hand
    it to `MapsApi.render_base_url`. Skipped entirely when the operator
    already pointed `AGENTIC_MAPS_RENDER_BASE_URL` at a running instance.
    """
    from ..rest.maps_api import RENDER_BASE_URL_ENV

    if os.environ.get(RENDER_BASE_URL_ENV, "").strip():
        return None
    try:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
    except ImportError:
        _notice("[maps-mcp] uvicorn/fastapi not fully available — render_map "
                "will need AGENTIC_MAPS_RENDER_BASE_URL pointing at a running "
                "instance.")
        return None

    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
    app = FastAPI(title="agentic-maps mcp render host")
    api.mount(app)
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():          # bind failed; surface instead of spinning
            task.result()
            return None
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    api.render_base_url = f"http://127.0.0.1:{port}"
    _notice(f"[maps-mcp] render host on {api.render_base_url}")
    return server, task


async def _serve(bundles_dir: Path, mode: str) -> None:
    from .server import build_mcp_server

    api = _build_api(bundles_dir, mode)
    server = build_mcp_server(api)
    render_host = await _start_render_host(api)
    try:
        await server.run_stdio_async()
    finally:
        if render_host is not None:
            # Graceful uvicorn shutdown, not task.cancel(): cancelling
            # mid-lifespan prints a CancelledError traceback on every exit.
            uvicorn_server, task = render_host
            uvicorn_server.should_exit = True
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


def main() -> None:
    # Same var/bundles convention the dev server uses (devserver.py's
    # _DEFAULT_BUNDLES_DIR) without importing that module — it pulls FastAPI
    # in at import time and prints demo-page chrome this entry never needs.
    default_bundles = Path(__file__).resolve().parent.parent.parent / "var" / "bundles"
    parser = argparse.ArgumentParser(description="agentic-maps MCP server (stdio)")
    parser.add_argument("--bundles-dir", type=Path, default=default_bundles)
    parser.add_argument(
        "--mode",
        default=os.environ.get("AGENTIC_MAPS_MODE", "").strip().lower() or "online",
        help="runtime mode: offline | mixed | online (default: "
             "AGENTIC_MAPS_MODE if set, else online)",
    )
    args = parser.parse_args()
    args.bundles_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_serve(args.bundles_dir, args.mode))


if __name__ == "__main__":
    main()
