"""greeks.pro options-analytics adapter (free-tier max-pain).

Free tier (``GREEKS_API_KEY``) exposes only ``/api/analytics/maxpain``
(10 req/min, 600 req/day, 1 symbol). ``/flow``, ``/greeks``, ``/gex``
are Trader+ (paid) and return 403 on free — verified 2026-05-16, not
fabricated. This package therefore ingests max-pain only.
"""
from __future__ import annotations

from .adapter import (
    GREEKS_MAXPAIN_ENV,
    GreeksProAdapter,
    MaxPainResult,
    MaxPainSnapshot,
)

__all__ = [
    "GREEKS_MAXPAIN_ENV",
    "GreeksProAdapter",
    "MaxPainResult",
    "MaxPainSnapshot",
]
