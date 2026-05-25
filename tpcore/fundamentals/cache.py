"""Postgres-backed cache wrapping ``FMPFundamentalsAdapter``.

Engines call ``FundamentalsCache.get_quarterly_fundamentals(...)`` and:
    1. We hit ``platform.fundamentals_quarterly`` first.
    2. On miss, we fall through to FMP, then upsert *every* period the
       adapter returned (latest + history) so subsequent point-in-time
       lookups across the whole returned window stay cache-only.

Schema mirrors the adapter's normalized output (see
``tpcore.fmp.fundamentals_adapter._merge``). Idempotent on
``(ticker, filing_date)``. The cache does NOT own the asyncpg pool —
the caller (the scheduler) opens and closes it.

Important caveat (FMP free tier): the adapter's underlying call is
capped at 5 quarters per request. ``backfill()`` therefore captures
only the most recent ~1.25 years of fundamentals; it cannot reach
2018-style depth without a paid FMP plan. Documented here so the
caller understands what the cache will actually contain.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.outage import DataProviderOutage

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

_SELECT_COLUMNS = (
    "ticker, filing_date, period_end_date, period_label, "
    "net_income, fcf, operating_cash_flow, capex, revenue, "
    "total_assets, total_liabilities, current_assets, current_liabilities, "
    "receivables, cash_and_equivalents, shares_outstanding"
)


def _row_to_dict(row) -> dict[str, Any]:
    """Materialize a fundamentals_quarterly row into the adapter's shape."""
    return {
        "symbol": row["ticker"],
        "period": row["period_label"],
        "period_end_date": row["period_end_date"],
        "filing_date": row["filing_date"],
        "net_income": _decimal(row["net_income"]),
        "revenue": _decimal(row["revenue"]),
        "fcf": _decimal(row["fcf"]),
        "operating_cash_flow": _decimal(row["operating_cash_flow"]),
        "capex": _decimal(row["capex"]),
        "total_assets": _decimal(row["total_assets"]),
        "total_liabilities": _decimal(row["total_liabilities"]),
        "current_assets": _decimal(row["current_assets"]),
        "current_liabilities": _decimal(row["current_liabilities"]),
        "receivables": _decimal(row["receivables"]),
        "cash_and_equivalents": _decimal(row["cash_and_equivalents"]),
        "shares_outstanding": _decimal(row["shares_outstanding"]),
    }


def _decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v))


