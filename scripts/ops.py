"""Maintenance CLI for the Short-Term Trading Engine platform.

Three top-level commands:

    python scripts/ops.py --update          # run the 6 maintenance stages
    python scripts/ops.py --check           # read-only health report (JSON)
    python scripts/ops.py --check --pretty  # …same, formatted for a human
    python scripts/ops.py --full            # --update then --check
    python scripts/ops.py --help            # this usage block

The CLI is the single entry point for daily/weekly data maintenance. It
replaces the previous mix of ad-hoc scripts (`run_daily_bars_all_active.py`,
`run_corporate_actions_all_active.py`, etc.) and Railway-dependent ingestion
checks. Every stage delegates to the existing `tpcore` handlers — no
data-pull logic is re-implemented here.

Logging goes to two places:

* stdout — structlog, human-readable, useful while watching a run live.
* `platform.application_log` — permanent audit trail keyed by a UUID
  `run_id` generated once at startup, engine=`ops`. The same handler
  enforces the 7-day rolling retention.

Every external stage in `--update` is hard-timeout limited to 120 seconds.
A timeout logs an ERROR event and the pipeline continues to the next
stage. In `--dry-run` mode, no data is fetched or written — but log
events are still emitted so a dry-run leaves an audit trail.

Required env vars (checked at startup, hard-fail on miss):

* `--check` only:                      DATABASE_URL
* `--update` / `--full`:               DATABASE_URL, ALPACA_KEY,
                                       ALPACA_SECRET, FMP_API_KEY

See `docs/OPERATIONS.md` for the operator's runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

# Module under tpcore — imports happen lazily inside stages where possible
# so `--help` works in environments missing optional deps. The required
# few are imported at top level.

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENGINE_NAME = "ops"
STAGE_TIMEOUT_SEC = 120.0
# Heavy ingestion stages need longer than the 120s default. Post the
# Phase 1 universe expansion (~7,300 tickers in prices_daily, ~5,981 in
# fundamentals_quarterly), the original 120s budget cuts off these
# handlers mid-batch and leaves the database in a partial-update state.
HEAVY_STAGE_TIMEOUT_SEC = 3600.0  # 60 minutes (1200s was still tripping on ~7,300 tickers)
# SEC EDGAR full historical backfill spans 2018-01-01 to today across
# ~66 T1+T2 stocks, each with hundreds of Form 4 + 8-K filings. At
# SEC's 8 req/sec courtesy budget the worst case is several thousand
# fetches, which can run 2-4 hours. 6h gives headroom without
# silently masking a real hang. Only affects the sec_filings stage;
# every other stage stays on HEAVY_STAGE_TIMEOUT_SEC.
SEC_FILINGS_STAGE_TIMEOUT_SEC = 21600.0  # 6 hours
DATA_FRESHNESS_MAX_DAYS = 4  # 2 trading days + weekend buffer
CORP_ACTIONS_FRESHNESS_MAX_DAYS = 7

# Row-count expected minimums — pulled from docs/OPERATIONS.md §3.
# A drop below these triggers a WARNING in --check output.
EXPECTED_MIN_ROWS: dict[str, int] = {
    "platform.prices_daily": 300_000,
    "platform.fundamentals_quarterly": 1_700,
    "platform.corporate_actions": 1_200,
    "platform.catalyst_events": 600,
}


# ────────────────────────────────────────────────────────────────────────
# Logging setup
# ────────────────────────────────────────────────────────────────────────


def _configure_logging(run_id: uuid.UUID, level: int = logging.INFO) -> structlog.stdlib.BoundLogger:
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.KeyValueRenderer(
                key_order=["timestamp", "level", "event", "run_id", "stage"]
            ),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    structlog.contextvars.bind_contextvars(run_id=str(run_id))
    return structlog.get_logger("scripts.ops")


# ────────────────────────────────────────────────────────────────────────
# Env validation
# ────────────────────────────────────────────────────────────────────────


def _require_env(names: list[str]) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(
            f"ops: required environment variable(s) not set: {', '.join(missing)}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _require_alpaca_env() -> None:
    # `_alpaca_headers()` accepts either short or `_API_` pair. Mirror that.
    has_key = os.environ.get("ALPACA_KEY") or os.environ.get("ALPACA_API_KEY")
    has_sec = os.environ.get("ALPACA_SECRET") or os.environ.get("ALPACA_API_SECRET")
    if not (has_key and has_sec):
        print(
            "ops: ALPACA_KEY/ALPACA_SECRET (or ALPACA_API_KEY/ALPACA_API_SECRET) not set",
            file=sys.stderr,
        )
        raise SystemExit(2)


# ────────────────────────────────────────────────────────────────────────
# Stage result + summary
# ────────────────────────────────────────────────────────────────────────


@dataclass
class StageResult:
    name: str
    status: str  # "OK" | "FAILED" | "TIMEOUT" | "SKIPPED" | "DRY_RUN"
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class UpdateSummary:
    run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    stages: list[StageResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        # Non-zero if any non-skipped stage failed. Dry-run is never an error.
        for s in self.stages:
            if s.status in ("FAILED", "TIMEOUT"):
                return 1
        return 0

    def to_table(self) -> str:
        rows: list[tuple[str, str, str, str]] = [("Stage", "Status", "Duration", "Detail")]
        for s in self.stages:
            secs = f"{s.duration_ms / 1000:.1f}s"
            d = s.error or ", ".join(f"{k}={v}" for k, v in s.detail.items())
            rows.append((s.name, s.status, secs, d[:80]))
        widths = [max(len(r[i]) for r in rows) for i in range(4)]
        out: list[str] = []
        for i, r in enumerate(rows):
            out.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(r)))
            if i == 0:
                out.append("  ".join("-" * widths[j] for j in range(4)))
        return "\n".join(out)


# ────────────────────────────────────────────────────────────────────────
# Configuration loader
# ────────────────────────────────────────────────────────────────────────


async def _load_daily_bars_config(pool: asyncpg.Pool) -> dict[str, Any]:
    """Read the config JSON from the `daily_bars` row of platform.ingestion_jobs.

    Hard-fails if the row is missing — the CLI is explicit that filter
    values must come from the database, not be hardcoded.
    """
    row = await pool.fetchrow("SELECT config FROM platform.ingestion_jobs WHERE job_name = 'daily_bars'")
    if row is None:
        raise RuntimeError(
            "ops: platform.ingestion_jobs has no row for job_name='daily_bars'. "
            "Seed it before running --update. Example:\n"
            "  INSERT INTO platform.ingestion_jobs (job_name, schedule, provider, config) "
            "VALUES ('daily_bars', '@daily', 'alpaca', "
            '\'{"universe": "all_active", "lookback_days": 7, '
            '"min_price": 5, "min_volume": 250000}\'::jsonb);'
        )
    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    return dict(cfg)


async def _coarse_filtered_universe(
    pool: asyncpg.Pool,
    *,
    min_price: float,
    min_volume: int,
    lookback_days: int,
) -> list[str]:
    """Tickers in `prices_daily` that pass the coarse filter over the lookback.

    Mirrors the filter `_handle_daily_bars_all_active` applies (last close
    above `min_price`, average volume above `min_volume`) but evaluated on
    the bars already present in the DB — no API calls.
    """
    sql = """
        WITH recent AS (
            SELECT ticker, close, volume, date,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM platform.prices_daily
            WHERE date >= CURRENT_DATE - ($1::int || ' days')::interval
              AND delisted = false
        )
        SELECT ticker
        FROM recent
        GROUP BY ticker
        HAVING MAX(close) FILTER (WHERE rn = 1) > $2
           AND AVG(volume) > $3
        ORDER BY ticker
    """
    rows = await pool.fetch(sql, lookback_days, min_price, min_volume)
    return [r["ticker"] for r in rows]


# ────────────────────────────────────────────────────────────────────────
# Stage runner
# ────────────────────────────────────────────────────────────────────────


async def _run_stage(
    name: str,
    coro_factory,
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
    timeout: float = STAGE_TIMEOUT_SEC,
    dry_run: bool = False,
) -> StageResult:
    """Run one stage with timeout + structured logging.

    `coro_factory` is a zero-arg callable that returns the awaitable; it
    is only invoked when `dry_run` is False so dry-run paths never start
    real work.
    """
    log = log.bind(stage=name)
    started = time.monotonic()
    await db_log.log(
        "INGESTION_START",
        f"{name} starting",
        severity="INFO",
        data={"stage": name, "dry_run": dry_run},
    )
    log.info("ops.stage.start", dry_run=dry_run)

    if dry_run:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("ops.stage.dry_run", duration_ms=elapsed_ms)
        await db_log.log(
            "INGESTION_COMPLETE",
            f"{name} dry-run (skipped)",
            severity="INFO",
            data={"stage": name, "dry_run": True, "duration_ms": elapsed_ms},
        )
        return StageResult(name=name, status="DRY_RUN", duration_ms=elapsed_ms)

    try:
        detail = await asyncio.wait_for(coro_factory(), timeout=timeout)
    except TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.error("ops.stage.timeout", duration_ms=elapsed_ms, timeout_sec=timeout)
        await db_log.log(
            "INGESTION_FAILED",
            f"{name} timed out after {timeout}s",
            severity="ERROR",
            data={"stage": name, "duration_ms": elapsed_ms, "reason": "timeout"},
        )
        return StageResult(
            name=name,
            status="TIMEOUT",
            duration_ms=elapsed_ms,
            error=f"timed out after {timeout}s",
        )
    except Exception as exc:  # noqa: BLE001 — surface, log, and continue
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.error("ops.stage.failed", duration_ms=elapsed_ms, error=str(exc))
        await db_log.log(
            "INGESTION_FAILED",
            f"{name} failed: {exc}",
            severity="ERROR",
            data={
                "stage": name,
                "duration_ms": elapsed_ms,
                "exception_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return StageResult(
            name=name,
            status="FAILED",
            duration_ms=elapsed_ms,
            error=str(exc),
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    detail_dict = detail if isinstance(detail, dict) else {"result": detail}
    log.info("ops.stage.complete", duration_ms=elapsed_ms, **detail_dict)
    await db_log.log(
        "INGESTION_COMPLETE",
        f"{name} complete",
        severity="INFO",
        data={"stage": name, "duration_ms": elapsed_ms, **detail_dict},
    )
    return StageResult(name=name, status="OK", duration_ms=elapsed_ms, detail=detail_dict)


# ────────────────────────────────────────────────────────────────────────
# Stage implementations
# ────────────────────────────────────────────────────────────────────────


async def _stage_daily_bars(pool: asyncpg.Pool, config: dict[str, Any]) -> dict[str, Any]:
    # Fast-path: if the most recent CLOSED NYSE session already has bars
    # for a healthy fraction of the active universe, this stage has
    # nothing to do. The 7,000-ticker threshold matches our observed
    # post-ingest population (we typically end up around 7,300 active
    # tickers). 50% would be too lax; 90% catches a stalled run.
    from tpcore.calendar import previous_close
    from tpcore.ingestion.handlers import handle_daily_bars

    target_session = previous_close(datetime.now(UTC)).date()
    async with pool.acquire() as conn:
        already_ingested = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT ticker)
            FROM platform.prices_daily
            WHERE date = $1
            """,
            target_session,
        )
    threshold = 6500  # tighter than the universe; leaves room for new IPOs
    if already_ingested and already_ingested >= threshold:
        return {
            "rows_upserted": 0,
            "universe": config.get("universe", "active"),
            "skipped": "already_ingested",
            "target_session": target_session.isoformat(),
            "tickers_present": already_ingested,
        }

    rows = await handle_daily_bars(pool, config)
    return {
        "rows_upserted": rows or 0,
        "universe": config.get("universe", "active"),
        "target_session": target_session.isoformat(),
    }


