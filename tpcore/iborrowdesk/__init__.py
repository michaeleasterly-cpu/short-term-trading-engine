"""IBorrowDesk borrow-rate adapter (no auth, scrape-fragile).

``www.iborrowdesk.com/api/ticker/<SYM>`` (the ``www.`` host — bare
domain 301-redirects; verified 2026-05-16) → JSON with a ``daily``
array of ``{date, fee, available}``. ``fee`` is the borrow rate %.
Scraping is fragile: blocks (403/429) are handled via @with_retry;
3 consecutive failures → CRITICAL log + skip, never crash the pipeline.
"""
from __future__ import annotations

from .adapter import BorrowRateRecord, IBorrowDeskAdapter

__all__ = ["BorrowRateRecord", "IBorrowDeskAdapter"]
