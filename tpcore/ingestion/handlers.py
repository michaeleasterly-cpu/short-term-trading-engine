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

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

HandlerFn = Callable[["asyncpg.Pool", dict[str, Any]], Awaitable["int | None"]]
"""Handlers return ``rows_ingested`` (or ``None`` if the metric doesn't apply,
e.g. validation). The engine threads the value into the application_log
``INGESTION_COMPLETE`` event so daily ops checks can see throughput."""


async def handle_data_validation(pool: asyncpg.Pool, config: dict[str, Any]) -> int | None:
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
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """Refresh FMP fundamentals for the active universe.

    ``config`` is currently unused — the cache reads the active universe
    from ``platform.prices_daily`` directly. Kept as the seed payload's
    ``{"universe": "active"}`` documents intent and leaves room for a
    future "ticker list override" knob without a schema change.
    """
    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fundamentals.cache import FundamentalsCache

    run_started = datetime.now(UTC)
    async with FMPFundamentalsAdapter() as adapter:
        cache = FundamentalsCache(pool, adapter=adapter)
        rows, no_data, failures = await cache.backfill_all()

    # CSV-first archive — pull rows touched in this run from the DB
    # and write them out. Schema mirrors fundamentals_quarterly so the
    # archive can fully reconstruct DB state if FMP revokes history.
    async with pool.acquire() as conn:
        new_rows = await conn.fetch(
            """
            SELECT ticker, filing_date, period_end_date, period_label,
                   net_income, fcf, operating_cash_flow, capex, revenue,
                   total_assets, total_liabilities, current_assets,
                   current_liabilities, receivables, cash_and_equivalents,
                   shares_outstanding, pb, de, recorded_at
            FROM platform.fundamentals_quarterly
            WHERE recorded_at >= $1
            ORDER BY ticker, period_end_date
            """,
            run_started,
        )
    archive_rows = [
        {k: str(v) if v is not None else "" for k, v in dict(r).items()}
        for r in new_rows
    ]
    # CSV-first audit archive (incremental — new rows this run only;
    # shrinkage detection is reserved for full-snapshot sources).
    from tpcore.ingestion.csv_archive import write_archive
    archive = write_archive(
        "fmp_fundamentals", archive_rows,
        fieldnames=[
            "ticker", "filing_date", "period_end_date", "period_label",
            "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
            "total_assets", "total_liabilities", "current_assets",
            "current_liabilities", "receivables", "cash_and_equivalents",
            "shares_outstanding", "pb", "de", "recorded_at",
        ],
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("period_end_date")),
    )

    logger.info(
        "ingestion.handler.fundamentals_done",
        rows=rows,
        no_data=len(no_data),
        failures=len(failures),
        csv_archive=str(archive.path),
    )
    if failures:
        # ETF skips (no_data) are expected and silent. Real FMP outages
        # bubble up so the engine records the run as failed.
        raise RuntimeError(
            f"fundamentals_refresh: {len(failures)} real failure(s); "
            f"first={failures[0][0]}: {failures[0][1]}"
        )
    return rows


_CORPORATE_ACTIONS_50_NAME_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
    "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
    "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
    "LMT", "RTX", "NOC", "GD",
    "SO", "DUK", "NEE",
    "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
)
"""Original 50-name backtest universe — kept in sync with
``ops/cron_corporate_actions.py:UNIVERSE``. Default for back-compat;
``config.universe = "all_active"`` overrides to the full prices_daily set."""