async def _stage_corporate_actions(pool: asyncpg.Pool) -> dict[str, Any]:
    from tpcore.ingestion.handlers import handle_corporate_actions

    rows = await handle_corporate_actions(pool, {"universe": "all_active"})
    return {"actions_ingested": rows or 0}


async def _stage_reconcile(pool: asyncpg.Pool) -> dict[str, Any]:
    """Reconcile ``platform.open_orders`` against Alpaca's authoritative state.

    Same code path TradeMonitor runs on startup (``reconcile_pending_on_startup``).
    Wired into the daily `--update` pipeline 2026-05-14 (audit gap G-3 fix):
    before this stage shipped, reconciliation only fired when the
    trade_monitor daemon restarted (KeepAlive → only crashes restart it),
    so orphan orders could accumulate silently between restarts.

    Idempotent and cheap (~5s typical). No skip-guard — runs every day.
    """
    import uuid as _uuid

    from tpcore.aar.writer import AARWriter
    from tpcore.alpaca import AlpacaPaperBrokerAdapter
    from tpcore.trade_monitor import TradeMonitor

    log = structlog.get_logger("scripts.ops")
    broker = AlpacaPaperBrokerAdapter()
    aar_writer = AARWriter(pool)
    monitor = TradeMonitor(
        pool=pool, broker=broker, aar_writer=aar_writer, run_id=_uuid.uuid4(),
    )
    reconciled = await monitor.reconcile_pending_on_startup()
    log.info("ops.stage.reconcile.done", reconciled_orders=int(reconciled or 0))
    return {"reconciled_orders": int(reconciled or 0)}


async def _stage_fundamentals_refresh(pool: asyncpg.Pool, config: dict[str, Any]) -> dict[str, Any]:
    """Refresh FMP fundamentals restricted to the coarse-filtered universe."""
    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fundamentals.cache import FundamentalsCache

    tickers = await _coarse_filtered_universe(
        pool,
        min_price=float(config.get("min_price", 5.0)),
        min_volume=int(config.get("min_volume", 250_000)),
        lookback_days=int(config.get("lookback_days", 7)),
    )
    if not tickers:
        return {"tickers": 0, "rows": 0, "no_data": 0, "failures": 0, "note": "empty universe"}

    async with FMPFundamentalsAdapter() as adapter:
        cache = FundamentalsCache(pool, adapter=adapter)
        # skip_if_refreshed_within_hours=24 makes this stage resumable:
        # if it timed out partway through, the next run picks up where it
        # left off; if it completed successfully today, the next run is a
        # near-instant no-op. Without this, the 1s per-symbol sleep means
        # the stage routinely exceeds its 1-hour timeout on the ~5k+
        # ticker universe — re-running solves nothing.
        rows, no_data, failures, skipped = await cache.backfill_all(
            tickers=tickers, skip_if_refreshed_within_hours=24.0,
        )

    detail = {
        "tickers": len(tickers),
        "rows": rows,
        "no_data": len(no_data),
        "failures": len(failures),
        "skipped_fresh": skipped,
    }
    if failures:
        # Match handler semantics: real FMP failures surface as an error
        # event for the stage, but the pipeline still continues.
        raise RuntimeError(
            f"fundamentals_refresh: {len(failures)} failure(s); first={failures[0][0]}: {failures[0][1]}"
        )
    return detail


async def _stage_cross_ref_cleanup(pool: asyncpg.Pool) -> dict[str, Any]:
    """Auto-clean known-safe cross-table integrity violations.

    The cross-table audit (scripts/audit_all_tables.py + dashboard's
    cross_ref panel) historically just *reported* violations and asked
    the operator to clean them. That's wrong: most of these are pure
    data-hygiene (e.g., options whose expiration_date passed) and have
    a single safe remediation — delete the row.

    What this stage cleans (additively — only add rules here for which
    "delete the row" is the proven-correct action):

    * ``tradier_options_chains`` rows with ``expiration_date < today``
      — frozen S2 table; expired contracts are permanently dead.
    * ``tradier_options_chains`` rows whose ticker has no row in
      ``prices_daily`` — orphan options, no underlying to price against.

    Returns counts so the operator can see what got cleaned. Idempotent
    by construction (same query, deletes shrink to zero next run).
    """
    def _count_from_status(status: str) -> int:
        # asyncpg execute() returns "DELETE N" — extract N.
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    async with pool.acquire() as conn:
        expired_status = await conn.execute(
            "DELETE FROM platform.tradier_options_chains WHERE expiration_date < CURRENT_DATE"
        )
        orphan_status = await conn.execute(
            """
            DELETE FROM platform.tradier_options_chains tc
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.prices_daily_tickers t
                WHERE t.ticker = tc.ticker
            )
            """
        )

    return {
        "deleted_expired_options": _count_from_status(expired_status),
        "deleted_orphan_options": _count_from_status(orphan_status),
    }


async def _stage_catalyst_refresh(pool: asyncpg.Pool) -> dict[str, Any]:
    """Weekly refresh of ``platform.catalyst_events`` (FMP earnings beats).

    Vector engine reads EARNINGS_BEAT events from this table. Earnings
    are quarterly so a daily refresh would waste FMP calls. The stage
    is idempotent and bounded:

    * **Skip guard** — if the table was refreshed within the last 6
      days, no-op. Use ``recorded_at`` (insert time) rather than
      ``event_date`` so we don't mistake "no new events this week"
      for "haven't refreshed."
    * **Universe** — T1+T2 stocks only (per
      ``ticker_classifications.asset_class='stock'``). ETFs/SPACs/
      funds have no earnings to beat; including them would waste ~80%
      of the FMP call budget.
    * **Window** — re-fetch all history back to 2018-01-01. The
      backfill script's INSERT ON CONFLICT pattern makes this safe.

    The stage fires through the same code path as the manual
    ``scripts/backfill_catalyst_events.py``, so behavior stays in
    lockstep between cron and operator-on-demand.
    """
    from datetime import date as _date
    from datetime import timedelta as _td
    from types import SimpleNamespace

    # Skip guard.
    async with pool.acquire() as conn:
        newest_recorded = await conn.fetchval(
            "SELECT MAX(recorded_at) FROM platform.catalyst_events"
        )
    log = structlog.get_logger("scripts.ops")
    if newest_recorded is not None:
        age = datetime.now(UTC) - newest_recorded
        if age.days < 6:
            log.info(
                "ops.stage.catalyst_refresh.skipped_fresh",
                last_refresh_age_days=age.days,
            )
            return {
                "skipped": True,
                "reason": "refreshed_within_6_days",
                "last_refresh_age_days": age.days,
            }

    # Build the addressable stock-class T1+T2 universe.
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
    if not universe:
        return {"tickers": 0, "inserted": 0, "note": "empty stock universe"}

    # Delegate to the existing backfill — same code path as the manual
    # script. The args namespace must shape exactly like its argparse
    # output (universe, start, end fields).
    from scripts.backfill_catalyst_events import amain as backfill_amain
    args = SimpleNamespace(
        universe=universe,
        start=_date(2018, 1, 1),
        end=datetime.now(UTC).date() - _td(days=1),
    )
    exit_code = await backfill_amain(args)
    if exit_code != 0:
        raise RuntimeError(
            f"catalyst_refresh: backfill_amain returned {exit_code}"
        )
    async with pool.acquire() as conn:
        post_count = await conn.fetchval("SELECT COUNT(*) FROM platform.catalyst_events")
        post_tickers = await conn.fetchval(
            "SELECT COUNT(DISTINCT ticker) FROM platform.catalyst_events"
        )
    return {
        "tickers": len(universe),
        "total_rows": int(post_count or 0),
        "covered_tickers": int(post_tickers or 0),
    }


async def _stage_tier_refresh(pool: asyncpg.Pool) -> dict[str, Any]:
    """Quarterly liquidity-tier refresh — bootstrap + aggregation.

    Two-phase, mirrors the operator's manual ``scripts/run_tier_refresh.sh``:

    1. **Corwin-Schultz spread bootstrap** — write fresh spread
       observations to ``platform.spread_observations`` so that step 2
       has something current to aggregate. Skipped if
       ``MAX(observed_at)`` is within 60 days (the bootstrap is
       expensive — ~20-30 min — and spreads themselves drift slower
       than tier *assignments* respond).
    2. **Tier aggregation** — re-run ``assign_tiers`` so every
       ticker's median spread → tier band lands in
       ``platform.liquidity_tiers``. Outer 90-day skip guard governs
       whether either phase runs at all.

    Closes audit gap G-2: prior to 2026-05-14 the stage only did
    phase 2, silently aggregating stale spread data forever if the
    operator never ran the wrapper script.
    """
    log = structlog.get_logger("scripts.ops")
    # Outer skip guard — gate the whole stage on liquidity_tiers freshness.
    newest = await pool.fetchval(
        "SELECT MAX(last_updated) FROM platform.liquidity_tiers"
    )
    if newest is not None:
        age = datetime.now(UTC) - newest
        if age.days < 90:
            log.info(
                "ops.stage.tier_refresh.skipped_fresh",
                last_refresh_age_days=age.days,
            )
            return {
                "skipped": True,
                "reason": "refreshed_within_90_days",
                "last_refresh_age_days": age.days,
            }

    import os

    from scripts.assign_liquidity_tiers import assign_tiers
    from tpcore.backtest.spread_estimator import rank_universe_by_liquidity

    # Phase 1 — bootstrap, gated by its own freshness check.
    newest_obs = await pool.fetchval(
        "SELECT MAX(observed_at) FROM platform.spread_observations "
        "WHERE source = 'corwin_schultz'"
    )
    bootstrap_skipped = False
    bootstrap_rows = 0
    if newest_obs is not None and (datetime.now(UTC) - newest_obs).days < 60:
        bootstrap_skipped = True
        log.info(
            "ops.stage.tier_refresh.bootstrap_skipped",
            spread_obs_age_days=(datetime.now(UTC) - newest_obs).days,
        )
    else:
        results = await rank_universe_by_liquidity(
            pool, persist=True, coarse_filter=False,
        )
        bootstrap_rows = len(results)
        log.info(
            "ops.stage.tier_refresh.bootstrap_done",
            spread_observations_written=bootstrap_rows,
            prior_obs_age_days=(
                (datetime.now(UTC) - newest_obs).days
                if newest_obs is not None else None
            ),
        )

    # Phase 2 — re-aggregate from spread_observations.
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        raise RuntimeError("tier_refresh: DATABASE_URL not set")
    sources = ["corwin_schultz"]
    bucket = await assign_tiers(db_url=db_url, sources=sources)
    log.info(
        "ops.stage.tier_refresh.done",
        tickers_assigned=sum(bucket.values()),
        tiers=bucket,
        bootstrap_skipped=bootstrap_skipped,
        bootstrap_rows=bootstrap_rows,
    )
    return {
        "tickers_assigned": sum(bucket.values()),
        "tiers": {int(k): int(v) for k, v in bucket.items()},
        "bootstrap_skipped": bootstrap_skipped,
        "bootstrap_rows": bootstrap_rows,
    }


