"""Daily-granularity insider-filings backfill + nightly-delta for
``platform.insider_filings``.

Carver-driven 2026-05-22: the vector engine candidate
``vector_beat_reversal_insider_filter_v1`` needs a 30d-rolling MSPR
signal at DAILY resolution. The existing monthly ``insider_sentiment``
(Finnhub free-tier) is information-lossy and empty pre-2025. FMP
Starter tier ($200/yr, already paid) exposes per-filing Form-4 rows
via ``/stable/insider-trading/search`` — paginated, full history.

This module is structurally symmetric to
``tpcore.data.survivorship_backfill`` (PR #283 / #288):

* ``backfill_one_symbol`` — pages FMP for one ticker, upserts every
  Form-4 row, emits a ``INSIDER_BACKFILL_SYMBOL_DONE`` event so a
  crash mid-run keeps completed work.
* ``backfill_universe`` — fan-out across the T1+T2 stock universe +
  delisted prices_daily tickers; resumable via the per-symbol event.
* ``daily_delta`` — nightly incremental: page 0 of /search for each
  symbol in the universe (last ~100 filings per symbol = last ~3-6
  months for high-volume names, longer for low-volume). Idempotent
  via the (symbol, transaction_date, reporting_cik, transaction_type,
  securities_transacted, price) PK + ON CONFLICT DO NOTHING.

Wired into ``scripts/ops.py`` as two stages:

* ``historical_insider_sentiment_daily`` — one-shot operator backfill.
  Off-cycle (NOT in OPS_UPDATE_STAGES); operator runs once after PR
  merge.
* ``daily_insider_sentiment_delta`` — IN the daily cadence via a
  FeedProfile entry; the feed dispatcher schedules it like every
  other source-of-truth feed.

Per the stream-long-running-output rule: per-symbol completion emits
an event to ``platform.application_log`` so a crash mid-run keeps
completed work.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from tpcore.data.ingest_fmp_bars import FMP_BASE_URL, _to_fmp_symbol
from tpcore.outage import DataProviderOutage

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# FMP endpoint constants
# ──────────────────────────────────────────────────────────────────────


_INSIDER_SEARCH_ENDPOINT = "insider-trading/search"
"""``/stable/insider-trading/search?symbol=X&page=N`` — per-filing
Form-4 rows. Page size is 100 (FMP-fixed). Pagination is 0-indexed."""

_PAGE_SIZE = 100
"""FMP returns at most 100 rows per page."""

_HTTP_TIMEOUT_SEC = 30.0
"""Per-request timeout — FMP /stable/insider-trading returns in
<1s typically; 30s is generous slack against transient slowness."""

_DEFAULT_BACKFILL_START = date(2018, 1, 1)
"""Per the Carver request: full daily granularity from 2018-01-01.
FMP Starter has bars going back to 2005 for some tickers but the
operator-spec backfill horizon is 2018-01-01."""

_DEFAULT_DELTA_PAGES = 1
"""Nightly delta pulls page 0 only — the last 100 filings per symbol.
For high-volume names that's the last ~3 months; for low-volume names
it's the last ~12-24 months. The (symbol, transaction_date, ...) PK +
ON CONFLICT DO NOTHING makes re-pulling old rows free. This is the
simplest invariant we can defend: page 0 always contains every filing
made since the last successful run."""


# ──────────────────────────────────────────────────────────────────────
# Progress event — used by the resume probe
# ──────────────────────────────────────────────────────────────────────


PROGRESS_EVENT_TYPE = "INSIDER_BACKFILL_SYMBOL_DONE"
"""Emitted to ``platform.application_log`` after each per-symbol
backfill completes (including 0-row symbols). The resume probe
queries this event-type to skip already-done symbols on re-run."""


async def already_completed_symbols(
    pool: asyncpg.Pool, *, lookback_days: int = 30,
) -> set[str]:
    """Symbols completed in the past N days (resume probe).

    30 days is far longer than any single backfill run; it's the
    cushion for an interrupted multi-day operator workflow.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT data->>'symbol' AS symbol
            FROM platform.application_log
            WHERE event_type = $1
              AND recorded_at >= now() - ($2::int * INTERVAL '1 day')
            """,
            PROGRESS_EVENT_TYPE,
            lookback_days,
        )
    return {r["symbol"] for r in rows if r["symbol"]}


# ──────────────────────────────────────────────────────────────────────
# Universe enumeration
# ──────────────────────────────────────────────────────────────────────


async def enumerate_insider_universe(pool: asyncpg.Pool) -> list[str]:
    """T1+T2 active stocks + every delisted ticker known to prices_daily.

    The active set is the canonical T1+T2 stock universe (the same query
    sec_filings uses; see tpcore.ingestion.handlers); we additionally
    include every ticker in prices_daily with delisted=true so the vector
    engine's lookback over a 2018+ window covers the symbols that lived
    AND died in-window.
    """
    async with pool.acquire() as conn:
        active_rows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2
              AND COALESCE(tc.asset_class, 'stock') = 'stock'
            ORDER BY lt.ticker
            """
        )
        delisted_rows = await conn.fetch(
            """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            WHERE delisted = true
            """
        )
    active = {r["ticker"] for r in active_rows}
    delisted = {r["ticker"] for r in delisted_rows}
    return sorted(active | delisted)