class FundamentalsCache:
    """DB-first fundamentals lookup with FMP fallback on miss.

    Args:
        pool: an ``asyncpg.Pool``. The cache uses but does not own it.
        adapter: optional FMP adapter to fall through to on miss. When
            ``None`` the cache is read-only — useful for backtests
            against a pre-populated table.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        adapter: FMPFundamentalsAdapter | None = None,
    ) -> None:
        self._pool = pool
        self._adapter = adapter

    async def get_quarterly_fundamentals(
        self,
        symbol: str,
        as_of_date: date | None = None,
    ) -> dict:
        """Return latest-as-of fundamentals for ``symbol``.

        Same shape as ``FMPFundamentalsAdapter.get_quarterly_fundamentals``:
        latest period at the top level plus a ``history`` list of priors.
        """
        latest, history = await self._read_db(symbol, as_of_date)
        if latest is None:
            if self._adapter is None:
                raise DataProviderOutage(
                    f"FundamentalsCache miss for {symbol} as_of={as_of_date} "
                    "and no FMP adapter wired (read-only mode)"
                )
            logger.info("fundamentals.cache.miss", symbol=symbol, as_of=str(as_of_date))
            payload = await self._adapter.get_quarterly_fundamentals(symbol, as_of_date)
            await self._upsert_payload(symbol, payload)
            # Reread from DB so the returned shape matches the cache path.
            latest, history = await self._read_db(symbol, as_of_date)
            if latest is None:  # pragma: no cover - defensive
                raise DataProviderOutage(
                    f"FundamentalsCache: write succeeded but readback empty for {symbol}"
                )
        else:
            logger.debug("fundamentals.cache.hit", symbol=symbol, as_of=str(as_of_date))
        out = dict(latest)
        out["history"] = history
        return out

    async def backfill(
        self,
        symbol: str,
        start_date: date | None = None,  # noqa: ARG002 - kept for API symmetry
        end_date: date | None = None,
    ) -> int:
        """Pull all available quarters from FMP and cache every period.

        ``start_date`` is accepted for API symmetry but the actual depth is
        bounded by the FMP plan: free tier silently caps at 5 quarters;
        Starter and above honor the adapter's ``DEFAULT_LIMIT`` (currently
        40 quarters ≈ 10 years). ``end_date`` is the PIT cutoff. Returns
        the row count upserted.
        """
        if self._adapter is None:
            raise DataProviderOutage("FundamentalsCache.backfill requires an adapter")
        payload = await self._adapter.get_quarterly_fundamentals(symbol, end_date)
        return await self._upsert_payload(symbol, payload)

    async def backfill_all(
        self,
        tickers: list[str] | None = None,
        *,
        inter_symbol_sleep_sec: float = 1.0,
        skip_if_refreshed_within_hours: float | None = 24.0,
    ) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]], int]:
        """Refresh every cached symbol. Returns ``(rows, no_data, failures, skipped)``.

        ``no_data`` collects symbols FMP responded to with "no usable
        fundamentals" — the canonical signal for ETFs and the rare
        delisted shell. These are expected-empty, not actionable, and
        callers should not exit non-zero on them. ``failures`` is for
        real outages: timeouts, 5xx, malformed payloads. ``skipped`` is
        the count of tickers whose cache row was already fresh enough
        per ``skip_if_refreshed_within_hours``.

        When ``tickers`` is ``None``, the active universe is read from
        ``platform.prices_daily`` (distinct tickers with a bar in the
        last 90 days and ``delisted = false``).

        ``inter_symbol_sleep_sec`` is a courtesy delay between FMP
        calls; Starter plan rate limits comfortably absorb 1s but
        tighter loops risk 429s on long universes.

        ``skip_if_refreshed_within_hours`` controls resumability — any
        ticker whose newest ``fundamentals_quarterly.recorded_at`` is
        younger than this threshold is skipped (no FMP call, no sleep).
        Default 24h means: a fully-completed run is a near-instant no-op
        on the same calendar day, and a run that timed out partway
        through resumes from where it left off. Pass ``None`` to force
        re-fetch every ticker regardless of freshness (the pre-2026-05-13
        behavior).
        """
        if self._adapter is None:
            raise DataProviderOutage(
                "FundamentalsCache.backfill_all requires an adapter"
            )
        if tickers is None:
            tickers = await self._list_active_tickers()

        # Resumability: pre-fetch the freshest recorded_at per ticker so we
        # can decide "skip" without a per-ticker DB round-trip. One bulk
        # query is cheap (~1s) and saves N - skipped FMP calls.
        already_fresh: set[str] = set()
        if skip_if_refreshed_within_hours is not None and tickers:
            already_fresh = await self._tickers_refreshed_within(
                tickers, hours=skip_if_refreshed_within_hours,
            )

        total = 0
        no_data: list[tuple[str, str]] = []
        failures: list[tuple[str, str]] = []
        skipped = 0
        for i, symbol in enumerate(tickers, start=1):
            if symbol.upper() in already_fresh:
                skipped += 1
                logger.debug("fundamentals.cache.backfill_all_skipped_fresh", symbol=symbol)
                continue
            try:
                n = await self.backfill(symbol)
            except DataProviderOutage as exc:
                msg = str(exc)
                # Classify the FMP error:
                #   * "no usable fundamentals" — ticker has no data (ETFs,
                #     recent IPOs); permanent skip.
                #   * "returned 402" — FMP Starter plan doesn't cover this
                #     ticker (e.g., BF.B / BRK.B dot-suffix names that
                #     require Premium). Permanent skip until we upgrade.
                #   * everything else — transient outage. Counts as a
                #     real failure that the stage surfaces.
                is_no_data = "no usable fundamentals" in msg
                is_premium_gated = "returned 402" in msg
                bucket = no_data if (is_no_data or is_premium_gated) else failures
                bucket.append((symbol, msg[:160]))
                logger.warning(
                    "fundamentals.cache.backfill_all_skipped"
                    if bucket is no_data
                    else "fundamentals.cache.backfill_all_failed",
                    symbol=symbol,
                    error=msg,
                )
                await asyncio.sleep(inter_symbol_sleep_sec)
                continue
            total += n
            logger.info(
                "fundamentals.cache.backfill_all_progress",
                symbol=symbol,
                rows=n,
                done=i,
                total=len(tickers),
                skipped_so_far=skipped,
            )
            await asyncio.sleep(inter_symbol_sleep_sec)
        return total, no_data, failures, skipped

    async def _tickers_refreshed_within(
        self,
        tickers: list[str],
        *,
        hours: float,
    ) -> set[str]:
        """Return the subset of ``tickers`` whose newest cache row is
        younger than ``hours`` old. One bulk query — much faster than
        per-ticker checks in the backfill loop."""
        upper_tickers = [t.upper() for t in tickers]
        sql = """
            SELECT ticker
            FROM platform.fundamentals_quarterly
            WHERE ticker = ANY($1::text[])
            GROUP BY ticker
            HAVING MAX(recorded_at) > now() - ($2::float * INTERVAL '1 hour')
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, upper_tickers, hours)
        return {r["ticker"] for r in rows}

    async def _list_active_tickers(self) -> list[str]:
        """Distinct tickers with a bar in the last 90 days and not delisted.

        Same definition ``PostgresDataAdapter.get_universe_symbols`` uses;
        kept inline here to avoid a cross-module dependency from
        ``tpcore.fundamentals`` into ``tpcore.data``.
        """
        sql = """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
              AND delisted = false
            ORDER BY ticker
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [r["ticker"] for r in rows]

    # ─── Internal ────────────────────────────────────────────────────────

    async def _read_db(
        self,
        symbol: str,
        as_of_date: date | None,
    ) -> tuple[dict | None, list[dict]]:
        """Query the cache. Returns ``(latest, history)`` (or ``(None, [])``)."""
        if as_of_date is None:
            sql = (
                f"SELECT {_SELECT_COLUMNS} FROM platform.fundamentals_quarterly "
                "WHERE ticker = $1 ORDER BY filing_date DESC"
            )
            args: tuple = (symbol.upper(),)
        else:
            sql = (
                f"SELECT {_SELECT_COLUMNS} FROM platform.fundamentals_quarterly "
                "WHERE ticker = $1 AND filing_date <= $2 ORDER BY filing_date DESC"
            )
            args = (symbol.upper(), as_of_date)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        if not rows:
            return None, []
        latest = _row_to_dict(rows[0])
        history = [_row_to_dict(r) for r in rows[1:]]
        return latest, history

    async def _upsert_payload(self, symbol: str, payload: dict) -> int:
        """Write the latest period plus every history entry to the cache."""
        periods: list[dict] = [{k: v for k, v in payload.items() if k != "history"}]
        for h in payload.get("history") or []:
            periods.append(h)
        usable = [p for p in periods if p.get("filing_date") is not None]
        if not usable:
            logger.warning("fundamentals.cache.upsert_skipped", symbol=symbol, reason="no filing_date")
            return 0

        sql = """
            INSERT INTO platform.fundamentals_quarterly (
                ticker, filing_date, period_end_date, period_label,
                net_income, fcf, operating_cash_flow, capex, revenue,
                total_assets, total_liabilities, current_assets, current_liabilities,
                receivables, cash_and_equivalents, shares_outstanding,
                recorded_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
            ON CONFLICT (ticker, filing_date) DO UPDATE SET
                period_end_date = EXCLUDED.period_end_date,
                period_label = EXCLUDED.period_label,
                net_income = EXCLUDED.net_income,
                fcf = EXCLUDED.fcf,
                operating_cash_flow = EXCLUDED.operating_cash_flow,
                capex = EXCLUDED.capex,
                revenue = EXCLUDED.revenue,
                total_assets = EXCLUDED.total_assets,
                total_liabilities = EXCLUDED.total_liabilities,
                current_assets = EXCLUDED.current_assets,
                current_liabilities = EXCLUDED.current_liabilities,
                receivables = EXCLUDED.receivables,
                cash_and_equivalents = EXCLUDED.cash_and_equivalents,
                shares_outstanding = EXCLUDED.shares_outstanding,
                recorded_at = now()
        """
        # Physical-truth gate — matches validation.fundamentals_integrity
        # expectations. Bad rows MUST NEVER reach the database, per the
        # platform's data-acceptance rules (per-row write-time filtering,
        # not post-hoc cleanup). Today's incident: USAR / SLDB came in
        # with shares_outstanding=0; VNOM had period_end > filing_date.
        today_d = datetime.now(UTC).date()
        rows: list[tuple] = []
        now = datetime.now(UTC)
        rejected = 0
        for p in usable:
            filing = p.get("filing_date")
            period_end = p.get("period_end_date") or filing
            shares = p.get("shares_outstanding")
            # filing must be on-or-before today
            if filing is None or filing > today_d:
                rejected += 1
                continue
            # period_end must be on-or-before filing
            if period_end is None or period_end > filing:
                rejected += 1
                continue
            # shares_outstanding must be > 0 OR NULL (not zero)
            if shares is not None and shares <= 0:
                rejected += 1
                continue
            rows.append((
                symbol.upper(),
                filing,
                period_end,
                p.get("period"),
                p.get("net_income"),
                p.get("fcf"),
                p.get("operating_cash_flow"),
                p.get("capex"),
                p.get("revenue"),
                p.get("total_assets"),
                p.get("total_liabilities"),
                p.get("current_assets"),
                p.get("current_liabilities"),
                p.get("receivables"),
                p.get("cash_and_equivalents"),
                shares,
                now,
            ))
        if rejected:
            logger.warning(
                "fundamentals.cache.physical_truth_rejected",
                symbol=symbol, rejected=rejected, accepted=len(rows),
            )
        if not rows:
            return 0
        async with self._pool.acquire() as conn:
            await conn.executemany(sql, rows)
        logger.info("fundamentals.cache.upsert", symbol=symbol, rows=len(rows))
        return len(rows)


    # ─────────────────────────────────────────────────────────────────
    # Archive-first surface (P1-sibling trust-audit 2026-05-25)
    # ─────────────────────────────────────────────────────────────────
    #
    # ``backfill_all`` interleaves FMP fetch + DB upsert per-symbol;
    # the post-hoc DB-readback archive that follows in
    # ``handle_fundamentals_refresh`` violated the archive-first
    # prime directive. These two helpers expose the fetch and upsert
    # legs independently so the handler can: (1) pre-fetch every
    # symbol's payload into memory, (2) write an archive + manifest
    # row BEFORE any DB write, (3) ETL from the archive file back to
    # ``upsert_payload`` per symbol, (4) mark the manifest loaded.

    async def fetch_payload(self, symbol: str) -> dict:
        """Pull the canonical FMP payload for one symbol without
        touching the DB. Raises ``DataProviderOutage`` on transport
        / contract failure; the caller decides whether to retry or
        record the failure."""
        if self._adapter is None:
            raise DataProviderOutage(
                "FundamentalsCache.fetch_payload requires an adapter"
            )
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        return await self._adapter.get_quarterly_fundamentals(
            symbol, _datetime.now(_UTC).date(),
        )

    async def upsert_payload(self, symbol: str, payload: dict) -> int:
        """Public wrapper around the internal ``_upsert_payload`` so
        the archive-first handler can drive Phase 2 from the on-disk
        archive without reaching into a private name."""
        return await self._upsert_payload(symbol, payload)

    async def list_active_tickers(self) -> list[str]:
        """Public wrapper around ``_list_active_tickers`` for the
        archive-first handler's pre-fetch loop."""
        return await self._list_active_tickers()

    async def tickers_refreshed_within(
        self, tickers: list[str], hours: float,
    ) -> set[str]:
        """Public wrapper around ``_tickers_refreshed_within`` for the
        archive-first handler's pre-fetch loop's skip-fresh gate."""
        return await self._tickers_refreshed_within(tickers, hours=hours)


__all__ = ["FundamentalsCache"]