async def _stage_classify_tickers(pool: asyncpg.Pool) -> dict[str, Any]:
    """Monthly ticker-classification refresh.

    Asset class is near-static — re-runs exist to pick up new
    listings (Phase-1 universe expansions, new SPAC IPOs). Skip-guard
    short-circuits when the table was touched within 30 days **and**
    coverage of the active universe is ≥ 95%. The second clause
    forces a re-run when a universe expansion has introduced
    unclassified tickers even if the table was recently touched.
    """
    log = structlog.get_logger("scripts.ops")

    # Skip guard — two conditions must BOTH hold for skip.
    snapshot = await pool.fetchrow(
        """
        SELECT
            (SELECT MAX(last_updated) FROM platform.ticker_classifications) AS latest,
            (SELECT COUNT(DISTINCT pd.ticker)
             FROM platform.prices_daily pd
             LEFT JOIN platform.ticker_classifications tc USING (ticker)
             WHERE pd.date >= CURRENT_DATE - INTERVAL '30 days'
               AND pd.delisted = false
               AND tc.ticker IS NULL) AS unclassified,
            (SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily
             WHERE date >= CURRENT_DATE - INTERVAL '30 days'
               AND delisted = false) AS active
        """
    )
    latest = snapshot["latest"] if snapshot else None
    unclassified = int(snapshot["unclassified"] or 0) if snapshot else 0
    active = int(snapshot["active"] or 0) if snapshot else 0
    coverage_pct = ((active - unclassified) / active) if active else 0.0

    if latest is not None and coverage_pct >= 0.95:
        age = datetime.now(UTC) - latest
        if age.days < 30:
            log.info(
                "ops.stage.classify_tickers.skipped_fresh",
                last_refresh_age_days=age.days,
                coverage_pct=round(coverage_pct, 3),
            )
            return {
                "skipped": True,
                "reason": "refreshed_within_30_days_and_coverage_sufficient",
                "last_refresh_age_days": age.days,
                "coverage_pct": round(coverage_pct, 3),
            }

    from tpcore.data.classify_tickers import classify_all_tickers
    from tpcore.data.ingest_alpaca_bars import _alpaca_broker_base, _alpaca_headers

    stats = await classify_all_tickers(
        pool,
        alpaca_base_url=_alpaca_broker_base(),
        alpaca_headers=_alpaca_headers(),
    )
    log.info("ops.stage.classify_tickers.done", **{str(k): int(v) for k, v in stats.items()})
    return {str(k): int(v) for k, v in stats.items()}


async def _stage_macro_indicators(pool: asyncpg.Pool) -> dict[str, Any]:
    """Weekly FRED macro-indicators ingest.

    Pulls Sahm Rule, industrial production, initial claims, yield curve,
    HY credit spread → ``platform.macro_indicators``. Idempotent; the
    handler's own 7-day skip-guard short-circuits intra-week reruns.
    Added 2026-05-14 as the last data source from MASTER_PLAN §6.1.
    """
    from tpcore.ingestion.handlers import handle_macro_indicators

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_macro_indicators(pool, {})
    except Exception as exc:
        log.error("ops.stage.macro_indicators.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_sec_filings(pool: asyncpg.Pool, *, backfill: bool = False) -> dict[str, Any]:
    """Weekly SEC EDGAR Form 4 + 8-K ingest.

    Reference implementation of the standard data-adapter pipeline
    (docs/superpowers/pipelines/data_adapter_pipeline.md). CSV-first:
    download → validate-at-CSV → load → compress. Idempotent — skip
    guard short-circuits in ~10ms when the tables were touched in
    the last 6 days.

    Universe: T1+T2 stocks (ticker_classifications.asset_class='stock').
    ETFs/funds/SPACs filtered out — they don't file Form 4 or 8-K.

    When ``backfill=True`` (operator invokes via
    ``python scripts/ops.py --stage sec_filings --backfill``):
    pulls the full Vector-overlap history from 2018-01-01, ignores
    the 6-day skip-guard, and drops the per-run ticker cap. This is
    the one-time historical bootstrap — multi-hour wall time at
    SEC's 10 req/sec courtesy budget.
    """
    from datetime import date as _date

    from tpcore.ingestion.handlers import handle_sec_filings

    log = structlog.get_logger("scripts.ops")
    config: dict[str, Any] = {}
    if backfill:
        today = datetime.now(UTC).date()
        lookback_days = (today - _date(2018, 1, 1)).days
        config = {
            "lookback_days": lookback_days,
            "max_tickers": None,
            "skip_guard_days": 0,  # bypass the 6-day skip-guard for the one-shot.
        }
        log.info(
            "ops.stage.sec_filings.backfill_start",
            lookback_days=lookback_days,
            start_date="2018-01-01",
            end_date=today.isoformat(),
        )
    try:
        rows = await handle_sec_filings(pool, config)
    except Exception as exc:
        log.error("ops.stage.sec_filings.failed", error=str(exc), backfill=backfill)
        raise

    # Self-verification snapshot — read back from the DB so the operator
    # sees the post-run state without a separate query. Cheap two-count
    # query; runs after thousands-of-hours backfills + 1-minute cron
    # runs alike.
    async with pool.acquire() as conn:
        snapshot = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM platform.sec_insider_transactions) AS insider_rows,
                (SELECT COUNT(*) FROM platform.sec_material_events) AS material_rows,
                (SELECT COUNT(DISTINCT ticker) FROM platform.sec_insider_transactions) AS insider_tickers,
                (SELECT COUNT(DISTINCT ticker) FROM platform.sec_material_events) AS material_tickers,
                LEAST(
                    (SELECT MIN(filing_date) FROM platform.sec_insider_transactions),
                    (SELECT MIN(filing_date) FROM platform.sec_material_events)
                ) AS earliest_filing,
                GREATEST(
                    (SELECT MAX(filing_date) FROM platform.sec_insider_transactions),
                    (SELECT MAX(filing_date) FROM platform.sec_material_events)
                ) AS latest_filing
            """
        )
    out = {
        "backfill": backfill,
        "rows_loaded": int(rows or 0),
        "insider_rows_total": int(snapshot["insider_rows"] or 0),
        "material_rows_total": int(snapshot["material_rows"] or 0),
        "tickers_covered_insider": int(snapshot["insider_tickers"] or 0),
        "tickers_covered_material": int(snapshot["material_tickers"] or 0),
        "earliest_filing": snapshot["earliest_filing"].isoformat() if snapshot["earliest_filing"] else None,
        "latest_filing": snapshot["latest_filing"].isoformat() if snapshot["latest_filing"] else None,
    }
    log.info("ops.stage.sec_filings.done", **out)
    return out


async def _stage_data_validation(pool: asyncpg.Pool) -> dict[str, Any]:
    from tpcore.quality.validation.suite import run_suite

    log = structlog.get_logger("scripts.ops")
    result = await run_suite(pool)
    if result.passed:
        log.info(
            "ops.validation_complete",
            checks=len(result.checks),
            passed_checks=sum(1 for c in result.checks if c.passed),
        )
        return {"passed": True, "checks": len(result.checks)}
    # The suite already wrote per-check rows; raise so the stage records
    # FAILED. The pipeline still moves on to universe simulation.
    failed_names = [c.name for c in result.checks if not c.passed]
    raise RuntimeError(f"validation suite failed: {failed_names}")


async def _stage_coverage_fill(pool: asyncpg.Pool) -> dict[str, Any]:
    """Self-healing — backfill any tier ≤ 2 ticker missing a bar in the last
    7 days via Alpaca SIP feed. Runs after ``corporate_actions`` (so split
    adjustments are applied) and before ``universe_prescreener`` (so the
    prescreener reads complete data). Idempotent and bounded: only the
    last 14 days per gap-ticker.

    Why this exists: the daily ``daily_bars`` stage hits every active
    ticker, but Alpaca's IEX feed (historic default) is missing some
    tier-1 names that the SIP feed has. Without this stage, those gaps
    persist forever and require manual intervention.
    """
    import httpx

    from tpcore.data.ingest_alpaca_bars import fetch_daily_bars_multi

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            LEFT JOIN (
                SELECT DISTINCT ticker FROM platform.prices_daily
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
            ) p ON p.ticker = lt.ticker
            WHERE lt.tier <= 2 AND p.ticker IS NULL
            ORDER BY lt.ticker
            """
        )
    gap_tickers = [r["ticker"] for r in rows]
    if not gap_tickers:
        return {"gap_tickers": 0, "rows_upserted": 0}

    start_d = date.today() - timedelta(days=14)
    end_d = date.today() - timedelta(days=1)

    async with httpx.AsyncClient(
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET", ""),
        },
        timeout=30.0,
    ) as client:
        bars_by_sym = await fetch_daily_bars_multi(
            client, gap_tickers, start_d, end_d, feed="sip",
        )

    upserts: list[tuple] = []
    for sym, bars in bars_by_sym.items():
        for b in bars:
            try:
                o = float(b["o"])
                h = float(b["h"])
                l_ = float(b["l"])
                c = float(b["c"])
                v = int(b["v"])
            except (KeyError, TypeError, ValueError):
                continue
            if c <= 0 or c > 100_000_000 or h < max(o, c, l_) or l_ > min(o, c, h) or v < 0:
                continue
            bar_date = (b.get("t") or "")[:10]
            if not bar_date:
                continue
            upserts.append((sym, date.fromisoformat(bar_date), o, h, l_, c, v))

    if upserts:
        sql = """
            INSERT INTO platform.prices_daily
                (ticker, date, open, high, low, close, volume, adjusted_close, source, delisted)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $6, 'alpaca', false)
            ON CONFLICT (ticker, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume,
                adjusted_close = EXCLUDED.adjusted_close, source = 'alpaca'
        """
        async with pool.acquire() as conn:
            await conn.executemany(sql, upserts)

    return {
        "gap_tickers": len(gap_tickers),
        "rows_upserted": len(upserts),
        "feed": "sip",
    }


async def _stage_universe_prescreener(pool: asyncpg.Pool) -> dict[str, Any]:
    """Populate ``platform.universe_candidates`` for today.

    V1 only writes ``engine='momentum'`` rows — Sigma/Reversion/Vector still
    use hardcoded universes. Runs after the corporate-actions stage so the
    ``last_close`` snapshot is post-split-adjusted; runs before the
    diagnostic ``universe_simulation`` stage.
    """
    from tpcore.universe.prescreener import prescreen_momentum

    counters = await prescreen_momentum(pool, date.today())
    return {"engine": "momentum", **counters}


_CANDIDATE_RE = re.compile(r"^\s*(\w[\w ]*?)\s+candidates?:\s*(\d+)", re.MULTILINE)


