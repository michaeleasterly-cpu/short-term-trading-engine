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
import subprocess
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
# Marker written into every canary-injected forensics_triggers row and used
# by the teardown DELETE predicate.  Single constant keeps the 4 sites in sync
# so a divergence can never make teardown silently fail to clean injected rows.
_CANARY_INJECTION_SOURCE = "canary_injection"
DATA_FRESHNESS_MAX_DAYS = 4  # 2 trading days + weekend buffer
CORP_ACTIONS_FRESHNESS_MAX_DAYS = 7

# Row-count expected minimums — pulled from docs/OPERATIONS.md §3.
# A drop below these triggers a WARNING in --check output.
EXPECTED_MIN_ROWS: dict[str, int] = {
    "platform.prices_daily": 300_000,
    "platform.fundamentals_quarterly": 1_700,
    "platform.corporate_actions": 1_200,
    "platform.earnings_events": 600,
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


def _parse_params(raw: list[str] | None) -> dict[str, Any]:
    """Parse repeated ``--param KEY=VALUE`` into a config-override dict.

    Light type coercion so JSONB config keys keep their natural types:
    ``int`` → ``float`` → ``bool`` (``true``/``false``) → ``str``
    fallback. ``--param universe=active`` stays a string; ``--param
    lookback_days=10`` becomes ``int``. This is the single mechanism for
    parameterised backfills / special pulls through the canonical CLI —
    no one-off scripts.
    """
    out: dict[str, Any] = {}
    for item in raw or []:
        if "=" not in item:
            raise ValueError(f"--param expects KEY=VALUE, got {item!r}")
        key, _, val = item.partition("=")
        key = key.strip()
        val = val.strip()
        coerced: Any
        if val.lower() in ("true", "false"):
            coerced = val.lower() == "true"
        else:
            try:
                coerced = int(val)
            except ValueError:
                try:
                    coerced = float(val)
                except ValueError:
                    coerced = val
        out[key] = coerced
    return out


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

    # Bounded targeted gap-repair (--param repair_gaps=true). This is the
    # auto-heal path: instead of force-refreshing the whole ~7,600-ticker
    # universe (which provably exceeds the 3600s stage timeout — proven
    # 2026-05-15, two 60-min timeouts), re-pull ONLY the tickers the
    # prices_daily_completeness invariant currently flags, over a window
    # that brackets the oldest missing session. Typically a handful of
    # tickers → 1 multi-symbol chunk → seconds. Detector and healer share
    # `_evaluate`, so the heal can never target a different set than the
    # check reports. A structural sentinel (no_sessions / empty universe)
    # returns no targets here → caller escalates (not bars-fixable).
    if bool(config.get("repair_gaps", False)):
        from tpcore.quality.validation.checks.prices_daily_completeness import (
            compute_gap_repair_targets,
        )
        tickers, lookback_days = await compute_gap_repair_targets(pool)
        if not tickers:
            return {
                "rows_upserted": 0,
                "mode": "repair_gaps",
                "skipped": "no_gaps_or_not_bars_fixable",
            }
        repaired = await handle_daily_bars(pool, {
            "universe": tickers,
            "lookback_days": lookback_days,
            "end_offset_days": int(config.get("end_offset_days", 1)),
        })
        return {
            "rows_upserted": repaired or 0,
            "mode": "repair_gaps",
            "tickers_repaired": len(tickers),
            "lookback_days": lookback_days,
            "tickers": ",".join(tickers[:30]) + ("…" if len(tickers) > 30 else ""),
        }

    # Bounded targeted COVERAGE-collapse repair (--param
    # repair_coverage=true). This is the self-heal path for
    # prices_daily_freshness's coverage_collapse: repair_gaps is blind
    # to it (it derives targets from the COMPLETENESS invariant, empty
    # in that failure mode) and a whole-universe force_refresh times
    # out at 3600s (proven 2026-05-17: reached only 6,910/7,650 in
    # 60min before the cap). Compute exactly the tickers that had a bar
    # on the most recent prior session but are missing the target
    # session, and re-pull ONLY those via the explicit-universe path —
    # 747 names = 8 chunks ≈ 6min, never the full-universe timeout.
    # Detector (freshness check) and healer agree by construction: both
    # key off "present on prior session, absent on target".
    if bool(config.get("repair_coverage", False)):
        from tpcore.calendar import previous_close
        from tpcore.quality.validation.checks.prices_daily_freshness import (
            CRITICAL_MAX_AGE_DAYS,
            CRITICAL_TICKERS,
            UNIVERSE_MAX_AGE_DAYS,
        )

        tgt = previous_close(datetime.now(UTC)).date()
        # Detector/healer convergence: prices_daily_freshness flags FOUR
        # modes — coverage_collapse, critical_ticker_stale/missing_ticker,
        # universe_stale_excess. The original query covered only
        # coverage_collapse (present prior session, absent target); a
        # multi-session SPY drop or a >14d-stale tail was never in that
        # set → never healed → exhaust + escalate (the residual
        # fake-healable found 2026-05-17). Target set is now the UNION of
        # all three populations, using the check's OWN constants
        # (single source of truth — never duplicated).
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH prior AS (
                    SELECT MAX(date) AS d FROM platform.prices_daily
                    WHERE date < $1
                ),
                latest AS (
                    SELECT ticker, MAX(date) AS last_date
                    FROM platform.prices_daily
                    WHERE delisted = false
                    GROUP BY ticker
                )
                SELECT DISTINCT t FROM (
                    -- coverage_collapse: present prior session, absent target
                    SELECT p.ticker AS t
                    FROM platform.prices_daily p, prior
                    WHERE p.date = prior.d AND p.delisted = false
                      AND NOT EXISTS (
                          SELECT 1 FROM platform.prices_daily q
                          WHERE q.ticker = p.ticker AND q.date = $1
                      )
                    UNION
                    -- critical_ticker_stale / missing_ticker
                    SELECT c.ticker AS t
                    FROM unnest($2::text[]) AS c(ticker)
                    LEFT JOIN latest l ON l.ticker = c.ticker
                    WHERE l.last_date IS NULL OR l.last_date < $1 - $3::int
                    UNION
                    -- universe_stale_excess
                    SELECT l.ticker AS t
                    FROM latest l
                    WHERE l.last_date < $1 - $4::int
                ) u
                ORDER BY t
                """,
                tgt,
                list(CRITICAL_TICKERS),
                CRITICAL_MAX_AGE_DAYS,
                UNIVERSE_MAX_AGE_DAYS,
            )
        missing = [r["t"] for r in rows]
        if not missing:
            return {
                "rows_upserted": 0,
                "mode": "repair_coverage",
                "target_session": tgt.isoformat(),
                "skipped": "no_coverage_gap",
            }
        repaired = await handle_daily_bars(pool, {
            "universe": missing,
            # Wide enough to backfill a >UNIVERSE_MAX_AGE_DAYS stale tail,
            # not just a single-session hole.
            "lookback_days": int(
                config.get("lookback_days", UNIVERSE_MAX_AGE_DAYS + 6)
            ),
            "end_offset_days": int(config.get("end_offset_days", 1)),
        })
        return {
            "rows_upserted": repaired or 0,
            "mode": "repair_coverage",
            "target_session": tgt.isoformat(),
            "tickers_repaired": len(missing),
            "tickers": ",".join(missing[:30]) + ("…" if len(missing) > 30 else ""),
        }

    target_session = previous_close(datetime.now(UTC)).date()
    # ``force_refresh`` (via --param force_refresh=true) bypasses the
    # skip-fast — the canonical way to run a coverage backfill: the
    # most-recent session may already be above threshold while older
    # sessions in the lookback window have holes. Same --param channel
    # as every other config key; no special-case flag.
    force_refresh = bool(config.get("force_refresh", False))
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
    if not force_refresh and already_ingested and already_ingested >= threshold:
        return {
            "rows_upserted": 0,
            "universe": config.get("universe", "active"),
            "skipped": "already_ingested",
            "target_session": target_session.isoformat(),
            "tickers_present": already_ingested,
        }

    rows = await handle_daily_bars(pool, config)

    # Producer self-validation (2026-05-17 incident hardening): a pull
    # can exit "OK" while having written only a fraction of the
    # universe — the all_active coarse-filter cap (468/8300) and the
    # feed=sip 403-every-chunk bug both did exactly this, and the
    # collapse was only caught a cycle later by the downstream
    # validation suite (or, worse, eyeballed as "good"). Refuse to
    # report OK on a collapsed target session: re-using the SAME
    # threshold the freshness check uses (single source of truth),
    # raise so the stage fails loudly → INGESTION_FAILED → the daemon
    # does NOT emit DATA_OPERATIONS_COMPLETE and self-heal/escalation
    # fires at ingest time, not a cycle later. "100% data or don't
    # trade" enforced at the producer, not just the consumer.
    #
    # #185 Phase 4 decision (kept, not retired): this coarse guard is a
    # cheap fail-fast PRE-FILTER, NOT the source of truth. It already
    # reuses the canonical check's threshold (COVERAGE_COLLAPSE_PCT
    # below) so it cannot diverge, and the authoritative per-feed
    # `prices_daily_*` checks now also run on-completion via the
    # Phase 2/3 tripwire (_per_feed_tripwire). Defense-in-depth — do
    # NOT grow bespoke logic here; extend the canonical check instead.
    from tpcore.quality.validation.checks.prices_daily_freshness import (
        COVERAGE_COLLAPSE_PCT,
    )

    async with pool.acquire() as conn:
        cov = await conn.fetch(
            """
            SELECT date, COUNT(DISTINCT ticker) AS n
            FROM platform.prices_daily
            WHERE date >= $1::date - INTERVAL '40 days' AND date <= $1
            GROUP BY date ORDER BY date DESC LIMIT 21
            """,
            target_session,
        )
    by_date = {r["date"]: int(r["n"]) for r in cov}
    latest_n = by_date.get(target_session, 0)
    trailing = [n for d, n in by_date.items() if d != target_session]
    if trailing:
        avg_trailing = sum(trailing) / len(trailing)
        floor = avg_trailing * (1 - COVERAGE_COLLAPSE_PCT)
        if avg_trailing > 0 and latest_n < floor:
            raise RuntimeError(
                f"daily_bars coverage collapse: {target_session} has "
                f"{latest_n} tickers = {latest_n / avg_trailing:.0%} of the "
                f"trailing-{len(trailing)}-session avg ({avg_trailing:,.0f}); "
                f"floor is {floor:,.0f} ({1 - COVERAGE_COLLAPSE_PCT:.0%}). "
                f"Refusing to report OK — partial/failed ingest "
                f"(rows_upserted={rows or 0}, universe="
                f"{config.get('universe', 'active')!r}). "
                f"Repair: ops.py --stage daily_bars --param force_refresh=true "
                f"--param universe=active --param end_offset_days=1"
            )

    return {
        "rows_upserted": rows or 0,
        "universe": config.get("universe", "active"),
        "target_session": target_session.isoformat(),
        "coverage_tickers": latest_n,
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


async def _stage_compute_fundamental_ratios(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compute point-in-time P/B and D/E on ``platform.fundamentals_quarterly``.

    Joins each filing to the most recent ``platform.prices_daily`` close
    on-or-before ``filing_date`` and writes ``pb``/``de`` via a single
    set-based UPDATE — scales to 100k+ rows without holding a pool
    connection long enough for the Supabase pooler to drop it.

    Definitions::

        book_value_per_share = (total_assets − total_liabilities) / shares_outstanding
        pb = close / book_value_per_share
        de = total_liabilities / (total_assets − total_liabilities)

    Idempotent: rows where both ratios are already populated are skipped
    on subsequent runs. ``config.force == "true"`` overwrites existing
    pb/de (mirrors the ``--force`` flag on the prior script).

    The ``total_assets > 0 AND total_liabilities >= 0`` predicates reject
    degenerate FMP rows (ta=0, tl<0 inverted accounting) where the naive
    ``(ta - tl) > 0`` check would let bogus ratios through (e.g. de=-1.0
    from tl/book = -x/x). 51 rows had this shape post-Phase-1 backfill
    and were nulled by a one-shot cleanup; this filter keeps re-runs
    clean.

    **Validation/freshness note:** this is a derived-column
    *computation*, not an ingestion. The ungameable substrate is
    ``fundamentals_quarterly_completeness`` (PR #172) which gates the
    parent table; pb/de NULL rows are expected on filings that fail the
    WHERE predicates (negative book value, missing shares, no prior
    price). No separate freshness check is appropriate.

    Chained AFTER ``fundamentals_refresh`` in ``_STAGE_SPECS`` so a fresh
    FMP pull's new rows get ratios in the same update cycle — closes the
    "operator forgot to re-run the script" manual step.
    """
    force = str((config or {}).get("force", "")).lower() == "true"
    where = "" if force else "AND (pb IS NULL OR de IS NULL)"
    sql = """
        WITH targets AS (
            SELECT ticker, filing_date, total_assets, total_liabilities, shares_outstanding
            FROM platform.fundamentals_quarterly
            WHERE total_assets IS NOT NULL
              AND total_liabilities IS NOT NULL
              AND shares_outstanding IS NOT NULL
              AND total_assets > 0
              AND total_liabilities >= 0
              AND shares_outstanding > 0
              AND (total_assets - total_liabilities) > 0
              """ + where + """
        ),
        priced AS (
            SELECT DISTINCT ON (t.ticker, t.filing_date)
                t.ticker, t.filing_date, t.total_assets, t.total_liabilities,
                t.shares_outstanding, pd.close
            FROM targets t
            JOIN platform.prices_daily pd
              ON pd.ticker = t.ticker AND pd.date <= t.filing_date
            ORDER BY t.ticker, t.filing_date, pd.date DESC
        )
        UPDATE platform.fundamentals_quarterly fq
        SET pb = round(p.close / ((p.total_assets - p.total_liabilities) / p.shares_outstanding), 6),
            de = round(p.total_liabilities / (p.total_assets - p.total_liabilities), 6)
        FROM priced p
        WHERE fq.ticker = p.ticker
          AND fq.filing_date = p.filing_date
        RETURNING fq.ticker
    """
    async with pool.acquire() as conn:
        updated = await conn.fetch(sql)
        populated = await conn.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE pb IS NOT NULL) AS pb_n, "
            "COUNT(*) FILTER (WHERE de IS NOT NULL) AS de_n, "
            "COUNT(*) AS total FROM platform.fundamentals_quarterly"
        )
    return {
        "rows_updated": len(updated),
        "pb_populated": int((populated or {}).get("pb_n", 0) or 0),
        "de_populated": int((populated or {}).get("de_n", 0) or 0),
        "total_rows": int((populated or {}).get("total", 0) or 0),
        "force": force,
    }


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


