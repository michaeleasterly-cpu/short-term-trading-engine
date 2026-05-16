"""FINRA Query API adapter — consolidated short interest.

OAuth2 client-credentials (FINRA_API_CLIENT_ID / FINRA_API_SECRET_KEY,
the real .env names — the task's FINRA_CLIENT_ID/SECRET were wrong).
Official, free, bi-monthly. FINRA gives short *shares* + days-to-cover
but NOT float, so `short_interest_pct` is derived downstream from
`fundamentals_quarterly.shares_outstanding` (reuse existing provider).
"""
from __future__ import annotations

from .adapter import FINRA_CLIENT_ID_ENV, FINRA_SECRET_ENV, FinraAdapter, ShortInterestRecord

__all__ = [
    "FINRA_CLIENT_ID_ENV",
    "FINRA_SECRET_ENV",
    "FinraAdapter",
    "ShortInterestRecord",
]