async def handle_corporate_actions(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """Pull Alpaca corporate actions and re-apply splits to ``platform.prices_daily``.

    ``config`` keys:
        * ``universe``:
            - default (omitted): the 50-name backtest universe.
            - ``"all_active"``: every distinct ticker in ``prices_daily``.
              Apply-splits then runs against the full table (no ticker
              filter), so Tradier-sourced bars get adjusted too.
            - ``list[str]``: explicit ticker override.
        * ``ingest_start``: ISO date, default ``"2018-01-01"``.
    """
    import httpx

    from tpcore.data.apply_splits import apply_all_splits
    from tpcore.data.ingest_alpaca_bars import _alpaca_headers
    from tpcore.data.ingest_corporate_actions import (
        DEFAULT_TYPES,
        fetch_corporate_actions,
        upsert_corporate_actions,
    )

    universe_cfg = config.get("universe", "default")
    if universe_cfg == "all_active":
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.prices_daily ORDER BY ticker"
            )
        universe: tuple[str, ...] = tuple(r["ticker"] for r in rows)
        apply_filter: list[str] | None = None
    elif isinstance(universe_cfg, list):
        universe = tuple(str(s).upper() for s in universe_cfg)
        apply_filter = list(universe)
    else:
        universe = _CORPORATE_ACTIONS_50_NAME_UNIVERSE
        apply_filter = list(universe)

    chunk_size = 20
    today = datetime.now(UTC).date()
    ingest_start = config.get("ingest_start", "2018-01-01")
    if isinstance(ingest_start, str):
        from datetime import date as date_t

        ingest_start = date_t.fromisoformat(ingest_start)

    headers = _alpaca_headers()
    total_actions = 0
    archive_rows: list[dict] = []
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
                for a in actions:
                    # actions is typically a list of dicts shaped by
                    # fetch_corporate_actions; archive what we have.
                    archive_rows.append({
                        "ticker": a.get("symbol") or a.get("ticker") or "",
                        "action_date": str(a.get("ex_date") or a.get("effective_date") or a.get("date") or ""),
                        "action_type": a.get("type") or a.get("action_type") or "",
                        "ratio": str(a.get("ratio") or a.get("split_ratio") or ""),
                        "raw": json.dumps(a, default=str)[:500],
                    })
                await upsert_corporate_actions(pool, actions)
            total_actions += len(actions)

    # CSV-first archive.
    from tpcore.ingestion.csv_archive import (
        detect_shrinkage,
        log_shrinkage_warning,
        write_archive,
    )
    archive = write_archive(
        "alpaca_corporate_actions", archive_rows,
        fieldnames=["ticker", "action_date", "action_type", "ratio", "raw"],
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("action_type")),
    )
    shrinkage = detect_shrinkage("alpaca_corporate_actions", archive.rows_written, exclude_path=archive.path)
    if shrinkage is not None:
        log_shrinkage_warning(shrinkage)

    split_summary = await apply_all_splits(pool, only_tickers=apply_filter)
    logger.info(
        "ingestion.handler.corporate_actions_done",
        universe_mode=universe_cfg if isinstance(universe_cfg, str) else "list",
        universe_size=len(universe),
        actions_ingested=total_actions,
        splits_applied=len(split_summary["applied"]),
        splits_skipped=len(split_summary["skipped"]),
        csv_archive=str(archive.path),
        shrinkage_over_threshold=shrinkage.over_threshold if shrinkage else False,
    )
    return total_actions


