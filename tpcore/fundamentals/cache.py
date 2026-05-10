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
    ) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]]]:
        """Refresh every cached symbol. Returns ``(rows, no_data, failures)``.

        ``no_data`` collects symbols FMP responded to with "no usable
        fundamentals" — the canonical signal for ETFs and the rare
        delisted shell. These are expected-empty, not actionable, and
        callers should not exit non-zero on them. ``failures`` is for
        real outages: timeouts, 5xx, malformed payloads.

        When ``tickers`` is ``None``, the active universe is read from
        ``platform.prices_daily`` (distinct tickers with a bar in the
        last 90 days and ``delisted = false``).

        ``inter_symbol_sleep_sec`` is a courtesy delay between FMP
        calls; Starter plan rate limits comfortably absorb 1s but
        tighter loops risk 429s on long universes.
        """
        if self._adapter is None:
            raise DataProviderOutage(
                "FundamentalsCache.backfill_all requires an adapter"
            )
        if tickers is None:
            tickers = await self._list_active_tickers()
        total = 0
        no_data: list[tuple[str, str]] = []
        failures: list[tuple[str, str]] = []
        for i, symbol in enumerate(tickers, start=1):
            try:
                n = await self.backfill(symbol)
            except DataProviderOutage as exc:
                msg = str(exc)
                bucket = no_data if "no usable fundamentals" in msg else failures
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
            )
            await asyncio.sleep(inter_symbol_sleep_sec)
        return total, no_data, failures

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
        rows: list[tuple] = []
        now = datetime.now(UTC)
        for p in usable:
            rows.append(
                (
                    symbol.upper(),
                    p["filing_date"],
                    p.get("period_end_date") or p["filing_date"],
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
                    p.get("shares_outstanding"),
                    now,
                )
            )
        async with self._pool.acquire() as conn:
            await conn.executemany(sql, rows)
        logger.info("fundamentals.cache.upsert", symbol=symbol, rows=len(rows))
        return len(rows)


__all__ = ["FundamentalsCache"]
