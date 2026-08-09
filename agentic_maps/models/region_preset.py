from pydantic import BaseModel

from .bbox_deg import BBoxDeg


class RegionPreset(BaseModel):
    """One named region the bulk-provisioning engine knows how to fill.

    The preset carries everything an estimate or a job needs that is not
    derivable: the bounding box the tile math runs over, the Geofabrik PBF
    for the routing layer (with its size as measured via HTTP HEAD — see
    `provision/estimates.py` for the measurement date), and which layers are
    offered at all (`earth` is aerial-only: a planet-wide street extract or
    routing graph is not something this engine should pretend to provide).
    """

    id: str
    name: str
    bbox: BBoxDeg
    # Geofabrik download URL for the routing layer; empty when the preset
    # does not offer routing (earth) — custom bboxes resolve their PBF
    # through the setup planner's Overpass path instead.
    geofabrik_url: str = ""
    # Content-Length of `geofabrik_url` as last measured (bytes). An estimate
    # input, not a contract — Geofabrik extracts grow week over week.
    pbf_bytes: int = 0
    # Estimated size of the z0-15 vector street extract (bytes), anchored on
    # the real Germany extract and scaled by area for the other presets.
    vector_bytes: int = 0
    layers: list[str] = ["maps", "aerial", "routing"]