# ──────────────────────────────────────────────────────────────────────
# Per-symbol FMP fetch + upsert
# ──────────────────────────────────────────────────────────────────────


def _upsert_sql() -> str:
    """Idempotent insert for ``platform.insider_filings``.

    Conflict on the full PK ``(symbol, transaction_date, reporting_cik,
    transaction_type, securities_transacted, price)`` — DO NOTHING so
    re-runs don't churn the recorded_at clock.
    """
    return """
        INSERT INTO platform.insider_filings (
            symbol, filing_date, transaction_date, reporting_cik,
            company_cik, transaction_type, reporting_name, type_of_owner,
            acquisition_or_disposition, direct_or_indirect, form_type,
            securities_transacted, price, securities_owned, security_name, url
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16
        )
        ON CONFLICT ON CONSTRAINT insider_filings_pk DO NOTHING
    """


def _safe_date(raw: Any, fallback: date) -> date:
    """Parse an ISO date string; fall back if FMP returns junk."""
    if raw is None:
        return fallback
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return fallback


def _safe_float(raw: Any, default: float = 0.0) -> float:
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _physical_truth_rows(symbol: str, raw: list[dict[str, Any]]) -> list[tuple]:
    """Translate FMP /insider-trading/search rows to upsert tuples.

    Drops obvious junk: missing transactionDate / reportingCik /
    transactionType, negative shares, negative price. Bad rows are
    dropped (not zero-filled) — the table stays clean.
    """
    today = datetime.now(UTC).date()
    out: list[tuple] = []
    for row in raw:
        sym = str(row.get("symbol", "") or "").upper().strip()
        if not sym:
            continue
        # Allow FMP to return a normalised symbol (BRK-B → BRK.B etc).
        # Trust the request symbol — that's what the engine reads.
        sym = symbol.upper()
        tx_date_raw = row.get("transactionDate") or row.get("filingDate")
        if not tx_date_raw:
            continue
        tx_date = _safe_date(tx_date_raw, today)
        # Reject implausible future dates (FMP has occasionally returned
        # placeholder rows with future transactionDate during a vendor
        # data-load window — drop them so the engine never reads them).
        if tx_date > today:
            continue
        filing_date = _safe_date(row.get("filingDate"), tx_date)
        reporting_cik = str(row.get("reportingCik") or "").strip()
        if not reporting_cik:
            continue
        tx_type = str(row.get("transactionType") or "").strip()
        if not tx_type:
            continue
        shares = _safe_float(row.get("securitiesTransacted"), -1.0)
        if shares < 0:
            continue
        price = _safe_float(row.get("price"), -1.0)
        if price < 0:
            continue
        out.append((
            sym,
            filing_date,
            tx_date,
            reporting_cik,
            (str(row.get("companyCik") or "") or None),
            tx_type,
            (str(row.get("reportingName") or "") or None),
            (str(row.get("typeOfOwner") or "") or None),
            (str(row.get("acquisitionOrDisposition") or "") or None),
            (str(row.get("directOrIndirect") or "") or None),
            (str(row.get("formType") or "") or None),
            shares,
            price,
            (_safe_float(row.get("securitiesOwned"), 0.0)),
            (str(row.get("securityName") or "") or None),
            (str(row.get("url") or "") or None),
        ))
    return out