async def _stage_risk_close_ledger_prune(pool: asyncpg.Pool) -> dict[str, Any]:
    """Bounded 14-day prune of ``platform.risk_close_ledger`` (#251 B1.4).

    ``risk_close_ledger`` is the idempotent arbiter that guarantees a
    single position close decrements ``risk_state.open_positions`` AT
    MOST once (the never-fail-open root fix). It only needs to retain a
    close's ``(engine, trade_id)`` key long enough to dedupe the two
    close paths (the trade-monitor stream + the scheduler rebalance-sell
    loop) — which fire within the same trading session. A settled
    ``trade_id`` is NEVER re-closed (the position no longer exists), so a
    pruned row cannot cause a re-decrement under normal flow.

    14 days is a generous age-ring: it keeps the table from growing
    unbounded (one row per close, forever) while never expiring a key
    that could still arbitrate a same-cycle duplicate. Wired into the
    existing daily ``--update`` cadence (NO new daemon). Idempotent by
    construction (same query; deletes shrink to zero on the next run).
    """
    async with pool.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM platform.risk_close_ledger "
            "WHERE recorded_at < now() - interval '14 days'"
        )
    try:
        pruned = int(status.split()[-1])
    except (ValueError, IndexError):
        pruned = 0
    return {"pruned_settled_close_keys": pruned}