async def _stage_forensics(pool: asyncpg.Pool) -> dict[str, Any]:
    """Run the Forensics service against ``platform.aar_events``.

    Detects drawdown periods, loss clusters, and outlier losses across
    every engine's AAR history, inserts new triggers into
    ``platform.forensics_triggers``, and writes Sprint Dossier markdown
    files under ``docs/sprints/`` (fingerprinted, so re-running is a
    no-op). Read-side stage — does not modify any data-update table.

    Lives as the final stage of ``--update`` so a single ``ops.py
    --update`` produces both fresh data AND a refreshed dossier set.
    Also reachable standalone via ``python scripts/ops.py --stage forensics``.
    """
    from tpcore.forensics.service import ForensicsService

    service = ForensicsService(pool=pool)
    counts = await service.run()
    total_new = sum(counts.values())
    return {"new_triggers_total": total_new, "by_kind": counts}


async def _stage_simulate_universe() -> dict[str, Any]:
    """Run scripts/simulate_universe.py as a subprocess and parse counts.

    Subprocess (not in-process import) keeps the simulation isolated from
    the rest of the maintenance run and gives the 120s asyncio.wait_for
    timeout something killable.
    """
    script_path = REPO_ROOT / "scripts" / "simulate_universe.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    print(stdout, end="")

    counts: dict[str, int] = {}
    for match in _CANDIDATE_RE.finditer(stdout):
        engine = match.group(1).strip().lower().replace(" ", "_")
        counts[engine] = int(match.group(2))

    if proc.returncode != 0:
        raise RuntimeError(
            f"simulate_universe exited {proc.returncode}: {stderr.strip()[:200] or 'no stderr'}"
        )
    return {"exit_code": proc.returncode, **{f"{k}_candidates": v for k, v in counts.items()}}


# ────────────────────────────────────────────────────────────────────────
# --update orchestrator
# ────────────────────────────────────────────────────────────────────────


# Canonical stage spec — (name, factory builder, timeout). The factory takes
# ``(pool, daily_bars_config_or_none)`` and returns a zero-arg coroutine
# factory suitable for ``_run_stage``. Single source of truth for both
# ``cmd_update`` (run all stages in order) and ``cmd_run_stage`` (run one).
# To add/remove a stage: update this list AND OPS_UPDATE_STAGES in
# dashboard_components/health.py.
_STAGE_SPECS: tuple[tuple[str, callable, float], ...] = (
    ("daily_bars",          lambda pool, cfg: (lambda: _stage_daily_bars(pool, cfg)),          HEAVY_STAGE_TIMEOUT_SEC),
    ("corporate_actions",   lambda pool, cfg: (lambda: _stage_corporate_actions(pool)),        HEAVY_STAGE_TIMEOUT_SEC),
    # Reconcile open_orders against Alpaca — daily. Closes audit gap G-3:
    # before 2026-05-14 reconciliation only fired on trade_monitor
    # daemon restart, leaving orphan orders to accumulate silently.
    ("reconcile",           lambda pool, cfg: (lambda: _stage_reconcile(pool)),                STAGE_TIMEOUT_SEC),
    ("coverage_fill",       lambda pool, cfg: (lambda: _stage_coverage_fill(pool)),            STAGE_TIMEOUT_SEC),
    # Self-heal cross-table integrity violations — delete expired
    # tradier_options_chains rows + orphan-ticker rows so the operator
    # doesn't see them as red on the dashboard for human triage. Each
    # rule must have a single proven-safe remediation; see the docstring.
    ("cross_ref_cleanup",   lambda pool, cfg: (lambda: _stage_cross_ref_cleanup(pool)),        STAGE_TIMEOUT_SEC),
    ("fundamentals_refresh",lambda pool, cfg: (lambda: _stage_fundamentals_refresh(pool, cfg)),HEAVY_STAGE_TIMEOUT_SEC),
    # Order corrected 2026-05-14 (audit O-1/O-2/O-3): tier_refresh +
    # classify_tickers must run BEFORE catalyst_refresh + sec_filings
    # because the latter two filter by ticker_classifications.asset_class.
    # classify_tickers' per-ticker fallback path reads liquidity_tiers
    # for the T1+T2 set, so tier_refresh runs first.
    # Liquidity tier refresh — quarterly cadence (90d skip guard).
    # Stage 1: Corwin-Schultz bootstrap (60d freshness gate) writes to
    # spread_observations. Stage 2: assign_tiers aggregates into
    # liquidity_tiers. Closes audit gap G-2: the bootstrap used to
    # be manual-only via scripts/run_tier_refresh.sh.
    ("tier_refresh",        lambda pool, cfg: (lambda: _stage_tier_refresh(pool)),             HEAVY_STAGE_TIMEOUT_SEC),
    # Ticker classifications refresh — monthly cadence (30d skip guard +
    # 95% coverage check). Picks up new listings after universe
    # expansion. ETFs/SPACs/funds get flagged so catalyst + earnings
    # pipelines can filter them out.
    ("classify_tickers",    lambda pool, cfg: (lambda: _stage_classify_tickers(pool)),         HEAVY_STAGE_TIMEOUT_SEC),
    # Catalyst refresh — earnings-beat events for vector engine.
    # Heavy timeout (1h) because the FMP loop is ~1 sec per ticker;
    # T1+T2 stock subset is ~66 tickers so a fresh run is ~3 min, but
    # the universe could grow. Stage short-circuits in ~10ms when
    # the table was refreshed within 6 days.
    ("catalyst_refresh",    lambda pool, cfg: (lambda: _stage_catalyst_refresh(pool)),         HEAVY_STAGE_TIMEOUT_SEC),
    # SEC EDGAR Form 4 + 8-K — reference implementation of the
    # standard 5-stage data-adapter pipeline. CSV-first, idempotent,
    # skip-guard tightened from 6 → 3 days 2026-05-14: Form 4 has a
    # 2-business-day filing deadline so 6d staleness was half-stale on
    # average. Heavy timeout: ~200 tickers × ~1.5s/call (rate-limited
    # under SEC's 10 req/sec cap) + Form 4 XML fetches.
    ("sec_filings",         lambda pool, cfg: (lambda: _stage_sec_filings(pool, backfill=bool(cfg.get("_sec_backfill")))), SEC_FILINGS_STAGE_TIMEOUT_SEC),
    # FRED macro indicators — weekly. Five canonical series (sahm_rule,
    # industrial_production, initial_claims, yield_curve, hy_spread)
    # via FREDAdapter, idempotent ON CONFLICT, 7-day skip-guard.
    # Added 2026-05-14 — closes the last "spec-only" gap in §6.1.
    ("macro_indicators",    lambda pool, cfg: (lambda: _stage_macro_indicators(pool)),         STAGE_TIMEOUT_SEC),
    # data_validation runs the 10-check suite against the live tables —
    # at the current 20M-row prices_daily it consistently runs ~120-
    # 130s. Bumping to 5 min gives headroom without masking a true hang.
    ("data_validation",     lambda pool, cfg: (lambda: _stage_data_validation(pool)),          300.0),
    ("universe_prescreener",lambda pool, cfg: (lambda: _stage_universe_prescreener(pool)),     STAGE_TIMEOUT_SEC),
    ("universe_simulation", lambda pool, cfg: _stage_simulate_universe,                        STAGE_TIMEOUT_SEC),
    # Forensics — read-side analysis over platform.aar_events. Runs last
    # so it sees the freshest data + the universe diagnostics. Idempotent
    # via fingerprint dedup in ForensicsService.persist_trigger; safe to
    # re-run. Standalone: ``python scripts/ops.py --stage forensics``.
    ("forensics",           lambda pool, cfg: (lambda: _stage_forensics(pool)),                STAGE_TIMEOUT_SEC),
)
KNOWN_STAGES: tuple[str, ...] = tuple(name for name, _, _ in _STAGE_SPECS)

# Reasons (from INGESTION_FAILED.data->>'reason' or exception_type) that
# we'll auto-retry exactly once after the main --update pipeline finishes.
# Grounded in a 14-day survey of platform.application_log: the failure
# population is dominated by transient network/timeouts. Logical-state
# errors (RuntimeError, validation_failed, no_data) intentionally do not
# auto-retry — re-running won't fix a delisted ticker or a real data gap.
_RETRYABLE_FAILURE_REASONS: frozenset[str] = frozenset({
    "timeout",
    "timed out",  # _run_stage emits "timed out after Ns"
    "ReadError",
    "ConnectError",
    "ConnectionError",
    "ServerDisconnectedError",
    "RemoteProtocolError",
    "429",
    "TooManyRequests",
})

# Stages that depend on today's regular session having closed. Running
# these mid-session produces partial / wrong-state bars and contaminates
# downstream queries — refuse unless the operator passes --force.
_STAGES_REQUIRING_CLOSED_MARKET: frozenset[str] = frozenset({"daily_bars"})


def _market_open_block_reason(now: datetime | None = None) -> str | None:
    """Return a human-readable refusal reason if the NYSE regular session
    is currently in progress, else None.

    Delegates the predicate to ``tpcore.calendar.require_market_closed``
    (the single source of truth across the platform). This function
    contributes only the operator-facing refusal string; bypass logic
    lives in the caller (which decides whether to pass ``--force``).
    """
    from tpcore.calendar import require_market_closed

    if require_market_closed(now=now):
        return None
    return (
        "NYSE regular session is currently open. Running daily_bars now "
        "would pull a partial intraday snapshot and corrupt today's row "
        "in prices_daily. Wait for 16:00 ET / 20:00 UTC and re-run, or "
        "pass --force to bypass this check."
    )


async def cmd_update(
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
    *,
    dry_run: bool,
    force: bool = False,
) -> UpdateSummary:
    started_at = datetime.now(UTC)
    summary = UpdateSummary(run_id=db_log._run_id, started_at=started_at, finished_at=started_at)

    # Pre-flight — refuse to run during the NYSE regular session unless
    # explicitly forced. --update includes daily_bars, which would corrupt
    # today's row in prices_daily if pulled mid-session.
    if not force:
        block = _market_open_block_reason()
        if block:
            log.error("ops.update.refused_market_open", reason=block)
            await db_log.log(
                "INGESTION_FAILED",
                f"refused: {block}",
                severity="ERROR",
                data={"stage": "pre_flight", "reason": "market_open"},
            )
            summary.stages.append(
                StageResult(name="pre_flight", status="FAILED", duration_ms=0, error=block)
            )
            summary.finished_at = datetime.now(UTC)
            return summary

    # Stage 1 — daily bars. Reads its config row up-front so all subsequent
    # stages can share the same min_price/min_volume/lookback values.
    try:
        daily_bars_config = await _load_daily_bars_config(pool)
    except Exception as exc:  # noqa: BLE001
        log.error("ops.config.load_failed", error=str(exc))
        await db_log.log(
            "INGESTION_FAILED",
            f"daily_bars config load failed: {exc}",
            severity="ERROR",
            data={"stage": "config_load", "error": str(exc)},
        )
        summary.stages.append(StageResult(name="config_load", status="FAILED", duration_ms=0, error=str(exc)))
        summary.finished_at = datetime.now(UTC)
        return summary

    for name, factory_builder, timeout in _STAGE_SPECS:
        summary.stages.append(
            await _run_stage(
                name,
                factory_builder(pool, daily_bars_config),
                log=log,
                db_log=db_log,
                dry_run=dry_run,
                timeout=timeout,
            )
        )

    # Self-healing — retry FAILED/TIMEOUT stages once if their error matches
    # the transient class (timeouts, network blips, 429). This addresses the
    # observed pattern in application_log: a single network hiccup leaves
    # one stage red even though every other stage is green and the issue
    # has already resolved. Bounded to one retry per stage so we surface
    # real persistent failures rather than mask them.
    if not dry_run:
        await _self_heal_failed_stages(
            summary, pool, daily_bars_config, log=log, db_log=db_log,
        )

    summary.finished_at = datetime.now(UTC)
    return summary