def _fmp_api_key() -> str:
    """Read the FMP API key from the env. Raises if absent so the stage
    surfaces the misconfig instead of silently no-oping."""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise DataProviderOutage(
            "FMP_API_KEY missing from environment — cannot pull insider-filings",
        )
    return key


async def _fetch_search_page(
    client: httpx.AsyncClient,
    symbol: str,
    page: int,
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """One page of FMP ``/stable/insider-trading/search``.

    Returns the raw JSON list (max 100 rows). On HTTP non-2xx, raises
    DataProviderOutage so the caller can mark the per-symbol failure
    and continue. On parse failure (FMP returned HTML for an outage)
    returns an empty list.
    """
    fmp_sym = _to_fmp_symbol(symbol)
    api_key = api_key or _fmp_api_key()
    url = f"{FMP_BASE_URL}/{_INSIDER_SEARCH_ENDPOINT}"
    params = {"symbol": fmp_sym, "page": str(page), "apikey": api_key}
    try:
        resp = await client.get(url, params=params, timeout=_HTTP_TIMEOUT_SEC)
    except httpx.RequestError as exc:
        raise DataProviderOutage(f"FMP request error: {exc!s}") from exc
    if resp.status_code in (429, 503):
        raise DataProviderOutage(
            f"FMP rate-limited / unavailable ({resp.status_code})",
        )
    if resp.status_code >= 400:
        raise DataProviderOutage(
            f"FMP HTTP {resp.status_code} for {symbol} page {page}",
        )
    try:
        data = resp.json()
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return data


async def backfill_one_symbol(
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    symbol: str,
    *,
    start: date = _DEFAULT_BACKFILL_START,
    max_pages: int = 200,
    api_key: str | None = None,
) -> int:
    """Page through FMP /insider-trading/search for ``symbol`` until we
    walk off ``start`` or hit an empty page.

    Returns the number of rows upserted (post-dedup). Emits a
    ``INSIDER_BACKFILL_SYMBOL_DONE`` event with the count so the
    resume probe can skip on the next pass.
    """
    api_key = api_key or _fmp_api_key()
    upsert = _upsert_sql()
    rows_written = 0
    last_seen_date: date | None = None
    for page in range(max_pages):
        try:
            raw = await _fetch_search_page(
                client, symbol, page, api_key=api_key,
            )
        except DataProviderOutage:
            # Re-raise — the universe loop catches & moves on.
            raise
        if not raw:
            break
        rows = _physical_truth_rows(symbol, raw)
        if rows:
            async with pool.acquire() as conn:
                await conn.executemany(upsert, rows)
            rows_written += len(rows)
            last_seen_date = min(r[2] for r in rows)  # tx_date
        # Stop if every row on this page is older than ``start``.
        if last_seen_date is not None and last_seen_date < start:
            break
        # If FMP returned a partial page (< _PAGE_SIZE), we've hit
        # the tail of the symbol's history.
        if len(raw) < _PAGE_SIZE:
            break
    await db_log.log(
        PROGRESS_EVENT_TYPE,
        f"insider backfill: {symbol} ← {rows_written} rows",
        severity="INFO",
        data={
            "symbol": symbol,
            "rows_written": rows_written,
            "start_date": start.isoformat(),
            "last_seen_date": (
                last_seen_date.isoformat() if last_seen_date else None
            ),
        },
    )
    return rows_written


async def backfill_universe(
    pool: asyncpg.Pool,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    universe: list[str],
    *,
    start: date = _DEFAULT_BACKFILL_START,
    resume: bool = True,
    max_pages: int = 200,
) -> dict[str, Any]:
    """Fan-out backfill across ``universe``.

    Resumable by default — queries application_log for symbols already
    completed in the past 30 days and skips them. Per-symbol permanent
    failures are logged and the run continues; the return dict carries
    the counters + failure sample.
    """
    if resume:
        done = await already_completed_symbols(pool)
        pending = [s for s in universe if s not in done]
        skipped = len(universe) - len(pending)
    else:
        pending = list(universe)
        skipped = 0
    total_rows = 0
    failures: list[str] = []
    succeeded: list[str] = []
    api_key = _fmp_api_key()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC) as client:
        for symbol in pending:
            try:
                rows = await backfill_one_symbol(
                    pool, client, db_log, symbol,
                    start=start, max_pages=max_pages, api_key=api_key,
                )
            except DataProviderOutage as exc:
                logger.error(
                    "insider.symbol_outage",
                    symbol=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:outage")
                continue
            except Exception as exc:  # noqa: BLE001 — keep the run moving
                logger.error(
                    "insider.symbol_failed",
                    symbol=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
            total_rows += rows
            succeeded.append(symbol)
    return {
        "universe_size": len(universe),
        "resumed_skipped": skipped,
        "symbols_attempted": len(pending),
        "symbols_succeeded": len(succeeded),
        "symbols_failed": len(failures),
        "rows_written": total_rows,
        "failures_sample": failures[:20],
    }


# ──────────────────────────────────────────────────────────────────────
# Nightly delta — page 0 only, full universe
# ──────────────────────────────────────────────────────────────────────


async def daily_delta(
    pool: asyncpg.Pool,
    db_log,  # tpcore.logging.db_handler.DBLogHandler
    *,
    universe: list[str] | None = None,
    pages: int = _DEFAULT_DELTA_PAGES,
) -> dict[str, Any]:
    """Nightly incremental — pages 0..(pages-1) of /search per symbol.

    Default ``pages=1`` is the last 100 filings per symbol, which covers
    every filing made in the last 3-6 months for high-volume names and
    multi-year stretches for low-volume names. The (symbol, transaction_
    date, reporting_cik, transaction_type, securities_transacted, price)
    PK + ON CONFLICT DO NOTHING make every overlap free.

    Returns counters identical-shape to ``backfill_universe`` so the
    daily monitoring dashboards can show the same fields.
    """
    universe = universe if universe is not None else (
        await enumerate_insider_universe(pool)
    )
    upsert = _upsert_sql()
    total_rows = 0
    failures: list[str] = []
    succeeded: list[str] = []
    api_key = _fmp_api_key()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC) as client:
        for symbol in universe:
            try:
                pulled = 0
                for page in range(max(int(pages), 1)):
                    raw = await _fetch_search_page(
                        client, symbol, page, api_key=api_key,
                    )
                    if not raw:
                        break
                    rows = _physical_truth_rows(symbol, raw)
                    if rows:
                        async with pool.acquire() as conn:
                            await conn.executemany(upsert, rows)
                        pulled += len(rows)
                    if len(raw) < _PAGE_SIZE:
                        break
                total_rows += pulled
                succeeded.append(symbol)
            except DataProviderOutage as exc:
                logger.error(
                    "insider.delta_outage",
                    symbol=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:outage")
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "insider.delta_failed",
                    symbol=symbol, error=str(exc)[:200],
                )
                failures.append(f"{symbol}:{type(exc).__name__}")
                continue
    await db_log.log(
        "INSIDER_DELTA_RUN_DONE",
        f"insider delta: {len(succeeded)} OK / {len(failures)} fail / "
        f"{total_rows} rows",
        severity="INFO",
        data={
            "universe_size": len(universe),
            "symbols_succeeded": len(succeeded),
            "symbols_failed": len(failures),
            "rows_written": total_rows,
        },
    )
    return {
        "universe_size": len(universe),
        "symbols_succeeded": len(succeeded),
        "symbols_failed": len(failures),
        "rows_written": total_rows,
        "failures_sample": failures[:20],
    }


__all__ = [
    "PROGRESS_EVENT_TYPE",
    "already_completed_symbols",
    "backfill_one_symbol",
    "backfill_universe",
    "daily_delta",
    "enumerate_insider_universe",
]
