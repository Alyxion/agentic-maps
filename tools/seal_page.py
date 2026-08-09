"""Record a page's live traffic and seal it into a standalone offline bundle.

Sealing is a two-step, browser-driven batch job — record every state a page
can reach against a LIVE host, then seal (fetch back exactly those recorded
keys and compute per-zoom attribution) — not a per-request call an
application makes mid-flow. That is why this lives here, alongside the other
`tools/verify_*.py` Playwright-driven scripts, rather than as a `POST /seal`
endpoint on `MapsApi`:

- it needs a real headless browser to drive a page through a multi-step
  choreography end to end (`agentic_maps.seal.recorder.SessionRecorder`),
  exactly the shape `tools/verify_render.py`/`verify_offline.py` already
  have, and unlike `POST /render`'s one quick screenshot this can run for
  minutes across several viewport variants — the wrong latency for a
  synchronous HTTP request;
- the reference implementation this was ported from (`llming_maps.capture`
  in the private reference codebase this was ported from) was itself
  driven the same way, as a plain module call from an export pipeline, never
  wired into that project's own REST API either;
- the SEALER half (`agentic_maps.seal.sealer.Sealer`) rides on MapsApi's
  EXISTING REST surface (`/api/v1/maps/live/...`, `/vector/...`, the demo
  spec endpoint) exactly like a browser tab would — no new server route is
  needed for it, sealing is just a disciplined HTTP client plus Playwright.

Usage:
    python tools/seal_page.py /view.html?lat=53.55&lon=9.99&z=12 \\
        --label hamburg-kiosk --out var/sealed/hamburg.json

    # A page with window.__agenticSealSteps (docs/sealed-sessions.md) —
    # drive at least 3 of its own declared states:
    python tools/seal_page.py /view.html?imgstep=1 --steps 3 --out var/sealed/plan.json

Requires a running dev server (`agentic-maps-dev`, default :8095) and a real
Chromium binary available to Playwright (`playwright install chromium`, or
`AGENTIC_MAPS_RENDER_CHROMIUM_PATH` pointed at one already on disk — the same
env var `agentic_maps/render/service.py` reads).
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from agentic_maps.models.embed_shot import EmbedShot
from agentic_maps.seal.recorder import SessionRecorder
from agentic_maps.seal.sealer import Sealer, bundle_json

DEFAULT_HOST = "http://127.0.0.1:8095"
CHROMIUM_PATH_ENV = "AGENTIC_MAPS_RENDER_CHROMIUM_PATH"


async def seal_page(
    host: str,
    url: str,
    *,
    label: str,
    width: int,
    height: int,
    steps: int,
    asset_prefixes: tuple[str, ...],
    spec_path: str,
    chromium_executable_path: str | None,
):
    recorder = SessionRecorder(
        host, asset_prefixes=asset_prefixes,
        chromium_executable_path=chromium_executable_path,
    )
    report = await recorder.record(
        [EmbedShot(label=label, url=url, width=width, height=height, steps=steps)]
    )
    sealer = Sealer(host, spec_path=spec_path)
    bundle = await sealer.seal(report)
    return report, bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="page path (+ query) to seal, e.g. /view.html?lat=53.55&lon=9.99&z=12")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--label", default=None, help="defaults to the url")
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=600)
    parser.add_argument("--steps", type=int, default=1,
                        help="floor on how many window.__agenticSealSteps entries to drive")
    parser.add_argument("--asset-prefix", action="append", default=[],
                        help="extra same-host path prefix to record verbatim "
                             "(repeatable) — see SessionRecorder's own docstring")
    parser.add_argument("--spec-path", default="/api/demo-spec",
                        help="where to GET the live MapPayload to seal (Sealer's spec_path)")
    parser.add_argument("--out", default="", help="defaults to var/sealed/<page>.json")
    parser.add_argument("--chromium-path", default=os.environ.get(CHROMIUM_PATH_ENV, "").strip() or None)
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path("var/sealed") / (
        Path(args.url.split("?")[0]).stem + ".json"
    )
    report, bundle = asyncio.run(seal_page(
        args.host, args.url,
        label=args.label or args.url,
        width=args.width, height=args.height, steps=args.steps,
        asset_prefixes=tuple(args.asset_prefix),
        spec_path=args.spec_path,
        chromium_executable_path=args.chromium_path,
    ))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(bundle_json(bundle))

    summary = {
        "out": str(out),
        "recorded_keys": len(report.keys),
        "recorded_routes": len(report.routes),
        "viewport_variants": report.viewport_variants,
        "missing": report.missing[:20],
        "bundle_byte_size": bundle.byte_size,
        "attribution": bundle.attribution,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not report.missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
