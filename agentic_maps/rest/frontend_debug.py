"""Remote debugging of the map frontend, over llming-com.

The problem this solves is concrete: the map renders in someone else's
browser, on their GPU, at their device pixel ratio — and several of the bugs
in this subproject (the shield 9-patch, the globe silhouette, the label pile-up)
could not be reproduced headlessly because the environment was the variable.
Screenshots pinned down *what* was wrong; this pins down *why*.

An attached browser reports its console, its uncaught errors, and the live
state of the map and the globe. An agent reads that over the HTTP debug API
that llming-com already provides — API-key protected, IP-restrictable — and
can ask a specific session to evaluate an expression in the page.

**Off unless `AGENTIC_MAPS_DEBUG=1`.** Arbitrary evaluation in someone's browser
is exactly as dangerous as it sounds; it is opt-in, key-protected, and never
implicit. The same posture llming-stage takes for its process introspection.
"""

import asyncio
import json
import os
import time
import uuid
from typing import Any

DEBUG_ENV = "AGENTIC_MAPS_DEBUG"
API_KEY_ENV = "AGENTIC_MAPS_DEBUG_KEY"
# How long an evaluate/state request waits for the browser to answer. A page
# that is mid-render can be slow; a page that has gone away must not hang the
# agent forever.
REQUEST_TIMEOUT_S = 8.0
# Console lines kept per session. Enough to hold the interesting burst around
# a failure, small enough that a chatty page cannot exhaust memory.
CONSOLE_RING = 400


def is_enabled() -> bool:
    return os.environ.get(DEBUG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class BrowserSession:
    """One attached page: its console ring and its pending round-trips."""

    def __init__(self, session_id: str, websocket: Any, user_agent: str = ""):
        self.session_id = session_id
        self.websocket = websocket
        self.user_agent = user_agent
        self.connected_at = time.time()
        self.last_activity = self.connected_at
        self.url = ""
        self.console: list[dict] = []
        self.errors: list[dict] = []
        self._pending: dict[str, asyncio.Future] = {}

    def note(self, kind: str, payload: dict) -> None:
        self.last_activity = time.time()
        bucket = self.errors if kind == "error" else self.console
        bucket.append({"at": self.last_activity, **payload})
        del bucket[:-CONSOLE_RING]

    async def ask(self, kind: str, **fields: Any) -> Any:
        """Round-trip a request to the page and wait for its reply."""
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.websocket.send_text(
                json.dumps({"type": kind, "id": request_id, **fields})
            )
            return await asyncio.wait_for(future, REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise TimeoutError(f"session {self.session_id} did not answer {kind} in time")
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, value: Any) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(value)

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "url": self.url,
            "user_agent": self.user_agent,
            "connected_at": self.connected_at,
            "last_activity": self.last_activity,
            "console_lines": len(self.console),
            "errors": len(self.errors),
        }


class FrontendDebug:
    """Registry of attached browsers plus the routes that expose them."""

    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}

    def _require_key(self, request: Any) -> None:
        from fastapi import HTTPException

        expected = os.environ.get(API_KEY_ENV, "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail=f"frontend debug needs {API_KEY_ENV} set (no key, no access)",
            )
        if request.headers.get("x-debug-key", "") != expected:
            raise HTTPException(status_code=401, detail="bad or missing X-Debug-Key")

    def _session(self, session_id: str) -> BrowserSession:
        from fastapi import HTTPException

        session = self.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"no attached session {session_id}")
        return session

    def mount(self, app: Any, *, prefix: str = "/api/v1/maps/debug") -> None:
        from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect

        @app.get(f"{prefix}/enabled")
        async def enabled() -> dict:
            """Unauthenticated on purpose: it reveals one boolean, and the page
            needs it to decide whether to open a socket at all."""
            return {"enabled": True}

        @app.websocket(f"{prefix}/ws")
        async def attach(websocket: WebSocket) -> None:
            await websocket.accept()
            session_id = uuid.uuid4().hex[:12]
            session = BrowserSession(
                session_id, websocket, websocket.headers.get("user-agent", "")
            )
            self.sessions[session_id] = session
            await websocket.send_text(json.dumps({"type": "hello", "session_id": session_id}))
            try:
                while True:
                    message = json.loads(await websocket.receive_text())
                    kind = message.get("type")
                    if kind == "reply":
                        session.resolve(message.get("id", ""), message.get("value"))
                    elif kind in ("console", "error"):
                        session.note(kind, message)
                    elif kind == "location":
                        session.url = message.get("url", "")
            except WebSocketDisconnect:
                pass
            finally:
                self.sessions.pop(session_id, None)

        @app.get(f"{prefix}/sessions")
        async def list_sessions(request: Request) -> dict:
            """Which browsers are attached right now."""
            self._require_key(request)
            return {"sessions": [s.summary() for s in self.sessions.values()]}

        @app.get(f"{prefix}/sessions/{{session_id}}/console")
        async def console(request: Request, session_id: str, limit: int = 100) -> dict:
            """Recent console output and uncaught errors from that page."""
            self._require_key(request)
            session = self._session(session_id)
            return {
                "console": session.console[-limit:],
                "errors": session.errors[-limit:],
            }

        @app.get(f"{prefix}/sessions/{{session_id}}/state")
        async def state(request: Request, session_id: str) -> dict:
            """Live map/globe state: camera, view, sources, layer counts."""
            self._require_key(request)
            try:
                return await self._session(session_id).ask("state")
            except TimeoutError as error:
                raise HTTPException(status_code=504, detail=str(error))

        @app.post(f"{prefix}/sessions/{{session_id}}/evaluate")
        async def evaluate(request: Request, session_id: str) -> dict:
            """Evaluate an expression in the page and return its value.

            The dangerous one, and the reason the whole surface is opt-in.
            """
            self._require_key(request)
            body = await request.json()
            expression = (body or {}).get("expression", "")
            if not expression:
                raise HTTPException(status_code=400, detail="expression is required")
            try:
                return {"value": await self._session(session_id).ask(
                    "evaluate", expression=expression)}
            except TimeoutError as error:
                raise HTTPException(status_code=504, detail=str(error))
