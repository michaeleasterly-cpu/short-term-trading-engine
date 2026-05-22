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


# ────────────────────────────────────────────────────────────────────────
# daily_bars force_refresh chunking (3600s timeout mitigation, 2026-05-21)
# ────────────────────────────────────────────────────────────────────────
#
# The `--param force_refresh=true --param universe=active` whole-universe
# path provably exceeds the 3600s stage timeout: two runs at 08:43 and
# 09:19 UTC on 2026-05-21 both timed out, leaving prices_daily stuck at
# ~5180 tickers/session vs the full ~7600. Root cause: a single
# `handle_daily_bars` call covering ~7000 tickers — bounded by Alpaca's
# multi-symbol endpoint (100 tickers/call) AND the per-call rate-limit
# sleep, so a whole-universe pull is many minutes regardless of cores.
#
# Fix mirrors the PR #222 Lab final-holdout chunking pattern: split the
# universe into bounded ticker slices, each its own `handle_daily_bars`
# call, aggregate the rows-upserted total at the stage level. 500-ticker
# slices ≈ 14 chunks × ~4 min ≈ comfortably under 3600s with margin.
# Per-chunk failure does NOT abort the run — we emit `CHUNK_FAILED`
# via structlog (the stage's own `db_log` would require threading
# another parameter — out of scope for a transport-layer fix) and
# continue. The producer-self-validation (coverage_collapse) runs ONCE
# at the end against the aggregate state.
#
# Idempotency: `handle_daily_bars` upserts, so re-running a chunk that
# already landed is a no-op on the row set.
FORCE_REFRESH_CHUNK_SIZE = 500


async def _resolve_force_refresh_universe(
    pool: asyncpg.Pool, universe_cfg: Any,
) -> list[str]:
    """Materialise the force_refresh universe BEFORE chunking.

    Mirrors the resolution `_handle_daily_bars_explicit` does internally
    but lifts it to the stage so the chunker can slice deterministically.
    Returns a sorted ticker list. Supports the same shapes the underlying
    handler does (``"active"`` / explicit list / CSV string).
    """
    if isinstance(universe_cfg, list):
        return [str(s).upper() for s in universe_cfg]
    if isinstance(universe_cfg, str) and "," in universe_cfg:
        return [s.strip().upper() for s in universe_cfg.split(",") if s.strip()]
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
        return [r["ticker"] for r in rows]
    # `all_active` is not chunked here — that path is the discovery sweep
    # and has its own batched implementation in
    # `_handle_daily_bars_all_active`. Force-refresh against a discovery
    # sweep is not a documented mode.
    raise ValueError(
        f"force_refresh chunking: unsupported universe {universe_cfg!r} — "
        "expected 'active', explicit list, or CSV string"
    )


async def _force_refresh_chunked(
    pool: asyncpg.Pool,
    config: dict[str, Any],
    target_session: date,
) -> dict[str, Any]:
    """Run `handle_daily_bars` over the universe in ``FORCE_REFRESH_CHUNK_SIZE``
    ticker slices. Aggregate rows_upserted across chunks; per-chunk
    failure is logged + a ``CHUNK_FAILED`` structlog event is emitted +
    the run continues. The aggregate state is the input to the
    producer-self-validation that the caller still runs once.
    """
    from tpcore.ingestion.handlers import handle_daily_bars

    log = structlog.get_logger("scripts.ops.daily_bars_chunked")
    universe_cfg = config.get("universe", "active")
    symbols = await _resolve_force_refresh_universe(pool, universe_cfg)
    if not symbols:
        return {
            "rows_upserted": 0,
            "chunks_total": 0,
            "chunks_ok": 0,
            "chunks_failed": 0,
            "universe_size": 0,
            "target_session": target_session.isoformat(),
        }

    n_chunks = (len(symbols) + FORCE_REFRESH_CHUNK_SIZE - 1) // FORCE_REFRESH_CHUNK_SIZE
    log.info(
        "ops.daily_bars.force_refresh_chunked_start",
        universe_size=len(symbols),
        chunk_size=FORCE_REFRESH_CHUNK_SIZE,
        chunks_total=n_chunks,
        target_session=target_session.isoformat(),
    )

    total_rows = 0
    chunks_ok = 0
    chunks_failed: list[dict[str, Any]] = []
    # Carry forward every config key (lookback_days, end_offset_days,
    # feed, ...) except `universe` which the chunker overrides.
    base_config = {k: v for k, v in config.items() if k != "universe"}
    # Strip `force_refresh` from the per-chunk config — the chunker
    # has already taken responsibility for force-refresh semantics and
    # the underlying handler does not branch on it.
    base_config.pop("force_refresh", None)

    for i in range(0, len(symbols), FORCE_REFRESH_CHUNK_SIZE):
        chunk = symbols[i : i + FORCE_REFRESH_CHUNK_SIZE]
        chunk_idx = i // FORCE_REFRESH_CHUNK_SIZE + 1
        try:
            chunk_rows = await handle_daily_bars(
                pool, {**base_config, "universe": list(chunk)}
            )
            total_rows += int(chunk_rows or 0)
            chunks_ok += 1
            log.info(
                "ops.daily_bars.force_refresh_chunk_done",
                chunk_idx=chunk_idx,
                chunk_total=n_chunks,
                chunk_size=len(chunk),
                rows_upserted=int(chunk_rows or 0),
            )
        except Exception as exc:  # noqa: BLE001 — one chunk's failure
            # MUST NOT abort the whole run. Log, continue.
            chunks_failed.append({
                "chunk_idx": chunk_idx,
                "first_ticker": chunk[0],
                "last_ticker": chunk[-1],
                "error": str(exc)[:200],
            })
            log.error(
                "CHUNK_FAILED",
                chunk_idx=chunk_idx,
                chunk_total=n_chunks,
                chunk_size=len(chunk),
                first_ticker=chunk[0],
                last_ticker=chunk[-1],
                error=str(exc),
            )

    log.info(
        "ops.daily_bars.force_refresh_chunked_done",
        chunks_total=n_chunks,
        chunks_ok=chunks_ok,
        chunks_failed=len(chunks_failed),
        rows_upserted=total_rows,
    )

    return {
        "rows_upserted": total_rows,
        "chunks_total": n_chunks,
        "chunks_ok": chunks_ok,
        "chunks_failed": len(chunks_failed),
        "chunks_failed_detail": chunks_failed[:5],
        "universe_size": len(symbols),
        "target_session": target_session.isoformat(),
    }


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

    # 2026-05-21 chunking fix: the `--param force_refresh=true --param
    # universe=active` path issues ONE multi-symbol `handle_daily_bars`
    # call covering the full ~7,000-ticker active universe over the
    # `lookback_days` window. Two operator runs on 2026-05-21 (08:43 +
    # 09:19 UTC) EACH timed out at the stage's 3600s budget — the
    # whole-universe call provably exceeds that ceiling on a
    # rate-limited multi-symbol Alpaca pull. Mirrors the PR #222 Lab
    # final-holdout chunking pattern: a transport-layer split into
    # bounded ticker slices, each its own `handle_daily_bars` call,
    # aggregated at the stage level. NOT a Lab probe → no ledger
    # entry; idempotency is preserved by the upsert.
    if force_refresh:
        chunk_result = await _force_refresh_chunked(
            pool, config, target_session
        )
        rows = chunk_result["rows_upserted"]
    else:
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

    result: dict[str, Any] = {
        "rows_upserted": rows or 0,
        "universe": config.get("universe", "active"),
        "target_session": target_session.isoformat(),
        "coverage_tickers": latest_n,
    }
    if force_refresh:
        result["mode"] = "force_refresh_chunked"
        result["chunks_total"] = chunk_result["chunks_total"]
        result["chunks_ok"] = chunk_result["chunks_ok"]
        result["chunks_failed"] = chunk_result["chunks_failed"]
        if chunk_result["chunks_failed"]:
            result["chunks_failed_detail"] = chunk_result["chunks_failed_detail"]
    return result


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


# ────────────────────────────────────────────────────────────────────────
# Operator-on-demand verification + utility stages
# ────────────────────────────────────────────────────────────────────────
# Migrated 2026-05-20 from the legacy one-off ``scripts/*.py`` orphans
# (catalog at ``docs/superpowers/audits/2026-05-20-orphan-scripts-catalog.md``)
# as part of the zero-allowlist sweep. These stages run on-demand
# (``--stage <name>``) — NOT registered in ``OPS_UPDATE_STAGES`` and
# never invoked by the daily ``--update`` cadence.


