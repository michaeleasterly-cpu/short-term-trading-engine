"""Built-in ingestion-job handlers.

Each handler is an async callable ``(pool, config: dict) -> int | None``.
The return value is ``rows_ingested`` and lands in the
``INGESTION_COMPLETE`` event payload; ``None`` means "rows" doesn't
apply to this job (validation, e.g.). Any raised exception is captured
by the engine and recorded as ``last_error``. Handlers reuse the
existing single-purpose modules — this file is mostly glue.

Registry: :data:`HANDLERS` maps ``job_name`` → handler. The engine
looks up by name; jobs without a registered handler land in
``last_status = 'failed'`` with a clear error.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

HandlerFn = Callable[["asyncpg.Pool", dict[str, Any]], Awaitable["int | None"]]
"""Handlers return ``rows_ingested`` (or ``None`` if the metric doesn't apply,
e.g. validation). The engine threads the value into the application_log
``INGESTION_COMPLETE`` event so daily ops checks can see throughput."""


async def handle_data_validation(pool: "asyncpg.Pool", config: dict[str, Any]) -> int | None:
    """Run the Data Validation Suite. Raises if the suite fails.

    Returns ``None`` — "rows ingested" doesn't map to a validation pass."""
    from tpcore.quality.validation.suite import run_suite

    result = await run_suite(pool)
    if not result.passed:
        # The suite already wrote per-check rows to platform.data_quality_log;
        # the exception is so the engine records last_status='failed'.
        raise RuntimeError(f"validation suite failed: {result}")
    return None


async def handle_fundamentals_refresh(
    pool: "asyncpg.Pool", config: dict[str, Any]
) -> int | None:
    """Refresh FMP fundamentals for the active universe.

    ``config`` is currently unused — the cache reads the active universe
    from ``platform.prices_daily`` directly. Kept as the seed payload's
    ``{"universe": "active"}`` documents intent and leaves room for a
    future "ticker list override" knob without a schema change.
    """
    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fundamentals.cache import FundamentalsCache

    async with FMPFundamentalsAdapter() as adapter:
        cache = FundamentalsCache(pool, adapter=adapter)
        rows, no_data, failures = await cache.backfill_all()
    logger.info(
        "ingestion.handler.fundamentals_done",
        rows=rows,
        no_data=len(no_data),
        failures=len(failures),
    )
    if failures:
        # ETF skips (no_data) are expected and silent. Real FMP outages
        # bubble up so the engine records the run as failed.
        raise RuntimeError(
            f"fundamentals_refresh: {len(failures)} real failure(s); "
            f"first={failures[0][0]}: {failures[0][1]}"
        )
    return rows


async def handle_corporate_actions(
    pool: "asyncpg.Pool", config: dict[str, Any]
) -> int | None:
    """Pull Alpaca corporate actions for the backtest universe and
    re-apply splits to ``platform.prices_daily``.

    This mirrors ``ops/cron_corporate_actions.py`` step-for-step but
    operates inside the engine's lifecycle (the engine owns the pool).
    """
    import httpx

    from tpcore.data.apply_splits import apply_all_splits
    from tpcore.data.ingest_alpaca_bars import _alpaca_headers
    from tpcore.data.ingest_corporate_actions import (
        DEFAULT_TYPES,
        fetch_corporate_actions,
        upsert_corporate_actions,
    )

    # 50-name backtest universe — kept in sync with
    # ``ops/cron_corporate_actions.py:UNIVERSE``.
    universe: tuple[str, ...] = (
        "SPY", "QQQ", "IWM",
        "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
        "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
        "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
        "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
        "LMT", "RTX", "NOC", "GD",
        "SO", "DUK", "NEE",
        "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
    )
    chunk_size = 20
    today = datetime.now(UTC).date()
    ingest_start = config.get("ingest_start", "2018-01-01")
    if isinstance(ingest_start, str):
        from datetime import date as date_t

        ingest_start = date_t.fromisoformat(ingest_start)

    headers = _alpaca_headers()
    total_actions = 0
    async with httpx.AsyncClient(
        headers=headers,
        base_url="https://data.alpaca.markets",
        timeout=60.0,
    ) as client:
        for i in range(0, len(universe), chunk_size):
            chunk = list(universe[i : i + chunk_size])
            actions = await fetch_corporate_actions(
                client,
                symbols=chunk,
                start=ingest_start,
                end=today,
                types=list(DEFAULT_TYPES),
            )
            if actions:
                await upsert_corporate_actions(pool, actions)
            total_actions += len(actions)

    split_summary = await apply_all_splits(pool, only_tickers=list(universe))
    logger.info(
        "ingestion.handler.corporate_actions_done",
        actions_ingested=total_actions,
        splits_applied=len(split_summary["applied"]),
        splits_skipped=len(split_summary["skipped"]),
    )
    return total_actions


