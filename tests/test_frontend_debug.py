"""The remote debug surface is opt-in, key-protected, and silent when off.

Arbitrary evaluation in someone else's browser is exactly as dangerous as it
sounds. These pin the gates rather than the plumbing: that nothing is mounted
without the env flag, that no route answers without the key, and that a missing
key is a refusal rather than an open door.
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_maps.devserver import mount_frontend_debug
from agentic_maps.rest.frontend_debug import API_KEY_ENV, DEBUG_ENV, FrontendDebug, is_enabled


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(DEBUG_ENV, raising=False)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    return monkeypatch


def test_disabled_by_default(clean_env):
    assert is_enabled() is False
    app = FastAPI()
    assert mount_frontend_debug(app) is False
    client = TestClient(app)
    # The probe endpoint answers in EVERY configuration — each page asks it
    # once at load, and a 404 here put two console errors on every page view.
    response = client.get("/api/v1/maps/debug/enabled")
    assert response.status_code == 200
    assert response.json() == {"enabled": False}
    # The actual debug surface stays absent: no sessions API, no websocket.
    assert client.get("/api/v1/maps/debug/sessions").status_code == 404


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_enabled_by_the_usual_truthy_spellings(clean_env, value):
    clean_env.setenv(DEBUG_ENV, value)
    assert is_enabled() is True


def test_without_a_key_nothing_is_readable(clean_env):
    clean_env.setenv(DEBUG_ENV, "1")
    app = FastAPI()
    FrontendDebug().mount(app)
    client = TestClient(app)

    # The page's own probe stays open: it discloses one boolean and the bridge
    # needs it to decide whether to open a socket.
    assert client.get("/api/v1/maps/debug/enabled").json() == {"enabled": True}
    # Everything else refuses while no key is configured.
    assert client.get("/api/v1/maps/debug/sessions").status_code == 503


def test_wrong_key_is_rejected(clean_env):
    clean_env.setenv(DEBUG_ENV, "1")
    clean_env.setenv(API_KEY_ENV, "the-real-key")
    app = FastAPI()
    FrontendDebug().mount(app)
    client = TestClient(app)

    assert client.get("/api/v1/maps/debug/sessions").status_code == 401
    assert client.get("/api/v1/maps/debug/sessions",
                      headers={"X-Debug-Key": "guess"}).status_code == 401
    ok = client.get("/api/v1/maps/debug/sessions", headers={"X-Debug-Key": "the-real-key"})
    assert ok.status_code == 200 and ok.json() == {"sessions": []}


def test_evaluate_needs_an_expression_and_a_live_session(clean_env):
    clean_env.setenv(DEBUG_ENV, "1")
    clean_env.setenv(API_KEY_ENV, "k")
    app = FastAPI()
    FrontendDebug().mount(app)
    client = TestClient(app)
    key = {"X-Debug-Key": "k"}

    assert client.post("/api/v1/maps/debug/sessions/nope/evaluate",
                       headers=key, json={}).status_code == 400
    assert client.post("/api/v1/maps/debug/sessions/nope/evaluate",
                       headers=key, json={"expression": "1+1"}).status_code == 404


def test_a_page_attaches_and_answers(clean_env):
    """The round trip: browser connects, agent asks, browser replies."""
    clean_env.setenv(DEBUG_ENV, "1")
    clean_env.setenv(API_KEY_ENV, "k")
    app = FastAPI()
    surface = FrontendDebug()
    surface.mount(app)
    client = TestClient(app)
    key = {"X-Debug-Key": "k"}

    with client.websocket_connect("/api/v1/maps/debug/ws") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        session_id = hello["session_id"]

        socket.send_json({"type": "location", "url": "http://host/#@1,2,3z"})
        socket.send_json({"type": "console", "level": "warn", "text": "something odd"})
        socket.send_json({"type": "error", "text": "boom", "line": 42})

        listed = client.get("/api/v1/maps/debug/sessions", headers=key).json()["sessions"]
        assert [s["session_id"] for s in listed] == [session_id]
        assert listed[0]["url"] == "http://host/#@1,2,3z"

        log = client.get(f"/api/v1/maps/debug/sessions/{session_id}/console",
                         headers=key).json()
        assert log["console"][-1]["text"] == "something odd"
        assert log["errors"][-1]["text"] == "boom"

    # Disconnecting deregisters, so a stale session is never offered to an agent.
    assert client.get("/api/v1/maps/debug/sessions", headers=key).json() == {"sessions": []}
