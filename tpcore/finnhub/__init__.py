"""Finnhub insider-sentiment adapter (free-tier MSPR).

Free tier (``FINNHUB_API_KEY``) exposes ``/stock/insider-sentiment``
(monthly MSPR + net insider share change per symbol). ``/news-sentiment``
and ``/stock/social-sentiment`` are premium and return 403 on free
(verified 2026-05-16) — not implemented (no fake against paywalled
endpoints).
"""
from __future__ import annotations

from .adapter import (
    FINNHUB_API_KEY_ENV,
    FinnhubAdapter,
    InsiderSentimentRecord,
    InsiderSentimentResult,
)

__all__ = [
    "FINNHUB_API_KEY_ENV",
    "FinnhubAdapter",
    "InsiderSentimentRecord",
    "InsiderSentimentResult",
]