async def _self_heal_failed_stages(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    daily_bars_config: dict[str, Any],
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """Retry each FAILED/TIMEOUT stage in ``summary`` exactly once, but only
    when the failure looks transient (see ``_RETRYABLE_FAILURE_REASONS``).

    The replacement StageResult is marked with ``retried=True`` so the
    summary table is honest about what was auto-healed vs. what passed
    on the first try. Emits ``ops.stage.retry`` events to
    ``application_log`` for forensic visibility.
    """
    spec_by_name = {n: (n, fb, to) for n, fb, to in _STAGE_SPECS}
    for i, result in enumerate(summary.stages):
        if result.status not in {"FAILED", "TIMEOUT"}:
            continue
        # Retry only if the error string contains a retryable token.
        err = (result.error or "").lower()
        if not any(tok.lower() in err for tok in _RETRYABLE_FAILURE_REASONS):
            log.info("ops.self_heal.skipped_non_retryable", stage=result.name, error=result.error[:80] if result.error else "")
            continue
        if result.name not in spec_by_name:
            continue
        name, factory_builder, timeout = spec_by_name[result.name]
        await db_log.log(
            "INGESTION_RETRY",
            f"self-healing retry for {name}",
            severity="INFO",
            data={"stage": name, "first_error": (result.error or "")[:160]},
        )
        log.info("ops.self_heal.retry_start", stage=name, prior_error=result.error)
        retry_result = await _run_stage(
            name,
            factory_builder(pool, daily_bars_config),
            log=log,
            db_log=db_log,
            dry_run=False,
            timeout=timeout,
        )
        # Annotate so the table reader can see this was a recovery.
        retry_result.detail = {**(retry_result.detail or {}), "retried": True, "first_error": (result.error or "")[:160]}
        summary.stages[i] = retry_result


async def cmd_run_stage(
    stage_name: str,
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
    *,
    dry_run: bool,
    force: bool = False,
    backfill: bool = False,
) -> UpdateSummary:
    """Run a single stage by name. Same logging + event shape as ``cmd_update``
    — different ``run_id``. Used by the dashboard's per-stage Fix buttons.

    Acquires a Postgres advisory lock keyed on the stage name so that a
    locally-launched stage cannot race a future Railway cron (or another
    operator session) running the same stage concurrently. Bails with a
    structured FAILED event if the lock is held — clear signal, not a
    silent merge."""
    started_at = datetime.now(UTC)
    summary = UpdateSummary(run_id=db_log._run_id, started_at=started_at, finished_at=started_at)

    matched = [s for s in _STAGE_SPECS if s[0] == stage_name]
    if not matched:
        msg = f"unknown stage '{stage_name}'; known: {', '.join(KNOWN_STAGES)}"
        await db_log.log("INGESTION_FAILED", msg, severity="ERROR", data={"stage": stage_name})
        summary.stages.append(StageResult(name=stage_name, status="FAILED", duration_ms=0, error=msg))
        summary.finished_at = datetime.now(UTC)
        return summary
    name, factory_builder, timeout = matched[0]

    # Pre-flight: refuse stages that need a closed session, unless --force.
    # Stages like fundamentals_refresh / data_validation / universe_*
    # are intraday-safe and pass through regardless.
    if not force and name in _STAGES_REQUIRING_CLOSED_MARKET:
        block = _market_open_block_reason()
        if block:
            log.error("ops.stage.refused_market_open", stage=name, reason=block)
            await db_log.log(
                "INGESTION_FAILED",
                f"refused {name}: {block}",
                severity="ERROR",
                data={"stage": name, "reason": "market_open"},
            )
            summary.stages.append(
                StageResult(name=name, status="FAILED", duration_ms=0, error=block)
            )
            summary.finished_at = datetime.now(UTC)
            return summary

    # Advisory lock — hash the stage name into an int, try to acquire. The
    # lock is auto-released on connection close.
    lock_key = abs(hash(name)) % (2**31)
    async with pool.acquire() as lock_conn:
        got = await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
        if not got:
            msg = f"stage '{name}' is already running elsewhere (advisory lock held)"
            await db_log.log("INGESTION_FAILED", msg, severity="ERROR", data={"stage": name, "reason": "lock_busy"})
            summary.stages.append(StageResult(name=name, status="FAILED", duration_ms=0, error=msg))
            summary.finished_at = datetime.now(UTC)
            return summary
        try:
            # daily_bars_config is needed by daily_bars + fundamentals_refresh.
            # Other stages take an unused config arg, so the load is harmless.
            try:
                daily_bars_config = await _load_daily_bars_config(pool)
            except Exception as exc:  # noqa: BLE001
                log.error("ops.config.load_failed", error=str(exc), stage=name)
                await db_log.log(
                    "INGESTION_FAILED",
                    f"{name}: config load failed: {exc}",
                    severity="ERROR",
                    data={"stage": "config_load", "error": str(exc)},
                )
                summary.stages.append(
                    StageResult(name="config_load", status="FAILED", duration_ms=0, error=str(exc))
                )
                summary.finished_at = datetime.now(UTC)
                return summary

            # Single-shot operator flags piggyback on daily_bars_config —
            # the factory_builder reads them out by namespaced key. Today
            # only --backfill (sec_filings) uses this channel.
            if backfill:
                daily_bars_config = {**daily_bars_config, "_sec_backfill": True}

            summary.stages.append(
                await _run_stage(
                    name,
                    factory_builder(pool, daily_bars_config),
                    log=log,
                    db_log=db_log,
                    dry_run=dry_run,
                    timeout=timeout,
                )
            )
        finally:
            await lock_conn.fetchval("SELECT pg_advisory_unlock($1)", lock_key)

    summary.finished_at = datetime.now(UTC)
    return summary
    summary.finished_at = datetime.now(UTC)
    return summary


# ────────────────────────────────────────────────────────────────────────
# --check (read-only health report)
# ────────────────────────────────────────────────────────────────────────


async def _check_connectivity(pool: asyncpg.Pool) -> dict[str, Any]:
    val = await pool.fetchval("SELECT 1")
    return {"ok": val == 1, "result": val}


async def _check_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    latest = await pool.fetchval("SELECT MAX(date) FROM platform.prices_daily")
    if latest is None:
        return {"ok": False, "latest_bar": None, "reason": "table empty"}
    today = date.today()
    age_days = (today - latest).days
    return {
        "ok": age_days <= DATA_FRESHNESS_MAX_DAYS,
        "latest_bar": latest.isoformat(),
        "age_days": age_days,
        "threshold_days": DATA_FRESHNESS_MAX_DAYS,
    }


async def _check_row_counts(pool: asyncpg.Pool) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    all_ok = True
    for qualified, expected_min in EXPECTED_MIN_ROWS.items():
        try:
            count = await pool.fetchval(f"SELECT COUNT(*) FROM {qualified}")
        except Exception as exc:  # noqa: BLE001 — surface as WARNING, keep going
            tables[qualified] = {"ok": False, "error": str(exc), "expected_min": expected_min}
            all_ok = False
            continue
        ok = count >= expected_min
        tables[qualified] = {"ok": ok, "count": count, "expected_min": expected_min}
        if not ok:
            all_ok = False
    return {"ok": all_ok, "tables": tables}


async def _check_corp_actions_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    latest = await pool.fetchval("SELECT MAX(action_date) FROM platform.corporate_actions")
    if latest is None:
        return {"ok": False, "latest_event": None, "reason": "table empty"}
    today = date.today()
    age_days = (today - latest).days
    return {
        "ok": age_days <= CORP_ACTIONS_FRESHNESS_MAX_DAYS,
        "latest_event": latest.isoformat(),
        "age_days": age_days,
        "threshold_days": CORP_ACTIONS_FRESHNESS_MAX_DAYS,
    }


SEC_FILINGS_FRESHNESS_MAX_DAYS = 14

# Fundamentals quarterly filings ship in earnings cycles — even slow
# names get a new 10-Q every ~95 days (one quarter + grace). Beyond
# that the table is stale and Reversion's earnings-quality gate stops
# reflecting reality.
FUNDAMENTALS_FRESHNESS_MAX_DAYS = 95

# Catalyst events are earnings-beat snapshots; the refresh stage is
# quarterly-cadence so 95 days is the same threshold as fundamentals.
CATALYST_FRESHNESS_MAX_DAYS = 95

# Liquidity tiers are recomputed quarterly. Anything beyond 100 days
# old means the operator forgot to run the refresh script.
LIQUIDITY_TIERS_FRESHNESS_MAX_DAYS = 100

# Ticker classifications are near-static — refreshed when the universe
# expands. Coverage warning fires below 90% of the active prices_daily
# universe. Staleness alone doesn't matter (asset class never changes
# for a given ticker), but coverage does.
TICKER_CLASSIFICATIONS_MIN_COVERAGE_PCT = 0.90


async def _check_fundamentals_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — newest fundamentals filing + pb/de coverage.

    Reads `platform.fundamentals_quarterly` directly so the operator
    sees freshness without drilling into the validation-suite output.
    """
    row = await pool.fetchrow(
        """
        SELECT
            (SELECT MAX(filing_date) FROM platform.fundamentals_quarterly) AS latest_filing,
            (SELECT COUNT(DISTINCT ticker) FROM platform.fundamentals_quarterly) AS tickers,
            (SELECT COUNT(*) FROM platform.fundamentals_quarterly) AS rows_total,
            (SELECT COUNT(*) FROM platform.fundamentals_quarterly WHERE pb IS NOT NULL) AS pb_filled,
            (SELECT COUNT(*) FROM platform.fundamentals_quarterly WHERE de IS NOT NULL) AS de_filled
        """
    )
    latest = row["latest_filing"] if row else None
    tickers = int(row["tickers"] or 0) if row else 0
    rows_total = int(row["rows_total"] or 0) if row else 0
    pb_filled = int(row["pb_filled"] or 0) if row else 0
    de_filled = int(row["de_filled"] or 0) if row else 0
    if latest is None:
        return {
            "ok": False,
            "latest_filing": None,
            "tickers": tickers,
            "reason": "table empty",
        }
    today = date.today()
    age_days = (today - latest).days
    pb_pct = (pb_filled / rows_total) if rows_total else 0.0
    de_pct = (de_filled / rows_total) if rows_total else 0.0
    return {
        "ok": age_days <= FUNDAMENTALS_FRESHNESS_MAX_DAYS,
        "latest_filing": latest.isoformat(),
        "age_days": age_days,
        "threshold_days": FUNDAMENTALS_FRESHNESS_MAX_DAYS,
        "tickers": tickers,
        "rows_total": rows_total,
        "pb_coverage_pct": round(pb_pct, 3),
        "de_coverage_pct": round(de_pct, 3),
    }


async def _check_catalyst_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — newest catalyst_events + T1+T2 stock coverage."""
    row = await pool.fetchrow(
        """
        SELECT
            (SELECT MAX(event_date) FROM platform.catalyst_events) AS latest_event,
            (SELECT COUNT(DISTINCT ticker) FROM platform.catalyst_events) AS tickers,
            (SELECT COUNT(*) FROM platform.catalyst_events) AS rows_total
        """
    )
    latest = row["latest_event"] if row else None
    tickers = int(row["tickers"] or 0) if row else 0
    rows_total = int(row["rows_total"] or 0) if row else 0
    if latest is None:
        return {
            "ok": False,
            "latest_event": None,
            "tickers": tickers,
            "reason": "table empty",
        }
    today = date.today()
    age_days = (today - latest).days
    return {
        "ok": age_days <= CATALYST_FRESHNESS_MAX_DAYS,
        "latest_event": latest.isoformat(),
        "age_days": age_days,
        "threshold_days": CATALYST_FRESHNESS_MAX_DAYS,
        "tickers": tickers,
        "rows_total": rows_total,
    }


async def _check_liquidity_tiers_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — newest tier assignment + tier distribution.

    Yellow/red triggers: any tier row older than 100 days. Tier
    assignment drifts slowly so daily-age is fine; the threshold
    catches operator inaction.
    """
    row = await pool.fetchrow(
        "SELECT MAX(last_updated) AS latest, COUNT(*) AS tickers FROM platform.liquidity_tiers"
    )
    if not row or row["latest"] is None:
        return {"ok": False, "latest_assignment": None, "tickers": 0, "reason": "table empty"}
    latest = row["latest"]
    tickers = int(row["tickers"] or 0)
    age_days = (datetime.now(UTC) - latest).days

    distrib = await pool.fetch(
        "SELECT tier, COUNT(*) AS n FROM platform.liquidity_tiers GROUP BY tier ORDER BY tier"
    )
    tiers = {int(r["tier"]): int(r["n"]) for r in distrib}
    return {
        "ok": age_days <= LIQUIDITY_TIERS_FRESHNESS_MAX_DAYS,
        "latest_assignment": latest.isoformat(),
        "age_days": age_days,
        "threshold_days": LIQUIDITY_TIERS_FRESHNESS_MAX_DAYS,
        "tickers": tickers,
        "tiers": tiers,
    }


async def _check_ticker_classifications(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — classification coverage of the active universe.

    Asset class is near-static, so freshness-by-time isn't the metric —
    coverage is. Fails when > 10% of prices_daily tickers lack a row in
    ticker_classifications (universe expansion since the last classify
    run).
    """
    row = await pool.fetchrow(
        """
        SELECT
            (SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily
             WHERE date >= CURRENT_DATE - INTERVAL '30 days'
               AND delisted = false) AS active_tickers,
            (SELECT COUNT(*) FROM platform.ticker_classifications) AS classified_rows,
            (SELECT COUNT(DISTINCT pd.ticker)
             FROM platform.prices_daily pd
             LEFT JOIN platform.ticker_classifications tc USING (ticker)
             WHERE pd.date >= CURRENT_DATE - INTERVAL '30 days'
               AND pd.delisted = false
               AND tc.ticker IS NULL) AS unclassified,
            (SELECT MAX(last_updated) FROM platform.ticker_classifications) AS latest_update
        """
    )
    active = int(row["active_tickers"] or 0) if row else 0
    classified_rows = int(row["classified_rows"] or 0) if row else 0
    unclassified = int(row["unclassified"] or 0) if row else 0
    latest = row["latest_update"] if row else None
    coverage_pct = ((active - unclassified) / active) if active else 0.0
    ok = coverage_pct >= TICKER_CLASSIFICATIONS_MIN_COVERAGE_PCT
    out: dict[str, Any] = {
        "ok": ok,
        "active_tickers": active,
        "classified_rows": classified_rows,
        "unclassified": unclassified,
        "coverage_pct": round(coverage_pct, 3),
        "threshold_pct": TICKER_CLASSIFICATIONS_MIN_COVERAGE_PCT,
    }
    if latest is not None:
        out["latest_update"] = latest.isoformat()
    return out


async def _check_sec_filings_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — newest SEC filing across both ingest tables.

    Wired into the ``--check`` output so the operator sees a dedicated
    sec_filings row instead of having to dig into the validation suite
    output. Threshold mirrors the validation check's MAX_AGE_DAYS so
    the two never disagree.
    """
    row = await pool.fetchrow(
        """
        SELECT
            GREATEST(
                COALESCE((SELECT MAX(filing_date) FROM platform.sec_insider_transactions), '-infinity'::date),
                COALESCE((SELECT MAX(filing_date) FROM platform.sec_material_events),     '-infinity'::date)
            ) AS newest_filing,
            (SELECT COUNT(*) FROM platform.sec_insider_transactions) AS insider_rows,
            (SELECT COUNT(*) FROM platform.sec_material_events) AS material_rows
        """
    )
    newest = row["newest_filing"] if row else None
    insider_rows = int(row["insider_rows"] or 0) if row else 0
    material_rows = int(row["material_rows"] or 0) if row else 0
    if newest is None or newest.year < 1970:
        return {
            "ok": False,
            "latest_filing": None,
            "insider_rows": insider_rows,
            "material_rows": material_rows,
            "reason": "tables empty",
        }
    today = date.today()
    age_days = (today - newest).days
    return {
        "ok": age_days <= SEC_FILINGS_FRESHNESS_MAX_DAYS,
        "latest_filing": newest.isoformat(),
        "age_days": age_days,
        "threshold_days": SEC_FILINGS_FRESHNESS_MAX_DAYS,
        "insider_rows": insider_rows,
        "material_rows": material_rows,
    }


async def _check_engine_schedulers(pool: asyncpg.Pool) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT engine, MAX(recorded_at) AS latest_startup
        FROM platform.application_log
        WHERE event_type = 'STARTUP'
          AND recorded_at > now() - INTERVAL '14 days'
        GROUP BY engine
        ORDER BY engine
        """
    )
    engines = {r["engine"]: r["latest_startup"].isoformat() if r["latest_startup"] else None for r in rows}
    return {"ok": True, "engines": engines}


