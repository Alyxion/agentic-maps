"""Sealed Sessions: freeze a live map session into one offline-replayable bundle.

See `docs/sealed-sessions.md` for the readiness/step contract a page
implements to become sealable, and `tools/seal_page.py` for the CLI that
drives this package end to end (record, then seal).
"""

from .page_seal import PageSealer
from .recorder import SessionRecorder, route_key
from .sealer import Sealer, bundle_json, sealed_url, trim_worldwide

__all__ = [
    "PageSealer",
    "Sealer",
    "SessionRecorder",
    "bundle_json",
    "route_key",
    "sealed_url",
    "trim_worldwide",
]
