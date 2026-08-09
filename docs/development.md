# Development setup

## Core + REST + fallback (Python 3.11 works)

```
python3.14 -m venv .venv          # 3.11+ works for this section; see below for why 3.14
source .venv/bin/activate
pip install -e ".[rest,fallback]"
pip install pytest pytest-asyncio
pytest tests -q
```

This installs the core engine, the REST surface, and the raster fallback
ladder — everything the test suite needs. Any Python from 3.11 up is enough
for this much. The sealed-runtime JS parity tests
(`tests/test_sealed_runtime_js.py`) additionally need `node` on PATH and
skip cleanly without it.

To actually run `agentic-maps-dev` you additionally need `uvicorn`
(`pip install "uvicorn[standard]"`) — it is part of the `dev-server` extra,
not of `rest` (which is FastAPI only, mirroring how `deploy/Dockerfile`
installs `rest,fallback` and then adds uvicorn itself).

## The `dev-server` / `globe` / `debug` extras (Python 3.14 required)

These extras depend on `llming-stage` (the shared frontend vendor bundle for
the 3D globe and the icon set) and `llming-com` (a WebSocket debug bridge).
Both are on public PyPI — a plain install works:

```
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[rest,fallback,dev-server]"
```

(Contributors hacking on those two packages themselves can still swap in
editable installs from their own checkouts; nothing here requires it.)

**`llming-stage` requires Python >= 3.14** (`llming-com`: >= 3.12). This is
the trap to know about: a 3.11 venv installs `rest`/`fallback` fine and the
dev server starts — but `llming-stage` cannot be installed into it, so the
3D globe silently never loads (the dev server prints
"llming-stage not installed — the 3D globe will not load" and carries on
with the flat map). Use a 3.14 venv from the start if you want the
globe/dev-server/debug extras; 3.11 is only enough for core + `rest` +
`fallback`.

Without access to the sibling repo, everyone else develops against
`rest` + `fallback` — the flat map, routing, harvesting, sealing and the
whole test suite work without the vendor bundle.

## The `render` extra

The `render` extra (`POST /render`, `agentic_maps/render/`) needs Playwright
— both the Python package AND a real Chromium binary, which is a separate,
one-time download `pip install` does not perform:

```
pip install -e ".[rest,fallback,render]"
playwright install chromium
```

See `docs/rendering.md` for the endpoint itself. `tools/verify_render.py`
needs a running dev server AND that Chromium binary; it is not part of
`pytest tests -q` for the same reason the other `tools/verify_*.py` scripts
are not — a real browser is too heavy for the fast unit suite.
