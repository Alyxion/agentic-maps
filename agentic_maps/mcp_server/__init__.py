"""MCP surface for agentic-maps — the product's capabilities as agent tools.

Package name is `mcp_server` (not `mcp`) so it can never shadow the official
`mcp` SDK it builds on. Nothing is imported here at package level: the SDK is
an optional extra (`pip install agentic-maps[mcp]`), and every entry point
feature-detects it — `agentic_maps.mcp_server.http.prepare_http_mcp` for the
`/mcp` mount on the dev server, `agentic_maps.mcp_server.stdio.main` for the
`agentic-maps-mcp` stdio command.

Architecture: every tool is a thin adapter over the REST surface, called
through an IN-PROCESS ASGI client (`httpx.ASGITransport`) against a FastAPI
app mounting the same `MapsApi` instance. One source of truth for
validation, runtime-mode gating and attribution — no logic duplicated into
the tool layer, and no self-HTTP over sockets. All bytes (map renders) flow
through the server as MCP content; a client is never handed a third-party
URL to fetch.
"""