async def handle_daily_bars(pool: asyncpg.Pool, config: dict[str, Any]) -> int | None:
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
        * ``end_offset_days``: int, default 0. Shifts the request window's
          end date back by N days relative to "today". Use ``1`` for
          mid-session backfills — Alpaca's SIP free tier returns 403
          when ``end=today`` during regular hours (intraday data
          subscription required), but historical ``end=yesterday`` is
          fine. The after-hours scheduled run leaves this at 0 since
          21:30 UTC ≈ 17:30 ET is post-close.
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
    """The 'active' / list-of-tickers code path. Multi-symbol fetches.

    Lifted out of ``handle_daily_bars`` so the discovery sweep
    (``all_active``) can live as its own helper without making the entry
    point unreadable. Uses Alpaca's ``/v2/stocks/bars?symbols=…`` multi
    endpoint in 100-symbol chunks (2026-05-15) — the prior per-symbol
    loop was a ~45-min rate-limit floor on the ~7,669-ticker universe.
    """
    import asyncio
    from datetime import timedelta

    import httpx

    from tpcore.data.ingest_alpaca_bars import (
        _RATE_LIMIT_SLEEP_SEC,
        _alpaca_headers,
        _upsert_bars,
        fetch_daily_bars_multi,
    )

    # Alpaca's /v2/stocks/bars multi endpoint accepts up to 100 symbols
    # per call. Chunking the universe collapses ~7,669 single-symbol
    # calls (a ~45-min rate-limit floor) into ~77 calls — minutes, not
    # hours. Same endpoint handle_corporate_actions + the all_active
    # sweep already use.
    _MULTI_CHUNK = 100

    lookback_days = int(config.get("lookback_days", 7))
    end_offset_days = int(config.get("end_offset_days", 0))
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
    end = today - timedelta(days=end_offset_days)
    start = end - timedelta(days=lookback_days)

    headers = _alpaca_headers()
    total_rows = 0
    failures: list[str] = []
    # CSV-first archive: collect every bar across symbols, write once
    # at the end (one archive per run). The shrinkage detector picks up
    # if Alpaca silently drops bars next run.
    archive_rows: list[dict] = []
    async with httpx.AsyncClient(
        headers=headers,
        base_url="https://data.alpaca.markets",
        timeout=60.0,
    ) as client:
        for i in range(0, len(symbols), _MULTI_CHUNK):
            chunk = symbols[i : i + _MULTI_CHUNK]
            try:
                by_symbol = await fetch_daily_bars_multi(client, chunk, start, end)
            except httpx.HTTPStatusError as exc:
                # Whole chunk failed (e.g. SIP end=today 403 mid-session,
                # or 429 still failing after @with_retry backoff). Record
                # the chunk's symbols and continue — one bad chunk must
                # not abort the rest of the universe.
                failures.append(
                    f"chunk[{chunk[0]}..{chunk[-1]}]({exc.response.status_code})"
                )
                await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
                continue
            for symbol, bars in by_symbol.items():
                if not bars:
                    continue
                for b in bars:
                    archive_rows.append({
                        "ticker": symbol, "date": b.get("t", ""),
                        "open": b.get("o", ""), "high": b.get("h", ""),
                        "low": b.get("l", ""), "close": b.get("c", ""),
                        "volume": b.get("v", ""), "vwap": b.get("vw", ""),
                    })
                inserted = await _upsert_bars(pool, symbol, bars, delisted=False)
                total_rows += inserted
            await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)

    # CSV-first audit archive. daily_bars pulls a VARIABLE window
    # (7-day incremental refresh vs 6000-day backfill), so row-count
    # shrinkage detection is noise here — the archive's value is the
    # audit trail (reconstruct what Alpaca returned on a given run),
    # not a vendor-truncation alarm. Shrinkage detection is reserved
    # for the full-snapshot sources (fred_macro, corporate_actions).
    from tpcore.ingestion.csv_archive import write_archive
    archive = write_archive(
        "alpaca_daily_bars", archive_rows,
        fieldnames=["ticker", "date", "open", "high", "low", "close", "volume", "vwap"],
        validator=lambda r: bool(r.get("ticker")) and r.get("date") not in ("", None),
    )

    n_chunks = (len(symbols) + _MULTI_CHUNK - 1) // _MULTI_CHUNK
    logger.info(
        "ingestion.handler.daily_bars_done",
        symbols=len(symbols),
        chunks=n_chunks,
        rows_upserted=total_rows,
        failures=len(failures),
        csv_archive=str(archive.path),
    )
    if failures:
        raise RuntimeError(
            f"daily_bars: {len(failures)} chunk fetch failure(s); first: {failures[0]}"
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
                # Daily ingestion uses IEX, not the SIP default. Alpaca's
                # paper free tier returns 403 on SIP for live data; the
                # historical backfill uses SIP (where the free tier allows
                # >15-min-old data), but this is the end-of-day daily
                # path which must work without a paid SIP subscription.
                # Tradeoff per CLAUDE.md: IEX silently misses tickers that
                # trade off-IEX (e.g., some ADRs / OTC names); the daily
                # path accepts that miss rather than failing entirely.
                bars_by_symbol = await fetch_daily_bars_multi(
                    data, batch, start, today, feed="iex"
                )
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


async def handle_sec_filings(pool: asyncpg.Pool, config: dict[str, Any]) -> int | None:
    """SEC EDGAR Form 4 + 8-K ingest. CSV-first per the standard pipeline.

    Workflow (per docs/superpowers/pipelines/data_adapter_pipeline.md
    ingest sub-protocol):

    1. **download** — adapter pulls submissions index for each T1+T2
       stock; for each Form 4 also fetches and parses the XML body.
       Rows written to two CSVs under ``data/sec_backfill/``:
       ``sec_insider_<run-stamp>.csv`` and ``sec_material_<run-stamp>.csv``.
       Every row passes the physical-truth predicate at the CSV-write
       boundary.
    2. **validate** — at CSV-write: shares > 0, price >= 0, value >= 0,
       transaction_type ∈ {BUY, SELL}; event_type non-empty. Rejected
       rows logged with reason; never enter the loader.
    3. **load** — CSV → DB via ``INSERT ... ON CONFLICT DO NOTHING``.
       Idempotent: second run of the same CSV inserts zero new rows.
    4. **compress** — gzip both CSVs in-place on successful upsert.

    ``config`` keys:
        * ``lookback_days``: how far back to scan submission indexes
          (default 90). Wider windows pull more historical filings.
        * ``max_tickers``: hard cap per run (default 200) to keep the
          stage well under the SEC's 10 req/sec budget. Set to ``None``
          to ingest the entire stock universe.
        * ``skip_guard_days``: skip if last ingest landed within this
          many days (default 6). Set to 0 to force-rerun.

    Returns ``rows_loaded`` (sum across both tables). The structured
    success event includes per-table counts, ticker coverage, and the
    csv artifact paths so the operator can reconcile without opening
    the database.
    """
    from datetime import timedelta
    from pathlib import Path

    # ── 0. Skip guard ────────────────────────────────────────────────
    # Default tightened 6 → 3 days 2026-05-14 (audit cadence finding):
    # Form 4 has a 2-business-day filing deadline so 6-day staleness was
    # half-stale on average. 3 days keeps signal value while still
    # skipping back-to-back runs in the daily pipeline.
    skip_days = int(config.get("skip_guard_days", 3))
    if skip_days > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                """
                SELECT GREATEST(
                    COALESCE((SELECT MAX(recorded_at) FROM platform.sec_insider_transactions), '-infinity'::timestamptz),
                    COALESCE((SELECT MAX(recorded_at) FROM platform.sec_material_events),     '-infinity'::timestamptz)
                )
                """
            )
        if newest is not None and newest != datetime.min.replace(tzinfo=UTC):
            age = datetime.now(UTC) - newest
            if age.days < skip_days:
                logger.info(
                    "ingestion.handler.sec_filings.skipped_fresh",
                    last_refresh_age_days=age.days,
                )
                return 0

    # ── 1. Universe: T1+T2 stocks (not ETFs/funds/SPACs) ─────────────
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN platform.ticker_classifications tc USING (ticker)
            WHERE lt.tier <= 2
              AND COALESCE(tc.asset_class, 'stock') = 'stock'
            ORDER BY lt.ticker
            """
        )
    universe = [r["ticker"] for r in rows]
    max_tickers = config.get("max_tickers", 200)
    if max_tickers is not None and len(universe) > int(max_tickers):
        universe = universe[: int(max_tickers)]

    if not universe:
        logger.info(
            "ingestion.handler.sec_filings.empty_universe",
            reason="no T1+T2 stocks in liquidity_tiers",
        )
        return 0

    lookback_days = int(config.get("lookback_days", 90))
    since = datetime.now(UTC).date() - timedelta(days=lookback_days)

    # ── 2. CSV artifact paths ────────────────────────────────────────
    repo_root = Path(__file__).resolve().parent.parent.parent
    csv_dir = repo_root / "data" / "sec_backfill"
    csv_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    insider_csv = csv_dir / f"sec_insider_{run_stamp}.csv"
    material_csv = csv_dir / f"sec_material_{run_stamp}.csv"

    # ── 3. Download + validate-at-CSV-write ──────────────────────────
    insider_rows, material_rows, downloaded, rejected, ticker_hits = (
        await _sec_download_to_csv(
            universe, since, insider_csv, material_csv,
        )
    )

    # ── 4. Load CSVs → DB (ON CONFLICT DO NOTHING) ───────────────────
    loaded_insider, loaded_material = await _sec_load_csvs_to_db(
        pool, insider_rows, material_rows,
    )

    # ── 5. Compress source CSVs on success ───────────────────────────
    _gzip_in_place(insider_csv)
    _gzip_in_place(material_csv)

    rows_loaded = loaded_insider + loaded_material
    logger.info(
        "ingestion.handler.sec_filings.done",
        rows_downloaded=downloaded,
        rows_rejected_at_csv_layer=rejected,
        rows_loaded=rows_loaded,
        insider_loaded=loaded_insider,
        material_loaded=loaded_material,
        tickers_attempted=len(universe),
        tickers_with_filings=ticker_hits,
        date_range_start=since.isoformat(),
        date_range_end=datetime.now(UTC).date().isoformat(),
        csv_insider=str(insider_csv) + ".gz",
        csv_material=str(material_csv) + ".gz",
    )
    return rows_loaded


# ── SEC helpers ───────────────────────────────────────────────────────


async def _sec_download_to_csv(
    universe: list[str],
    since: Any,  # datetime.date
    insider_csv: Any,  # Path
    material_csv: Any,  # Path
) -> tuple[list[tuple], list[tuple], int, int, int]:
    """Adapter loop — pulls submission indexes + Form 4 XMLs, writes CSV.

    Returns ``(insider_rows, material_rows, downloaded, rejected, ticker_hits)``.
    Bad rows (physical-truth failures) are counted under ``rejected`` and
    NOT written to either CSV — the loader never sees them.
    """
    import csv as _csv

    from tpcore.sec.edgar_adapter import SECEdgarAdapter

    insider_rows: list[tuple] = []
    material_rows: list[tuple] = []
    downloaded = rejected = ticker_hits = 0

    insider_csv_h = open(insider_csv, "w", newline="", encoding="utf-8")
    material_csv_h = open(material_csv, "w", newline="", encoding="utf-8")
    try:
        ins_writer = _csv.writer(insider_csv_h)
        mat_writer = _csv.writer(material_csv_h)
        ins_writer.writerow([
            "ticker", "filing_date", "insider_name", "transaction_type",
            "shares", "price", "value",
        ])
        mat_writer.writerow(["ticker", "filing_date", "event_type", "summary"])

        async with SECEdgarAdapter() as sec:
            for ticker in universe:
                try:
                    filings = await sec.get_recent_filings(
                        ticker, forms=("4", "8-K"), since=since,
                    )
                except Exception as exc:
                    logger.warning(
                        "ingestion.handler.sec_filings.ticker_failed",
                        ticker=ticker, error=str(exc),
                    )
                    continue
                if not filings:
                    continue
                ticker_hits += 1

                for f in filings:
                    downloaded += 1
                    form = f["form"]
                    if form == "4":
                        try:
                            xml_text = await sec.fetch_form4_xml(
                                f["cik"], f["accession_number"],
                                f["primary_document"],
                            )
                        except Exception as exc:
                            logger.debug(
                                "ingestion.handler.sec_filings.form4_fetch_failed",
                                ticker=ticker, accession=f["accession_number"],
                                error=str(exc),
                            )
                            rejected += 1
                            continue
                        tx_rows, skipped = sec.parse_form4_transactions(
                            xml_text, ticker, f["filing_date"],
                        )
                        rejected += skipped
                        for row in tx_rows:
                            # CSV-layer physical-truth gate.
                            if not _insider_row_ok(row):
                                rejected += 1
                                continue
                            ins_writer.writerow([
                                row["ticker"],
                                row["filing_date"].isoformat(),
                                row["insider_name"],
                                row["transaction_type"],
                                row["shares"],
                                f"{row['price']}",
                                f"{row['value']}",
                            ])
                            insider_rows.append((
                                row["ticker"],
                                row["filing_date"],
                                row["insider_name"],
                                row["transaction_type"],
                                row["shares"],
                                row["price"],
                                row["value"],
                            ))
                    elif form == "8-K":
                        items = sec.parse_8k_items(f["items"])
                        for item_code in items:
                            if not _material_row_ok(item_code):
                                rejected += 1
                                continue
                            mat_writer.writerow([
                                ticker,
                                f["filing_date"].isoformat(),
                                item_code,
                                "",
                            ])
                            material_rows.append((
                                ticker,
                                f["filing_date"],
                                item_code,
                                None,
                            ))
    finally:
        insider_csv_h.close()
        material_csv_h.close()
    return insider_rows, material_rows, downloaded, rejected, ticker_hits


def _insider_row_ok(row: dict[str, Any]) -> bool:
    """Physical-truth predicate for insider rows — mirrors the table's
    CHECK constraints so a CSV-layer rejection is exactly equivalent
    to what would be rejected at INSERT time. Defense-in-depth: the
    DB CHECK is the ultimate guard; this stops bad rows earlier.
    """
    if row["transaction_type"] not in ("BUY", "SELL"):
        return False
    if int(row["shares"]) <= 0:
        return False
    from decimal import Decimal as _Decimal
    if _Decimal(row["price"]) < 0:
        return False
    if _Decimal(row["value"]) < 0:
        return False
    return True


def _material_row_ok(event_type: str) -> bool:
    return bool(event_type and event_type.strip())


async def _sec_load_csvs_to_db(
    pool: asyncpg.Pool,
    insider_rows: list[tuple],
    material_rows: list[tuple],
) -> tuple[int, int]:
    """Idempotent upsert. ON CONFLICT DO NOTHING on both unique keys."""
    loaded_insider = loaded_material = 0
    if insider_rows:
        async with pool.acquire() as conn:
            res = await conn.executemany(
                """
                INSERT INTO platform.sec_insider_transactions
                    (ticker, filing_date, insider_name, transaction_type,
                     shares, price, value)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (ticker, filing_date, insider_name,
                             transaction_type, shares) DO NOTHING
                """,
                insider_rows,
            )
        # asyncpg.executemany returns None; treat all input rows as
        # "loaded or already present". The accurate "new" count would
        # need RETURNING, which executemany doesn't surface.
        loaded_insider = len(insider_rows)
        del res
    if material_rows:
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO platform.sec_material_events
                    (ticker, filing_date, event_type, summary)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (ticker, filing_date, event_type) DO NOTHING
                """,
                material_rows,
            )
        loaded_material = len(material_rows)
    return loaded_insider, loaded_material