async def handle_daily_bars(pool: "asyncpg.Pool", config: dict[str, Any]) -> int | None:
    """Incremental daily-bar refresh for the active universe.

    Pulls the last ``lookback_days`` (default 7) of bars from Alpaca for
    every ticker that has a recent bar in ``platform.prices_daily`` and
    is not flagged delisted. The 7-day window is overlap insurance —
    Alpaca's IEX free tier occasionally restates a bar a day or two
    later.

    ``config`` keys:
        * ``universe``:
            - ``"active"`` (default): reads tickers from prices_daily.
            - ``"all_active"``: discovery-mode sweep — enumerate every
              tradable Alpaca asset on NYSE/NASDAQ and apply a coarse
              price/volume filter. New tickers that pass the filter
              enter ``platform.prices_daily``.
            - ``list[str]``: explicit ticker override.
        * ``lookback_days``: int, default 7.
        * ``min_price`` / ``min_volume``: coarse filter floors (only
          consulted on the ``all_active`` path). Defaults: $5, 250k.
        * ``batch_size`` / ``inter_batch_sleep_sec``: ``all_active``
          batching. Defaults: 50, 0.3.
    """
    universe_cfg = config.get("universe", "active")
    if universe_cfg == "all_active":
        return await _handle_daily_bars_all_active(pool, config)
    return await _handle_daily_bars_explicit(pool, config, universe_cfg)