async def _check_ingestion_engine(pool: asyncpg.Pool) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT data->>'stage' AS job,
               MAX(recorded_at) AS latest_complete
        FROM platform.application_log
        WHERE event_type = 'INGESTION_COMPLETE'
          AND recorded_at > now() - INTERVAL '14 days'
        GROUP BY data->>'stage'
        ORDER BY job
        """
    )
    jobs = {
        (r["job"] or "<no-stage>"): r["latest_complete"].isoformat() for r in rows if r["latest_complete"]
    }
    return {"ok": True, "jobs": jobs}


async def _check_validation(pool: asyncpg.Pool) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT source, timestamp, confidence, notes
        FROM platform.data_quality_log
        WHERE source LIKE 'validation.%'
          AND timestamp > now() - INTERVAL '14 days'
        ORDER BY timestamp DESC
        LIMIT 6
        """
    )
    if not rows:
        return {"ok": False, "reason": "no validation runs in last 14 days"}
    latest_ts = rows[0]["timestamp"]
    # Group rows recorded within ~5 minutes of the latest — that's one run.
    cohort = [r for r in rows if (latest_ts - r["timestamp"]).total_seconds() <= 600]
    passed = all(
        float(r["confidence"]) >= 1.0 and (r["notes"] in (None, "[]", []) or r["notes"] == []) for r in cohort
    )
    return {
        "ok": passed,
        "latest_run_at": latest_ts.isoformat(),
        "checks_in_run": len(cohort),
        "passed": passed,
    }


async def _check_risk_governor(pool: asyncpg.Pool) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT engine, kill_switch_active, kill_switch_reason,
               daily_pnl, weekly_pnl, engine_equity, open_positions, updated_at
        FROM platform.risk_state
        ORDER BY engine
        """
    )
    engines: dict[str, Any] = {}
    any_killed = False
    for r in rows:
        engines[r["engine"]] = {
            "kill_switch_active": bool(r["kill_switch_active"]),
            "kill_switch_reason": r["kill_switch_reason"],
            "daily_pnl": float(r["daily_pnl"]),
            "weekly_pnl": float(r["weekly_pnl"]),
            "engine_equity": float(r["engine_equity"]),
            "open_positions": int(r["open_positions"]),
            "updated_at": r["updated_at"].isoformat(),
        }
        if r["kill_switch_active"]:
            any_killed = True
    return {"ok": not any_killed, "engines": engines}


async def _check_missed_data_operations(pool: asyncpg.Pool) -> dict[str, Any]:
    """Watchdog — warn if no automated data_operations daemon run in last 30 hours.

    Catches launchd misfires (Mac asleep at trigger + replay window
    expired, dst transition, manual unload). Filters on
    ``data->>'source' = 'data_operations_daemon'`` so manual operator
    invocations (``--check``, ad-hoc ``--update``) don't mask a missed
    daemon fire. The daemon's wrapper (``scripts/run_data_operations.sh``)
    passes ``--source data_operations_daemon`` to ops.py for this tag.

    30-hour ceiling tolerates one missed daily cycle of grace before
    flagging. Closes audit gap G-6.
    """
    latest = await pool.fetchval(
        """
        SELECT MAX(recorded_at)
        FROM platform.application_log
        WHERE engine = 'ops'
          AND event_type = 'STARTUP'
          AND data->>'source' = 'data_operations_daemon'
        """
    )
    threshold_h = 30.0
    if latest is None:
        return {
            "ok": False,
            "latest_run": None,
            "threshold_hours": threshold_h,
            "reason": "no data_operations_daemon STARTUP events on record",
        }
    age_hours = (datetime.now(UTC) - latest).total_seconds() / 3600.0
    return {
        "ok": age_hours <= threshold_h,
        "latest_run": latest.isoformat(),
        "age_hours": round(age_hours, 2),
        "threshold_hours": threshold_h,
    }


async def _check_supabase_backup(pool: asyncpg.Pool) -> dict[str, Any]:
    """Probe Supabase's WAL archiver — confirm the last backup landed.

    Supabase Pro runs continuous WAL archiving + daily backups. The
    ``pg_stat_archiver`` system view exposes ``last_archived_time``
    (timestamp of the last successful WAL archive). 26 hours is the
    ceiling — daily backups run nightly, so > 26 hours means either
    Supabase's managed backup pipeline broke or our role lost the
    system-view grant. Closes audit gap G-5.

    Fails soft: if the view query raises (role grants), report ``ok``
    with a ``reason`` so the probe is informational rather than a
    blocker for the dashboard.
    """
    try:
        row = await pool.fetchrow(
            """
            SELECT last_archived_time, archived_count, failed_count
            FROM pg_stat_archiver
            """
        )
    except Exception as exc:  # noqa: BLE001 — managed-service-side blind spot
        return {
            "ok": True,
            "reason": f"pg_stat_archiver unavailable: {exc}",
            "informational_only": True,
        }
    if row is None or row["last_archived_time"] is None:
        return {
            "ok": False,
            "last_archived_time": None,
            "reason": "pg_stat_archiver returned no archiver row",
        }
    last = row["last_archived_time"]
    age_hours = (datetime.now(UTC) - last).total_seconds() / 3600.0
    threshold_h = 26.0
    return {
        "ok": age_hours <= threshold_h,
        "last_archived_time": last.isoformat(),
        "age_hours": round(age_hours, 2),
        "threshold_hours": threshold_h,
        "archived_count": int(row["archived_count"] or 0),
        "failed_count": int(row["failed_count"] or 0),
    }


DISK_SPACE_MIN_FREE_GB = 5.0


MACRO_FRESHNESS_GREEN_DAYS = 90
MACRO_FRESHNESS_YELLOW_DAYS = 180


async def _check_macro_indicators_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — per-indicator freshness for FRED macro data.

    Returns the most-recent value + observation date for each of the
    five canonical indicators, plus a global ok/yellow/red status:
        * ok=True  — every indicator within 90d
        * ok=False — any indicator older than 90d
    A separate ``warn`` flag marks the 90-180d band so the dashboard
    can render yellow rather than red.
    """
    rows = await pool.fetch(
        """
        SELECT indicator,
               MAX(date) AS latest_date,
               COUNT(*) AS rows_total
        FROM platform.macro_indicators
        GROUP BY indicator
        """
    )
    today = date.today()
    by_ind: dict[str, dict[str, Any]] = {}
    max_age = 0
    for r in rows:
        age = (today - r["latest_date"]).days
        by_ind[r["indicator"]] = {
            "latest_date": r["latest_date"].isoformat(),
            "age_days": age,
            "rows_total": int(r["rows_total"] or 0),
        }
        if age > max_age:
            max_age = age
    if not by_ind:
        return {"ok": False, "reason": "macro_indicators is empty", "indicators": {}}
    ok = max_age <= MACRO_FRESHNESS_GREEN_DAYS
    warn = MACRO_FRESHNESS_GREEN_DAYS < max_age <= MACRO_FRESHNESS_YELLOW_DAYS
    return {
        "ok": ok,
        "warn": warn,
        "max_age_days": max_age,
        "threshold_green_days": MACRO_FRESHNESS_GREEN_DAYS,
        "threshold_yellow_days": MACRO_FRESHNESS_YELLOW_DAYS,
        "indicators": by_ind,
    }


