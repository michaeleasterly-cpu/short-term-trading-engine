"""FRED (Federal Reserve Economic Data) adapter.

Free public REST API at https://api.stlouisfed.org/fred/ requiring a
free no-rate-limit API key. Ingests macro time-series into
``platform.macro_indicators`` — Sahm Rule, industrial production
(PMI proxy), initial claims, yield curve, HY credit spread.

Built 2026-05-14 as the last data source from MASTER_PLAN §6.1.
Reference implementation: ``tpcore.sec.SECEdgarAdapter``.
"""

from .adapter import INDICATOR_SERIES, FREDAdapter
from .diffusion import DEFAULT_SPAN_MONTHS, compute_sos_diffusion

__all__ = [
    "DEFAULT_SPAN_MONTHS",
    "FREDAdapter",
    "INDICATOR_SERIES",
    "compute_sos_diffusion",
]
