"""The sealed runtime's route lookup, exercised in a real JS engine.

`web/sealed-runtime.js` runs in a browser inside a sealed session, where it
is awkward to test — but it is plain ES5 in an IIFE, so Node can load it with
a two-line stand-in for `window`. That is worth doing for exactly one
behaviour:

**a route that is not in the bundle is refused, and SAYS SO.** Routes are
keyed by their coordinates, which is right — a near-enough substitute would
draw a line starting somewhere the page does not. But pages treat a failed
route as "draw nothing", so without a record of the refusal a stale
recording produces a map that comes up silently missing its routes. Keeping
a recording in step with the page it seals is whatever produces the sealed
file's job; saying what was denied is this file's.

It also proves the Python (`agentic_maps.seal.recorder.route_key`) and
JavaScript (`agenticSealed.routeKey`) key derivations agree byte for byte —
the two languages do not print floats or default a missing `mode` the same
way unless both sides are deliberate about it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_maps.seal.recorder import route_key

RUNTIME = Path(__file__).resolve().parent.parent / "web" / "sealed-runtime.js"

SITE = {"lat": 53.6003, "lon": 10.0689}
SITE_MOVED = {"lat": 53.5991, "lon": 10.0686}          # ~135 m south
HBF = {"lat": 53.55298, "lon": 10.00676}
AIRPORT = {"lat": 53.63035, "lon": 10.00647}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _lookup(requests: list[dict]) -> dict:
    """Run a series of route lookups against a two-route bundle in Node."""
    harness = """
    const fs = require('fs');
    const src = fs.readFileSync(process.argv[1], 'utf8');
    const win = { atob: (s) => Buffer.from(s, 'base64').toString('binary'),
                  console: { warn() {} } };
    new Function('window', src)(win);

    const routes = [
      { key: win.agenticSealed.routeKey({mode: 'car', start: SITE, end: HBF}),
        request: {route_id: 'hbf', mode: 'car', start: SITE, end: HBF},
        response: {id: 'hbf'} },
      { key: win.agenticSealed.routeKey({mode: 'car', start: SITE, end: AIRPORT}),
        request: {route_id: 'airport', mode: 'car', start: SITE, end: AIRPORT},
        response: {id: 'airport'} },
    ];
    const store = new win.agenticSealed.Store({index: {}, media_types: [], data: '', routes});
    const out = REQUESTS.map((body) => {
      const hit = store.route(body);
      return hit ? hit.id : null;
    });
    console.log(JSON.stringify({found: out, misses: store.misses}));
    """
    script = (harness
              .replace("SITE", json.dumps(SITE), 1)
              .replace("HBF", json.dumps(HBF), 1)
              .replace("SITE", json.dumps(SITE), 1)
              .replace("AIRPORT", json.dumps(AIRPORT), 1)
              .replace("SITE", json.dumps(SITE))
              .replace("HBF", json.dumps(HBF))
              .replace("AIRPORT", json.dumps(AIRPORT))
              .replace("REQUESTS", json.dumps(requests)))
    result = subprocess.run(["node", "-e", script, str(RUNTIME)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _js_route_key(body: dict) -> str:
    harness = """
    const fs = require('fs');
    const src = fs.readFileSync(process.argv[1], 'utf8');
    const win = { atob: (s) => Buffer.from(s, 'base64').toString('binary'),
                  console: { warn() {} } };
    new Function('window', src)(win);
    console.log(win.agenticSealed.routeKey(BODY));
    """
    script = harness.replace("BODY", json.dumps(body))
    result = subprocess.run(["node", "-e", script, str(RUNTIME)],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


# -- lookup behaviour -------------------------------------------------------


def test_an_unchanged_route_is_found_by_its_exact_key():
    out = _lookup([{"route_id": "hbf", "mode": "car", "start": SITE, "end": HBF}])
    assert out["found"] == ["hbf"]
    assert out["misses"] == []                      # nothing to report


def test_a_nudged_site_loses_its_routes_and_the_session_records_that():
    """A pin moved 135 m re-keys every route — and the session must not be
    quiet about it. The routes are genuinely gone: nothing near-enough is
    substituted, because a substituted line would start where the page no
    longer is. What must NOT happen is losing them silently, so each
    refusal lands in `misses` where a verification harness fails on it."""
    out = _lookup([
        {"route_id": "hbf", "mode": "car", "start": SITE_MOVED, "end": HBF},
        {"route_id": "airport", "mode": "car", "start": SITE_MOVED, "end": AIRPORT},
    ])
    assert out["found"] == [None, None]
    assert [m["kind"] for m in out["misses"]] == ["route", "route"]
    # The key names the coordinate it looked for — which is the diagnosis.
    assert "53.599100" in out["misses"][0]["what"]


def test_a_different_destination_is_never_substituted():
    munich = {"lat": 48.1372, "lon": 11.5756}
    out = _lookup([{"route_id": "muenchen", "mode": "car",
                    "start": SITE, "end": munich}])
    assert out["found"] == [None]
    assert out["misses"][0]["kind"] == "route"


def test_mode_is_part_of_the_identity():
    """A walking route is not a driving route, however equal the endpoints."""
    out = _lookup([{"route_id": "hbf", "mode": "walk", "start": SITE, "end": HBF}])
    assert out["found"] == [None]
    assert out["misses"][0]["kind"] == "route"


# -- Python/JS key parity ----------------------------------------------------


@pytest.mark.parametrize("body", [
    {"mode": "car", "start": SITE, "end": HBF},
    {"start": SITE, "end": HBF},                            # mode omitted -> "car" both sides
    {"mode": "walk", "start": SITE, "end": HBF, "via": [AIRPORT]},
    {"mode": "car", "start": SITE, "end": HBF, "avoid": ["toll", "ferry"]},
    {"mode": "car", "start": SITE, "end": HBF, "steps": True, "alternates": 2},
])
def test_python_and_javascript_derive_the_identical_key(body):
    assert route_key(body) == _js_route_key(body)
