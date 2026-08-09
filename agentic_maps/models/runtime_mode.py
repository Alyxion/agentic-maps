from typing import Literal

# The three states `MapsApi` (rest/maps_api.py) and the setup wizard
# (setup/planner.py's own SetupMode — kept separate on purpose, see its
# docstring) both speak:
#
#   "offline": every network-touching endpoint refuses (403) — presentation
#     mode, tiles/glyphs/vectors serve exclusively from bundles/cache.
#   "mixed": live, per-request, non-provisioning actions are allowed
#     (routing, geocoding, the live tile proxy, one-shot boundary GeoJSON,
#     glyphs/sprites) but bulk-provisioning actions stay blocked (harvest,
#     harvest-world, vector/extract) — authoring with a live map, without
#     silently minting gigabytes of new offline data.
#   "online": everything is allowed, the historical default.
#
# A plain three-value Literal rather than an enum: it is consumed directly as
# a pydantic field type (`ModeState.mode`) and as a `Literal["offline", ...]`
# default value, both of which want the bare type, not an Enum wrapper.
RuntimeMode = Literal["offline", "mixed", "online"]

RUNTIME_MODES: tuple[RuntimeMode, ...] = ("offline", "mixed", "online")
