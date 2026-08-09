"""The 2-minute geo server: plan, write and (optionally) launch a local
Docker stack — tile serving (this package), routing (self-hosted Valhalla)
and geocoding (self-hosted Nominatim) — scoped to one region.

`planner.py` is the shared brain (pure logic, unit-testable without Docker);
`pbf_fetch.py` is the one real I/O side effect it delegates to (cutting a
city-sized OSM PBF extract); `wizard.py` is the CLI front-end
(`agentic-maps setup`); `web/apps/setup-wizard/` is the web front-end, both
calling the same planner through `rest/setup_api.py`.
"""