def _gzip_in_place(path: Any) -> None:
    """Compress ``path`` to ``path.gz`` and delete the original.

    Mirrors the pattern in ``scripts/compress_backfill_csvs.py``. No-op
    if the source doesn't exist (e.g., zero filings → empty file kept
    for audit, not gzipped twice).
    """
    import gzip
    import shutil
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        return
    gz_path = p.with_suffix(p.suffix + ".gz")
    with open(p, "rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    p.unlink()


async def _ingest_macro_hist_csv(
    pool: asyncpg.Pool, csv_path: str, indicator: str
) -> int | None:
    """One-time historical CSV → ``platform.macro_indicators`` for a
    single indicator. Idempotent (``ON CONFLICT (indicator, date) DO
    NOTHING``) — re-running inserts nothing and never overwrites or
    deletes other indicators (e.g. ``credit_spread``/BAA10Y) or the
    live truncated rows of this same indicator.

    Parsing reuses the FRED adapter's own date/value parsers so the
    ``"."`` missing-marker and value semantics are byte-identical to
    the live API path. Archived under the distinct ``fred_macro_hist``
    source so the one-off volume does not poison the recurring
    ``fred_macro`` shrinkage comparator.
    """
    import csv as _csv
    from pathlib import Path as _Path

    from tpcore.fred.adapter import _parse_observation_date, _parse_value
    from tpcore.ingestion.csv_archive import write_archive

    rows = list(_csv.reader(_Path(csv_path).open()))
    if not rows or len(rows) < 2:
        raise RuntimeError(f"macro hist csv {csv_path}: empty or header-only")
    body = rows[1:]  # skip header (DATE,<series>)

    upsert_rows: list[tuple] = []
    skipped_missing = 0
    for r in body:
        if len(r) < 2:
            continue
        d = _parse_observation_date(r[0])
        v = _parse_value(r[1])
        if d is None:
            continue
        if v is None:  # "." / blank — FRED missing marker, skip (not zero)
            skipped_missing += 1
            continue
        upsert_rows.append((indicator, d, v))

    if not upsert_rows:
        raise RuntimeError(
            f"macro hist csv {csv_path}: zero parseable rows for {indicator}"
        )

    archive = write_archive(
        "fred_macro_hist",
        [{"indicator": indicator, "date": d.isoformat(), "value": str(v)}
         for (_, d, v) in upsert_rows],
        fieldnames=["indicator", "date", "value"],
        validator=lambda x: bool(x.get("indicator")) and x.get("date") is not None,
    )

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.macro_indicators (indicator, date, value)
            VALUES ($1, $2, $3)
            ON CONFLICT (indicator, date) DO NOTHING
            """,
            upsert_rows,
        )

    dmin = min(d for _, d, _ in upsert_rows)
    dmax = max(d for _, d, _ in upsert_rows)
    logger.info(
        "ingestion.handler.macro_indicators.hist_csv_done",
        indicator=indicator,
        rows_parsed=len(upsert_rows),
        skipped_missing=skipped_missing,
        date_min=dmin.isoformat(),
        date_max=dmax.isoformat(),
        csv_archive=str(archive.path),
        source_csv=csv_path,
    )
    return len(upsert_rows)


async def handle_macro_indicators(
    pool: asyncpg.Pool, config: dict[str, Any],
) -> int | None:
    """FRED macro-indicators ingest. Weekly stage (Monday 08:00 UTC).

    Pulls the five canonical series via ``tpcore.fred.FREDAdapter`` and
    upserts into ``platform.macro_indicators`` with ``ON CONFLICT
    (indicator, date) DO NOTHING``. Idempotent — second run within the
    7-day skip-guard window returns 0 new rows.

    ``config`` keys:
        * ``start_date``: ISO date, default ``"2018-01-01"`` (full
          backtest-overlap window).
        * ``skip_guard_days``: skip if MAX(recorded_at) is within this
          many days (default 7). Pass 0 to force-rerun.

    Returns ``rows_loaded`` across all indicators. Structured success
    event includes per-indicator counts + date range so the operator
    can reconcile without opening the DB.
    """
    from datetime import date as _date

    from tpcore.fred import INDICATOR_SERIES, FREDAdapter

    # ── One-time historical CSV backfill (canonical knob, not a one-off
    # script). FRED + ALFRED permanently truncated BAMLH0A0HYM2 to a
    # rolling 3yr window (verified 2026-05-16: retroactive across all
    # ALFRED vintages). The pre-truncation history survives only in an
    # external CSV archive. This branch ingests such a CSV for ONE named
    # indicator, idempotently, without disturbing any other series.
    #     ops.py --stage macro_indicators \
    #       --param hist_csv_path=<file> --param hist_indicator=hy_spread --force
    hist_csv = config.get("hist_csv_path")
    hist_ind = config.get("hist_indicator")
    if hist_csv and hist_ind:
        return await _ingest_macro_hist_csv(pool, str(hist_csv), str(hist_ind))

    # ── 0. Skip guard ────────────────────────────────────────────────
    skip_days = int(config.get("skip_guard_days", 7))
    if skip_days > 0:
        async with pool.acquire() as conn:
            newest = await conn.fetchval(
                "SELECT MAX(recorded_at) FROM platform.macro_indicators"
            )
        if newest is not None:
            age = datetime.now(UTC) - newest
            if age.days < skip_days:
                logger.info(
                    "ingestion.handler.macro_indicators.skipped_fresh",
                    last_refresh_age_days=age.days,
                )
                return 0

    start_raw = config.get("start_date", "2018-01-01")
    start = _date.fromisoformat(start_raw) if isinstance(start_raw, str) else start_raw

    # ── 1. Fetch all five series ─────────────────────────────────────
    per_indicator: dict[str, list[dict[str, Any]]] = {}
    async with FREDAdapter() as fred:
        per_indicator = await fred.get_all_indicators(start=start)

    # ── 2. Bulk upsert ───────────────────────────────────────────────
    upsert_rows: list[tuple] = []
    for name, _ in INDICATOR_SERIES:
        for obs in per_indicator.get(name, []):
            upsert_rows.append((name, obs["date"], obs["value"]))

    if not upsert_rows:
        logger.info(
            "ingestion.handler.macro_indicators.empty",
            reason="all series returned zero observations",
        )
        return 0

    # ── 2a. CSV-first archive (BAMLH0A0HYM2 truncation defence) ──────
    # Write the full vendor response to a gzipped CSV BEFORE the upsert.
    # If FRED retroactively truncates a series again, this archive is
    # the only place the pre-truncation history survives. Shrinkage
    # detection on the next run compares this archive to its predecessor.
    from tpcore.ingestion.csv_archive import (
        detect_shrinkage,
        log_shrinkage_warning,
        write_archive,
    )

    csv_rows = [
        {"indicator": name, "date": d, "value": str(value)}
        for (name, d, value) in upsert_rows
    ]
    archive = write_archive(
        "fred_macro", csv_rows,
        fieldnames=["indicator", "date", "value"],
        validator=lambda r: bool(r.get("indicator")) and r.get("date") is not None,
    )
    shrinkage = detect_shrinkage(
        "fred_macro", archive.rows_written, exclude_path=archive.path,
    )
    if shrinkage is not None:
        log_shrinkage_warning(shrinkage)

    # ── 3. Load CSV → DB (ON CONFLICT DO NOTHING) ────────────────────
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.macro_indicators (indicator, date, value)
            VALUES ($1, $2, $3)
            ON CONFLICT (indicator, date) DO NOTHING
            """,
            upsert_rows,
        )

    summary: dict[str, dict[str, Any]] = {}
    for name, _ in INDICATOR_SERIES:
        obs = per_indicator.get(name, [])
        if obs:
            summary[name] = {
                "rows": len(obs),
                "date_min": obs[0]["date"].isoformat(),
                "date_max": obs[-1]["date"].isoformat(),
            }
        else:
            summary[name] = {"rows": 0, "reason": "no observations returned"}

    logger.info(
        "ingestion.handler.macro_indicators.done",
        rows_upserted=len(upsert_rows),
        per_indicator=summary,
        csv_archive=str(archive.path),
        shrinkage_over_threshold=shrinkage.over_threshold if shrinkage else False,
    )
    return len(upsert_rows)


