# agentic-maps — agent notes

- Standalone product: an offline-capable, legally-licensed mapping platform
  (tiles, routing, rendering). Isolated dev server on :8095. Concept:
  `docs/concept.md`.
- One pydantic model per file under `agentic_maps/models/`; access domain data
  through typed models, never raw dict string keys.
- Core stays framework-free: FastAPI only under `rest/` + `devserver.py`
  (optional extras). Heavy/optional imports live inside functions.
- Integration surface is a class with `mount(app, *, prefix)`, so a host
  application can bring its own auth/scopes policy around it later.
- Licensing is a hard constraint: only sources whose license allows bulk
  download may become `TileSource` presets (`sources/presets.py`); every preset
  carries attribution + license fields and they must be rendered on-map. Never
  add `tile.openstreetmap.org` or commercial-API scraping.
- Tile scheme is XYZ web-mercator; MBTiles stores TMS rows — the flip is
  handled inside `storage/mbtiles.py` only, everything else speaks XYZ.
- Frontend: `web/` with vendored MapLibre (`web/vendor/`), no CDN calls; the
  runtime mounts `[data-agentic-map]` elements from a JSON payload.
- `web/sealed-host.js` and `web/sealed-runtime.js` are the "Sealed Sessions"
  frontend, backed by `agentic_maps/seal/` (`SessionRecorder`, `Sealer`,
  `PageSealer`) and driven by `tools/seal_page.py` — see
  `docs/sealed-sessions.md` for the readiness/step contract a page
  implements to become sealable and the `amap://` scheme sealed pages run on.
