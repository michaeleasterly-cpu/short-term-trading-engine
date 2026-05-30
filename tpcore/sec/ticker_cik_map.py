"""SEC ticker → CIK map (P0-001).

Authoritative SEC EDGAR file at
``https://www.sec.gov/files/company_tickers.json``:

    {
      "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
      "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
      ...
    }

Used to backfill CIK on ``platform.ticker_classifications`` rows where
the FMP-derived CIK is NULL. Provenance is recorded in the new
``cik_source`` column (this module sets ``cik_source='sec_ticker_map'``).

Safety:
  * NEVER overwrites a non-NULL CIK. SEC ticker reuse means a ticker
    string can map to different CIKs over time; we trust the
    operator's existing FMP-derived CIK as authoritative.
  * Records UNRESOLVED tickers explicitly (returned from
    ``resolve_missing_ciks``) so the operator can review.
  * SEC fair-use sleep (~0.5 s) between calls — this is ONE call per
    backfill run, so the impact is minimal; the constant is kept for
    consistency with other SEC adapters.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

import httpx
import structlog

logger = structlog.get_logger(__name__)


SEC_TICKER_MAP_URL: Final[str] = (
    "https://www.sec.gov/files/company_tickers.json"
)
SEC_USER_AGENT_ENV: Final[str] = "SEC_EDGAR_USER_AGENT"

_DEFAULT_TIMEOUT_SEC: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class TickerCIKEntry:
    """One row of the SEC ticker map."""

    ticker: str
    """SEC's normalized ticker string (upper-case, no .U/.W suffix
    differentiation — the SEC map only carries the base equity ticker
    for the issuer's common stock)."""

    cik: str
    """Zero-padded 10-digit CIK string (e.g. ``"0000320193"``)."""

    company_name: str | None = None
    """SEC's ``title`` field — the legal entity name. Used for
    operator review of ambiguous matches."""


@dataclass(slots=True)
class CIKResolveResult:
    """Outcome of ``resolve_missing_ciks`` for one batch of tickers."""

    resolved: dict[str, TickerCIKEntry] = field(default_factory=dict)
    """ticker → entry mapping for tickers the map covered."""

    unresolved: list[str] = field(default_factory=list)
    """tickers the SEC map does NOT cover (likely non-equity
    instruments, recently-delisted, or pink-sheet/OTC tickers SEC
    doesn't index in this public file)."""

    skipped_already_set: list[str] = field(default_factory=list)
    """tickers we did not touch because their CIK was already
    populated. NEVER overwritten — provenance preserved."""


class SECTickerCIKMap:
    """Async client for the SEC ticker→CIK map file.

    One-shot fetcher: the file is ~1.5 MB JSON. Cache the parsed map
    in memory for the lifetime of one backfill run. Re-fetch on the
    next operator-on-demand cycle.
    """

    def __init__(
        self,
        *,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._timeout = timeout_sec
        self._map: dict[str, TickerCIKEntry] | None = None

    @property
    def cached(self) -> dict[str, TickerCIKEntry] | None:
        """Returns the cached map (or None if not yet loaded). Tests
        + diagnostics use this; the public API is ``fetch`` /
        ``resolve_missing_ciks``."""
        return self._map

    async def fetch(self) -> dict[str, TickerCIKEntry]:
        """Pull the SEC ticker map and return a ticker → entry dict.

        Caches the result on the instance — subsequent calls return
        the cached map. Use a new instance to force re-fetch.

        Raises ``RuntimeError`` if SEC_USER_AGENT is not set (SEC
        rejects unauthenticated bulk fetches; same rule used by
        SECCompanyFactsAdapter).
        """
        if self._map is not None:
            return self._map
        ua = os.environ.get(SEC_USER_AGENT_ENV)
        if not ua:
            raise RuntimeError(
                f"{SEC_USER_AGENT_ENV} env var is required for SEC fetches"
                " — set to e.g. 'operator-name operator-email@domain'."
            )
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": ua, "Accept": "application/json"},
        ) as client:
            resp = await client.get(SEC_TICKER_MAP_URL)
            resp.raise_for_status()
            raw = resp.json()
        out: dict[str, TickerCIKEntry] = {}
        for _row_key, payload in raw.items():
            if not isinstance(payload, dict):
                continue
            ticker = (payload.get("ticker") or "").strip().upper()
            cik_int = payload.get("cik_str")
            if not ticker or cik_int is None:
                continue
            cik_padded = str(int(cik_int)).zfill(10)
            out[ticker] = TickerCIKEntry(
                ticker=ticker,
                cik=cik_padded,
                company_name=payload.get("title"),
            )
        logger.info(
            "sec.ticker_cik_map.fetched",
            ticker_count=len(out),
            url=SEC_TICKER_MAP_URL,
        )
        self._map = out
        return out

    async def resolve_missing_ciks(
        self,
        tickers: list[str],
        existing_ciks: dict[str, str | None],
    ) -> CIKResolveResult:
        """For each ticker in ``tickers`` whose CIK is missing in
        ``existing_ciks``, look up the SEC map and return resolutions.

        Args:
            tickers: full list of tickers under consideration.
            existing_ciks: ticker → current CIK (str or None). Used
                to gate the SEC lookup — already-CIK rows are left
                untouched (no overwrite).

        Returns:
            CIKResolveResult with three buckets: resolved (SEC had
            it), unresolved (SEC did NOT have it), and
            skipped_already_set (we did not even look — operator's
            existing CIK is preserved).
        """
        sec_map = await self.fetch()
        result = CIKResolveResult()
        for raw_t in tickers:
            t = (raw_t or "").strip().upper()
            if not t:
                continue
            existing = existing_ciks.get(t) or existing_ciks.get(raw_t)
            if existing:
                result.skipped_already_set.append(t)
                continue
            entry = sec_map.get(t)
            if entry is not None:
                result.resolved[t] = entry
            else:
                result.unresolved.append(t)
        logger.info(
            "sec.ticker_cik_map.resolve_complete",
            requested=len(tickers),
            resolved=len(result.resolved),
            unresolved=len(result.unresolved),
            skipped_already_set=len(result.skipped_already_set),
        )
        return result


__all__ = [
    "CIKResolveResult",
    "SEC_TICKER_MAP_URL",
    "SECTickerCIKMap",
    "TickerCIKEntry",
]