async def _check_disk_space(pool: asyncpg.Pool) -> dict[str, Any]:
    """Local disk space probe — warns when the repo's filesystem dips
    below ``DISK_SPACE_MIN_FREE_GB``. Catches the operator-forgot-to-
    prune scenario where CSV backfills + log retention silently fill
    the disk. Closes audit gap D6-1.

    Read-only — uses ``shutil.disk_usage`` on the repo root. The pool
    parameter is unused but kept for the ``_CHECK_FNS`` signature
    contract.
    """
    import shutil
    from pathlib import Path

    del pool  # unused; signature contract only
    repo_root = Path(__file__).resolve().parent.parent
    usage = shutil.disk_usage(repo_root)
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    used_pct = 1.0 - (usage.free / usage.total)
    return {
        "ok": free_gb >= DISK_SPACE_MIN_FREE_GB,
        "free_gb": round(free_gb, 2),
        "total_gb": round(total_gb, 2),
        "used_pct": round(used_pct, 3),
        "threshold_gb": DISK_SPACE_MIN_FREE_GB,
    }


TRADE_MONITOR_HEARTBEAT_MAX_MINUTES = 60.0


async def _check_trade_monitor_heartbeat(pool: asyncpg.Pool) -> dict[str, Any]:
    """Trade-monitor liveness probe — warns when the persistent daemon
    hasn't written an ``application_log`` event in the last 60 minutes.

    The trade_monitor logs STARTUP on launch and emits per-fill events
    during regular sessions. A silent WebSocket disconnect would stop
    the per-fill events; this probe surfaces that condition without
    waiting for the next reconcile attempt. Closes audit gap D6-2.

    Threshold rationale: trade_monitor emits at least heartbeat-style
    events through its WebSocket polling loop; even outside market
    hours it logs reconciliation activity. 60 minutes of complete
    silence is anomalous.
    """
    latest = await pool.fetchval(
        """
        SELECT MAX(recorded_at)
        FROM platform.application_log
        WHERE engine = 'trade_monitor'
        """
    )
    if latest is None:
        return {
            "ok": False,
            "latest_event": None,
            "reason": "no trade_monitor events on record",
        }
    age_minutes = (datetime.now(UTC) - latest).total_seconds() / 60.0
    return {
        "ok": age_minutes <= TRADE_MONITOR_HEARTBEAT_MAX_MINUTES,
        "latest_event": latest.isoformat(),
        "age_minutes": round(age_minutes, 2),
        "threshold_minutes": TRADE_MONITOR_HEARTBEAT_MAX_MINUTES,
    }


async def _check_forensics(pool: asyncpg.Pool) -> dict[str, Any]:
    """Surface open Sprint Dossiers from ``platform.forensics_triggers``.

    "Open" = ``resolved_at IS NULL``. The probe reports total count,
    the most recent fire timestamp, and the distinct set of engines
    under review (each trigger's ``payload->>'engine'``). Operator
    workflow: open the linked dossier markdown under ``docs/sprints/``,
    diagnose, then set ``resolved_at = now()`` to close.

    Returns ``ok=True`` regardless of dossier count — open dossiers
    are findings to review, not platform errors. The dashboard renders
    them in the operator-action panel rather than the red-light strip.
    """
    rows = await pool.fetch(
        """
        SELECT trigger_kind,
               payload->>'engine' AS engine_under_review,
               fired_at
        FROM platform.forensics_triggers
        WHERE resolved_at IS NULL
        ORDER BY fired_at DESC
        """
    )
    open_count = len(rows)
    last_fired_at = rows[0]["fired_at"].isoformat() if rows else None
    engines_under_review = sorted({
        r["engine_under_review"] for r in rows if r["engine_under_review"]
    })
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["trigger_kind"]] = by_kind.get(r["trigger_kind"], 0) + 1
    return {
        "ok": True,
        "open_dossiers": open_count,
        "last_fired_at": last_fired_at,
        "engines_under_review": engines_under_review,
        "by_kind": by_kind,
    }


async def _check_recent_errors(pool: asyncpg.Pool) -> dict[str, Any]:
    rows = await pool.fetch(
        """
        SELECT engine, event_type, severity, message, recorded_at
        FROM platform.application_log
        WHERE severity IN ('ERROR', 'CRITICAL')
          AND recorded_at > now() - INTERVAL '24 hours'
        ORDER BY recorded_at DESC
        LIMIT 50
        """
    )
    errors = [
        {
            "engine": r["engine"],
            "event_type": r["event_type"],
            "severity": r["severity"],
            "message": r["message"],
            "recorded_at": r["recorded_at"].isoformat(),
        }
        for r in rows
    ]
    return {"ok": len(errors) == 0, "count": len(errors), "errors": errors}


_CHECK_FNS = [
    ("db_connectivity", _check_connectivity),
    ("data_freshness", _check_freshness),
    ("row_counts", _check_row_counts),
    ("corporate_actions_freshness", _check_corp_actions_freshness),
    ("fundamentals_freshness", _check_fundamentals_freshness),
    ("catalyst_freshness", _check_catalyst_freshness),
    ("sec_filings_freshness", _check_sec_filings_freshness),
    ("macro_indicators_freshness", _check_macro_indicators_freshness),
    ("liquidity_tiers_freshness", _check_liquidity_tiers_freshness),
    ("ticker_classifications", _check_ticker_classifications),
    ("engine_schedulers", _check_engine_schedulers),
    ("ingestion_engine", _check_ingestion_engine),
    ("validation_suite", _check_validation),
    ("risk_governor", _check_risk_governor),
    ("missed_data_operations", _check_missed_data_operations),
    ("supabase_backup", _check_supabase_backup),
    ("disk_space", _check_disk_space),
    ("trade_monitor_heartbeat", _check_trade_monitor_heartbeat),
    ("forensics", _check_forensics),
    ("recent_errors", _check_recent_errors),
]


async def cmd_check(
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "run_id": str(db_log._run_id),
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": {},
    }
    overall_ok = True
    for name, fn in _CHECK_FNS:
        try:
            result = await fn(pool)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash --check
            log.warning("ops.check.failed", check=name, error=str(exc))
            result = {"ok": False, "error": str(exc)}
        report["checks"][name] = result
        if not result.get("ok", False):
            overall_ok = False
    report["ok"] = overall_ok
    await db_log.log(
        "HEALTH_CHECK",
        f"health check {'OK' if overall_ok else 'DEGRADED'}",
        severity="INFO" if overall_ok else "WARNING",
        data=report,
    )
    return report


def _format_check_pretty(report: dict[str, Any]) -> str:
    lines: list[str] = []
    overall = "OK" if report.get("ok") else "DEGRADED"
    lines.append(f"Health check {overall} — run_id={report.get('run_id')} ts={report.get('timestamp')}")
    lines.append("=" * 72)
    for name, result in report.get("checks", {}).items():
        mark = "[OK]" if result.get("ok") else "[!! ]"
        head = f"{mark} {name}"
        lines.append(head)
        for k, v in result.items():
            if k == "ok":
                continue
            if isinstance(v, dict) and v:
                lines.append(f"    {k}:")
                for kk, vv in v.items():
                    if isinstance(vv, dict):
                        inner = ", ".join(f"{x}={y}" for x, y in vv.items())
                        lines.append(f"      {kk}: {inner}")
                    else:
                        lines.append(f"      {kk}: {vv}")
            elif isinstance(v, list):
                lines.append(f"    {k}: {len(v)} item(s)")
                for item in v[:5]:
                    lines.append(f"      - {item}")
                if len(v) > 5:
                    lines.append(f"      … {len(v) - 5} more")
            else:
                lines.append(f"    {k}: {v}")
        lines.append("")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Consolidated operator commands (added 2026-05-14)
# ────────────────────────────────────────────────────────────────────────


_AUDIT_CHECKS: tuple[tuple[str, str, str], ...] = (
    # (table, check_name, sql) — every cross-reference check the
    # dashboard's "Cross-table integrity" row runs lives here.
    ("catalyst_events", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.catalyst_events ce
        LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p
        ON p.ticker = ce.ticker WHERE p.ticker IS NULL"""),
    ("corporate_actions", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.corporate_actions ca
        LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p
        ON p.ticker = ca.ticker WHERE p.ticker IS NULL"""),
    ("fundamentals_quarterly", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.fundamentals_quarterly fq
        LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p
        ON p.ticker = fq.ticker WHERE p.ticker IS NULL"""),
    ("liquidity_tiers", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.liquidity_tiers lt
        LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p
        ON p.ticker = lt.ticker WHERE p.ticker IS NULL"""),
    ("universe_candidates", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.universe_candidates uc
        LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p
        ON p.ticker = uc.ticker WHERE p.ticker IS NULL"""),
    ("tradier_options_chains", "expired",
        "SELECT COUNT(*) FROM platform.tradier_options_chains WHERE expiration_date < CURRENT_DATE"),
    ("tradier_options_chains", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.tradier_options_chains tc
        LEFT JOIN (SELECT DISTINCT ticker FROM platform.prices_daily) p
        ON p.ticker = tc.ticker WHERE p.ticker IS NULL"""),
    ("liquidity_tiers", "stale_30d",
        "SELECT COUNT(*) FROM platform.liquidity_tiers WHERE last_updated < now() - INTERVAL '30 days'"),
)