async def _handle_daily_bars_explicit(
    pool: asyncpg.Pool,
    config: dict[str, Any],
    universe_cfg: Any,
) -> int:
    """Existing 'active' / list-of-tickers code path. Per-symbol fetches.

    Lifted out of ``handle_daily_bars`` so the discovery sweep
    (``all_active``) can live as its own helper without making the entry
    point unreadable.
    """
    import asyncio
    from datetime import timedelta

    import httpx

    from tpcore.data.ingest_alpaca_bars import (
        _RATE_LIMIT_SLEEP_SEC,
        _alpaca_headers,
        _upsert_bars,
        fetch_daily_bars,
    )

    lookback_days = int(config.get("lookback_days", 7))
    if universe_cfg == "active":
        sql = """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
              AND delisted = false
            ORDER BY ticker
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
        symbols = [r["ticker"] for r in rows]
    elif isinstance(universe_cfg, list):
        symbols = [str(s).upper() for s in universe_cfg]
    else:
        raise ValueError(
            f"daily_bars: unsupported universe config {universe_cfg!r} — "
            "expected 'active', 'all_active', or a list of tickers"
        )

    today = datetime.now(UTC).date()
    start = today - timedelta(days=lookback_days)

    headers = _alpaca_headers()
    total_rows = 0
    failures: list[str] = []
    async with httpx.AsyncClient(
        headers=headers,
        base_url="https://data.alpaca.markets",
        timeout=30.0,
    ) as client:
        for symbol in symbols:
            try:
                bars = await fetch_daily_bars(client, symbol, start, today)
            except httpx.HTTPStatusError as exc:
                failures.append(f"{symbol}({exc.response.status_code})")
                await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
                continue
            if bars:
                inserted = await _upsert_bars(pool, symbol, bars, delisted=False)
                total_rows += inserted
            await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)

    logger.info(
        "ingestion.handler.daily_bars_done",
        symbols=len(symbols),
        rows_upserted=total_rows,
        failures=len(failures),
    )
    if failures:
        raise RuntimeError(
            f"daily_bars: {len(failures)} symbol fetch failure(s); first: {failures[0]}"
        )
    return total_rows


async def _handle_daily_bars_all_active(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int:
    """Discovery sweep: enumerate every active US equity, coarse-filter, upsert.

    Uses Alpaca's multi-symbol bars endpoint (``/v2/stocks/bars?symbols=…``)
    so 8k tickers fits in ~160 calls — under 4 minutes wall time at
    50 symbols / 0.3s between batches. Filters at the engine layer
    (close > min_price AND avg volume > min_volume over the lookback
    window) before upserting; symbols that don't pass never touch the
    database. New rows land with ``source = 'alpaca'`` (the upsert SQL
    sets it explicitly so a previously-Tradier-tagged row gets promoted
    back to alpaca-provenance).
    """
    import asyncio
    from datetime import timedelta

    import httpx

    from tpcore.data.ingest_alpaca_bars import (
        _ALPACA_DATA_BASE,
        _alpaca_broker_base,
        _alpaca_headers,
        _upsert_bars,
        fetch_active_us_equities,
        fetch_daily_bars_multi,
    )

    lookback_days = int(config.get("lookback_days", 7))
    min_price = float(config.get("min_price", 5.0))
    min_volume = int(config.get("min_volume", 250_000))
    batch_size = int(config.get("batch_size", 50))
    inter_batch_sleep = float(config.get("inter_batch_sleep_sec", 0.3))

    today = datetime.now(UTC).date()
    start = today - timedelta(days=lookback_days)

    headers = _alpaca_headers()
    rows_upserted = 0
    symbols_passed_coarse = 0
    failed_batches = 0
    async with (
        httpx.AsyncClient(headers=headers, base_url=_alpaca_broker_base(), timeout=60.0) as broker,
        httpx.AsyncClient(headers=headers, base_url=_ALPACA_DATA_BASE, timeout=60.0) as data,
    ):
        assets = await fetch_active_us_equities(broker)
        all_symbols = [a["symbol"] for a in assets]
        logger.info(
            "ingestion.handler.daily_bars.all_active.universe",
            count=len(all_symbols),
            min_price=min_price,
            min_volume=min_volume,
        )

        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i : i + batch_size]
            try:
                bars_by_symbol = await fetch_daily_bars_multi(data, batch, start, today)
            except httpx.HTTPStatusError as exc:
                failed_batches += 1
                logger.warning(
                    "ingestion.handler.daily_bars.all_active.batch_failed",
                    batch_start=i,
                    status=exc.response.status_code,
                )
                await asyncio.sleep(inter_batch_sleep)
                continue

            for symbol, bars in bars_by_symbol.items():
                if not bars:
                    continue
                last_close = float(bars[-1].get("c") or 0.0)
                avg_volume = sum(int(b.get("v") or 0) for b in bars) / len(bars)
                if last_close <= min_price or avg_volume <= min_volume:
                    continue
                symbols_passed_coarse += 1
                rows_upserted += await _upsert_bars(pool, symbol, bars, delisted=False)

            await asyncio.sleep(inter_batch_sleep)

    logger.info(
        "ingestion.handler.daily_bars.all_active.done",
        symbols_listed=len(all_symbols),
        symbols_passed_coarse=symbols_passed_coarse,
        rows_upserted=rows_upserted,
        failed_batches=failed_batches,
    )
    if failed_batches:
        raise RuntimeError(
            f"daily_bars all_active: {failed_batches} batch fetch failure(s)"
        )
    return rows_upserted


HANDLERS: dict[str, HandlerFn] = {
    "data_validation": handle_data_validation,
    "fundamentals_refresh": handle_fundamentals_refresh,
    "corporate_actions": handle_corporate_actions,
    "daily_bars": handle_daily_bars,
}


__all__ = [
    "HANDLERS",
    "HandlerFn",
    "handle_data_validation",
    "handle_fundamentals_refresh",
    "handle_corporate_actions",
    "handle_daily_bars",
]