async def _stage_earnings_refresh(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Weekly refresh of ``platform.earnings_events`` (FMP earnings beats).

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
    ``scripts/backfill_earnings_events.py``, so behavior stays in
    lockstep between cron and operator-on-demand.
    """
    from datetime import date as _date
    from datetime import timedelta as _td
    from types import SimpleNamespace

    # Skip guard. ``skip_guard_days`` (via --param) mirrors the
    # _stage_sec_filings contract: default 6; set 0 to bypass for a
    # forced full-universe refresh (the canonical way to run the
    # coverage backfill — no one-off script).
    config = config or {}
    skip_guard_days = int(config.get("skip_guard_days", 6))
    async with pool.acquire() as conn:
        newest_recorded = await conn.fetchval(
            "SELECT MAX(recorded_at) FROM platform.earnings_events"
        )
    log = structlog.get_logger("scripts.ops")
    if newest_recorded is not None and skip_guard_days > 0:
        age = datetime.now(UTC) - newest_recorded
        if age.days < skip_guard_days:
            log.info(
                "ops.stage.earnings_refresh.skipped_fresh",
                last_refresh_age_days=age.days,
                skip_guard_days=skip_guard_days,
            )
            return {
                "skipped": True,
                "reason": f"refreshed_within_{skip_guard_days}_days",
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
    from scripts.backfill_earnings_events import amain as backfill_amain
    args = SimpleNamespace(
        universe=universe,
        start=_date(2018, 1, 1),
        end=datetime.now(UTC).date() - _td(days=1),
    )
    run_started = datetime.now(UTC)
    exit_code = await backfill_amain(args)
    if exit_code != 0:
        raise RuntimeError(
            f"earnings_refresh: backfill_amain returned {exit_code}"
        )
    async with pool.acquire() as conn:
        post_count = await conn.fetchval("SELECT COUNT(*) FROM platform.earnings_events")
        post_tickers = await conn.fetchval(
            "SELECT COUNT(DISTINCT ticker) FROM platform.earnings_events"
        )
        new_rows = await conn.fetch(
            """
            SELECT ticker, event_date, event_type, magnitude_pct, source, recorded_at
            FROM platform.earnings_events
            WHERE recorded_at >= $1
            ORDER BY ticker, event_date
            """,
            run_started,
        )

    # CSV-first audit archive (incremental — new rows this run only;
    # shrinkage detection is reserved for full-snapshot sources).
    from tpcore.ingestion.csv_archive import write_archive
    archive_rows = [
        {k: str(v) if v is not None else "" for k, v in dict(r).items()}
        for r in new_rows
    ]
    archive = write_archive(
        "fmp_earnings_events", archive_rows,
        fieldnames=["ticker", "event_date", "event_type", "magnitude_pct", "source", "recorded_at"],
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("event_type")),
    )

    return {
        "tickers": len(universe),
        "total_rows": int(post_count or 0),
        "covered_tickers": int(post_tickers or 0),
        "csv_archive": str(archive.path),
    }


async def _stage_tier_refresh(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    # Outer skip guard — gate the whole stage on liquidity_tiers
    # freshness. ``skip_guard_days`` (via --param) overrides the 90-day
    # default; 0 forces a re-run — the canonical self-heal force the
    # selfheal orchestrator uses (mirrors the other stages' contract).
    skip_days = int((cfg or {}).get("skip_guard_days", 90))
    newest = await pool.fetchval(
        "SELECT MAX(last_updated) FROM platform.liquidity_tiers"
    )
    if newest is not None and skip_days > 0:
        age = datetime.now(UTC) - newest
        if age.days < skip_days:
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
    from tpcore.backtest.spread_estimator import SOURCE_TAG, rank_universe_by_liquidity

    # Phase 1 — bootstrap, gated by its own freshness check.
    # Source-tagged to the active estimator (abdi_ranaldo as of
    # 2026-05-15). The legacy 'corwin_schultz' rows in
    # spread_observations are retained for audit / historical view
    # but no longer consulted for freshness or aggregation.
    newest_obs = await pool.fetchval(
        "SELECT MAX(observed_at) FROM platform.spread_observations "
        "WHERE source = $1",
        SOURCE_TAG,
    )
    bootstrap_skipped = False
    bootstrap_rows = 0
    if newest_obs is not None and (datetime.now(UTC) - newest_obs).days < 60:
        bootstrap_skipped = True
        log.info(
            "ops.stage.tier_refresh.bootstrap_skipped",
            spread_obs_age_days=(datetime.now(UTC) - newest_obs).days,
            source=SOURCE_TAG,
        )
    else:
        results = await rank_universe_by_liquidity(
            pool, persist=True, coarse_filter=False,
        )
        bootstrap_rows = len(results)
        log.info(
            "ops.stage.tier_refresh.bootstrap_done",
            spread_observations_written=bootstrap_rows,
            source=SOURCE_TAG,
            prior_obs_age_days=(
                (datetime.now(UTC) - newest_obs).days
                if newest_obs is not None else None
            ),
        )

    # Phase 2 — re-aggregate from spread_observations.
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        raise RuntimeError("tier_refresh: DATABASE_URL not set")
    sources = [SOURCE_TAG]
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


async def _stage_classify_tickers(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
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

    # ``skip_guard_days`` (via --param) overrides the 30-day default;
    # 0 forces a re-run — the canonical self-heal force.
    skip_days = int((cfg or {}).get("skip_guard_days", 30))
    if latest is not None and coverage_pct >= 0.95 and skip_days > 0:
        age = datetime.now(UTC) - latest
        if age.days < skip_days:
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


async def _stage_delist_stale(pool: asyncpg.Pool) -> dict[str, Any]:
    """Auto-promote stale SPAC / fund tickers to ``delisted=true``.

    SPACs and funds frequently stop trading via merger / redemption /
    liquidation without an explicit delisting event reaching our feed.
    They accumulate as "stale" tickers in ``prices_daily`` (last bar
    weeks-to-months old, ``delisted=false``), inflating universe counts
    and triggering false alerts in per-ticker freshness checks. This
    stage promotes any non-stock asset whose last bar is > 30 days old
    to ``delisted=true``, with ``delisting_date`` set to the last bar.

    **Common stocks are intentionally excluded** — a single illiquid
    stock that hasn't traded for a month may simply be temporarily
    halted, awaiting an earnings restatement, etc. Auto-delisting them
    is reversible-but-noisy. The operator handles stale stocks via the
    forensics dashboard.

    Idempotent — re-running has no effect once the stale roster is
    flushed. Safe under the data_operations pipeline.
    """
    log = structlog.get_logger("scripts.ops")
    sql = """
        UPDATE platform.prices_daily pd
        SET delisted = true,
            delisting_date = COALESCE(pd.delisting_date, (
                SELECT MAX(date) FROM platform.prices_daily WHERE ticker = pd.ticker
            ))
        WHERE pd.ticker IN (
            SELECT pd2.ticker FROM platform.prices_daily pd2
            JOIN platform.ticker_classifications tc ON tc.ticker = pd2.ticker
            WHERE pd2.delisted = false AND tc.asset_class IN ('spac', 'fund')
            GROUP BY pd2.ticker
            HAVING MAX(pd2.date) < CURRENT_DATE - INTERVAL '30 days'
        )
    """
    # Count first (so the log reports tickers, not row counts), then apply.
    count_sql = """
        SELECT COUNT(DISTINCT pd.ticker) AS n
        FROM platform.prices_daily pd
        JOIN platform.ticker_classifications tc ON tc.ticker = pd.ticker
        WHERE pd.delisted = false AND tc.asset_class IN ('spac', 'fund')
        GROUP BY pd.ticker
        HAVING MAX(pd.date) < CURRENT_DATE - INTERVAL '30 days'
    """
    async with pool.acquire() as conn:
        candidates = await conn.fetchval(
            "SELECT COUNT(*) FROM (" + count_sql + ") t"
        )
        candidates = int(candidates or 0)
        if candidates == 0:
            log.info("ops.stage.delist_stale.no_candidates")
            return {"candidates": 0, "delisted": 0}
        await conn.execute(sql)
    log.info("ops.stage.delist_stale.done", candidates=candidates)
    return {"candidates": candidates, "delisted": candidates}


async def _stage_macro_indicators(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Weekly FRED macro-indicators ingest.

    Pulls Sahm Rule, industrial production, initial claims, yield curve,
    HY credit spread → ``platform.macro_indicators``. Idempotent; the
    handler's own 7-day skip-guard short-circuits intra-week reruns.
    Added 2026-05-14 as the last data source from MASTER_PLAN §6.1.

    Canonical one-time historical CSV backfill (no one-off script):
    ``--param hist_csv_path=<file> --param hist_indicator=<name>`` loads
    a pre-truncation archive for a single indicator, idempotently.
    """
    from tpcore.ingestion.handlers import handle_macro_indicators

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_macro_indicators(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.macro_indicators.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_greeks_max_pain(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Daily greeks.pro free-tier max-pain snapshot (1 symbol).

    Handler has its own same-day skip-guard (idempotent regardless).
    ``--param symbol=XXX`` overrides the tracked symbol (default SPY);
    ``--param skip_guard=false`` forces a re-pull (used by self-heal).
    """
    from tpcore.ingestion.handlers import handle_greeks_max_pain

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_greeks_max_pain(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.greeks_max_pain.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_finnhub_insider_sentiment(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Finnhub insider-sentiment (MSPR) for the T1/T2 stock universe.

    Monthly data → handler 25-day skip-guard. ``--param symbols=...``
    or ``--param skip_guard_days=0`` (force re-pull; used by self-heal).
    """
    from tpcore.ingestion.handlers import handle_finnhub_insider_sentiment

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_finnhub_insider_sentiment(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.finnhub_insider_sentiment.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_apewisdom_social_sentiment(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """ApeWisdom Reddit social-sentiment (T1/T2 universe, daily).

    API refreshes ~2h; handler 24h skip-guard. ``--param
    skip_guard_hours=0`` forces a re-pull (self-heal).
    """
    from tpcore.ingestion.handlers import handle_apewisdom_social_sentiment

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_apewisdom_social_sentiment(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.apewisdom_social_sentiment.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_fear_greed(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Recompute the Fear & Greed index from existing platform data.

    Daily (after close). ``--param backfill=true`` computes the full
    2001→today history; ``--param start_date=YYYY-MM-DD`` for an
    explicit window. No external provider. NOT a one-off script — this
    canonical stage IS the backfill path.
    """
    from tpcore.ingestion.handlers import handle_fear_greed

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_fear_greed(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.fear_greed.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_finra_short_interest(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """FINRA consolidated short interest (T1/T2), bi-monthly, PIT-safe.
    ``--param skip_guard_days=0`` forces a re-pull (self-heal)."""
    from tpcore.ingestion.handlers import handle_finra_short_interest

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_finra_short_interest(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.finra_short_interest.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_iborrowdesk_borrow_rates(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """IBorrowDesk daily borrow rates (T1/T2). Scrape-fragile — the
    handler skips (never crashes) on repeated blocks. ``--param
    skip_guard_hours=0`` forces a re-pull (self-heal)."""
    from tpcore.ingestion.handlers import handle_iborrowdesk_borrow_rates

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_iborrowdesk_borrow_rates(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.iborrowdesk_borrow_rates.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_aaii_sentiment(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """AAII weekly Sentiment Survey (full-history workbook, idempotent
    ON CONFLICT DO UPDATE). ``--param skip_guard_days=0`` forces a
    re-pull (self-heal)."""
    from tpcore.ingestion.handlers import handle_aaii_sentiment

    log = structlog.get_logger("scripts.ops")
    try:
        rows = await handle_aaii_sentiment(pool, config or {})
    except Exception as exc:
        log.error("ops.stage.aaii_sentiment.failed", error=str(exc))
        raise
    return {"rows_loaded": int(rows or 0)}


async def _stage_sec_filings(
    pool: asyncpg.Pool, *, backfill: bool = False,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    cfg = cfg or {}
    config: dict[str, Any] = {}

    # One-time 8-K historical backfill (material events). 8-K item
    # codes are not in any SEC bulk dataset (verified) — they come
    # from the per-issuer submissions index, one request per issuer
    # (no per-document XML), so a full-history pull is fast. Driven
    # canonically by ``--param eight_k_backfill=true``. full_history
    # follows the older submissions shards so 2018→now is complete
    # for prolific filers, not just whatever is still in `recent`.
    if cfg.get("eight_k_backfill"):
        today = datetime.now(UTC).date()
        config = {
            "lookback_days": (today - _date(2018, 1, 1)).days,
            "max_tickers": None,
            "skip_guard_days": 0,
            "eight_k_only": True,
            "full_history": True,
            "ticker_chunk_size": 200,
        }
        log.info(
            "ops.stage.sec_filings.eight_k_backfill_start",
            start_date="2018-01-01", end_date=today.isoformat(),
        )
        try:
            rows = await handle_sec_filings(pool, config)
        except Exception as exc:
            log.error("ops.stage.sec_filings.failed",
                      error=str(exc), eight_k_backfill=True)
            raise
        async with pool.acquire() as conn:
            snap = await conn.fetchrow(
                "SELECT COUNT(*) r, COUNT(DISTINCT ticker) t, "
                "MIN(filing_date) mn, MAX(filing_date) mx "
                "FROM platform.sec_material_events"
            )
        out = {
            "eight_k_backfill": True, "rows_loaded": int(rows or 0),
            "material_rows_total": int(snap["r"] or 0),
            "tickers_covered_material": int(snap["t"] or 0),
            "earliest": snap["mn"].isoformat() if snap["mn"] else None,
            "latest": snap["mx"].isoformat() if snap["mx"] else None,
        }
        log.info("ops.stage.sec_filings.done", **out)
        return out

    # Self-heal coverage repair (--param repair=true, the
    # sec_filings_freshness HealSpec). The daily/default path runs with
    # handler defaults (max_tickers=200, lookback=90, skip-guard ON) —
    # correct for the incremental, but it CANNOT clear
    # `insufficient_stock_coverage` (needs ≥30% of the ~1,500-name T1+T2
    # stock universe with a filing in the trailing 180d): 200<<needed,
    # 90d<180d window, and (pre-fix) the HealSpec's skip_guard_days was
    # silently dropped because cfg was never overlaid here. The heal
    # must re-pull the full universe over the coverage window. Mirrors
    # the daily_bars repair_coverage / eight_k_backfill named-mode
    # pattern (no fragile --param soup; max_tickers=None can't be
    # expressed via the int-coercing --param channel).
    if cfg.get("repair"):
        config = {
            "lookback_days": int(cfg.get("lookback_days", 200)),  # ≥ 180d coverage window
            "max_tickers": None,                                  # whole T1+T2 stock universe
            "skip_guard_days": 0,                                 # force past the skip-guard
        }
        log.info("ops.stage.sec_filings.repair_start",
                 lookback_days=config["lookback_days"])
        try:
            rows = await handle_sec_filings(pool, config)
        except Exception as exc:
            log.error("ops.stage.sec_filings.failed", error=str(exc), repair=True)
            raise
        async with pool.acquire() as conn:
            snap = await conn.fetchrow(
                "SELECT COUNT(*) r, COUNT(DISTINCT ticker) t, "
                "MAX(filing_date) mx FROM platform.sec_insider_transactions"
            )
        out = {
            "repair": True, "rows_loaded": int(rows or 0),
            "insider_rows_total": int(snap["r"] or 0),
            "tickers_covered_insider": int(snap["t"] or 0),
            "latest_filing": snap["mx"].isoformat() if snap["mx"] else None,
        }
        log.info("ops.stage.sec_filings.done", **out)
        return out

    if backfill:
        today = datetime.now(UTC).date()
        lookback_days = (today - _date(2018, 1, 1)).days
        config = {
            "lookback_days": lookback_days,
            "max_tickers": None,
            "skip_guard_days": 0,  # bypass the 6-day skip-guard for the one-shot.
            # #132: the historical insider bootstrap uses SEC's BULK
            # Form 345 quarterly datasets (~336 MB / ~33 zips, parsed
            # locally) instead of the per-ticker submissions+XML crawl
            # (hundreds of thousands of fetches at 8 req/s ≈ ~30h). The
            # bulk path is minutes, idempotent, and pooler-safe (one
            # short txn per quarter). The per-ticker adapter remains the
            # daily/weekly incremental. ticker_chunk_size/skip_covered
            # still apply to any non-bulk fallback.
            "bulk_form345": True,
            "ticker_chunk_size": 40,
            "skip_covered": True,
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

    start_d = date.today() - timedelta(days=14)  # noqa: DTZ011
    end_d = date.today() - timedelta(days=1)  # noqa: DTZ011

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

    counters = await prescreen_momentum(pool, date.today())  # noqa: DTZ011
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


async def _stage_canary_inject_trigger(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inject ONE well-formed forensics_triggers row for engine='canary'
    ONLY (DA-2 end-to-end harness). Payload mirrors the forensics
    producer's shape per kind + a source='canary_injection' marker for
    audit/teardown. ``--param teardown=true`` removes all injected rows.
    NEVER writes for any engine other than canary.

    Supported kinds: ``outlier_loss``, ``loss_cluster``, ``drawdown_period``.
    Default kind is ``loss_cluster``.
    """
    import json as _json
    from datetime import UTC
    from datetime import datetime as _dt

    cfg = config or {}
    if cfg.get("engine", "canary") != "canary":
        raise ValueError(
            "canary_inject_trigger writes for engine='canary' ONLY — "
            "pass engine='canary' or omit the param entirely")
    if cfg.get("teardown"):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.forensics_triggers "
                "WHERE payload->>'source' = $1",
                _CANARY_INJECTION_SOURCE)
        return {"teardown": True}

    kind = str(cfg.get("kind", "loss_cluster"))
    if kind not in ("outlier_loss", "loss_cluster", "drawdown_period"):
        raise ValueError(
            f"unknown kind {kind!r}; valid kinds: "
            "outlier_loss, loss_cluster, drawdown_period")
    now = _dt.now(UTC)
    if kind == "loss_cluster":
        streak = int(cfg.get("streak", 5))
        fp = f"canary|cluster|ca_inject|{streak}"
        payload: dict[str, Any] = {
            "engine": "canary",
            "streak_length": streak,
            "trade_ids": [f"ca_inject_{i}" for i in range(streak)],
            "total_loss": "-100.00",
            "ended_at": now.isoformat(),
            "fingerprint": fp,
            "source": _CANARY_INJECTION_SOURCE,
        }
    elif kind == "drawdown_period":
        fp = f"canary|dd|inject|{now.date().isoformat()}"
        payload = {
            "engine": "canary",
            "peak_equity": "10000",
            "peak_date": now.date().isoformat(),
            "trough_equity": "8500",
            "drawdown_pct": "0.1500",
            "days_in_drawdown": int(cfg.get("days", 20)),
            "fingerprint": fp,
            "source": _CANARY_INJECTION_SOURCE,
        }
    else:  # outlier_loss
        fp = "canary|ca_inject_outlier"
        payload = {
            "engine": "canary",
            "trade_id": "ca_inject_outlier",
            "ticker": "SPY",
            "pnl_net": "-500.0000",
            "mean": "-10.0000",
            "stdev": "50.0000",
            "threshold": "-160.0000",
            "exit_ts": now.isoformat(),
            "fingerprint": fp,
            "source": _CANARY_INJECTION_SOURCE,
        }
    async with pool.acquire() as conn:
        exists = await conn.fetchrow(
            "SELECT 1 FROM platform.forensics_triggers WHERE "
            "trigger_kind=$1 AND payload->>'fingerprint'=$2 LIMIT 1",
            kind, fp)
        if exists is None:
            await conn.execute(
                "INSERT INTO platform.forensics_triggers "
                "(trigger_kind, payload, fired_at) VALUES ($1,$2::jsonb,$3)",
                kind, _json.dumps(payload), now)
    return {"injected": kind, "fingerprint": fp, "engine": "canary"}


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
    # Bounded 14-day prune of platform.risk_close_ledger (#251 B1.4) —
    # the never-fail-open close arbiter only needs to retain a close key
    # long enough to dedupe the same-session dual-decrement; a settled
    # trade_id is never re-closed. No new daemon — rides --update.
    ("risk_close_ledger_prune", lambda pool, cfg: (lambda: _stage_risk_close_ledger_prune(pool)), STAGE_TIMEOUT_SEC),
    ("fundamentals_refresh",lambda pool, cfg: (lambda: _stage_fundamentals_refresh(pool, cfg)),HEAVY_STAGE_TIMEOUT_SEC),
    # Compute point-in-time P/B + D/E on the rows fundamentals_refresh
    # just landed. Set-based UPDATE — closes the manual operator step
    # where pb/de sat NULL until someone ran scripts/compute_fundamental_ratios.py.
    # Chained immediately after fundamentals_refresh so the new rows get
    # ratios in the same cycle (cmd_update runs stages sequentially +
    # asyncpg writes commit per-statement, so the read-after-write
    # semantics hold without an explicit transaction boundary). Migrated
    # 2026-05-20 from scripts/compute_fundamental_ratios.py (orphan-
    # scripts audit; see docs/superpowers/audits/2026-05-20-orphan-scripts-catalog.md).
    ("compute_fundamental_ratios", lambda pool, cfg: (lambda: _stage_compute_fundamental_ratios(pool, cfg)), STAGE_TIMEOUT_SEC),
    # Order corrected 2026-05-14 (audit O-1/O-2/O-3): tier_refresh +
    # classify_tickers must run BEFORE earnings_refresh + sec_filings
    # because the latter two filter by ticker_classifications.asset_class.
    # classify_tickers' per-ticker fallback path reads liquidity_tiers
    # for the T1+T2 set, so tier_refresh runs first.
    # Liquidity tier refresh — quarterly cadence (90d skip guard).
    # Stage 1: Corwin-Schultz bootstrap (60d freshness gate) writes to
    # spread_observations. Stage 2: assign_tiers aggregates into
    # liquidity_tiers. Closes audit gap G-2: the bootstrap used to
    # be manual-only via scripts/run_tier_refresh.sh.
    ("tier_refresh",        lambda pool, cfg: (lambda: _stage_tier_refresh(pool, cfg)),         HEAVY_STAGE_TIMEOUT_SEC),
    # Ticker classifications refresh — monthly cadence (30d skip guard +
    # 95% coverage check). Picks up new listings after universe
    # expansion. ETFs/SPACs/funds get flagged so catalyst + earnings
    # pipelines can filter them out.
    ("classify_tickers",    lambda pool, cfg: (lambda: _stage_classify_tickers(pool, cfg)),     HEAVY_STAGE_TIMEOUT_SEC),
    ("delist_stale",        lambda pool, cfg: (lambda: _stage_delist_stale(pool)),             STAGE_TIMEOUT_SEC),
    # earnings_refresh — earnings-beat events for vector engine.
    # Heavy timeout (1h) because the FMP loop is ~1 sec per ticker;
    # T1+T2 stock subset is ~66 tickers so a fresh run is ~3 min, but
    # the universe could grow. Stage short-circuits in ~10ms when
    # the table was refreshed within 6 days.
    ("earnings_refresh",    lambda pool, cfg: (lambda: _stage_earnings_refresh(pool, cfg)),    HEAVY_STAGE_TIMEOUT_SEC),
    # SEC EDGAR Form 4 + 8-K — reference implementation of the
    # standard 5-stage data-adapter pipeline. CSV-first, idempotent,
    # skip-guard tightened from 6 → 3 days 2026-05-14: Form 4 has a
    # 2-business-day filing deadline so 6d staleness was half-stale on
    # average. Heavy timeout: ~200 tickers × ~1.5s/call (rate-limited
    # under SEC's 10 req/sec cap) + Form 4 XML fetches.
    ("sec_filings",         lambda pool, cfg: (lambda: _stage_sec_filings(pool, backfill=bool(cfg.get("_sec_backfill")), cfg=cfg)), SEC_FILINGS_STAGE_TIMEOUT_SEC),
    # FRED macro indicators — weekly. Five canonical series (sahm_rule,
    # industrial_production, initial_claims, yield_curve, hy_spread)
    # via FREDAdapter, idempotent ON CONFLICT, 7-day skip-guard.
    # Added 2026-05-14 — closes the last "spec-only" gap in §6.1.
    ("macro_indicators",    lambda pool, cfg: (lambda: _stage_macro_indicators(pool, cfg)),    STAGE_TIMEOUT_SEC),
    # greeks.pro free-tier max-pain (1 symbol/day, X-API-Key, idempotent
    # ON CONFLICT, handler same-day skip-guard). Added 2026-05-16.
    ("greeks_max_pain",     lambda pool, cfg: (lambda: _stage_greeks_max_pain(pool, cfg)),     STAGE_TIMEOUT_SEC),
    # Finnhub insider-sentiment (MSPR) for the T1/T2 universe; monthly,
    # 25-day skip-guard, idempotent ON CONFLICT. Added 2026-05-16.
    ("finnhub_insider_sentiment", lambda pool, cfg: (lambda: _stage_finnhub_insider_sentiment(pool, cfg)), HEAVY_STAGE_TIMEOUT_SEC),
    # ApeWisdom Reddit social sentiment; all pages, T1/T2 local filter,
    # 24h skip-guard, idempotent ON CONFLICT. Added 2026-05-16.
    ("apewisdom_social_sentiment", lambda pool, cfg: (lambda: _stage_apewisdom_social_sentiment(pool, cfg)), STAGE_TIMEOUT_SEC),
    # Fear & Greed: derived from existing platform data (no provider).
    # Daily recompute; --param backfill=true for full 2001→ history.
    ("fear_greed",          lambda pool, cfg: (lambda: _stage_fear_greed(pool, cfg)),            STAGE_TIMEOUT_SEC),
    # FINRA short interest (bi-monthly, OAuth2, PIT release_date) and
    # IBorrowDesk borrow rates (daily, scrape-fragile). Final 2 of the
    # master-plan data layer. Added 2026-05-16.
    ("finra_short_interest", lambda pool, cfg: (lambda: _stage_finra_short_interest(pool, cfg)), HEAVY_STAGE_TIMEOUT_SEC),
    ("iborrowdesk_borrow_rates", lambda pool, cfg: (lambda: _stage_iborrowdesk_borrow_rates(pool, cfg)), HEAVY_STAGE_TIMEOUT_SEC),
    # AAII Sentiment Survey (weekly, no auth, full-history workbook).
    # Added 2026-05-16.
    ("aaii_sentiment", lambda pool, cfg: (lambda: _stage_aaii_sentiment(pool, cfg)), HEAVY_STAGE_TIMEOUT_SEC),
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
    # Canary harness — injects / tears down forensics_triggers test rows
    # for the DA-2 end-to-end proof run. Canary-only hard-guarded.
    # Use: --stage canary_inject_trigger --param kind=loss_cluster [--param streak=5]
    #      --stage canary_inject_trigger --param teardown=true
    ("canary_inject_trigger", lambda pool, cfg: (lambda: _stage_canary_inject_trigger(pool, cfg)), STAGE_TIMEOUT_SEC),
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


async def _per_feed_tripwire(
    pool: asyncpg.Pool,
    stage_name: str,
    run_id: str,
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
    cycle_green: set[str],
) -> None:
    """#185 Phase 2/3 — fail-safe wrapper around the per-feed
    validate-on-completion hook. Resolves stage→feed, validates it
    immediately and bounded-heals on red (detection a cycle earlier).
    ``cycle_green`` is the in-cycle set of feeds already validated green
    this run (Phase 3: a derived feed validates only once every upstream
    is in it); ``on_stage_complete`` mutates it.

    Hard invariant: this NEVER raises into the ingest cycle and NEVER
    aborts it. An escalation here is logged + surfaced (forensic
    visibility), but the authoritative 100%-green decision still belongs
    to the end-of-cycle data_validation + Step-4 whole-layer self-heal
    (spec §4). Worst case the hook is a no-op and the final gate catches
    everything exactly as before this change.
    """
    try:
        from tpcore.selfheal.per_feed import on_stage_complete

        outcome = await on_stage_complete(
            pool, stage_name, run_id, cycle_green=cycle_green
        )
        if outcome is None:
            return  # infra stage or deferred derived feed — no-op
        if outcome.green:
            log.info(
                "ops.per_feed.green",
                stage=stage_name, feed=outcome.feed, healed=outcome.healed,
            )
            if outcome.healed:
                await db_log.log(
                    "SELF_HEAL",
                    f"per-feed early heal: {outcome.feed} "
                    f"({', '.join(outcome.healed)})",
                    severity="INFO",
                    data={"feed": outcome.feed, "healed": outcome.healed,
                          "stage": stage_name, "phase": "per_feed_tripwire"},
                )
        else:
            log.warning(
                "ops.per_feed.escalated",
                stage=stage_name, feed=outcome.feed,
                escalated=outcome.escalated,
            )
            await db_log.log(
                "INGESTION_FAILED",
                f"per-feed tripwire: {outcome.feed} still red "
                f"(final gate is authoritative) — {outcome.escalated}",
                severity="WARNING",
                data={"feed": outcome.feed, "stage": stage_name,
                      "escalated": outcome.escalated,
                      "phase": "per_feed_tripwire",
                      # Not a cycle failure: the end-of-cycle gate
                      # remains the authority. Marked so the recent-
                      # error probe doesn't double-count it.
                      "noise": True},
            )
    except Exception as exc:  # noqa: BLE001 — tripwire must never break the cycle
        log.error(
            "ops.per_feed.hook_error",
            stage=stage_name, error=str(exc),
            exc_type=type(exc).__name__,
        )


async def cmd_update(
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
    *,
    dry_run: bool,
    force: bool = False,
    only: set[str] | None = None,
    per_feed_validate: bool = True,
) -> UpdateSummary:
    started_at = datetime.now(UTC)
    summary = UpdateSummary(run_id=db_log.run_id, started_at=started_at, finished_at=started_at)

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
                data={
                    "stage": "pre_flight",
                    "reason": "market_open",
                    # Marks this row as expected behavior so the
                    # _check_recent_errors probe filters it out. The
                    # operator chose to invoke --update during regular
                    # session and got the guard's designed refusal;
                    # this is not a platform failure.
                    "noise": True,
                },
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

    # #185 Phase 3 — in-cycle set of feeds the per-feed tripwire has
    # validated green this run. A derived feed (fear_greed) validates
    # only once every upstream feed is in here.
    cycle_green: set[str] = set()

    for name, factory_builder, timeout in _STAGE_SPECS:
        # Profile-driven feed dispatch (#165): when ``only`` is given
        # (the feed dispatcher's due list), run just those stages.
        # ``only=None`` → every stage (today's blanket behaviour,
        # preserved). data_validation/forensics are infra steps, not
        # feeds — always allowed through so the green-gate + dossier
        # still run regardless of which feeds were due.
        if only is not None and name not in only and name not in (
            "data_validation", "forensics", "reconcile",
        ):
            continue
        result = await _run_stage(
            name,
            factory_builder(pool, daily_bars_config),
            log=log,
            db_log=db_log,
            dry_run=dry_run,
            timeout=timeout,
        )
        summary.stages.append(result)
        # #185 Phase 2/3 — per-feed validate-on-completion (early
        # tripwire). Leaf feeds validate on their own stage; a derived
        # feed validates on its own stage once every upstream went green
        # this cycle (cycle_green). The standalone single-stage path
        # does not trigger this (lock-safety, spec §5). Runs inside
        # run_data_operations.sh's lock (cycle path). Fail-safe: a hook
        # error NEVER aborts the cycle — the end-of-cycle
        # data_validation + Step-4 whole-layer self-heal stay the
        # authoritative 100%-green gate (spec §4).
        if per_feed_validate and not dry_run and result.status == "OK":
            await _per_feed_tripwire(
                pool, name, str(summary.run_id),
                log=log, db_log=db_log, cycle_green=cycle_green,
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
    params: dict[str, Any] | None = None,
) -> UpdateSummary:
    """Run a single stage by name. Same logging + event shape as ``cmd_update``
    — different ``run_id``. Used by the dashboard's per-stage Fix buttons.

    Acquires a Postgres advisory lock keyed on the stage name so that a
    locally-launched stage cannot race a future Railway cron (or another
    operator session) running the same stage concurrently. Bails with a
    structured FAILED event if the lock is held — clear signal, not a
    silent merge."""
    started_at = datetime.now(UTC)
    summary = UpdateSummary(run_id=db_log.run_id, started_at=started_at, finished_at=started_at)

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

            # --param overrides overlay the DB config dict. This is the
            # canonical parameterised-backfill channel: e.g. daily_bars
            # with {"universe": "active", "lookback_days": 10,
            # "end_offset_days": 1} re-pulls the recent window for the
            # whole active universe — no one-off script.
            if params:
                daily_bars_config = {**daily_bars_config, **params}
                log.info("ops.stage.param_override", stage=name, params=params)

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
    today = date.today()  # noqa: DTZ011
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
    today = date.today()  # noqa: DTZ011
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

# Earnings events are earnings-beat snapshots; the refresh stage is
# quarterly-cadence so 95 days is the same threshold as fundamentals.
EARNINGS_EVENTS_FRESHNESS_MAX_DAYS = 95

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
    today = date.today()  # noqa: DTZ011
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


async def _check_earnings_events_freshness(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — newest earnings_events + T1+T2 stock coverage."""
    row = await pool.fetchrow(
        """
        SELECT
            (SELECT MAX(event_date) FROM platform.earnings_events) AS latest_event,
            (SELECT COUNT(DISTINCT ticker) FROM platform.earnings_events) AS tickers,
            (SELECT COUNT(*) FROM platform.earnings_events) AS rows_total
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
    today = date.today()  # noqa: DTZ011
    age_days = (today - latest).days
    return {
        "ok": age_days <= EARNINGS_EVENTS_FRESHNESS_MAX_DAYS,
        "latest_event": latest.isoformat(),
        "age_days": age_days,
        "threshold_days": EARNINGS_EVENTS_FRESHNESS_MAX_DAYS,
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
    today = date.today()  # noqa: DTZ011
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
    today = date.today()  # noqa: DTZ011
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
    """Trade-monitor liveness probe — reads from ``platform.daemon_heartbeats``.

    The trade_monitor writes a heartbeat row (UPSERT, single row) every
    15 minutes regardless of fills, so quiet-day silence no longer
    looks like daemon death. The status column is the daemon's
    self-report: ``healthy`` (stream connected, in normal loop) or
    ``degraded`` (stream errored, backing off for reconnect). The probe
    interprets:

    * ``healthy`` + fresh timestamp → ok=True (green)
    * ``degraded`` + fresh timestamp → ok=False (red), reason set
    * any status with stale timestamp (> 60 min) → ok=False (red)
    * ``down`` (operator-set, not written by the daemon today) → red
    * missing row → red

    Replaces the prior ``MAX(recorded_at) WHERE engine='trade_monitor'``
    query against ``application_log``, which went red on quiet days
    because the monitor only logged on fills / reconnects. Heartbeat
    writer in ``tpcore/trade_monitor.py::_heartbeat_writer``.
    """
    row = await pool.fetchrow(
        """
        SELECT last_heartbeat, status
        FROM platform.daemon_heartbeats
        WHERE daemon_name = 'trade_monitor'
        """
    )
    if row is None:
        return {
            "ok": False,
            "latest_event": None,
            "status": None,
            "reason": "no daemon_heartbeats row for trade_monitor",
        }
    last_heartbeat = row["last_heartbeat"]
    status = row["status"]
    age_minutes = (datetime.now(UTC) - last_heartbeat).total_seconds() / 60.0
    is_fresh = age_minutes <= TRADE_MONITOR_HEARTBEAT_MAX_MINUTES
    ok = (status == "healthy") and is_fresh
    reason = None
    if not is_fresh:
        reason = f"heartbeat stale: {age_minutes:.1f} min > {TRADE_MONITOR_HEARTBEAT_MAX_MINUTES} min"
    elif status != "healthy":
        reason = f"daemon self-reports status={status!r}"
    return {
        "ok": ok,
        "latest_event": last_heartbeat.isoformat(),
        "age_minutes": round(age_minutes, 2),
        "threshold_minutes": TRADE_MONITOR_HEARTBEAT_MAX_MINUTES,
        "status": status,
        **({"reason": reason} if reason else {}),
    }


_EXPECTED_DAEMON_LABELS = {
    "com.michael.trading.engine-service",
    "com.michael.trading.data-repair-service",
    "com.michael.trading.data-operations",
}
_RETIRED_DAEMON_LABELS = {
    "com.michael.trading.trade-monitor",
    "com.michael.trading.weekly-digest",
}


async def _check_consolidated_daemon_topology(pool: asyncpg.Pool) -> dict[str, Any]:
    """DA-3 two-daemon invariant (engine-lane probe; NOT in the
    data-pipeline audit). Live ``launchctl list`` label set must be
    exactly the 3 expected daemons, with the 2 retired ones absent.

    macOS-only — returns ``ok=False`` (not an exception) when
    ``launchctl`` is absent (CI/Linux), so a non-macOS ``--check`` run
    shows this probe red-by-design rather than crashing.
    """
    del pool  # unused; signature contract only
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        labels = {
            ln.split("\t")[-1].strip()
            for ln in proc.stdout.splitlines()
            if "com.michael.trading." in ln
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"launchctl list failed: {exc}"}
    present_retired = labels & _RETIRED_DAEMON_LABELS
    missing_expected = _EXPECTED_DAEMON_LABELS - labels
    unexpected = labels - _EXPECTED_DAEMON_LABELS - _RETIRED_DAEMON_LABELS
    ok = not present_retired and not missing_expected and not unexpected
    res: dict[str, Any] = {"ok": ok, "labels": sorted(labels)}
    if present_retired:
        res["reason"] = f"retired daemon still loaded: {sorted(present_retired)}"
    elif unexpected:
        res["reason"] = f"unexpected daemon label: {sorted(unexpected)}"
        res["unexpected"] = sorted(unexpected)
    elif missing_expected:
        res["reason"] = f"expected daemon missing: {sorted(missing_expected)}"
    return res


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
    """ERROR/CRITICAL events in the last 24h, filtered for actionability.

    Two filters convert "noise" into a separate informational bucket
    so the red light only flips for genuinely-actionable failures:

    1. **Structured noise tag.** ``data->>'noise' = 'true'`` marks
       rows the operator should NOT be paged on (e.g. the market-hours
       pre-flight refusal — operator chose to fire --update during
       regular session and got the guard's designed exit). Added at
       the write site in ``cmd_update``; new noise sources should add
       the same flag rather than be allowlisted by message pattern
       (which drifts).
    2. **Self-heal correlation.** Any ERROR whose ``run_id`` has a
       later ``SHUTDOWN`` event with ``exit_code=0`` within the 24h
       window is considered self-healed (e.g. a transient timeout
       followed by a successful retry). Excluded from the critical
       bucket.

    Output splits the surviving rows into:
        ``critical`` — red, ok=False, operator action required
        ``transient`` — informational, ok still True; surfaced for
                        diagnostics but not for alerting
    """
    critical_rows = await pool.fetch(
        """
        SELECT engine, event_type, severity, message, recorded_at, run_id
        FROM platform.application_log a
        WHERE a.severity IN ('ERROR', 'CRITICAL')
          AND a.recorded_at > now() - INTERVAL '24 hours'
          AND COALESCE(a.data->>'noise', 'false') != 'true'
          AND NOT EXISTS (
            SELECT 1
            FROM platform.application_log b
            WHERE b.run_id = a.run_id
              AND b.event_type = 'SHUTDOWN'
              AND b.message LIKE '%exit_code=0%'
              AND b.recorded_at > a.recorded_at
              AND b.recorded_at > now() - INTERVAL '24 hours'
          )
        ORDER BY a.recorded_at DESC
        LIMIT 50
        """
    )
    transient_count = await pool.fetchval(
        """
        SELECT COUNT(*) FROM platform.application_log a
        WHERE a.severity IN ('ERROR', 'CRITICAL')
          AND a.recorded_at > now() - INTERVAL '24 hours'
          AND (
            COALESCE(a.data->>'noise', 'false') = 'true'
            OR EXISTS (
                SELECT 1 FROM platform.application_log b
                WHERE b.run_id = a.run_id
                  AND b.event_type = 'SHUTDOWN'
                  AND b.message LIKE '%exit_code=0%'
                  AND b.recorded_at > a.recorded_at
                  AND b.recorded_at > now() - INTERVAL '24 hours'
            )
          )
        """
    ) or 0
    critical = [
        {
            "engine": r["engine"],
            "event_type": r["event_type"],
            "severity": r["severity"],
            "message": r["message"],
            "recorded_at": r["recorded_at"].isoformat(),
        }
        for r in critical_rows
    ]
    return {
        "ok": len(critical) == 0,
        "critical_count": len(critical),
        "transient_count": int(transient_count),
        "critical": critical,
    }


async def _check_daemon_progress(pool: asyncpg.Pool) -> dict[str, Any]:
    """Live progress probe for the data_operations daemon run.

    Phase 0 audit (2026-05-15) verified every `_STAGE_SPECS` stage writes
    a symmetric INGESTION_START + INGESTION_COMPLETE pair (or
    INGESTION_FAILED on failure), all share the daemon's tagged
    ``run_id``, and the daemon writes a SHUTDOWN event on exit. Stage
    name is consistently available at ``data->>'stage'``. This probe
    builds on those guarantees.

    Status semantics:
        * ``no_recent_run``  — no daemon STARTUP within 25h. ok=True
          (informational; if it's been > 30h, the dedicated
          ``missed_data_operations`` probe goes red).
        * ``running``        — STARTUP present, no SHUTDOWN, no FAILED
          stages yet. ok=True (informational, panel-only).
        * ``completed_clean``— SHUTDOWN exit_code=0, no FAILED stages.
          ok=True.
        * ``completed_with_failures`` — SHUTDOWN exit_code != 0 OR any
          INGESTION_FAILED row for this run. ok=False.

    Scope-limit: this probe sees only the 15 stages run inside
    ``ops.py --update``. The shell-wrapper's later steps (audit,
    validation re-confirm, compress, DATA_OPERATIONS_COMPLETE emit) do
    NOT write to ``application_log`` — they're outside this probe's
    visibility. The terminal signal that the WHOLE workflow finished
    is the ``DATA_OPERATIONS_COMPLETE`` event in ``application_log``,
    which IS surfaced here as ``workflow_complete``.
    """
    # Find the most recent daemon-tagged STARTUP.
    startup = await pool.fetchrow(
        """
        SELECT run_id, recorded_at
        FROM platform.application_log
        WHERE engine = 'ops'
          AND event_type = 'STARTUP'
          AND data->>'source' = 'data_operations_daemon'
          AND recorded_at > now() - INTERVAL '25 hours'
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    )
    if startup is None:
        return {
            "ok": True,
            "state": "no_recent_run",
            "stages": [],
            "reason": "no data_operations_daemon STARTUP in last 25h",
        }
    run_id = startup["run_id"]
    started_at = startup["recorded_at"]

    # Collect every per-stage event for this run.
    stage_rows = await pool.fetch(
        """
        SELECT data->>'stage' AS stage, event_type, recorded_at
        FROM platform.application_log
        WHERE run_id = $1
          AND event_type IN ('INGESTION_START', 'INGESTION_COMPLETE', 'INGESTION_FAILED')
          AND data->>'stage' IS NOT NULL
        ORDER BY recorded_at
        """,
        run_id,
    )

    # Per-stage status. Stage with START + no COMPLETE/FAILED → running.
    # Stage with COMPLETE → completed. Stage with FAILED → failed.
    by_stage: dict[str, dict[str, Any]] = {}
    for r in stage_rows:
        st = r["stage"]
        if st not in by_stage:
            by_stage[st] = {"stage": st, "started_at": None, "ended_at": None, "status": None}
        if r["event_type"] == "INGESTION_START":
            by_stage[st]["started_at"] = r["recorded_at"]
            if by_stage[st]["status"] is None:
                by_stage[st]["status"] = "running"
        elif r["event_type"] == "INGESTION_COMPLETE":
            by_stage[st]["ended_at"] = r["recorded_at"]
            by_stage[st]["status"] = "completed"
        elif r["event_type"] == "INGESTION_FAILED":
            by_stage[st]["ended_at"] = r["recorded_at"]
            by_stage[st]["status"] = "failed"

    # Compute elapsed-ms per stage for the panel.
    stages_out = []
    for st, info in by_stage.items():
        elapsed_ms = None
        if info["started_at"] and info["ended_at"]:
            elapsed_ms = int((info["ended_at"] - info["started_at"]).total_seconds() * 1000)
        elif info["started_at"]:
            elapsed_ms = int((datetime.now(UTC) - info["started_at"]).total_seconds() * 1000)
        stages_out.append({
            "stage": st,
            "status": info["status"],
            "started_at": info["started_at"].isoformat() if info["started_at"] else None,
            "ended_at": info["ended_at"].isoformat() if info["ended_at"] else None,
            "elapsed_ms": elapsed_ms,
        })

    # Was there a SHUTDOWN for this run? Parse its exit_code from message.
    shutdown_row = await pool.fetchrow(
        """
        SELECT recorded_at, message
        FROM platform.application_log
        WHERE run_id = $1
          AND event_type = 'SHUTDOWN'
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        run_id,
    )
    n_failed = sum(1 for s in stages_out if s["status"] == "failed")
    workflow_complete = False
    if shutdown_row is not None:
        # message format: "ops CLI finished (exit_code=N)"
        msg = shutdown_row["message"] or ""
        exit_clean = "exit_code=0" in msg
        if exit_clean and n_failed == 0:
            state = "completed_clean"
            ok = True
        else:
            state = "completed_with_failures"
            ok = False
        # Workflow done iff DATA_OPERATIONS_COMPLETE was emitted (wrapper Step 6).
        evt = await pool.fetchval(
            """
            SELECT 1 FROM platform.application_log
            WHERE event_type = 'DATA_OPERATIONS_COMPLETE'
              AND recorded_at > $1
              AND recorded_at < $1 + INTERVAL '30 minutes'
            LIMIT 1
            """,
            shutdown_row["recorded_at"],
        )
        workflow_complete = evt is not None
    else:
        state = "running"
        ok = True  # informational while in flight

    return {
        "ok": ok,
        "state": state,
        "run_id": str(run_id),
        "started_at": started_at.isoformat(),
        "n_stages_total": len(stages_out),
        "n_stages_completed": sum(1 for s in stages_out if s["status"] == "completed"),
        "n_stages_failed": n_failed,
        "n_stages_running": sum(1 for s in stages_out if s["status"] == "running"),
        "workflow_complete": workflow_complete,
        "stages": stages_out,
    }


async def _check_greeks_max_pain(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — greeks.pro max-pain snapshot freshness.

    ok=True if the tracked symbol (SPY) has a snapshot within 7d;
    warn (yellow) in the 7-14d band; red beyond.
    """
    row = await pool.fetchrow(
        """
        SELECT MAX(observed_date) AS latest, COUNT(*) AS rows_total
        FROM platform.options_max_pain
        WHERE symbol = 'SPY'
        """
    )
    latest = row["latest"] if row else None
    total = int(row["rows_total"]) if row and row["rows_total"] else 0
    if latest is None:
        return {"ok": False, "reason": "no SPY max-pain rows", "rows_total": 0}
    age = (date.today() - latest).days  # noqa: DTZ011
    return {
        "ok": age <= 7,
        "warn": 7 < age <= 14,
        "latest_observed_date": latest.isoformat(),
        "age_days": age,
        "threshold_days": 7,
        "rows_total": total,
    }


async def _check_finnhub_insider_sentiment(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — Finnhub insider-sentiment freshness.

    ok=True if the newest (year,month) period is ≤ 3 months old;
    warn (yellow) at 3-5 months; red beyond / empty.
    """
    row = await pool.fetchrow(
        """
        SELECT MAX(year * 12 + month) AS newest_period, COUNT(*) AS rows_total
        FROM platform.insider_sentiment
        """
    )
    total = int(row["rows_total"]) if row and row["rows_total"] else 0
    newest = row["newest_period"] if row else None
    if not total or newest is None:
        return {"ok": False, "reason": "no insider_sentiment rows", "rows_total": 0}
    now = date.today()  # noqa: DTZ011
    age = (now.year * 12 + now.month) - int(newest)
    return {
        "ok": age <= 3,
        "warn": 3 < age <= 5,
        "newest_period_months_old": age,
        "threshold_months": 3,
        "rows_total": total,
    }


async def _check_apewisdom_social_sentiment(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — ApeWisdom social-sentiment freshness.

    'ApeWisdom sentiment: N tickers / latest YYYY-MM-DD'. Green ≤3d,
    yellow ≤7d, red >7d / empty.
    """
    row = await pool.fetchrow(
        """
        SELECT MAX(date) AS latest,
               COUNT(DISTINCT ticker) FILTER (
                   WHERE date = (SELECT MAX(date) FROM platform.social_sentiment)
               ) AS tickers
        FROM platform.social_sentiment
        """
    )
    latest = row["latest"] if row else None
    tickers = int(row["tickers"]) if row and row["tickers"] else 0
    if latest is None:
        return {"ok": False, "reason": "no social_sentiment rows",
                "summary": "ApeWisdom sentiment: 0 tickers / latest none"}
    age = (date.today() - latest).days  # noqa: DTZ011
    return {
        "ok": age <= 3,
        "warn": 3 < age <= 7,
        "summary": f"ApeWisdom sentiment: {tickers} tickers / latest {latest.isoformat()}",
        "latest": latest.isoformat(),
        "tickers": tickers,
        "age_days": age,
    }


async def _check_fear_greed(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — 'Fear & Greed: XX (label) as of YYYY-MM-DD'.

    Color: red Extreme Fear, yellow Fear, green Neutral/Greed,
    gray Extreme Greed.
    """
    row = await pool.fetchrow(
        """
        SELECT date, score, label FROM platform.fear_greed
        ORDER BY date DESC LIMIT 1
        """
    )
    if row is None:
        return {"ok": False, "reason": "no fear_greed rows",
                "summary": "Fear & Greed: — (no data)"}
    label = str(row["label"])
    score = float(row["score"])
    color = {
        "Extreme Fear": "red", "Fear": "yellow",
        "Neutral": "green", "Greed": "green", "Extreme Greed": "gray",
    }.get(label, "gray")
    age = (date.today() - row["date"]).days  # noqa: DTZ011
    return {
        "ok": age <= 5,  # ~3 trading days incl. a weekend
        "summary": f"Fear & Greed: {score:.1f} ({label}) as of {row['date'].isoformat()}",
        "score": score,
        "label": label,
        "color": color,
        "as_of": row["date"].isoformat(),
    }


async def _check_finra_short_interest(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — FINRA short-interest freshness (bi-monthly).
    Green ≤21d, yellow ≤35d, red >35d / empty."""
    row = await pool.fetchrow(
        """
        SELECT MAX(settlement_date) latest, COUNT(*) n,
               COUNT(*) FILTER (WHERE short_interest_pct IS NOT NULL) with_pct
        FROM platform.short_interest
        """
    )
    latest = row["latest"] if row else None
    if latest is None:
        return {"ok": False, "reason": "no short_interest rows", "rows": 0}
    age = (date.today() - latest).days  # noqa: DTZ011
    return {
        "ok": age <= 21, "warn": 21 < age <= 35,
        "summary": f"FINRA short interest: latest settlement {latest.isoformat()} ({age}d)",
        "rows": int(row["n"]), "with_pct": int(row["with_pct"]), "age_days": age,
    }


async def _check_iborrowdesk_borrow_rates(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — IBorrowDesk borrow-rate freshness (daily).
    Green ≤2d, yellow ≤5d, red >5d / empty."""
    row = await pool.fetchrow(
        """
        SELECT MAX(date) latest,
               COUNT(DISTINCT ticker) FILTER (
                   WHERE date = (SELECT MAX(date) FROM platform.borrow_rates)
               ) tickers
        FROM platform.borrow_rates
        """
    )
    latest = row["latest"] if row else None
    if latest is None:
        return {"ok": False, "reason": "no borrow_rates rows (scrape blocked?)",
                "summary": "Borrow rates: no data"}
    age = (date.today() - latest).days  # noqa: DTZ011
    return {
        "ok": age <= 2, "warn": 2 < age <= 5,
        "summary": f"Borrow rates: {int(row['tickers'])} tickers / latest {latest.isoformat()}",
        "tickers": int(row["tickers"]), "age_days": age,
    }


async def _check_aaii_sentiment(pool: asyncpg.Pool) -> dict[str, Any]:
    """Dashboard probe — AAII Sentiment Survey (weekly).

    Data health: red if no data or latest > 10d old (matches the
    ``aaii_sentiment_freshness`` validation threshold). When data is
    fresh, the colour reflects the contrarian regime per spec: red if
    bearish > 55% (extreme fear → contrarian-bullish signal), green
    if neutral/balanced, gray (yellow) otherwise (e.g. extreme greed).
    """
    row = await pool.fetchrow(
        """
        SELECT date, bullish_pct, bearish_pct, neutral_pct
        FROM platform.aaii_sentiment
        ORDER BY date DESC
        LIMIT 1
        """
    )
    if row is None:
        return {"ok": False, "reason": "no aaii_sentiment rows",
                "summary": "AAII Sentiment: no data"}
    latest = row["date"]
    age = (date.today() - latest).days  # noqa: DTZ011
    bull = float(row["bullish_pct"])
    bear = float(row["bearish_pct"])
    summary = (
        f"AAII Sentiment: Bullish {bull:.1f}% / Bearish {bear:.1f}% "
        f"as of {latest.isoformat()}"
    )
    if age > 10:
        return {"ok": False, "reason": f"stale ({age}d old)",
                "summary": summary, "age_days": age}
    # Fresh data — colour by contrarian regime.
    if bear > 55.0:
        # Extreme bearishness is a contrarian-bullish flag — surface
        # it red so the operator sees the signal, per spec.
        return {"ok": False, "reason": "extreme bearish (>55%) — "
                "contrarian-bullish signal", "summary": summary,
                "age_days": age, "bearish_pct": bear}
    if bull <= 55.0:
        return {"ok": True, "summary": summary, "age_days": age}
    # Extreme greed / other — neither alarm nor green.
    return {"ok": True, "warn": True, "summary": summary, "age_days": age}


_CHECK_FNS = [
    ("db_connectivity", _check_connectivity),
    ("data_freshness", _check_freshness),
    ("row_counts", _check_row_counts),
    ("corporate_actions_freshness", _check_corp_actions_freshness),
    ("fundamentals_freshness", _check_fundamentals_freshness),
    ("earnings_events_freshness", _check_earnings_events_freshness),
    ("sec_filings_freshness", _check_sec_filings_freshness),
    ("macro_indicators_freshness", _check_macro_indicators_freshness),
    ("greeks_max_pain_freshness", _check_greeks_max_pain),
    ("insider_sentiment_freshness", _check_finnhub_insider_sentiment),
    ("social_sentiment_freshness", _check_apewisdom_social_sentiment),
    ("fear_greed", _check_fear_greed),
    ("short_interest_freshness", _check_finra_short_interest),
    ("borrow_rates_freshness", _check_iborrowdesk_borrow_rates),
    ("aaii_sentiment_freshness", _check_aaii_sentiment),
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
    ("consolidated_daemon_topology", _check_consolidated_daemon_topology),
    ("forensics", _check_forensics),
    ("daemon_progress", _check_daemon_progress),
    ("recent_errors", _check_recent_errors),
]


async def cmd_check(
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "run_id": str(db_log.run_id),
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
    ("earnings_events", "ticker_not_in_prices", """
        SELECT COUNT(*) FROM platform.earnings_events ce
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
        "--only",
        default=None,
        help="--update only: comma-separated stage names to run (the "
             "feed dispatcher's due list). Omitted = every stage "
             "(today's blanket sweep). data_validation/forensics/"
             "reconcile always run regardless.",
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
        "--param",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=(
            "--stage only: override a stage config key for this run "
            "(repeatable). Overlays the platform.ingestion_jobs config "
            "dict the stage handler receives. Values are coerced "
            "int/float/bool where unambiguous, else string. This is how "
            "backfills + special pulls run — via the canonical CLI with "
            "parameters, NOT a one-off script. Example: "
            "`--stage daily_bars --param lookback_days=10 "
            "--param end_offset_days=1 --force` re-pulls a 10-day "
            "window ending yesterday for the full active universe."
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
    p.add_argument(
        "--run-id",
        default=None,
        help=(
            "Optional UUID to use as the run_id for this invocation. "
            "Lets the bash wrapper (run_data_operations.sh) generate a "
            "shared run_id and instrument its own steps with the same "
            "id so the daemon progress panel sees the full end-to-end "
            "workflow. Auto-generated if omitted (backward-compatible)."
        ),
    )
    return p


async def amain(args: argparse.Namespace) -> int:
    from tpcore.db import build_asyncpg_pool
    from tpcore.logging.db_handler import DBLogHandler

    # --run-id lets the bash wrapper (run_data_operations.sh) pre-generate
    # a UUID it can share with its own _log_event.py calls; without it we
    # auto-generate so direct CLI invocations stay unchanged.
    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
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
            # ``--only`` provided (even as the NONE_DUE sentinel / empty)
            # → feed-driven subset; absent → None → full sweep (today's
            # behaviour). Truthiness must NOT collapse "nothing due"
            # into "run everything".
            _only = (
                {s.strip() for s in args.only.split(",") if s.strip()}
                if args.only is not None else None
            )
            update_summary = await cmd_update(
                pool, log, db_log, dry_run=args.dry_run,
                force=args.force, only=_only,
            )
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
                params=_parse_params(args.param),
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
