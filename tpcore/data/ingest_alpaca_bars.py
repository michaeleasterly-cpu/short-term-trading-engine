"""Bootstrap script: download daily bars for the entire US equity universe from Alpaca.

Run once to populate ``platform.prices_daily``. Later wired up as a daily cron.

* Uses Alpaca **free tier** REST API (IEX feed for free historical bars).
* Active universe via ``GET /v2/assets?status=active&class=us_equity``.
* Inactive universe via ``GET /v2/assets?status=inactive&class=us_equity``;
  for each, pulls bars covering its trading life and marks ``delisted=True``.
* Rate-limited (Alpaca free tier ≈ 200 req/min).
* All timestamps stored as UTC; ``date`` is the bar's session date.

This file deliberately depends only on ``httpx`` and ``tpcore`` — it does
*not* import the ``alpaca_trade_api`` SDK (which is blocked by
``check_imports``). The newer ``alpaca-py`` package would also work but we
prefer raw HTTP here so the bootstrap script has no SDK churn risk.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.outage import with_retry

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

_ALPACA_BROKER_BASE_LIVE = "https://api.alpaca.markets"
_ALPACA_BROKER_BASE_PAPER = "https://paper-api.alpaca.markets"
_ALPACA_DATA_BASE = "https://data.alpaca.markets"  # same URL for paper + live
_RATE_LIMIT_SLEEP_SEC = 0.35  # ~170 rpm, safely under 200 rpm cap


def _alpaca_broker_base() -> str:
    """Pick the paper or live broker URL based on ``ALPACA_PAPER`` env.

    The data endpoint is shared, so only the broker (assets / orders) URL
    needs to switch. Defaults to paper — every environment we run today
    is paper-keyed, and a live key against the paper URL just 401s with
    a clear error rather than silently leaking real orders.
    """
    return (
        _ALPACA_BROKER_BASE_PAPER
        if os.getenv("ALPACA_PAPER", "true").lower() == "true"
        else _ALPACA_BROKER_BASE_LIVE
    )


# Back-compat shim — older callers referenced the constant directly.
# Kept as a property-like getter so importers always see the env-resolved
# value at access time, not at module-import time.
_ALPACA_BROKER_BASE = _ALPACA_BROKER_BASE_PAPER  # default; runtime callers use _alpaca_broker_base()


@dataclass
class _AssetRecord:
    symbol: str
    status: str  # "active" | "inactive"
    delisting_date: date | None  # populated for inactive
    exchange: str


def _alpaca_headers() -> dict[str, str]:
    """Build auth headers from the env. Accepts both naming conventions
    (``ALPACA_KEY``/``ALPACA_SECRET`` is what `.env` uses today)."""
    api_key = os.environ.get("ALPACA_KEY") or os.environ["ALPACA_API_KEY"]
    api_secret = os.environ.get("ALPACA_SECRET") or os.environ["ALPACA_API_SECRET"]
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}


async def _list_assets(client: httpx.AsyncClient, status: str) -> list[_AssetRecord]:
    """List active or inactive US equities."""
    url = f"{_alpaca_broker_base()}/v2/assets"
    params = {"status": status, "class": "us_equity"}
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    out: list[_AssetRecord] = []
    for row in resp.json():
        # NOTE: Alpaca does not expose delisting_date on the assets endpoint.
        # We treat inactive symbols as delisted and infer the delisting date
        # from the last bar returned by the data endpoint. TODO: capture it.
        out.append(
            _AssetRecord(
                symbol=row["symbol"],
                status=row["status"],
                delisting_date=None,
                exchange=row.get("exchange", ""),
            )
        )
    return out


async def fetch_active_us_equities(
    client: httpx.AsyncClient,
    *,
    exchanges: tuple[str, ...] = ("NYSE", "NASDAQ"),
) -> list[dict]:
    """List active, tradable US equities on the named exchanges.

    Returns a list of ``{"symbol", "exchange"}`` dicts. Used by the
    ``all_active`` daily_bars discovery sweep — distinct from
    ``_list_assets`` (which returns ``_AssetRecord`` for the bootstrap
    script).
    """
    url = f"{_alpaca_broker_base()}/v2/assets"
    resp = await client.get(url, params={"status": "active", "asset_class": "us_equity"})
    resp.raise_for_status()
    wanted = {e.upper() for e in exchanges}
    out: list[dict] = []
    for row in resp.json():
        if not row.get("tradable"):
            continue
        ex = (row.get("exchange") or "").upper()
        if ex not in wanted:
            continue
        out.append({"symbol": row["symbol"], "exchange": ex})
    return out


@with_retry(max_attempts=4, backoff_base_sec=2.0, backoff_cap_sec=30.0)
async def fetch_daily_bars_multi(
    client: httpx.AsyncClient,
    symbols: list[str],
    start: date,
    end: date,
    *,
    feed: str = "sip",
    adjustment: str = "all",
) -> dict[str, list[dict]]:
    """Multi-symbol equivalent of :func:`fetch_daily_bars`.

    Hits ``/v2/stocks/bars`` (no symbol in path) which accepts up to 100
    symbols per call. Pages through ``next_page_token`` and merges all
    pages into one dict keyed by symbol. Symbols with no bars in the
    window are returned as empty lists rather than missing keys, so the
    caller can iterate over all input symbols deterministically.

    Wrapped with ``@with_retry`` so a transient 429/503 doesn't tank
    the whole batch — the inner ``raise_for_status`` becomes a retry
    signal rather than a permanent failure.
    """
    url = f"{_ALPACA_DATA_BASE}/v2/stocks/bars"
    params: dict[str, str] = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": feed,
        "limit": "10000",
        "adjustment": adjustment,
    }
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    while True:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        bars_by_symbol = body.get("bars") or {}
        for sym, page in bars_by_symbol.items():
            out.setdefault(sym, []).extend(page or [])
        next_token = body.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
        await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    return out


async def fetch_daily_bars(
    client: httpx.AsyncClient,
    symbol: str,
    start: date,
    end: date,
    *,
    feed: str = "sip",
    adjustment: str = "all",
) -> list[dict]:
    """Page through ``/v2/stocks/{symbol}/bars`` for ``timeframe=1Day``.

    ``adjustment="all"`` returns split- and dividend-adjusted prices, which
    is what backtests want. Set to ``"raw"`` if you need to compare with
    intraday quotes.
    """
    url = f"{_ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars"
    params: dict[str, str] = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": feed,
        "limit": "10000",
        "adjustment": adjustment,
    }
    bars: list[dict] = []
    while True:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        page = body.get("bars") or []
        bars.extend(page)
        next_token = body.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
        await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    return bars


# ─────────────────────────────────────────────────────────────────────
# P3 trust-audit (2026-05-25) — stage-then-promote write path.
#
# Production writes to ``platform.prices_daily`` (post-P3) flow:
#   1. physical-truth filter (same as legacy ``_upsert_bars``);
#      rejects route to ``platform.ingest_quarantine``.
#   2. accepted rows bulk-INSERT into ``platform.prices_daily_staging``
#      with this batch's ``staging_run_id`` (= ``ingest_manifest_id``).
#   3. validate staging row count = accepted-row count.
#   4. promote via SQL ``INSERT ... SELECT ... FROM staging WHERE
#      staging_run_id = $1 ... ON CONFLICT (ticker, date) DO UPDATE
#      ... WHERE platform._source_priority(...) >= ...`` (carrying
#      the P4 provenance-downgrade guard).
#   5. mark staging rows ``promoted = true``.
#
# Legacy ``_upsert_bars`` (used by all_active discovery sweep +
# rebuild_from_archive) is intentionally unchanged so its call sites
# stay green; the new ``stage_then_promote_bars`` is the path the
# archive-first orchestrator drives.
# ─────────────────────────────────────────────────────────────────────


_STAGE_INSERT_SQL = """
    INSERT INTO platform.prices_daily_staging (
        staging_run_id, ticker, date, open, high, low, close, volume,
        adjusted_close, delisted, delisting_date, source
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (staging_run_id, ticker, date) DO NOTHING
"""

_STAGE_COUNT_SQL = """
    SELECT COUNT(*)::bigint AS n
    FROM platform.prices_daily_staging
    WHERE staging_run_id = $1 AND ticker = $2
"""

_PROMOTE_FROM_STAGE_SQL = """
    INSERT INTO platform.prices_daily (
        ticker, date, open, high, low, close, volume,
        adjusted_close, delisted, delisting_date, source
    )
    SELECT
        ticker, date, open, high, low, close, volume,
        adjusted_close, delisted, delisting_date, source
    FROM platform.prices_daily_staging
    WHERE staging_run_id = $1 AND ticker = $2 AND promoted = false
    ON CONFLICT (ticker, date) DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        adjusted_close = EXCLUDED.adjusted_close,
        delisted = EXCLUDED.delisted,
        delisting_date = EXCLUDED.delisting_date,
        source = EXCLUDED.source
    WHERE platform._source_priority(EXCLUDED.source)
        >= platform._source_priority(platform.prices_daily.source)
"""

_MARK_PROMOTED_SQL = """
    UPDATE platform.prices_daily_staging
    SET promoted = true
    WHERE staging_run_id = $1 AND ticker = $2 AND promoted = false
"""


class StagingValidationError(RuntimeError):
    """Raised when staging-level batch validation fails.

    The caller's ``archive_first_load_bars`` catches this in its
    Phase 3 ``except`` and writes ``manifest_id`` → status='failed'
    with the validation reason. Staging rows for the failing batch
    are LEFT in place (promoted=false) for forensic review.
    """


async def stage_then_promote_bars(
    pool: asyncpg.Pool,
    symbol: str,
    bars: list[dict],
    *,
    staging_run_id,  # UUID; forward-ref to avoid an import at module top
    delisted: bool = False,
    delisting_date: date | None = None,
    source: str = "alpaca",
) -> int:
    """P3 stage-then-promote write path for prices_daily.

    Returns the count promoted into ``platform.prices_daily`` (which
    equals ``rows_staged`` on the happy path; less if the
    provenance-downgrade guard from P4 filtered some rows).

    Raises :class:`StagingValidationError` when batch-level validation
    fails (e.g. staged row count != filtered count). The archive-first
    orchestrator catches that and marks the manifest 'failed'; the
    staging rows are preserved with ``promoted=false`` so the operator
    can audit / re-promote / abandon.
    """
    if not bars:
        return 0
    # Phase 1 — physical-truth filter + quarantine routing (mirrors
    # the legacy _upsert_bars contract — bad rows MUST NEVER reach
    # staging either).
    from tpcore.ingestion.quarantine import ERROR_VALIDATION, record_rejection
    today = datetime.now(UTC).date()
    rows: list[tuple] = []
    rejections: list[tuple[dict, str]] = []
    for b in bars:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        session_date = ts.date()
        o = b.get("o")
        h = b.get("h")
        low = b.get("l")
        close = b.get("c")
        v = b.get("v")
        if (o is None or h is None or low is None or close is None or v is None):
            rejections.append((b, "null_required_field"))
            continue
        if close <= 0 or close > 1e8 or o <= 0 or h <= 0 or low <= 0:
            rejections.append((b, "ohlc_out_of_range"))
            continue
        if h < max(o, close, low) or low > min(o, close, h):
            rejections.append((b, "ohlc_inconsistent"))
            continue
        if session_date > today:
            rejections.append((b, "future_date"))
            continue
        rows.append((
            staging_run_id, symbol, session_date, o, h, low, close, int(v),
            close,  # adjusted_close — same as close because adjustment=all
            delisted, delisting_date, source,
        ))
    if rejections:
        logger.warning(
            "ingest_alpaca_bars.stage_then_promote.physical_truth_rejected",
            symbol=symbol, rejected=len(rejections), accepted=len(rows),
        )
        feed_source = "fmp_daily_bars" if source == "fmp" else "alpaca_daily_bars"
        for payload, reason in rejections:
            await record_rejection(
                pool,
                source=feed_source,
                target_table="platform.prices_daily",
                payload={**payload, "ticker": symbol, "source": source},
                error_message=(
                    f"physical_truth_gate: {reason} "
                    f"(symbol={symbol} ts={payload.get('t')})"
                ),
                error_kind=ERROR_VALIDATION,
            )

    if not rows:
        return 0

    async with pool.acquire() as conn:
        # Phase 2 — bulk INSERT to staging.
        await conn.executemany(_STAGE_INSERT_SQL, rows)

        # Phase 3 — validate staging row count matches what we staged.
        # If a duplicate (staging_run_id, ticker, date) existed in the
        # input the ON CONFLICT DO NOTHING above silently dropped it;
        # a mismatch here means the producer fed us a same-batch
        # duplicate, which is a bug.
        staged_count = int(await conn.fetchval(
            _STAGE_COUNT_SQL, staging_run_id, symbol,
        ) or 0)
        if staged_count != len(rows):
            raise StagingValidationError(
                f"staging row count mismatch for symbol={symbol} "
                f"staging_run_id={staging_run_id}: "
                f"expected={len(rows)} actual={staged_count} — likely "
                "in-batch duplicate (ticker, date)"
            )

        # Phase 4 — promote via INSERT ... SELECT, honoring the P4
        # provenance-downgrade guard.
        promote_result = await conn.execute(
            _PROMOTE_FROM_STAGE_SQL, staging_run_id, symbol,
        )

        # Phase 5 — mark staging rows promoted=true.
        await conn.execute(_MARK_PROMOTED_SQL, staging_run_id, symbol)

    # asyncpg returns "INSERT 0 N" for INSERT ... ON CONFLICT — N is
    # the count rows that were INSERT'd OR UPDATE'd. Rows the
    # provenance-downgrade guard skipped don't count.
    promoted = 0
    if isinstance(promote_result, str) and promote_result.startswith("INSERT"):
        try:
            promoted = int(promote_result.rsplit(" ", 1)[-1])
        except ValueError:
            promoted = 0
    logger.info(
        "ingest_alpaca_bars.stage_then_promote.done",
        symbol=symbol, staging_run_id=str(staging_run_id),
        staged=staged_count, promoted=promoted, source=source,
    )
    return promoted


async def stage_then_promote_bars_batch(
    pool: asyncpg.Pool,
    bars_by_ticker: dict[str, list[dict]],
    *,
    staging_run_id,  # UUID
    delisted: bool = False,
    delisting_date: date | None = None,
    source: str = "alpaca",
) -> int:
    """Batched variant of stage_then_promote_bars — N tickers, 4 DB round-trips.

    The per-ticker variant does ~5 RTTs per ticker. On a 7,600-ticker
    full-universe ingest that's ~30k RTTs × ~400ms Supabase pooler RTT
    = ~3.4 hours of pure round-trip time, dwarfing the work itself.

    This batch variant does the same logical work but bounded to a single
    DB acquire():

      1. executemany INSERT to staging (one row per (ticker, date) tuple)
      2. ONE SELECT COUNT for validation (covers all tickers)
      3. ONE INSERT ... SELECT promote (covers all tickers)
      4. ONE UPDATE mark-promoted (covers all tickers)

    Same physical-truth filter + quarantine routing as the per-ticker
    function — same rejection log shape per ticker (the rejection batch
    accumulates across tickers; quarantine routing happens once at end).

    Returns total promoted row count across all tickers.
    """
    if not bars_by_ticker:
        return 0
    from tpcore.ingestion.quarantine import ERROR_VALIDATION, record_rejection

    today = datetime.now(UTC).date()
    rows: list[tuple] = []
    rejections: list[tuple[str, dict, str]] = []
    expected_per_ticker: dict[str, int] = {}

    for symbol, bars in bars_by_ticker.items():
        sym_kept = 0
        for b in bars:
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            session_date = ts.date()
            o = b.get("o")
            h = b.get("h")
            low = b.get("l")
            close = b.get("c")
            v = b.get("v")
            if (o is None or h is None or low is None or close is None or v is None):
                rejections.append((symbol, b, "null_required_field"))
                continue
            if close <= 0 or close > 1e8 or o <= 0 or h <= 0 or low <= 0:
                rejections.append((symbol, b, "ohlc_out_of_range"))
                continue
            if h < max(o, close, low) or low > min(o, close, h):
                rejections.append((symbol, b, "ohlc_inconsistent"))
                continue
            if session_date > today:
                rejections.append((symbol, b, "future_date"))
                continue
            rows.append((
                staging_run_id, symbol, session_date, o, h, low, close, int(v),
                close, delisted, delisting_date, source,
            ))
            sym_kept += 1
        expected_per_ticker[symbol] = sym_kept

    if rejections:
        feed_source = "fmp_daily_bars" if source == "fmp" else "alpaca_daily_bars"
        for sym, payload, reason in rejections:
            await record_rejection(
                pool,
                source=feed_source,
                target_table="platform.prices_daily",
                payload={**payload, "ticker": sym, "source": source},
                error_message=(
                    f"physical_truth_gate: {reason} "
                    f"(symbol={sym} ts={payload.get('t')})"
                ),
                error_kind=ERROR_VALIDATION,
            )

    if not rows:
        return 0

    # Batch-scope variants of the per-ticker SQL: drop the AND ticker = $2
    # filter so one call covers all tickers in this staging_run_id.
    promote_all_sql = """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        )
        SELECT
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        FROM platform.prices_daily_staging
        WHERE staging_run_id = $1 AND promoted = false
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            delisted = EXCLUDED.delisted,
            delisting_date = EXCLUDED.delisting_date,
            source = EXCLUDED.source
        WHERE platform._source_priority(EXCLUDED.source)
            >= platform._source_priority(platform.prices_daily.source)
    """
    mark_all_sql = """
        UPDATE platform.prices_daily_staging
        SET promoted = true
        WHERE staging_run_id = $1 AND promoted = false
    """
    count_all_sql = (
        "SELECT COUNT(*)::bigint FROM platform.prices_daily_staging "
        "WHERE staging_run_id = $1"
    )

    async with pool.acquire() as conn:
        # Phase 2 — bulk INSERT to staging.
        await conn.executemany(_STAGE_INSERT_SQL, rows)
        # Phase 3 — single COUNT for validation across all tickers.
        staged_total = int(
            await conn.fetchval(count_all_sql, staging_run_id) or 0
        )
        if staged_total != len(rows):
            raise StagingValidationError(
                f"staging row count mismatch (batch) "
                f"staging_run_id={staging_run_id}: "
                f"expected={len(rows)} actual={staged_total} — likely "
                "in-batch duplicate (ticker, date)"
            )
        # Phase 4 — single promote INSERT ... SELECT covers all tickers.
        promote_result = await conn.execute(promote_all_sql, staging_run_id)
        # Phase 5 — single UPDATE mark-promoted.
        await conn.execute(mark_all_sql, staging_run_id)

    promoted = 0
    if isinstance(promote_result, str) and promote_result.startswith("INSERT"):
        try:
            promoted = int(promote_result.rsplit(" ", 1)[-1])
        except ValueError:
            promoted = 0
    logger.info(
        "ingest_alpaca_bars.stage_then_promote.batch_done",
        staging_run_id=str(staging_run_id),
        tickers=len(bars_by_ticker), staged=staged_total,
        promoted=promoted, source=source,
    )
    return promoted


async def _upsert_bars(
    pool: asyncpg.Pool,
    symbol: str,
    bars: list[dict],
    delisted: bool,
    delisting_date: date | None = None,
    source: str = "alpaca",
) -> int:
    """Insert/update bars in ``platform.prices_daily``. Returns rows written.

    Idempotent via the ``(ticker, date)`` primary key + ``ON CONFLICT DO
    UPDATE``. Adjusted_close mirrors close — we ingest with
    ``adjustment="all"``, so the returned prices are already adjusted; the
    column is kept distinct for forward compatibility with a future raw+adj
    dual ingestion.

    ``source`` is the canonical provenance tag written to
    ``platform.prices_daily.source`` for every row touched by this call.
    Callers MUST pass the provider that actually fetched the bars — the
    FMP-feed path passes ``source='fmp'`` so its rows aren't mislabeled
    'alpaca' (the prior bug). The kwarg defaults to ``'alpaca'`` only for
    back-compat with the legacy Alpaca-only callers; new code should pass
    the source explicitly.
    """
    if not bars:
        return 0
    # P4 trust-audit (2026-05-25): provenance downgrade guard on
    # ON CONFLICT. The WHERE clause uses platform._source_priority(s)
    # (migration 20260525_0700) to rank sources so a lower-priority
    # writer (e.g. legacy ``alpaca``) re-running over a row already
    # tagged ``fmp`` no longer silently overwrites the FMP-primary
    # provenance flagged by the audit. Same-priority refresh is
    # allowed (fresh fmp pull over existing fmp row); strictly-lower
    # priority is rejected silently (the UPDATE is skipped — ON
    # CONFLICT WHERE filters the row out; INSERT didn't fire because
    # the PK collided). The ``source`` column survives unchanged.
    sql = """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            delisted = EXCLUDED.delisted,
            delisting_date = EXCLUDED.delisting_date,
            source = EXCLUDED.source
        WHERE platform._source_priority(EXCLUDED.source)
            >= platform._source_priority(platform.prices_daily.source)
    """
    # Physical-truth gate — matches validation.row_integrity expectations.
    # Bad rows MUST NEVER reach the database (per the data-acceptance rule
    # codified after the 94k-bad-row Tradier incident in May 2026):
    #   * close > 0 and <= 100M (no scale corruption, no pre-IPO zeros)
    #   * OHLC consistent (high >= max(open, close, low), low <= min(open, close, high))
    #   * volume >= 0 and not NULL
    #   * date not in the future
    #
    # P5 trust-audit (2026-05-25): rejected rows are routed to
    # ``platform.ingest_quarantine`` with the offending payload, the
    # reason, and ``error_kind='validation'``. The legacy behaviour
    # was to count rejected rows in a log line and drop them
    # silently — operators had no forensic record of bad bars and
    # couldn't audit retries.
    from tpcore.ingestion.quarantine import ERROR_VALIDATION, record_rejection
    today = datetime.now(UTC).date()
    rows: list[tuple] = []
    rejections: list[tuple[dict, str]] = []
    for b in bars:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        session_date = ts.date()
        o = b.get("o")
        h = b.get("h")
        low = b.get("l")
        close = b.get("c")
        v = b.get("v")
        if (o is None or h is None or low is None or close is None or v is None):
            rejections.append((b, "null_required_field"))
            continue
        if close <= 0 or close > 1e8 or o <= 0 or h <= 0 or low <= 0:
            rejections.append((b, "ohlc_out_of_range"))
            continue
        if h < max(o, close, low) or low > min(o, close, h):
            rejections.append((b, "ohlc_inconsistent"))
            continue
        if session_date > today:
            rejections.append((b, "future_date"))
            continue
        rows.append((
            symbol, session_date, o, h, low, close, int(v),
            close,  # adjusted_close — same as close because adjustment=all
            delisted, delisting_date, source,
        ))
    if rejections:
        logger.warning(
            "ingest_alpaca_bars.physical_truth_rejected",
            symbol=symbol, rejected=len(rejections), accepted=len(rows),
        )
        # Route each rejected bar to quarantine. Best-effort —
        # record_rejection swallows + logs its own write errors so
        # an audit-row write failure cannot abort the accepted-rows
        # upsert below.
        feed_source = "fmp_daily_bars" if source == "fmp" else "alpaca_daily_bars"
        for payload, reason in rejections:
            await record_rejection(
                pool,
                source=feed_source,
                target_table="platform.prices_daily",
                payload={**payload, "ticker": symbol, "source": source},
                error_message=(
                    f"physical_truth_gate: {reason} "
                    f"(symbol={symbol} ts={payload.get('t')})"
                ),
                error_kind=ERROR_VALIDATION,
            )
    if not rows:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(sql, rows)
    return len(rows)


async def run() -> None:
    """Ingest the entire active+inactive US equity universe.

    For a focused subset (e.g. the backtest universe), use
    ``scripts/backfill_backtest_universe.py`` which only takes ~1 minute
    of API time vs. the hours this full sweep would consume.
    """
    headers = _alpaca_headers()
    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])

    end = date.today()  # noqa: DTZ011
    start = date(2016, 1, 1)  # Alpaca IEX free history cutoff

    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            active = await _list_assets(client, "active")
            inactive = await _list_assets(client, "inactive")
            logger.info("alpaca.assets", n_active=len(active), n_inactive=len(inactive))

            for asset in active + inactive:
                try:
                    bars = await fetch_daily_bars(client, asset.symbol, start, end)
                except httpx.HTTPStatusError as exc:  # pragma: no cover - operational
                    logger.warning(
                        "alpaca.bars.failed",
                        symbol=asset.symbol,
                        status=exc.response.status_code,
                    )
                    await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
                    continue
                inserted = await _upsert_bars(
                    pool,
                    asset.symbol,
                    bars,
                    delisted=(asset.status == "inactive"),
                )
                logger.info("alpaca.upsert", symbol=asset.symbol, rows=inserted)
                await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover
    asyncio.run(run())


__all__ = ["fetch_daily_bars", "_alpaca_headers", "_upsert_bars", "main", "run"]


if __name__ == "__main__":  # pragma: no cover
    main()