async def cmd_audit(pool: asyncpg.Pool) -> dict[str, Any]:
    """Cross-table integrity audit. Same checks the dashboard's
    'Cross-table integrity' row runs. Returns ``{passed, findings}``."""
    findings: list[dict] = []
    async with pool.acquire() as conn:
        for table, check, sql in _AUDIT_CHECKS:
            n = int(await conn.fetchval(sql) or 0)
            findings.append({"table": table, "check": check, "count": n})
    return {
        "passed": all(f["count"] == 0 for f in findings),
        "findings": findings,
    }


async def cmd_reconcile(
    pool: asyncpg.Pool, log: structlog.stdlib.BoundLogger, db_log,
) -> int:
    """Reconcile ``platform.open_orders`` against Alpaca's authoritative
    state. Same code path TradeMonitor uses on startup."""
    import uuid as _uuid

    from tpcore.aar.writer import AARWriter
    from tpcore.alpaca import AlpacaPaperBrokerAdapter
    from tpcore.trade_monitor import TradeMonitor

    broker = AlpacaPaperBrokerAdapter()
    aar_writer = AARWriter(pool)
    monitor = TradeMonitor(
        pool=pool, broker=broker, aar_writer=aar_writer, run_id=_uuid.uuid4(),
    )
    return await monitor.reconcile_pending_on_startup()


async def cmd_allocate(
    pool: asyncpg.Pool, log: structlog.stdlib.BoundLogger,
    *, enforce_freeze: bool = False,
) -> list[Any]:
    """AllocatorService.run_once. Idempotent on (engine, allocation_date)."""
    from decimal import Decimal as _Decimal

    from tpcore.allocator import AllocatorService

    svc = AllocatorService(pool, platform_capital=_Decimal("40000"), enforce_freeze=enforce_freeze)
    return await svc.run_once()


async def cmd_status(pool: asyncpg.Pool) -> str:
    """Terse one-screen platform-state summary for cron output. Reads
    the same signals the dashboard surfaces; pure text output."""
    from tpcore.quality.validation.checks.row_integrity import _INTEGRITY_PREDICATE as _PRED

    async with pool.acquire() as conn:
        latest_bar = await conn.fetchval(
            "SELECT MAX(date) FROM platform.prices_daily WHERE date > CURRENT_DATE - INTERVAL '10 days'"
        )
        viol = int(await conn.fetchval(f"SELECT COUNT(*) FROM platform.prices_daily WHERE {_PRED}") or 0)
        n_t12 = int(await conn.fetchval("SELECT COUNT(*) FROM platform.liquidity_tiers WHERE tier <= 2") or 0)
        n_universe = int(await conn.fetchval(
            "SELECT COUNT(*) FROM platform.universe_candidates WHERE engine='momentum' AND as_of_date=CURRENT_DATE"
        ) or 0)
        n_pending = int(await conn.fetchval(
            "SELECT COUNT(*) FROM platform.open_orders WHERE status NOT IN ('filled','canceled','cancelled','rejected','expired')"
        ) or 0)
        # Latest allocator decision
        alloc_rows = await conn.fetch(
            "SELECT engine, allocated_capital, freeze_state FROM platform.allocations "
            "WHERE allocation_date = (SELECT MAX(allocation_date) FROM platform.allocations) "
            "ORDER BY engine"
        )
        # Latest validation
        val_rows = await conn.fetch(
            """
            WITH latest AS (
                SELECT source, MAX(timestamp) AS t FROM platform.data_quality_log
                WHERE source LIKE 'validation.%' GROUP BY source
            )
            SELECT q.source, q.stale, q.confidence
            FROM platform.data_quality_log q JOIN latest l
              ON l.source = q.source AND l.t = q.timestamp
            """
        )
    lines: list[str] = []
    lines.append("─" * 56)
    lines.append(f"PLATFORM STATUS — {datetime.now(UTC):%Y-%m-%d %H:%M UTC}")
    lines.append("─" * 56)
    lines.append(f"  prices_daily latest:    {latest_bar}")
    lines.append(f"  integrity violations:   {viol}")
    lines.append(f"  tier ≤ 2 universe:      {n_t12:,}")
    lines.append(f"  universe_candidates(today): {n_universe:,}")
    lines.append(f"  pending open_orders:    {n_pending}")
    lines.append("")
    lines.append("  Validation suite (latest run):")
    for r in val_rows:
        tag = "🟢" if (not r["stale"] and (r["confidence"] or 1) >= 1.0) else "🔴"
        lines.append(f"    {tag} {r['source']:35s} stale={r['stale']} conf={r['confidence']}")
    if alloc_rows:
        lines.append("")
        lines.append("  Allocator (latest):")
        for r in alloc_rows:
            lines.append(f"    {r['engine']:10s} ${r['allocated_capital']:>10}  state={r['freeze_state']}")
    lines.append("─" * 56)
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Argparse + entry point
# ────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ops.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/ops.py --update              # run all 6 maintenance stages\n"
            "  python scripts/ops.py --update --dry-run    # log what would run, no work\n"
            "  python scripts/ops.py --check               # JSON health report to stdout\n"
            "  python scripts/ops.py --check --pretty      # formatted health report\n"
            "  python scripts/ops.py --full                # update, then check\n"
        ),
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--update", action="store_true", help="run maintenance stages")
    mode.add_argument("--check", action="store_true", help="read-only health report")
    mode.add_argument("--full", action="store_true", help="--update then --check")
    mode.add_argument(
        "--stage",
        choices=KNOWN_STAGES,
        help="run a single --update stage by name (used by the dashboard's per-stage Fix buttons)",
    )
    # Consolidated platform-level operator commands (added 2026-05-14).
    # All route through this CLI for unified logging + retry + audit.
    mode.add_argument("--audit", action="store_true",
                      help="cross-table integrity audit (read-only)")
    mode.add_argument("--reconcile", action="store_true",
                      help="reconcile open_orders against Alpaca (heals YUMC-class orphans)")
    mode.add_argument("--allocate", action="store_true",
                      help="run AllocatorService — weekly capital rebalance across engines")
    mode.add_argument("--status", action="store_true",
                      help="terse one-screen summary of platform state for cron output")
    p.add_argument("--dry-run", action="store_true", help="log without writing data")
    p.add_argument("--pretty", action="store_true", help="pretty-print --check output")
    p.add_argument(
        "--force",
        action="store_true",
        help="bypass the market-closed pre-flight check (use with care)",
    )
    p.add_argument(
        "--enforce-freeze",
        action="store_true",
        help="--allocate only: write risk_state.kill_switch_active on hard freeze (live mode)",
    )
    p.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "--stage sec_filings only: pull the full history from "
            "2018-01-01 (one-time bootstrap; multi-hour wall time at "
            "SEC's 10 req/sec courtesy budget; bypasses skip-guard)."
        ),
    )
    p.add_argument(
        "--source",
        default=None,
        help=(
            "Optional source-tag included in the STARTUP event's data "
            "payload (e.g. 'data_operations_daemon'). Lets the "
            "missed_data_operations probe distinguish automated daemon "
            "runs from operator-typed --check or --update calls."
        ),
    )
    return p


async def amain(args: argparse.Namespace) -> int:
    from tpcore.db import build_asyncpg_pool
    from tpcore.logging.db_handler import DBLogHandler

    run_id = uuid.uuid4()
    log = _configure_logging(run_id)

    if args.update or args.full or args.stage:
        _require_env(["DATABASE_URL", "FMP_API_KEY"])
        _require_alpaca_env()
    elif args.reconcile:
        _require_env(["DATABASE_URL"])
        _require_alpaca_env()
    elif args.allocate:
        _require_env(["DATABASE_URL"])
    else:
        # --check, --audit, --status are read-only
        _require_env(["DATABASE_URL"])

    db_url = os.environ["DATABASE_URL"]
    pool = await build_asyncpg_pool(db_url, max_size=4)
    db_log = DBLogHandler(pool, engine=ENGINE_NAME, run_id=run_id)
    started = time.monotonic()
    exit_code = 0

    try:
        mode_str = (
            f"stage:{args.stage}" if args.stage
            else "update" if args.update
            else "full" if args.full
            else "audit" if args.audit
            else "reconcile" if args.reconcile
            else "allocate" if args.allocate
            else "status" if args.status
            else "check"
        )
        startup_data = {
            "argv": sys.argv,
            "dry_run": args.dry_run,
            "mode": mode_str,
        }
        if args.source:
            startup_data["source"] = args.source
        await db_log.log(
            "STARTUP",
            f"ops CLI starting (mode={mode_str} dry_run={args.dry_run})",
            severity="INFO",
            data=startup_data,
        )
        log.info("ops.start", mode=mode_str)

        update_summary: UpdateSummary | None = None
        if args.update or args.full:
            update_summary = await cmd_update(pool, log, db_log, dry_run=args.dry_run, force=args.force)
            print("\nUPDATE SUMMARY")
            print("=" * 72)
            print(update_summary.to_table())
            print()
            if update_summary.exit_code != 0:
                exit_code = update_summary.exit_code
        elif args.stage:
            # --backfill is only meaningful for sec_filings today; reject
            # silently if used elsewhere so future stages can opt in without
            # surprise.
            if args.backfill and args.stage != "sec_filings":
                log.warning(
                    "ops.cli.backfill_ignored",
                    stage=args.stage,
                    reason="--backfill only applies to sec_filings",
                )
            update_summary = await cmd_run_stage(
                args.stage, pool, log, db_log,
                dry_run=args.dry_run, force=args.force,
                backfill=args.backfill,
            )
            print(f"\nSTAGE SUMMARY ({args.stage})")
            print("=" * 72)
            print(update_summary.to_table())
            print()
            if update_summary.exit_code != 0:
                exit_code = update_summary.exit_code

        if args.check or args.full:
            report = await cmd_check(pool, log, db_log)
            if args.pretty:
                print(_format_check_pretty(report))
            else:
                print(json.dumps(report, indent=2, default=str))
            if not report.get("ok"):
                # --check itself doesn't fail the process — degraded state
                # is the operator's signal, not the CLI's exit code. Keep
                # exit code 0 unless --update stages failed.
                pass
        elif args.audit:
            audit = await cmd_audit(pool)
            print(json.dumps(audit, indent=2, default=str))
            exit_code = 0 if audit["passed"] else 1
        elif args.reconcile:
            n = await cmd_reconcile(pool, log, db_log)
            print(f"reconciled {n} pending order(s)")
        elif args.allocate:
            decisions = await cmd_allocate(pool, log, enforce_freeze=args.enforce_freeze)
            for d in decisions:
                vol = f"σ={d.realized_vol:.2f}" if d.realized_vol is not None else "σ=bootstrap"
                print(f"  {d.engine:9s}  weight={d.weight:>7.4f}  capital=${d.allocated_capital:>10}  {vol}  state={d.freeze_state}")
        elif args.status:
            print(await cmd_status(pool))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        await db_log.log(
            "SHUTDOWN",
            f"ops CLI finished (exit_code={exit_code})",
            severity="INFO" if exit_code == 0 else "ERROR",
            data={"duration_ms": elapsed_ms, "exit_code": exit_code},
        )
        log.info("ops.shutdown", duration_ms=elapsed_ms, exit_code=exit_code)
    finally:
        await pool.close()

    return exit_code


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