async def handle_greeks_max_pain(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> int | None:
    """greeks.pro free-tier max-pain ingest (1 symbol, daily snapshot).

    CSV-first → idempotent upsert. Skip-guard: if today's
    ``observed_date`` already has rows for the symbol, no-op (saves a
    call against the 600/day free quota; the upsert is idempotent
    regardless). ``config`` keys: ``symbol`` (default ``"SPY"``),
    ``skip_guard`` (default True; pass False to force a re-pull).
    """
    from datetime import UTC, datetime

    from tpcore.greeks import GreeksProAdapter
    from tpcore.ingestion.csv_archive import write_archive

    symbol = str(config.get("symbol", "SPY")).upper()
    skip_guard = bool(config.get("skip_guard", True))
    today = datetime.now(UTC).date()

    if skip_guard:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT COUNT(*) FROM platform.options_max_pain
                WHERE symbol = $1 AND observed_date = $2
                """,
                symbol, today,
            )
        if existing and existing > 0:
            logger.info(
                "ingestion.handler.greeks_max_pain.skipped_fresh",
                symbol=symbol, observed_date=today.isoformat(),
            )
            return 0

    async with GreeksProAdapter() as adapter:
        snap = await adapter.get_max_pain(symbol)

    obs_date = snap.observed_at.date()
    rows = [
        (
            snap.symbol, r.expiration_date.date(), obs_date, r.dte,
            snap.spot_price, r.max_pain_strike, r.total_pain_at_max,
            r.spot_distance, r.spot_distance_pct, snap.observed_at,
        )
        for r in snap.results
    ]
    if not rows:
        logger.info(
            "ingestion.handler.greeks_max_pain.empty",
            symbol=symbol, reason="provider returned zero expirations",
        )
        return 0

    write_archive(
        "greeks_max_pain",
        [
            {
                "symbol": s, "expiration_date": ed.isoformat(),
                "observed_date": od.isoformat(), "dte": str(d),
                "spot_price": str(sp), "max_pain_strike": str(mp),
                "total_pain_at_max": str(tp), "spot_distance": str(sd),
                "spot_distance_pct": str(sdp), "observed_at": oa.isoformat(),
            }
            for (s, ed, od, d, sp, mp, tp, sd, sdp, oa) in rows
        ],
        fieldnames=[
            "symbol", "expiration_date", "observed_date", "dte",
            "spot_price", "max_pain_strike", "total_pain_at_max",
            "spot_distance", "spot_distance_pct", "observed_at",
        ],
        validator=lambda x: bool(x.get("symbol")) and bool(x.get("expiration_date")),
    )

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO platform.options_max_pain
                (symbol, expiration_date, observed_date, dte, spot_price,
                 max_pain_strike, total_pain_at_max, spot_distance,
                 spot_distance_pct, observed_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (symbol, expiration_date, observed_date) DO NOTHING
            """,
            rows,
        )

    logger.info(
        "ingestion.handler.greeks_max_pain.done",
        symbol=symbol, observed_date=obs_date.isoformat(),
        expirations=len(rows), spot_price=str(snap.spot_price),
    )
    return len(rows)


HANDLERS: dict[str, HandlerFn] = {
    "data_validation": handle_data_validation,
    "fundamentals_refresh": handle_fundamentals_refresh,
    "corporate_actions": handle_corporate_actions,
    "daily_bars": handle_daily_bars,
    "sec_filings": handle_sec_filings,
    "macro_indicators": handle_macro_indicators,
    "greeks_max_pain": handle_greeks_max_pain,
}


__all__ = [
    "HANDLERS",
    "HandlerFn",
    "handle_data_validation",
    "handle_fundamentals_refresh",
    "handle_corporate_actions",
    "handle_daily_bars",
    "handle_sec_filings",
    "handle_macro_indicators",
    "handle_greeks_max_pain",
]
