"""OpenFIGI provider — cross-vendor security identity (US Composite FIGI).

Per v2.2 spec §1.8-§1.10. OpenFIGI integration is EVENT-DRIVEN (invoked
by `parent_resolver` on `UNKNOWN_TICKER_OBSERVED`), NOT a scheduled feed.

Public surface: `OpenFIGIAdapter.map_tickers([tickers])` returns one
`OpenFIGIResult` per ticker with `composite_figi`, `share_class_figi`,
`exchange_figi`, plus the cross-vendor `name`/`security_type`/`market_sector`.

For our system the `composite_figi` is the canonical per-jurisdiction
identifier we store on `platform.ticker_classifications.figi`.
"""
from __future__ import annotations

from tpcore.openfigi.figi_adapter import (
    OPENFIGI_BASE_URL,
    OPENFIGI_FIGI_REGEX,
    OpenFIGIAdapter,
    OpenFIGIResult,
)

__all__ = [
    "OPENFIGI_BASE_URL",
    "OPENFIGI_FIGI_REGEX",
    "OpenFIGIAdapter",
    "OpenFIGIResult",
]