async def _stage_compare_baselines(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Diff two trade-log CSVs and report whether they are equivalent.

    Regression-safety wrapper around ``tpcore.backtest.compare_trade_lists``
    used when refactoring an engine or migrating between strategy
    constructions (e.g. monthly-rebalance → rolling). Tolerances are
    absolute, not relative; defaults match the underlying API's defaults
    (1e-6 on pnl_pct, 1e-4 on prices).

    Operator-on-demand only. Migrated from
    ``scripts/compare_baselines.py`` (orphan-scripts sweep 2026-05-20).
    Pool arg unused — no DB writes.

    Config (``--param key=value``)::

        baseline   = path to known-good trade-log CSV (REQUIRED)
        candidate  = path to candidate trade-log CSV (REQUIRED)
        tol_pnl_pct = absolute tol on pnl_pct (default 1e-6)
        tol_price   = absolute tol on entry/exit prices (default 1e-4)
    """
    del pool  # no DB touch — pure file I/O against the equivalence API.
    from pathlib import Path as _Path

    from tpcore.backtest import compare_trade_lists
    from tpcore.backtest.equivalence import (
        DEFAULT_TOL_PNL_PCT,
        DEFAULT_TOL_PRICE,
    )
    from tpcore.backtest.search import read_trade_log_csv

    cfg = config or {}
    baseline_arg = cfg.get("baseline")
    candidate_arg = cfg.get("candidate")
    if not baseline_arg or not candidate_arg:
        raise SystemExit(
            "compare_baselines: both --param baseline=… and "
            "--param candidate=… are required"
        )
    baseline_path = _Path(str(baseline_arg))
    candidate_path = _Path(str(candidate_arg))
    if not baseline_path.exists():
        raise SystemExit(f"baseline file not found: {baseline_path}")
    if not candidate_path.exists():
        raise SystemExit(f"candidate file not found: {candidate_path}")
    tol_pnl_pct = float(cfg.get("tol_pnl_pct", DEFAULT_TOL_PNL_PCT))
    tol_price = float(cfg.get("tol_price", DEFAULT_TOL_PRICE))

    baseline = read_trade_log_csv(baseline_path)
    candidate = read_trade_log_csv(candidate_path)
    report = compare_trade_lists(
        baseline, candidate,
        tol_pnl_pct=tol_pnl_pct, tol_price=tol_price,
    )
    return {
        "equivalent": bool(report.equivalent),
        "baseline_path": str(baseline_path),
        "candidate_path": str(candidate_path),
        "baseline_trades": len(baseline),
        "candidate_trades": len(candidate),
        "tol_pnl_pct": tol_pnl_pct,
        "tol_price": tol_price,
        "summary": report.summary(),
    }


async def _stage_aar_pipeline_smoke(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """End-to-end synthetic verification of the AAR persistence pipeline.

    Proves ``tpcore.aar.writer.AARWriter`` writes ``AfterActionReport``
    rows to ``platform.aar_events`` against the live database without
    waiting for an engine to actually fill a real trade. Useful when the
    trading-side prerequisites aren't met (no live broker, paper engines
    haven't fired) but the operator still needs to verify the AAR
    plumbing works.

    Behaviour:

    1. Build a clearly-synthetic ``AfterActionReport`` with
       ``engine='synthetic_test'`` and a UUID ``trade_id`` (cannot
       collide with any real engine row, past or future).
    2. ``write_aar(aar)`` — first call must return ``True`` (new row).
    3. Read row back from ``platform.aar_events``; round-trip equality
       check against the original JSON.
    4. ``write_aar(aar)`` — second call with the SAME object; must
       return ``False`` (idempotent skip via the ``UNIQUE(engine,
       trade_id)`` + ``ON CONFLICT DO NOTHING`` constraint).
    5. Verify the row count is exactly 1 after the dup write.
    6. ``DELETE`` the synthetic row in a ``finally`` block — the
       production table never accumulates harness data.

    Operator-on-demand only. Migrated from
    ``scripts/test_aar_pipeline.py`` (orphan-scripts sweep 2026-05-20).
    """
    del config  # no tunables — the synthetic shape is fixed.
    import json as _json
    import uuid as _uuid
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from decimal import Decimal as _D

    from tpcore.aar.models import AfterActionReport, ExitReason
    from tpcore.aar.writer import AARWriter

    synthetic_engine = "synthetic_test"
    now = _dt.now(_UTC)
    aar = AfterActionReport(
        engine=synthetic_engine,
        trade_id=f"aar_pipeline_test_{_uuid.uuid4()}",
        ticker="ZZZZ",  # not a real ticker
        entry_ts=now,
        exit_ts=now,
        entry_price=_D("100.00"),
        exit_price=_D("101.50"),
        qty=_D("1"),
        confidence_at_entry=_D("0.75"),
        confidence_at_exit=_D("0.80"),
        sizing_pct_of_engine_equity=_D("0.05"),
        pnl_gross=_D("1.50"),
        pnl_net=_D("1.45"),
        fees=_D("0.05"),
        slippage_bps=_D("2.0"),
        regime_tags=["synthetic", "harness_test"],
        exit_reason=ExitReason.OTHER,
        rule_compliance=True,
        notes="Generated by ops.py --stage aar_pipeline_smoke — safe to ignore.",
    )

    writer = AARWriter(pool)
    deleted = 0
    try:
        # Count rows before (sanity baseline).
        async with pool.acquire() as conn:
            rows_before = await conn.fetchval(
                "SELECT COUNT(*) FROM platform.aar_events "
                "WHERE engine = $1 AND trade_id = $2",
                aar.engine, aar.trade_id,
            )
        if rows_before != 0:
            raise SystemExit(
                f"aar_pipeline_smoke: synthetic key already exists "
                f"(rows_before={rows_before}); cleanup leak from a "
                "previous run."
            )

        # 1. First write — expect INSERT True.
        wrote_first = await writer.write_aar(aar)
        if not wrote_first:
            raise SystemExit(
                "aar_pipeline_smoke: first write_aar returned False "
                "(expected True for a new row)"
            )

        # 2. Round-trip read.
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT engine, trade_id, ticker, aar_data "
                "FROM platform.aar_events "
                "WHERE engine = $1 AND trade_id = $2",
                aar.engine, aar.trade_id,
            )
        if row is None:
            raise SystemExit(
                "aar_pipeline_smoke: row not found after insert"
            )
        read_back = _json.loads(row["aar_data"])
        original = _json.loads(aar.model_dump_json())
        if read_back != original:
            raise SystemExit(
                "aar_pipeline_smoke: round-trip mismatch between "
                "written and read AAR JSON"
            )

        # 3. Second (duplicate) write — expect idempotent skip False.
        wrote_second = await writer.write_aar(aar)
        if wrote_second:
            raise SystemExit(
                "aar_pipeline_smoke: second write_aar returned True "
                "(expected False — idempotent skip)"
            )

        # 4. Final row count — exactly one synthetic row.
        async with pool.acquire() as conn:
            rows_after = await conn.fetchval(
                "SELECT COUNT(*) FROM platform.aar_events "
                "WHERE engine = $1 AND trade_id = $2",
                aar.engine, aar.trade_id,
            )
        if rows_after != 1:
            raise SystemExit(
                f"aar_pipeline_smoke: expected exactly 1 row after "
                f"idempotent re-write, found {rows_after}"
            )

        return {
            "verified": True,
            "rows_before": int(rows_before or 0),
            "rows_after": int(rows_after or 0),
            "synthetic_engine": synthetic_engine,
            "synthetic_trade_id": aar.trade_id,
        }
    finally:
        async with pool.acquire() as conn:
            cleanup_rows = await conn.fetch(
                "DELETE FROM platform.aar_events "
                "WHERE engine = $1 AND trade_id = $2 "
                "RETURNING id",
                aar.engine, aar.trade_id,
            )
        deleted = len(cleanup_rows)
        structlog.get_logger("scripts.ops").info(
            "ops.stage.aar_pipeline_smoke.cleanup",
            deleted=deleted,
            engine=aar.engine,
            trade_id=aar.trade_id,
        )


async def _stage_probe_sentinel_activation(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Offline activation-score distribution probe for the Sentinel
    graduated-composite path.

    Diagnoses whether the FAILED ``sentinel_bear_score`` Lab probe
    (``docs/lab/2026-05-21-sentinel_bear_score-FAILED-seed0.md``) failed
    because the gate is structurally dormant (composite < 0.45 floor
    across the OOS window) OR threshold-clipped (composite fires but
    band-to-execution wiring drops the trade). Read-only — no Lab
    spend, no n_trials increment, no dossier. Operator-on-demand
    (NOT in OPS_UPDATE_STAGES).

    Implementation lives in ``sentinel.activation_probe.run_probe`` for
    clean engine-module homing; this stage is a thin wrapper.

    Defect ref: ``SENTINEL-ACTIVATION-DORMANT-2026-05-21``.
    """
    del config  # accepted-but-unused per the stage contract
    from sentinel.activation_probe import run_probe

    payload = await run_probe(pool)

    # Floor print — operator-visibility for "did the gate fire on OOS?"
    # before they need to open the JSON sidecar (visible-progress rule).
    oos = payload.get("oos_window_stats", {})
    oos_pcts = oos.get("composite_percentiles", {})
    bucket = oos.get("per_bucket", {}).get("DORMANT", {})
    print("=== Sentinel Activation-Score Distribution Probe ===")
    print(f"OOS samples={oos.get('total_samples', 0)}  "
          f"p50={oos_pcts.get('p50', 0.0):.4f}  "
          f"p95={oos_pcts.get('p95', 0.0):.4f}")
    print(f"OOS DORMANT pct={bucket.get('pct', 0.0):.3%}  "
          f"max_dormant_streak={oos.get('max_contiguous_dormant_streak_days', 0)}d")
    print(f"VERDICT: {payload.get('verdict')} — "
          f"{payload.get('verdict_rationale')}")
    print(f"wrote: {payload.get('_sidecar_path')}")

    return {
        "verdict": payload.get("verdict"),
        "verdict_rationale": payload.get("verdict_rationale"),
        "oos_samples": oos.get("total_samples", 0),
        "oos_p95": oos_pcts.get("p95", 0.0),
        "oos_dormant_pct": bucket.get("pct", 0.0),
        "sidecar_path": payload.get("_sidecar_path"),
    }


async def _stage_kill_switch_smoke(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """End-to-end verification of the engine-scheduler kill-switch
    short-circuit against the live database.

    Flips ``platform.risk_state.kill_switch_active`` to ``true`` for the
    named engine, runs that engine's ``scheduler.run_once()`` against
    the production pool, and asserts the startup kill-switch check
    short-circuits (zero candidates scanned, zero trades submitted).
    Resets the kill switch to ``false`` in a ``finally`` block, even on
    failure, so the live engine is never left frozen.

    Operator-on-demand only. Migrated from
    ``scripts/test_kill_switch.py`` (orphan-scripts sweep 2026-05-20).

    Config (``--param key=value``)::

        engine = reversion | vector  (REQUIRED)
    """
    cfg = config or {}
    engine = str(cfg.get("engine", "")).strip().lower()
    if engine not in {"reversion", "vector"}:
        raise SystemExit(
            "kill_switch_smoke: --param engine=… required "
            "(choices: reversion, vector)"
        )

    from datetime import UTC as _UTC
    from datetime import date as _date
    from datetime import datetime as _dt
    from decimal import Decimal as _D

    from tpcore.risk.persistent_store import PostgresRiskStateStore

    async def _ensure_engine_row(p: asyncpg.Pool, eng: str, equity: _D) -> None:
        sql = """
            INSERT INTO platform.risk_state (
                engine, engine_equity, daily_pnl, weekly_pnl, open_positions,
                daily_reset_at, weekly_reset_at, kill_switch_active, updated_at
            )
            VALUES ($1, $2, 0, 0, 0, now() + interval '1 day',
                    now() + interval '7 days', false, now())
            ON CONFLICT (engine) DO NOTHING
        """
        async with p.acquire() as conn:
            await conn.execute(sql, eng, equity)

    async def _set_kill_switch(
        p: asyncpg.Pool, eng: str, *, active: bool, reason: str | None,
    ) -> None:
        sql = """
            UPDATE platform.risk_state
               SET kill_switch_active = $2,
                   kill_switch_reason = $3,
                   updated_at         = now()
             WHERE engine = $1
        """
        async with p.acquire() as conn:
            await conn.execute(sql, eng, active, reason)

    async def _run_engine(eng: str, as_of: _date) -> object:
        if eng == "reversion":
            from reversion.scheduler import ReversionScheduler
            return await ReversionScheduler().run_once(as_of=as_of)
        from vector.scheduler import VectorScheduler
        return await VectorScheduler().run_once(as_of=as_of)

    log = structlog.get_logger("scripts.ops")
    try:
        await _ensure_engine_row(pool, engine, equity=_D("10000"))
        await _set_kill_switch(
            pool, engine, active=True,
            reason="ops.py --stage kill_switch_smoke harness",
        )
        store = PostgresRiskStateStore(pool)
        before = await store.get(engine)
        if not (before is not None and before.kill_switch_active):
            raise SystemExit(
                f"kill_switch_smoke: failed to set kill switch on "
                f"{engine}; got {before}"
            )
        log.info("ops.stage.kill_switch_smoke.set_active", engine=engine)

        as_of = _dt.now(_UTC).date()
        summary = await _run_engine(engine, as_of)
        n_candidates = int(getattr(summary, "n_candidates", -1))
        n_submitted = int(getattr(summary, "n_submitted", -1))
        if n_candidates != 0:
            raise SystemExit(
                f"kill_switch_smoke: {engine} scanned {n_candidates} "
                f"candidates despite kill switch — startup check "
                "missing or broken"
            )
        if n_submitted != 0:
            raise SystemExit(
                f"kill_switch_smoke: {engine} submitted {n_submitted} "
                "trades despite kill switch"
            )
        log.info(
            "ops.stage.kill_switch_smoke.short_circuited",
            engine=engine,
            n_candidates=n_candidates, n_submitted=n_submitted,
        )
        return {
            "verified": True,
            "engine": engine,
            "n_candidates": n_candidates,
            "n_submitted": n_submitted,
        }
    finally:
        await _set_kill_switch(pool, engine, active=False, reason=None)
        log.info("ops.stage.kill_switch_smoke.reset", engine=engine)


async def _stage_ingest_tradier_csv(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Ingest ``tradier_bars_full.csv`` into ``platform.prices_daily``.

    Streams the wide Tradier CSV produced by
    ``--stage extract_tradier_full`` (22M rows, ~1GB on disk), filters
    to tickers that also appear in Alpaca's active-asset list
    (NYSE/NASDAQ), and inserts with ``source='tradier'`` using ``ON
    CONFLICT (ticker, date) DO NOTHING``. Existing Alpaca rows are
    never overwritten — Tradier fills gaps, primarily the pre-2020
    history Alpaca's IEX free tier doesn't cover.

    Idempotent — re-running after a crash or partial run is safe.
    The CSV is streamed so memory stays bounded regardless of file
    size. ``prices_daily.{open,high,low,close,adjusted_close}`` are
    ``NUMERIC(20,6)`` (14 integer digits) so any ``|value| >= 1e14`` or
    non-finite OHLC row is skipped (the wide export occasionally emits
    Inf or absurd values — ~50k bad rows skipped on the production
    load, ~0.23% of the source).

    Operator-on-demand only. Migrated 2026-05-20 from
    ``scripts/ingest_tradier_csv.py`` (orphan-scripts zero-allowlist
    sweep). Paired with ``--stage extract_tradier_full`` (also migrated
    in the same sweep).

    Config (``--param key=value``)::

        csv               = path to the Tradier wide-export CSV
                            (default: ``data/tradier_export/tradier_bars_full.csv``)
        no_alpaca_filter  = "true" → load every ticker in the CSV;
                            skip the Alpaca active-asset gate
                            (default: "false")
    """
    import csv as _csv
    import time as _time
    from datetime import date as _date
    from decimal import Decimal as _D
    from pathlib import Path as _Path

    import httpx as _httpx

    from tpcore.data.ingest_alpaca_bars import (
        _alpaca_broker_base,
        _alpaca_headers,
        fetch_active_us_equities,
    )

    cfg = config or {}
    csv_path = _Path(str(cfg.get("csv", "data/tradier_export/tradier_bars_full.csv")))
    no_alpaca_filter = str(cfg.get("no_alpaca_filter", "")).lower() == "true"

    if not csv_path.exists():
        raise SystemExit(
            f"ingest_tradier_csv: CSV not found: {csv_path}"
        )

    copy_batch = 5_000
    numeric_max = _D("1e14")
    insert_sql = """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'tradier')
        ON CONFLICT (ticker, date) DO NOTHING
    """

    log = structlog.get_logger("scripts.ops")

    def _row_to_tuple(row: list[str]) -> tuple | None:
        if len(row) < 7:
            return None
        ticker, date_str, o, h, low, c, v = row[:7]
        if not ticker or not date_str:
            return None
        try:
            d = _date.fromisoformat(date_str)
            open_ = _D(o) if o else None
            high = _D(h) if h else None
            low_ = _D(low) if low else None
            close = _D(c) if c else None
            volume = int(v) if v else 0
        except (ValueError, ArithmeticError):
            return None
        if open_ is None or high is None or low_ is None or close is None:
            return None
        for x in (open_, high, low_, close):
            if not x.is_finite() or abs(x) >= numeric_max:
                return None
        return (
            ticker, d, open_, high, low_, close, volume,
            close,  # adjusted_close — Tradier history is split-adjusted
            False,  # delisted: unknown from CSV; assume active
            None,   # delisting_date
        )

    # Resolve the Alpaca-active filter unless explicitly disabled.
    allowed: set[str] | None
    if no_alpaca_filter:
        log.info("ops.stage.ingest_tradier_csv.alpaca_filter_disabled")
        allowed = None
    else:
        log.info("ops.stage.ingest_tradier_csv.alpaca_filter.fetching")
        headers = _alpaca_headers()
        async with _httpx.AsyncClient(
            headers=headers, base_url=_alpaca_broker_base(), timeout=60.0,
        ) as client:
            assets = await fetch_active_us_equities(client)
        allowed = {a["symbol"] for a in assets}
        log.info(
            "ops.stage.ingest_tradier_csv.alpaca_filter.fetched",
            count=len(allowed),
        )

    counters: dict[str, int] = {
        "rows_read": 0,
        "rows_skipped_filter": 0,
        "rows_skipped_malformed": 0,
        "rows_attempted": 0,
        "tickers_seen": 0,
    }
    seen_tickers: set[str] = set()
    batch: list[tuple] = []
    started = _time.monotonic()

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = _csv.reader(fh)
        try:
            next(reader)  # header
        except StopIteration:
            return {**counters, "csv_path": str(csv_path)}

        async with pool.acquire() as conn:
            for row in reader:
                counters["rows_read"] += 1
                if allowed is not None and row and row[0] not in allowed:
                    counters["rows_skipped_filter"] += 1
                    continue
                tup = _row_to_tuple(row)
                if tup is None:
                    counters["rows_skipped_malformed"] += 1
                    continue
                seen_tickers.add(tup[0])
                batch.append(tup)
                if len(batch) >= copy_batch:
                    await conn.executemany(insert_sql, batch)
                    counters["rows_attempted"] += len(batch)
                    batch.clear()
                    if counters["rows_attempted"] % (copy_batch * 20) == 0:
                        elapsed = _time.monotonic() - started
                        rate = counters["rows_attempted"] / max(elapsed, 1e-3)
                        log.info(
                            "ops.stage.ingest_tradier_csv.progress",
                            rows_attempted=counters["rows_attempted"],
                            rows_read=counters["rows_read"],
                            tickers=len(seen_tickers),
                            rate_per_sec=round(rate, 0),
                        )

            if batch:
                await conn.executemany(insert_sql, batch)
                counters["rows_attempted"] += len(batch)
                batch.clear()

    counters["tickers_seen"] = len(seen_tickers)
    return {**counters, "csv_path": str(csv_path)}


async def _stage_extract_tradier_full(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Wide-universe Tradier daily-bar extractor → flat CSV (no DB writes).

    Walks the full Tradier-tradable US equity + ETF universe via
    ``/v1/markets/lookup`` and pulls daily history from 2000-01-01
    through the configured ``end_date`` for each name, streaming bars
    into a single CSV. Operator-on-demand re-extraction tool; pairs
    with the downstream ``ingest_tradier_csv`` loader.

    The stage does NOT touch Postgres. It produces a flat file the
    operator can audit, hash, and ingest later. Behaviour mirrors the
    legacy script:

    * One symbol-enumeration call → ``<out_dir>/tradier_symbols_full.csv``
      (saved on first run, reused on resume so the universe is stable
      across restarts).
    * Bars stream into ``<out_dir>/tradier_bars_full.csv`` as each
      symbol completes — a crash mid-run leaves a partial-but-valid
      CSV.
    * Resumable: scans the existing bars CSV for distinct tickers and
      skips those.
    * Rate limit: 0.5s sleep between bars requests (~120 req/min
      ceiling), 5s backoff on HTTP 429.

    Operator-on-demand only. Migrated from
    ``scripts/extract_tradier_full.py`` (orphan-scripts sweep
    2026-05-20). Pool arg ignored — no DB writes.

    Config (``--param key=value``)::

        out_dir          = output directory
                           (default: ``data/tradier_export``)
        max_symbols      = stop after N new symbols (smoke-test knob;
                           default: no limit)
        refresh_symbols  = "true" → re-enumerate universe even if cache
                           exists (default: "false")
        end_date         = ISO date for bars end window
                           (default: today UTC)

    Token env var: ``TRADIER_PRODUCTION_TOKEN`` (preferred) or
    ``TRADIER_TOKEN`` (alias).
    """
    del pool  # CSV-only — no DB.
    import csv as _csv
    from datetime import UTC as _UTC
    from datetime import date as _date
    from datetime import datetime as _dt
    from pathlib import Path as _Path
    from typing import Any as _Any

    import httpx as _httpx

    cfg = config or {}
    out_dir_str = str(cfg.get("out_dir", "data/tradier_export"))
    max_symbols_raw = cfg.get("max_symbols")
    max_symbols = int(max_symbols_raw) if max_symbols_raw not in (None, "") else None
    refresh_symbols = str(cfg.get("refresh_symbols", "")).lower() == "true"
    end_raw = cfg.get("end_date")
    end_date: _date = (
        _date.fromisoformat(str(end_raw))
        if end_raw
        else _dt.now(_UTC).date()
    )

    token = (
        os.environ.get("TRADIER_PRODUCTION_TOKEN")
        or os.environ.get("TRADIER_TOKEN")
    )
    if not token:
        raise SystemExit(
            "extract_tradier_full: TRADIER_PRODUCTION_TOKEN (or "
            "TRADIER_TOKEN) not set in environment."
        )

    tradier_base = "https://api.tradier.com"
    inter_request_sleep_s = 0.5
    rate_limit_backoff_s = 5.0
    exchanges = "N,Q,A"
    symbol_types = "stock,etf"
    bars_start = _date(2000, 1, 1)
    symbols_csv_name = "tradier_symbols_full.csv"
    bars_csv_name = "tradier_bars_full.csv"
    symbols_columns = ["symbol", "exchange", "type", "description"]
    bars_columns = ["ticker", "date", "open", "high", "low", "close", "volume"]

    log = structlog.get_logger("scripts.ops")

    def _ensure_list(x: _Any) -> list:
        if x is None:
            return []
        return x if isinstance(x, list) else [x]

    async def _get(
        client: _httpx.AsyncClient,
        path: str,
        params: dict[str, _Any] | None = None,
    ) -> dict | None:
        try:
            resp = await client.get(path, params=params or {})
        except (_httpx.RequestError, _httpx.HTTPError) as exc:
            log.warning(
                "tradier.network_error", path=path,
                params=params, error=str(exc),
            )
            return None
        if resp.status_code == 429:
            log.warning(
                "tradier.rate_limited", path=path,
                sleep=rate_limit_backoff_s,
            )
            await asyncio.sleep(rate_limit_backoff_s)
            try:
                resp = await client.get(path, params=params or {})
            except Exception as exc:  # noqa: BLE001 - retry-then-skip is the only sane move
                log.warning(
                    "tradier.retry_failed", path=path, error=str(exc),
                )
                return None
        if resp.status_code != 200:
            log.warning(
                "tradier.http_error", path=path,
                status=resp.status_code, body=resp.text[:200],
            )
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning(
                "tradier.non_json_response", path=path,
                body=resp.text[:200],
            )
            return None

    out_dir = _Path(out_dir_str)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols_csv = out_dir / symbols_csv_name
    bars_csv = out_dir / bars_csv_name

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with _httpx.AsyncClient(
        base_url=tradier_base, headers=headers, timeout=30.0,
    ) as client:
        if refresh_symbols or not symbols_csv.exists():
            log.info("tradier.symbols.fetch_start")
            body = await _get(
                client, "/v1/markets/lookup",
                {"exchanges": exchanges, "types": symbol_types},
            )
            universe_raw = (
                _ensure_list((body or {}).get("securities", {}).get("security"))
                if body else []
            )
            if not universe_raw:
                raise SystemExit(
                    "extract_tradier_full: symbol enumeration returned empty"
                )
            with symbols_csv.open("w", newline="", encoding="utf-8") as fh:
                w = _csv.writer(fh, quoting=_csv.QUOTE_MINIMAL)
                w.writerow(symbols_columns)
                for s in universe_raw:
                    w.writerow([
                        s.get("symbol", ""),
                        s.get("exchange", ""),
                        s.get("type", ""),
                        (s.get("description") or "").replace("\n", " "),
                    ])
            log.info(
                "tradier.symbols.fetched", count=len(universe_raw),
                path=str(symbols_csv),
            )
        else:
            log.info("tradier.symbols.cached", path=str(symbols_csv))

        symbols: list[str] = []
        with symbols_csv.open(newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                sym = (row.get("symbol") or "").strip()
                if sym:
                    symbols.append(sym)
        if not symbols:
            raise SystemExit(
                f"extract_tradier_full: symbols CSV is empty: {symbols_csv}"
            )

        done: set[str] = set()
        if bars_csv.exists():
            with bars_csv.open(newline="", encoding="utf-8") as fh:
                r = _csv.reader(fh)
                try:
                    next(r)  # header
                except StopIteration:
                    pass
                else:
                    for row in r:
                        if row:
                            done.add(row[0])

        work = [s for s in symbols if s not in done]
        if max_symbols is not None:
            work = work[:max_symbols]

        log.info(
            "tradier.extract.start",
            total=len(symbols),
            already_done=len(done),
            to_process=len(work),
            max_symbols=max_symbols,
        )

        bars_csv.parent.mkdir(parents=True, exist_ok=True)
        write_header = not bars_csv.exists() or bars_csv.stat().st_size == 0
        summary = {
            "tickers_fetched": 0, "tickers_no_data": 0,
            "tickers_failed": 0, "rows_appended": 0,
        }
        with bars_csv.open("a", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh, quoting=_csv.QUOTE_MINIMAL)
            if write_header:
                w.writerow(bars_columns)
                fh.flush()

            for i, symbol in enumerate(work, 1):
                try:
                    body = await _get(
                        client, "/v1/markets/history",
                        {
                            "symbol": symbol,
                            "interval": "daily",
                            "start": bars_start.isoformat(),
                            "end": end_date.isoformat(),
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - per-symbol isolation
                    log.warning(
                        "tradier.extract.symbol_exception",
                        symbol=symbol, error=str(exc),
                    )
                    summary["tickers_failed"] += 1
                    await asyncio.sleep(inter_request_sleep_s)
                    continue

                days = (
                    _ensure_list((body or {}).get("history", {}).get("day"))
                    if body else []
                )
                if not days:
                    summary["tickers_no_data"] += 1
                    log.info(
                        "tradier.extract.no_data",
                        progress=f"{i}/{len(work)}", symbol=symbol,
                    )
                    await asyncio.sleep(inter_request_sleep_s)
                    continue

                for d in days:
                    w.writerow([
                        symbol, d.get("date"), d.get("open"),
                        d.get("high"), d.get("low"), d.get("close"),
                        d.get("volume"),
                    ])
                fh.flush()
                summary["tickers_fetched"] += 1
                summary["rows_appended"] += len(days)

                if i % 50 == 0 or i == len(work):
                    size_mb = bars_csv.stat().st_size / 1_000_000
                    log.info(
                        "tradier.extract.progress",
                        progress=f"{i}/{len(work)}",
                        last_symbol=symbol,
                        rows_so_far=summary["rows_appended"],
                        file_mb=round(size_mb, 2),
                    )

                await asyncio.sleep(inter_request_sleep_s)

    return {
        "out_dir": str(out_dir),
        "bars_csv": str(bars_csv),
        "symbols_total": len(symbols),
        "tickers_already_done": len(done),
        "tickers_processed": len(work),
        **summary,
    }


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


async def _stage_dedupe_monotone(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """D7 — clean rogue duplicate rows on monotone-watched tables.

    Per spec §4 ANSWERED Q3: dedupe rule is **latest event_date wins**,
    tiebreaker on latest ``recorded_at``. The earnings_events table is
    keyed on ``(ticker, event_date, event_type)`` and sec_insider on the
    ``ticker, filing_date, insider_name, transaction_type, shares``
    uniqueness key — under those constraints rogue rows are only
    possible if the constraint was ever dropped or bulk-loaded around.
    The stage acts as a safety net: scans the dedupe-key groups, keeps
    the row with the latest ``recorded_at`` per group, deletes the rest.

    ``cfg`` knobs (all optional):
        * ``table``: if provided, restrict to one of
          ``platform.earnings_events`` or
          ``platform.sec_insider_transactions``. Default is BOTH.
        * ``dry_run``: bool — when True, count rogues but emit no
          DELETE. Default False.

    Returns ``{table: {found: int, deleted: int}}`` — telemetry for the
    cascade. ``found == 0`` is the steady-state expectation.

    The stage is idempotent and side-effect-free when no rogues exist.
    It is wired into ``_auto_cascade_validation_failures`` for D7 and
    can also be invoked standalone via the ops CLI for operator-driven
    audits.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    only_table = cfg.get("table")
    dry_run = bool(cfg.get("dry_run", False))

    # Per-table dedupe specs:
    #   key_cols       — the equality columns defining "same row"
    #   table          — qualified table name
    #   tiebreaker     — recorded_at (latest wins; spec §4 Q3)
    specs: list[dict[str, Any]] = [
        {
            "name": "earnings_events",
            "table": "platform.earnings_events",
            "key_cols": ["ticker", "event_date", "event_type"],
        },
        {
            "name": "sec_insider_transactions",
            "table": "platform.sec_insider_transactions",
            "key_cols": [
                "ticker", "filing_date", "insider_name",
                "transaction_type", "shares",
            ],
        },
    ]

    out: dict[str, dict[str, int]] = {}

    for spec in specs:
        if only_table is not None and only_table != spec["table"]:
            continue
        key_csv = ", ".join(spec["key_cols"])
        # Count rogues: rows beyond the per-group latest recorded_at.
        count_sql = f"""
            WITH ranked AS (
                SELECT ctid,
                       ROW_NUMBER() OVER (
                           PARTITION BY {key_csv}
                           ORDER BY recorded_at DESC, ctid DESC
                       ) AS rn
                FROM {spec["table"]}
            )
            SELECT COUNT(*) AS rogues FROM ranked WHERE rn > 1
        """
        delete_sql = f"""
            WITH ranked AS (
                SELECT ctid,
                       ROW_NUMBER() OVER (
                           PARTITION BY {key_csv}
                           ORDER BY recorded_at DESC, ctid DESC
                       ) AS rn
                FROM {spec["table"]}
            )
            DELETE FROM {spec["table"]} t
            USING ranked r
            WHERE t.ctid = r.ctid AND r.rn > 1
        """
        async with pool.acquire() as conn:
            try:
                rogues = await conn.fetchval(count_sql) or 0
            except Exception as exc:  # noqa: BLE001 — never crash the cycle
                log.warning(
                    "ops.stage.dedupe_monotone.count_failed",
                    table=spec["table"], error=str(exc),
                )
                out[spec["name"]] = {"found": -1, "deleted": 0}
                continue
            rogues = int(rogues)
            deleted = 0
            if rogues > 0 and not dry_run:
                try:
                    res = await conn.execute(delete_sql)
                    # asyncpg returns "DELETE <N>"; parse the trailing N.
                    try:
                        deleted = int(res.split()[-1])
                    except (ValueError, IndexError):
                        deleted = rogues
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "ops.stage.dedupe_monotone.delete_failed",
                        table=spec["table"], error=str(exc),
                    )
                    out[spec["name"]] = {"found": rogues, "deleted": 0}
                    continue
            out[spec["name"]] = {"found": rogues, "deleted": deleted}
            log.info(
                "ops.stage.dedupe_monotone.scanned",
                table=spec["table"], found=rogues, deleted=deleted,
                dry_run=dry_run,
            )

    return out


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


async def _stage_seed_monotone_snapshots(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot bulk-seed the per-ticker monotone-baseline snapshot tables.

    Both ``platform.sec_insider_row_counts_snapshot`` and
    ``platform.earnings_events_count_snapshot`` hold a per-ticker rowcount
    baseline that the corresponding validation checks (``sec_insider_monotone``,
    ``earnings_events_monotone``) gate against. The checks themselves seed the
    baseline on first run by UPSERTing one row per ticker in a Python loop —
    correct, but at ~1000-1300 tickers × pooler latency (Manila → Supabase) the
    loop routinely exceeds the 2-min Supavisor statement_timeout, leaving the
    baseline empty and the check stuck in "exception" red.

    This stage performs the seed via a single set-based ``INSERT ... SELECT``
    bulk statement per table — one server round-trip, sub-second. After this
    runs, the next validation cycle finds an existing baseline and proceeds in
    a normal compare-against-prior path.

    Idempotent: ``ON CONFLICT (ticker) DO UPDATE`` makes a re-run a refresh of
    the snapshot to the current live counts (the same semantic the checks
    apply on a clean PASS). Safe to call any time.

    Use:
        python scripts/ops.py --stage seed_monotone_snapshots

    Returns per-table row counts written for the audit log.
    """
    del cfg
    log = structlog.get_logger("scripts.ops")
    async with pool.acquire() as conn:
        sec_rows = await conn.execute(
            """
            INSERT INTO platform.sec_insider_row_counts_snapshot
                (ticker, rowcount, snapshot_at)
            SELECT ticker, COUNT(*), now()
            FROM platform.sec_insider_transactions
            GROUP BY ticker
            ON CONFLICT (ticker) DO UPDATE
              SET rowcount = EXCLUDED.rowcount,
                  snapshot_at = EXCLUDED.snapshot_at
            """
        )
        earnings_rows = await conn.execute(
            """
            INSERT INTO platform.earnings_events_count_snapshot
                (ticker, beat_count, snapshot_at)
            SELECT ticker, COUNT(*), now()
            FROM platform.earnings_events
            WHERE event_type IN ('EARNINGS_BEAT', 'EARNINGS_NO_BEAT')
            GROUP BY ticker
            ON CONFLICT (ticker) DO UPDATE
              SET beat_count = EXCLUDED.beat_count,
                  snapshot_at = EXCLUDED.snapshot_at
            """
        )
        sec_count = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.sec_insider_row_counts_snapshot"
        )
        earnings_count = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.earnings_events_count_snapshot"
        )
    log.info(
        "ops.stage.seed_monotone_snapshots.done",
        sec_insider_status=sec_rows,
        earnings_events_status=earnings_rows,
        sec_insider_rows=int(sec_count or 0),
        earnings_events_rows=int(earnings_count or 0),
    )
    return {
        "sec_insider_row_counts_snapshot_rows": int(sec_count or 0),
        "earnings_events_count_snapshot_rows": int(earnings_count or 0),
    }


async def _stage_historical_delisted_universe(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot survivorship backfill — populate ``platform.prices_daily``
    with complete historical bars for KNOWN US-equity delistings via FMP.

    The 2026-05-22 corpus-fitness audit (PR #281) found 18 of 20 known
    historical delistings completely absent from ``prices_daily``,
    structurally biasing every backtest credibility score. This stage is
    THE survivorship-gap closer: it enumerates the delisted universe from
    five sources (existing corpus markers, historical-corpus orphans,
    operator-curated KNOWN_DELISTINGS manifest, validation-suite fixtures,
    and best-effort probes of FMP ``/symbol-change`` + ``/delisted-companies``
    when those endpoints are accessible at the operator's tier), then per-
    ticker GETs FMP's ``/historical-price-eod/full`` over each ticker's
    trading life and upserts every bar with ``delisted=true`` +
    ``delisting_date`` set to the ticker's final bar.

    Resumable via ``application_log`` events
    (``SURVIVORSHIP_BACKFILL_TICKER_DONE``) — a crash mid-run keeps
    completed work; the next invocation skips already-done tickers.

    Operator-on-demand only (NOT in ``OPS_UPDATE_STAGES``). Run after
    PR merge to populate the corpus, then re-run as needed when the
    universe-enumeration sources surface new delistings.

    Optional ``--param`` knobs:
        * ``start_date=YYYY-MM-DD`` — override the 2010-01-01 default
        * ``end_date=YYYY-MM-DD`` — override today's date
        * ``resume=false`` — re-process every enumerated ticker (default true)
        * ``limit=N`` — process at most N tickers (handy for live spot-checks)
        * ``probe_fmp=false`` — skip the FMP enumeration probes (testing)

    Use:
        DATABASE_URL=… FMP_API_KEY=… .venv/bin/python scripts/ops.py \\
            --stage historical_delisted_universe
    """
    from tpcore.data.survivorship_backfill import (
        backfill_universe,
        enumerate_delisted_universe,
    )
    from tpcore.logging.db_handler import DBLogHandler

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    probe_fmp = bool(cfg.get("probe_fmp", True))
    resume = bool(cfg.get("resume", True))
    limit = int(cfg.get("limit", 0)) or None
    start = (
        date.fromisoformat(str(cfg["start_date"]))
        if cfg.get("start_date") else date(2010, 1, 1)
    )
    end = (
        date.fromisoformat(str(cfg["end_date"]))
        if cfg.get("end_date") else None
    )

    candidates = await enumerate_delisted_universe(pool, probe_fmp=probe_fmp)
    log.info(
        "ops.stage.historical_delisted_universe.enumerated",
        total=len(candidates),
        sources={
            src: sum(1 for c in candidates if c.source == src)
            for src in {c.source for c in candidates}
        },
    )
    if len(candidates) < 500:
        # Per operator instructions: "if the universe enumeration finds
        # <500 delisted tickers, that's suspiciously low — most US-equity
        # historical universes have ~3000-5000 delistings/M&A events over
        # 15 years." Warn but don't crash — corpus may be sparse early on.
        log.warning(
            "ops.stage.historical_delisted_universe.universe_below_floor",
            count=len(candidates),
            note="expected ~3000-5000 over a 15-year window — re-check enumeration sources",
        )
    universe = [c.ticker for c in candidates]
    if limit:
        universe = universe[:limit]
        log.info(
            "ops.stage.historical_delisted_universe.limited", limit=limit,
        )

    # We need a DBLogHandler for the per-ticker progress events. Re-use
    # the ops engine + a fresh run_id so the audit trail is self-contained.
    db_log = DBLogHandler(
        pool, engine=ENGINE_NAME, run_id=uuid.uuid4(),
    )
    result = await backfill_universe(
        pool, db_log, universe,
        start=start, end=end, resume=resume,
    )
    log.info("ops.stage.historical_delisted_universe.done", **result)
    return {
        "universe_enumerated": len(candidates),
        "enumeration_sources": sorted({c.source for c in candidates}),
        **result,
    }


async def _stage_daily_delisted_universe_check(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Nightly newly-delisted detection — silent-disappearance catcher.

    Queries ``platform.prices_daily`` for tickers that had a bar 5+
    trading days ago, were T1/T2 on the most recent
    ``platform.liquidity_tiers`` snapshot, and have NO bar in the past
    5 sessions. For each candidate, probes FMP one more time over the
    recent window; if FMP also has no bar, marks the ticker
    ``delisted=true`` with today's date (the silent-disappearance
    delisting). If FMP DOES have bars, this is a vendor gap that the
    ``daily_bars --param repair_coverage=true`` path is the canonical
    recovery for — we log and leave it alone (no marking).

    Operator-on-demand (NOT in OPS_UPDATE_STAGES today — the operator
    can promote it to the daily cadence once the structural backfill is
    stable). Idempotent.

    Use:
        DATABASE_URL=… FMP_API_KEY=… .venv/bin/python scripts/ops.py \\
            --stage daily_delisted_universe_check
    """
    import httpx

    from tpcore.data.ingest_fmp_bars import fetch_daily_bars_multi as fmp_fetch
    from tpcore.data.survivorship_backfill import (
        detect_newly_delisted,
        mark_delisted,
    )

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    candidates = await detect_newly_delisted(pool)
    today = datetime.now(UTC).date()
    if not candidates:
        log.info("ops.stage.daily_delisted_universe_check.no_candidates")
        return {"candidates": 0, "marked_delisted": 0, "vendor_gap": 0}

    marked = 0
    vendor_gap = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Probe a 14-day window — wide enough to clear any single-week
        # vendor blip but tight enough that an FMP-empty result is
        # decisive evidence of delisting.
        probe_start = today - timedelta(days=14)
        try:
            by_symbol = await fmp_fetch(client, candidates, probe_start, today)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.error(
                "ops.stage.daily_delisted_universe_check.fmp_failed",
                error=str(exc)[:200],
            )
            raise
    for ticker in candidates:
        bars = by_symbol.get(ticker, [])
        if bars:
            vendor_gap += 1
            log.info(
                "ops.stage.daily_delisted_universe_check.vendor_gap",
                ticker=ticker,
                note="FMP has bars; canonical recovery: daily_bars repair_coverage",
            )
            continue
        # Newly delisted — final bar date is whatever the corpus shows
        # as the ticker's last-seen session (NOT today; the ticker
        # already stopped trading).
        async with pool.acquire() as conn:
            final_date = await conn.fetchval(
                "SELECT MAX(date) FROM platform.prices_daily WHERE ticker = $1",
                ticker,
            )
        delist_on = final_date or today
        if await mark_delisted(pool, ticker, delist_on):
            marked += 1
    out = {
        "candidates": len(candidates),
        "marked_delisted": marked,
        "vendor_gap": vendor_gap,
    }
    log.info("ops.stage.daily_delisted_universe_check.done", **out)
    return out


async def _stage_historical_earnings_events_t1_t2(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot T1+T2 earnings-events backfill — unblock Vector engine.

    The 2026-05-13 Vector parameter-search produced ZERO trades on
    every candidate because ``platform.earnings_events`` had no
    overlap with the T1+T2 universe. MASTER_PLAN.md §4.3:

        "Re-enabling Vector is gated on a one-time data-ingestion
        backfill (catalyst events for T1+T2 tickers from FMP
        earnings-history endpoint), not on any strategy work."

    This stage is that backfill. It enumerates the T1+T2 stock-class
    universe (~1500 tickers per the
    ``platform.ticker_classifications.asset_class='stock'`` filter,
    excluding the 586 ETFs / 301 SPACs / 125 funds that have no
    earnings to beat), then per-ticker GETs FMP's
    ``/stable/earnings?symbol=<t>`` endpoint, classifies each
    historical event via the shared
    ``scripts.backfill_earnings_events._classify_earnings``, and
    upserts the BEAT + NO_BEAT rows into ``platform.earnings_events``.

    Mirrors the survivorship-backfill operator-shape:

    * Resumable via ``application_log`` events
      (``EARNINGS_BACKFILL_TICKER_DONE``) — a crash mid-run keeps
      completed work; the next invocation skips already-done tickers.
    * Idempotent — ``ON CONFLICT DO NOTHING`` on the existing PK.
    * Operator-on-demand only (NOT in ``OPS_UPDATE_STAGES``). Run after
      PR merge to populate the corpus; the weekly
      ``earnings_refresh`` stage keeps the tail fresh.

    Distinction from the existing ``earnings_refresh`` stage:
    ``earnings_refresh`` (a) carries a 6-day skip guard that no-ops
    same-week re-invocations, (b) calls ``backfill_amain`` directly
    without per-ticker progress emission so a mid-run crash loses all
    completed work, and (c) has no audit-trail event surface. This
    stage is the resumable, audit-emitting one-shot the survivorship
    PRs (#283 / #288) established as the canonical heavy-lane backfill
    shape.

    Optional ``--param`` knobs:
        * ``start_date=YYYY-MM-DD`` — override the 2018-01-01 default
          (FMP earnings-history coverage start).
        * ``end_date=YYYY-MM-DD`` — override today's date.
        * ``resume=false`` — re-process every enumerated ticker
          (default true).
        * ``limit=N`` — process at most N tickers (handy for spot-checks).

    Use:
        DATABASE_URL=… FMP_API_KEY=… .venv/bin/python scripts/ops.py \\
            --stage historical_earnings_events_t1_t2
    """
    from tpcore.data.earnings_events_backfill import (
        backfill_universe,
        enumerate_t1_t2_stock_universe,
    )
    from tpcore.logging.db_handler import DBLogHandler

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    resume = bool(cfg.get("resume", True))
    limit = int(cfg.get("limit", 0)) or None
    start = (
        date.fromisoformat(str(cfg["start_date"]))
        if cfg.get("start_date") else date(2018, 1, 1)
    )
    end = (
        date.fromisoformat(str(cfg["end_date"]))
        if cfg.get("end_date") else None
    )

    universe = await enumerate_t1_t2_stock_universe(pool)
    log.info(
        "ops.stage.historical_earnings_events_t1_t2.enumerated",
        total=len(universe),
    )
    # T1+T2 stock-class typically ≈ 1500 tickers. Below 500 is a
    # universe-enumeration regression worth flagging (e.g.,
    # liquidity_tiers empty, or asset_class predicate inverted).
    if len(universe) < 500:
        log.warning(
            "ops.stage.historical_earnings_events_t1_t2.universe_below_floor",
            count=len(universe),
            note="expected ~1500 stock-class T1+T2 tickers — check enumeration",
        )
    if limit:
        universe = universe[:limit]
        log.info(
            "ops.stage.historical_earnings_events_t1_t2.limited", limit=limit,
        )

    db_log = DBLogHandler(
        pool, engine=ENGINE_NAME, run_id=uuid.uuid4(),
    )
    result = await backfill_universe(
        pool, db_log, universe,
        start=start, end=end, resume=resume,
    )
    log.info("ops.stage.historical_earnings_events_t1_t2.done", **result)
    return result


async def _stage_historical_fundamentals_quarterly(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot historical-quarter backfill for fundamentals_quarterly.

    The 2026-05-22 full-spectrum data-feed hardening audit
    (``docs/audits/2026-05-22-full-spectrum-data-feed-hardening.md``)
    flagged this as the largest single corpus integrity red on ``main``:

        fundamentals_quarterly_completeness — 285 of 1090 active T1/T2
        stock tickers failing.

    The canonical ``fundamentals_refresh`` stage cannot heal these gaps:
    its ``backfill_all`` skips tickers whose newest ``recorded_at`` is
    fresh (<24h), and the FMP adapter's default 40-quarter limit
    bounds the historical depth.

    This stage:

    * Reads target tickers from ``compute_fundamentals_repair_targets``
      (the SAME function the D6 validation cascade consults). Detector
      and healer cannot disagree.
    * Per-ticker FMP fetch via ``FMPFundamentalsAdapter`` (the existing
      adapter — no schema change). Resumable via the
      ``FUNDAMENTALS_BACKFILL_TICKER_DONE`` event in
      ``platform.application_log``.
    * Idempotent upsert via ``FundamentalsCache._upsert_payload`` —
      same physical-truth gate path as the daily refresh.

    Operator-on-demand only (NOT in ``OPS_UPDATE_STAGES``). Run after
    PR merge to populate the missing quarters; the weekly
    ``fundamentals_refresh`` stage keeps the tail fresh post-backfill.

    Optional ``--param`` knobs:
        * ``resume=false`` — re-process every gap-ticker (default true).
        * ``limit=N`` — process at most N tickers (handy for spot-checks).
        * ``end_date=YYYY-MM-DD`` — point-in-time cutoff (default today).
        * ``tickers=AAPL,MSFT,…`` — explicit override of the gap-target
          list (handy when the operator wants to pre-empt the next
          completeness probe).

    Use:
        DATABASE_URL=… FMP_API_KEY=… .venv/bin/python scripts/ops.py \\
            --stage historical_fundamentals_quarterly
    """
    from tpcore.data.fundamentals_backfill import (
        backfill_universe,
        enumerate_gap_tickers,
    )
    from tpcore.logging.db_handler import DBLogHandler

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    resume = bool(cfg.get("resume", True))
    limit = int(cfg.get("limit", 0)) or None
    end = (
        date.fromisoformat(str(cfg["end_date"]))
        if cfg.get("end_date") else None
    )
    explicit_tickers = cfg.get("tickers")
    if explicit_tickers:
        if isinstance(explicit_tickers, str):
            universe = [
                t.strip().upper()
                for t in explicit_tickers.split(",") if t.strip()
            ]
        else:
            universe = [str(t).upper() for t in explicit_tickers]
        log.info(
            "ops.stage.historical_fundamentals_quarterly.explicit_universe",
            count=len(universe),
        )
    else:
        universe = await enumerate_gap_tickers(pool)
        log.info(
            "ops.stage.historical_fundamentals_quarterly.gap_targets",
            count=len(universe),
        )
        if not universe:
            log.info(
                "ops.stage.historical_fundamentals_quarterly.nothing_to_repair",
                note="compute_fundamentals_repair_targets returned []",
            )
            return {
                "universe_size": 0,
                "resumed_skipped": 0,
                "tickers_attempted": 0,
                "tickers_succeeded": 0,
                "tickers_failed": 0,
                "rows_written": 0,
                "history_limit_quarters": 0,
                "failures_sample": [],
            }
    if limit:
        universe = universe[:limit]
        log.info(
            "ops.stage.historical_fundamentals_quarterly.limited", limit=limit,
        )

    db_log = DBLogHandler(
        pool, engine=ENGINE_NAME, run_id=uuid.uuid4(),
    )
    result = await backfill_universe(
        pool, db_log, universe, end=end, resume=resume,
    )
    log.info(
        "ops.stage.historical_fundamentals_quarterly.done", **result,
    )
    return result


async def _stage_historical_macro_indicators(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-indicator historical FRED re-pull stage.

    The 2026-05-22 full-spectrum data-feed hardening audit
    (``docs/audits/2026-05-22-full-spectrum-data-feed-hardening.md``)
    flagged ``macro_indicators`` as missing a one-shot historical
    stage. The canonical ``macro_indicators`` stage runs a rolling
    refresh; the D8 cascade computes per-indicator targets at heal
    time. This stage is the operator-invokable shape for the same
    primitive: name an indicator (+ optional date range) and pull
    its full FRED observation set into ``platform.macro_indicators``.

    Wraps ``per_indicator_fred_repull`` (the existing D8 primitive)
    so the operator path and the auto-cascade path land identical
    rows.

    Optional ``--param`` knobs:
        * ``indicator=initial_claims`` — single canonical indicator
          name (left-side of ``INDICATOR_SERIES``). Required when
          ``indicators`` is not given.
        * ``indicators=vix,credit_spread,…`` — comma-separated batch.
        * ``since=YYYY-MM-DD`` — ``observation_start``; default = full
          history (None).
        * ``until=YYYY-MM-DD`` — ``observation_end``; default = today.

    Use:
        DATABASE_URL=… FRED_API_KEY=… .venv/bin/python scripts/ops.py \\
            --stage historical_macro_indicators \\
            --param indicator=initial_claims --param since=1967-01-01
    """
    from tpcore.fred.targeted_repull import per_indicator_fred_repull

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    indicator = cfg.get("indicator")
    indicators_csv = cfg.get("indicators")
    if indicators_csv:
        if isinstance(indicators_csv, str):
            indicators = [
                s.strip() for s in indicators_csv.split(",") if s.strip()
            ]
        else:
            indicators = [str(s) for s in indicators_csv]
    elif indicator:
        indicators = [str(indicator)]
    else:
        raise RuntimeError(
            "_stage_historical_macro_indicators: pass --param indicator=<name> "
            "or --param indicators=<csv>"
        )
    since: date | None = (
        date.fromisoformat(str(cfg["since"])) if cfg.get("since") else None
    )
    until: date | None = (
        date.fromisoformat(str(cfg["until"])) if cfg.get("until") else None
    )
    log.info(
        "ops.stage.historical_macro_indicators.start",
        indicators=indicators,
        since=since.isoformat() if since else None,
        until=until.isoformat() if until else None,
    )
    results = await per_indicator_fred_repull(
        pool, indicators, start=since, end=until,
    )
    rows_total = sum(v for v in results.values() if v >= 0)
    log.info(
        "ops.stage.historical_macro_indicators.done",
        indicators=indicators,
        rows_per_indicator=results,
        rows_total=rows_total,
    )
    return {
        "indicators": indicators,
        "rows_per_indicator": results,
        "rows_total": rows_total,
    }


async def _stage_rebuild_corporate_actions_from_archive(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the latest CSV-archive snapshot into platform.corporate_actions.

    The 2026-05-22 full-spectrum data-feed hardening audit
    (``docs/audits/2026-05-22-full-spectrum-data-feed-hardening.md``)
    flagged ``corporate_actions_completeness`` as RED on ``main`` —
    live DB 109737 rows vs archive 110630 rows (0.81% shrinkage,
    archive 2026-05-15). The standard D6 cascade fires the canonical
    ``corporate_actions`` stage which re-pulls from Alpaca, but
    Alpaca's *current* response no longer contains the 893 missing
    rows (the typical shape: a ticker delisted between archive-
    snapshot and now, so the vendor stopped serving its corp-actions
    history).

    This stage is the structural recovery: read the most recent
    on-disk CSV archive (``data/csv_archives/alpaca_corporate_actions/
    alpaca_corporate_actions_*.csv.gz``) and idempotently upsert every
    row back into ``platform.corporate_actions`` via the canonical
    ``upsert_corporate_actions`` path (which keeps the physical-truth
    gate intact — ratio bounds, NULL filters, etc.).

    Operator-on-demand only (NOT in ``OPS_UPDATE_STAGES``); the D6
    cascade already dispatches the canonical ``corporate_actions``
    stage on a shrinkage red — this stage is the *manual* archive-
    replay path for when the operator confirms the vendor change is
    permanent and wants the historical truth restored.

    Optional ``--param`` knobs:
        * ``archive_path=…`` — explicit ``.csv.gz`` path; default =
          ``latest_archive("alpaca_corporate_actions")``.
        * ``dry_run=true`` — log row count + sample without upsert
          (default false).

    Use:
        DATABASE_URL=… .venv/bin/python scripts/ops.py \\
            --stage rebuild_corporate_actions_from_archive
    """
    from decimal import Decimal
    from pathlib import Path

    from tpcore.data.ingest_corporate_actions import upsert_corporate_actions
    from tpcore.ingestion.csv_archive import latest_archive, read_archive_rows

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    archive_path_arg = cfg.get("archive_path")
    dry_run = bool(cfg.get("dry_run", False))

    if archive_path_arg:
        archive_path: Path | None = Path(str(archive_path_arg))
        if not archive_path.exists():
            raise RuntimeError(
                f"rebuild_corporate_actions_from_archive: archive_path "
                f"{archive_path} does not exist"
            )
    else:
        archive_path = latest_archive("alpaca_corporate_actions")
        if archive_path is None:
            raise RuntimeError(
                "rebuild_corporate_actions_from_archive: no prior archive "
                "found for source='alpaca_corporate_actions'. Run --stage "
                "corporate_actions first to write a baseline snapshot."
            )
    log.info(
        "ops.stage.rebuild_corporate_actions_from_archive.start",
        archive=str(archive_path),
        dry_run=dry_run,
    )

    # Re-shape archive rows back into the canonical
    # upsert_corporate_actions input dict (matches _normalize_*).
    actions: list[dict[str, Any]] = []
    parse_failures = 0
    for r in read_archive_rows(archive_path):
        ticker = r.get("ticker") or ""
        action_date_str = r.get("action_date") or ""
        action_type = r.get("action_type") or ""
        ratio_str = r.get("ratio") or ""
        if not ticker or not action_date_str or not action_type or not ratio_str:
            parse_failures += 1
            continue
        try:
            action_date_v = date.fromisoformat(action_date_str)
            ratio_d = Decimal(str(ratio_str))
        except Exception:  # noqa: BLE001 — bad-row, count and skip
            parse_failures += 1
            continue
        actions.append({
            "ticker": ticker,
            "action_date": action_date_v,
            "action_type": action_type,
            "ratio": ratio_d,
            "raw_data": {"replayed_from_archive": str(archive_path)},
        })

    log.info(
        "ops.stage.rebuild_corporate_actions_from_archive.parsed",
        archive=str(archive_path),
        rows_parsed=len(actions),
        parse_failures=parse_failures,
    )

    if dry_run:
        return {
            "archive": str(archive_path),
            "rows_parsed": len(actions),
            "rows_inserted": 0,
            "parse_failures": parse_failures,
            "dry_run": True,
        }

    rows_inserted = await upsert_corporate_actions(pool, actions)
    log.info(
        "ops.stage.rebuild_corporate_actions_from_archive.done",
        archive=str(archive_path),
        rows_parsed=len(actions),
        rows_inserted=rows_inserted,
        parse_failures=parse_failures,
    )
    return {
        "archive": str(archive_path),
        "rows_parsed": len(actions),
        "rows_inserted": rows_inserted,
        "parse_failures": parse_failures,
        "dry_run": False,
    }


async def _stage_historical_insider_sentiment_daily(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot insider-filings backfill — daily granularity, full
    T1+T2 stock universe + every delisted ticker known to prices_daily.

    Carver-driven 2026-05-22: the vector engine candidate
    ``vector_beat_reversal_insider_filter_v1`` needs a 30d-rolling MSPR
    signal at DAILY resolution. The existing monthly
    ``platform.insider_sentiment`` (Finnhub free-tier) is information-
    lossy and empty pre-2025. FMP $200/yr Starter ``/stable/insider-
    trading/search`` returns per-symbol per-filing Form-4 rows — the
    substrate for any rolling window downstream. This stage backfills
    2018-01-01 → today, idempotent under the (symbol, transaction_date,
    reporting_cik, transaction_type, securities_transacted, price) PK.

    Resumable via ``application_log`` events
    (``INSIDER_BACKFILL_SYMBOL_DONE``) — a crash mid-run keeps
    completed work; the next invocation skips already-done symbols.

    Operator-on-demand only (NOT in ``OPS_UPDATE_STAGES``). Run after
    PR merge to populate ``platform.insider_filings``. Sister stage
    ``daily_insider_sentiment_delta`` IS in the daily cadence and
    catches new filings nightly.

    Optional ``--param`` knobs:
        * ``start_date=YYYY-MM-DD`` — override the 2018-01-01 default.
        * ``resume=false`` — re-process every symbol (default true).
        * ``limit=N`` — process at most N symbols (smoke).
        * ``max_pages=N`` — per-symbol page cap (default 200; safety
          ceiling — typical T1+T2 symbol bottoms out ~12-50 pages).

    Use:
        DATABASE_URL=… FMP_API_KEY=… .venv/bin/python scripts/ops.py \\
            --stage historical_insider_sentiment_daily
    """
    from tpcore.data.insider_backfill import (
        backfill_universe,
        enumerate_insider_universe,
    )
    from tpcore.logging.db_handler import DBLogHandler

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    resume = bool(cfg.get("resume", True))
    limit = int(cfg.get("limit", 0)) or None
    max_pages = int(cfg.get("max_pages", 200))
    start = (
        date.fromisoformat(str(cfg["start_date"]))
        if cfg.get("start_date") else date(2018, 1, 1)
    )

    universe = await enumerate_insider_universe(pool)
    log.info(
        "ops.stage.historical_insider_sentiment_daily.enumerated",
        total=len(universe),
    )
    if limit:
        universe = universe[:limit]
        log.info(
            "ops.stage.historical_insider_sentiment_daily.limited",
            limit=limit,
        )

    db_log = DBLogHandler(pool, engine=ENGINE_NAME, run_id=uuid.uuid4())
    result = await backfill_universe(
        pool, db_log, universe,
        start=start, resume=resume, max_pages=max_pages,
    )
    log.info("ops.stage.historical_insider_sentiment_daily.done", **result)
    return {
        "universe_enumerated": len(universe),
        **result,
    }


async def _stage_daily_insider_sentiment_delta(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Nightly incremental for ``platform.insider_filings``.

    Pages 0..(pages-1) of FMP ``/stable/insider-trading/search`` per
    symbol in the T1+T2 stock universe + delisted-prices_daily set.
    The (symbol, transaction_date, reporting_cik, transaction_type,
    securities_transacted, price) PK + ON CONFLICT DO NOTHING keep
    re-runs idempotent and free for already-seen rows.

    Wired into ``OPS_UPDATE_STAGES`` via ``_STAGE_SPECS`` (NOT off-
    cycle) so the existing data-operations daemon (21:30 UTC weekday
    cron) catches every filing the day after it lands at FMP — per
    the operator directive "make sure automation works so we aren't
    backfilling all the damn time."

    Optional ``--param`` knobs:
        * ``pages=N`` — per-symbol page cap (default 1 = last 100 rows
          per symbol; high-volume names that need >100 rows/day are
          covered by the historical backfill — the delta stage exists
          purely to catch the prior day's filings, never to backfill).
        * ``limit=N`` — process at most N symbols (smoke).
    """
    from tpcore.data.insider_backfill import (
        daily_delta,
        enumerate_insider_universe,
    )
    from tpcore.logging.db_handler import DBLogHandler

    cfg = cfg or {}
    log = structlog.get_logger("scripts.ops")
    pages = int(cfg.get("pages", 1))
    limit = int(cfg.get("limit", 0)) or None

    universe = await enumerate_insider_universe(pool)
    if limit:
        universe = universe[:limit]

    db_log = DBLogHandler(pool, engine=ENGINE_NAME, run_id=uuid.uuid4())
    result = await daily_delta(
        pool, db_log, universe=universe, pages=pages,
    )
    log.info("ops.stage.daily_insider_sentiment_delta.done", **result)
    return result


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


async def _stage_aar_replay(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wave-4 E4 replay — drain ``platform.aar_deferred`` into ``aar_events``.

    Reads pending deferred AAR rows (``replayed_at IS NULL``) oldest-
    first and re-attempts the canonical ``aar_events`` insert via
    :class:`tpcore.aar.writer.AARWriter`. Successful rows are marked
    ``replayed_at = now()``; rows that still raise stay queued for the
    next run (the substrate is presumed still degraded).

    Bounded per call by ``--param limit=<int>`` (default 100) so the
    stage never exceeds the standard timeout when a large backlog has
    accumulated. The next run drains the next slice.

    Operator-on-demand AND wired as an off-cycle stage (not in
    ``OPS_UPDATE_STAGES``) — the replay is also triggered implicitly on
    the next engine cycle by anything that constructs an
    :class:`AARWriter`, so the off-cycle stage is the bulk-drain knob
    rather than the only path.
    """
    cfg = config or {}
    try:
        limit = int(cfg.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    if limit <= 0:
        limit = 100

    from tpcore.aar.deferred import replay_deferred_aars

    counts = await replay_deferred_aars(pool, limit=limit)
    return {
        "limit": limit,
        "pending": counts.get("pending", 0),
        "replayed": counts.get("replayed", 0),
        "still_failing": counts.get("still_failing", 0),
    }


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


async def _stage_rebuild_from_archive(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the most-recent ``<source>_archive`` into the live DB.

    Catastrophic-recovery path — the canonical way to rebuild
    ``platform.prices_daily`` (and other archive-backed tables) from
    the CSV-first archive after a DB loss / restore-from-scratch.
    Reads through the env-selected backend
    (``CSV_ARCHIVE_BACKEND=local|s3``), so the same one-liner works
    on the local Mac (today) and on Railway-after-migration (with
    ``CSV_ARCHIVE_BACKEND=s3`` pointed at the bucket).

    Bounded, idempotent, operator-on-demand. Use:
        ``python scripts/ops.py --stage rebuild_from_archive \
            --param source=alpaca_daily_bars``

    Idempotency: the underlying upsert uses
    ``ON CONFLICT (ticker, date) DO UPDATE`` (the same statement as
    the daily ingest path) so re-running is safe.

    Currently shipped sources:
        * ``alpaca_daily_bars`` → ``platform.prices_daily``

    Other sources can be added as their authoritative upsert paths
    get factored out of the daily handlers; for now the rebuild is
    the daily_bars path because that's the table whose loss is
    catastrophic. AAR / forensics / engine state are NOT rebuilt
    here — they regenerate from the prices replay on the next sweep.
    """
    cfg = cfg or {}
    source = cfg.get("source")
    if not source:
        raise ValueError(
            "rebuild_from_archive: --param source=<name> is required "
            "(e.g. alpaca_daily_bars). No default — the operator MUST "
            "name the source being rebuilt so a typo doesn't silently "
            "replay the wrong archive into the live table."
        )

    log = structlog.get_logger("scripts.ops")
    log.info("ops.stage.rebuild_from_archive.start", source=source)

    from tpcore.ingestion.csv_archive_backends import select_backend
    backend = select_backend()
    body = backend.read_latest(source)
    if body is None:
        log.warning(
            "ops.stage.rebuild_from_archive.no_archive",
            source=source,
            backend=type(backend).__name__,
        )
        return {
            "source": source,
            "rows_replayed": 0,
            "skipped": True,
            "reason": "no_archive_found",
            "backend": type(backend).__name__,
        }

    # Decompress + parse the CSV in memory. The archives are
    # gzip-compressed CSV (the LocalFSBackend wrote the file; the
    # S3Backend uploaded the same gzip bytes); decompress→csv reader
    # works identically against both bodies.
    import csv as _csv
    import gzip
    import io
    text = gzip.decompress(body).decode("utf-8", errors="replace")
    reader = _csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if source != "alpaca_daily_bars":
        # Fail-loud rather than silently no-op on an unknown source —
        # the operator may have typo'd or be asking for a not-yet-shipped
        # rebuild path; either way the right answer is to surface the
        # gap, not pretend to recover.
        raise NotImplementedError(
            f"rebuild_from_archive: source={source!r} has no shipped "
            "upsert path. Currently shipped: alpaca_daily_bars."
        )

    # Replay into platform.prices_daily via the canonical idempotent
    # upsert (mirrors tpcore.data.ingest_alpaca_bars.upsert_bars). The
    # archive rows already passed physical-truth validation at write
    # time, so this loop just normalises types and pushes through.
    sql = """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'alpaca')
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            delisted = EXCLUDED.delisted,
            delisting_date = EXCLUDED.delisting_date,
            source = 'alpaca'
    """
    from datetime import datetime as _dt
    args: list[tuple] = []
    rejected = 0
    for row in rows:
        try:
            ts = _dt.fromisoformat(row["date"].replace("Z", "+00:00"))
            session_date = ts.date()
            close = float(row["close"])
            args.append((
                row["ticker"],
                session_date,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                close,
                int(row["volume"]),
                close,           # adjusted_close mirrors close (same convention as ingest)
                False,           # delisted: rebuild path treats rows as currently active
                None,            # delisting_date
            ))
        except (ValueError, KeyError, TypeError):
            rejected += 1

    if args:
        async with pool.acquire() as conn:
            await conn.executemany(sql, args)

    log.info(
        "ops.stage.rebuild_from_archive.done",
        source=source,
        rows_replayed=len(args),
        rows_rejected=rejected,
        backend=type(backend).__name__,
    )
    return {
        "source": source,
        "rows_replayed": len(args),
        "rows_rejected": rejected,
        "backend": type(backend).__name__,
    }


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
    # seed_monotone_snapshots — one-shot bulk-seed the per-ticker monotone-
    # baseline snapshot tables (sec_insider_row_counts_snapshot +
    # earnings_events_count_snapshot). Resolves the structural blocker where
    # the in-check Python UPSERT loop times out against the Supavisor pooler
    # before the seed baseline lands. Operator-on-demand; NOT in --update.
    ("seed_monotone_snapshots", lambda pool, cfg: (lambda: _stage_seed_monotone_snapshots(pool, cfg)), STAGE_TIMEOUT_SEC),
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
    # ── Operator-on-demand verification + utility stages ──
    # NOT in OPS_UPDATE_STAGES — never invoked by the daily --update
    # cadence. Migrated 2026-05-20 from orphan one-off scripts
    # (docs/superpowers/audits/2026-05-20-orphan-scripts-catalog.md);
    # operator overruled the keep-as-helper disposition in favour of
    # the zero-allowlist end-state.
    #
    # compare_baselines — diff two trade-log CSVs via
    # ``tpcore.backtest.compare_trade_lists``. Pure file I/O, no DB.
    # Use: --stage compare_baselines --param baseline=… --param candidate=…
    ("compare_baselines",   lambda pool, cfg: (lambda: _stage_compare_baselines(pool, cfg)),     STAGE_TIMEOUT_SEC),
    # aar_pipeline_smoke — synthetic round-trip verification of
    # ``AARWriter.write_aar`` against the live ``platform.aar_events``.
    # Self-cleaning (DELETE in finally). Operator-on-demand.
    ("aar_pipeline_smoke",  lambda pool, cfg: (lambda: _stage_aar_pipeline_smoke(pool, cfg)),    STAGE_TIMEOUT_SEC),
    # aar_replay — Wave-4 E4 drain of platform.aar_deferred into
    # platform.aar_events via tpcore.aar.deferred.replay_deferred_aars.
    # Operator-on-demand (off-cycle) bulk-drain knob; the implicit
    # replay path runs every engine cycle as AAR writes happen, so this
    # stage is the "catch up the backlog" lever rather than the only
    # recovery path.
    ("aar_replay",          lambda pool, cfg: (lambda: _stage_aar_replay(pool, cfg)),            STAGE_TIMEOUT_SEC),
    # kill_switch_smoke — flip kill_switch_active=true for one engine,
    # run scheduler.run_once(), assert zero candidates/submissions,
    # reset in finally. Use: --stage kill_switch_smoke --param engine=reversion
    ("kill_switch_smoke",   lambda pool, cfg: (lambda: _stage_kill_switch_smoke(pool, cfg)),     STAGE_TIMEOUT_SEC),
    # Offline Sentinel activation-score distribution probe (one-off
    # diagnostic; operator-on-demand). Read-only — no Lab spend, no
    # n_trials increment, no dossier. Used to diagnose whether a FAILED
    # Sentinel Lab probe is structurally-dormant (composite < 0.45
    # floor) vs threshold-clipped. Migrated 2026-05-21 from the orphan
    # scripts/probe_sentinel_activation.py per the no-orphan-scripts
    # gate.
    ("probe_sentinel_activation", lambda pool, cfg: (lambda: _stage_probe_sentinel_activation(pool, cfg)), STAGE_TIMEOUT_SEC),
    # extract_tradier_full — wide-universe Tradier CSV extractor
    # (NYSE/NASDAQ/AMEX stocks+ETFs, 2000-01-01 → today). No DB writes,
    # streaming + resumable. Long-running, bounded — uses
    # HEAVY_STAGE_TIMEOUT_SEC.
    ("extract_tradier_full", lambda pool, cfg: (lambda: _stage_extract_tradier_full(pool, cfg)), HEAVY_STAGE_TIMEOUT_SEC),
    # ingest_tradier_csv — downstream of extract_tradier_full; streams
    # the wide CSV into platform.prices_daily with ON CONFLICT DO
    # NOTHING idempotency + an Alpaca-active-asset filter gate. Long-
    # running, bounded — uses HEAVY_STAGE_TIMEOUT_SEC. Operator-on-
    # demand only.
    ("ingest_tradier_csv",  lambda pool, cfg: (lambda: _stage_ingest_tradier_csv(pool, cfg)),    HEAVY_STAGE_TIMEOUT_SEC),
    # rebuild_from_archive — replay the latest <source>_archive into
    # the live DB via the canonical upsert. Catastrophic-recovery path
    # for the R3 substrate migration (env-pluggable backend; works
    # identically against local FS today and an S3-compatible bucket
    # after Railway migration). Operator-on-demand:
    # --stage rebuild_from_archive --param source=alpaca_daily_bars.
    # NOT in --update; idempotent; bounded per-source. Heavy timeout
    # because the alpaca_daily_bars archive holds ~50k rows/run × N
    # historical archives — a full replay can move millions of rows.
    ("rebuild_from_archive", lambda pool, cfg: (lambda: _stage_rebuild_from_archive(pool, cfg)),  HEAVY_STAGE_TIMEOUT_SEC),
    # D7 — dedupe rogue rows on monotone-watched tables. NOT in the
    # default daily cycle; invoked by the validation cascade
    # (``_auto_cascade_validation_failures``) when a *_monotone check
    # reds, and operator-callable via the ops CLI for audits.
    ("dedupe_monotone",     lambda pool, cfg: (lambda: _stage_dedupe_monotone(pool, cfg)),       STAGE_TIMEOUT_SEC),
    # historical_delisted_universe — survivorship-bias backfill via FMP
    # (operator one-shot, NOT in OPS_UPDATE_STAGES). Enumerates the
    # known delisted universe across five sources + per-ticker FMP
    # /historical-price-eod/full GETs, upserts each bar with
    # delisted=true + delisting_date set to FMP's final-bar date.
    # Resumable via SURVIVORSHIP_BACKFILL_TICKER_DONE events.
    # Heavy timeout: ~3000-5000 enumerated tickers × ~0.2s/call ≈
    # 10-15 min wall time; gives 60min headroom.
    ("historical_delisted_universe",
        lambda pool, cfg: (lambda: _stage_historical_delisted_universe(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # daily_delisted_universe_check — nightly silent-disappearance
    # catcher. T1/T2 ticker missing 5+ sessions + FMP confirms no bars
    # → mark delisted with the corpus's final-seen date. Operator-on-
    # demand for now; promote to daily cadence once the structural
    # backfill above is stable. Standard timeout (probes are bounded).
    ("daily_delisted_universe_check",
        lambda pool, cfg: (lambda: _stage_daily_delisted_universe_check(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # historical_earnings_events_t1_t2 — Vector-unblock backfill (one-shot).
    # Enumerates the ~1500 T1+T2 stock-class universe + per-ticker FMP
    # /stable/earnings GETs, upserts EARNINGS_BEAT / EARNINGS_NO_BEAT rows
    # into platform.earnings_events. Resumable via
    # EARNINGS_BACKFILL_TICKER_DONE events. Heavy timeout: 1500 tickers ×
    # ~0.6s/call ≈ 15 min wall time; 60-min HEAVY headroom keeps a slow
    # FMP day inside one stage budget.
    ("historical_earnings_events_t1_t2",
        lambda pool, cfg: (lambda: _stage_historical_earnings_events_t1_t2(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # historical_fundamentals_quarterly — Wave-1 critical-path blocker
    # heal (one-shot). Reads gap-target tickers from
    # compute_fundamentals_repair_targets (the same source the D6
    # validation cascade consults) and per-ticker FMP fetches via
    # FMPFundamentalsAdapter; resumable via
    # FUNDAMENTALS_BACKFILL_TICKER_DONE events. 285 gap-tickers × ~1s
    # cadence ≈ 5 min wall time; HEAVY headroom for FMP slowness.
    ("historical_fundamentals_quarterly",
        lambda pool, cfg: (lambda: _stage_historical_fundamentals_quarterly(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # historical_macro_indicators — Wave-1 critical-path blocker heal
    # (per-indicator targeted re-pull). Wraps per_indicator_fred_repull
    # so the operator can backfill ANY missing FRED-observation date
    # range for a single (or batch of) indicator(s). Standard timeout —
    # FRED is fast (5-50 series, courtesy 0.5s/req) and the upsert is
    # bounded by the indicator's history depth.
    ("historical_macro_indicators",
        lambda pool, cfg: (lambda: _stage_historical_macro_indicators(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # rebuild_corporate_actions_from_archive — Wave-1 critical-path
    # blocker heal (archive-replay). Reads the latest CSV archive and
    # upserts back into platform.corporate_actions via the canonical
    # upsert path (physical-truth gate preserved). The D6 cascade
    # already covers the "re-pull from Alpaca" path; THIS stage is the
    # structural recovery for vendor-shrinkage cases where Alpaca no
    # longer serves the historical rows.
    ("rebuild_corporate_actions_from_archive",
        lambda pool, cfg: (lambda: _stage_rebuild_corporate_actions_from_archive(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # historical_insider_sentiment_daily — one-shot FMP /stable/insider-
    # trading/search backfill (Carver, 2026-05-22) for the vector engine's
    # 30d-rolling MSPR signal. Off-cycle: operator runs once after PR
    # merge; ~2400 active + ~248 delisted symbols × ~12-50 pages × ~0.5s
    # = 30-90 min wall-time. Heavy timeout (60min) — limit/resume knobs
    # available; a crash mid-run is replayed by the resume probe.
    ("historical_insider_sentiment_daily",
        lambda pool, cfg: (lambda: _stage_historical_insider_sentiment_daily(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # daily_insider_sentiment_delta — nightly incremental for insider_
    # filings. IN OPS_UPDATE_STAGES via the feed dispatcher (FeedProfile
    # 'insider_sentiment_daily', cadence_days=1, CONTINUOUS trigger).
    # Pulls page 0 of /insider-trading/search per symbol — last 100
    # filings per ticker, idempotent under the table PK + ON CONFLICT
    # DO NOTHING. Heavy timeout: ~2400 symbols × ~0.5s/call ≈ 20 min.
    ("daily_insider_sentiment_delta",
        lambda pool, cfg: (lambda: _stage_daily_insider_sentiment_delta(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
)
KNOWN_STAGES: tuple[str, ...] = tuple(name for name, _, _ in _STAGE_SPECS)
# Stages that are NOT part of the default daily ``cmd_update`` cycle —
# they are only invoked on-demand (operator CLI) or by the auto-cascade.
# Keeps the daily cycle bounded; new self-heal stages live here.
_OFF_CYCLE_STAGES: frozenset[str] = frozenset({
    "rebuild_from_archive",
    "dedupe_monotone",
    # Wave-4 E4 deferred-AAR drain (operator-on-demand; the implicit
    # replay path runs on every engine cycle via AARWriter so this is
    # the bulk-drain knob, not the only recovery path).
    "aar_replay",
    # Survivorship-bias backfill stages — operator-on-demand. The
    # one-shot historical fill runs once after PR merge; the nightly
    # delta probe stays out of the daily cadence until the structural
    # backfill stabilises.
    "historical_delisted_universe",
    "daily_delisted_universe_check",
    # Vector-unblock earnings-events one-shot — populates the T1+T2
    # stock-class catalyst-event coverage. Not in the daily cadence;
    # the weekly earnings_refresh stage keeps the tail fresh post-
    # backfill.
    "historical_earnings_events_t1_t2",
    # Wave-1 feed-audit critical-path one-shots (PR fix/feed-audit-
    # wave-1-critical-path-blockers): historical fundamentals + macro
    # per-indicator + corporate-actions archive replay. Operator-on-
    # demand only — the daily refresh stages keep the tail fresh
    # post-backfill.
    "historical_fundamentals_quarterly",
    "historical_macro_indicators",
    "rebuild_corporate_actions_from_archive",
    # Insider-filings one-shot backfill — Carver 2026-05-22. Operator-on-
    # demand. The SISTER stage ``daily_insider_sentiment_delta`` IS in
    # the daily cadence (NOT in this off-cycle set) and rides the feed
    # dispatcher via FEED_PROFILES['insider_sentiment_daily'].
    "historical_insider_sentiment_daily",
})

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

# Discriminator token (lowercased) that identifies a daily_bars
# coverage-collapse producer-self-validation failure. The exact message
# raised by ``_stage_daily_bars`` is
#   "daily_bars coverage collapse: <date> has <n> tickers = <pct>% of …"
# (see the RuntimeError raise near the COVERAGE_COLLAPSE_PCT block).
# The token is matched case-insensitively against StageResult.error so a
# coverage_collapse RuntimeError triggers the one-shot force-refresh
# cascade — NOT a blanket retry of every INGESTION_FAILED. Other
# failures (auth, schema drift) need different handling and are left
# alone here.
_DAILY_BARS_COVERAGE_COLLAPSE_TOKEN: str = "coverage collapse"


# ─────────────────────────────────────────────────────────────────────────
# Wave-1 deterministic self-heal cascade (D6..D10) — see
# docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-expansion-design.md
# ─────────────────────────────────────────────────────────────────────────

# Discriminator token (lowercased) that identifies a data_validation
# stage failure. The exact message raised by ``_stage_data_validation``
# is "validation suite failed: [<check_name>, …]". Matched case-insensitively
# against StageResult.error so a validation-suite RuntimeError triggers the
# D6 cascade.
_VALIDATION_SUITE_FAILED_TOKEN: str = "validation suite failed"

# D6 — canonical refresh stage for each known validation check name.
# When ``data_validation`` reds on a check listed here, the cascade
# dispatches the named stage with the listed params. ``skip_guard_days=0``
# forces the refresh past the stage's own freshness skip-guard so the
# cascade can actually heal in the same cycle. Checks NOT in this map are
# left to the LLM-side backstop (the long-tail per the spec §0).
#
# D7 dedupe is wired through a separate stage (``_stage_dedupe_monotone``)
# because monotone violations have a distinct recovery shape (delete-then-
# re-pull); the dedupe stage chains a canonical refresh on success.
_VALIDATION_CASCADE_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    # D6 — completeness checks → canonical refresh stages
    "fundamentals_quarterly_completeness": (
        "fundamentals_refresh",
        {"skip_guard_days": 0},
    ),
    "corporate_actions_completeness": (
        "corporate_actions",
        {"skip_guard_days": 0},
    ),
    # D9 — liquidity-tier ticker missing → tier_refresh with skip_guard=0
    "liquidity_tiers_completeness": (
        "tier_refresh",
        {"skip_guard_days": 0},
    ),
    "liquidity_tiers_freshness": (
        "tier_refresh",
        {"skip_guard_days": 0},
    ),
    # D10 — ticker classifications drift → classify_tickers force re-pull
    "ticker_classifications_coverage": (
        "classify_tickers",
        {"force": True, "skip_guard_days": 0},
    ),
    # D6 — earnings completeness → canonical earnings_refresh
    "earnings_events_freshness": (
        "earnings_refresh",
        {"skip_guard_days": 0},
    ),
    # D6 — SEC filings → canonical refresh
    "sec_filings_freshness": (
        "sec_filings",
        {"skip_guard_days": 0},
    ),
}

# D7 monotone violations → dedupe-then-refresh. The dedupe stage runs
# FIRST (cleans rogue rows per the spec rule), then the canonical refresh
# stage is invoked to re-pull truncated history.
_MONOTONE_CASCADE_MAP: dict[str, tuple[str, str, dict[str, Any]]] = {
    # check_name → (dedupe_target_table, refresh_stage, refresh_params)
    "earnings_events_monotone": (
        "platform.earnings_events",
        "earnings_refresh",
        {"skip_guard_days": 0},
    ),
    "sec_insider_monotone": (
        "platform.sec_insider_transactions",
        "sec_filings",
        {"skip_guard_days": 0},
    ),
}

# D8 — macro_indicators_completeness fires when one or more FRED series
# have a per-publication-date gap. The cascade resolves the targeted
# indicator list + lookback via ``compute_macro_repair_targets`` and
# routes through ``_per_indicator_fred_repull`` (NOT the full macro
# stage — the spec calls for a per-indicator targeted re-pull).
_MACRO_COMPLETENESS_CHECK: str = "macro_indicators_completeness"


# ─────────────────────────────────────────────────────────────────────────
# D11 — vendor_late classification registry. Maps a freshness check name
# to the feed key that ``tpcore.selfheal.probes.VENDOR_PROBES`` knows. For
# each red freshness check in this map, the D11 cascade consults the
# vendor probe; if the vendor probe says "I have nothing newer than what
# you hold" (has_newer == False) the check is classified VENDOR_LATE
# (NOT our defect) and skipped from the Wave-1 validation cascade — the
# daemon stops retrying a heal that can't help because the vendor hasn't
# published yet. The freshness check itself stays red in the summary;
# this is a CLASSIFICATION, not a downgrade — per the spec §1 D11
# guardrail "vendor_late is a CLASSIFICATION (skip-because-not-our-
# defect), not a relaxation".
#
# Scope is intentionally narrow: only the two feed shapes the spec
# names — AAII Sentiment (Thursday weekly publish) and fear_greed
# (derived from prices_daily; vendor_late means no new NYSE close has
# published yet). Adding a row is one entry; the prices_daily +
# macro_indicators probes are deliberately NOT wired here because their
# freshness checks have other downstream invariants (prices_daily
# completeness, macro per-indicator gap) the operator wants to keep
# strict.
_VENDOR_LATE_CHECK_MAP: dict[str, str] = {
    # AAII Sentiment Survey — weekly Thursday publish; HEAD Last-Modified
    # probe on the .xls confirms the vendor's actual latest publish date.
    "aaii_sentiment_freshness": "aaii_sentiment",
    # fear_greed is DERIVED from prices_daily; vendor_late here means
    # "no new NYSE session has published yet" — answered by the Alpaca
    # SPY-anchor probe (every NYSE session, never delisted). When SPY's
    # latest bar matches our latest fear_greed date, the derived feed
    # can't move until the next session — not our defect.
    "fear_greed_freshness": "prices_daily",
}


# ─────────────────────────────────────────────────────────────────────────
# D14 — data_validation TIMEOUT chunking. The Wave-1 validation cascade
# is keyed on a FAILED check_name list, which a TIMEOUT on the monolithic
# ``_stage_data_validation`` does NOT produce (the 300s cap fires before
# the suite returns). Live exercise 2026-05-22 found data_validation
# timing out at the cap before completing the 25-check suite (50 new SOS
# PHCI series per PR #216 pushed total wall time past the budget).
#
# Recovery: re-run the suite chunked into smaller sub-stages, each with
# its own 60s budget. Per-chunk timeouts are recorded as failed checks
# (operator-visible, NOT silently swallowed). The aggregated failed-check
# list is then synthesised into the same shape the Wave-1 cascade
# already consumes — ``RuntimeError("validation suite failed: [<names>]")``
# — so no contract change at the consumer side. Per the spec D14 task:
# "chunking must preserve the same aggregate failed-check list shape
# that ``_auto_cascade_validation_failures`` consumes — don't change the
# contract; just chunk the production".
#
# The chunks partition the 25 checks roughly by data layer + cost. Each
# chunk targets <= 60s wall time on a healthy DB; the budget is per-
# chunk, not a hard cap on each check, so a single slow check inside
# its chunk still completes if the others in the chunk are fast.
_VALIDATION_CHUNK_BUDGET_SEC: float = 60.0

# Chunk specs: (chunk_name, list[check_name]). Names match the
# ``CHECK_NAME`` constants from each tpcore/quality/validation/checks/*.py
# module so the suite reader is the single source of truth. A check
# NOT listed in any chunk is silently skipped by chunking (operator-
# visible because it does NOT appear in the chunked-run output — the
# missing check is the operator-surfacing signal).
_VALIDATION_CHUNK_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Chunk 1 — prices/integrity (lightweight read-only sweeps).
    (
        "structure_integrity",
        (
            "delistings",
            "constituent",
            "splits",
            "row_integrity",
            "fundamentals_integrity",
            "corporate_actions_integrity",
        ),
    ),
    # Chunk 2 — prices_daily heavy (the largest table; isolated so
    # it doesn't steal budget from sibling checks).
    (
        "prices_daily",
        (
            "prices_daily_freshness",
            "prices_daily_completeness",
        ),
    ),
    # Chunk 3 — completeness checks (medium cost; per-ticker scans
    # across fundamentals / corporate_actions / liquidity / classify).
    (
        "completeness",
        (
            "fundamentals_quarterly_completeness",
            "corporate_actions_completeness",
            "liquidity_tiers_completeness",
            "liquidity_tiers_freshness",
            "ticker_classifications_coverage",
        ),
    ),
    # Chunk 4 — events + monotone (per-ticker UPSERT loops; pre-PR-#261
    # the source of the 300s overrun; pinned in its own chunk so the
    # seed_monotone_snapshots baseline path is the only churn).
    (
        "events_monotone",
        (
            "earnings_events_freshness",
            "earnings_events_monotone",
            "sec_filings_freshness",
            "sec_insider_monotone",
        ),
    ),
    # Chunk 5 — macro/FRED (per-indicator gap scan; cost scales with
    # INDICATOR_SERIES * dates).
    (
        "macro",
        (
            "macro_indicators_freshness",
            "macro_indicators_completeness",
        ),
    ),
    # Chunk 6 — sentiment / sentiment-derived (mostly single-row
    # freshness queries; cheap).
    (
        "sentiment",
        (
            "options_max_pain_freshness",
            "insider_sentiment_freshness",
            "insider_filings_freshness",
            "social_sentiment_freshness",
            "fear_greed_freshness",
            "short_interest_freshness",
            "borrow_rates_freshness",
            "aaii_sentiment_freshness",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────────
# Wave-2 deterministic self-heal cascade (D2 / D3 / D5 / D13) — see
# docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
# expansion-design.md §1 rows D2 D3 D5 D13 + §4 ANSWERED.
# ─────────────────────────────────────────────────────────────────────────
#
# Discriminator-token sets (lowercased). Each set captures the exact
# error-string shapes that mark the failure for one Wave-2 row. The
# cascade dispatcher walks each FAILED/TIMEOUT stage's error string
# AND its INGESTION_FAILED data.reason (when available) against these
# sets, dispatching at most ONE Wave-2 cascade per failed stage — the
# rows are mutually exclusive by construction (a timeout is not an
# auth-401, a connection-drop is not a pool-exhaustion, etc.).
#
# Non-overlap with `_RETRYABLE_FAILURE_REASONS`: the generic transient
# retry handles timeouts + network blips with the SAME stage config. The
# Wave-2 cascades use a DIFFERENT recovery shape per row (chunked
# force_refresh for D2, recycled pool for D13, etc.) — so Wave-2 fires
# BEFORE `_self_heal_failed_stages` to claim the stage first, then the
# transient retry only sees what we left FAILED.

# D2 — timeout on a NON-chunked daily_bars invocation. The cascade
# re-invokes with the chunked force_refresh path (PR #236 + PR #231
# smart-feed) which has provably stayed under the 3600s ceiling on the
# operator's full ~7300-ticker universe.
_TIMEOUT_TOKENS: frozenset[str] = frozenset({
    "timed out",        # _run_stage's own message: "timed out after Ns"
    "timeout",
})

# D3 — connection-drop tokens. The operator's 2026-05-18..20 incidents
# logged the asyncpg phrase "connection was closed in the middle of
# operation" + a few sibling shapes from other drivers. Substring match,
# case-insensitive.
_CONNECTION_DROP_TOKENS: frozenset[str] = frozenset({
    "connection was closed in the middle of operation",
    "connection was closed",
    "connectionreseterror",
    "server disconnected",
    "remote protocol error",
})

# D5 — provider 401 tokens. Matches HTTP 401 in a response body /
# httpx-style error message; the cascade is per-stage (any stage can
# fail with provider auth, not just daily_bars). We use a permissive
# substring set because providers stringify 401s in many shapes:
#   "401 Unauthorized"
#   "HTTP 401"
#   "status_code=401"
#   "Client error '401 Unauthorized'..."
_AUTH_401_TOKENS: frozenset[str] = frozenset({
    " 401 ",
    "401 unauthorized",
    "http 401",
    "status_code=401",
    "status code 401",
})

# D13 — Postgres pool / connection exhaustion tokens. The asyncpg
# exception class names appear in `str(exc)` when their __repr__ is
# baked into the error message; the libpq strings appear when the
# server itself rejects.
_POOL_EXHAUSTION_TOKENS: frozenset[str] = frozenset({
    "toomanyconnectionserror",
    "postgresconnectionerror",
    "pooltimeout",
    "connection slots are reserved",
    "remaining connection slots are reserved",
    "too many connections",
})


def _matches_any(error_text: str | None, tokens: frozenset[str]) -> bool:
    """Lower-case substring match against a token set. Safe on None."""
    if not error_text:
        return False
    err = error_text.lower()
    return any(tok in err for tok in tokens)


# Live SIP-availability probe. Single GET against the Alpaca data
# endpoint with feed=sip; bounded 10s timeout, no retry, no cache. The
# probe is cheap (one request) and the cascade is one-shot, so we re-
# probe every time the cascade fires — that's deliberate, because the
# operator demonstrated 2026-05-21 that SIP entitlement can transiently
# 403 and then recover within ~20min. A cached "False" would freeze the
# cascade to IEX-only for the duration of the cache, which is exactly
# what we DON'T want during a recovery window.
#
# NOTE 2026-05-22: With FMP as the primary daily-bars feed, this
# SIP/IEX probe-and-failover cascade becomes a SECONDARY fallback —
# it fires only when an FMP-driven daily_bars pull raises
# coverage_collapse. The probe-and-fall-back-to-IEX semantics are
# kept intact (option A — leave the SIP cascade in place) so the
# operator has a deterministic Alpaca-side recovery path if FMP
# itself goes down. Option B (strip SIP probing) was rejected
# because the SIP entitlement is free with the existing Alpaca
# subscription; keeping the probe costs one HTTP call per cascade
# fire (rare) for non-zero recovery value.
_SIP_PROBE_URL: str = "https://data.alpaca.markets/v2/stocks/AAPL/bars"
_SIP_PROBE_TIMEOUT_SEC: float = 10.0


async def _alpaca_sip_available(client: Any | None = None) -> bool:
    """Probe Alpaca SIP feed availability for the current account.

    Returns True iff a recent (≤5 trading days ago) AAPL bar pull with
    ``feed=sip`` returns HTTP 200 with a non-empty bar list. Returns
    False on:

    * HTTP 403 (any reason — typically "subscription does not permit
      querying recent SIP data"; the operator observed this both
      transiently and persistently between 2026-05-21 04:38 → 04:55 UTC)
    * Any other non-200 (5xx, 429, etc.)
    * Network error / timeout (bounded 10s)
    * HTTP 200 with empty bars (entitlement permits the request but
      returns nothing — also unsafe to assume SIP works)

    The probe is INTENTIONALLY uncached: the SIP entitlement state
    flapped within a 17-minute window in the operator's 2026-05-21
    incident, so caching the negative answer would freeze the cascade
    into IEX-only mode through a recovery. Cost of a live probe is
    1 HTTP request per cascade — and the cascade itself only fires on
    a coverage_collapse, which is rare.

    ``client`` is an optional ``httpx.AsyncClient`` injection seam for
    tests; when None, a 10s-timeout client is built internally with the
    canonical Alpaca headers from env.
    """
    import httpx

    from tpcore.data.ingest_alpaca_bars import _alpaca_headers

    # Probe window: 5 calendar-days back to 1 day back. SIP bars for the
    # most-recent closed session exist immediately after market close;
    # the 5-day window is wide enough to span a weekend or holiday so
    # the probe never gets a false-negative from "no bars yet today".
    today = datetime.now(UTC).date()
    start = (today - timedelta(days=5)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    params = {
        "timeframe": "1Day",
        "start": start,
        "end": end,
        "feed": "sip",
        "limit": "10",
    }

    async def _do(c: httpx.AsyncClient) -> bool:
        try:
            resp = await c.get(_SIP_PROBE_URL, params=params)
        except (httpx.RequestError, httpx.HTTPError):
            return False
        if resp.status_code != 200:
            return False
        try:
            payload = resp.json()
        except (ValueError, TypeError):
            return False
        bars = payload.get("bars") if isinstance(payload, dict) else None
        return bool(bars)

    if client is not None:
        return await _do(client)

    async with httpx.AsyncClient(
        headers=_alpaca_headers(),
        timeout=_SIP_PROBE_TIMEOUT_SEC,
    ) as c:
        return await _do(c)

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
        # Off-cycle stages (operator-on-demand or cascade-only) are
        # skipped by the daily ``cmd_update`` loop. ``only`` overrides
        # this — passing ``only={"dedupe_monotone"}`` runs it.
        if only is None and name in _OFF_CYCLE_STAGES:
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

    # Coverage-collapse auto-cascade — operator-reproduced 2026-05-18 →
    # 2026-05-20 (three consecutive nights INGESTION_FAILED with
    # "daily_bars coverage collapse: <date> has <n> tickers = 6–7%",
    # ZERO auto-recovery despite the codebase already shipping the
    # repair_gaps targeted heal). The producer-self-validation refuses
    # to report OK on a sub-floor target session; the heal that closes
    # exactly that gap (`_stage_daily_bars(repair_gaps=true)`) is then
    # NEVER invoked from the failure path. This one-shot cascade closes
    # the gap WITHOUT changing the safety check thresholds and WITHOUT
    # blanket-retrying every INGESTION_FAILED (auth / schema drift /
    # other RuntimeError modes need different handling).
    if not dry_run:
        await _auto_cascade_coverage_collapse(
            summary, pool, daily_bars_config, log=log, db_log=db_log,
        )

    # D14 — data_validation TIMEOUT → chunked re-run + synthesize FAILED.
    # Fires BEFORE the Wave-1 validation cascade so a TIMEOUT (which
    # produces no failed-check list) is rewritten to a FAILED entry
    # whose error string matches the Wave-1 cascade's parser contract.
    # Operator-visible recovery via INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED.
    if not dry_run:
        await _auto_cascade_validation_timeout(
            summary, pool, log=log, db_log=db_log,
        )

    # D11 — freshness vendor_late classification. Runs BEFORE the Wave-1
    # validation cascade so red freshness checks whose vendor hasn't
    # published anything newer are CLASSIFIED (not refreshed). The
    # freshness check stays red — D11 is a classification, not a
    # relaxation. INGESTION_VENDOR_LATE_SKIPPED surfaces the verdict.
    if not dry_run:
        await _auto_cascade_vendor_late(
            summary, pool, log=log, db_log=db_log,
        )

    # Wave-1 deterministic self-heal cascade (D6..D10) — parse the
    # data_validation red-check list + dispatch the canonical refresh
    # per check. See ``_auto_cascade_validation_failures`` docstring +
    # docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
    # expansion-design.md.
    if not dry_run:
        await _auto_cascade_validation_failures(
            summary, pool, daily_bars_config, log=log, db_log=db_log,
        )

    # Wave-2 deterministic self-heal cascade (D2 / D3 / D5 / D13) — fires
    # BEFORE the generic transient retry below so the Wave-2 recovery
    # shapes (chunked force_refresh, recycled pool, provider-auth retry-
    # then-skip, orchestrator-level connection-drop re-invoke) claim
    # their failures first. See ``_auto_cascade_stage_robustness``.
    if not dry_run:
        await _auto_cascade_stage_robustness(
            summary, pool, daily_bars_config, log=log, db_log=db_log,
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


async def _auto_cascade_coverage_collapse(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    daily_bars_config: dict[str, Any],
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """Auto-cascade daily_bars coverage_collapse → SIP/IEX force_refresh.

    Scans ``summary.stages`` for a FAILED ``daily_bars`` entry whose
    error message matches ``_DAILY_BARS_COVERAGE_COLLAPSE_TOKEN``. If
    found, picks the recovery feed via a live SIP-availability probe
    and runs ``_stage_daily_bars`` ONCE more with
    ``force_refresh=true`` (the actual recovery the operator runs
    manually — verified 2026-05-21 to restore full coverage when SIP
    entitlement is active).

    History — why force_refresh, not repair_gaps:

    * PR #227 (2026-05-20) wired the cascade trigger but cascaded to
      ``repair_gaps`` — which is BLIND to the coverage_collapse failure
      mode. The completeness check that drives repair_gaps' target list
      doesn't see partial sessions as "gaps" (the partial sessions
      don't meet its threshold for a "valid session"), so the cascade
      returned ``skipped: no_gaps_or_not_bars_fixable`` and proudly
      logged ``INGESTION_AUTO_RECOVERED`` while the data stayed broken
      at 7%. Operator-reproduced 2026-05-21 07:39 UTC.
    * The actual recovery: ``--stage daily_bars --param force_refresh
      =true --param universe=active --param feed=sip
      --param end_offset_days=1`` — which is what this cascade now does.

    Feed-selection logic (the fix this docstring exists for):

    * Live SIP probe (``_alpaca_sip_available``) — single 10s GET. Not
      cached. The operator observed the SIP entitlement flap inside a
      17-minute window 2026-05-21 04:38 → 04:55 UTC, so a cached "False"
      would freeze us in IEX-only mode through a recovery.
    * Probe True  → ``feed="sip"`` — full universe recovery available.
    * Probe False → ``feed="iex"`` — degraded recovery; IEX has fewer
      tickers than SIP, so coverage may still land below the floor
      after the cascade. That's still strictly better than no cascade,
      and the dedicated ``INGESTION_AUTO_RECOVERY_DEGRADED`` event
      flags it so the operator knows to investigate SIP entitlement.

    Behaviour:

    * One-shot — never loops. If the chosen-feed force_refresh ALSO
      fails entirely (no fetch landed), the stage stays FAILED with an
      ``INGESTION_AUTO_RECOVERY_FAILED`` escalation event.
    * Fires ONLY on coverage_collapse — not on auth/schema/other
      RuntimeError modes. The discriminator is the message token raised
      by the producer-self-validation block in ``_stage_daily_bars``.
    * Counts as ONE additional stage execution: the original
      ``daily_bars`` FAILED entry is REPLACED with the cascade result,
      annotated ``cascade=True`` + ``cascade_mode`` + ``feed``
      + ``first_error`` (mirrors ``_self_heal_failed_stages`` so the
      table reader is honest about what was healed vs first-try green).
    * Does NOT touch the Lab n_trials ledger — the data daemon never
      burns Lab trials.

    Logged events (engine=ops, run_id = summary.run_id):

    * ``INGESTION_AUTO_RECOVERY_START`` — cascade fires (data.feed
      records which feed was picked).
    * ``INGESTION_AUTO_RECOVERED`` — SIP probe passed, force_refresh
      feed=sip recovered to ≥ floor (stage now OK).
    * ``INGESTION_AUTO_RECOVERY_DEGRADED`` — SIP probe failed,
      force_refresh feed=iex ran but coverage stayed below floor (IEX
      subset only — partial recovery, operator investigation needed).
    * ``INGESTION_AUTO_RECOVERY_FAILED`` — both probe + fallback failed
      entirely (no fetch landed at all).
    """
    spec_by_name = {n: (n, fb, to) for n, fb, to in _STAGE_SPECS}
    token = _DAILY_BARS_COVERAGE_COLLAPSE_TOKEN.lower()
    for i, result in enumerate(summary.stages):
        if result.name != "daily_bars":
            continue
        if result.status != "FAILED":
            continue
        err = (result.error or "").lower()
        if token not in err:
            # Different failure mode (auth, schema drift, unknown) —
            # the cascade is coverage_collapse-only by design.
            continue
        if result.name not in spec_by_name:
            continue  # pragma: no cover — manifest invariant
        name, factory_builder, timeout = spec_by_name[result.name]
        first_error = (result.error or "")[:240]

        # Feed selection — the load-bearing decision this PR exists for.
        # Live probe, no cache. See docstring.
        try:
            sip_ok = await _alpaca_sip_available()
        except Exception as exc:  # noqa: BLE001 — probe must never break the cascade
            log.warning("ops.auto_cascade.probe_error", error=str(exc))
            sip_ok = False
        feed = "sip" if sip_ok else "iex"
        probe_reason = "sip_probe_ok" if sip_ok else "sip_probe_fail"

        await db_log.log(
            "INGESTION_AUTO_RECOVERY_START",
            (
                f"auto-cascade: daily_bars coverage_collapse → "
                f"force_refresh feed={feed}"
            ),
            severity="INFO",
            data={
                "stage": name,
                "cascade_mode": "force_refresh",
                "trigger": "coverage_collapse",
                "feed": feed,
                "reason": probe_reason,
                "first_error": first_error,
            },
        )
        log.info(
            "ops.auto_cascade.start",
            stage=name,
            cascade_mode="force_refresh",
            feed=feed,
            reason=probe_reason,
            first_error=first_error,
        )

        # Build the cascade config: same daily_bars_config base +
        # force_refresh=True + universe=active + the probe-selected feed
        # + a bounded lookback. Matches the operator-verified manual
        # recovery one-liner.
        cascade_config = {
            **daily_bars_config,
            "force_refresh": True,
            "universe": "active",
            "feed": feed,
            "lookback_days": int(daily_bars_config.get("lookback_days", 7)),
            "end_offset_days": int(daily_bars_config.get("end_offset_days", 1)),
        }
        cascade_result = await _run_stage(
            name,
            factory_builder(pool, cascade_config),
            log=log,
            db_log=db_log,
            dry_run=False,
            timeout=timeout,
        )

        # Annotate so STAGE SUMMARY / dashboard readers see this was an
        # auto-cascade, not a first-try result.
        cascade_result.detail = {
            **(cascade_result.detail or {}),
            "cascade": True,
            "cascade_mode": "force_refresh",
            "feed": feed,
            "sip_probe": sip_ok,
            "first_error": first_error,
        }
        summary.stages[i] = cascade_result

        # Outcome triage:
        #   OK                                 → RECOVERED (SIP) or
        #                                        DEGRADED (IEX — fetch
        #                                        landed but IEX has a
        #                                        narrower universe so
        #                                        coverage is partial by
        #                                        construction; the
        #                                        operator must
        #                                        investigate SIP)
        #   FAILED w/ coverage_collapse error  → DEGRADED (the fetch
        #                                        DID land, just landed
        #                                        below the floor —
        #                                        IEX-subset only; this
        #                                        is the honest
        #                                        partial-recovery
        #                                        outcome the spec
        #                                        names DEGRADED)
        #   FAILED w/ other error              → FAILED (no fetch
        #                                        landed: auth, network,
        #                                        schema drift, etc.)
        #   TIMEOUT                            → FAILED (no fetch
        #                                        landed in time)
        cascade_err_lower = (cascade_result.error or "").lower()
        coverage_collapse_again = token in cascade_err_lower
        if cascade_result.status == "OK" and sip_ok:
            await db_log.log(
                "INGESTION_AUTO_RECOVERED",
                (
                    "auto-cascade healed daily_bars coverage_collapse "
                    "via force_refresh feed=sip"
                ),
                severity="INFO",
                data={
                    "stage": name,
                    "cascade_mode": "force_refresh",
                    "feed": "sip",
                    "first_error": first_error,
                    "duration_ms": cascade_result.duration_ms,
                    **(cascade_result.detail or {}),
                },
            )
            log.info(
                "ops.auto_cascade.recovered",
                stage=name,
                feed="sip",
                duration_ms=cascade_result.duration_ms,
            )
        elif (
            (cascade_result.status == "OK" and not sip_ok)
            or (cascade_result.status == "FAILED" and coverage_collapse_again)
        ):
            # IEX path: either it landed OK (rare — IEX would have to
            # somehow cover the full active universe) or it landed but
            # the producer-self-validation refused on still-too-low
            # coverage. Both are the DEGRADED outcome: cascade DID run
            # an action; coverage may still be below floor on IEX.
            await db_log.log(
                "INGESTION_AUTO_RECOVERY_DEGRADED",
                (
                    f"auto-cascade ran with feed={feed} (sip_ok={sip_ok}); "
                    f"coverage may still be below floor — partial recovery"
                ),
                severity="WARNING",
                data={
                    "stage": name,
                    "cascade_mode": "force_refresh",
                    "feed": feed,
                    "reason": probe_reason,
                    "cascade_status": cascade_result.status,
                    "cascade_error": (cascade_result.error or "")[:240],
                    "first_error": first_error,
                    "duration_ms": cascade_result.duration_ms,
                    **(cascade_result.detail or {}),
                },
            )
            log.warning(
                "ops.auto_cascade.degraded",
                stage=name,
                feed=feed,
                cascade_status=cascade_result.status,
                duration_ms=cascade_result.duration_ms,
            )
        else:
            await db_log.log(
                "INGESTION_AUTO_RECOVERY_FAILED",
                (
                    f"auto-cascade FAILED: daily_bars coverage_collapse "
                    f"→ force_refresh feed={feed} did not recover "
                    f"(status={cascade_result.status})"
                ),
                severity="ERROR",
                data={
                    "stage": name,
                    "cascade_mode": "force_refresh",
                    "feed": feed,
                    "first_error": first_error,
                    "cascade_status": cascade_result.status,
                    "cascade_error": (cascade_result.error or "")[:240],
                    "duration_ms": cascade_result.duration_ms,
                },
            )
            log.error(
                "ops.auto_cascade.failed",
                stage=name,
                feed=feed,
                cascade_status=cascade_result.status,
                cascade_error=cascade_result.error,
            )
        # One-shot: do not iterate beyond the first match. (Only one
        # `daily_bars` entry exists per cmd_update cycle by manifest.)
        return


def _parse_failed_check_names(error_text: str | None) -> list[str]:
    """Extract failed check names from a data_validation RuntimeError.

    ``_stage_data_validation`` raises:
        RuntimeError(f"validation suite failed: {failed_names}")
    where ``failed_names`` is a Python ``list[str]`` repr. We use a
    permissive token extraction (single-quoted names) rather than
    ast.literal_eval — keeps the parser robust to the python repr's
    own punctuation drift and tolerant of truncation. Returns the empty
    list if the token can't be parsed, which is the safe degrade (the
    cascade is a NO-OP rather than mis-routing).
    """
    import re
    if not error_text:
        return []
    if _VALIDATION_SUITE_FAILED_TOKEN not in error_text.lower():
        return []
    # Capture quoted names. The repr emits 'fundamentals_quarterly_completeness'
    # with single quotes; allow double-quotes too for resilience.
    names = re.findall(r"['\"]([a-z][a-z0-9_]+)['\"]", error_text)
    # Drop duplicates while preserving order — same check name shouldn't
    # appear twice but be defensive.
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        if n not in seen:
            out.append(n)
            seen.add(n)
    return out


async def _auto_cascade_validation_failures(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    daily_bars_config: dict[str, Any],
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """Deterministic auto-cascade for ``data_validation`` red checks (D6..D10).

    Spec: ``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
    expansion-design.md``. Wave-1 rows:

    * **D6** validation-suite-partial-failure → parse the red-check list,
      dispatch the canonical refresh stage per check.
    * **D7** ``*_monotone`` red → run ``dedupe_monotone`` stage first
      (cleans rogue rows per spec §4 Q3: latest event_date, recorded_at
      tiebreaker) then the canonical refresh stage.
    * **D8** ``macro_indicators_completeness`` red → per-indicator
      targeted FRED re-pull via ``per_indicator_fred_repull`` for the
      affected series + window from ``compute_macro_repair_targets``.
    * **D9** ``liquidity_tiers_completeness`` red → ``tier_refresh``
      with ``skip_guard_days=0``. The targeted-ticker list from
      ``compute_liquidity_tiers_repair_targets`` is carried in the
      cascade telemetry (advisory until tier_refresh gains --tickers).
    * **D10** ``ticker_classifications_coverage`` red → ``classify_tickers``
      with ``force=True, skip_guard_days=0``.

    Logged events (engine=ops, run_id = summary.run_id):

    * ``INGESTION_AUTO_RECOVERED_VALIDATION`` — D6 generic completeness
      recovery (fundamentals/corp-actions/earnings/sec/etc.).
    * ``INGESTION_AUTO_RECOVERED_MONOTONE`` — D7 dedupe-then-refresh.
    * ``INGESTION_AUTO_RECOVERED_MACRO_GAP`` — D8 per-indicator pull.
    * ``INGESTION_AUTO_RECOVERED_TIER`` — D9 liquidity tier refresh.
    * ``INGESTION_AUTO_RECOVERED_CLASSIFICATION`` — D10 classify-tickers.
    * ``INGESTION_AUTO_RECOVERY_VALIDATION_SKIPPED`` — failed-check name
      not in the cascade map (long-tail; LLM-side backstop catches it).

    Behaviour:

    * One-shot per check name within a cycle (no loops).
    * Does NOT re-run ``data_validation`` after recovery — the next
      cmd_update cycle's validation suite is the authoritative gate.
    * Cascade NEVER raises into the caller; logs + returns on any
      sub-cascade error so the daemon stays alive (matches the
      ``_auto_cascade_coverage_collapse`` invariant).
    """
    # Locate the data_validation FAILED entry, if any.
    val_idx: int | None = None
    val_result: StageResult | None = None
    for i, result in enumerate(summary.stages):
        if result.name != "data_validation":
            continue
        if result.status != "FAILED":
            continue
        if _VALIDATION_SUITE_FAILED_TOKEN not in (result.error or "").lower():
            continue
        val_idx = i
        val_result = result
        break
    if val_idx is None or val_result is None:
        return

    failed_checks = _parse_failed_check_names(val_result.error)
    if not failed_checks:
        log.warning(
            "ops.auto_cascade.validation.parse_failed",
            error=(val_result.error or "")[:240],
        )
        return

    # D11 — honour the vendor_late classification added by
    # ``_auto_cascade_vendor_late``. Checks in this set have already
    # been classified VENDOR_LATE (their distinct INGESTION_VENDOR_LATE_
    # SKIPPED event landed) — refreshing them would be useless churn
    # (the vendor has nothing newer than we hold). The freshness check
    # remains red in the summary; D11 is a classification, not a
    # relaxation.
    vendor_late_set: set[str] = set(
        (val_result.detail or {}).get("vendor_late_checks", []) or []
    )

    spec_by_name = {n: (n, fb, to) for n, fb, to in _STAGE_SPECS}

    await db_log.log(
        "INGESTION_AUTO_RECOVERY_START",
        f"validation cascade: {len(failed_checks)} red check(s)",
        severity="INFO",
        data={
            "stage": "data_validation",
            "cascade_mode": "validation_failures",
            "failed_checks": failed_checks,
            "vendor_late_skipped": sorted(vendor_late_set),
        },
    )
    log.info(
        "ops.auto_cascade.validation.start",
        failed_checks=failed_checks,
        vendor_late_skipped=sorted(vendor_late_set),
    )

    handled: list[str] = []
    skipped: list[str] = []
    vendor_late_handled: list[str] = []

    for check_name in failed_checks:
        # D11 — vendor_late classification claims this check; skip the
        # canonical refresh dispatch entirely. The distinct
        # INGESTION_VENDOR_LATE_SKIPPED event already landed.
        if check_name in vendor_late_set:
            vendor_late_handled.append(check_name)
            continue

        # D8 — macro: per-indicator targeted re-pull (NOT the full
        # macro_indicators stage; per spec §1 D8).
        if check_name == _MACRO_COMPLETENESS_CHECK:
            try:
                from tpcore.fred import per_indicator_fred_repull
                from tpcore.quality.validation.checks.macro_indicators_completeness import (
                    compute_macro_repair_targets,
                )
                indicators, lookback_days = await compute_macro_repair_targets(pool)
                if not indicators:
                    log.info(
                        "ops.auto_cascade.macro_gap.no_targets",
                        check=check_name,
                    )
                    skipped.append(check_name)
                    continue
                start_d: date | None = None
                if lookback_days > 0:
                    start_d = date.today() - timedelta(days=int(lookback_days))  # noqa: DTZ011
                results = await per_indicator_fred_repull(
                    pool, indicators, start=start_d,
                )
                await db_log.log(
                    "INGESTION_AUTO_RECOVERED_MACRO_GAP",
                    (
                        f"macro per-indicator re-pull: "
                        f"{len(indicators)} indicator(s) "
                        f"(lookback={lookback_days}d)"
                    ),
                    severity="INFO",
                    data={
                        "check": check_name,
                        "indicators": indicators,
                        "lookback_days": int(lookback_days),
                        "per_indicator_rows": results,
                    },
                )
                log.info(
                    "ops.auto_cascade.macro_gap.recovered",
                    indicators=indicators,
                    rows_per_indicator=results,
                )
                handled.append(check_name)
            except Exception as exc:  # noqa: BLE001 — never crash the cycle
                log.error(
                    "ops.auto_cascade.macro_gap.failed",
                    check=check_name, error=str(exc),
                )
                skipped.append(check_name)
            continue

        # D7 — monotone violations: dedupe THEN canonical refresh.
        if check_name in _MONOTONE_CASCADE_MAP:
            target_table, refresh_stage, refresh_params = (
                _MONOTONE_CASCADE_MAP[check_name]
            )
            # Phase 1 — dedupe rogue rows.
            try:
                dedupe_out = await _stage_dedupe_monotone(
                    pool, {"table": target_table},
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "ops.auto_cascade.monotone.dedupe_failed",
                    check=check_name, table=target_table, error=str(exc),
                )
                dedupe_out = {}
            # Phase 2 — canonical refresh (force past skip-guard).
            refresh_outcome = await _invoke_cascade_stage(
                refresh_stage,
                refresh_params,
                daily_bars_config,
                spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            await db_log.log(
                "INGESTION_AUTO_RECOVERED_MONOTONE",
                (
                    f"monotone cascade {check_name}: "
                    f"dedupe + {refresh_stage}"
                ),
                severity="INFO",
                data={
                    "check": check_name,
                    "target_table": target_table,
                    "dedupe": dedupe_out,
                    "refresh_stage": refresh_stage,
                    "refresh_status": refresh_outcome.get("status"),
                    "refresh_error": refresh_outcome.get("error"),
                },
            )
            log.info(
                "ops.auto_cascade.monotone.recovered",
                check=check_name, dedupe=dedupe_out,
                refresh=refresh_outcome,
            )
            handled.append(check_name)
            continue

        # D9 — liquidity_tiers_completeness: tier_refresh + carry the
        # targeted-ticker list as telemetry.
        if check_name == "liquidity_tiers_completeness":
            try:
                from tpcore.quality.validation.checks.liquidity_tiers_completeness import (
                    compute_liquidity_tiers_repair_targets,
                )
                missing = await compute_liquidity_tiers_repair_targets(pool)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ops.auto_cascade.tier.target_compute_failed",
                    error=str(exc),
                )
                missing = []
            stage_name, params = _VALIDATION_CASCADE_MAP[check_name]
            refresh_outcome = await _invoke_cascade_stage(
                stage_name, params, daily_bars_config,
                spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            await db_log.log(
                "INGESTION_AUTO_RECOVERED_TIER",
                (
                    f"tier_refresh cascade: {len(missing)} missing ticker(s) "
                    f"(advisory)"
                ),
                severity="INFO",
                data={
                    "check": check_name,
                    "missing_tickers_count": len(missing),
                    "missing_tickers_sample": missing[:25],
                    "refresh_stage": stage_name,
                    "refresh_status": refresh_outcome.get("status"),
                    "refresh_error": refresh_outcome.get("error"),
                },
            )
            log.info(
                "ops.auto_cascade.tier.recovered",
                check=check_name,
                missing_tickers=len(missing),
                refresh=refresh_outcome,
            )
            handled.append(check_name)
            continue

        # D10 — ticker_classifications_coverage: classify_tickers + force.
        if check_name == "ticker_classifications_coverage":
            stage_name, params = _VALIDATION_CASCADE_MAP[check_name]
            refresh_outcome = await _invoke_cascade_stage(
                stage_name, params, daily_bars_config,
                spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            await db_log.log(
                "INGESTION_AUTO_RECOVERED_CLASSIFICATION",
                "classify_tickers cascade: force re-sync",
                severity="INFO",
                data={
                    "check": check_name,
                    "refresh_stage": stage_name,
                    "refresh_status": refresh_outcome.get("status"),
                    "refresh_error": refresh_outcome.get("error"),
                },
            )
            log.info(
                "ops.auto_cascade.classification.recovered",
                check=check_name, refresh=refresh_outcome,
            )
            handled.append(check_name)
            continue

        # D6 — generic completeness check → canonical refresh stage.
        if check_name in _VALIDATION_CASCADE_MAP:
            stage_name, params = _VALIDATION_CASCADE_MAP[check_name]
            refresh_outcome = await _invoke_cascade_stage(
                stage_name, params, daily_bars_config,
                spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            await db_log.log(
                "INGESTION_AUTO_RECOVERED_VALIDATION",
                f"validation cascade {check_name} → {stage_name}",
                severity="INFO",
                data={
                    "check": check_name,
                    "refresh_stage": stage_name,
                    "refresh_status": refresh_outcome.get("status"),
                    "refresh_error": refresh_outcome.get("error"),
                },
            )
            log.info(
                "ops.auto_cascade.validation.recovered",
                check=check_name, stage=stage_name,
                refresh=refresh_outcome,
            )
            handled.append(check_name)
            continue

        # Unknown check — long-tail; LLM-side backstop will see it.
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_VALIDATION_SKIPPED",
            f"no deterministic cascade for {check_name}",
            severity="WARNING",
            data={
                "check": check_name,
                "note": "long-tail — LLM-side backstop handles this",
            },
        )
        log.info(
            "ops.auto_cascade.validation.skipped_unmapped",
            check=check_name,
        )
        skipped.append(check_name)

    # Annotate the data_validation stage result so dashboard readers see
    # which checks were auto-recovered vs deferred to the LLM backstop.
    # ``vendor_late`` carries the D11 classification (claimed-by-D11
    # checks did NOT dispatch a refresh — they got the distinct
    # INGESTION_VENDOR_LATE_SKIPPED event instead).
    val_result.detail = {
        **(val_result.detail or {}),
        "cascade": True,
        "cascade_mode": "validation_failures",
        "failed_checks": failed_checks,
        "handled": handled,
        "skipped": skipped,
        "vendor_late": vendor_late_handled,
    }
    summary.stages[val_idx] = val_result


async def _invoke_cascade_stage(
    stage_name: str,
    extra_params: dict[str, Any],
    daily_bars_config: dict[str, Any],
    spec_by_name: dict[str, tuple],
    *,
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> dict[str, Any]:
    """Helper: run a single stage by name in the validation cascade.

    Returns ``{"status": "...", "error": "...", "duration_ms": int}``
    so the cascade telemetry can report the outcome without bubbling an
    exception. The actual stage run uses ``_run_stage`` so the
    structured INGESTION_START/COMPLETE/FAILED events are emitted on
    the cascade run too — matches the coverage_collapse cascade.
    """
    if stage_name not in spec_by_name:
        log.error(
            "ops.auto_cascade.unknown_stage",
            stage=stage_name,
        )
        return {"status": "UNKNOWN_STAGE", "error": stage_name, "duration_ms": 0}
    name, factory_builder, timeout = spec_by_name[stage_name]
    cascade_config = {**daily_bars_config, **extra_params}
    result = await _run_stage(
        name,
        factory_builder(pool, cascade_config),
        log=log,
        db_log=db_log,
        dry_run=False,
        timeout=timeout,
    )
    return {
        "status": result.status,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


async def _auto_cascade_stage_robustness(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    daily_bars_config: dict[str, Any],
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """Wave-2 deterministic self-heal — D2 / D3 / D5 / D13.

    Spec: ``docs/superpowers/specs/2026-05-21-deterministic-self-heal-
    coverage-expansion-design.md`` §1 rows D2 D3 D5 D13 + §4 ANSWERED.

    Iterates the summary's FAILED/TIMEOUT stage entries and dispatches at
    most ONE Wave-2 cascade per entry (the rows are mutually exclusive
    by detection — a timeout is not an auth-401, etc.). The cascade fires
    BEFORE ``_self_heal_failed_stages`` so each Wave-2 row claims its
    failure with the row-specific recovery shape, not the generic
    same-config retry. Each cascade is ONE-SHOT: a re-fail on the
    cascade attempt does NOT loop — the stage stays FAILED and the
    operator-surfacing event is emitted.

    Rows + events:

    * **D2** ``daily_bars`` stage TIMEOUT on a non-chunked invocation
      → re-invoke with ``force_refresh=True universe=active feed=sip
      end_offset_days=1`` (the chunked path from PR #236). Event:
      ``INGESTION_AUTO_RECOVERED_TIMEOUT``.
    * **D3** any stage with "connection was closed in the middle of
      operation" (or sibling driver-drop shapes) → re-invoke the same
      stage ONCE with the same config. Event:
      ``INGESTION_AUTO_RECOVERED_CONNDROP``. Per-chunk retry (PR #163)
      already handles in-flight drops; this row catches the
      orchestrator-level drop where the whole stage tipped over.
    * **D5** any stage with HTTP 401 in the error string → retry the
      stage ONCE with the same config (transient creds-cycle
      assumption). On the second 401 we emit
      ``PROVIDER_AUTH_ESCALATED`` (FAILED-shape) + leave the stage
      FAILED but the daemon stays alive (key §4-Q2 invariant: the
      daemon NEVER aborts on a 401; operator rotates creds on their own
      cadence). Event: ``PROVIDER_AUTH_ESCALATED``.
    * **D13** any stage with asyncpg pool-exhaustion tokens
      (``TooManyConnectionsError`` / ``PostgresConnectionError`` /
      ``PoolTimeout`` / "connection slots are reserved") → recycle the
      daemon's LOCAL pool (close + rebuild via
      ``tpcore.db.recycle_asyncpg_pool``) + retry the stage ONCE
      against the fresh pool. Sibling-process pools (engine_service,
      lane-service) are NOT touched — that scope-down is documented
      in the helper's docstring. Event: ``POOL_CIRCUIT_BREAKER_TRIPPED``.

    Pin: unknown error shapes (random RuntimeError, schema-drift, etc.)
    do NOT trigger any Wave-2 cascade and are passed through to
    ``_self_heal_failed_stages`` / the operator-facing failure path
    unchanged.
    """
    spec_by_name = {n: (n, fb, to) for n, fb, to in _STAGE_SPECS}
    for i, result in enumerate(summary.stages):
        if result.status not in {"FAILED", "TIMEOUT"}:
            continue
        if result.name not in spec_by_name:
            continue
        err_text = result.error or ""
        # Order matters: pool exhaustion + connection drop must precede
        # the generic timeout match (a pool-acquire timeout can stringify
        # with "timeout" alongside "PoolTimeout" — we want D13 to claim
        # it, not D2). Auth-401 is independent.
        if _matches_any(err_text, _POOL_EXHAUSTION_TOKENS):
            await _cascade_d13_pool_exhaustion(
                summary, i, result, daily_bars_config, spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            continue
        if _matches_any(err_text, _CONNECTION_DROP_TOKENS):
            await _cascade_d3_connection_drop(
                summary, i, result, daily_bars_config, spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            continue
        if _matches_any(err_text, _AUTH_401_TOKENS):
            await _cascade_d5_provider_auth(
                summary, i, result, daily_bars_config, spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            continue
        # D2 — timeout cascade is daily_bars-specific (chunked
        # force_refresh is the recovery shape only that stage supports).
        # Other stages with a generic timeout fall through to
        # `_self_heal_failed_stages` which retries with the same config.
        if (
            result.name == "daily_bars"
            and result.status == "TIMEOUT"
            and _matches_any(err_text, _TIMEOUT_TOKENS)
            and not bool(daily_bars_config.get("force_refresh", False))
        ):
            await _cascade_d2_daily_bars_timeout(
                summary, i, result, daily_bars_config, spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            continue


async def _cascade_d2_daily_bars_timeout(
    summary: UpdateSummary,
    idx: int,
    result: StageResult,
    daily_bars_config: dict[str, Any],
    spec_by_name: dict[str, tuple],
    *,
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """D2 — daily_bars non-chunked timeout → chunked force_refresh re-invoke.

    Mirrors the operator-verified manual recovery: ``--stage daily_bars
    --param force_refresh=true --param universe=active --param feed=sip
    --param end_offset_days=1``. The chunked path inside ``_stage_daily_bars``
    (PR #236) splits the universe into 500-ticker slices so the whole
    invocation stays under the 3600s stage-timeout ceiling.
    """
    name, factory_builder, timeout = spec_by_name[result.name]
    first_error = (result.error or "")[:240]
    await db_log.log(
        "INGESTION_AUTO_RECOVERY_START",
        "D2 cascade: daily_bars timeout → chunked force_refresh feed=sip",
        severity="INFO",
        data={
            "stage": name,
            "cascade_mode": "force_refresh_chunked",
            "trigger": "timeout",
            "first_error": first_error,
        },
    )
    log.info(
        "ops.auto_cascade.d2_timeout.start",
        stage=name, first_error=first_error,
    )
    cascade_config = {
        **daily_bars_config,
        "force_refresh": True,
        "universe": "active",
        "feed": "sip",
        "end_offset_days": int(daily_bars_config.get("end_offset_days", 1)),
    }
    cascade_result = await _run_stage(
        name,
        factory_builder(pool, cascade_config),
        log=log,
        db_log=db_log,
        dry_run=False,
        timeout=timeout,
    )
    cascade_result.detail = {
        **(cascade_result.detail or {}),
        "cascade": True,
        "cascade_mode": "force_refresh_chunked",
        "trigger": "timeout",
        "first_error": first_error,
    }
    summary.stages[idx] = cascade_result
    if cascade_result.status == "OK":
        await db_log.log(
            "INGESTION_AUTO_RECOVERED_TIMEOUT",
            (
                "D2 cascade recovered: daily_bars timeout → "
                "chunked force_refresh feed=sip OK"
            ),
            severity="INFO",
            data={
                "stage": name,
                "cascade_mode": "force_refresh_chunked",
                "first_error": first_error,
                "duration_ms": cascade_result.duration_ms,
                **(cascade_result.detail or {}),
            },
        )
        log.info(
            "ops.auto_cascade.d2_timeout.recovered",
            stage=name, duration_ms=cascade_result.duration_ms,
        )
    else:
        # One-shot: the cascade itself failed. Leave it FAILED; do NOT
        # loop. The operator-facing INGESTION_FAILED row from _run_stage
        # already landed; we just add the cascade-failed escalation.
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_FAILED",
            (
                f"D2 cascade FAILED: daily_bars timeout → chunked "
                f"force_refresh did not recover "
                f"(status={cascade_result.status})"
            ),
            severity="ERROR",
            data={
                "stage": name,
                "cascade_mode": "force_refresh_chunked",
                "first_error": first_error,
                "cascade_status": cascade_result.status,
                "cascade_error": (cascade_result.error or "")[:240],
                "duration_ms": cascade_result.duration_ms,
            },
        )
        log.error(
            "ops.auto_cascade.d2_timeout.failed",
            stage=name, cascade_status=cascade_result.status,
            cascade_error=cascade_result.error,
        )


async def _cascade_d3_connection_drop(
    summary: UpdateSummary,
    idx: int,
    result: StageResult,
    daily_bars_config: dict[str, Any],
    spec_by_name: dict[str, tuple],
    *,
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """D3 — orchestrator-level connection-drop re-invoke (one-shot).

    Per-chunk drops are already covered by PR #163's transient-retry
    inside the data adapters; this row catches the case where the WHOLE
    stage's outermost connection tipped over (e.g. asyncpg-level pool
    disconnect mid-batch on a 30-min Lab statement). One re-invoke with
    the same config; if THAT also drops, the stage stays FAILED and
    bubbles to the existing INGESTION_FAILED path — no looping.
    """
    name, factory_builder, timeout = spec_by_name[result.name]
    first_error = (result.error or "")[:240]
    await db_log.log(
        "INGESTION_AUTO_RECOVERY_START",
        f"D3 cascade: {name} connection-drop → one-shot re-invoke",
        severity="INFO",
        data={
            "stage": name,
            "cascade_mode": "conndrop_reinvoke",
            "trigger": "connection_drop",
            "first_error": first_error,
        },
    )
    log.info(
        "ops.auto_cascade.d3_conndrop.start",
        stage=name, first_error=first_error,
    )
    cascade_result = await _run_stage(
        name,
        factory_builder(pool, daily_bars_config),
        log=log,
        db_log=db_log,
        dry_run=False,
        timeout=timeout,
    )
    cascade_result.detail = {
        **(cascade_result.detail or {}),
        "cascade": True,
        "cascade_mode": "conndrop_reinvoke",
        "trigger": "connection_drop",
        "first_error": first_error,
    }
    summary.stages[idx] = cascade_result
    if cascade_result.status == "OK":
        await db_log.log(
            "INGESTION_AUTO_RECOVERED_CONNDROP",
            f"D3 cascade recovered: {name} re-invoke OK after conn-drop",
            severity="INFO",
            data={
                "stage": name,
                "cascade_mode": "conndrop_reinvoke",
                "first_error": first_error,
                "duration_ms": cascade_result.duration_ms,
                **(cascade_result.detail or {}),
            },
        )
        log.info(
            "ops.auto_cascade.d3_conndrop.recovered",
            stage=name, duration_ms=cascade_result.duration_ms,
        )
    else:
        # Cascade re-fail: do NOT loop. The stage stays FAILED; the
        # INGESTION_FAILED row from _run_stage already landed.
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_FAILED",
            (
                f"D3 cascade FAILED: {name} conndrop re-invoke did not "
                f"recover (status={cascade_result.status})"
            ),
            severity="ERROR",
            data={
                "stage": name,
                "cascade_mode": "conndrop_reinvoke",
                "first_error": first_error,
                "cascade_status": cascade_result.status,
                "cascade_error": (cascade_result.error or "")[:240],
                "duration_ms": cascade_result.duration_ms,
            },
        )
        log.error(
            "ops.auto_cascade.d3_conndrop.failed",
            stage=name, cascade_status=cascade_result.status,
            cascade_error=cascade_result.error,
        )


async def _cascade_d5_provider_auth(
    summary: UpdateSummary,
    idx: int,
    result: StageResult,
    daily_bars_config: dict[str, Any],
    spec_by_name: dict[str, tuple],
    *,
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """D5 — provider 401: retry-once, then escalate-but-don't-abort.

    Per spec §4 Q2 ANSWERED: daemon STAYS ALIVE. One retry covers the
    transient creds-cycle case (provider rotated a key mid-rate-limit-
    cycle; the next request would succeed). A second 401 confirms the
    creds are bad and we emit ``PROVIDER_AUTH_ESCALATED`` so the
    operator sees the escalation in ``application_log`` and rotates on
    their own cadence — NO operator-blocking task, NO daemon abort.
    The stage stays FAILED in the summary; subsequent stages keep running.
    """
    name, factory_builder, timeout = spec_by_name[result.name]
    first_error = (result.error or "")[:240]
    # Identify the provider best-effort from the stage name. Used in
    # the escalation event so the operator's rotate runbook can target
    # the right vendor.
    provider = _infer_provider_from_stage(name)
    await db_log.log(
        "INGESTION_AUTO_RECOVERY_START",
        f"D5 cascade: {name} 401 → one-shot retry (provider={provider})",
        severity="INFO",
        data={
            "stage": name,
            "cascade_mode": "auth_retry",
            "trigger": "auth_401",
            "provider": provider,
            "first_error": first_error,
        },
    )
    log.info(
        "ops.auto_cascade.d5_auth.start",
        stage=name, provider=provider, first_error=first_error,
    )
    retry_result = await _run_stage(
        name,
        factory_builder(pool, daily_bars_config),
        log=log,
        db_log=db_log,
        dry_run=False,
        timeout=timeout,
    )
    retry_result.detail = {
        **(retry_result.detail or {}),
        "cascade": True,
        "cascade_mode": "auth_retry",
        "trigger": "auth_401",
        "provider": provider,
        "first_error": first_error,
    }
    summary.stages[idx] = retry_result
    retry_err_text = retry_result.error or ""
    second_401 = (
        retry_result.status != "OK"
        and _matches_any(retry_err_text, _AUTH_401_TOKENS)
    )
    if retry_result.status == "OK":
        await db_log.log(
            "INGESTION_AUTO_RECOVERED_AUTH",
            f"D5 cascade recovered: {name} 401 retry OK",
            severity="INFO",
            data={
                "stage": name,
                "cascade_mode": "auth_retry",
                "provider": provider,
                "first_error": first_error,
                "duration_ms": retry_result.duration_ms,
            },
        )
        log.info(
            "ops.auto_cascade.d5_auth.recovered",
            stage=name, provider=provider,
            duration_ms=retry_result.duration_ms,
        )
    elif second_401:
        # Confirmed bad creds — emit the operator-surfacing escalation.
        # The daemon stays alive; subsequent stages still run.
        # The stage's failed-URL snippet (or its first line) is carried
        # for the operator's rotate runbook.
        failed_url_snippet = _extract_failed_url_snippet(retry_err_text)
        await db_log.log(
            "PROVIDER_AUTH_ESCALATED",
            (
                f"D5 cascade ESCALATED: {name} second 401 on retry "
                f"(provider={provider}) — operator must rotate creds; "
                f"daemon CONTINUING"
            ),
            severity="ERROR",
            data={
                "stage": name,
                "provider": provider,
                "first_error": first_error,
                "retry_error": retry_err_text[:240],
                "failed_url_snippet": failed_url_snippet,
                "cascade_mode": "auth_retry",
                "daemon_continuing": True,
            },
        )
        log.error(
            "ops.auto_cascade.d5_auth.escalated",
            stage=name, provider=provider,
            retry_error=retry_err_text,
        )
    else:
        # Retry failed with a NON-401 error — different failure mode on
        # the retry. The stage stays FAILED with the cascade annotation;
        # other Wave-2 / self-heal stages will see the new error shape
        # on their own pass (we only ran the auth-retry path).
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_FAILED",
            (
                f"D5 cascade: {name} retry FAILED with non-401 error — "
                f"NOT escalated as auth"
            ),
            severity="ERROR",
            data={
                "stage": name,
                "cascade_mode": "auth_retry",
                "provider": provider,
                "first_error": first_error,
                "retry_status": retry_result.status,
                "retry_error": retry_err_text[:240],
            },
        )
        log.warning(
            "ops.auto_cascade.d5_auth.retry_other_error",
            stage=name, provider=provider,
            retry_error=retry_err_text,
        )


async def _cascade_d13_pool_exhaustion(
    summary: UpdateSummary,
    idx: int,
    result: StageResult,
    daily_bars_config: dict[str, Any],
    spec_by_name: dict[str, tuple],
    *,
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """D13 — pool exhaustion: close+reopen LOCAL pool + retry stage once.

    Scope-down per spec: this resets ONLY the daemon's local pool. Sibling
    processes (engine_service, lane-service) hold their own pools — those
    are NOT touched. A cross-process pool reset would
    need IPC fencing + graceful drain semantics that are out of scope for
    a single-row Wave-2 cascade. The cascade emits
    ``POOL_CIRCUIT_BREAKER_TRIPPED`` so the operator can see if the
    exhaustion was a daemon-local burst (probably benign — sticky cleanup)
    vs. a system-wide capacity wall (needs Supabase quota investigation).

    Implementation: ``recycle_asyncpg_pool`` builds a FRESH pool with the
    same shape against DATABASE_URL, closes the old one. The stage-retry
    runs against the fresh pool. The caller's reference to the original
    pool is unchanged (asyncpg.Pool isn't reassignable from inside a
    helper); the old pool stays closed in the background. Subsequent
    stages in the *same* cmd_update cycle continue to use the original
    pool reference — closed. THAT IS THE LIMIT of this row's blast radius:
    we trade one stage's recovery against the rest of the cycle's
    likelihood of immediately hitting the same exhaustion on the closed
    pool reference (which will surface as a fresh `pool is closed` error
    and itself be handled). The next cmd_update cycle (`amain`) builds a
    fresh pool from scratch so the limit is bounded to the rest of THIS
    cycle.

    The cascade is best-effort: any exception inside the recycle/retry
    is caught and logged so the cascade NEVER raises into the daemon
    loop (matching the `_auto_cascade_coverage_collapse` invariant).
    """
    name, factory_builder, timeout = spec_by_name[result.name]
    first_error = (result.error or "")[:240]
    await db_log.log(
        "POOL_CIRCUIT_BREAKER_TRIPPED",
        (
            f"D13 cascade: {name} pool exhaustion → recycle LOCAL pool + "
            f"retry stage once (sibling processes untouched)"
        ),
        severity="WARNING",
        data={
            "stage": name,
            "cascade_mode": "pool_recycle",
            "trigger": "pool_exhaustion",
            "first_error": first_error,
            "scope": "daemon_local_pool_only",
        },
    )
    log.warning(
        "ops.auto_cascade.d13_pool.tripped",
        stage=name, first_error=first_error,
    )

    fresh_pool: asyncpg.Pool | None = None
    try:
        from tpcore.db import recycle_asyncpg_pool
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            log.error(
                "ops.auto_cascade.d13_pool.no_database_url",
                stage=name,
            )
            return
        fresh_pool = await recycle_asyncpg_pool(pool, db_url, max_size=4)
    except Exception as exc:  # noqa: BLE001 — recycle must never crash daemon
        log.error(
            "ops.auto_cascade.d13_pool.recycle_failed",
            stage=name, error=str(exc),
        )
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_FAILED",
            f"D13 cascade: pool recycle itself failed ({type(exc).__name__})",
            severity="ERROR",
            data={
                "stage": name,
                "cascade_mode": "pool_recycle",
                "first_error": first_error,
                "recycle_error": str(exc)[:240],
            },
        )
        return

    try:
        retry_result = await _run_stage(
            name,
            factory_builder(fresh_pool, daily_bars_config),
            log=log,
            db_log=db_log,
            dry_run=False,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — never crash on cascade retry
        log.error(
            "ops.auto_cascade.d13_pool.retry_exception",
            stage=name, error=str(exc),
        )
        # Close the fresh pool best-effort before returning.
        try:
            await fresh_pool.close()
        except Exception:  # noqa: BLE001
            pass
        return

    retry_result.detail = {
        **(retry_result.detail or {}),
        "cascade": True,
        "cascade_mode": "pool_recycle",
        "trigger": "pool_exhaustion",
        "first_error": first_error,
    }
    summary.stages[idx] = retry_result

    # The fresh pool only holds queries from THIS stage retry. The
    # rest of the cycle uses the original (now-closed) pool reference.
    # Close the fresh pool so we don't leak connection budget; the next
    # cmd_update cycle's amain builds a brand-new pool from scratch.
    try:
        await fresh_pool.close()
    except Exception:  # noqa: BLE001
        pass

    if retry_result.status == "OK":
        await db_log.log(
            "INGESTION_AUTO_RECOVERED_POOL",
            f"D13 cascade recovered: {name} OK against recycled pool",
            severity="INFO",
            data={
                "stage": name,
                "cascade_mode": "pool_recycle",
                "first_error": first_error,
                "duration_ms": retry_result.duration_ms,
            },
        )
        log.info(
            "ops.auto_cascade.d13_pool.recovered",
            stage=name, duration_ms=retry_result.duration_ms,
        )
    else:
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_FAILED",
            (
                f"D13 cascade FAILED: {name} retry against recycled pool "
                f"did not recover (status={retry_result.status})"
            ),
            severity="ERROR",
            data={
                "stage": name,
                "cascade_mode": "pool_recycle",
                "first_error": first_error,
                "retry_status": retry_result.status,
                "retry_error": (retry_result.error or "")[:240],
            },
        )
        log.error(
            "ops.auto_cascade.d13_pool.retry_failed",
            stage=name, retry_status=retry_result.status,
            retry_error=retry_result.error,
        )


# Best-effort mapping from a stage name → its primary external provider
# string. Used in the D5 PROVIDER_AUTH_ESCALATED event so the operator's
# rotate-credentials runbook knows which vendor to target. An unmapped
# stage returns "unknown" — the event still lands.
_STAGE_PROVIDER_MAP: dict[str, str] = {
    "daily_bars": "alpaca",
    "corporate_actions": "fmp",
    "fundamentals_refresh": "fmp",
    "earnings_refresh": "fmp",
    "historical_earnings_events_t1_t2": "fmp",
    "historical_fundamentals_quarterly": "fmp",
    "historical_macro_indicators": "fred",
    "rebuild_corporate_actions_from_archive": "csv_archive",
    "sec_filings": "sec",
    "macro_indicators": "fred",
    "finnhub_insider_sentiment": "finnhub",
    "apewisdom_social_sentiment": "apewisdom",
    "fear_greed": "cnn",
    "aaii_sentiment": "aaii",
    "finra_short_interest": "finra",
    "iborrowdesk_borrow_rates": "iborrowdesk",
    "greeks_max_pain": "alpaca",
    "classify_tickers": "fmp",
    "tier_refresh": "alpaca",
}


def _infer_provider_from_stage(stage_name: str) -> str:
    """Map stage_name → external provider (best-effort, defaults to 'unknown')."""
    return _STAGE_PROVIDER_MAP.get(stage_name, "unknown")


def _extract_failed_url_snippet(error_text: str) -> str:
    """Pull the first URL-shaped token out of an error string, bounded.

    Used in PROVIDER_AUTH_ESCALATED so the operator can confirm WHICH
    endpoint 401'd (e.g. paper-vs-live, data-vs-trading) and rotate the
    right key. Returns an empty string if no URL is found.
    """
    import re
    if not error_text:
        return ""
    m = re.search(r"https?://[^\s'\"<>]+", error_text)
    return m.group(0)[:200] if m else ""


# ─────────────────────────────────────────────────────────────────────────
# D14 — data_validation chunking helper + cascade
# ─────────────────────────────────────────────────────────────────────────


async def _chunk_validation_suite(
    pool: asyncpg.Pool,
    *,
    log: structlog.stdlib.BoundLogger,
    chunk_budget_sec: float = _VALIDATION_CHUNK_BUDGET_SEC,
) -> dict[str, Any]:
    """Run the data_validation suite chunked, each chunk under its own budget.

    Per spec D14: the monolithic ``_stage_data_validation`` hits the 300s
    stage timeout before the 25-check suite returns. This helper re-runs
    the same checks split into smaller sub-stages (see
    ``_VALIDATION_CHUNK_SPECS``), each with its own ``chunk_budget_sec``
    budget. The aggregate failed-check list is returned in the same shape
    the Wave-1 validation cascade already consumes — a list of check
    names that the existing parser (``_parse_failed_check_names``) and
    the existing dispatcher (``_auto_cascade_validation_failures``)
    handle without contract change.

    Returns:
        {
            "failed_checks": list[str],         # aggregate failed-check names
            "chunks": list[dict],               # per-chunk telemetry
            "total_duration_ms": int,           # wall-clock sum
            "any_chunk_timed_out": bool,        # at least one chunk hit budget
        }

    Per-chunk timeout semantics: when a chunk hits its budget the helper
    treats EVERY check inside it as failed (their names join the
    aggregate failed-check list). That is the safe-degrade — the
    operator-facing application_log row carries the per-chunk timeout
    detail so it's not silently swallowed; the downstream validation
    cascade then dispatches the canonical refresh for each — exactly
    what would have happened if the original 300s monolithic run had
    returned the same FAILED list. Non-cascadable check names fall
    through to the existing INGESTION_AUTO_RECOVERY_VALIDATION_SKIPPED
    long-tail path.

    Never raises — exceptions inside ``_safe_run`` are wrapped as failed
    CheckResults by the suite; an unexpected exception in this helper
    is caught and surfaced as a synthetic "<chunk_name>:exception"
    failed-check name in the aggregate output. Per the cascade family
    invariant: never raise into the daemon loop.
    """
    import time as _time

    # Lazy-load the check functions + safe-run helper. The chunk-level
    # invocation uses the same primitives as run_suite, just partitioned
    # into chunks so each chunk has its own timeout budget.
    from tpcore.quality.validation import checks as _checks_pkg  # noqa: F401

    # Build a name → check_fn registry by importing each module's
    # ``check_*`` entry-point. The registry is module-private (we don't
    # widen the suite's public API) — D14 owns this mapping because the
    # chunking is a self-heal concern, not a suite-shape change.
    from tpcore.quality.validation.checks.aaii_sentiment_freshness import (
        check_aaii_sentiment_freshness,
    )
    from tpcore.quality.validation.checks.borrow_rates_freshness import (
        check_borrow_rates_freshness,
    )
    from tpcore.quality.validation.checks.constituent import (
        check_constituent_snapshot,
    )
    from tpcore.quality.validation.checks.corporate_actions_completeness import (
        check_corporate_actions_completeness,
    )
    from tpcore.quality.validation.checks.corporate_actions_integrity import (
        check_corporate_actions_integrity,
    )
    from tpcore.quality.validation.checks.delistings import check_delistings
    from tpcore.quality.validation.checks.earnings_events_freshness import (
        check_earnings_events_freshness,
    )
    from tpcore.quality.validation.checks.earnings_events_monotone import (
        check_earnings_events_monotone,
    )
    from tpcore.quality.validation.checks.fear_greed_freshness import (
        check_fear_greed_freshness,
    )
    from tpcore.quality.validation.checks.fundamentals_integrity import (
        check_fundamentals_integrity,
    )
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        check_fundamentals_quarterly_completeness,
    )
    from tpcore.quality.validation.checks.insider_sentiment_freshness import (
        check_insider_sentiment_freshness,
    )
    from tpcore.quality.validation.checks.liquidity_tiers_completeness import (
        check_liquidity_tiers_completeness,
    )
    from tpcore.quality.validation.checks.liquidity_tiers_freshness import (
        check_liquidity_tiers_freshness,
    )
    from tpcore.quality.validation.checks.macro_indicators_completeness import (
        check_macro_indicators_completeness,
    )
    from tpcore.quality.validation.checks.macro_indicators_freshness import (
        check_macro_indicators_freshness,
    )
    from tpcore.quality.validation.checks.options_max_pain_freshness import (
        check_options_max_pain_freshness,
    )
    from tpcore.quality.validation.checks.prices_daily_completeness import (
        check_prices_daily_completeness,
    )
    from tpcore.quality.validation.checks.prices_daily_freshness import (
        check_prices_daily_freshness,
    )
    from tpcore.quality.validation.checks.row_integrity import check_row_integrity
    from tpcore.quality.validation.checks.sec_filings_freshness import (
        check_sec_filings_freshness,
    )
    from tpcore.quality.validation.checks.sec_insider_monotone import (
        check_sec_insider_monotone,
    )
    from tpcore.quality.validation.checks.short_interest_freshness import (
        check_short_interest_freshness,
    )
    from tpcore.quality.validation.checks.social_sentiment_freshness import (
        check_social_sentiment_freshness,
    )
    from tpcore.quality.validation.checks.splits import check_splits
    from tpcore.quality.validation.checks.ticker_classifications_freshness import (
        check_ticker_classifications_coverage,
    )
    from tpcore.quality.validation.suite import _safe_run

    check_fns: dict[str, Any] = {
        "delistings": check_delistings,
        "constituent": check_constituent_snapshot,
        "splits": check_splits,
        "row_integrity": check_row_integrity,
        "fundamentals_integrity": check_fundamentals_integrity,
        "fundamentals_quarterly_completeness": check_fundamentals_quarterly_completeness,
        "corporate_actions_integrity": check_corporate_actions_integrity,
        "corporate_actions_completeness": check_corporate_actions_completeness,
        "earnings_events_freshness": check_earnings_events_freshness,
        "earnings_events_monotone": check_earnings_events_monotone,
        "sec_filings_freshness": check_sec_filings_freshness,
        "sec_insider_monotone": check_sec_insider_monotone,
        "liquidity_tiers_freshness": check_liquidity_tiers_freshness,
        "liquidity_tiers_completeness": check_liquidity_tiers_completeness,
        "ticker_classifications_coverage": check_ticker_classifications_coverage,
        "macro_indicators_freshness": check_macro_indicators_freshness,
        "macro_indicators_completeness": check_macro_indicators_completeness,
        "prices_daily_freshness": check_prices_daily_freshness,
        "prices_daily_completeness": check_prices_daily_completeness,
        "options_max_pain_freshness": check_options_max_pain_freshness,
        "insider_sentiment_freshness": check_insider_sentiment_freshness,
        "social_sentiment_freshness": check_social_sentiment_freshness,
        "fear_greed_freshness": check_fear_greed_freshness,
        "short_interest_freshness": check_short_interest_freshness,
        "borrow_rates_freshness": check_borrow_rates_freshness,
        "aaii_sentiment_freshness": check_aaii_sentiment_freshness,
    }

    total_started = _time.perf_counter()
    failed_checks: list[str] = []
    chunks_telemetry: list[dict[str, Any]] = []
    any_timed_out = False

    for chunk_name, check_names in _VALIDATION_CHUNK_SPECS:
        chunk_started = _time.perf_counter()
        # Build per-chunk safe-run tasks. Unknown names get a synthetic
        # exception row (defensive — would only fire on a typo above).
        tasks: list[Any] = []
        for cn in check_names:
            fn = check_fns.get(cn)
            if fn is None:
                log.warning(
                    "ops.auto_cascade.d14_chunked.unknown_check",
                    chunk=chunk_name, check=cn,
                )
                failed_checks.append(cn)
                continue
            tasks.append(_safe_run(cn, fn, pool, None))

        chunk_failed: list[str] = []
        chunk_timed_out = False
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False),
                timeout=chunk_budget_sec,
            )
            for r in results:
                # _safe_run always returns a CheckResult; surface failures
                # but NOT exceptions (those are already failed-result-wrapped).
                if not getattr(r, "passed", False):
                    chunk_failed.append(r.name)
        except TimeoutError:
            chunk_timed_out = True
            any_timed_out = True
            # Safe-degrade: every check in the chunk is treated as failed
            # so the downstream cascade dispatches their canonical refreshes
            # (operator-visible: per-chunk timeout is in chunks_telemetry).
            chunk_failed.extend(list(check_names))
        except Exception as exc:  # noqa: BLE001 — never crash the cascade
            log.error(
                "ops.auto_cascade.d14_chunked.chunk_exception",
                chunk=chunk_name, error=str(exc),
            )
            # Mark every check in the chunk failed + a synthetic
            # "<chunk>:exception" sentinel so the operator can grep
            # application_log for the broken chunk.
            chunk_failed.extend(list(check_names))
            chunk_failed.append(f"{chunk_name}:exception")

        failed_checks.extend(chunk_failed)
        chunks_telemetry.append({
            "chunk": chunk_name,
            "checks": list(check_names),
            "failed": chunk_failed,
            "timed_out": chunk_timed_out,
            "duration_ms": int((_time.perf_counter() - chunk_started) * 1000),
        })
        log.info(
            "ops.auto_cascade.d14_chunked.chunk_complete",
            chunk=chunk_name,
            failed=len(chunk_failed),
            timed_out=chunk_timed_out,
            duration_ms=chunks_telemetry[-1]["duration_ms"],
        )

    # Dedupe failed_checks while preserving order — the Wave-1 cascade
    # is order-insensitive but emits one event per name; duplicates would
    # double-fire.
    seen: set[str] = set()
    deduped: list[str] = []
    for n in failed_checks:
        if n not in seen:
            seen.add(n)
            deduped.append(n)

    return {
        "failed_checks": deduped,
        "chunks": chunks_telemetry,
        "total_duration_ms": int((_time.perf_counter() - total_started) * 1000),
        "any_chunk_timed_out": any_timed_out,
    }


async def _auto_cascade_validation_timeout(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """D14 — data_validation TIMEOUT → chunked re-run + synthesize FAILED.

    Spec ``2026-05-21-deterministic-self-heal-coverage-expansion-design.md``
    §1 row D14 (added by PR #263). The monolithic ``_stage_data_validation``
    hits the 300s cap before completing the 25-check suite; the Wave-1
    validation cascade is keyed on a FAILED check_name list which a
    TIMEOUT does not produce, so the cascade can't fire.

    Recovery: scan ``summary.stages`` for a TIMEOUT data_validation entry
    (status == "TIMEOUT", error contains "timed out"). If found, call
    ``_chunk_validation_suite`` to re-run the suite in smaller chunks
    with their own 60s budgets; then REPLACE the TIMEOUT entry with a
    synthetic FAILED entry whose ``error`` field matches the canonical
    shape "validation suite failed: [<names>]" — exactly the shape the
    Wave-1 cascade's ``_parse_failed_check_names`` consumes. The
    downstream validation cascade then sees the failed-check list and
    dispatches canonical refreshes as if the original monolithic suite
    had returned them itself. NO contract change at the consumer.

    Event: ``INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED`` (the cascade's
    terminal observability handle). Per-chunk telemetry is carried in
    the event data + the synthetic stage entry's ``detail`` so the
    operator can grep application_log for "chunk=<name> timed_out=true"
    and see exactly which sub-suite blew the budget.

    Never raises — wrapped in a broad try/except per the cascade family
    invariant (daemon stays alive).
    """
    # Locate the TIMEOUT data_validation entry, if any.
    val_idx: int | None = None
    val_result: StageResult | None = None
    for i, result in enumerate(summary.stages):
        if result.name != "data_validation":
            continue
        if result.status != "TIMEOUT":
            continue
        if not _matches_any(result.error, _TIMEOUT_TOKENS):
            continue
        val_idx = i
        val_result = result
        break
    if val_idx is None or val_result is None:
        return

    first_error = (val_result.error or "")[:240]
    await db_log.log(
        "INGESTION_AUTO_RECOVERY_START",
        "D14 cascade: data_validation TIMEOUT → chunked re-run",
        severity="INFO",
        data={
            "stage": "data_validation",
            "cascade_mode": "validation_chunked",
            "trigger": "timeout",
            "first_error": first_error,
            "chunk_budget_sec": _VALIDATION_CHUNK_BUDGET_SEC,
        },
    )
    log.info(
        "ops.auto_cascade.d14_chunked.start",
        first_error=first_error,
        chunk_count=len(_VALIDATION_CHUNK_SPECS),
    )

    try:
        chunked = await _chunk_validation_suite(pool, log=log)
    except Exception as exc:  # noqa: BLE001 — never crash the cascade
        log.error(
            "ops.auto_cascade.d14_chunked.failed",
            error=str(exc),
        )
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_FAILED",
            f"D14 cascade FAILED: chunked re-run raised ({type(exc).__name__})",
            severity="ERROR",
            data={
                "stage": "data_validation",
                "cascade_mode": "validation_chunked",
                "first_error": first_error,
                "cascade_error": str(exc)[:240],
            },
        )
        return

    failed_checks = chunked["failed_checks"]
    # Synthesize the FAILED stage entry. The error string MUST match the
    # canonical shape the Wave-1 cascade parses — that's the explicit
    # contract D14 preserves (spec rule "don't change the contract; just
    # chunk the production"). If chunking produced zero failed checks,
    # report the (rare) success: monolithic timed out but chunked passed,
    # so the suite green-flipped (the chunk_budget effectively gave the
    # suite more wall-time per check).
    if not failed_checks:
        # Replace the TIMEOUT with an OK so subsequent cascades don't
        # re-trigger on the (now-stale) TIMEOUT entry. Carry the
        # chunked-recovery breadcrumb in detail.
        synthetic = StageResult(
            name="data_validation",
            status="OK",
            duration_ms=int(chunked["total_duration_ms"]),
            detail={
                "cascade": True,
                "cascade_mode": "validation_chunked",
                "trigger": "timeout",
                "first_error": first_error,
                "chunks": chunked["chunks"],
                "any_chunk_timed_out": chunked["any_chunk_timed_out"],
                "chunked_recovery": True,
            },
            error=None,
        )
        summary.stages[val_idx] = synthetic
        await db_log.log(
            "INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED",
            (
                "D14 cascade recovered: data_validation TIMEOUT → "
                "chunked re-run produced zero failed checks "
                "(suite green-flipped under per-chunk budget)"
            ),
            severity="INFO",
            data={
                "stage": "data_validation",
                "cascade_mode": "validation_chunked",
                "first_error": first_error,
                "failed_checks": [],
                "any_chunk_timed_out": chunked["any_chunk_timed_out"],
                "chunks": chunked["chunks"],
                "total_duration_ms": int(chunked["total_duration_ms"]),
            },
        )
        log.info(
            "ops.auto_cascade.d14_chunked.recovered_green",
            total_duration_ms=int(chunked["total_duration_ms"]),
        )
        return

    synthetic_error = f"validation suite failed: {failed_checks!r}"
    synthetic = StageResult(
        name="data_validation",
        status="FAILED",
        duration_ms=int(chunked["total_duration_ms"]),
        detail={
            "cascade": True,
            "cascade_mode": "validation_chunked",
            "trigger": "timeout",
            "first_error": first_error,
            "chunks": chunked["chunks"],
            "any_chunk_timed_out": chunked["any_chunk_timed_out"],
            "chunked_recovery": True,
            "failed_checks": failed_checks,
        },
        error=synthetic_error,
    )
    summary.stages[val_idx] = synthetic

    await db_log.log(
        "INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED",
        (
            f"D14 cascade: data_validation TIMEOUT → chunked re-run "
            f"produced {len(failed_checks)} failed check(s) — routing to "
            f"Wave-1 validation cascade"
        ),
        severity="INFO",
        data={
            "stage": "data_validation",
            "cascade_mode": "validation_chunked",
            "first_error": first_error,
            "failed_checks": failed_checks,
            "any_chunk_timed_out": chunked["any_chunk_timed_out"],
            "chunks": chunked["chunks"],
            "total_duration_ms": int(chunked["total_duration_ms"]),
        },
    )
    log.info(
        "ops.auto_cascade.d14_chunked.recovered_failed",
        failed_checks=failed_checks,
        any_chunk_timed_out=chunked["any_chunk_timed_out"],
        total_duration_ms=int(chunked["total_duration_ms"]),
    )


# ─────────────────────────────────────────────────────────────────────────
# D11 — Freshness vendor_late cascade
# ─────────────────────────────────────────────────────────────────────────


async def _auto_cascade_vendor_late(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> None:
    """D11 — classify red freshness checks as VENDOR_LATE when applicable.

    Spec ``2026-05-21-deterministic-self-heal-coverage-expansion-design.md``
    §1 row D11. Some freshness checks red because the vendor has not
    published anything newer than what we hold — not our defect; running
    a refresh against the vendor would return the same stale window we
    already have. Per the [[etl-bulk-before-api-crawl]] memory + the
    no-lazy-vendor-blame rule, this classification is PROVEN by a live
    cheap probe (``tpcore.selfheal.probes.VENDOR_PROBES``), never assumed.

    For each red freshness check in ``_VENDOR_LATE_CHECK_MAP``:

    1. Look up the feed key and call its vendor probe (returns a typed
       ``VendorState`` with our_latest, vendor_latest, has_newer).
    2. If ``has_newer == False`` → vendor has NOTHING newer than we hold
       → classify VENDOR_LATE: emit ``INGESTION_VENDOR_LATE_SKIPPED`` and
       record the check in the data_validation stage's
       ``detail["vendor_late_checks"]`` set so the Wave-1 validation
       cascade skips dispatching a (useless) refresh for it.
    3. If ``has_newer == True`` → genuine staleness; do nothing here so
       Wave-1's cascade refreshes as normal.
    4. If probe returns None (undeterminable: no DB rows yet, probe
       transient failure, no probe registered) → stay strict; do nothing.

    Per the spec D11 guardrail:
      * The freshness check itself remains red in summary.stages — D11
        does NOT downgrade or relax any check.
      * ``prices_daily_completeness`` is INTENTIONALLY not in the map
        (the spec calls out preserving this invariant).
      * vendor_late is a CLASSIFICATION (not-our-defect), surfaced as a
        DISTINCT event so the operator's daemon doesn't keep retrying.

    Event: ``INGESTION_VENDOR_LATE_SKIPPED`` (one per vendor-late check).

    Never raises — the cascade is wrapped in per-check try/except so a
    transient probe failure NEVER aborts the daemon. Daemon-alive
    invariant matches the sibling Wave-2 cascade pattern.
    """
    # Locate the FAILED data_validation entry, if any. Mirrors the
    # Wave-1 cascade — D11 only acts when validation has reds.
    val_idx: int | None = None
    val_result: StageResult | None = None
    for i, result in enumerate(summary.stages):
        if result.name != "data_validation":
            continue
        if result.status != "FAILED":
            continue
        if _VALIDATION_SUITE_FAILED_TOKEN not in (result.error or "").lower():
            continue
        val_idx = i
        val_result = result
        break
    if val_idx is None or val_result is None:
        return

    failed_checks = _parse_failed_check_names(val_result.error)
    if not failed_checks:
        return

    # Filter to checks we have a vendor-late probe wiring for.
    candidates = [
        c for c in failed_checks if c in _VENDOR_LATE_CHECK_MAP
    ]
    if not candidates:
        return

    # Lazy-import the probe registry so a missing tpcore.selfheal at
    # collection time never breaks the module load.
    try:
        from tpcore.selfheal.probes import VENDOR_PROBES
    except Exception as exc:  # noqa: BLE001 — defensive; module must exist
        log.error(
            "ops.auto_cascade.d11_vendor_late.import_failed",
            error=str(exc),
        )
        return

    vendor_late: list[str] = []
    for check_name in candidates:
        feed_key = _VENDOR_LATE_CHECK_MAP[check_name]
        probe = VENDOR_PROBES.get(feed_key)
        if probe is None:
            # Probe-less feed — stay strict per the publication-gate
            # contract; the check stays red and routes to Wave-1.
            log.info(
                "ops.auto_cascade.d11_vendor_late.no_probe",
                check=check_name, feed=feed_key,
            )
            continue
        try:
            state = await probe(pool)
        except Exception as exc:  # noqa: BLE001 — probe must never crash daemon
            log.warning(
                "ops.auto_cascade.d11_vendor_late.probe_failed",
                check=check_name, feed=feed_key, error=str(exc),
            )
            continue
        if state is None:
            # Undeterminable (empty DB, vendor probe transient fail).
            # Stay strict; the freshness check remains red and the
            # Wave-1 cascade gets a shot at it normally.
            log.info(
                "ops.auto_cascade.d11_vendor_late.indeterminate",
                check=check_name, feed=feed_key,
            )
            continue
        if state.has_newer:
            # Vendor IS ahead — genuine staleness; Wave-1 should heal.
            # No event emitted here; this is normal flow.
            log.info(
                "ops.auto_cascade.d11_vendor_late.vendor_ahead",
                check=check_name, feed=feed_key,
                our_latest=str(state.our_latest),
                vendor_latest=str(state.vendor_latest),
            )
            continue

        # has_newer is False → vendor_late. Emit the distinct event.
        await db_log.log(
            "INGESTION_VENDOR_LATE_SKIPPED",
            (
                f"D11 cascade: {check_name} classified VENDOR_LATE "
                f"(feed={feed_key}, our_latest={state.our_latest}, "
                f"vendor_latest={state.vendor_latest}) — not our defect, "
                f"skipping refresh; daemon CONTINUING"
            ),
            severity="WARNING",
            data={
                "check": check_name,
                "feed": feed_key,
                "our_latest": state.our_latest.isoformat(),
                "vendor_latest": state.vendor_latest.isoformat(),
                "cascade_mode": "vendor_late",
                "note": (
                    "vendor has nothing newer than we hold; freshness "
                    "check stays red as a classification, no refresh "
                    "dispatched (would return the same window)"
                ),
            },
        )
        log.warning(
            "ops.auto_cascade.d11_vendor_late.classified",
            check=check_name, feed=feed_key,
            our_latest=str(state.our_latest),
            vendor_latest=str(state.vendor_latest),
        )
        vendor_late.append(check_name)

    if not vendor_late:
        return

    # Annotate the data_validation stage entry so the Wave-1 cascade
    # can skip vendor-late checks. The entry keeps status=FAILED — D11
    # does NOT downgrade — but the detail carries the classification.
    val_result.detail = {
        **(val_result.detail or {}),
        "vendor_late_checks": vendor_late,
    }
    summary.stages[val_idx] = val_result


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
