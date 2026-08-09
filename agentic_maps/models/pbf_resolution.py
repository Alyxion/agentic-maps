from pydantic import BaseModel

from .bbox_deg import BBoxDeg


class PbfResolution(BaseModel):
    """How the region's OSM PBF extract was (or will be) obtained.

    Three honest outcomes (see `setup/planner.choose_pbf_strategy`):

    - `"overpass"`: small enough (a town/city bbox) to cut with the Overpass
      API's `/api/map` bbox export + `osmium` — fully automatic. Ends up
      with `path` set (a local file `pbf_fetch.fetch_city_pbf` already
      wrote), not `url`.
    - `"user-url"` / `"user-path"`: the caller supplied a PBF directly (e.g.
      a Geofabrik regional/country extract URL) — there is no on-demand
      city-scoped extract service with a real, scriptable, unauthenticated
      API (Geofabrik stops at country/state granularity; BBBike's extract
      service is a web form with a 2-7 minute queue and no documented API),
      so anything bigger than `overpass`'s reach needs this.
    - `"manual-required"`: the bbox is too big for Overpass and no override
      was given — `note` explains why and what to do.
    """

    method: str
    url: str = ""
    path: str = ""
    bbox: BBoxDeg
    note: str
