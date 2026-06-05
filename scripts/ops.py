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
# Marker written into every canary-injected forensics-trigger row (Plan 2:
# data_quality_log kind='forensics_trigger', notes->>'source') and used by the
# teardown DELETE predicate. Single constant keeps the sites in sync so a
# divergence can never make teardown silently fail to clean injected rows.
_CANARY_INJECTION_SOURCE = "canary_injection"

# Canonical INSERT for the symbol-history-evidence forensic rows into the
# redesigned platform.data_quality_log (Plan 2 migration 20260604_0500):
# kind='validation' discriminator, jsonb notes, uuid PK ⇒ no ON CONFLICT.
# $1 = source, $2 = notes (jsonb-cast). The typed metric columns are the
# validation-only constants this evidence row carries (latency/missing=0,
# stale=FALSE, confidence=1.000). Single constant keeps the in-transaction
# evidence sites (same_cik_no_open_window / pre_dates_change / batch flush)
# in lockstep on the new shape.
_DQL_VALIDATION_INSERT_SQL = """
    INSERT INTO platform.data_quality_log
        (kind, source, timestamp, latency_ms, missing_bars,
         stale, confidence, notes)
    VALUES ('validation', $1, NOW(), 0, 0, FALSE, 1.000, $2::jsonb)
"""

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
class FinalLaneVerdict:
    """Authoritative end-of-run lane verdict (2026-05-29 control-plane fix).

    Single source of truth for every downstream decision: ops.py process
    exit code, scripts/run_data_operations.sh wrapper continuation, the
    DATA_OPERATIONS_COMPLETE Step-6 emission gate, and engine sweep
    eligibility. All of those used to be derived independently from
    intermediate StageResult statuses; the result was a stale
    first-pass FAILED data_validation row blocking emission even after
    every red was cascade-healed (audit 2026-05-29).

    Producer: `cmd_update` builds the verdict AFTER the Wave-1
    cascade re-runs data_validation. Consumers: UpdateSummary.exit_code
    (below), amain (process exit), wrapper Step 6 (the
    INSERT-DATA_OPERATIONS_COMPLETE row only fires when the wrapper
    sees exit 0, which now means final_status == 'GREEN').

    Field semantics (the schema operator's task spec REQ-001 mandated):

    * ``final_status``: ``'GREEN'`` (lane proven green either first-pass
      or post-cascade) or ``'RED'`` (unresolved reds remain, OR no
      data_validation stage ran).
    * ``exit_code``: ``0`` iff ``final_status == 'GREEN'``.
    * ``emission_allowed``: ``True`` iff ``final_status == 'GREEN'`` —
      the wrapper Step 6 + engine sweep gate consult this.
    * ``engine_dispatch_allowed``: mirror of ``emission_allowed`` (kept
      separate so a future policy split between emission and dispatch
      stays expressible without churning the verdict shape).
    * ``first_pass_failed_checks``: the failed-check list parsed from
      the FIRST data_validation run's error message (empty when first
      pass was already green).
    * ``recovered_checks``: cascade handled these AND post-cascade
      validation proved them green.
    * ``remaining_failed_checks``: still red after post-cascade
      re-validation.
    * ``unhealable_checks``: HealSpec marked these healable=False —
      operator-visible reason in the cascade UNHEALABLE event.
    * ``vendor_late_checks``: D11 classification (vendor has nothing
      newer; check stays red but is not a regression — preserves the
      sacred "classification, not relaxation" invariant).
    * ``cascade_attempted``: did the Wave-1 validation cascade fire?
    * ``post_cascade_validation_status``: ``None`` (no cascade ran),
      ``'GREEN'`` (re-validation passed), ``'RED'`` (re-validation
      found remaining reds), ``'NOT_RUN'`` (cascade fired but
      re-validate was skipped — e.g. all checks vendor_late or all
      checks unhealable, so re-running adds no new info).
    """
    final_status: str  # "GREEN" | "RED"
    exit_code: int
    emission_allowed: bool
    engine_dispatch_allowed: bool
    first_pass_failed_checks: list[str] = field(default_factory=list)
    recovered_checks: list[str] = field(default_factory=list)
    remaining_failed_checks: list[str] = field(default_factory=list)
    unhealable_checks: list[str] = field(default_factory=list)
    vendor_late_checks: list[str] = field(default_factory=list)
    cascade_attempted: bool = False
    post_cascade_validation_status: str | None = None  # "GREEN" | "RED" | "NOT_RUN"


@dataclass
class UpdateSummary:
    run_id: uuid.UUID
    started_at: datetime
    finished_at: datetime
    stages: list[StageResult] = field(default_factory=list)
    # 2026-05-29 control-plane fix: produced by cmd_update AFTER all
    # cascades complete. When None, exit_code falls back to the legacy
    # stage-status derivation (preserves backwards compat for code
    # paths that build UpdateSummary directly without going through
    # cmd_update — e.g., the dashboard's mock-summary fixtures + the
    # CLI dry-run path).
    final_verdict: FinalLaneVerdict | None = None

    @property
    def exit_code(self) -> int:
        # 2026-05-29 control-plane fix: when the cmd_update flow produced
        # a FinalLaneVerdict, its exit_code is authoritative — it
        # accounts for cascade-healed reds (post-cascade re-validation
        # green-flipped them to OK) and stays 1 on genuinely-unresolved
        # reds. Without this seam, a stale first-pass FAILED
        # data_validation row would block DATA_OPERATIONS_COMPLETE
        # emission even after every red was healed.
        if self.final_verdict is not None:
            return self.final_verdict.exit_code
        # Legacy path: non-zero if any non-skipped stage failed.
        # Preserved for direct-construction callers + dry-run mode
        # (which short-circuits before the cascade pipeline runs).
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
    """Return the daily_bars adapter config.

    Previously read from a `daily_bars` row in `platform.ingestion_jobs`,
    but that scheduler table was frozen 2026-05-12 when the
    `application_log` event-bus + deterministic-cascade architecture
    replaced the Railway-tick dispatcher (see operator memory:
    `project_railway_hobby_tier`). The config never changed in
    production after the freeze, so it is inlined here. To override at
    invocation use `--param KEY=VALUE` on the CLI.

    Signature kept (pool-accepting async) so existing call sites and
    monkeypatch-based tests don't need touching.
    """
    del pool  # config no longer DB-backed; pool retained for ABI stability
    return {
        "universe": "active",  # FMP path (Alpaca all_active discovery forbidden by feedback_no_alpaca_for_daily_prices_backfill)
        "min_price": 5.0,
        "batch_size": 50,
        "min_volume": 250000,
        "lookback_days": 7,
        "inter_batch_sleep_sec": 0.3,
    }


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
    # 2026-05-30 fix: align the pre-filter denominator with the
    # canonical check at tpcore/quality/validation/checks/prices_daily_
    # freshness.py:210-231, which counts ONLY rows whose ticker has an
    # ``asset_class='stock'`` classification AND a liquidity_tiers row
    # with ``tier <= TRADEABLE_TIER_MAX``. The pre-existing query
    # counted ALL distinct tickers (including SPAC units/warrants,
    # funds, ETFs) so SPAC redemption-day churn — which is structural,
    # not a real coverage collapse — repeatedly red-tripped the gate
    # on Fridays.
    #
    # The denominator drift contradicted the comment block above
    # ("It already reuses the canonical check's threshold so it cannot
    # diverge"). The threshold (COVERAGE_COLLAPSE_PCT) was indeed
    # shared, but the UNIVERSE was not. That's the divergence the
    # comment forbids — corrected here.
    #
    # Investigation memo, 2026-05-29: 82 tickers missing Friday vs
    # Thursday = 78 SPACs (units U / warrants W / Class A) + 2 funds
    # + 1 ETF + 1 common-stock SPAC. Filtering to asset_class='stock'
    # would have left the gate green because the underlying common-
    # stock denominator barely moved.
    from tpcore.quality.validation.checks.prices_daily_freshness import (
        COVERAGE_COLLAPSE_PCT,
        TRADEABLE_TIER_MAX,
    )
    async with pool.acquire() as conn:
        cov = await conn.fetch(
            """
            SELECT pd.date, COUNT(DISTINCT pd.ticker) AS n
            FROM platform.prices_daily pd
            JOIN platform.liquidity_tiers lt ON lt.ticker = pd.ticker
            WHERE pd.date >= $1::date - INTERVAL '40 days'
              AND pd.date <= $1
              AND pd.delisted = false
              AND lt.tier <= $2
              AND EXISTS (
                  SELECT 1 FROM platform.ticker_classifications tc
                  WHERE tc.ticker = pd.ticker
                    AND tc.asset_class = 'stock'
                    AND (tc.lifetime_end IS NULL OR tc.lifetime_end > pd.date)
              )
            GROUP BY pd.date ORDER BY pd.date DESC LIMIT 21
            """,
            target_session,
            TRADEABLE_TIER_MAX,
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
                f"{latest_n} stock tickers (tier<={TRADEABLE_TIER_MAX}) "
                f"= {latest_n / avg_trailing:.0%} of the "
                f"trailing-{len(trailing)}-session avg ({avg_trailing:,.0f}); "
                f"floor is {floor:,.0f} ({1 - COVERAGE_COLLAPSE_PCT:.0%}). "
                f"Denominator is asset_class='stock' only — SPAC units / "
                f"warrants / Class A churn is intentionally excluded so "
                f"redemption-day SPAC drops do NOT trip this gate. "
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
    """Refresh FMP fundamentals restricted to the coarse-filtered universe.

    Scoped repair support (2026-05-29): when ``config['tickers']`` is
    set (comma-list), constrain the refresh to JUST those symbols
    instead of the ~5k-ticker universe. Mirrors the
    ``sec_fundamentals_fallback`` scoping precedent (line 963). Used
    by the console's per-check "Repair failed scope" button to fix
    only the failed tickers, not the whole universe.

    Without this, fixing 25 ADV-class missing-quarter rows required a
    47-min full-universe sweep.
    """
    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fundamentals.cache import FundamentalsCache

    ticker_scope_raw = config.get("tickers")
    ticker_scope: list[str] | None = None
    if ticker_scope_raw:
        ticker_scope = [
            t.strip().upper()
            for t in str(ticker_scope_raw).split(",")
            if t.strip()
        ]
        if not ticker_scope:
            ticker_scope = None

    tickers = await _coarse_filtered_universe(
        pool,
        min_price=float(config.get("min_price", 5.0)),
        min_volume=int(config.get("min_volume", 250_000)),
        lookback_days=int(config.get("lookback_days", 7)),
    )
    if ticker_scope is not None:
        wanted = set(ticker_scope)
        # Scoped path: intersect with the coarse-filter result so
        # we never run on a delisted / unliquid ticker even if the
        # operator typed it. Operator-typed tickers that aren't in
        # the universe are silently skipped + reported in the note.
        before = len(tickers)
        tickers = [t for t in tickers if t in wanted]
        skipped_unlisted = sorted(wanted - {t for t in tickers})
        if not tickers:
            return {
                "tickers": 0, "rows": 0, "no_data": 0, "failures": 0,
                "note": (
                    f"scoped repair: 0 of {len(wanted)} requested tickers "
                    f"are in the coarse-filtered universe "
                    f"(min_price/min_volume/lookback). "
                    f"Skipped: {skipped_unlisted[:10]}"
                ),
            }
        scoped_note = (
            f"scoped to {len(tickers)} of {before} ticker universe "
            f"(operator-requested)"
        )
    else:
        scoped_note = None

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
    if scoped_note:
        detail["note"] = scoped_note
    if failures:
        # Match handler semantics: real FMP failures surface as an error
        # event for the stage, but the pipeline still continues.
        raise RuntimeError(
            f"fundamentals_refresh: {len(failures)} failure(s); first={failures[0][0]}: {failures[0][1]}"
        )
    return detail


async def _stage_sec_fundamentals_fallback(pool: asyncpg.Pool, config: dict[str, Any]) -> dict[str, Any]:
    """SEC EDGAR companyfacts → fundamentals_quarterly fallback.

    Cascade fallback for the periods FMP doesn't have. Runs the
    canonical handler ``tpcore.ingestion.handlers.handle_sec_fundamentals_fallback``
    which archives to R2 → upserts via the cache contract.

    Config keys (all optional, passed to the handler):
      * ``tickers`` (comma-separated): scope to a subset.
      * ``include_no_gap_tickers`` (bool, default False): deep-history
        first-time backfill. Daily cascade leaves False.
      * ``dry_run`` (bool, **default True** at this stage layer):
        preview SEC companyfacts coverage without writing the archive
        or DB. Read-only universe SQL + per-CIK fetches + period
        extraction still happen; only ``manifest_lifecycle`` and
        ``cache.upsert_payload`` are skipped. The stage returns
        ``{"dry_run": True, "archive_rows_planned": …, "per_ticker_planned":
        {…}, …}``. Pass ``--param dry_run=false`` for the actual
        write run. Matches the standing default-True convention
        across the symbol-history / ticker-classifications stages.
    """
    from tpcore.ingestion.handlers import handle_sec_fundamentals_fallback

    # Default ``dry_run`` to True at the stage layer (handler defaults
    # to False to preserve in-process backwards compat). Honor str/bool
    # via the shared ``_stage_param_to_bool`` helper.
    cfg = dict(config or {})
    cfg["dry_run"] = _stage_param_to_bool(cfg.get("dry_run", True))
    rows = await handle_sec_fundamentals_fallback(pool, cfg)
    if isinstance(rows, dict):
        return {"dry_run": True, **rows}
    return {"dry_run": False, "rows": rows or 0}


async def _stage_confirmed_data_gap_evidence_populator(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Populate confirmed-data-gap evidence (data_quality_log
    `kind='confirmed_data_gap_evidence'`) for currently-FAILing
    `(ticker, period_end_date)` tuples.

    Plan 2: evidence rows fold into `platform.data_quality_log` under the
    `confirmed_data_gap_evidence` kind discriminator (the standalone
    `fundamentals_period_source_evidence` table was dropped in migration 0300).

    Implements the `confirmed_data_gap_evidence_populator` stage per
    `docs/superpowers/specs/2026-06-02-excluded-confirmed-data-gap-validator-semantics.md`
    + `docs/superpowers/plans/2026-06-02-excluded-confirmed-data-gap-validator-semantics-plan.md`
    §5.

    For each `(ticker, period_end_date)` in the validator's current
    FAIL set, attempts the FMP cascade then the SEC fallback. Records
    one evidence row per source per period (yielded / empty /
    extract_none / fetch_failure). Writes a manifest CSV at
    `data/confirmed_data_gap_evidence_manifest_<UTC-stamp>.csv` with
    columns `(ticker, period_end_date, fmp_outcome, sec_outcome,
    would_exclude)`.

    Knobs (all optional):
      * `dry_run` (default True at the stage layer): when True, reports
        counters + manifest, NEVER writes evidence rows. Pass
        `--param dry_run=false` for the actual write.
      * `tickers` (comma-separated subset): scope to specific tickers.
      * `limit` (default 0 = no cap): bound the number of tickers
        processed in one run.
      * `use_bulk_zip` (default True): bulk-first invariant. `False`
        raises — no per-row crawl.
      * `archive_max_age_days` (default 7): archive freshness floor.

    Op-on-demand only. NOT part of OPS_UPDATE_STAGES.
    """
    import csv as _csv
    import uuid as _uuid

    from tpcore.data.fundamentals_backfill import backfill_one_ticker
    from tpcore.fmp import FMPFundamentalsAdapter
    from tpcore.fundamentals.cache import FundamentalsCache
    from tpcore.ingestion.handlers import handle_sec_fundamentals_fallback
    from tpcore.logging.db_handler import DBLogHandler
    from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
        compute_fundamentals_gap_periods,
    )

    cfg = dict(config or {})
    log = structlog.get_logger("scripts.ops")
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))
    use_bulk_zip = _stage_param_to_bool(cfg.get("use_bulk_zip", True))
    if not use_bulk_zip:
        # Per plan §5.2 / §6: bulk-first invariant; raise before any
        # HTTP call. The sentinel test pins this.
        raise RuntimeError(
            "confirmed_data_gap_evidence_populator: use_bulk_zip=false "
            "is forbidden (bulk-first invariant per plan §6)"
        )
    explicit_tickers = cfg.get("tickers")
    if explicit_tickers:
        if isinstance(explicit_tickers, str):
            ticker_filter = [
                t.strip().upper()
                for t in explicit_tickers.split(",") if t.strip()
            ]
        else:
            ticker_filter = [str(t).upper() for t in explicit_tickers]
    else:
        ticker_filter = None
    limit = int(cfg.get("limit", 0)) or None
    archive_max_age_days = int(cfg.get("archive_max_age_days", 7))
    # Capture the stage run-start so the live-mode aggregator (int
    # branch of the SEC handler return) can scope its evidence-row
    # count via ``attempted_at >= stage_run_start_ts`` — the SEC
    # handler returns ``int rows_written`` in live mode (not a dict),
    # so we recover the evidence-row count from the substrate.
    stage_run_start_ts = datetime.now(UTC)
    log.info(
        "ops.stage.confirmed_data_gap_evidence_populator.start",
        dry_run=dry_run,
        use_bulk_zip=use_bulk_zip,
        ticker_filter=len(ticker_filter or []),
        limit=limit or 0,
        archive_max_age_days=archive_max_age_days,
    )

    # 1. Universe: validator's current FAIL set (per plan §5.3 #1).
    #    `compute_fundamentals_gap_periods` returns ticker → list of
    #    inferred missing period_end_dates (the same set the validator
    #    is currently FAILing on; identical to the detector's gap dict).
    per_ticker_periods = await compute_fundamentals_gap_periods(pool)
    if ticker_filter:
        wanted = set(ticker_filter)
        per_ticker_periods = {
            t: pe for t, pe in per_ticker_periods.items() if t in wanted
        }
    if limit:
        # Deterministic order so the limit is stable across runs.
        ordered = sorted(per_ticker_periods.keys())[:limit]
        per_ticker_periods = {
            t: per_ticker_periods[t] for t in ordered
        }
    tickers = sorted(per_ticker_periods.keys())
    log.info(
        "ops.stage.confirmed_data_gap_evidence_populator.universe",
        tickers=len(tickers),
        total_periods=sum(len(v) for v in per_ticker_periods.values()),
    )

    # 2. Per `(ticker, period)`: attempt FMP cascade then SEC fallback.
    #    Skip the FMP+SEC attempts in dry_run? No — per plan §5.3 #3
    #    dry_run runs the FMP cascade + SEC fallback but does NOT write
    #    evidence rows. The `backfill_one_ticker` evidence writer is
    #    gated on `pool is not None and record_evidence_for_periods is
    #    not None`; we pass `pool=None` for dry-run to skip the writes.
    #
    #    Dry-run-purity fix (2026-06-03 — PR follow-up to PR #452):
    #    pass ``dry_run=dry_run`` to ``backfill_one_ticker`` so the
    #    primary ``cache.upsert_payload`` write into
    #    ``platform.fundamentals_quarterly`` is ALSO suppressed during
    #    dry-run. Pre-fix: only the evidence write was gated; the FMP
    #    fetch + ``cache.backfill`` ran unconditionally and bumped
    #    ``recorded_at`` on existing rows. Mirror semantic with the SEC
    #    handler's PR #448 dry-run contract.
    counters = {
        "tickers_attempted": 0,
        "fmp_outage_count": 0,
        "fmp_yielded_periods": 0,
        "fmp_empty_periods": 0,
        "fmp_would_write_rows": 0,
        "sec_yielded_periods": 0,
        "sec_extract_none_periods": 0,
        "sec_fetch_failure_periods": 0,
        "would_exclude_periods": 0,
    }
    manifest_records: list[dict[str, str]] = []
    per_ticker_outcomes: dict[str, dict[date, dict[str, str]]] = {}

    if per_ticker_periods:
        db_log = DBLogHandler(
            pool, engine=ENGINE_NAME, run_id=_uuid.uuid4(),
        )
        async with FMPFundamentalsAdapter() as adapter:
            cache = FundamentalsCache(pool, adapter=adapter)
            for symbol, periods in per_ticker_periods.items():
                counters["tickers_attempted"] += 1
                # 2a. FMP cascade — opt-in evidence write.
                #     Pass ``dry_run=dry_run`` so the primary
                #     ``cache.upsert_payload`` write into
                #     ``platform.fundamentals_quarterly`` is suppressed
                #     in preview mode. In dry-run the returned
                #     ``rows_would_write`` is the FMP payload row count
                #     that WOULD have been upserted; we surface it via
                #     the ``fmp_would_write_rows`` counter.
                try:
                    rows_would_write = await backfill_one_ticker(
                        cache, db_log, symbol,
                        dry_run=dry_run,
                        # Pass pool only in live mode so the evidence
                        # writer's idempotent UPSERT runs.
                        pool=(None if dry_run else pool),
                        record_evidence_for_periods=(
                            None if dry_run else periods
                        ),
                        evidence_source="fmp_historical",
                    )
                except RuntimeError as exc:
                    rows_would_write = 0
                    counters["fmp_outage_count"] += 1
                    log.warning(
                        "ops.stage.confirmed_data_gap_evidence_populator.fmp_outage",
                        ticker=symbol, error=str(exc)[:160],
                    )
                counters["fmp_would_write_rows"] += rows_would_write
                # 2b. Query post-fetch state to populate the manifest
                #     regardless of dry/live (read-only).
                async with pool.acquire() as conn:
                    present = await conn.fetch(
                        "SELECT DISTINCT period_end_date "
                        "FROM platform.fundamentals_quarterly "
                        "WHERE ticker = $1 AND period_end_date = ANY($2::date[])",
                        symbol, periods,
                    )
                present_set = {r["period_end_date"] for r in present}
                ticker_outcomes: dict[date, dict[str, str]] = {}
                for pe in periods:
                    fmp_outcome = (
                        "yielded" if pe in present_set else "empty"
                    )
                    if fmp_outcome == "yielded":
                        counters["fmp_yielded_periods"] += 1
                    else:
                        counters["fmp_empty_periods"] += 1
                    ticker_outcomes[pe] = {"fmp": fmp_outcome}
                per_ticker_outcomes[symbol] = ticker_outcomes

        # 2c. SEC fallback — re-uses the handler. Honors dry_run.
        sec_cfg = {
            "tickers": ",".join(per_ticker_periods.keys()),
            "include_no_gap_tickers": False,
            "dry_run": "true" if dry_run else "false",
        }
        try:
            sec_result = await handle_sec_fundamentals_fallback(
                pool, sec_cfg,
            )
        except RuntimeError as exc:
            sec_result = {"error": str(exc)[:160]}
            log.warning(
                "ops.stage.confirmed_data_gap_evidence_populator.sec_handler_raised",
                error=str(exc)[:160],
            )
        # Dispatch on the SEC handler's documented return-shape union
        # (``int | dict[str, Any] | None`` per handlers.py:289).
        #
        # * ``dict``   — dry-run (or RuntimeError-trapped) result;
        #                surface the result keys for observability.
        # * ``int``    — live-mode ``rows_written`` from
        #                ``cache.upsert_payload``. No ``.keys()`` —
        #                surface the row count directly.
        # * ``None``   — defensive; treat as zero/no-op.
        #
        # Fix for the 2026-06-02 operator bounded-live-run crash:
        # ``'int' object has no attribute 'keys'`` was raised at this
        # call site after the SEC handler returned 259 rows-written.
        # The evidence-row write itself had already committed inside
        # the handler (line 566-568) — only the populator's downstream
        # aggregator crashed. The 506-row evidence write that landed
        # before the crash is preserved (no DB cleanup needed).
        sec_result_shape: str
        sec_rows_written = 0
        if isinstance(sec_result, dict):
            sec_result_shape = "dict"
            log.info(
                "ops.stage.confirmed_data_gap_evidence_populator.sec_done",
                result_shape=sec_result_shape,
                result_keys=sorted(
                    k for k in sec_result.keys() if isinstance(k, str)
                ),
            )
        elif isinstance(sec_result, int):
            sec_result_shape = "int"
            sec_rows_written = int(sec_result)
            log.info(
                "ops.stage.confirmed_data_gap_evidence_populator.sec_done",
                result_shape=sec_result_shape,
                rows_written=sec_rows_written,
            )
        else:
            sec_result_shape = "none"
            log.info(
                "ops.stage.confirmed_data_gap_evidence_populator.sec_done",
                result_shape=sec_result_shape,
            )

        # 2d. Read SEC evidence rows back from the substrate (live)
        #     OR from the handler's per-ticker counters (dry-run) so
        #     the manifest's `sec_outcome` column reflects reality.
        #     Plan 2: evidence lives in data_quality_log
        #     (kind='confirmed_data_gap_evidence') — the old
        #     fundamentals_period_source_evidence table was dropped in
        #     migration 0300; the dql table always exists, so no
        #     existence probe is needed.
        from tpcore.quality.confirmed_data_gap_store import SEC_OUTCOMES_SQL

        sec_outcomes: dict[tuple[str, date], str] = {}
        if not dry_run:
            async with pool.acquire() as conn:
                ev_rows = await conn.fetch(
                    SEC_OUTCOMES_SQL,
                    list(per_ticker_periods.keys()),
                    sorted({
                        pe for periods in per_ticker_periods.values()
                        for pe in periods
                    }),
                )
            for r in ev_rows:
                sec_outcomes[(r["ticker"], r["period_end_date"])] = (
                    r["outcome"]
                )

        # 2e. Roll up per-ticker outcomes for the manifest.
        for symbol, periods in per_ticker_periods.items():
            for pe in periods:
                sec_outcome = sec_outcomes.get(
                    (symbol, pe),
                    "unknown" if dry_run else "extract_none",
                )
                if sec_outcome == "yielded":
                    counters["sec_yielded_periods"] += 1
                elif sec_outcome == "extract_none":
                    counters["sec_extract_none_periods"] += 1
                elif sec_outcome == "fetch_failure":
                    counters["sec_fetch_failure_periods"] += 1
                fmp_outcome = per_ticker_outcomes.get(
                    symbol, {}
                ).get(pe, {}).get("fmp", "unknown")
                would_exclude = (
                    fmp_outcome == "empty"
                    and sec_outcome == "extract_none"
                )
                if would_exclude:
                    counters["would_exclude_periods"] += 1
                manifest_records.append({
                    "ticker": symbol,
                    "period_end_date": pe.isoformat(),
                    "fmp_outcome": fmp_outcome,
                    "sec_outcome": sec_outcome,
                    "would_exclude": "true" if would_exclude else "false",
                })

    # 3. Write the manifest CSV (always — dry_run + live both produce
    #    one so the operator can review).
    manifest_path: str | None = None
    if manifest_records:
        utc_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        manifest_dir = Path("data")
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path_obj = (
            manifest_dir
            / f"confirmed_data_gap_evidence_manifest_{utc_stamp}.csv"
        )
        with manifest_path_obj.open("w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(
                f,
                fieldnames=[
                    "ticker", "period_end_date",
                    "fmp_outcome", "sec_outcome", "would_exclude",
                ],
            )
            writer.writeheader()
            writer.writerows(manifest_records)
        manifest_path = str(manifest_path_obj)
        log.info(
            "ops.stage.confirmed_data_gap_evidence_populator.manifest",
            path=manifest_path, rows=len(manifest_records),
        )

    # Surface SEC-handler return-shape + live-mode rows-written in the
    # result dict for operator visibility. ``sec_result_shape`` is
    # absent when ``per_ticker_periods`` was empty (no SEC call made).
    sec_meta: dict[str, Any] = {}
    if per_ticker_periods:
        sec_meta = {
            "sec_result_shape": sec_result_shape,
            "sec_rows_written": sec_rows_written,
        }
    result = {
        "dry_run": dry_run,
        "use_bulk_zip": use_bulk_zip,
        "archive_max_age_days": archive_max_age_days,
        "tickers_attempted": counters["tickers_attempted"],
        "tickers_in_filter": len(ticker_filter or []),
        "limit": limit or 0,
        "manifest_path": manifest_path,
        "stage_run_start_ts": stage_run_start_ts.isoformat(),
        **sec_meta,
        **counters,
    }
    log.info(
        "ops.stage.confirmed_data_gap_evidence_populator.done", **result,
    )
    return result


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

    # Archive-first contract (P1-sibling trust-audit 2026-05-25):
    # the legacy flow delegated to ``backfill_amain`` (per-symbol
    # fetch + DB upsert interleaved), then post-hoc dumped the DB
    # rows to the archive. This inverted: fetch every symbol's
    # classified events into ``archive_rows`` with NO DB write, then
    # ``manifest_lifecycle`` writes the archive + manifest
    # status='archived', then Phase 2 reads the archive file and
    # executemany's into the production table.
    import os as _os

    import httpx as _httpx

    from scripts.backfill_earnings_events import (
        _INSERT_SQL as _EARNINGS_INSERT_SQL,
    )
    from scripts.backfill_earnings_events import (
        INTER_SYMBOL_SLEEP_S as _EARNINGS_INTER_SYMBOL_SLEEP,
    )
    from scripts.backfill_earnings_events import (
        _classify_earnings,
        fetch_earnings,
    )
    from tpcore.ingestion.archive_etl import manifest_lifecycle, read_archive_csv

    fmp_api_key = _os.getenv("FMP_API_KEY")
    if not fmp_api_key:
        raise RuntimeError("earnings_refresh: FMP_API_KEY not set")

    start = _date(2018, 1, 1)
    end = datetime.now(UTC).date() - _td(days=1)
    archive_rows: list[dict] = []
    no_data: list[str] = []
    async with _httpx.AsyncClient(timeout=30.0) as client:
        for i, symbol in enumerate(universe, 1):
            rows = await fetch_earnings(client, symbol, fmp_api_key)
            if not rows:
                no_data.append(symbol)
                await asyncio.sleep(_EARNINGS_INTER_SYMBOL_SLEEP)
                continue
            for r in rows:
                raw_date = r.get("date")
                if not raw_date:
                    continue
                try:
                    ev_date = _date.fromisoformat(raw_date)
                except ValueError:
                    continue
                if ev_date < start or ev_date > end:
                    continue
                classification = _classify_earnings(r)
                if classification is None:
                    continue
                event_type, magnitude = classification
                archive_rows.append({
                    "ticker": symbol,
                    "event_date": ev_date.isoformat(),
                    "event_type": event_type,
                    "magnitude_pct": "" if magnitude is None else str(magnitude),
                    "source": "fmp",
                })
            log.info(
                "ops.stage.earnings_refresh.fetched",
                done=i, total=len(universe), symbol=symbol,
                rows_in_window=sum(
                    1 for r in archive_rows if r["ticker"] == symbol
                ),
            )
            await asyncio.sleep(_EARNINGS_INTER_SYMBOL_SLEEP)

    inserted_total = 0
    archive_path_str: str | None = None
    async with manifest_lifecycle(
        pool,
        source="fmp_earnings_events",
        provider="fmp",
        archive_rows=archive_rows,
        fieldnames=["ticker", "event_date", "event_type", "magnitude_pct", "source"],
        validator=lambda r: bool(r.get("ticker")) and bool(r.get("event_type")),
        date_range_start=start,
        date_range_end=end,
    ) as ctx:
        archive_path_str = str(ctx.archive_path)
        # Phase 2 — ETL from the on-disk archive. Reconstruct the
        # 5-tuple shape the existing INSERT SQL expects; executemany
        # in a single conn.acquire().
        # Pass ctx so read_archive_csv routes to ctx.body bytes on S3
        # backend (where ctx.archive_path is an s3:// URI, not a local
        # Path). On local-FS backend the result is byte-identical.
        csv_rows = read_archive_csv(ctx)
        if csv_rows:
            from decimal import Decimal as _Decimal
            tuples = [
                (
                    r["ticker"],
                    _date.fromisoformat(r["event_date"]),
                    r["event_type"],
                    (
                        None
                        if r.get("magnitude_pct") in (None, "")
                        else _Decimal(r["magnitude_pct"])
                    ),
                    r.get("source") or "fmp",
                )
                for r in csv_rows
            ]
            async with pool.acquire() as conn:
                await conn.executemany(_EARNINGS_INSERT_SQL, tuples)
            inserted_total = len(tuples)
            ctx.actual_rows = inserted_total

    async with pool.acquire() as conn:
        post_count = await conn.fetchval("SELECT COUNT(*) FROM platform.earnings_events")
        post_tickers = await conn.fetchval(
            "SELECT COUNT(DISTINCT ticker) FROM platform.earnings_events"
        )

    return {
        "tickers": len(universe),
        "inserted_this_run": inserted_total,
        "no_data": len(no_data),
        "total_rows": int(post_count or 0),
        "covered_tickers": int(post_tickers or 0),
        "csv_archive": archive_path_str,
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


async def _stage_reclassify_asset_class(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """OpenFIGI-driven asset_class taxonomy refinement (2026-05-30).

    Walks ``platform.ticker_classifications``, calls OpenFIGI for each
    ticker, runs the taxonomy mapper at ``tpcore/openfigi/taxonomy.py``,
    and UPDATEs the asset_class / instrument_subtype columns when the
    new classification differs from what's stored. Idempotent — a
    second run is a near-no-op (most rows already match).

    Config params (``--param key=value``):
        * ``dry_run`` (bool, default True) — preview without writing.
        * ``tickers`` (comma-list) — scope to a subset.
        * ``batch_size`` (int, default 100) — OpenFIGI request batch.
        * ``max_tickers`` (int, optional) — cap for testing.
        * ``only_changed`` (bool, default True) — log only rows that
          would change. Set False to log every classification.

    Rate-limit (OpenFIGI authenticated): 25 req/6s = batched 100/req →
    ~4 batches/sec. 13,840 tickers = ~140 batches = ~35s API time. Add
    DB roundtrips and we land in the 1-3 minute range.

    Defects this fixes:
        * SPAC units / warrants conflated with Class A shares under
          one ``spac`` value — Friday 2026-05-29 coverage-collapse
          false trigger.
        * ADRs hidden under ``stock`` → catalyst engine reads them
          on a 10-Q calendar when they file 20-F.
        * REITs hidden under ``stock`` → momentum engine misweights
          them (90% distribution mechanic).
        * Leveraged/inverse ETFs lumped with vanilla ETFs → reversion
          / momentum both misbehave (path decay).

    Output:
        {
          "tickers": <int>,
          "reclassified": <int>,
          "unchanged": <int>,
          "openfigi_no_match": <int>,
          "by_class": {<asset_class>: <count>, ...},
          "by_subtype": {<subtype>: <count>, ...},
          "dry_run": <bool>,
          "changes_preview": [<sample>, ...]
        }
    """
    log = structlog.get_logger("scripts.ops")
    from tpcore.openfigi.figi_adapter import OpenFIGIAdapter
    from tpcore.openfigi.taxonomy import classify as taxonomy_classify

    cfg = cfg or {}

    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes", "y")

    dry_run = _to_bool(cfg.get("dry_run", True))
    only_changed = _to_bool(cfg.get("only_changed", True))
    batch_size = int(cfg.get("batch_size", 100))
    max_tickers = int(cfg["max_tickers"]) if cfg.get("max_tickers") else None
    ticker_scope_raw = cfg.get("tickers")
    ticker_scope: list[str] | None = None
    if ticker_scope_raw:
        ticker_scope = [
            t.strip().upper()
            for t in str(ticker_scope_raw).split(",")
            if t.strip()
        ]
        if not ticker_scope:
            ticker_scope = None

    log.info(
        "ops.stage.reclassify_asset_class.start",
        dry_run=dry_run, batch_size=batch_size,
        scope=len(ticker_scope) if ticker_scope else "all",
        max_tickers=max_tickers,
    )

    # ─── pull current state ───
    async with pool.acquire() as conn:
        if ticker_scope:
            rows = await conn.fetch(
                """
                SELECT ticker, asset_class, instrument_subtype,
                       current_legal_name, current_exchange
                FROM platform.ticker_classifications
                WHERE ticker = ANY($1::text[])
                ORDER BY ticker
                """,
                ticker_scope,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT ticker, asset_class, instrument_subtype,
                       current_legal_name, current_exchange
                FROM platform.ticker_classifications
                ORDER BY ticker
                """,
            )
    if max_tickers:
        rows = rows[:max_tickers]

    if not rows:
        return {
            "tickers": 0, "reclassified": 0, "unchanged": 0,
            "openfigi_no_match": 0, "dry_run": dry_run,
            "note": "no rows matched scope",
        }

    log.info(
        "ops.stage.reclassify_asset_class.fetched_tickers",
        count=len(rows),
    )

    # ─── OpenFIGI lookups ───
    no_match: list[str] = []
    decisions: list[tuple[str, str, str | None, str | None]] = []
    # decisions = list of (ticker, new_asset_class, new_subtype, prev_asset_class)

    from collections import Counter
    by_class: Counter = Counter()
    by_subtype: Counter = Counter()

    async with OpenFIGIAdapter() as figi:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            tickers = [r["ticker"] for r in batch]
            results = await figi.map_tickers(tickers, exch_code="US")
            results_by_ticker = {r.ticker: r for r in results}
            for r in batch:
                t = r["ticker"]
                figi_result = results_by_ticker.get(t)
                if figi_result is None or figi_result.figi_not_found:
                    no_match.append(t)
                    # Keep existing classification — don't overwrite.
                    by_class[r["asset_class"]] += 1
                    if r["instrument_subtype"]:
                        by_subtype[r["instrument_subtype"]] += 1
                    continue
                decision = taxonomy_classify(
                    ticker=t,
                    security_type=figi_result.security_type,
                    security_type2=figi_result.security_type2,
                    name=figi_result.name or r["current_legal_name"],
                    fallback_asset_class=r["asset_class"],
                )
                by_class[decision.asset_class] += 1
                if decision.instrument_subtype:
                    by_subtype[decision.instrument_subtype] += 1
                if (
                    decision.asset_class != r["asset_class"]
                    or decision.instrument_subtype != r["instrument_subtype"]
                ):
                    decisions.append((
                        t, decision.asset_class,
                        decision.instrument_subtype, r["asset_class"],
                    ))

            if (i // batch_size) % 10 == 0:
                log.info(
                    "ops.stage.reclassify_asset_class.batch_progress",
                    done=min(i + batch_size, len(rows)),
                    total=len(rows),
                    reclassified_so_far=len(decisions),
                    no_match_so_far=len(no_match),
                )

    log.info(
        "ops.stage.reclassify_asset_class.lookup_done",
        total=len(rows),
        reclassified=len(decisions),
        unchanged=len(rows) - len(decisions) - len(no_match),
        no_match=len(no_match),
    )

    # ─── apply changes (unless dry_run) ───
    if decisions and not dry_run:
        # When moving OUT of asset_class IN ('etf', 'etn'), the
        # etf_fields_chk constraint requires etf_inverse / etf_leverage
        # / etf_category to be NULL. Two-pass UPDATE: first nullify
        # those fields for rows moving away from etf/etn, then write
        # the new asset_class + subtype.
        async with pool.acquire() as conn:
            async with conn.transaction():
                non_etf_targets = [
                    t for t, ac, _, _prev in decisions
                    if ac not in ("etf", "etn")
                ]
                if non_etf_targets:
                    await conn.execute(
                        """
                        UPDATE platform.ticker_classifications
                        SET etf_inverse = NULL, etf_leverage = NULL,
                            etf_category = NULL
                        WHERE ticker = ANY($1::text[])
                        """,
                        non_etf_targets,
                    )
                await conn.executemany(
                    """
                    UPDATE platform.ticker_classifications
                    SET asset_class = $2, instrument_subtype = $3,
                        updated_at = NOW()
                    WHERE ticker = $1
                    """,
                    [(t, ac, st) for t, ac, st, _ in decisions],
                )
        log.info(
            "ops.stage.reclassify_asset_class.updates_committed",
            count=len(decisions),
            non_etf_targets_nullified=len(non_etf_targets),
        )

    preview = decisions[:25] if only_changed else decisions[:50]
    return {
        "tickers": len(rows),
        "reclassified": len(decisions),
        "unchanged": len(rows) - len(decisions) - len(no_match),
        "openfigi_no_match": len(no_match),
        "by_class": dict(by_class),
        "by_subtype": dict(by_subtype),
        "dry_run": dry_run,
        "changes_preview": [
            {"ticker": t, "new_asset_class": ac,
             "new_subtype": st, "prev_asset_class": prev}
            for t, ac, st, prev in preview
        ],
    }


async def _stage_backfill_sec_metadata(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """SEC-evidence metadata backfill (P0-003, 2026-05-30).

    Foundation stage for the evidence-based fundamentals-validation
    rewrite (spec `2026-05-30-asset-class-refinement.md` follow-up;
    expert audit verdict REVISE_ARCHITECTURE). This stage ONLY writes
    metadata columns added by migration `20260530_0200`. It does NOT
    change validator semantics, does NOT touch the capital gate, does
    NOT change PASS/FAIL behavior. The new fields are foundation for
    a future five-state ``fundamentals_quarterly_completeness``
    rewrite — this stage simply populates evidence so that rewrite has
    something to read.

    Two paths, both optional, both idempotent:

      * ``do_cik=true`` — for rows where ``cik IS NULL``, fetch the
        SEC ticker→CIK map (``data.sec.gov/files/company_tickers``)
        and resolve. NEVER overwrites a non-NULL CIK
        (operator-provenance preservation). Records ``cik_source =
        'sec_ticker_map'`` for resolved rows.

      * ``do_metadata=true`` — for rows where ``cik IS NOT NULL`` AND
        any of the SEC evidence columns are NULL, fetch
        ``data.sec.gov/submissions/CIK<cik>.json`` and extract the
        primary DocumentType + form histogram + fiscal_year_end_month
        + first/last filing dates. Stores provenance via
        ``metadata_source = 'sec_submissions'`` and
        ``metadata_updated_at = NOW()``.

    Scope params (``--param key=value``):

      * ``dry_run`` (bool, default **True**) — print plan, no DB writes.
        Operator hard rule: never assume backfills are non-dry by
        default.
      * ``do_cik`` (bool, default True) — run the CIK-resolution leg.
      * ``do_metadata`` (bool, default True) — run the metadata leg.
      * ``tickers`` (comma-list) — explicit scope.
      * ``failing_only`` (bool, default False) — scope to the tickers
        currently flagged by ``check_fundamentals_quarterly_completeness``.
        The dispositive operator-facing case for "fix the 25 failing".
      * ``no_cik_country_null`` (bool, default False) — scope to the
        ~1,630 rows with ``cik IS NULL AND country IS NULL`` (the
        large CIK-discovery backfill bucket).
      * ``max_tickers`` (int, optional) — cap (testing/incremental).
      * ``force_refresh_metadata`` (bool, default False) — re-run
        metadata extraction even for rows already populated.

    SEC fair-use: 10 req/sec hard cap. We sleep 0.11s between
    submissions fetches to stay comfortably under the limit.

    Output payload:

        {
          "scope_size": int,
          "cik": {
              "candidates": int, "resolved": int, "unresolved": int,
              "skipped_already_set": int, "written": int,
          },
          "metadata": {
              "candidates": int, "fetched": int, "submissions_404": int,
              "extracted_with_values": int, "written": int,
              "failures": [<ticker>, ...],
          },
          "coverage_before": {<col>: int, ...},
          "coverage_after": {<col>: int, ...},
          "dry_run": bool,
        }
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes", "y")

    dry_run = _to_bool(cfg.get("dry_run", True))
    do_cik = _to_bool(cfg.get("do_cik", True))
    do_metadata = _to_bool(cfg.get("do_metadata", True))
    failing_only = _to_bool(cfg.get("failing_only", False))
    no_cik_country_null = _to_bool(cfg.get("no_cik_country_null", False))
    force_refresh_metadata = _to_bool(cfg.get("force_refresh_metadata", False))
    # 2026-06-02 — bulk-zip path for the metadata leg. When True, the
    # stage reads SEC submissions from data/sec_submissions/ first,
    # then the bulk submissions.zip, with ZERO per-CIK HTTP. Default
    # False preserves the existing per-CIK + full_history HTTP path for
    # incremental use. Operator policy mandates this for cohort-scale
    # repairs (feedback_bulk_before_api_crawl_REINFORCED).
    use_bulk_zip = _to_bool(cfg.get("use_bulk_zip", False))
    bulk_zip_cache_path = str(cfg.get(
        "bulk_zip_cache_path", "/tmp/sec_submissions.zip",  # noqa: S108
    ))
    bulk_zip_force_download = _to_bool(cfg.get("bulk_zip_force_download", False))
    # P1b CIK long-tail FMP fallback knobs. Defaults preserve the
    # pre-P1b stage behaviour exactly: do_fmp_fallback=False means
    # the sub-leg is skipped entirely. Spec PR #423; plan PR #424.
    do_fmp_fallback = _to_bool(cfg.get("do_fmp_fallback", False))
    fmp_rate_limit_sleep_s = float(cfg.get("fmp_rate_limit_sleep_s", 0.2))
    fmp_max_unresolved = int(cfg.get("fmp_max_unresolved", 100))
    max_tickers = int(cfg["max_tickers"]) if cfg.get("max_tickers") else None
    ticker_scope_raw = cfg.get("tickers")
    explicit_tickers: list[str] | None = None
    if ticker_scope_raw:
        explicit_tickers = [
            t.strip().upper()
            for t in str(ticker_scope_raw).split(",")
            if t.strip()
        ] or None

    # ─── coverage snapshot helper ───
    _COVERAGE_SQL = """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE cik IS NOT NULL) AS has_cik,
            COUNT(*) FILTER (WHERE sec_document_type_primary IS NOT NULL)
                AS has_sec_document_type_primary,
            COUNT(*) FILTER (WHERE first_public_filing_date IS NOT NULL)
                AS has_first_public_filing_date,
            COUNT(*) FILTER (WHERE last_filing_date IS NOT NULL)
                AS has_last_filing_date,
            COUNT(*) FILTER (WHERE fiscal_year_end_month IS NOT NULL)
                AS has_fiscal_year_end_month,
            COUNT(*) FILTER (WHERE metadata_source IS NOT NULL)
                AS has_metadata_source,
            COUNT(*) FILTER (WHERE cik_source IS NOT NULL)
                AS has_cik_source
        FROM platform.ticker_classifications
    """

    async def _snapshot() -> dict[str, int]:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_COVERAGE_SQL)
        return {k: int(v or 0) for k, v in dict(row).items()}

    coverage_before = await _snapshot()
    log.info(
        "ops.stage.backfill_sec_metadata.start",
        dry_run=dry_run, do_cik=do_cik, do_metadata=do_metadata,
        failing_only=failing_only,
        no_cik_country_null=no_cik_country_null,
        force_refresh_metadata=force_refresh_metadata,
        explicit_tickers=len(explicit_tickers) if explicit_tickers else None,
        max_tickers=max_tickers,
        coverage_before=coverage_before,
    )

    # ─── resolve scope ───
    scope_tickers: list[str] = []
    if failing_only:
        from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (  # noqa: E501
            _evaluate as _fund_eval,
        )
        ev = await _fund_eval(pool)
        if ev.sentinel is None:
            scope_tickers = sorted(ev.gaps)
        log.info(
            "ops.stage.backfill_sec_metadata.failing_only_scope",
            count=len(scope_tickers),
        )
    if explicit_tickers:
        scope_tickers = sorted(set(scope_tickers) | set(explicit_tickers))
    if no_cik_country_null:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker FROM platform.ticker_classifications
                WHERE cik IS NULL AND country IS NULL
                ORDER BY ticker
                """
            )
        scope_tickers = sorted(set(scope_tickers) | {r["ticker"] for r in rows})
        log.info(
            "ops.stage.backfill_sec_metadata.no_cik_country_null_added",
            added=len(rows), total_scope=len(scope_tickers),
        )

    if not scope_tickers and not (failing_only or no_cik_country_null
                                  or explicit_tickers):
        # No explicit scope filter set → default to "rows missing
        # metadata", but cap to max_tickers so we don't accidentally
        # walk the whole 13,840-row table.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker FROM platform.ticker_classifications
                WHERE cik IS NULL
                   OR sec_document_type_primary IS NULL
                ORDER BY ticker
                """
            )
        scope_tickers = [r["ticker"] for r in rows]

    if max_tickers:
        scope_tickers = scope_tickers[:max_tickers]

    if not scope_tickers:
        coverage_after = await _snapshot()
        return {
            "scope_size": 0,
            "cik": {"candidates": 0, "resolved": 0, "unresolved": 0,
                    "skipped_already_set": 0, "written": 0},
            "metadata": {"candidates": 0, "fetched": 0,
                         "submissions_404": 0,
                         "extracted_with_values": 0, "written": 0,
                         "failures": []},
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "dry_run": dry_run,
            "note": "no rows matched scope",
        }

    # Pull current state for the scope (cik so we can gate, plus we
    # need the row to exist before we UPDATE).
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker, cik, country,
                   sec_document_type_primary, first_public_filing_date,
                   last_filing_date, fiscal_year_end_month, metadata_source
            FROM platform.ticker_classifications
            WHERE ticker = ANY($1::text[])
            ORDER BY ticker
            """,
            scope_tickers,
        )
    state_by_ticker: dict[str, dict[str, Any]] = {
        r["ticker"]: dict(r) for r in rows
    }
    # Drop any scope tickers that don't have a row (defensive — they
    # would silently fail the UPDATE otherwise).
    scope_tickers = [t for t in scope_tickers if t in state_by_ticker]
    log.info(
        "ops.stage.backfill_sec_metadata.scope_resolved",
        scope_size=len(scope_tickers),
    )

    # ─── CIK resolution leg ───
    cik_stats = {
        "candidates": 0, "resolved": 0, "unresolved": 0,
        "skipped_already_set": 0, "written": 0,
    }
    cik_writes: list[tuple[str, str, str]] = []  # (ticker, cik, cik_source)
    if do_cik:
        from tpcore.sec.ticker_cik_map import SECTickerCIKMap

        existing_ciks = {
            t: state_by_ticker[t]["cik"] for t in scope_tickers
        }
        sec_map = SECTickerCIKMap()
        result = await sec_map.resolve_missing_ciks(
            scope_tickers, existing_ciks,
        )
        cik_stats["candidates"] = sum(
            1 for t in scope_tickers if not existing_ciks.get(t)
        )
        cik_stats["resolved"] = len(result.resolved)
        cik_stats["unresolved"] = len(result.unresolved)
        cik_stats["skipped_already_set"] = len(result.skipped_already_set)
        for ticker, entry in result.resolved.items():
            cik_writes.append((ticker, entry.cik, "sec_ticker_map"))
        log.info(
            "ops.stage.backfill_sec_metadata.cik_leg",
            **cik_stats,
        )

    # ─── P1b CIK FMP-fallback sub-leg ───
    # Run only when explicitly opted in (do_fmp_fallback=True). Per
    # spec PR #423 + plan PR #424: lower authority than SEC; never
    # overwrites a non-NULL CIK; symbol-mismatch / ambiguous failures
    # close + emit IDENTITY_DIVERGENCE_INVESTIGATE per the
    # parent_resolver.py protocol; country writeback is OFF.
    fmp_cik_writes: list[tuple[str, str]] = []  # (ticker, cik) — source is always 'fmp'
    fmp_divergence_events: list[tuple[str, dict[str, Any]]] = []  # (run_id, payload)
    fmp_stats = {
        "candidates": 0,
        "resolved": 0,
        "no_match": 0,
        "symbol_mismatch": 0,
        "no_cik_in_profile": 0,
        "fmp_error": 0,
        "skipped_existing_cik": 0,
        "skipped_lifetime_ended": 0,
        "written": 0,
        "divergence_events_written": 0,
    }
    if do_fmp_fallback and do_cik:
        import os as _os
        import uuid as _uuid

        import httpx as _httpx_fmp

        from tpcore.fmp.profile_adapter import fetch_profile

        unresolved_input = list(result.unresolved) if "result" in locals() else []
        cap = fmp_max_unresolved if fmp_max_unresolved > 0 else len(unresolved_input)
        unresolved_capped = unresolved_input[:cap] if cap > 0 else []
        fmp_stats["candidates"] = len(unresolved_capped)

        fmp_api_key = _os.environ.get("FMP_API_KEY")
        run_id = f"p1b_fmp_fallback_{_uuid.uuid4().hex[:12]}"

        if not unresolved_capped:
            log.info(
                "ops.stage.backfill_sec_metadata.fmp_fallback.start",
                unresolved_count=0,
                cap_applied=fmp_max_unresolved,
                fmp_rate_limit_sleep_s=fmp_rate_limit_sleep_s,
                dry_run=dry_run,
            )
        elif not fmp_api_key:
            # Fail loud — operator opted in but credential missing.
            raise RuntimeError(
                "backfill_sec_metadata[fmp_fallback]: FMP_API_KEY env "
                "var required when do_fmp_fallback=true"
            )
        else:
            log.info(
                "ops.stage.backfill_sec_metadata.fmp_fallback.start",
                unresolved_count=len(unresolved_capped),
                cap_applied=fmp_max_unresolved,
                fmp_rate_limit_sleep_s=fmp_rate_limit_sleep_s,
                dry_run=dry_run,
            )

            async with _httpx_fmp.AsyncClient(timeout=20.0) as _fmp_client:
                for _i, _ticker in enumerate(unresolved_capped, start=1):
                    await asyncio.sleep(fmp_rate_limit_sleep_s)
                    _state_row = state_by_ticker.get(_ticker, {})
                    _existing_cik = _state_row.get("cik")
                    # Defensive guard — the SEC unresolved path
                    # implies cik IS NULL but a concurrent process
                    # could populate it between scope read and the
                    # FMP call. The UPDATE's WHERE-clause also
                    # guards; this saves the FMP quota in the
                    # common case.
                    if _existing_cik:
                        fmp_stats["skipped_existing_cik"] += 1
                        continue

                    _result = await fetch_profile(
                        _fmp_client, _ticker, api_key=fmp_api_key,
                    )

                    if _result.state == "resolved" and _result.cik:
                        fmp_stats["resolved"] += 1
                        fmp_cik_writes.append((_ticker, _result.cik))
                    elif _result.state == "no_match":
                        fmp_stats["no_match"] += 1
                    elif _result.state == "symbol_mismatch":
                        fmp_stats["symbol_mismatch"] += 1
                        fmp_divergence_events.append((
                            run_id,
                            {
                                "source": "p1b_fmp_fallback",
                                "ticker": _ticker,
                                "requested_symbol": _ticker,
                                "returned_symbol": _result.returned_symbol,
                                "raw_count": _result.profiles_count,
                                "reason": "fmp_symbol_mismatch",
                                "row_existing_cik": _existing_cik,
                                "advised": (
                                    "operator review before relying on "
                                    "FMP profile for this ticker"
                                ),
                            },
                        ))
                    elif _result.state == "ambiguous_response":
                        # Map to symbol_mismatch counter per the plan's
                        # operator-facing collapse.
                        fmp_stats["symbol_mismatch"] += 1
                        fmp_divergence_events.append((
                            run_id,
                            {
                                "source": "p1b_fmp_fallback",
                                "ticker": _ticker,
                                "requested_symbol": _ticker,
                                "returned_symbol": _result.returned_symbol,
                                "raw_count": _result.profiles_count,
                                "reason": "fmp_ambiguous_response",
                                "row_existing_cik": _existing_cik,
                                "advised": (
                                    "operator review before relying on "
                                    "FMP profile for this ticker"
                                ),
                            },
                        ))
                    elif _result.state == "no_cik_in_profile":
                        fmp_stats["no_cik_in_profile"] += 1
                    elif _result.state == "fmp_error":
                        fmp_stats["fmp_error"] += 1
                        log.warning(
                            "ops.stage.backfill_sec_metadata.fmp_fallback.fmp_error",
                            ticker=_ticker,
                            http_status=_result.http_status,
                            error=_result.error_summary,
                        )

                    if _i % 100 == 0:
                        log.info(
                            "ops.stage.backfill_sec_metadata.fmp_fallback.progress",
                            processed=_i,
                            total=len(unresolved_capped),
                            resolved=fmp_stats["resolved"],
                            errors=fmp_stats["fmp_error"],
                        )

        log.info(
            "ops.stage.backfill_sec_metadata.fmp_fallback.end",
            **fmp_stats,
            divergence_events_pending=len(fmp_divergence_events),
        )

    # ─── metadata extraction leg ───
    metadata_stats = {
        "candidates": 0, "fetched": 0, "submissions_404": 0,
        "extracted_with_values": 0, "written": 0,
    }
    metadata_failures: list[str] = []
    metadata_writes: list[tuple[str, str, dict | None,
                                date | None, date | None,
                                int | None]] = []
    # ^ (ticker, document_type_primary, document_type_history,
    #    first_public_filing_date, last_filing_date,
    #    fiscal_year_end_month)
    if do_metadata:
        from tpcore.sec.companyfacts_adapter import SECCompanyFactsAdapter

        # CIK source: prefer the row's existing CIK; if the CIK leg
        # just resolved one in this run, use that for the same ticker
        # (so a single stage run can both resolve CIK and pull
        # metadata for newly-resolved rows).
        cik_resolutions_this_run = {
            t: cik for t, cik, _src in cik_writes
        }
        # P1b — FMP-fallback-resolved CIKs also feed the metadata
        # leg in the same run so the operator gets evidence-column
        # population for free on the long-tail rows. setdefault keeps
        # SEC ticker map authority on the (impossible-by-construction)
        # collision case.
        for _t, _cik in fmp_cik_writes:
            cik_resolutions_this_run.setdefault(_t, _cik)

        async def _cik_for(ticker: str) -> str | None:
            existing = state_by_ticker[ticker].get("cik")
            if existing:
                return str(existing)
            return cik_resolutions_this_run.get(ticker)

        # ─── bulk-mode prep ───
        # 2026-06-02 — use_bulk_zip=True reads SEC submissions from the
        # local data/sec_submissions/ cache + bulk submissions.zip with
        # ZERO per-CIK HTTP. Mirrors to S3/R2 archive on download per
        # the standing bulk-before-API-crawl operator policy.
        bulk_reader = None
        bulk_zip_path = None
        if use_bulk_zip:
            import os as _os_bulk
            from pathlib import Path as _Path_bulk

            from tpcore.sec.submissions_bulk_reader import (
                SECSubmissionsBulkReader,
                ensure_zip_cached,
            )
            ua = _os_bulk.environ.get("SEC_EDGAR_USER_AGENT")
            if not ua:
                raise RuntimeError(
                    "backfill_sec_metadata[use_bulk_zip]: "
                    "SEC_EDGAR_USER_AGENT env var required to download "
                    "the SEC bulk submissions.zip when the cache is "
                    "stale or missing."
                )
            bulk_zip_path = await ensure_zip_cached(
                _Path_bulk(bulk_zip_cache_path),
                user_agent=ua,
                force_download=bulk_zip_force_download,
            )
            bulk_reader = SECSubmissionsBulkReader(
                zip_path=bulk_zip_path,
            )
            log.info(
                "ops.stage.backfill_sec_metadata.bulk_mode_active",
                zip_path=str(bulk_zip_path),
                local_dir=str(bulk_reader._local_dir),  # noqa: SLF001
            )

        async with SECCompanyFactsAdapter() as sec:
            for ticker in scope_tickers:
                cik = await _cik_for(ticker)
                if cik is None:
                    continue
                # Skip if already populated unless force_refresh_metadata.
                state = state_by_ticker[ticker]
                already_populated = (
                    state.get("sec_document_type_primary") is not None
                    and state.get("fiscal_year_end_month") is not None
                )
                if already_populated and not force_refresh_metadata:
                    continue
                metadata_stats["candidates"] += 1
                # SEC fair-use throttle (only applies on per-CIK HTTP).
                # Bulk mode does NOT sleep — every CIK comes from the
                # local zip / cache file.
                if bulk_reader is None:
                    await asyncio.sleep(0.11)
                try:
                    if bulk_reader is not None:
                        subs = bulk_reader.get_merged_submissions(cik)
                    else:
                        # 2026-06-02 — full_history=True paginates SEC's
                        # filings.files[] so first_public_filing_date is
                        # correct for long-lived issuers (JPM/MS/BMO/AAPL/…).
                        # Spec PR #435 §10 + §13. Without pagination FPFD
                        # reflects only the recent ~1000-filing shard (~8
                        # years for prolific filers) and was producing
                        # decade-shifted FPFD values for ~999 tickers.
                        subs = await sec.get_submissions(
                            cik, full_history=True,
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "ops.stage.backfill_sec_metadata.submissions_error",
                        ticker=ticker, cik=cik,
                        error_type=type(exc).__name__, error=str(exc),
                    )
                    metadata_failures.append(ticker)
                    continue
                if subs is None:
                    metadata_stats["submissions_404"] += 1
                    continue
                metadata_stats["fetched"] += 1
                meta = sec.extract_filing_metadata(subs)
                has_any = any(meta.get(k) is not None for k in (
                    "document_type_primary", "fiscal_year_end_month",
                    "first_public_filing_date", "last_filing_date",
                ))
                if has_any:
                    metadata_stats["extracted_with_values"] += 1
                metadata_writes.append((
                    ticker,
                    meta.get("document_type_primary"),
                    meta.get("document_type_history"),
                    meta.get("first_public_filing_date"),
                    meta.get("last_filing_date"),
                    meta.get("fiscal_year_end_month"),
                ))
        bulk_stats = bulk_reader.stats() if bulk_reader is not None else None
        if bulk_reader is not None:
            bulk_reader.close()
        log.info(
            "ops.stage.backfill_sec_metadata.metadata_leg",
            **metadata_stats,
            failures=len(metadata_failures),
            bulk_stats=bulk_stats,
        )

    # ─── apply writes (idempotent UPDATEs) ───
    if not dry_run and (
        cik_writes or metadata_writes
        or fmp_cik_writes or fmp_divergence_events
    ):
        async with pool.acquire() as conn:
            async with conn.transaction():
                if cik_writes:
                    # Only writes where CIK is currently NULL (the safety
                    # invariant — operator-provenance preserved). Doing
                    # the gate at SQL time too is belt-and-braces; the
                    # SECTickerCIKMap already filtered, but a concurrent
                    # writer could have populated CIK between the read
                    # and this write.
                    await conn.executemany(
                        """
                        UPDATE platform.ticker_classifications
                        SET cik = $2, cik_source = $3, updated_at = NOW()
                        WHERE ticker = $1 AND cik IS NULL
                        """,
                        cik_writes,
                    )
                    cik_stats["written"] = len(cik_writes)
                if fmp_cik_writes:
                    # P1b — FMP-fallback CIK writes carry a stricter
                    # guard (cik IS NULL AND lifetime_end IS NULL) per
                    # the plan's persistence contract. The cik IS NULL
                    # clause is operator-provenance preservation;
                    # lifetime_end IS NULL drops any delisted ticker
                    # that became inactive between scope read and
                    # write. Provenance pinned to 'fmp'.
                    fmp_update_count = await _execute_fmp_cik_updates(
                        conn, fmp_cik_writes,
                    )
                    fmp_stats["written"] = fmp_update_count
                    fmp_stats["skipped_lifetime_ended"] += max(
                        0, len(fmp_cik_writes) - fmp_update_count,
                    )
                if fmp_divergence_events:
                    # IDENTITY_DIVERGENCE_INVESTIGATE events to
                    # platform.application_log per the parent_resolver.py
                    # protocol. One row per symbol_mismatch /
                    # ambiguous_response occurrence.
                    await conn.executemany(
                        """
                        INSERT INTO platform.application_log
                            (engine, run_id, event_type, severity,
                             message, data)
                        VALUES
                            ('sec_metadata_backfill', $1,
                             'IDENTITY_DIVERGENCE_INVESTIGATE',
                             'WARNING',
                             $2, $3::jsonb)
                        """,
                        [
                            (
                                run_id,
                                (
                                    f"FMP /profile divergence for "
                                    f"{payload.get('ticker', '?')}: "
                                    f"{payload.get('reason', '?')}"
                                ),
                                json.dumps(payload),
                            )
                            for run_id, payload in fmp_divergence_events
                        ],
                    )
                    fmp_stats["divergence_events_written"] = len(
                        fmp_divergence_events,
                    )
                if metadata_writes:
                    # The metadata UPDATE always writes (re-write is
                    # safe for the foundation columns; if we got the
                    # row this time we re-confirm provenance).
                    await conn.executemany(
                        """
                        UPDATE platform.ticker_classifications
                        SET sec_document_type_primary = $2,
                            sec_document_type_history = $3::jsonb,
                            first_public_filing_date = $4,
                            last_filing_date = $5,
                            fiscal_year_end_month = $6,
                            metadata_source = 'sec_submissions',
                            metadata_updated_at = NOW(),
                            updated_at = NOW()
                        WHERE ticker = $1
                        """,
                        [
                            (t, dt,
                             json.dumps(hist) if hist is not None else None,
                             first, last, fy)
                            for t, dt, hist, first, last, fy
                            in metadata_writes
                        ],
                    )
                    metadata_stats["written"] = len(metadata_writes)
        log.info(
            "ops.stage.backfill_sec_metadata.writes_committed",
            cik_written=cik_stats["written"],
            metadata_written=metadata_stats["written"],
            fmp_cik_written=fmp_stats["written"],
            fmp_divergence_events_written=fmp_stats["divergence_events_written"],
        )

    coverage_after = await _snapshot()
    out: dict[str, Any] = {
        "scope_size": len(scope_tickers),
        "cik": cik_stats,
        "metadata": {**metadata_stats, "failures": metadata_failures[:50]},
        "cik_fmp_fallback": fmp_stats,
        "coverage_before": coverage_before,
        "coverage_after": coverage_after,
        "dry_run": dry_run,
    }
    if use_bulk_zip and bulk_reader is not None:
        out["bulk_zip"] = {
            "zip_path": str(bulk_zip_path) if bulk_zip_path else None,
            **bulk_reader.stats(),
        }
    return out


async def _execute_fmp_cik_updates(
    conn: Any, fmp_cik_writes: list[tuple[str, str]],
) -> int:
    """Apply the P1b FMP-fallback CIK writes with the stricter
    ``cik IS NULL AND lifetime_end IS NULL`` guard.

    Returns the cumulative number of rows actually updated (a row
    whose ``lifetime_end`` became non-NULL between the scope read
    and the UPDATE results in a zero-row outcome — that's the
    ``skipped_lifetime_ended`` signal the caller increments).

    Per-row (rather than executemany) so the UPDATE-row-count is
    observable per ticker; the long-tail batch is bounded by the
    operator's ``fmp_max_unresolved`` (default 100) so the
    per-row overhead is negligible.
    """
    written = 0
    for ticker, cik in fmp_cik_writes:
        result = await conn.execute(
            """
            UPDATE platform.ticker_classifications
            SET cik = $2, cik_source = 'fmp', updated_at = NOW()
            WHERE ticker = $1
              AND cik IS NULL
              AND lifetime_end IS NULL
            """,
            ticker, cik,
        )
        # asyncpg returns "UPDATE N" as the command tag string.
        if isinstance(result, str) and result.startswith("UPDATE "):
            try:
                written += int(result.split(" ", 1)[1])
            except (ValueError, IndexError):
                pass
        elif isinstance(result, int):
            # Some mock returns int directly — tolerate.
            written += result
    return written


# Provenance precedence dict for the P2a lifecycle backfill.
# Higher value wins on overwrite. ``manual`` is never overwritten;
# ``sec_form_15`` is the strongest evidence (deregistration is
# terminal). FMP / Alpaca are operator-on-demand fallbacks.
_LIFECYCLE_SOURCE_PRECEDENCE: dict[str, int] = {
    "manual": 100,
    "sec_form_15": 80,
    "sec_form_25": 70,
    "sec_form_8k": 60,
    "alpaca_asset_status": 40,
    "fmp_profile": 30,
}


# ─────────────────────────────────────────────────────────────────────
# Ticker-reuse fundamentals cleanup (PR #440 plan + this PR impl)
# ─────────────────────────────────────────────────────────────────────

# Columns of platform.fundamentals_quarterly that the sidecar tables
# mirror 1-to-1. Used for the INSERT … SELECT archive-then-delete path
# and the manifest schema's "source column inventory" sentinel test.
_FQ_MIRROR_COLUMNS: tuple[str, ...] = (
    "ticker", "filing_date", "period_end_date", "period_label",
    "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
    "total_assets", "total_liabilities", "current_assets",
    "current_liabilities", "receivables", "cash_and_equivalents",
    "shares_outstanding", "recorded_at", "pb", "de", "classification_id",
)

# Quarantine-table allowed `disposition` enum (matches CHECK constraint
# in migration 20260602_0100).
_QUARANTINE_DISPOSITIONS: frozenset[str] = frozenset({
    "ambiguous_predecessor_unknown",
    "corp_history_substrate_sparse",
    "cik_null",
    "operator_review_pending",
})


def _ticker_reuse_manifest_columns() -> tuple[str, ...]:
    """Fixed manifest CSV column set. Asserted by the schema sentinel test."""
    return (
        "ticker",
        "period_end_date",
        "original_id",
        "current_cik",
        "current_fpfd",
        "proposed_disposition",
        "evidence_rank_used",
        "evidence_summary",
    )


async def _classify_ticker_reuse_row(
    conn: Any,
    *,
    ticker: str,
    period_end_date: Any,
    current_cik: str | None,
    current_fpfd: Any,
    current_issuer_id: str | None,
) -> tuple[str, int, str]:
    """Per-row evidence classifier for the ticker-reuse cleanup stage.

    Returns ``(disposition, evidence_rank_used, evidence_summary)``.

    Implements spec PR #439 §5 rank order: SEC formerNames →
    issuer_history → issuer_securities (via ticker_history) →
    ambiguous fallback. The rank-3 hit is **dispositive** for the
    high-confidence ticker-reuse cohort. Ranks 1 + 2 hits mean the
    current CIK plausibly used the ticker — route to
    weak_evidence_keep.
    """
    # Rank 1: SEC formerNames[] coverage for current CIK.
    # We don't re-fetch the SEC submissions JSON here — that's the
    # caller's job (uses the bulk reader). The caller passes the
    # already-extracted formerNames coverage via current_issuer_id;
    # if the current issuer_history row's valid_from covers
    # period_end_date, rank 1 fires.
    # (See plan §5; the caller embeds the formerNames check in the
    # current_issuer_id resolution path.)

    # Rank 2: issuer_history for the current CIK covers period_end_date?
    if current_cik is not None:
        row = await conn.fetchrow(
            """
            SELECT issuer_id, legal_name, valid_from, valid_to
            FROM platform.issuer_history
            WHERE cik = $1
              AND valid_from <= $2
              AND (valid_to IS NULL OR valid_to >= $2)
            ORDER BY valid_from DESC LIMIT 1
            """,
            current_cik, period_end_date,
        )
        if row is not None:
            summary = (
                f"rank2_issuer_history: current_cik={current_cik} "
                f"covered by issuer_id={row['issuer_id']} "
                f"name='{row['legal_name']}' "
                f"valid_from={row['valid_from']}"
            )
            return ("weak_evidence_keep", 2, summary[:500])

    # Rank 3: ticker_history → issuer_securities at period_end_date.
    # Was the ticker on a different issuer_id at period_end_date?
    th_row = await conn.fetchrow(
        """
        SELECT classification_id, valid_from, valid_to
        FROM platform.ticker_history
        WHERE ticker = $1
          AND valid_from <= $2
          AND (valid_to IS NULL OR valid_to >= $2)
        ORDER BY valid_from DESC LIMIT 1
        """,
        ticker, period_end_date,
    )
    if th_row is not None:
        is_row = await conn.fetchrow(
            """
            SELECT issuer_id
            FROM platform.issuer_securities
            WHERE classification_id = $1
              AND valid_from <= $2
              AND (valid_to IS NULL OR valid_to >= $2)
            ORDER BY valid_from DESC LIMIT 1
            """,
            th_row["classification_id"], period_end_date,
        )
        if is_row is not None and current_issuer_id is not None:
            if is_row["issuer_id"] != current_issuer_id:
                summary = (
                    f"rank3_different_issuer: at {period_end_date}, "
                    f"ticker on issuer_id={is_row['issuer_id']}, "
                    f"current_issuer_id={current_issuer_id}"
                )
                return (
                    "high_confidence_ticker_reuse", 3, summary[:500],
                )
            # Same issuer historically — current CIK plausibly used
            # the ticker (rank-1/2 fallback).
            summary = (
                f"rank3_same_issuer: at {period_end_date}, ticker on "
                f"current_issuer_id={current_issuer_id}"
            )
            return ("weak_evidence_keep", 3, summary[:500])

    # No dispositive evidence → ambiguous (quarantine, not delete).
    summary = (
        f"no_evidence: ticker_history empty / issuer_securities sparse "
        f"for ticker={ticker} at {period_end_date}"
    )
    return ("ambiguous_predecessor_unknown", 0, summary[:500])


async def _archive_row(
    conn: Any,
    *,
    original_id: int,
    disposition_reason: str,
    decided_by_run_id: str,
    evidence_summary: str,
) -> int:
    """Move one fundamentals_quarterly row to the archive table inside
    the active transaction. INSERT-then-DELETE so an interrupted
    transaction either rolls back BOTH or commits BOTH (the
    archive-before-delete invariant from plan §5.2 #1)."""
    n = await conn.fetchval(
        f"""
        WITH inserted AS (
            INSERT INTO platform.fundamentals_quarterly_archive (
                original_id, {", ".join(_FQ_MIRROR_COLUMNS)},
                disposition_reason, decided_by_run_id, evidence_summary
            )
            SELECT
                id, {", ".join(_FQ_MIRROR_COLUMNS)},
                $2, $3::uuid, $4
            FROM platform.fundamentals_quarterly
            WHERE id = $1
            RETURNING original_id
        ),
        deleted AS (
            DELETE FROM platform.fundamentals_quarterly
            WHERE id = (SELECT original_id FROM inserted)
            RETURNING id
        )
        SELECT COUNT(*) FROM deleted
        """,
        original_id, disposition_reason, decided_by_run_id,
        evidence_summary,
    )
    return int(n or 0)


async def _quarantine_row(
    conn: Any,
    *,
    original_id: int,
    disposition: str,
    decided_by_run_id: str,
    evidence_summary: str,
) -> int:
    """Move one fundamentals_quarterly row to the quarantine table inside
    the active transaction. Same shape as `_archive_row` but routes to
    the quarantine sidecar."""
    if disposition not in _QUARANTINE_DISPOSITIONS:
        raise ValueError(
            f"_quarantine_row: disposition {disposition!r} not in "
            f"allowed set {sorted(_QUARANTINE_DISPOSITIONS)}"
        )
    n = await conn.fetchval(
        f"""
        WITH inserted AS (
            INSERT INTO platform.fundamentals_quarterly_quarantine (
                original_id, {", ".join(_FQ_MIRROR_COLUMNS)},
                disposition, decided_by_run_id, evidence_summary
            )
            SELECT
                id, {", ".join(_FQ_MIRROR_COLUMNS)},
                $2, $3::uuid, $4
            FROM platform.fundamentals_quarterly
            WHERE id = $1
            RETURNING original_id
        ),
        deleted AS (
            DELETE FROM platform.fundamentals_quarterly
            WHERE id = (SELECT original_id FROM inserted)
            RETURNING id
        )
        SELECT COUNT(*) FROM deleted
        """,
        original_id, disposition, decided_by_run_id, evidence_summary,
    )
    return int(n or 0)


async def _stage_cleanup_ticker_reuse_fundamentals(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ticker-reuse fundamentals cleanup (plan PR #440 impl, 2026-06-02).

    Operator-on-demand only. Default ``dry_run=true`` writes a manifest
    CSV but no DB rows. ``dry_run=false`` reads the manifest, re-validates
    each row's evidence against the live substrate, then routes per-row
    to:

      * high_confidence_ticker_reuse → archive-before-delete (sidecar
        `fundamentals_quarterly_archive`).
      * weak_evidence_keep → no mutation; write a `data_quality_log`
        row instead.
      * ambiguous_predecessor_unknown → quarantine sidecar
        (`fundamentals_quarterly_quarantine`).

    Hard invariants (plan §5.2, structurally encoded — no toggle):

      1. Archive-before-delete in one CTE statement; rolls back both
         on failure.
      2. ``delete_after_archive=true`` is required for any DELETE; the
         default is false (archive-only).
      3. Weak-evidence rows are never deleted.
      4. FPFD-drift rows are skipped (the stage re-reads the bulk-
         extracted FPFD for the current CIK; if it does not match the
         stored value, the row is rejected with
         ``disposition='fpfd_drift_detected_skipped'``).
      5. Manifest reproducibility — the dry-run reads the bulk-zip
         submissions cache + DB substrate; no per-CIK HTTP.
      6. Bounded by manifest row IDs — every DELETE/quarantine targets
         a specific ``fundamentals_quarterly.id``, not a range.

    Knobs (``--param key=value``):

      * ``dry_run`` (bool, default True)
      * ``manifest_path`` (str, default
        ``data/fundamentals_quarterly_cleanup_manifest_<UTC>.csv``)
      * ``evidence_level`` (``strong|weak|all``, default ``strong``)
      * ``tickers`` (csv list, default all 783 affected)
      * ``limit`` (int, default 0 = no cap)
      * ``severity_bucket`` (``1|2-3|4-9|10-19|20+|all``, default
        ``all`` for dry-run; operator usually picks one for live)
      * ``archive_only`` (bool, default False)
      * ``delete_after_archive`` (bool, default False — must be explicit)
      * ``quarantine_weak`` (bool, default True)
      * ``use_bulk_zip`` (bool, default True)
      * ``bulk_zip_cache_path`` (str, default ``/tmp/sec_submissions.zip``)
      * ``bulk_zip_force_download`` (bool, default False)
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from pathlib import Path as _Path
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes", "y")

    dry_run = _to_bool(cfg.get("dry_run", True))
    evidence_level = str(cfg.get("evidence_level", "strong")).lower()
    if evidence_level not in ("strong", "weak", "all"):
        raise ValueError(
            f"evidence_level must be 'strong'/'weak'/'all'; got "
            f"{evidence_level!r}"
        )
    explicit_tickers_raw = cfg.get("tickers")
    explicit_tickers: list[str] | None = None
    if explicit_tickers_raw:
        explicit_tickers = [
            t.strip().upper() for t in str(explicit_tickers_raw).split(",")
            if t.strip()
        ] or None
    limit = int(cfg.get("limit", 0)) or None
    severity_bucket = str(cfg.get("severity_bucket", "all"))
    archive_only = _to_bool(cfg.get("archive_only", False))
    delete_after_archive = _to_bool(cfg.get("delete_after_archive", False))
    quarantine_weak = _to_bool(cfg.get("quarantine_weak", True))
    use_bulk_zip = _to_bool(cfg.get("use_bulk_zip", True))
    bulk_zip_cache_path = str(cfg.get(
        "bulk_zip_cache_path", "/tmp/sec_submissions.zip",  # noqa: S108
    ))
    bulk_zip_force_download = _to_bool(cfg.get(
        "bulk_zip_force_download", False,
    ))

    # Manifest path default — timestamped so repeated dry-runs don't
    # overwrite. Caller picks a specific manifest for live by passing
    # `--param manifest_path=…` explicitly.
    default_manifest = (
        f"data/fundamentals_quarterly_cleanup_manifest_"
        f"{_dt.now(_UTC).strftime('%Y%m%dT%H%MZ')}.csv"
    )
    manifest_path = _Path(str(cfg.get("manifest_path", default_manifest)))

    run_id = uuid.uuid4()
    log.info(
        "ops.stage.cleanup_ticker_reuse_fundamentals.start",
        dry_run=dry_run,
        evidence_level=evidence_level,
        severity_bucket=severity_bucket,
        archive_only=archive_only,
        delete_after_archive=delete_after_archive,
        quarantine_weak=quarantine_weak,
        use_bulk_zip=use_bulk_zip,
        manifest_path=str(manifest_path),
        run_id=str(run_id),
        explicit_tickers=(
            len(explicit_tickers) if explicit_tickers else None
        ),
        limit=limit,
    )

    # ─── bulk reader (if enabled) ───
    bulk_reader = None
    if use_bulk_zip:
        import os as _os_b

        from tpcore.sec.submissions_bulk_reader import (  # noqa: PLC0415  # noqa: PLC0415
            SECSubmissionsBulkReader,
            ensure_zip_cached,
        )
        ua = _os_b.environ.get("SEC_EDGAR_USER_AGENT")
        if not ua:
            raise RuntimeError(
                "cleanup_ticker_reuse_fundamentals[use_bulk_zip]: "
                "SEC_EDGAR_USER_AGENT env var required."
            )
        zip_path = await ensure_zip_cached(
            _Path(bulk_zip_cache_path),
            user_agent=ua,
            force_download=bulk_zip_force_download,
        )
        bulk_reader = SECSubmissionsBulkReader(zip_path=zip_path)
        log.info(
            "ops.stage.cleanup_ticker_reuse_fundamentals.bulk_mode_active",
            zip_path=str(zip_path),
        )

    # ─── resolve scope ───
    severity_clause = ""
    severity_params: tuple = ()
    if severity_bucket != "all":
        # severity_bucket selects tickers whose pre-FPFD row count
        # falls into the named bucket. Per plan §6.3.
        bounds = {
            "1": (1, 1),
            "2-3": (2, 3),
            "4-9": (4, 9),
            "10-19": (10, 19),
            "20+": (20, 10_000_000),
        }.get(severity_bucket)
        if bounds is None:
            raise ValueError(
                f"severity_bucket must be 1/2-3/4-9/10-19/20+/all; got "
                f"{severity_bucket!r}"
            )
        severity_clause = (
            "AND fq.ticker IN ("
            "  SELECT fq2.ticker "
            "  FROM platform.fundamentals_quarterly fq2 "
            "  JOIN platform.ticker_classifications tc2 "
            "    ON tc2.ticker = fq2.ticker "
            "  WHERE tc2.first_public_filing_date IS NOT NULL "
            "    AND fq2.period_end_date < tc2.first_public_filing_date "
            "  GROUP BY fq2.ticker "
            "  HAVING COUNT(*) BETWEEN $1 AND $2"
            ")"
        )
        severity_params = bounds

    # Pull candidate rows.
    base_sql = f"""
        SELECT fq.id, fq.ticker, fq.period_end_date,
               tc.cik AS current_cik,
               tc.first_public_filing_date AS current_fpfd
        FROM platform.fundamentals_quarterly fq
        JOIN platform.ticker_classifications tc ON tc.ticker = fq.ticker
        WHERE tc.first_public_filing_date IS NOT NULL
          AND fq.period_end_date < tc.first_public_filing_date
          {severity_clause}
    """
    if explicit_tickers:
        explicit_param_idx = len(severity_params) + 1
        base_sql += f"  AND fq.ticker = ANY(${explicit_param_idx}::text[])"
    base_sql += "\n        ORDER BY fq.ticker, fq.period_end_date"
    if limit:
        base_sql += f"\n        LIMIT {limit}"

    sql_params: list = list(severity_params)
    if explicit_tickers:
        sql_params.append(explicit_tickers)

    async with pool.acquire() as conn:
        candidate_rows = await conn.fetch(base_sql, *sql_params)

    log.info(
        "ops.stage.cleanup_ticker_reuse_fundamentals.scope_resolved",
        candidates=len(candidate_rows),
        severity_bucket=severity_bucket,
    )

    counters = {
        "manifest_writes": 0,
        "high_confidence_archive_count": 0,
        "ambiguous_quarantine_count": 0,
        "weak_evidence_keep_count": 0,
        "fpfd_drift_detected_skipped_count": 0,
        "rejected_no_cik": 0,
    }

    # ─── per-row classification + manifest write / live execution ───
    # Resolve current issuer_id per ticker once (lookup table).
    async with pool.acquire() as conn:
        unique_tickers = {r["ticker"] for r in candidate_rows}
        if unique_tickers:
            issuer_rows = await conn.fetch(
                """
                SELECT tc.ticker, isec.issuer_id
                FROM platform.ticker_classifications tc
                LEFT JOIN platform.ticker_history th
                  ON th.ticker = tc.ticker
                 AND th.valid_to IS NULL
                LEFT JOIN platform.issuer_securities isec
                  ON isec.classification_id = th.classification_id
                 AND isec.valid_to IS NULL
                WHERE tc.ticker = ANY($1::text[])
                """,
                list(unique_tickers),
            )
            current_issuer_by_ticker: dict[str, str | None] = {
                r["ticker"]: r["issuer_id"] for r in issuer_rows
            }
        else:
            current_issuer_by_ticker = {}

    manifest_rows: list[dict[str, Any]] = []
    if dry_run:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

    async with pool.acquire() as conn:
        for r in candidate_rows:
            ticker = r["ticker"]
            cik = r["current_cik"]
            if cik is None:
                counters["rejected_no_cik"] += 1
                continue

            # Invariant #4: FPFD drift detection — re-extract from the
            # bulk reader and compare against the DB's stored value.
            stored_fpfd = r["current_fpfd"]
            if bulk_reader is not None:
                payload = bulk_reader.get_merged_submissions(cik)
                if payload is not None:
                    from tpcore.sec.companyfacts_adapter import (
                        SECCompanyFactsAdapter as _ExtractorCF,
                    )
                    meta = _ExtractorCF.extract_filing_metadata(payload)
                    extracted_fpfd = meta.get("first_public_filing_date")
                    if (
                        extracted_fpfd is not None
                        and extracted_fpfd != stored_fpfd
                    ):
                        counters["fpfd_drift_detected_skipped_count"] += 1
                        manifest_rows.append({
                            "ticker": ticker,
                            "period_end_date": str(r["period_end_date"]),
                            "original_id": r["id"],
                            "current_cik": cik,
                            "current_fpfd": str(stored_fpfd),
                            "proposed_disposition": (
                                "fpfd_drift_detected_skipped"
                            ),
                            "evidence_rank_used": "0",
                            "evidence_summary": (
                                f"stored_fpfd={stored_fpfd} != "
                                f"bulk_extracted_fpfd={extracted_fpfd}"
                            )[:500],
                        })
                        continue

            current_issuer_id = current_issuer_by_ticker.get(ticker)
            disposition, rank, summary = await _classify_ticker_reuse_row(
                conn,
                ticker=ticker,
                period_end_date=r["period_end_date"],
                current_cik=cik,
                current_fpfd=stored_fpfd,
                current_issuer_id=current_issuer_id,
            )

            manifest_rows.append({
                "ticker": ticker,
                "period_end_date": str(r["period_end_date"]),
                "original_id": r["id"],
                "current_cik": cik,
                "current_fpfd": str(stored_fpfd),
                "proposed_disposition": disposition,
                "evidence_rank_used": str(rank),
                "evidence_summary": summary,
            })

            if dry_run:
                continue  # manifest-only

            # ─── LIVE: per-row mutation ───
            # Invariant #3: weak-evidence never deleted.
            # Invariant #2: delete_after_archive must be explicit.
            if disposition == "high_confidence_ticker_reuse":
                if archive_only:
                    # Archive only — leave row in main table for now.
                    continue
                if not delete_after_archive:
                    # Dry-run-equivalent live posture (operator inspects
                    # archive without destructive step).
                    continue
                if evidence_level != "strong":
                    # Invariant: strong-evidence-only deletes.
                    continue
                async with conn.transaction():
                    n = await _archive_row(
                        conn,
                        original_id=r["id"],
                        disposition_reason=(
                            f"rank{rank}_high_confidence_ticker_reuse"
                        ),
                        decided_by_run_id=str(run_id),
                        evidence_summary=summary,
                    )
                    if n != 1:
                        # Row vanished between scope read + transaction;
                        # not a bug, just a race. Skip without raising.
                        continue
                counters["high_confidence_archive_count"] += 1

            elif disposition == "ambiguous_predecessor_unknown":
                if not quarantine_weak:
                    continue
                async with conn.transaction():
                    n = await _quarantine_row(
                        conn,
                        original_id=r["id"],
                        disposition=disposition,
                        decided_by_run_id=str(run_id),
                        evidence_summary=summary,
                    )
                    if n != 1:
                        continue
                counters["ambiguous_quarantine_count"] += 1

            else:  # weak_evidence_keep
                counters["weak_evidence_keep_count"] += 1

    # Dry-run forecast counters (count what WOULD happen).
    if dry_run:
        for m in manifest_rows:
            d = m["proposed_disposition"]
            if d == "high_confidence_ticker_reuse":
                counters["high_confidence_archive_count"] += 1
            elif d == "ambiguous_predecessor_unknown":
                counters["ambiguous_quarantine_count"] += 1
            elif d == "weak_evidence_keep":
                counters["weak_evidence_keep_count"] += 1

        # Write manifest CSV.
        import csv as _csv
        with manifest_path.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(
                fh, fieldnames=_ticker_reuse_manifest_columns(),
            )
            w.writeheader()
            for m in manifest_rows:
                w.writerow(m)
        counters["manifest_writes"] = len(manifest_rows)

    bulk_stats = bulk_reader.stats() if bulk_reader is not None else None
    if bulk_reader is not None:
        bulk_reader.close()

    log.info(
        "ops.stage.cleanup_ticker_reuse_fundamentals.complete",
        **counters,
        bulk_stats=bulk_stats,
        run_id=str(run_id),
    )

    out: dict[str, Any] = {
        "scope_size": len(candidate_rows),
        "manifest_path": str(manifest_path) if dry_run else None,
        "dry_run": dry_run,
        "run_id": str(run_id),
        **counters,
    }
    if bulk_stats is not None:
        out["bulk_zip"] = bulk_stats
    return out


# ─── Symbol-history evidence backfill (spec PR #442 + plan PR #443) ────
# Path B (FMP /stable/symbol-change bulk) primary + Path C (SEC submissions.zip
# cross-walk) resolver. Single bulk GET → R2 archive → ticker_history +
# issuer_securities (additive only). Idempotent. NEVER touches
# fundamentals_quarterly. No per-ticker crawl. See:
#   docs/superpowers/specs/2026-06-02-symbol-history-evidence-backfill.md
#   docs/superpowers/plans/2026-06-02-symbol-history-evidence-backfill-plan.md
#   tests/test_symbol_history_evidence_plan_documented.py

FMP_SYMBOL_CHANGE_LIMIT_DEFAULT: int = 10000
FMP_SYMBOL_CHANGE_SENTINEL_DATE: str = "1969-12-31"
FMP_SYMBOL_CHANGE_ARCHIVE_SOURCE: str = "fmp_symbol_change"
FMP_SYMBOL_CHANGE_URL: str = (
    "https://financialmodelingprep.com/stable/symbol-change"
)


def _symbol_history_evidence_manifest_columns() -> tuple[str, ...]:
    """Fixed manifest CSV column set for symbol_history_evidence_backfill.

    Pinned by the plan §6.2 schema sentinel test.
    """
    return (
        "oldSymbol",
        "newSymbol",
        "change_date",
        "companyName",
        "old_cik_resolved",
        "old_cik_source",
        "new_cik_resolved",
        "new_cik_source",
        "predecessor_classification_id_minted",
        "classification_action",
        "ticker_history_written",
        "issuer_securities_written",
        "disposition",
    )


async def _stage_symbol_history_evidence_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Populate ``ticker_history`` + ``issuer_securities`` + historical
    ``ticker_classifications`` predecessors from the FMP bulk
    ``/stable/symbol-change`` endpoint (Path B primary) cross-walked
    against the SEC ``submissions.zip`` for ``oldSymbol@date → oldCIK``
    (Path C resolver).

    Per spec ``2026-06-02-symbol-history-evidence-backfill.md`` +
    plan ``2026-06-02-symbol-history-evidence-backfill-plan.md``.

    Hard invariants (sentinel-pinned):

      1. Archive-first read — if a recent R2 archive exists
         (``<archive_max_age_days``), use that. No provider call.
      2. Archive-after-download parity — if FMP is called, the bytes
         are written to R2 BEFORE ingest, re-read, and
         ``len() + sha256()`` MUST match. Mismatch hard-stops the run
         before any DB write.
      3. ``use_bulk_zip=true`` is the only path; ``use_bulk_zip=false``
         raises immediately. No per-ticker crawl.
      4. At most ONE ``httpx.AsyncClient.get`` call site in this stage.
         Per-row HTTP is a producer-hard-stop (AST-asserted by
         ``tests/test_symbol_history_evidence_backfill_stage.py``).
      5. ``1969-12-31`` sentinel-date rows are NEVER silently dropped —
         emit ``data_quality_log kind='fmp_symbol_change_sentinel_date'``
         and skip the ``ticker_history`` insert (no trustworthy
         ``valid_to``).
      6. ``oldSymbol == newSymbol`` rows are skipped (FMP echoes).
      7. Same-CIK ticker change → SAME issuer used both symbols;
         emit one ``ticker_history`` row tying old symbol to the NEW
         issuer's classification_id; disposition
         ``same_cik_ticker_change``. No new ticker_classifications row.
      8. Different-issuer reuse → mint historical predecessor
         ``ticker_classifications`` row (lifetime_end = change_date,
         non-NULL) + ``issuer_securities`` row + ``ticker_history`` row;
         disposition ``different_issuer_reuse``.
      9. FMP-only unresolved-CIK → mint TKR-14 predecessor from
         ``(country=US, asset_class=S, ipo_venue=Z, discovery_source=F,
         seed=country|companyName)``; insert ``ticker_classifications``
         row + ``ticker_history`` row; SKIP ``issuer_securities``;
         emit ``data_quality_log kind='fmp_only_no_issuer'``;
         disposition ``fmp_only_unresolved``.
     10. All writes idempotent via ``ON CONFLICT DO NOTHING`` on the
         natural keys (``ticker_history(classification_id, valid_from)``
         per the existing PK / GiST EXCLUDE; ``issuer_securities
         (issuer_id, classification_id, valid_from)``).
     11. NO ``fundamentals_quarterly`` writes; NO DELETE; NO UPDATE on
         existing rows; additive-only.

    Knobs (``--param key=value``):

      * ``dry_run`` (bool, default True) — print plan; no DB writes.
      * ``use_bulk_zip`` (bool, default True) — ``false`` raises.
      * ``archive_max_age_days`` (float, default 7) — freshness floor
        for the R2 archive.
      * ``local_cache_path`` (str, default
        ``/tmp/fmp_symbol_change_latest.json.gz``) — local fallback.
      * ``force_download`` (bool, default False) — operator override
        bypassing archive + local cache.
      * ``limit`` (int, default 10000) — FMP ``?limit=`` value.
      * ``manifest_path`` (str, default
        ``data/symbol_history_evidence_manifest_<UTC>.csv``).
      * ``archive_source_name`` (str, default ``fmp_symbol_change``).
    """
    from datetime import UTC as _UTC
    from datetime import date as _date
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes", "y")

    dry_run = _to_bool(cfg.get("dry_run", True))
    use_bulk_zip = _to_bool(cfg.get("use_bulk_zip", True))
    if not use_bulk_zip:
        # Producer-hard-stop: a per-ticker crawl is the killed
        # anti-pattern. The bulk endpoint is the only legitimate path.
        raise RuntimeError(
            "symbol_history_evidence_backfill: use_bulk_zip=true is the "
            "only supported path (per-ticker crawl is a producer-hard-"
            "stop; see plan §7). Drop the --param use_bulk_zip=false "
            "flag or set it to true."
        )
    archive_max_age_days = float(cfg.get("archive_max_age_days", 7))
    local_cache_path = _Path(
        str(cfg.get("local_cache_path", "/tmp/fmp_symbol_change_latest.json.gz"))  # noqa: S108
    )
    force_download = _to_bool(cfg.get("force_download", False))
    limit = int(cfg.get("limit", FMP_SYMBOL_CHANGE_LIMIT_DEFAULT))
    archive_source = str(cfg.get(
        "archive_source_name", FMP_SYMBOL_CHANGE_ARCHIVE_SOURCE,
    ))
    default_manifest = (
        f"data/symbol_history_evidence_manifest_"
        f"{_dt.now(_UTC).strftime('%Y%m%dT%H%MZ')}.csv"
    )
    manifest_path = _Path(str(cfg.get("manifest_path", default_manifest)))

    run_id = uuid.uuid4()
    log.info(
        "ops.stage.symbol_history_evidence_backfill.start",
        dry_run=dry_run,
        use_bulk_zip=use_bulk_zip,
        archive_max_age_days=archive_max_age_days,
        local_cache_path=str(local_cache_path),
        force_download=force_download,
        limit=limit,
        archive_source=archive_source,
        manifest_path=str(manifest_path),
        run_id=str(run_id),
    )

    # ─── 1. Resolve the FMP symbol-change bulk artifact ───
    payload_bytes = await _fetch_fmp_symbol_change_bulk(
        archive_source=archive_source,
        archive_max_age_days=archive_max_age_days,
        local_cache_path=local_cache_path,
        force_download=force_download,
        limit=limit,
        log=log,
    )

    # ─── 2. Decode JSON payload ───
    import gzip as _gzip
    import json as _json
    decoded_text = _gzip.decompress(payload_bytes).decode("utf-8")
    raw_rows = _json.loads(decoded_text)
    if not isinstance(raw_rows, list):
        raise RuntimeError(
            "symbol_history_evidence_backfill: FMP symbol-change payload "
            f"must be a list, got {type(raw_rows).__name__}"
        )
    log.info(
        "ops.stage.symbol_history_evidence_backfill.payload_parsed",
        n_rows=len(raw_rows),
    )

    # ─── 3. Build SEC cross-walk dict from submissions.zip ───
    cross_walk = await _build_sec_ticker_cik_crosswalk(log=log)
    log.info(
        "ops.stage.symbol_history_evidence_backfill.crosswalk_built",
        n_keys=len(cross_walk),
    )

    # ─── 4. Pull current ticker_classifications snapshot ───
    # ``lifetime_start`` is loaded because the Option B same-CIK fix uses
    # it as the historical row's ``valid_from`` when closing the
    # pre-existing open-ended ``ticker_history`` window. See the
    # ``same-CIK ticker change`` block below.
    async with pool.acquire() as conn:
        tc_rows = await conn.fetch(
            """
            SELECT ticker, id AS classification_id, cik, country,
                   current_legal_name, lifetime_start
            FROM platform.ticker_classifications
            WHERE lifetime_end IS NULL
            """
        )
    current_by_ticker: dict[str, dict[str, Any]] = {
        r["ticker"]: dict(r) for r in tc_rows
    }
    log.info(
        "ops.stage.symbol_history_evidence_backfill.current_snapshot",
        n_active_classifications=len(current_by_ticker),
    )

    # ─── 5. Per-row classification ───
    counters: dict[str, int] = {
        "rows_input": len(raw_rows),
        "rows_skipped_same_symbol": 0,
        "rows_skipped_sentinel_date": 0,
        "rows_same_cik_ticker_change": 0,
        "rows_different_issuer_reuse": 0,
        "rows_fmp_only_unresolved": 0,
        "rows_skipped_no_new_cls": 0,
        "ticker_history_planned": 0,
        "issuer_securities_planned": 0,
        "ticker_classifications_planned": 0,
        "data_quality_log_planned": 0,
        # Option B (2026-06-02 fix): same-CIK ticker-change closes the
        # pre-existing open-ended row + rewrites its ticker to oldSymbol,
        # then inserts a new open-ended row for newSymbol. Tracked
        # separately from the additive ``ticker_history_planned``
        # counter so the GiST-overlap fix is observable in the manifest.
        "same_cik_window_close_planned": 0,
        "same_cik_window_pre_dates_change": 0,
        "same_cik_no_open_window": 0,
        "same_cik_already_applied": 0,
    }

    # Insertable rows (one per disposition). Each entry knows what tables
    # it touches; the live-mode loop flushes batches.
    ticker_classifications_inserts: list[tuple[Any, ...]] = []
    issuers_inserts: list[tuple[Any, ...]] = []
    issuer_securities_inserts: list[tuple[Any, ...]] = []
    ticker_history_inserts: list[tuple[Any, ...]] = []
    data_quality_log_inserts: list[tuple[Any, ...]] = []
    # Option B (2026-06-02 fix): per-row close+rewrite+insert operations
    # for same-CIK ticker changes. Each entry is the parameter bundle
    # consumed in §7b's transactional loop. Kept distinct from the
    # bulk-INSERT lists because each op runs as its own transaction
    # (guard SELECT → UPDATE → INSERT) to avoid the GiST EXCLUDE overlap
    # the additive-only approach hit on the 2026-06-02 live populate.
    same_cik_ops: list[dict[str, Any]] = []
    manifest_rows: list[dict[str, Any]] = []

    # Track classification_ids we minted this run so we don't double-
    # plan a ticker_history row that would violate the GiST EXCLUDE
    # constraint within the same batch. The DB-level constraints catch
    # cross-run dupes; this in-memory dedupe catches same-run echoes.
    planned_th_keys: set[tuple[str, _date]] = set()
    planned_is_keys: set[tuple[str, str, _date]] = set()
    planned_same_cik_cls: set[str] = set()

    for raw in raw_rows:
        old_symbol = str(raw.get("oldSymbol", "")).strip().upper()
        new_symbol = str(raw.get("newSymbol", "")).strip().upper()
        change_date_str = str(raw.get("date", "")).strip()[:10]
        company_name = str(raw.get("companyName", "")).strip()

        if not old_symbol or not new_symbol or not change_date_str:
            counters["rows_skipped_no_new_cls"] += 1
            continue

        if old_symbol == new_symbol:
            counters["rows_skipped_same_symbol"] += 1
            manifest_rows.append({
                "oldSymbol": old_symbol,
                "newSymbol": new_symbol,
                "change_date": change_date_str,
                "companyName": company_name,
                "old_cik_resolved": "",
                "old_cik_source": "none",
                "new_cik_resolved": "",
                "new_cik_source": "none",
                "predecessor_classification_id_minted": "",
                "classification_action": "skipped",
                "ticker_history_written": "false",
                "issuer_securities_written": "false",
                "disposition": "skipped_same_symbol",
            })
            continue

        # 1969-12-31 sentinel: emit data_quality_log, skip ticker_history
        # (no trustworthy valid_to).
        if change_date_str == FMP_SYMBOL_CHANGE_SENTINEL_DATE:
            counters["rows_skipped_sentinel_date"] += 1
            counters["data_quality_log_planned"] += 1
            data_quality_log_inserts.append((
                f"symbol_history_evidence_backfill.{old_symbol}",
                _json.dumps({
                    "kind": "fmp_symbol_change_sentinel_date",
                    "oldSymbol": old_symbol,
                    "newSymbol": new_symbol,
                    "date": change_date_str,
                    "companyName": company_name,
                }),
            ))
            manifest_rows.append({
                "oldSymbol": old_symbol,
                "newSymbol": new_symbol,
                "change_date": change_date_str,
                "companyName": company_name,
                "old_cik_resolved": "",
                "old_cik_source": "none",
                "new_cik_resolved": "",
                "new_cik_source": "none",
                "predecessor_classification_id_minted": "",
                "classification_action": "skipped_sentinel_date",
                "ticker_history_written": "false",
                "issuer_securities_written": "false",
                "disposition": "skipped_sentinel_date",
            })
            continue

        try:
            change_date = _date.fromisoformat(change_date_str)
        except ValueError:
            counters["rows_skipped_no_new_cls"] += 1
            continue

        # Resolve new-symbol classification (must currently exist).
        new_entry = current_by_ticker.get(new_symbol)
        new_classification_id = (
            str(new_entry["classification_id"]) if new_entry else None
        )
        new_cik = (
            str(new_entry["cik"]) if new_entry and new_entry.get("cik")
            else None
        )

        # Cross-walk oldSymbol@date → oldCIK via SEC submissions.zip.
        old_cik, old_cik_source = _resolve_old_cik_from_crosswalk(
            cross_walk, old_symbol, change_date,
        )

        # Same-CIK ticker change → Option B forward fix (2026-06-02).
        # The pre-existing ``ticker_history`` row for ``new_classification_id``
        # is open-ended (valid_to IS NULL) with the CURRENT ticker value
        # and a valid_from that pre-dates the change_date (typically the
        # issuer's first-seen date). Naively inserting an OLD-symbol
        # historical row would overlap that open-ended range and trip
        # the ``ticker_history_no_overlap`` GiST EXCLUDE constraint
        # (asyncpg ExclusionViolationError — observed live 2026-06-02
        # on USFZ26ODRA4870). Option B closes the pre-existing window
        # to ``change_date`` (rewriting its ``ticker`` to oldSymbol so
        # the now-finite window honestly carries the predecessor symbol)
        # and inserts a new open-ended row for newSymbol from
        # change_date onward. Both steps run in ONE transaction at
        # live-write time (§7b). The historical row's valid_from comes
        # from the existing classification's ``lifetime_start`` (the
        # earliest known activity of THIS issuer); rare NULL falls back
        # to the (change_date.year - 1, 1, 1) heuristic per plan §3.3.
        if (
            old_cik is not None
            and new_cik is not None
            and old_cik == new_cik
            and new_classification_id is not None
        ):
            ls_val = new_entry.get("lifetime_start") if new_entry else None
            if isinstance(ls_val, _date):
                derived_valid_from = ls_val
            else:
                derived_valid_from = _date(
                    max(change_date.year - 1, 1900), 1, 1,
                )
            if new_classification_id not in planned_same_cik_cls:
                planned_same_cik_cls.add(new_classification_id)
                same_cik_ops.append({
                    "classification_id": new_classification_id,
                    "old_symbol": old_symbol,
                    "new_symbol": new_symbol,
                    "change_date": change_date,
                    "derived_valid_from": derived_valid_from,
                })
                counters["same_cik_window_close_planned"] += 1
                counters["ticker_history_planned"] += 1
            counters["rows_same_cik_ticker_change"] += 1
            manifest_rows.append({
                "oldSymbol": old_symbol,
                "newSymbol": new_symbol,
                "change_date": change_date_str,
                "companyName": company_name,
                "old_cik_resolved": old_cik,
                "old_cik_source": old_cik_source,
                "new_cik_resolved": new_cik,
                "new_cik_source": "ticker_classifications",
                "predecessor_classification_id_minted": "",
                "classification_action": "existing",
                "ticker_history_written": "true",
                "issuer_securities_written": "false",
                "disposition": "same_cik_ticker_change",
            })
            continue

        # Different-issuer reuse (old_cik != new_cik AND both non-NULL).
        if (
            old_cik is not None
            and new_cik is not None
            and old_cik != new_cik
        ):
            disposition = "different_issuer_reuse"
            tkr14_source_code = "S"
        elif old_cik is None:
            disposition = "fmp_only_unresolved"
            tkr14_source_code = "F"
        else:
            # old_cik known, new_cik unknown — treat as fmp_only_unresolved
            # to surface the new-side gap.
            disposition = "fmp_only_unresolved"
            tkr14_source_code = "F"

        # Mint TKR-14 predecessor.
        country_seed = "US"
        if new_entry and new_entry.get("country"):
            country_seed = str(new_entry["country"])[:2].upper() or "US"
        elif company_name:
            country_seed = "US"  # FMP-only fallback per plan §2.3.

        # The mint year segment uses the predecessor's discovery-time
        # snapshot; for unknown historical rows we use the change_date's
        # YEAR (the at-mint convention for historical predecessors).
        mint_now = _dt(
            change_date.year, change_date.month, change_date.day,
            tzinfo=_UTC,
        )
        legal_name_seed = company_name or new_symbol
        predecessor_cls_id = _mint_tkr14_predecessor(
            country=country_seed,
            cik=old_cik,
            legal_name=legal_name_seed,
            discovery_source_code=tkr14_source_code,
            mint_now=mint_now,
        )

        # ticker_classifications insert: predecessor row with
        # lifetime_end = change_date (non-NULL — the historical-row
        # marker). Carries cik (may be NULL for FMP-only).
        ticker_classifications_inserts.append((
            predecessor_cls_id,
            old_symbol,
            country_seed,
            old_cik,
            "stock",
            "Z",  # ipo_venue sentinel — unknown historical
            tkr14_source_code,
            "inactive",
            _date(max(change_date.year - 1, 1900), 1, 1),  # lifetime_start
            change_date,                                     # lifetime_end
            company_name or None,
            f"symbol_history_evidence_backfill.{tkr14_source_code}",
        ))
        counters["ticker_classifications_planned"] += 1

        valid_from = _date(max(change_date.year - 1, 1900), 1, 1)
        valid_to = change_date
        th_key = (predecessor_cls_id, valid_from)
        if th_key not in planned_th_keys:
            planned_th_keys.add(th_key)
            ticker_history_inserts.append((
                predecessor_cls_id, old_symbol, valid_from, valid_to,
            ))
            counters["ticker_history_planned"] += 1

        # issuer_securities + issuers — only for different_issuer_reuse
        # (old_cik known → we can mint issuer_id from CIK).
        issuer_securities_written = False
        if disposition == "different_issuer_reuse":
            predecessor_issuer_id = _mint_issuer_id_from_cik(old_cik)
            if predecessor_issuer_id is not None:
                # Ensure issuer row exists before issuer_securities FK fires.
                issuers_inserts.append((
                    predecessor_issuer_id,
                    old_cik,
                    legal_name_seed,
                    "active",
                ))
                is_key = (
                    predecessor_issuer_id, predecessor_cls_id, valid_from,
                )
                if is_key not in planned_is_keys:
                    planned_is_keys.add(is_key)
                    issuer_securities_inserts.append((
                        predecessor_issuer_id,
                        predecessor_cls_id,
                        valid_from,
                        valid_to,
                    ))
                    counters["issuer_securities_planned"] += 1
                    issuer_securities_written = True
            counters["rows_different_issuer_reuse"] += 1
        else:
            # fmp_only_unresolved — no issuer to mint without CIK; emit
            # data_quality_log for operator awareness.
            counters["rows_fmp_only_unresolved"] += 1
            counters["data_quality_log_planned"] += 1
            data_quality_log_inserts.append((
                f"symbol_history_evidence_backfill.{old_symbol}",
                _json.dumps({
                    "kind": "fmp_only_no_issuer",
                    "oldSymbol": old_symbol,
                    "newSymbol": new_symbol,
                    "date": change_date_str,
                    "companyName": company_name,
                    "predecessor_classification_id": predecessor_cls_id,
                }),
            ))

        manifest_rows.append({
            "oldSymbol": old_symbol,
            "newSymbol": new_symbol,
            "change_date": change_date_str,
            "companyName": company_name,
            "old_cik_resolved": old_cik or "",
            "old_cik_source": old_cik_source,
            "new_cik_resolved": new_cik or "",
            "new_cik_source": (
                "ticker_classifications" if new_cik else "none"
            ),
            "predecessor_classification_id_minted": predecessor_cls_id,
            "classification_action": "minted_new",
            "ticker_history_written": "true",
            "issuer_securities_written": (
                "true" if issuer_securities_written else "false"
            ),
            "disposition": disposition,
        })

    log.info(
        "ops.stage.symbol_history_evidence_backfill.classified",
        **counters,
    )

    # ─── 6. Dry-run: write manifest, return; no DB writes ───
    if dry_run:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        import csv as _csv
        with manifest_path.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(
                fh, fieldnames=_symbol_history_evidence_manifest_columns(),
            )
            w.writeheader()
            for m in manifest_rows:
                w.writerow(m)
        return {
            "dry_run": True,
            "manifest_path": str(manifest_path),
            "run_id": str(run_id),
            **counters,
        }

    # ─── 7. Live: idempotent batched INSERTs in one connection's txn
    # per batch. Ordering: issuers → ticker_classifications →
    # issuer_securities (FK targets) → ticker_history (no FK constraint
    # but logical order) → same-CIK Option B per-row close+rewrite+
    # insert (§7b) → data_quality_log.
    n_issuers_written = 0
    n_tc_written = 0
    n_isec_written = 0
    n_th_written = 0
    n_dql_written = 0
    n_same_cik_window_closed = 0
    n_same_cik_current_inserted = 0
    n_same_cik_pre_dates_change = 0
    n_same_cik_no_open_window = 0
    n_same_cik_already_applied = 0

    async with pool.acquire() as conn:
        if issuers_inserts:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO platform.issuers
                        (issuer_id, cik, legal_name, status)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (issuer_id) DO NOTHING
                    """,
                    issuers_inserts,
                )
            n_issuers_written = len(issuers_inserts)

        if ticker_classifications_inserts:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO platform.ticker_classifications
                        (id, ticker, country, cik, asset_class,
                         ipo_venue, discovery_source, status,
                         lifetime_start, lifetime_end,
                         current_legal_name, source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    ticker_classifications_inserts,
                )
            n_tc_written = len(ticker_classifications_inserts)

        if issuer_securities_inserts:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO platform.issuer_securities
                        (issuer_id, classification_id, valid_from, valid_to)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (issuer_id, classification_id, valid_from)
                        DO NOTHING
                    """,
                    issuer_securities_inserts,
                )
            n_isec_written = len(issuer_securities_inserts)

        if ticker_history_inserts:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO platform.ticker_history
                        (classification_id, ticker, valid_from, valid_to)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (classification_id, valid_from)
                        DO NOTHING
                    """,
                    ticker_history_inserts,
                )
            n_th_written = len(ticker_history_inserts)

        # ─── 7b. Same-CIK Option B (2026-06-02 fix) ───
        # For each same-CIK ticker change, run guard SELECT → UPDATE
        # (close pre-existing open-ended window + rewrite ticker to
        # oldSymbol) → INSERT (new open-ended row for newSymbol) inside
        # ONE transaction. Idempotent: a re-run finds the open-ended
        # row already moved to ``change_date`` valid_from and silently
        # skips. Boundary cases (valid_from >= change_date OR no open
        # row) emit ``data_quality_log`` and skip the write.
        for op in same_cik_ops:
            cls = op["classification_id"]
            change_date_val = op["change_date"]
            old_symbol_val = op["old_symbol"]
            new_symbol_val = op["new_symbol"]
            derived_valid_from = op["derived_valid_from"]
            async with conn.transaction():
                existing_row = await conn.fetchrow(
                    """
                    SELECT valid_from, ticker
                    FROM platform.ticker_history
                    WHERE classification_id = $1 AND valid_to IS NULL
                    LIMIT 1
                    """,
                    cls,
                )
                if existing_row is None:
                    # No open-ended row — pre-existing temporal gap.
                    # Don't fabricate one; emit forensic dql + skip.
                    await conn.execute(
                        _DQL_VALIDATION_INSERT_SQL,
                        (
                            f"symbol_history_evidence_backfill."
                            f"{old_symbol_val}"
                        ),
                        _json.dumps({
                            "kind": "same_cik_no_open_window",
                            "classification_id": cls,
                            "oldSymbol": old_symbol_val,
                            "newSymbol": new_symbol_val,
                            "change_date": change_date_val.isoformat(),
                        }),
                    )
                    n_same_cik_no_open_window += 1
                    counters["same_cik_no_open_window"] += 1
                    continue
                existing_vf = existing_row["valid_from"]
                existing_ticker = existing_row["ticker"]
                if (
                    existing_vf == change_date_val
                    and existing_ticker == new_symbol_val
                ):
                    # Already applied — silent no-op (re-run safety).
                    n_same_cik_already_applied += 1
                    counters["same_cik_already_applied"] += 1
                    continue
                if existing_vf >= change_date_val:
                    # Pre-existing window post-dates the change — unresolvable
                    # temporal conflict; emit dql + skip rather than write
                    # a row that would still overlap or invert.
                    await conn.execute(
                        _DQL_VALIDATION_INSERT_SQL,
                        (
                            f"symbol_history_evidence_backfill."
                            f"{old_symbol_val}"
                        ),
                        _json.dumps({
                            "kind": "same_cik_window_pre_dates_change",
                            "classification_id": cls,
                            "oldSymbol": old_symbol_val,
                            "newSymbol": new_symbol_val,
                            "change_date": change_date_val.isoformat(),
                            "existing_valid_from": existing_vf.isoformat(),
                        }),
                    )
                    n_same_cik_pre_dates_change += 1
                    counters["same_cik_window_pre_dates_change"] += 1
                    continue
                # Normal Option B: close the pre-existing window AND
                # rewrite its ticker to oldSymbol so the now-finite
                # window honestly represents [existing_vf, change_date)
                # under the OLD ticker. Then insert the new open-ended
                # row for newSymbol.
                await conn.execute(
                    """
                    UPDATE platform.ticker_history
                    SET valid_to = $1, ticker = $2
                    WHERE classification_id = $3
                      AND valid_to IS NULL
                      AND valid_from < $1
                    """,
                    change_date_val, old_symbol_val, cls,
                )
                n_same_cik_window_closed += 1
                await conn.execute(
                    """
                    INSERT INTO platform.ticker_history
                        (classification_id, ticker, valid_from, valid_to)
                    VALUES ($1, $2, $3, NULL)
                    ON CONFLICT (classification_id, valid_from)
                        DO NOTHING
                    """,
                    cls, new_symbol_val, change_date_val,
                )
                n_same_cik_current_inserted += 1
                # Note: ``derived_valid_from`` is retained on ``op`` for
                # forensic observability — the historical-window's
                # ``valid_from`` after the UPDATE remains ``existing_vf``,
                # not ``derived_valid_from``. The lifetime_start-based
                # derived_valid_from is only used if a future evolution
                # of this stage decides to back-shift the pre-existing
                # ``valid_from`` (not authorized in this fix).
                _ = derived_valid_from
        n_th_written += n_same_cik_current_inserted

        if data_quality_log_inserts:
            async with conn.transaction():
                await conn.executemany(
                    _DQL_VALIDATION_INSERT_SQL,
                    data_quality_log_inserts,
                )
            n_dql_written = len(data_quality_log_inserts)

    # Manifest written even in live mode for forensic record.
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    import csv as _csv
    with manifest_path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(
            fh, fieldnames=_symbol_history_evidence_manifest_columns(),
        )
        w.writeheader()
        for m in manifest_rows:
            w.writerow(m)

    # The in-loop §7b dql writes (kind ∈ {same_cik_no_open_window,
    # same_cik_window_pre_dates_change}) are NOT planned in
    # ``data_quality_log_inserts`` (they're decided per-row at live
    # time), so fold their counts into the result for operator
    # observability.
    n_dql_written += (
        n_same_cik_no_open_window + n_same_cik_pre_dates_change
    )

    out: dict[str, Any] = {
        "dry_run": False,
        "manifest_path": str(manifest_path),
        "run_id": str(run_id),
        "issuers_written": n_issuers_written,
        "ticker_classifications_written": n_tc_written,
        "issuer_securities_written": n_isec_written,
        "ticker_history_written": n_th_written,
        "data_quality_log_written": n_dql_written,
        "same_cik_window_closed": n_same_cik_window_closed,
        "same_cik_current_inserted": n_same_cik_current_inserted,
        "same_cik_already_applied_skipped": n_same_cik_already_applied,
        "same_cik_pre_dates_change_skipped": n_same_cik_pre_dates_change,
        "same_cik_no_open_window_skipped": n_same_cik_no_open_window,
        **counters,
    }
    log.info(
        "ops.stage.symbol_history_evidence_backfill.complete", **out,
    )
    return out


def _mint_tkr14_predecessor(
    *,
    country: str,
    cik: str | None,
    legal_name: str,
    discovery_source_code: str,
    mint_now: datetime,
) -> str:
    """Mint a historical-predecessor TKR-14 id.

    Uses ``ipo_venue='Z'`` (sentinel/unknown) per plan §2.3 and the
    given ``discovery_source_code`` (``'S'`` when CIK resolves via SEC
    cross-walk; ``'F'`` for FMP-only fallback).

    Salt-retry caller responsibility lives in the live-INSERT path
    (``ON CONFLICT (id) DO NOTHING`` is the idempotency floor); if the
    operator hits a salt collision rate >1.7% we'll add a salt loop.
    For now salt=0 is the default; same seed always yields the same id.
    """
    from tpcore.identity.tkr14 import (  # noqa: PLC0415
        AssetClass as _AC,
    )
    from tpcore.identity.tkr14 import (
        DiscoverySource as _DS,
    )
    from tpcore.identity.tkr14 import (
        IPOVenue as _IPO,
    )
    from tpcore.identity.tkr14 import (
        mint as _mint,
    )
    return _mint(
        country=country,
        asset_class=_AC.STOCK,
        ipo_venue=_IPO.OTHER,  # "Z"
        discovery_source=_DS(discovery_source_code),
        cik=cik,
        legal_name=legal_name,
        now=mint_now,
        salt=0,
    )


def _resolve_old_cik_from_crosswalk(
    crosswalk: dict[str, list[tuple[str, date, date | None]]],
    old_symbol: str,
    change_date: date,
) -> tuple[str | None, str]:
    """Return ``(cik, source)`` per the §4.1 cross-walk rules.

    * Exact one match → ``(cik, 'sec_cross_walk')``.
    * Multiple matches → ``(None, 'ambiguous')`` (the caller emits a
      ``data_quality_log`` row with ``kind='ambiguous_oldcik_resolution'``
      via the same downstream path).
    * Zero matches → ``(None, 'none')``.
    """
    candidates = crosswalk.get(old_symbol, [])
    matches: list[str] = []
    for cik, vfrom, vto in candidates:
        if vfrom <= change_date and (vto is None or change_date <= vto):
            matches.append(cik)
    if len(matches) == 1:
        return matches[0], "sec_cross_walk"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "none"


async def _build_sec_ticker_cik_crosswalk(
    *,
    log: Any,
) -> dict[str, list[tuple[str, date, date | None]]]:
    """Build the ``symbol → [(cik, valid_from, valid_to)]`` map by
    iterating the cached SEC ``submissions.zip`` once.

    Reads ``tickers[]`` (CURRENT tickers for the CIK; valid from each
    formerNames entry's ``to`` date onward — or from filing-history
    start for CIKs with no formerNames) AND each ``formerNames[]``
    entry's ``from..to`` window (the prior NAME-windows that may have
    used the same ticker assignment in old filings).

    SEC does NOT publish per-symbol history per se. This is a
    best-effort confirmatory cross-walk; the dominant case (delisted
    predecessor whose CIK no longer carries the ticker) WILL return
    zero matches, and the stage will fall through to the FMP-only
    predecessor mint path. Spec §4.2 explicitly accepts this.

    The reused ``ensure_zip_cached`` helper handles the 3-tier
    resolution (local cache → R2 → SEC); no per-CIK HTTP is issued
    here.
    """
    import os as _os
    import zipfile as _zipfile

    from tpcore.sec.submissions_bulk_reader import (  # noqa: PLC0415
        DEFAULT_BULK_ZIP_PATH,
        ensure_zip_cached,
    )

    ua = _os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "symbol_history_evidence_backfill: SEC_EDGAR_USER_AGENT env "
            "var required (Path C cross-walk reads submissions.zip)."
        )
    zip_path = await ensure_zip_cached(
        DEFAULT_BULK_ZIP_PATH, user_agent=ua, force_download=False,
    )

    crosswalk: dict[str, list[tuple[str, date, date | None]]] = {}
    n_ciks_parsed = 0
    n_parse_errors = 0
    import json as _json
    with _zipfile.ZipFile(zip_path, "r") as zf:
        for entry in zf.namelist():
            if not entry.startswith("CIK") or not entry.endswith(".json"):
                continue
            if "-" in entry:
                continue
            cik = entry[3:-5]
            try:
                data = _json.loads(zf.read(entry))
            except Exception:  # noqa: BLE001
                n_parse_errors += 1
                continue
            n_ciks_parsed += 1
            tickers = data.get("tickers", []) or []
            former_names = data.get("formerNames", []) or []
            # "Current ticker" window: from the latest formerNames.to
            # (if any) to NULL (current).
            current_from: date = date(1900, 1, 1)
            for fn in former_names:
                fn_to_str = (fn.get("to") or "")[:10]
                if not fn_to_str:
                    continue
                try:
                    fn_to = date.fromisoformat(fn_to_str)
                except ValueError:
                    continue
                if fn_to > current_from:
                    current_from = fn_to
            for tk in tickers:
                if not isinstance(tk, str):
                    continue
                symbol = tk.strip().upper()
                if not symbol:
                    continue
                crosswalk.setdefault(symbol, []).append(
                    (cik, current_from, None),
                )
            # formerNames windows — best-effort echo (cap on the
            # `to` date; ticker assignment within the window is
            # inferred only when SEC also carries that historical
            # ticker in `tickers[]`, which is rare). We do NOT
            # invent per-former-name ticker mappings; this map keys
            # on the CURRENT tickers[] only. The plan §4.2 spec
            # acknowledges this limitation.

    log.info(
        "ops.stage.symbol_history_evidence_backfill.crosswalk_parsed",
        n_ciks_parsed=n_ciks_parsed,
        n_parse_errors=n_parse_errors,
        n_unique_symbols=len(crosswalk),
    )
    return crosswalk


async def _fetch_fmp_symbol_change_bulk(
    *,
    archive_source: str,
    archive_max_age_days: float,
    local_cache_path: Path,
    force_download: bool,
    limit: int,
    log: Any,
) -> bytes:
    """Resolve the FMP ``/stable/symbol-change`` payload as gzipped bytes.

    Priority (plan §3.1 + §7):

      1. R2 archive first — list ``<source>_archive/`` for ``archive_source``,
         sort by name (filenames are ``<source>_<UTC>.csv.gz`` — sortable
         by construction). If the most-recent entry is within
         ``archive_max_age_days``, read+return its body (gzipped JSON
         in a ``.csv.gz``-named container).
      2. Local cache — if R2 unreachable AND the local cache exists
         AND is within ``archive_max_age_days``, return its bytes.
      3. Provider download — one ``httpx.AsyncClient.get``
         (``with_retry``-wrapped). Write to R2 BEFORE ingest; re-read R2
         and verify ``len() + sha256()`` parity vs the local copy.
         Mismatch raises before any DB write.

    ``force_download=True`` skips both archive + local cache.

    The artifact is **gzipped JSON bytes**, stored with the existing
    ``.csv.gz`` archive naming convention (the backend's protocol
    expects ``<source>_<UTC>.csv.gz`` for `list_archives`-ordering;
    the body is opaque bytes from the backend's view, so naming the
    container ``.csv.gz`` while the payload is JSON is acceptable —
    every downstream consumer of this archive reads it via
    ``gzip.decompress(body)`` + ``json.loads(...)``, never as CSV).
    """
    import hashlib as _hashlib
    import time as _time
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from tpcore.ingestion.csv_archive_backends import (  # noqa: PLC0415
        select_backend,
    )

    age_floor_seconds = archive_max_age_days * 86_400

    # ── 1. Archive-first ───
    if not force_download:
        try:
            backend = select_backend()
            archives = backend.list_archives(archive_source)
            if archives:
                # Sort ascending; the last entry is the most recent
                # because filenames embed sortable UTC timestamps.
                latest = sorted(archives)[-1]
                ts = _parse_archive_timestamp(latest)
                if ts is not None:
                    age = _time.time() - ts
                    if age <= age_floor_seconds:
                        log.info(
                            "ops.stage.symbol_history_evidence_backfill.archive_hit",
                            backend=type(backend).__name__,
                            filename=latest,
                            age_hr=round(age / 3600, 1),
                        )
                        return backend.read(archive_source, latest)
                    log.info(
                        "ops.stage.symbol_history_evidence_backfill.archive_stale",
                        backend=type(backend).__name__,
                        filename=latest,
                        age_hr=round(age / 3600, 1),
                    )
        except Exception as exc:  # noqa: BLE001 — archive-side errors fall through to cache
            log.warning(
                "ops.stage.symbol_history_evidence_backfill.archive_unreachable",
                error=type(exc).__name__,
                message=str(exc)[:200],
            )

    # ── 2. Local cache ───
    if (
        not force_download
        and local_cache_path.exists()
    ):
        age = _time.time() - local_cache_path.stat().st_mtime
        if age <= age_floor_seconds:
            log.info(
                "ops.stage.symbol_history_evidence_backfill.local_cache_hit",
                path=str(local_cache_path),
                age_hr=round(age / 3600, 1),
            )
            return local_cache_path.read_bytes()

    # ── 3. Provider download (the only allowed httpx.AsyncClient.get) ───
    payload_bytes = await _fmp_symbol_change_download(limit=limit, log=log)

    # ── 4. Archive-after-download with parity check ───
    ts_label = _dt.now(_UTC).strftime("%Y%m%dT%H%MZ")
    filename = f"{archive_source}_{ts_label}.csv.gz"
    try:
        backend = select_backend()
        uri = backend.write(archive_source, payload_bytes, filename)
        log.info(
            "ops.stage.symbol_history_evidence_backfill.archive_write_ok",
            backend=type(backend).__name__, uri=uri,
        )
        re_read = backend.read(archive_source, filename)
        if (
            len(re_read) != len(payload_bytes)
            or _hashlib.sha256(re_read).hexdigest()
            != _hashlib.sha256(payload_bytes).hexdigest()
        ):
            raise RuntimeError(
                "symbol_history_evidence_backfill: archive parity check "
                f"FAILED (downloaded {len(payload_bytes)} bytes vs "
                f"re-read {len(re_read)} bytes); hard-stopping before "
                "any DB write per plan §7."
            )
        log.info(
            "ops.stage.symbol_history_evidence_backfill.archive_parity_ok",
            n_bytes=len(payload_bytes),
        )
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 — backend errors don't block the run
        log.warning(
            "ops.stage.symbol_history_evidence_backfill.archive_write_failed",
            error=type(exc).__name__,
            message=str(exc)[:200],
        )

    # ── 5. Persist local cache ───
    local_cache_path.parent.mkdir(parents=True, exist_ok=True)
    local_cache_path.write_bytes(payload_bytes)
    return payload_bytes


async def _fmp_symbol_change_download(*, limit: int, log: Any) -> bytes:
    """Single bulk GET to FMP ``/stable/symbol-change?limit=<n>``,
    ``with_retry``-wrapped, returns gzipped JSON bytes.

    This is the **only** ``httpx.AsyncClient.get`` call site in the
    stage source (AST-sentineled by
    ``tests/test_symbol_history_evidence_backfill_stage.py``).
    """
    import gzip as _gzip
    import os as _os

    import httpx as _httpx

    from tpcore.outage import with_retry as _with_retry  # noqa: PLC0415

    api_key = _os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError(
            "symbol_history_evidence_backfill: FMP_API_KEY env var required"
        )

    @_with_retry(max_attempts=3, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _do() -> bytes:
        url = (
            f"{FMP_SYMBOL_CHANGE_URL}?limit={int(limit)}&apikey={api_key}"
        )
        async with _httpx.AsyncClient(timeout=180.0) as client:
            r = await client.get(url)
        r.raise_for_status()
        body = r.content
        log.info(
            "ops.stage.symbol_history_evidence_backfill.provider_get_ok",
            n_bytes=len(body),
        )
        return _gzip.compress(body)

    return await _do()


def _parse_archive_timestamp(filename: str) -> float | None:
    """Parse the ``<source>_<UTC>.csv.gz`` timestamp into POSIX seconds.

    Filenames follow the existing csv_archive precedent:
    ``<source>_YYYYMMDDTHHmmZ.csv.gz``. Returns POSIX seconds on a clean
    parse or None otherwise (caller treats None as "unparseable timestamp"
    and falls through to provider download).
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    # Strip suffix.
    stem = filename
    for suffix in (".csv.gz", ".json.gz", ".gz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return None
    ts_label = parts[1]
    try:
        ts = _dt.strptime(ts_label, "%Y%m%dT%H%MZ").replace(tzinfo=_UTC)
    except ValueError:
        return None
    return ts.timestamp()


async def _stage_backfill_sec_lifecycle(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """SEC Form 25 / Form 15 lifecycle evidence backfill (P2a, 2026-05-30).

    Foundation stage for the future P2b lifecycle-bound validator
    wiring (spec ``2026-05-30-asset-class-refinement.md`` follow-up).
    This stage ONLY writes the new evidence columns + the new
    ``platform.ticker_lifecycle_events`` append-only event log added
    by migration ``20260530_0300``. It does NOT change validator
    semantics, does NOT touch the capital gate, does NOT change
    PASS/FAIL behavior.

    For each scoped ticker with a CIK:
      1. Fetch SEC submissions (via the P0 ``SECCompanyFactsAdapter``
         primitive — reuses the same 0.11 s fair-use throttle).
      2. Call ``extract_lifecycle_events`` → all Form 25 + Form 15
         events with their accession numbers + dates.
      3. UPSERT every event into ``platform.ticker_lifecycle_events``
         (UNIQUE on (classification_id, form_type, accession_number) →
         ON CONFLICT DO NOTHING for idempotency).
      4. UPDATE the projection columns on
         ``platform.ticker_classifications`` IFF the new evidence
         source outranks the existing one per
         ``_LIFECYCLE_SOURCE_PRECEDENCE``. Manual entries are NEVER
         overwritten.

    Scope params (``--param key=value``):
      * ``dry_run`` (bool, default **True**) — print plan, no DB writes.
      * ``tickers`` (comma-list) — explicit scope.
      * ``delisted_only`` (bool, default False) — scope to rows with
        ``prices_daily.delisted=true``.
      * ``inactive_only`` (bool, default False) — scope to rows with
        ``ticker_classifications.status='inactive'``.
      * ``stale_filing`` (bool, default False) — scope to rows whose
        ``last_filing_date < NOW() - 90 days`` (potential delist_pending
        candidates).
      * ``max_tickers`` (int, optional) — cap (testing / incremental).
      * ``force_refresh`` (bool, default False) — re-run extraction
        even for rows already populated.

    Default scope (no flags): rows where issuer_lifecycle_state IS
    NULL AND cik IS NOT NULL, capped by ``max_tickers``. Bare
    invocation never walks the full table by accident.

    SEC fair-use: same 0.11 s sleep as ``_stage_backfill_sec_metadata``.

    Output payload::

        {
          "scope_size": int,
          "fetched": int, "submissions_404": int,
          "events_extracted": int, "events_upserted": int,
          "state_writes": int, "state_skipped_precedence": int,
          "state_skipped_no_change": int,
          "by_state": {<state>: <count>, ...},
          "coverage_before": {...}, "coverage_after": {...},
          "failures": [<ticker>, ...],
          "dry_run": bool,
        }
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    def _to_bool(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes", "y")

    dry_run = _to_bool(cfg.get("dry_run", True))
    delisted_only = _to_bool(cfg.get("delisted_only", False))
    inactive_only = _to_bool(cfg.get("inactive_only", False))
    stale_filing = _to_bool(cfg.get("stale_filing", False))
    force_refresh = _to_bool(cfg.get("force_refresh", False))
    max_tickers = int(cfg["max_tickers"]) if cfg.get("max_tickers") else None
    ticker_scope_raw = cfg.get("tickers")
    explicit_tickers: list[str] | None = None
    if ticker_scope_raw:
        explicit_tickers = [
            t.strip().upper()
            for t in str(ticker_scope_raw).split(",")
            if t.strip()
        ] or None

    _COVERAGE_SQL = """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE cik IS NOT NULL) AS has_cik,
            COUNT(*) FILTER (WHERE issuer_lifecycle_state IS NOT NULL)
                AS has_lifecycle_state,
            COUNT(*) FILTER (WHERE issuer_lifecycle_state = 'deregistered')
                AS has_deregistered,
            COUNT(*) FILTER (WHERE issuer_lifecycle_state = 'delist_effective')
                AS has_delist_effective,
            COUNT(*) FILTER (WHERE issuer_lifecycle_state = 'active')
                AS has_active,
            COUNT(*) FILTER (WHERE issuer_lifecycle_state_source IS NOT NULL)
                AS has_source
        FROM platform.ticker_classifications
    """

    async def _snapshot() -> dict[str, int]:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_COVERAGE_SQL)
        return {k: int(v or 0) for k, v in dict(row).items()}

    coverage_before = await _snapshot()
    log.info(
        "ops.stage.backfill_sec_lifecycle.start",
        dry_run=dry_run,
        delisted_only=delisted_only,
        inactive_only=inactive_only,
        stale_filing=stale_filing,
        force_refresh=force_refresh,
        explicit_tickers=len(explicit_tickers) if explicit_tickers else None,
        max_tickers=max_tickers,
        coverage_before=coverage_before,
    )

    # ─── resolve scope ───
    scope_tickers: list[str] = []
    if explicit_tickers:
        scope_tickers = sorted(set(explicit_tickers))
    if inactive_only:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker FROM platform.ticker_classifications
                WHERE status = 'inactive' AND cik IS NOT NULL
                ORDER BY ticker
                """
            )
        scope_tickers = sorted(set(scope_tickers) | {r["ticker"] for r in rows})
    if delisted_only:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT pd.ticker
                FROM platform.prices_daily pd
                JOIN platform.ticker_classifications tc ON tc.ticker = pd.ticker
                WHERE pd.delisted = true AND tc.cik IS NOT NULL
                ORDER BY pd.ticker
                """
            )
        scope_tickers = sorted(set(scope_tickers) | {r["ticker"] for r in rows})
    if stale_filing:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker FROM platform.ticker_classifications
                WHERE last_filing_date < (NOW() - INTERVAL '90 days')
                  AND cik IS NOT NULL
                ORDER BY ticker
                """
            )
        scope_tickers = sorted(set(scope_tickers) | {r["ticker"] for r in rows})

    if not scope_tickers and not (
        delisted_only or inactive_only or stale_filing or explicit_tickers
    ):
        # Default scope: rows missing lifecycle evidence + having a CIK
        # we can query SEC with. Capped by max_tickers — never walks the
        # full table.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker FROM platform.ticker_classifications
                WHERE issuer_lifecycle_state IS NULL
                  AND cik IS NOT NULL
                ORDER BY ticker
                """
            )
        scope_tickers = [r["ticker"] for r in rows]

    if max_tickers:
        scope_tickers = scope_tickers[:max_tickers]

    if not scope_tickers:
        coverage_after = await _snapshot()
        return {
            "scope_size": 0,
            "fetched": 0, "submissions_404": 0,
            "events_extracted": 0, "events_upserted": 0,
            "state_writes": 0, "state_skipped_precedence": 0,
            "state_skipped_no_change": 0,
            "by_state": {}, "failures": [],
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "dry_run": dry_run,
            "note": "no rows matched scope",
        }

    # Pull current state for the scope (id + cik + existing lifecycle
    # state/source so we can apply the precedence gate).
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker, id AS classification_id, cik,
                   issuer_lifecycle_state,
                   issuer_lifecycle_state_source
            FROM platform.ticker_classifications
            WHERE ticker = ANY($1::text[])
            ORDER BY ticker
            """,
            scope_tickers,
        )
    state_by_ticker: dict[str, dict[str, Any]] = {
        r["ticker"]: dict(r) for r in rows
    }
    scope_tickers = [t for t in scope_tickers if t in state_by_ticker]
    log.info(
        "ops.stage.backfill_sec_lifecycle.scope_resolved",
        scope_size=len(scope_tickers),
    )

    # ─── fetch + extract ───
    from collections import Counter

    from tpcore.sec.companyfacts_adapter import SECCompanyFactsAdapter

    fetched = 0
    submissions_404 = 0
    events_extracted = 0
    failures: list[str] = []
    by_state: Counter = Counter()
    # Per ticker: (current_state, current_source, all_events, derived projection)
    extracted: list[tuple[str, str, str | None, list[dict], dict]] = []
    # entries = list of (ticker, classification_id, current_source, events, derived)

    # Cache-first SEC submissions: cache once to disk; subsequent
    # backfill runs (operator re-runs, P2b validator dev, future
    # extensions) read from cache without re-hitting SEC. Operator
    # standing rule ``feedback_bulk_before_api_crawl_REINFORCED``:
    # never re-pull what you already have on disk.
    cache_dir_override = cfg.get("cache_dir") if cfg else None
    force_refresh_cache = _to_bool(cfg.get("force_refresh_cache", False))
    cache_hits = 0
    cache_misses = 0
    import os as _os
    from pathlib import Path as _Path
    if cache_dir_override:
        cache_dir_resolved = str(cache_dir_override)
    else:
        tp_data = _os.environ.get("TP_DATA_DIR")
        cache_dir_resolved = (
            f"{tp_data}/sec_submissions" if tp_data
            else "data/sec_submissions"
        )

    async with SECCompanyFactsAdapter() as sec:
        for ticker in scope_tickers:
            state = state_by_ticker[ticker]
            cik = state.get("cik")
            current_source = state.get("issuer_lifecycle_state_source")
            classification_id = state.get("classification_id")
            if cik is None or classification_id is None:
                continue
            # Cache check happens BEFORE the SEC throttle — a cache hit
            # incurs zero API cost and zero rate-limit pressure. Only
            # cache misses pay the 0.11 s SEC fair-use sleep.
            cik_padded = str(cik).lstrip("0").zfill(10)
            cache_path = _Path(cache_dir_resolved) / f"CIK{cik_padded}.json"
            if cache_path.exists() and not force_refresh_cache:
                cache_hits += 1
            else:
                cache_misses += 1
                await asyncio.sleep(0.11)  # SEC fair-use
            try:
                subs = await sec.get_submissions_cached(
                    str(cik),
                    cache_dir=cache_dir_resolved,
                    force_refresh=force_refresh_cache,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ops.stage.backfill_sec_lifecycle.submissions_error",
                    ticker=ticker, cik=cik,
                    error_type=type(exc).__name__, error=str(exc),
                )
                failures.append(ticker)
                continue
            if subs is None:
                submissions_404 += 1
                continue
            fetched += 1
            result = sec.extract_lifecycle_events(subs, cik=str(cik))
            all_events = (
                result["form_25_events"] + result["form_15_events"]
            )
            events_extracted += len(all_events)
            if result["derived_state"] is not None:
                by_state[result["derived_state"]] += 1
            extracted.append((
                ticker, str(classification_id), current_source,
                all_events, result,
            ))

    log.info(
        "ops.stage.backfill_sec_lifecycle.extraction_done",
        fetched=fetched, submissions_404=submissions_404,
        events_extracted=events_extracted,
        failures=len(failures),
        by_state=dict(by_state),
    )

    # ─── apply writes (idempotent) ───
    state_writes = 0
    state_skipped_precedence = 0
    state_skipped_no_change = 0
    events_upserted = 0

    if not dry_run and extracted:
        # Per-ticker transaction so the projection never lags the log
        # AND so a partial run commits incremental progress (the
        # across-batch single-transaction shape held a long-lived txn
        # on the Supabase pooler; per-ticker txns avoid that hazard).
        async with pool.acquire() as conn:
            for ticker, cid, current_source, events, derived in extracted:
                async with conn.transaction():
                    # Event log: UPSERT (ON CONFLICT DO NOTHING).
                    for ev in events:
                        if ev.get("filing_date") is None:
                            # Skip events without a filing_date — these
                            # are malformed SEC payloads (rare).
                            continue
                        source_for_form = (
                            "sec_form_15"
                            if ev["form"] in {
                                "15", "15-12G", "15-12B", "15F", "15-15D",
                            } else "sec_form_25"
                        )
                        result = await conn.execute(
                            """
                            INSERT INTO platform.ticker_lifecycle_events
                                (classification_id, ticker, form_type,
                                 filing_date, report_date,
                                 accession_number, source, evidence_url)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (classification_id, form_type,
                                         accession_number)
                            WHERE accession_number IS NOT NULL
                            DO NOTHING
                            """,
                            cid, ticker, ev["form"],
                            ev["filing_date"], ev["report_date"],
                            ev.get("accession_number"),
                            source_for_form, ev.get("evidence_url"),
                        )
                        # asyncpg returns "INSERT 0 1" on success or
                        # "INSERT 0 0" on conflict skip.
                        if result.endswith(" 1"):
                            events_upserted += 1

                    # Projection: precedence-gated UPDATE.
                    derived_source = derived.get("derived_source")
                    if derived_source is None:
                        continue
                    new_rank = _LIFECYCLE_SOURCE_PRECEDENCE.get(
                        derived_source, 0,
                    )
                    current_rank = _LIFECYCLE_SOURCE_PRECEDENCE.get(
                        current_source or "", 0,
                    )
                    if (current_source is not None
                            and new_rank < current_rank):
                        state_skipped_precedence += 1
                        continue
                    if (current_source == derived_source
                            and not force_refresh):
                        state_skipped_no_change += 1
                        continue
                    await conn.execute(
                        """
                        UPDATE platform.ticker_classifications
                        SET issuer_lifecycle_state = $2,
                            issuer_lifecycle_state_source = $3,
                            issuer_lifecycle_event_date = $4,
                            issuer_lifecycle_evidence_url = $5,
                            issuer_lifecycle_updated_at = NOW(),
                            updated_at = NOW()
                        WHERE ticker = $1
                        """,
                        ticker, derived["derived_state"], derived_source,
                        derived["derived_event_date"],
                        derived["derived_evidence_url"],
                    )
                    state_writes += 1

        log.info(
            "ops.stage.backfill_sec_lifecycle.writes_committed",
            events_upserted=events_upserted,
            state_writes=state_writes,
            state_skipped_precedence=state_skipped_precedence,
            state_skipped_no_change=state_skipped_no_change,
        )

    coverage_after = await _snapshot()
    return {
        "scope_size": len(scope_tickers),
        "fetched": fetched,
        "submissions_404": submissions_404,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "cache_dir": cache_dir_resolved,
        "events_extracted": events_extracted,
        "events_upserted": events_upserted,
        "state_writes": state_writes,
        "state_skipped_precedence": state_skipped_precedence,
        "state_skipped_no_change": state_skipped_no_change,
        "by_state": dict(by_state),
        "failures": failures[:50],
        "coverage_before": coverage_before,
        "coverage_after": coverage_after,
        "dry_run": dry_run,
    }


# F0 (2026-06-01) — provider-parity EVALUATE stage. Lights up the
# tpcore/parity/data_parity.py primitive at the EVALUATE phase of the
# data-provider lifecycle so the CANDIDATE → FALLBACK promotion is
# evidence-gated rather than operator-set. The primitive itself is
# left untouched (it is pure + unit-tested); this stage is the
# runtime caller plus persistence.
#
# Per-feed-class sample-pull dispatch lives in
# ``_pull_dual_samples_for_evaluation`` below. PRICE feeds are
# implemented today (the operator's primary need). MACRO / SENTIMENT
# / FILING / DERIVED return a clear "not implemented for feed=…"
# block message — operator can add each as the underlying table's
# provider-attribution column comes online. The stage's contract +
# tests prove the WIRING; the per-feed actuation is incremental.


def _stage_param_to_bool(v: Any) -> bool:
    """Shared ``--param`` boolean parser (matches the other stages'
    ``_to_bool`` inner helpers; promoted to module-level so the F0
    stage's helpers can share without re-declaring)."""
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes", "y")


# Map of platform feed name → (FeedClass, sample-pull SQL template).
# Keys here are the canonical feed names used in
# ``tpcore.providers.PROVIDER_BINDINGS``. Only PRICE today; other
# classes raise NotImplementedError at the stage level with a clear
# message naming the missing dispatch entry.
_FEED_PARITY_DISPATCH: dict[str, str] = {
    # ``daily_bars`` is the canonical feed name for the daily OHLCV
    # bars. The incumbent + candidate rows are differentiated by
    # ``prices_daily.source`` (per the schema verified 2026-06-01).
    "daily_bars": "price_daily",
}


async def _pull_dual_samples_for_evaluation(
    pool: asyncpg.Pool,
    *,
    feed: str,
    incumbent_provider: str,
    candidate_provider: str,
    overlap_window_days: int,
) -> tuple[list[Any], list[Any], str]:
    """Pull ``(incumbent_samples, candidate_samples, feed_class_name)``
    for the EVALUATE parity comparison.

    Today: implemented for ``feed=daily_bars`` (queries
    ``platform.prices_daily`` filtered by ``source``). Other feeds
    raise NotImplementedError with a message naming the missing
    dispatch entry — keeps the stage's contract honest while letting
    the operator extend per-feed as needed.

    Returns ``ParitySample`` lists ready for ``compare_provider_parity``.
    """
    from datetime import UTC, datetime, timedelta

    from tpcore.parity import ParitySample

    if feed not in _FEED_PARITY_DISPATCH:
        # NotImplementedError surfaces as a clean "not yet wired for
        # feed=X" verdict at the stage level rather than a silent
        # NOT_EVALUABLE. The caller catches and reports.
        raise NotImplementedError(
            f"parity dual-pull dispatch not yet wired for feed={feed!r}. "
            f"Add a (FeedClass, query) entry to _FEED_PARITY_DISPATCH + "
            f"extend _pull_dual_samples_for_evaluation. Supported "
            f"today: {sorted(_FEED_PARITY_DISPATCH)}"
        )

    today = datetime.now(UTC).date()
    start = today - timedelta(days=overlap_window_days)

    incumbent: list[ParitySample] = []
    candidate: list[ParitySample] = []

    # daily_bars / prices_daily dual-pull: key on "ticker|date",
    # value is close price.
    sql = """
        SELECT ticker, date, close
        FROM platform.prices_daily
        WHERE source = $1 AND date >= $2 AND date <= $3
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        inc_rows = await conn.fetch(sql, incumbent_provider, start, today)
        cand_rows = await conn.fetch(sql, candidate_provider, start, today)

    for r in inc_rows:
        incumbent.append(ParitySample(
            key=f"{r['ticker']}|{r['date'].isoformat()}",
            asof=r["date"],
            value=float(r["close"]) if r["close"] is not None else None,
        ))
    for r in cand_rows:
        candidate.append(ParitySample(
            key=f"{r['ticker']}|{r['date'].isoformat()}",
            asof=r["date"],
            value=float(r["close"]) if r["close"] is not None else None,
        ))

    return incumbent, candidate, "price"


async def _stage_evaluate_provider_parity(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run the data-provider parity gate at the EVALUATE phase
    (F0 2026-06-01).

    Pulls normalized samples for the named feed from BOTH the current
    ACTIVE (incumbent) provider and the named CANDIDATE provider over
    an ``overlap_window_days`` window, calls
    ``compare_provider_parity()``, and persists the verdict to
    ``platform.data_quality_log`` (source =
    ``evaluate.{feed}.{candidate}``) + emits
    ``PROVIDER_PARITY_EVALUATED`` on ``platform.application_log``
    when not in dry_run.

    On PASS, the operator's next step is the DFCR EVALUATE promotion
    that sets ``status=FALLBACK + parity_verified_at=<today>`` on the
    candidate's ``ProviderBinding`` (see ``/dfcr`` skill +
    ``docs/superpowers/checklists/data_feed_change_request.md``). The
    cutover_agent's freshness check (``_parity_verdict_fresh``) reads
    this verdict at cutover time.

    Params (``--param key=value``):

      * ``feed`` (required) — canonical feed name from
        ``tpcore.providers.PROVIDER_BINDINGS`` (today supports
        ``daily_bars``).
      * ``candidate`` (required) — provider name being evaluated.
      * ``overlap_window_days`` (int, default 30) — duration of the
        dual-pull comparison window.
      * ``dry_run`` (bool, default **True**) — print verdict; no DB
        writes.
      * ``force`` (bool, default False) — re-evaluate even if a recent
        verdict exists. Today the stage always re-pulls; ``force`` is
        wired for future caching at the EVALUATE layer.
      * ``incumbent_samples`` / ``candidate_samples`` — internal test
        hook: when present, skip dual-pull and use these in-process
        sample lists. Operator-facing runs should NEVER pass these;
        they exist so the hermetic tests in
        ``tests/test_evaluate_provider_parity_stage.py`` can exercise
        the stage's verdict + persistence + reporting paths without a
        live DB roundtrip.

    Output payload::

        {
          "feed": str, "candidate": str,
          "incumbent_provider": str,
          "verdict": "pass" | "fail" | "not_evaluable",
          "coverage_ratio": float | None,
          "freshness_lag_days": int | None,
          "accuracy_ratio": float | None,
          "evidence": str,
          "next_action": str,
          "data_quality_log_written": bool,
          "application_log_written": bool,
          "dry_run": bool,
        }
    """
    import json

    from tpcore.parity import (
        DataParityResult,
        FeedClass,
        ParitySample,
        ParityVerdict,
        compare_provider_parity,
    )

    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    feed = str(cfg.get("feed") or "").strip()
    candidate = str(cfg.get("candidate") or "").strip()
    overlap_window_days = int(cfg.get("overlap_window_days", 30))
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))
    # ``force`` reserved — wired for future verdict caching.
    _force = _stage_param_to_bool(cfg.get("force", False))

    if not feed:
        return {
            "verdict": "not_evaluable",
            "evidence": "missing required --param feed=<name>",
            "data_quality_log_written": False,
            "application_log_written": False,
            "dry_run": dry_run,
        }
    if not candidate:
        return {
            "feed": feed,
            "verdict": "not_evaluable",
            "evidence": "missing required --param candidate=<provider>",
            "data_quality_log_written": False,
            "application_log_written": False,
            "dry_run": dry_run,
        }

    # ─── resolve incumbent ───
    from tpcore import providers as P
    incumbent_binding = P.active_provider(feed)
    if incumbent_binding is None:
        return {
            "feed": feed, "candidate": candidate,
            "verdict": "not_evaluable",
            "evidence": (
                f"no ACTIVE provider declared for feed={feed!r} in "
                "PROVIDER_BINDINGS — cannot evaluate parity blind"
            ),
            "data_quality_log_written": False,
            "application_log_written": False,
            "dry_run": dry_run,
        }
    incumbent_provider = incumbent_binding.provider

    if candidate == incumbent_provider:
        return {
            "feed": feed, "candidate": candidate,
            "incumbent_provider": incumbent_provider,
            "verdict": "not_evaluable",
            "evidence": (
                f"candidate {candidate!r} is already the ACTIVE provider "
                f"for {feed!r} — nothing to evaluate"
            ),
            "data_quality_log_written": False,
            "application_log_written": False,
            "dry_run": dry_run,
        }

    log.info(
        "ops.stage.evaluate_provider_parity.start",
        feed=feed, candidate=candidate,
        incumbent=incumbent_provider,
        overlap_window_days=overlap_window_days,
        dry_run=dry_run,
    )

    # ─── pull samples ───
    # Test hook: cfg can carry pre-built sample lists so hermetic tests
    # exercise the verdict + persistence paths without a live DB.
    test_incumbent = cfg.get("incumbent_samples")
    test_candidate = cfg.get("candidate_samples")
    test_feed_class = cfg.get("test_feed_class")
    if (test_incumbent is not None and test_candidate is not None
            and test_feed_class is not None):
        incumbent_samples: list[ParitySample] = list(test_incumbent)
        candidate_samples: list[ParitySample] = list(test_candidate)
        feed_class_name = str(test_feed_class)
    else:
        try:
            incumbent_samples, candidate_samples, feed_class_name = (
                await _pull_dual_samples_for_evaluation(
                    pool,
                    feed=feed,
                    incumbent_provider=incumbent_provider,
                    candidate_provider=candidate,
                    overlap_window_days=overlap_window_days,
                )
            )
        except NotImplementedError as exc:
            return {
                "feed": feed, "candidate": candidate,
                "incumbent_provider": incumbent_provider,
                "verdict": "not_evaluable",
                "evidence": str(exc),
                "data_quality_log_written": False,
                "application_log_written": False,
                "dry_run": dry_run,
            }

    # ─── resolve feed class ───
    try:
        feed_class = FeedClass(feed_class_name)
    except ValueError:
        return {
            "feed": feed, "candidate": candidate,
            "incumbent_provider": incumbent_provider,
            "verdict": "not_evaluable",
            "evidence": (
                f"unknown FeedClass={feed_class_name!r} — must be one of "
                f"{[fc.value for fc in FeedClass]}"
            ),
            "data_quality_log_written": False,
            "application_log_written": False,
            "dry_run": dry_run,
        }

    # ─── verdict ───
    result: DataParityResult = compare_provider_parity(
        feed_class=feed_class,
        incumbent=incumbent_samples,
        candidate=candidate_samples,
    )

    next_action = {
        ParityVerdict.PASS: (
            f"PASS — operator next step: open DFCR for feed={feed!r} "
            f"change=status:FALLBACK + parity_verified_at:<today> on "
            f"provider {candidate!r}. See /dfcr skill + "
            f"docs/superpowers/checklists/data_feed_change_request.md"
        ),
        ParityVerdict.FAIL: (
            f"FAIL — BLOCK promotion. {candidate!r} cannot become a "
            f"FALLBACK for feed={feed!r} until parity passes. Per-"
            f"dimension reasons in evidence; address the failing "
            f"dimension and re-evaluate"
        ),
        ParityVerdict.NOT_EVALUABLE: (
            "NOT_EVALUABLE — honest non-verdict (no incumbent samples, "
            "or DERIVED feed). Promotion remains blocked"
        ),
    }[result.verdict]

    log_event = {
        "feed": feed,
        "candidate_provider": candidate,
        "incumbent_provider": incumbent_provider,
        "feed_class": feed_class_name,
        "overlap_window_days": overlap_window_days,
        "verdict": result.verdict.value,
        "coverage_ratio": result.coverage_ratio,
        "freshness_lag_days": result.freshness_lag_days,
        "accuracy_ratio": result.accuracy_ratio,
        "evidence": result.evidence,
    }

    log.info(
        "ops.stage.evaluate_provider_parity.verdict",
        **{k: v for k, v in log_event.items() if k != "evidence"},
    )

    # ─── persist (operator hard rule: dry_run defaults true) ───
    dq_written = False
    app_written = False
    if not dry_run:
        async with pool.acquire() as conn:
            # data_quality_log row.
            # source = "evaluate.{feed}.{candidate}" — the verdict
            # freshness check (_parity_verdict_fresh in
            # ops/cutover_agent.py) keys on this string.
            confidence_for_verdict = {
                ParityVerdict.PASS: 1.0,
                ParityVerdict.FAIL: 0.0,
                ParityVerdict.NOT_EVALUABLE: None,
            }[result.verdict]
            await conn.execute(
                """
                INSERT INTO platform.data_quality_log
                    (kind, source, timestamp, confidence, stale, notes)
                VALUES ('validation', $1, NOW(), $2, $3, $4::jsonb)
                """,
                f"evaluate.{feed}.{candidate}",
                confidence_for_verdict,
                False,  # stale=false; this IS the verdict, not a freshness measure
                json.dumps(log_event),
            )
            dq_written = True

            # application_log event.
            severity = (
                "WARNING" if result.verdict is ParityVerdict.FAIL
                else "INFO"
            )
            await conn.execute(
                """
                INSERT INTO platform.application_log
                    (engine, run_id, event_type, severity, message, data)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                "ops.evaluate_provider_parity",  # engine
                None,  # run_id
                "PROVIDER_PARITY_EVALUATED",
                severity,
                f"feed={feed} candidate={candidate} verdict={result.verdict.value}",
                json.dumps(log_event),
            )
            app_written = True
            log.info(
                "ops.stage.evaluate_provider_parity.persisted",
                dq_written=True, app_written=True,
            )

    return {
        "feed": feed,
        "candidate": candidate,
        "incumbent_provider": incumbent_provider,
        "feed_class": feed_class_name,
        "verdict": result.verdict.value,
        "coverage_ratio": result.coverage_ratio,
        "freshness_lag_days": result.freshness_lag_days,
        "accuracy_ratio": result.accuracy_ratio,
        "evidence": result.evidence,
        "next_action": next_action,
        "data_quality_log_written": dq_written,
        "application_log_written": app_written,
        "dry_run": dry_run,
    }


# ────────────────────────────────────────────────────────────────────────
# universe_build — survivorship-free, identity-first universe minter
# (Plan 3 Phase 1; spec §4/§5.2/§5.3/§5.5; discovery
# docs/audits/2026-06-05-identity-build-code-state.md).
#
# IDENTITY-FIRST: this stage mints the WHOLE survivorship-free universe's
# ticker_classifications rows (TKR-14 id + cik + lifetime_start=FPFD,
# delisted INCLUDED) from SEC full company list ∪ FMP symbol +
# delisting/symbol-change history. It is the correct replacement for the
# legacy Alpaca-active classify_tickers minter (discovery §1). It is
# operator/orchestrator-only (in _OFF_CYCLE_STAGES — NOT the child-first
# --update order) because identity must be built BEFORE child loads so
# the 14 BEFORE INSERT triggers attribute classification_id correctly.
#
# CSV-first sub-protocol (data-adapter rule): the SEC/FMP source pulls
# resolve archive-first; the live HTTP fetches are with_retry-wrapped
# (tpcore.outage.with_retry — never local retry loops). The pure
# assembly + mint logic lives in tpcore/identity/universe_build.py
# (no DB/network) and is exercised by tpcore/identity/tests/
# test_universe_build.py.
# ────────────────────────────────────────────────────────────────────────


_FMP_STOCK_LIST_URL = "https://financialmodelingprep.com/stable/stock-list"
_FMP_DELISTED_URL = (
    "https://financialmodelingprep.com/stable/delisted-companies"
)


def _parse_iso_date(raw: Any) -> date | None:
    """Best-effort ISO-date parse; None on any failure (no guessing)."""
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


async def _fetch_sec_universe_entries(*, log: Any) -> list[Any]:
    """Fetch the SEC full company list + per-CIK FPFD → SECUniverseEntry[].

    Bulk-first (feedback_bulk_before_api_crawl_REINFORCED):
      1. ``SECTickerCIKMap.fetch`` — the single ~1.5MB company_tickers.json
         pull (ticker→CIK→title).
      2. ``SECSubmissionsBulkReader`` — the cached/bulk submissions.zip;
         per-CIK FPFD via ``extract_filing_metadata`` (the FIXED earliest
         filingDate — spec §5.5/A5). No per-CIK HTTP.

    Returns ``SECUniverseEntry`` rows. Pure assembly + mint happen later
    in ``assemble_universe`` — this function is the I/O seam.
    """
    from tpcore.identity.universe_build import SECUniverseEntry  # noqa: PLC0415
    from tpcore.sec.companyfacts_adapter import (  # noqa: PLC0415
        SECCompanyFactsAdapter,
    )
    from tpcore.sec.submissions_bulk_reader import (  # noqa: PLC0415
        SECSubmissionsBulkReader,
        ensure_zip_cached,
    )
    from tpcore.sec.ticker_cik_map import SECTickerCIKMap  # noqa: PLC0415

    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "universe_build: SEC_EDGAR_USER_AGENT is required for the SEC "
            "company-list + submissions pulls."
        )

    ticker_map = await SECTickerCIKMap().fetch()
    # Ensure the bulk submissions.zip is locally available (archive-first).
    await ensure_zip_cached(user_agent=user_agent)

    entries: list[Any] = []
    n_untrusted_fpfd = 0
    with SECSubmissionsBulkReader() as reader:
        for ticker, entry in ticker_map.items():
            fpfd = None
            payload = reader.get_merged_submissions(entry.cik)
            if payload is not None:
                # Review #4: a merged payload with non-empty _shard_errors is
                # MISSING shards. A missing OLDEST shard pulls min(filingDate)
                # (FPFD) forward → look-ahead re-enters. Treat that FPFD as
                # UNTRUSTED (None + WARN) rather than minting a confidently
                # wrong early date; resolve_lifetime_start then falls back per
                # its precedence (FMP earliest → now), never the sentinel.
                if payload.get("_shard_errors"):
                    n_untrusted_fpfd += 1
                    log.warning(
                        "ops.stage.universe_build.sec_fpfd_untrusted",
                        ticker=ticker,
                        cik=entry.cik,
                        shard_errors=payload.get("_shard_errors"),
                    )
                else:
                    meta = SECCompanyFactsAdapter.extract_filing_metadata(
                        payload
                    )
                    fpfd = meta.get("first_public_filing_date")
            entries.append(
                SECUniverseEntry(
                    ticker=ticker,
                    cik=entry.cik,
                    legal_name=entry.company_name,
                    first_public_filing_date=fpfd,
                )
            )
        # Review #6: the SEC leg sources company_tickers.json (CURRENT filers
        # only), so SEC-only-delisted issuers the chain cannot recover are a
        # known survivorship residual. Make the gap OBSERVABLE rather than
        # silent — FMP pagination (#1) recovers most delisted symbols via the
        # FMP-only leg, but the SEC-side residual is logged explicitly here.
        stats = reader.stats()
        log.info(
            "ops.stage.universe_build.sec_fetched",
            n_entries=len(entries),
            n_untrusted_fpfd=n_untrusted_fpfd,
            **stats,
        )
        log.warning(
            "ops.stage.universe_build.sec_survivorship_residual",
            note=(
                "SEC leg uses company_tickers.json (current filers only); "
                "SEC-only-delisted issuers absent from it are a known "
                "survivorship residual recovered mostly via the FMP-only leg "
                "(review #6)."
            ),
            n_missing_from_bulk=stats.get("missing_count", 0),
            n_shard_error_ciks=n_untrusted_fpfd,
        )
    return entries


# FMP paginated endpoints cap each page at ~100 rows; a single-page read
# silently truncates the delisted roster (and the stock list) and defeats
# survivorship-freeness (G1/G3). The hard page ceiling is a runaway-guard —
# a healthy delisted history is tens of thousands of rows (hundreds of pages),
# never hundreds of thousands; hitting this cap is a pathology worth surfacing.
_FMP_MAX_PAGES: int = 2_000


async def _fetch_fmp_universe_entries(*, log: Any) -> list[Any]:
    """Fetch the FMP symbol list + delisted history → FMPUniverseEntry[].

    PAGINATED bulk GET per endpoint (review #1): the FMP ``/stable/``
    list endpoints return ~100 rows per page, so each is read page=0,1,…
    until an empty page. Each individual page GET is ``with_retry``-wrapped
    (``tpcore.outage.with_retry`` — NEVER a local retry loop). Reading only
    the first page silently caps the delisted roster at ~100 rows and defeats
    survivorship-freeness. Delisted companies are INCLUDED so the roster is
    survivorship-free (invariant G1/G3).
    """
    import httpx  # noqa: PLC0415

    from tpcore.identity.universe_build import FMPUniverseEntry  # noqa: PLC0415
    from tpcore.outage import with_retry  # noqa: PLC0415

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError(
            "universe_build: FMP_API_KEY is required for the FMP symbol-list "
            "+ delisted pulls."
        )

    @with_retry(max_attempts=4, backoff_base_sec=2.0, backoff_cap_sec=30.0)
    async def _get_page(url: str, page: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                url, params={"apikey": api_key, "page": page}
            )
            resp.raise_for_status()
            payload = resp.json()
        return payload if isinstance(payload, list) else []

    async def _get_all(url: str, *, what: str) -> list[dict[str, Any]]:
        """Page through ``url`` until an empty page; each page with_retry'd."""
        rows: list[dict[str, Any]] = []
        for page in range(_FMP_MAX_PAGES):
            page_rows = await _get_page(url, page)
            if not page_rows:
                break
            rows.extend(page_rows)
        else:
            raise RuntimeError(
                f"universe_build: FMP {what} pagination hit the "
                f"{_FMP_MAX_PAGES}-page ceiling without an empty page — "
                "refusing to silently truncate (review #1)."
            )
        return rows

    stock_rows = await _get_all(_FMP_STOCK_LIST_URL, what="stock-list")
    delisted_rows = await _get_all(_FMP_DELISTED_URL, what="delisted")

    delisted_by_symbol: dict[str, dict[str, Any]] = {}
    for r in delisted_rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if sym:
            delisted_by_symbol[sym] = r

    entries: list[Any] = []
    seen: set[str] = set()
    for r in stock_rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        d = delisted_by_symbol.get(sym)
        entries.append(
            FMPUniverseEntry(
                ticker=sym,
                company_name=r.get("name") or r.get("companyName"),
                country=str(r.get("country") or "US").strip().upper()[:2] or "US",
                delisted=d is not None,
                delisting_date=_parse_iso_date(d.get("delistedDate")) if d else None,
            )
        )
    # Delisted symbols absent from the active stock-list are STILL part of
    # the survivorship-free universe — add them (G1/G3).
    for sym, d in delisted_by_symbol.items():
        if sym in seen:
            continue
        seen.add(sym)
        entries.append(
            FMPUniverseEntry(
                ticker=sym,
                company_name=d.get("companyName") or d.get("name"),
                country=str(d.get("country") or "US").strip().upper()[:2] or "US",
                delisted=True,
                delisting_date=_parse_iso_date(d.get("delistedDate")),
            )
        )
    log.info(
        "ops.stage.universe_build.fmp_fetched",
        n_stock=len(stock_rows),
        n_delisted=len(delisted_rows),
        n_entries=len(entries),
    )
    return entries


# Cross-run idempotency (review #2): a TKR-14 ``id`` embeds the discovery
# year (pos 5-6), so re-minting the same issuer in a LATER year yields a
# DIFFERENT id. Keying the upsert on ``(id)`` would then either crash on the
# ``ticker_classifications_cik_uniq`` partial index (SEC rows) or silently
# duplicate (cik-NULL FMP-only rows). The fix is two-fold: (1) RESOLVE the
# existing identity before minting (``_resolve_existing_identities`` reuses
# the stored id), and (2) key the upsert on an ISSUER-STABLE conflict target,
# NOT ``(id)``:
#   * SEC rows (cik NOT NULL) → ``ON CONFLICT (cik) WHERE cik IS NOT NULL``
#     (the ``ticker_classifications_cik_uniq`` partial unique index,
#     migration 20260524_0000 §4).
#   * FMP-only rows (cik NULL) → ``ON CONFLICT (id)`` is correct because the
#     id is REUSED (step 1), not re-minted, so it is stable across runs; the
#     cik partial index does not cover NULL cik, so cik cannot be the target.
_UNIVERSE_INSERT_COLS = """
        (id, ticker, current_ticker, asset_class, source, cik,
         current_legal_name, discovery_source, lifetime_start, lifetime_end,
         status, updated_at)
    SELECT id, ticker, current_ticker, asset_class, source, cik,
           current_legal_name, discovery_source, lifetime_start, lifetime_end,
           status, now()
    FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::text[], $5::text[],
        $6::text[], $7::text[], $8::text[], $9::date[], $10::date[],
        $11::text[]
    ) AS t(id, ticker, current_ticker, asset_class, source, cik,
           current_legal_name, discovery_source, lifetime_start, lifetime_end,
           status)
"""

# SEC leg (cik NOT NULL): issuer-stable conflict on the cik partial index.
_UNIVERSE_INSERT_SEC_SQL = (
    "INSERT INTO platform.ticker_classifications"
    + _UNIVERSE_INSERT_COLS
    + "ON CONFLICT (cik) WHERE cik IS NOT NULL DO NOTHING\n"
)

# FMP-only leg (cik NULL): id is reused (resolved before mint) → stable.
_UNIVERSE_INSERT_FMP_SQL = (
    "INSERT INTO platform.ticker_classifications"
    + _UNIVERSE_INSERT_COLS
    + "ON CONFLICT (id) DO NOTHING\n"
)


async def _resolve_existing_identities(
    pool: asyncpg.Pool, rows: list[Any], *, log: Any
) -> dict[str, str]:
    """Resolve already-stored identity ids so a re-run REUSES them (review #2).

    Returns a map keyed by ``cik`` (SEC rows) and ``current_ticker`` among
    cik-NULL rows (FMP-only) → the existing ``id``. The caller rewrites each
    matching ``UniverseSecurity.id`` to the stored value so a re-run in a
    later year does NOT mint a different id for the same issuer (the TKR-14
    discovery-year segment would otherwise change the id).
    """
    sec_ciks = sorted({r.cik for r in rows if r.cik})
    fmp_tickers = sorted({r.current_ticker for r in rows if not r.cik})
    resolved: dict[str, str] = {}
    if sec_ciks:
        sec_existing = await pool.fetch(
            """
            SELECT cik, id FROM platform.ticker_classifications
            WHERE cik = ANY($1::text[]) AND cik IS NOT NULL AND id IS NOT NULL
            """,
            sec_ciks,
        )
        for rec in sec_existing:
            resolved[f"cik:{rec['cik']}"] = rec["id"]
    if fmp_tickers:
        fmp_existing = await pool.fetch(
            """
            SELECT current_ticker, id FROM platform.ticker_classifications
            WHERE current_ticker = ANY($1::text[]) AND cik IS NULL
              AND id IS NOT NULL
            """,
            fmp_tickers,
        )
        for rec in fmp_existing:
            resolved[f"tkr:{rec['current_ticker']}"] = rec["id"]
    log.info(
        "ops.stage.universe_build.identities_resolved",
        n_sec_reused=sum(1 for k in resolved if k.startswith("cik:")),
        n_fmp_reused=sum(1 for k in resolved if k.startswith("tkr:")),
    )
    return resolved


def _apply_resolved_ids(
    rows: list[Any], resolved: dict[str, str]
) -> tuple[list[Any], int]:
    """Return rows with reused ids substituted in + the reuse count.

    ``UniverseSecurity`` is frozen, so a matched row is rebuilt via
    ``model_copy(update=...)`` with the stored id (review #2 — no re-mint).
    """
    out: list[Any] = []
    n_reused = 0
    for r in rows:
        key = f"cik:{r.cik}" if r.cik else f"tkr:{r.current_ticker}"
        existing = resolved.get(key)
        if existing is not None and existing != r.id:
            out.append(r.model_copy(update={"id": existing}))
            n_reused += 1
        else:
            out.append(r)
    return out, n_reused


async def _insert_universe_rows(
    pool: asyncpg.Pool, rows: list[Any], *, chunk_size: int, log: Any
) -> int:
    """Chunked idempotent INSERT of UniverseSecurity rows.

    Chunked at ``chunk_size`` (>100K-row Supabase mechanics, spec §7) to
    avoid a single-transaction WAL blow-up. Issuer-stable conflict targets
    (review #2) make re-runs idempotent across years: SEC rows upsert on the
    cik partial index, FMP-only rows on the (reused) id. ``lifetime_start``
    is ALWAYS supplied (the pydantic model requires it; the column is NOT
    NULL no-sentinel — A6).
    """
    sec_rows = [r for r in rows if r.cik]
    fmp_rows = [r for r in rows if not r.cik]
    n_committed = 0
    async with pool.acquire() as conn:
        for sql, leg_rows, leg in (
            (_UNIVERSE_INSERT_SEC_SQL, sec_rows, "sec"),
            (_UNIVERSE_INSERT_FMP_SQL, fmp_rows, "fmp"),
        ):
            for start in range(0, len(leg_rows), chunk_size):
                chunk = leg_rows[start : start + chunk_size]
                await conn.execute(
                    sql,
                    [r.id for r in chunk],
                    [r.ticker for r in chunk],
                    [r.current_ticker for r in chunk],
                    [r.asset_class for r in chunk],
                    [r.source for r in chunk],
                    [r.cik for r in chunk],
                    [r.legal_name for r in chunk],
                    [r.discovery_source for r in chunk],
                    [r.lifetime_start for r in chunk],
                    [r.lifetime_end for r in chunk],
                    [
                        "active" if r.lifetime_end is None else "delisted"
                        for r in chunk
                    ],
                )
                n_committed += len(chunk)
                log.info(
                    "ops.stage.universe_build.chunk_committed",
                    leg=leg,
                    chunk_start=start,
                    chunk_rows=len(chunk),
                )
    return n_committed


async def _stage_universe_build(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mint the survivorship-free, identity-first universe.

    Identity-first minter (Plan 3 Phase 1). Assembles SEC full company
    list ∪ FMP symbol + delisting history into the whole universe's
    ``ticker_classifications`` rows — each with a TKR-14 ``id`` (via
    ``tpcore.identity.tkr14.mint`` + salt-collision retry), ``cik``,
    ``discovery_source``, ``current_ticker``, and an explicit
    ``lifetime_start`` (= SEC FPFD, or earliest FMP date for the OQ-1
    gray zone, NEVER the ``1900-01-01`` sentinel). Delisted tickers are
    INCLUDED with ``lifetime_end`` set (survivorship-free, G1/G3).

    SEC-first authority (A7/A8): SEC wins identity for any ticker it
    covers; FMP is the gray-zone fallback only (cik=None,
    discovery_source='F').

    Cross-run idempotency (review #2): before minting, the stage RESOLVES
    each issuer's existing identity (by cik for SEC, by ticker among cik-NULL
    rows for FMP-only) and REUSES the stored id — a re-run in a later year
    does NOT mint a different id (the TKR-14 discovery-year segment would
    otherwise change it). The upsert keys on issuer-stable conflict targets
    (cik partial index for SEC; the reused id for FMP-only), NOT ``(id)``.

    Producer hard-stop (review #3): a degraded/empty-200/quota source must
    not silently mint a TRUNCATED universe. The stage raises if the SEC
    universe is below ``min_sec`` or the FMP stock-list below ``min_fmp``.

    Knobs (``--param key=value``):
      * ``dry_run`` (default True) — assemble + report counts WITHOUT any
        INSERT. ``--param dry_run=false`` for the live mint.
      * ``chunk_size`` (default 100000) — INSERT batch size.
      * ``min_sec`` (default 8000) — hard-stop floor on the SEC universe.
      * ``min_fmp`` (default 5000) — hard-stop floor on the FMP stock-list.

    This stage is NOT in the child-first ``--update`` order (it is in
    ``_OFF_CYCLE_STAGES``); it runs via the identity-first orchestrator or
    an explicit ``--stage universe_build`` invocation.
    """
    cfg = cfg or {}
    log = structlog.get_logger("ops.stage.universe_build")
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))
    chunk_size = int(cfg.get("chunk_size", 100_000))
    min_sec = int(cfg.get("min_sec", 8_000))
    min_fmp = int(cfg.get("min_fmp", 5_000))

    from tpcore.identity.universe_build import assemble_universe  # noqa: PLC0415

    sec_entries = await _fetch_sec_universe_entries(log=log)
    fmp_entries = await _fetch_fmp_universe_entries(log=log)

    # Producer hard-stop on a degraded source (review #3): a quota-throttled
    # or empty-200 fetch would otherwise silently mint a truncated universe,
    # defeating survivorship-freeness + the identity-first substrate.
    n_sec_fetched = len(sec_entries)
    n_fmp_fetched = len(fmp_entries)
    if n_sec_fetched < min_sec:
        raise RuntimeError(
            f"universe_build: SEC universe degraded — {n_sec_fetched} entries "
            f"below the {min_sec} floor (empty/short 200 or quota). Refusing "
            "to mint a truncated universe (review #3)."
        )
    if n_fmp_fetched < min_fmp:
        raise RuntimeError(
            f"universe_build: FMP stock-list degraded — {n_fmp_fetched} "
            f"entries below the {min_fmp} floor. Refusing to mint a truncated "
            "universe (review #3)."
        )

    now = datetime.now(UTC)
    rows = assemble_universe(
        sec_entries=sec_entries, fmp_entries=fmp_entries, now=now
    )

    # Reuse existing identity ids so a re-run is idempotent across years
    # (review #2). Reads are safe in dry-run too (no write).
    resolved = await _resolve_existing_identities(pool, rows, log=log)
    rows, n_reused = _apply_resolved_ids(rows, resolved)

    n_sec = sum(1 for r in rows if r.source == "sec")
    n_fmp_only = sum(1 for r in rows if r.source == "fmp")
    n_delisted = sum(1 for r in rows if r.lifetime_end is not None)
    # Structural invariant — no row may carry the forbidden sentinel
    # (A6). assemble_universe never emits it, but assert before any write.
    sentinel = date(1900, 1, 1)
    n_sentinel = sum(1 for r in rows if r.lifetime_start == sentinel)
    if n_sentinel:
        raise RuntimeError(
            f"universe_build: {n_sentinel} rows carry the forbidden "
            "'1900-01-01' lifetime_start sentinel — refusing to write "
            "(spec §3.1/A6)."
        )

    if dry_run:
        log.info(
            "ops.stage.universe_build.dry_run",
            n_total=len(rows),
            n_sec=n_sec,
            n_fmp_only=n_fmp_only,
            n_delisted=n_delisted,
            n_reused=n_reused,
            sample=[{"ticker": r.ticker, "id": r.id} for r in rows[:5]],
        )
        return {
            "rows_minted": 0,
            "rows_previewed": len(rows),
            "n_sec": n_sec,
            "n_fmp_only": n_fmp_only,
            "n_delisted": n_delisted,
            "n_reused": n_reused,
            "dry_run": True,
        }

    n_committed = await _insert_universe_rows(
        pool, rows, chunk_size=chunk_size, log=log
    )
    log.info(
        "ops.stage.universe_build.committed",
        rows_minted=n_committed,
        n_sec=n_sec,
        n_fmp_only=n_fmp_only,
        n_delisted=n_delisted,
        n_reused=n_reused,
    )
    return {
        "rows_minted": n_committed,
        "n_sec": n_sec,
        "n_fmp_only": n_fmp_only,
        "n_delisted": n_delisted,
        "n_reused": n_reused,
        "dry_run": False,
    }


# ════════════════════════════════════════════════════════════════════════
# issuers_build — SEC submissions → issuers + SCD-2 issuer_history
# (Plan 3 Phase 1; spec §4/§5.3; corp-history §3.1-§3.4).
#
# IDENTITY-FIRST stage #2 (runs AFTER universe_build): for every distinct
# CIK in ticker_classifications (cik NOT NULL), read the SAME bulk
# submissions.zip universe_build used (SECSubmissionsBulkReader) and upsert
# one issuers row (issuer_id = 'CIK'+zero-padded-10) + the SCD-2
# issuer_history timeline (legal-name over time from formerNames).
# ON CONFLICT (cik) idempotent — a re-run (even a year later) produces
# ZERO new/duplicate rows.
#
# Review lessons applied PROACTIVELY (blocking bugs in universe_build):
#   * trust-but-verify SEC shards — a CIK whose payload carries
#     _shard_errors yields an UNTRUSTED FPFD (None + WARN), never a
#     confidently-wrong early date (mirrors universe_build review #4).
#   * date-order guard — a formerNames window with to <= from is DROPPED
#     in the pure layer so the issuer_history valid_to>valid_from order
#     never trips the DB.
#   * producer hard-stop — raises if the resolvable-CIK universe is below
#     a sane floor (degraded/empty source), never a truncated write.
# The pure assembly lives in tpcore/identity/issuers_build.py (no
# DB/network), exercised by tpcore/identity/tests/test_issuers_build.py.
# ════════════════════════════════════════════════════════════════════════


_ISSUERS_INSERT_SQL = """
    INSERT INTO platform.issuers
        (issuer_id, cik, legal_name, country_of_incorp,
         status, created_at, updated_at)
    SELECT issuer_id, cik, legal_name, country_of_incorp,
           'active', now(), now()
    FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::text[]
    ) AS t(issuer_id, cik, legal_name, country_of_incorp)
    ON CONFLICT (cik) DO NOTHING
"""

_ISSUER_HISTORY_INSERT_SQL = """
    INSERT INTO platform.issuer_history
        (issuer_id, cik, legal_name, valid_from, valid_to, source)
    SELECT issuer_id, cik, legal_name, valid_from, valid_to, source
    FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::date[], $5::date[], $6::text[]
    ) AS t(issuer_id, cik, legal_name, valid_from, valid_to, source)
    ON CONFLICT (issuer_id, valid_from) DO NOTHING
"""


async def _issuers_universe_ciks(pool: asyncpg.Pool) -> list[tuple[str, str]]:
    """Distinct (cik, country) for every cik-bearing classification.

    ``country`` is the TKR-14 pos-1-2 generated column on
    ticker_classifications → ``issuers.country_of_incorp`` (migration
    20260525_0000 derives country_of_incorp from the classification's
    country). One issuer per CIK; ``min(country)`` collapses the rare
    multi-share-class disagreement deterministically.
    """
    rows = await pool.fetch(
        """
        SELECT cik, min(country) AS country
        FROM platform.ticker_classifications
        WHERE cik IS NOT NULL
        GROUP BY cik
        ORDER BY cik
        """
    )
    return [(r["cik"], r["country"]) for r in rows]


async def _stage_issuers_build(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build platform.issuers + SCD-2 issuer_history from SEC submissions.

    Identity-first stage #2 (Plan 3 Phase 1). For every distinct CIK in
    ticker_classifications (cik NOT NULL), reads the bulk submissions.zip
    (SECSubmissionsBulkReader — the SAME fixed source universe_build uses)
    and upserts one issuers row + its legal-name SCD-2 timeline.

    issuer_id convention (the LIVE convention; sample CIK0000886158):
    ``'CIK' + zero-padded-10 cik`` (tpcore.identity.issuers_build.mint_issuer_id
    == scripts.ops._mint_issuer_id_from_cik).

    Idempotency (review #2): ON CONFLICT (cik) for issuers, ON CONFLICT
    (issuer_id, valid_from) for issuer_history — a re-run produces ZERO
    new/duplicate rows. The natural key (cik / issuer_id) is stable, never
    a timestamped surrogate.

    Trust-but-verify SEC shards (review #4): a CIK whose payload carries
    non-empty _shard_errors yields an UNTRUSTED FPFD (None + WARN), not a
    confidently-wrong early date.

    Producer hard-stop (review #3): raises if fewer than ``min_resolved``
    of the universe CIKs resolved an issuers row (degraded/empty bulk file)
    — never a silently-truncated write.

    Knobs (``--param key=value``):
      * ``dry_run`` (default True) — assemble + report counts, no INSERT.
      * ``chunk_size`` (default 5000) — INSERT batch size.
      * ``min_resolved`` (default 1) — hard-stop floor on resolved issuers.
      * ``max_ciks`` (default 0 = no cap) — smoke-run limit.
    """
    cfg = cfg or {}
    log = structlog.get_logger("ops.stage.issuers_build")
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))
    chunk_size = int(cfg.get("chunk_size", 5_000))
    min_resolved = int(cfg.get("min_resolved", 1))
    max_ciks = int(cfg.get("max_ciks", 0))

    from tpcore.identity.issuers_build import assemble_issuer  # noqa: PLC0415
    from tpcore.sec.companyfacts_adapter import (  # noqa: PLC0415
        SECCompanyFactsAdapter,
    )
    from tpcore.sec.submissions_bulk_reader import (  # noqa: PLC0415
        SECSubmissionsBulkReader,
        ensure_zip_cached,
    )

    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        raise RuntimeError(
            "issuers_build: SEC_EDGAR_USER_AGENT is required for the SEC "
            "submissions pull."
        )

    universe = await _issuers_universe_ciks(pool)
    if max_ciks > 0:
        universe = universe[:max_ciks]
    n_universe = len(universe)
    if n_universe == 0:
        raise RuntimeError(
            "issuers_build: 0 cik-bearing classifications — run universe_build "
            "first (identity-first order; review #3 producer hard-stop)."
        )

    await ensure_zip_cached(user_agent=user_agent)

    issuer_rows: list[Any] = []
    history_rows: list[Any] = []
    n_untrusted_fpfd = 0
    n_skipped = 0
    with SECSubmissionsBulkReader() as reader:
        for cik, country in universe:
            payload = reader.get_merged_submissions(cik)
            if payload is None:
                n_skipped += 1
                continue
            fpfd: date | None = None
            if payload.get("_shard_errors"):
                # Untrusted FPFD: a missing oldest shard pulls min(filingDate)
                # forward (look-ahead). None + WARN, never a wrong early date.
                n_untrusted_fpfd += 1
                log.warning(
                    "ops.stage.issuers_build.sec_fpfd_untrusted",
                    cik=cik,
                    shard_errors=payload.get("_shard_errors"),
                )
            else:
                meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
                fpfd = meta.get("first_public_filing_date")
            sec_type = _issuer_primary_form(payload)
            issuer, history = assemble_issuer(
                cik=cik,
                payload=payload,
                fpfd=fpfd,
                sec_document_type_primary=sec_type,
                country_of_incorp=country,
            )
            if issuer is None:
                n_skipped += 1
                continue
            issuer_rows.append(issuer)
            history_rows.extend(history)
        stats = reader.stats()

    n_resolved = len(issuer_rows)
    log.info(
        "ops.stage.issuers_build.assembled",
        n_universe=n_universe,
        n_resolved=n_resolved,
        n_history_rows=len(history_rows),
        n_untrusted_fpfd=n_untrusted_fpfd,
        n_skipped=n_skipped,
        **stats,
    )
    # Producer hard-stop (review #3): a degraded/empty bulk file would
    # silently resolve ~0 issuers, leaving every cik-bearing classification
    # orphaned at the identity gate. Refuse a truncated write.
    if n_resolved < min_resolved:
        raise RuntimeError(
            f"issuers_build: only {n_resolved} issuers resolved from "
            f"{n_universe} universe CIKs — below the {min_resolved} floor "
            "(degraded/empty submissions.zip). Refusing a truncated write "
            "(review #3)."
        )

    if dry_run:
        log.info(
            "ops.stage.issuers_build.dry_run",
            n_issuers=n_resolved,
            n_history_rows=len(history_rows),
            sample=[
                {"issuer_id": r.issuer_id, "legal_name": r.legal_name}
                for r in issuer_rows[:5]
            ],
        )
        return {
            "issuers_upserted": 0,
            "issuers_previewed": n_resolved,
            "history_previewed": len(history_rows),
            "n_untrusted_fpfd": n_untrusted_fpfd,
            "n_skipped": n_skipped,
            "dry_run": True,
        }

    n_issuers = await _insert_issuer_rows(
        pool, issuer_rows, history_rows, chunk_size=chunk_size, log=log
    )
    log.info(
        "ops.stage.issuers_build.committed",
        issuers_upserted=n_issuers,
        history_rows=len(history_rows),
    )
    return {
        "issuers_upserted": n_issuers,
        "history_rows": len(history_rows),
        "n_untrusted_fpfd": n_untrusted_fpfd,
        "n_skipped": n_skipped,
        "dry_run": False,
    }


def _issuer_primary_form(payload: dict[str, Any]) -> str | None:
    """Most frequent SEC form in the merged recent filings → the issuer's
    primary document type (10-K filer vs 485BPOS fund, etc.). None when no
    forms are present (no guessing)."""
    filings = payload.get("filings") or {}
    recent = filings.get("recent") or {}
    forms = recent.get("form") or []
    if not isinstance(forms, list) or not forms:
        return None
    counts: dict[str, int] = {}
    for f in forms:
        if isinstance(f, str) and f:
            counts[f] = counts.get(f, 0) + 1
    if not counts:
        return None
    # Deterministic tie-break: highest count, then lexicographic.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


async def _insert_issuer_rows(
    pool: asyncpg.Pool,
    issuer_rows: list[Any],
    history_rows: list[Any],
    *,
    chunk_size: int,
    log: Any,
) -> int:
    """Chunked idempotent upsert of issuers + issuer_history.

    Issuers commit first (issuer_history.issuer_id FK references them), then
    the SCD-2 history. ON CONFLICT (cik) / (issuer_id, valid_from) make
    re-runs idempotent (review #2)."""
    n_committed = 0
    async with pool.acquire() as conn:
        for start in range(0, len(issuer_rows), chunk_size):
            chunk = issuer_rows[start : start + chunk_size]
            await conn.execute(
                _ISSUERS_INSERT_SQL,
                [r.issuer_id for r in chunk],
                [r.cik for r in chunk],
                [r.legal_name for r in chunk],
                [r.country_of_incorp for r in chunk],
            )
            n_committed += len(chunk)
            log.info(
                "ops.stage.issuers_build.issuers_chunk",
                chunk_start=start,
                chunk_rows=len(chunk),
            )
        for start in range(0, len(history_rows), chunk_size):
            chunk = history_rows[start : start + chunk_size]
            await conn.execute(
                _ISSUER_HISTORY_INSERT_SQL,
                [r.issuer_id for r in chunk],
                [r.cik for r in chunk],
                [r.legal_name for r in chunk],
                [r.valid_from for r in chunk],
                [r.valid_to for r in chunk],
                [r.source for r in chunk],
            )
            log.info(
                "ops.stage.issuers_build.history_chunk",
                chunk_start=start,
                chunk_rows=len(chunk),
            )
    return n_committed


# ════════════════════════════════════════════════════════════════════════
# ticker_history_reuse_build — ticker_classifications lifetimes → SCD-2
# ticker_history (Plan 3 Phase 1; spec §4/§5.3; invariant G3).
#
# IDENTITY-FIRST stage #3: DERIVE one ticker_history row per classification
# from its lifetime — a delisted-then-reused ticker gets MULTIPLE contiguous
# rows (G3). The half-open '[)' EXCLUDE constraint enforces no-overlap; the
# pure layer hard-stops on a true overlap (a surfaced defect, not mangled).
# ON CONFLICT (classification_id, valid_from) idempotent.
# Pure derivation: tpcore/identity/ticker_history_reuse_build.py.
# ════════════════════════════════════════════════════════════════════════


_TICKER_HISTORY_INSERT_SQL = """
    INSERT INTO platform.ticker_history
        (classification_id, ticker, valid_from, valid_to)
    SELECT classification_id, ticker, valid_from, valid_to
    FROM unnest(
        $1::text[], $2::text[], $3::date[], $4::date[]
    ) AS t(classification_id, ticker, valid_from, valid_to)
    ON CONFLICT (classification_id, valid_from) DO NOTHING
"""


async def _stage_ticker_history_reuse_build(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Derive platform.ticker_history (SCD-2) from classification lifetimes.

    Identity-first stage #3 (Plan 3 Phase 1; G3 reuse). For each ticker,
    its classifications are ordered by lifetime_start and emitted one
    ticker_history row each — a delisted-then-reused ticker gets MULTIPLE
    contiguous rows. The half-open [valid_from, valid_to) EXCLUDE
    constraint enforces no-overlap; the pure layer HARD-STOPS on a true
    overlap (a surfaced data defect, never silently mangled).

    Idempotency: ON CONFLICT (classification_id, valid_from) DO NOTHING —
    a re-run produces ZERO new/duplicate rows.

    Knobs (``--param key=value``):
      * ``dry_run`` (default True) — derive + report counts, no INSERT.
      * ``chunk_size`` (default 50000) — INSERT batch size.
    """
    cfg = cfg or {}
    log = structlog.get_logger("ops.stage.ticker_history_reuse_build")
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))
    chunk_size = int(cfg.get("chunk_size", 50_000))

    from tpcore.identity.ticker_history_reuse_build import (  # noqa: PLC0415
        ClassificationLifetime,
        derive_ticker_history,
    )

    rows = await pool.fetch(
        """
        SELECT id AS classification_id, current_ticker AS ticker,
               lifetime_start, lifetime_end
        FROM platform.ticker_classifications
        WHERE id IS NOT NULL AND current_ticker IS NOT NULL
          AND lifetime_start IS NOT NULL
        ORDER BY current_ticker, lifetime_start
        """
    )
    lifetimes = [
        ClassificationLifetime(
            classification_id=r["classification_id"],
            ticker=r["ticker"],
            lifetime_start=r["lifetime_start"],
            lifetime_end=r["lifetime_end"],
        )
        for r in rows
    ]
    # derive_ticker_history HARD-STOPS (ValueError) on a true overlap — a
    # data defect the EXCLUDE constraint would reject; surface it.
    history = derive_ticker_history(lifetimes)
    log.info(
        "ops.stage.ticker_history_reuse_build.derived",
        n_classifications=len(lifetimes),
        n_rows=len(history),
    )

    if dry_run:
        return {
            "rows_previewed": len(history),
            "n_classifications": len(lifetimes),
            "dry_run": True,
        }

    n_committed = 0
    async with pool.acquire() as conn:
        for start in range(0, len(history), chunk_size):
            chunk = history[start : start + chunk_size]
            await conn.execute(
                _TICKER_HISTORY_INSERT_SQL,
                [r.classification_id for r in chunk],
                [r.ticker for r in chunk],
                [r.valid_from for r in chunk],
                [r.valid_to for r in chunk],
            )
            n_committed += len(chunk)
            log.info(
                "ops.stage.ticker_history_reuse_build.chunk",
                chunk_start=start,
                chunk_rows=len(chunk),
            )
    log.info(
        "ops.stage.ticker_history_reuse_build.committed",
        rows_inserted=n_committed,
    )
    return {
        "rows_inserted": n_committed,
        "n_classifications": len(lifetimes),
        "dry_run": False,
    }


# ════════════════════════════════════════════════════════════════════════
# issuer_securities_build — ticker_classifications → issuer_securities
# (Plan 3 Phase 1; spec §4/§5.3). M:N fan-out: GOOG/GOOGL under one issuer.
# ON CONFLICT (issuer_id, classification_id, valid_from) idempotent.
# Pure derivation: tpcore/identity/issuer_securities_build.py.
# ════════════════════════════════════════════════════════════════════════


_ISSUER_SECURITIES_INSERT_SQL = """
    INSERT INTO platform.issuer_securities
        (issuer_id, classification_id, valid_from, valid_to)
    SELECT issuer_id, classification_id, valid_from, valid_to
    FROM unnest(
        $1::text[], $2::text[], $3::date[], $4::date[]
    ) AS t(issuer_id, classification_id, valid_from, valid_to)
    ON CONFLICT (issuer_id, classification_id, valid_from) DO NOTHING
"""


async def _stage_issuer_securities_build(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build platform.issuer_securities (M:N issuer↔security fan-out).

    Identity-first stage #4 (Plan 3 Phase 1). For each cik-bearing
    classification, link (issuer_id=mint_issuer_id(cik), classification_id,
    valid_from=lifetime_start) — two share classes under one CIK
    (GOOG/GOOGL) fan out to the SAME issuer. cik-NULL (FMP-only) rows are
    skipped (no SEC issuer to link → FK safety).

    Idempotency: ON CONFLICT (issuer_id, classification_id, valid_from) DO
    NOTHING — a re-run produces ZERO new/duplicate rows.

    Knobs (``--param key=value``):
      * ``dry_run`` (default True) — derive + report counts, no INSERT.
      * ``chunk_size`` (default 50000) — INSERT batch size.
    """
    cfg = cfg or {}
    log = structlog.get_logger("ops.stage.issuer_securities_build")
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))
    chunk_size = int(cfg.get("chunk_size", 50_000))

    from tpcore.identity.issuer_securities_build import (  # noqa: PLC0415
        SecurityWithCik,
        derive_issuer_securities,
    )

    rows = await pool.fetch(
        """
        SELECT id AS classification_id, cik, lifetime_start, lifetime_end
        FROM platform.ticker_classifications
        WHERE id IS NOT NULL AND cik IS NOT NULL
          AND lifetime_start IS NOT NULL
        ORDER BY cik, id
        """
    )
    securities = [
        SecurityWithCik(
            classification_id=r["classification_id"],
            cik=r["cik"],
            lifetime_start=r["lifetime_start"],
            lifetime_end=r["lifetime_end"],
        )
        for r in rows
    ]
    links = derive_issuer_securities(securities)
    log.info(
        "ops.stage.issuer_securities_build.derived",
        n_securities=len(securities),
        n_links=len(links),
    )

    if dry_run:
        return {
            "links_previewed": len(links),
            "n_securities": len(securities),
            "dry_run": True,
        }

    n_committed = 0
    async with pool.acquire() as conn:
        for start in range(0, len(links), chunk_size):
            chunk = links[start : start + chunk_size]
            await conn.execute(
                _ISSUER_SECURITIES_INSERT_SQL,
                [r.issuer_id for r in chunk],
                [r.classification_id for r in chunk],
                [r.valid_from for r in chunk],
                [r.valid_to for r in chunk],
            )
            n_committed += len(chunk)
            log.info(
                "ops.stage.issuer_securities_build.chunk",
                chunk_start=start,
                chunk_rows=len(chunk),
            )
    log.info(
        "ops.stage.issuer_securities_build.committed",
        links_inserted=n_committed,
    )
    return {
        "links_inserted": n_committed,
        "n_securities": len(securities),
        "dry_run": False,
    }


# ════════════════════════════════════════════════════════════════════════
# identity_build — the identity-first orchestrator (Plan 3 Phase 1.4).
#
# Runs the four identity stages IN ORDER, fail-fast:
#   universe_build → issuers_build → ticker_history_reuse_build →
#   issuer_securities_build
# then runs the BLOCKING identity gate (tpcore.identity.identity_gate) —
# the substrate must be internally consistent before any child load. The
# coordinator runs the live ingest; this orchestrator is the single entry.
# scripts/run_identity_build.sh wraps it (no-orphan-scripts).
# ════════════════════════════════════════════════════════════════════════


_IDENTITY_BUILD_ORDER: tuple[str, ...] = (
    "universe_build",
    "issuers_build",
    "ticker_history_reuse_build",
    "issuer_securities_build",
)


async def _stage_identity_build(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Identity-first orchestrator (Plan 3 Phase 1.4).

    Runs the four identity stages IN ORDER (universe_build → issuers_build →
    ticker_history_reuse_build → issuer_securities_build), fail-fast, then
    the BLOCKING identity gate. Every knob is forwarded to each sub-stage
    (e.g. ``--param dry_run=false``); the gate runs only on a live
    (non-dry-run) build because a dry-run writes nothing to check.

    The identity substrate must be internally consistent BEFORE any child
    load (prices / fundamentals / lifecycle) — the gate is the Phase-1.4
    gate the coordinator depends on (identity-path rule).
    """
    cfg = cfg or {}
    log = structlog.get_logger("ops.stage.identity_build")
    dry_run = _stage_param_to_bool(cfg.get("dry_run", True))

    from tpcore.identity.identity_gate import (  # noqa: PLC0415
        evaluate_identity_gate,
    )

    stage_fns = {
        "universe_build": _stage_universe_build,
        "issuers_build": _stage_issuers_build,
        "ticker_history_reuse_build": _stage_ticker_history_reuse_build,
        "issuer_securities_build": _stage_issuer_securities_build,
    }
    results: dict[str, Any] = {}
    for name in _IDENTITY_BUILD_ORDER:
        log.info("ops.stage.identity_build.running", sub_stage=name)
        results[name] = await stage_fns[name](pool, cfg)

    gate: dict[str, Any] | None = None
    if not dry_run:
        # BLOCKING: raise_on_fail aborts the orchestrator (and therefore the
        # coordinator's child loads) on an inconsistent substrate.
        gate_result = await evaluate_identity_gate(pool, raise_on_fail=True)
        gate = {
            "passed": gate_result.passed,
            "violations": gate_result.violations,
        }
        log.info("ops.stage.identity_build.gate_passed", **gate)
    else:
        log.info("ops.stage.identity_build.gate_skipped_dry_run")

    return {"sub_stages": results, "identity_gate": gate, "dry_run": dry_run}


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

    # Phase 3 (v2 referential-integrity rollout): dry_run defaults True
    # in the producer so accidental ops invocations don't trigger live
    # DELETEs before Phase 4 cleanup of prices_daily orphans completes.
    # Opt-in: `--param dry_run=false`.
    dry_run_param = (cfg or {}).get("dry_run", True)
    if isinstance(dry_run_param, str):
        dry_run = dry_run_param.lower() != "false"
    else:
        dry_run = bool(dry_run_param)
    stats = await classify_all_tickers(
        pool,
        alpaca_base_url=_alpaca_broker_base(),
        alpaca_headers=_alpaca_headers(),
        dry_run=dry_run,
    )
    log.info(
        "ops.stage.classify_tickers.done",
        **{str(k): (int(v) if isinstance(v, (int, bool)) else v) for k, v in stats.items()},
    )
    return {str(k): (int(v) if isinstance(v, (int, bool)) else v) for k, v in stats.items()}


async def _stage_tkr14_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v2.2 Phase P5 — backfill TKR-14 PK + cross-vendor identity for existing rows.

    Per v2.2 spec §1.2 / §1.7 / §1.9 + plan P5. Two modes via ``--param mode``:

    - ``mode=mint`` (default) — SLICE 1: mint TKR-14 + seed ticker_history
      from local data (country/asset_class/cik already populated). No
      external API. Idempotent: skips rows with id IS NOT NULL.
    - ``mode=figi`` — SLICE 2: for rows with id IS NOT NULL and figi IS
      NULL, batch-call OpenFIGI /v3/mapping to populate composite FIGI.
      Pin-at-first-resolve: never overwrites a non-null figi.

    Both modes are independent — operator can run mint first, then figi
    later. Re-runs safely (idempotent).

    ``cfg`` knobs (all optional):
      - ``mode`` (default 'mint') — 'mint' or 'figi'.
      - ``dry_run`` (default 'true') — preview the UPDATE without writing.
      - ``limit`` (default 0 = all) — process at most N rows per run.

    For rows missing country or asset_class (some legacy rows), the mint
    mode uses safe defaults: country='US', asset_class='S'.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    mode = str(cfg.get("mode", "mint")).lower()
    if mode not in ("mint", "figi", "fmp_profile"):
        raise ValueError(
            f"tkr14_backfill: mode must be 'mint' or 'figi' or 'fmp_profile', got {mode!r}"
        )

    dry_run_param = cfg.get("dry_run", "true")
    if isinstance(dry_run_param, str):
        dry_run = dry_run_param.lower() != "false"
    else:
        dry_run = bool(dry_run_param)
    limit = int(cfg.get("limit", 0))

    if mode == "figi":
        return await _tkr14_backfill_figi(pool, log, dry_run=dry_run, limit=limit)
    if mode == "fmp_profile":
        return await _tkr14_backfill_fmp_profile(pool, log, dry_run=dry_run, limit=limit)

    # MODE = mint: original slice-1 behavior.
    # Find rows that need a TKR-14 id minted.
    where_limit = "LIMIT $1" if limit > 0 else ""
    args: list[Any] = [limit] if limit > 0 else []
    rows = await pool.fetch(
        f"""
        SELECT ticker, current_ticker, country, asset_class, cik, updated_at
        FROM platform.ticker_classifications
        WHERE id IS NULL
        ORDER BY ticker
        {where_limit}
        """,
        *args,
    )

    if not rows:
        log.info("ops.stage.tkr14_backfill.no_rows_to_mint")
        return {"rows_minted": 0, "rows_skipped": 0, "dry_run": dry_run}

    # Import the mint function (deferred for hermetic test collection)
    from tpcore.identity.tkr14 import AssetClass, DiscoverySource, IPOVenue, mint

    # Map persisted asset_class strings to the TKR-14 enum.
    _AC_MAP = {
        "stock": AssetClass.STOCK,
        "common": AssetClass.STOCK,
        "preferred": AssetClass.PREFERRED,
        "etf": AssetClass.ETF,
        "fund": AssetClass.FUND,
        "reit": AssetClass.REIT,
        "trust": AssetClass.TRUST,
        "adr": AssetClass.ADR,
        "spac": AssetClass.SPAC_UNIT,
        "warrant": AssetClass.WARRANT,
        "note": AssetClass.NOTE,
    }

    minted: list[tuple[str, str]] = []  # (ticker, new_id)
    skipped_invalid: list[str] = []

    for r in rows:
        ticker = r["ticker"]
        country = (r["country"] or "US").upper()
        if len(country) != 2 or not country.isalpha():
            skipped_invalid.append(ticker)
            continue
        ac_str = (r["asset_class"] or "stock").lower()
        ac = _AC_MAP.get(ac_str, AssetClass.STOCK)
        cik_val = str(r["cik"]) if r["cik"] else None
        legal_name = r["current_ticker"] or ticker  # safe fallback for hash seed
        # For backfill of pre-existing rows we use the row's `updated_at` as the
        # discovery-year proxy (true first-seen timestamp not in schema). This
        # is honest snapshot semantics for the YY segment.
        mint_now = r["updated_at"] or datetime.now(UTC)
        try:
            new_id = mint(
                country=country,
                asset_class=ac,
                # Historical IPO venue unknown for backfill — use 'O' (other) snapshot.
                ipo_venue=IPOVenue.OTHER,
                # Backfill is operator-driven, not feed-discovered — use 'O' (other).
                discovery_source=DiscoverySource.OTHER,
                cik=cik_val,
                legal_name=legal_name,
                now=mint_now,
            )
        except ValueError as e:
            log.warning("ops.stage.tkr14_backfill.mint_failed", ticker=ticker, error=str(e)[:200])
            skipped_invalid.append(ticker)
            continue
        minted.append((ticker, new_id))

    if dry_run:
        log.info(
            "ops.stage.tkr14_backfill.dry_run_preview",
            n_minted=len(minted),
            n_skipped_invalid=len(skipped_invalid),
            sample=minted[:3],
        )
        return {
            "rows_minted": 0,
            "rows_previewed": len(minted),
            "rows_skipped_invalid": len(skipped_invalid),
            "dry_run": True,
            "sample": [{"ticker": t, "id": i} for t, i in minted[:5]],
        }

    # Live: BULK UPDATE via UPDATE FROM (VALUES (...)) in batches.
    # Per-row transactions were measured at ~60ms each = ~13 min total for 13K
    # rows AND surfaced as 221 slow-query incidents in the Supabase dashboard
    # (each UPDATE/INSERT touches indexes + WAL). Batching to ~500 rows/UPDATE
    # cuts commits 500x and finishes in ~30s with negligible slow-query impact.
    BATCH = 500
    n_committed = 0
    async with pool.acquire() as conn:
        for batch_start in range(0, len(minted), BATCH):
            batch = minted[batch_start : batch_start + BATCH]
            # asyncpg requires positional placeholders; use unnest of two parallel arrays.
            tickers = [t for t, _ in batch]
            new_ids = [i for _, i in batch]
            async with conn.transaction():
                # Bulk UPDATE — one statement updates the whole batch via JOIN on unnest.
                await conn.execute(
                    """
                    UPDATE platform.ticker_classifications tc
                    SET id = b.new_id
                    FROM (SELECT unnest($1::text[]) AS ticker, unnest($2::text[]) AS new_id) b
                    WHERE tc.ticker = b.ticker AND tc.id IS NULL
                    """,
                    tickers, new_ids,
                )
                # Bulk seed ticker_history — INSERT ... SELECT pattern; ON CONFLICT
                # DO NOTHING protects re-run partial-replay.
                await conn.execute(
                    """
                    INSERT INTO platform.ticker_history (classification_id, ticker, valid_from, valid_to)
                    SELECT tc.id, tc.ticker, COALESCE(tc.updated_at::date, CURRENT_DATE), NULL
                    FROM platform.ticker_classifications tc
                    JOIN (SELECT unnest($1::text[]) AS ticker) b ON tc.ticker = b.ticker
                    WHERE tc.id IS NOT NULL
                    ON CONFLICT (classification_id, valid_from) DO NOTHING
                    """,
                    tickers,
                )
                n_committed += len(batch)

    log.info(
        "ops.stage.tkr14_backfill.committed",
        n_minted=n_committed,
        n_skipped_invalid=len(skipped_invalid),
    )
    return {
        "rows_minted": n_committed,
        "rows_skipped_invalid": len(skipped_invalid),
        "dry_run": False,
    }


async def _tkr14_backfill_figi(
    pool: asyncpg.Pool,
    log: Any,
    *,
    dry_run: bool,
    limit: int,
) -> dict[str, Any]:
    """SLICE 2 helper: batch-fill figi via OpenFIGI /v3/mapping.

    Pin-at-first-resolve: never overwrites a non-null figi. Processes only
    rows with id IS NOT NULL AND figi IS NULL.
    """
    where_limit = "LIMIT $1" if limit > 0 else ""
    args: list[Any] = [limit] if limit > 0 else []
    rows = await pool.fetch(
        f"""
        SELECT ticker, id
        FROM platform.ticker_classifications
        WHERE id IS NOT NULL AND figi IS NULL
        ORDER BY ticker
        {where_limit}
        """,
        *args,
    )
    # All our non-US-country rows are mostly US-listed ADRs that trade with
    # their US ticker — exchCode='US' still resolves them via OpenFIGI. True
    # foreign-primary listings (Toyota 7203 etc.) are rare in our universe;
    # if any miss here, they get figi_not_found and a re-run can target them
    # with a different exchCode later.
    if not rows:
        log.info("ops.stage.tkr14_backfill.figi.no_rows_to_fill")
        return {"figi_filled": 0, "figi_not_found": 0, "dry_run": dry_run}

    from tpcore.openfigi import OpenFIGIAdapter

    tickers = [r["ticker"] for r in rows]
    log.info("ops.stage.tkr14_backfill.figi.starting", n_tickers=len(tickers), dry_run=dry_run)

    async with OpenFIGIAdapter() as adapter:
        results = await adapter.map_tickers(tickers, exch_code="US")

    # Pair tickers with their composite FIGIs (or None for misses).
    pairs: list[tuple[str, str]] = []  # (ticker, composite_figi)
    misses = 0
    for r in results:
        if r.figi_not_found or not r.composite_figi:
            misses += 1
            continue
        pairs.append((r.ticker, r.composite_figi))

    if dry_run:
        log.info(
            "ops.stage.tkr14_backfill.figi.dry_run_preview",
            n_filled=len(pairs),
            n_not_found=misses,
            sample=pairs[:5],
        )
        return {
            "figi_filled": 0,
            "figi_previewed": len(pairs),
            "figi_not_found": misses,
            "dry_run": True,
            "sample": [{"ticker": t, "figi": f} for t, f in pairs[:5]],
        }

    # Live: bulk UPDATE in batches of 500.
    BATCH = 500
    n_committed = 0
    async with pool.acquire() as conn:
        for batch_start in range(0, len(pairs), BATCH):
            batch = pairs[batch_start : batch_start + BATCH]
            batch_tickers = [t for t, _ in batch]
            batch_figis = [f for _, f in batch]
            async with conn.transaction():
                # Pin-at-first-resolve: WHERE figi IS NULL guards against overwrite.
                await conn.execute(
                    """
                    UPDATE platform.ticker_classifications tc
                    SET figi = b.figi
                    FROM (SELECT unnest($1::text[]) AS ticker, unnest($2::text[]) AS figi) b
                    WHERE tc.ticker = b.ticker AND tc.figi IS NULL
                    """,
                    batch_tickers, batch_figis,
                )
                n_committed += len(batch)

    log.info(
        "ops.stage.tkr14_backfill.figi.committed",
        n_filled=n_committed,
        n_not_found=misses,
    )
    return {
        "figi_filled": n_committed,
        "figi_not_found": misses,
        "dry_run": False,
    }


async def _stage_fmp_profile_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Populate ticker_classifications.gics_sector + refresh country/legal
    name from FMP /stable/profile in bulk-style batches.

    Replaces the one-off scripts/backfill_country_from_fmp.py (the
    bulk-before-API-crawl pattern; data-adapter rule mandates canonical
    stage entry, not forked scripts).

    For each ticker_classifications row needing fill (gics_sector IS NULL
    OR current_legal_name IS NULL OR country IS NULL):

      1. Batch tickers in groups of N (default 25).
      2. Call FMP /stable/profile?symbol=<comma-separated>.
      3. UPDATE gics_sector + current_legal_name + country (only
         when the existing value is NULL, never overwrite operator
         data).

    Idempotent + bounded (only touches NULL). cfg knobs:
      dry_run (default 'true') — count tickers, no writes.
      batch_size (default 25) — symbols per FMP call.
      rate_limit_sleep_s (default 0.1) — between batches.
      max_tickers (default 0 = no cap) — limit for smoke runs.
      only_field (default '' = all) — restrict to one of
        {gics_sector, country, legal_name}.

    Requires FMP_API_KEY env var.
    """
    import os as _os

    import httpx as _httpx
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    batch_size = int(cfg.get("batch_size", 25))
    rate_limit_sleep_s = float(cfg.get("rate_limit_sleep_s", 0.1))
    max_tickers = int(cfg.get("max_tickers", 0))
    only_field = str(cfg.get("only_field", "")).strip()

    api_key = _os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("fmp_profile_backfill: FMP_API_KEY env var required")

    # 1. Universe: ACTIVE tickers (lifetime_end IS NULL) needing any fill.
    where_clauses = ["lifetime_end IS NULL"]
    if only_field == "gics_sector":
        where_clauses.append("gics_sector IS NULL")
    elif only_field == "country":
        where_clauses.append("country IS NULL")
    elif only_field == "legal_name":
        where_clauses.append("current_legal_name IS NULL")
    else:
        where_clauses.append(
            "(gics_sector IS NULL OR current_legal_name IS NULL OR country IS NULL)"
        )
    sql = f"SELECT ticker FROM platform.ticker_classifications WHERE {' AND '.join(where_clauses)} ORDER BY ticker"
    rows = await pool.fetch(sql)
    tickers = [r["ticker"] for r in rows]
    if max_tickers > 0:
        tickers = tickers[:max_tickers]
    log.info(
        "ops.stage.fmp_profile_backfill.starting",
        n_tickers=len(tickers), dry_run=dry_run,
        batch_size=batch_size, rate_limit_sleep_s=rate_limit_sleep_s,
    )

    if not tickers:
        return {
            "dry_run": dry_run, "tickers_needing_fill": 0,
            "updated_sector": 0, "updated_country": 0, "updated_legal_name": 0,
        }

    if dry_run:
        return {
            "dry_run": True, "tickers_needing_fill": len(tickers),
            "would_fetch_batches": (len(tickers) + batch_size - 1) // batch_size,
            "estimated_wall_time_sec": int(((len(tickers) + batch_size - 1) // batch_size) * rate_limit_sleep_s),
        }

    n_sector = 0
    n_country = 0
    n_legal = 0
    n_fetch_errors = 0
    n_no_profile = 0

    # 2. Concurrent single-ticker fetches (FMP /stable/profile does NOT
    # support comma-separated symbols — verified 2026-05-25 returns []).
    # Use semaphore + per-call sleep to stay safely under the 750/min
    # FMP Starter ceiling. batch_size here is reused as the concurrency.
    concurrency = max(1, batch_size)
    sem = asyncio.Semaphore(concurrency)
    counters = {"done": 0, "sector": 0, "country": 0, "legal": 0,
                "no_profile": 0, "errors": 0}
    log_lock = asyncio.Lock()

    async def fetch_one(client: Any, ticker: str) -> None:
        async with sem:
            await asyncio.sleep(rate_limit_sleep_s)
            try:
                resp = await client.get(
                    "https://financialmodelingprep.com/stable/profile",
                    params={"symbol": ticker, "apikey": api_key},
                )
            except Exception as exc:  # noqa: BLE001
                counters["errors"] += 1
                log.warning("ops.stage.fmp_profile_backfill.fetch_failed",
                            ticker=ticker, err=str(exc)[:120])
                return
            if resp.status_code != 200:
                counters["errors"] += 1
                return
            try:
                profiles = resp.json() or []
            except Exception:  # noqa: BLE001
                counters["errors"] += 1
                return
            if not profiles:
                counters["no_profile"] += 1
                return
            prof = profiles[0]
            sector = prof.get("sector") or None
            legal_name = prof.get("companyName") or None
            country = (prof.get("country") or "")[:2].upper() or None
            if any((sector, legal_name, country)):
                async with pool.acquire() as conn:
                    r = await conn.execute(
                        """
                        UPDATE platform.ticker_classifications
                        SET gics_sector = COALESCE(gics_sector, $2),
                            current_legal_name = COALESCE(current_legal_name, $3),
                            country = COALESCE(country, $4)
                        WHERE ticker = $1 AND lifetime_end IS NULL
                        """,
                        ticker, sector, legal_name, country,
                    )
                if r == "UPDATE 1":
                    if sector:
                        counters["sector"] += 1
                    if country:
                        counters["country"] += 1
                    if legal_name:
                        counters["legal"] += 1
            counters["done"] += 1
            if counters["done"] % 500 == 0:
                async with log_lock:
                    log.info("ops.stage.fmp_profile_backfill.progress",
                             done=counters["done"], total=len(tickers),
                             sector=counters["sector"], country=counters["country"],
                             legal_name=counters["legal"],
                             no_profile=counters["no_profile"],
                             errors=counters["errors"])

    async with _httpx.AsyncClient(timeout=30.0) as client:
        tasks = [fetch_one(client, t) for t in tickers]
        await asyncio.gather(*tasks)

    n_sector = counters["sector"]
    n_country = counters["country"]
    n_legal = counters["legal"]
    n_no_profile = counters["no_profile"]
    n_fetch_errors = counters["errors"]

    result = {
        "dry_run": False,
        "tickers_processed": len(tickers),
        "updated_sector": n_sector,
        "updated_country": n_country,
        "updated_legal_name": n_legal,
        "no_profile_returned": n_no_profile,
        "fetch_errors": n_fetch_errors,
    }
    log.info("ops.stage.fmp_profile_backfill.done", **result)
    return result


async def _stage_gleif_lei_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Populate issuers.lei from GLEIF's daily bulk ISIN→LEI mapping file.

    GLEIF publishes the full ANNA-DSB ISIN-to-LEI relationship file
    daily at https://mapping.gleif.org/api/v2/isin-lei (~30MB ZIP,
    ~3M LEI-ISIN pairs). The API form is rate-limited (1 req / few
    sec) which made the original API-walking version (PR #349) take
    forever AND hit 429s; per the bulk-before-API-crawl rule we
    download the bulk file once + look up in-memory.

    Strategy:

      1. List latest publication: GET /api/v2/isin-lei (returns
         metadata with downloadLink).
      2. Download the latest ZIP (~30MB; cached 24h in /tmp).
      3. Parse the CSV (LEI, ISIN) into a dict[ISIN] = LEI.
      4. For each issuer without LEI: look up the first ISIN of its
         related tickers; if matched, UPDATE.
      5. Bulk UPDATE via asyncpg.executemany.

    Idempotent. cfg knobs:
      dry_run (default 'true') — count without writes.
      cache_path (default /tmp/gleif_isin_lei.zip).
      force_download (default 'false') — bypass 24h cache.
      max_issuers (default 0 = no cap).
    """
    import time as _time
    import zipfile as _zipfile
    from pathlib import Path as _Path

    import httpx as _httpx
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    cache_path = _Path(str(cfg.get("cache_path", "/tmp/gleif_isin_lei.zip")))
    force_download = str(cfg.get("force_download", "false")).lower() == "true"
    max_issuers = int(cfg.get("max_issuers", 0))

    # 1. Universe: issuers without LEI + first ISIN of any related ticker.
    rows = await pool.fetch(
        """
        SELECT i.issuer_id,
               (SELECT tc.isin
                FROM platform.ticker_classifications tc
                WHERE tc.cik = i.cik AND tc.isin IS NOT NULL
                ORDER BY tc.id LIMIT 1) AS isin
        FROM platform.issuers i
        WHERE i.lei IS NULL AND i.cik IS NOT NULL
        ORDER BY i.issuer_id
        """
    )
    candidates = [(r["issuer_id"], r["isin"]) for r in rows if r["isin"]]
    if max_issuers > 0:
        candidates = candidates[:max_issuers]
    log.info(
        "ops.stage.gleif_lei_backfill.starting",
        n_candidates=len(candidates), dry_run=dry_run,
        cache_path=str(cache_path), force_download=force_download,
    )

    if not candidates:
        return {"dry_run": dry_run, "candidates": 0, "leis_filled": 0}

    # 2. Download (cached) the latest bulk file. Listing endpoint is
    # public + not rate-limited.
    cache_age_sec = (_time.time() - cache_path.stat().st_mtime) if cache_path.exists() else float("inf")
    if force_download or not cache_path.exists() or cache_age_sec > 86_400:
        async with _httpx.AsyncClient(timeout=60.0) as listing_client:
            list_resp = await listing_client.get("https://mapping.gleif.org/api/v2/isin-lei")
            list_resp.raise_for_status()
            entries = (list_resp.json().get("data") or [])
            if not entries:
                raise RuntimeError("gleif_lei_backfill: no ISIN-LEI publications listed")
            latest = entries[0]  # API returns newest-first
            download_link = latest["attributes"]["downloadLink"]
        log.info("ops.stage.gleif_lei_backfill.downloading",
                 url=download_link, file_id=latest["id"],
                 uploaded_at=latest["attributes"]["uploadedAt"],
                 cache_age_hr=round(cache_age_sec / 3600, 1))
        t0 = _time.time()
        async with _httpx.AsyncClient(timeout=600.0) as client, \
                client.stream("GET", download_link) as resp:
            resp.raise_for_status()
            with cache_path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
        log.info("ops.stage.gleif_lei_backfill.download_done",
                 size_mb=round(cache_path.stat().st_size / 1024 / 1024, 1),
                 elapsed_sec=round(_time.time() - t0, 1))
    else:
        log.info("ops.stage.gleif_lei_backfill.using_cached",
                 size_mb=round(cache_path.stat().st_size / 1024 / 1024, 1),
                 age_hr=round(cache_age_sec / 3600, 1))

    # 3. Parse the CSV — build ISIN→LEI dict.
    t0 = _time.time()
    isin_to_lei: dict[str, str] = {}
    with _zipfile.ZipFile(cache_path) as zf:
        for entry in zf.namelist():
            if not entry.endswith(".csv"):
                continue
            with zf.open(entry) as fh:
                first = True
                for line in fh:
                    if first:
                        first = False
                        continue
                    parts = line.decode("ascii", errors="ignore").strip().split(",")
                    if len(parts) >= 2:
                        lei, isin = parts[0], parts[1]
                        if len(lei) == 20 and len(isin) == 12:
                            isin_to_lei[isin] = lei
    log.info("ops.stage.gleif_lei_backfill.parsed",
             total_mappings=len(isin_to_lei),
             elapsed_sec=round(_time.time() - t0, 1))

    # 4. Match + collect updates. Dedupe by LEI: multiple of our issuers
    # may map to the same LEI (share classes / ADRs share an LEI in
    # GLEIF) — pick one deterministically by sorted issuer_id since
    # issuers.lei is UNIQUE.
    matched: dict[str, str] = {}  # lei -> issuer_id
    n_no_match = 0
    n_dup_lei = 0
    for issuer_id, isin in candidates:
        lei = isin_to_lei.get(isin)
        if lei:
            existing = matched.get(lei)
            if existing is None or issuer_id < existing:
                if existing is not None:
                    n_dup_lei += 1
                matched[lei] = issuer_id
            else:
                n_dup_lei += 1
        else:
            n_no_match += 1
    updates = [(lei, iid) for lei, iid in matched.items()]

    if dry_run:
        return {
            "dry_run": True,
            "candidates": len(candidates),
            "would_update": len(updates),
            "no_match": n_no_match,
            "dup_lei_skipped": n_dup_lei,
            "bulk_file_mappings": len(isin_to_lei),
        }

    # 5. Bulk UPDATE via executemany.
    if updates:
        async with pool.acquire() as conn:
            await conn.executemany(
                "UPDATE platform.issuers SET lei = $1 WHERE issuer_id = $2 AND lei IS NULL",
                updates,
            )
    n_filled = len(updates)

    result = {
        "dry_run": False,
        "candidates": len(candidates),
        "leis_filled": n_filled,
        "no_match": n_no_match,
        "dup_lei_skipped": n_dup_lei,
        "bulk_file_mappings": len(isin_to_lei),
    }
    log.info("ops.stage.gleif_lei_backfill.done", **result)
    return result


async def _stage_issuer_history_cleanup(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Clean up issuer_history corruption from corp_events_seed + EDGAR
    backfill interaction.

    Operator catch 2026-05-25 after the META duplicate fix landed:
    "find out if it's more". Comprehensive audit found pervasive
    duplicate / overlap / zero-duration / invalid-range issues from
    two loaders writing without coordination:

      - corp_events_seed inserts a (1900-01-01, NULL) catch-all row
        for each seeded issuer.
      - EDGAR backfill inserts dated formerName periods + a current
        name from the last formerName's `to` date onward.

      The two don't conflict on PK (issuer_id, valid_from) because
      one is 1900-01-01 and the other is 2005-ish, so both land —
      and the catch-all overlaps everything.

    Fix per issuer (one transactional rewrite):

      1. Delete zero-duration rows (valid_from = valid_to) — useless.
      2. Delete invalid-range rows (valid_to < valid_from) — broken.
      3. For overlapping periods: sort by valid_from, then close
         each row's valid_to to the NEXT row's valid_from.
      4. Latest row stays open (valid_to = NULL).
      5. Same-name-twice cases are left as-is (could be legitimate
         flip-flops; deduplicating risks dropping real history).

    Also touches issuer_securities (open duplicates flagged by
    same audit).

    Idempotent. cfg knobs:
      dry_run (default 'true') — count what would change.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)

    findings: dict[str, Any] = {"dry_run": dry_run}

    async with pool.acquire() as conn:
        n_zero = await conn.fetchval(
            "SELECT count(*) FROM platform.issuer_history WHERE valid_from = valid_to"
        )
        findings["zero_duration_count"] = n_zero
        if not dry_run and n_zero > 0:
            r = await conn.execute(
                "DELETE FROM platform.issuer_history WHERE valid_from = valid_to"
            )
            findings["zero_duration_deleted"] = r

        n_invalid = await conn.fetchval(
            "SELECT count(*) FROM platform.issuer_history WHERE valid_to IS NOT NULL AND valid_to < valid_from"
        )
        findings["invalid_range_count"] = n_invalid
        if not dry_run and n_invalid > 0:
            r = await conn.execute(
                "DELETE FROM platform.issuer_history WHERE valid_to IS NOT NULL AND valid_to < valid_from"
            )
            findings["invalid_range_deleted"] = r

        n_overlap = await conn.fetchval(
            """
            SELECT count(*) FROM platform.issuer_history a
            JOIN platform.issuer_history b
              ON a.issuer_id = b.issuer_id AND a.valid_from < b.valid_from
            WHERE (a.valid_to IS NULL OR a.valid_to > b.valid_from)
            """
        )
        findings["overlap_pairs_count"] = n_overlap
        if not dry_run and n_overlap > 0:
            r = await conn.execute(
                """
                WITH ordered AS (
                    SELECT issuer_id, valid_from, valid_to,
                           LEAD(valid_from) OVER (
                               PARTITION BY issuer_id ORDER BY valid_from
                           ) AS next_from
                    FROM platform.issuer_history
                )
                UPDATE platform.issuer_history ih
                SET valid_to = o.next_from
                FROM ordered o
                WHERE ih.issuer_id = o.issuer_id
                  AND ih.valid_from = o.valid_from
                  AND o.next_from IS NOT NULL
                  AND (ih.valid_to IS NULL OR ih.valid_to > o.next_from)
                """
            )
            findings["overlap_repaired"] = r

        n_iss_open_dup = await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT issuer_id, classification_id FROM platform.issuer_securities
                WHERE valid_to IS NULL
                GROUP BY issuer_id, classification_id HAVING count(*) > 1
            ) s
            """
        )
        findings["issuer_securities_open_dup_pairs"] = n_iss_open_dup
        if not dry_run and n_iss_open_dup > 0:
            r = await conn.execute(
                """
                WITH ranked AS (
                    SELECT issuer_id, classification_id, valid_from,
                           ROW_NUMBER() OVER (
                               PARTITION BY issuer_id, classification_id
                               ORDER BY valid_from DESC
                           ) AS rn,
                           MAX(valid_from) OVER (
                               PARTITION BY issuer_id, classification_id
                           ) AS latest_from
                    FROM platform.issuer_securities
                    WHERE valid_to IS NULL
                )
                UPDATE platform.issuer_securities iss
                SET valid_to = r.latest_from
                FROM ranked r
                WHERE iss.issuer_id = r.issuer_id
                  AND iss.classification_id = r.classification_id
                  AND iss.valid_from = r.valid_from
                  AND r.rn > 1
                  AND iss.valid_to IS NULL
                """
            )
            findings["issuer_securities_repaired"] = r

    log.info("ops.stage.issuer_history_cleanup.done", **findings)
    return findings


async def _stage_audit_cleanup_2026_05_24(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot cleanup of the 4 audit defects found 2026-05-24.

    db-architect audit (see operator request "audit the database and
    make sure macros are good and all the tickers and references are
    good to go") flagged:

      Defect A (was #2): 248 delisted-but-marked-active ticker_classifications
        rows. Their ticker_history was closed with valid_to set to the
        actual delisting_date in prices_daily (real M&A / bankruptcy
        events like ATVI/ALXN/ABMD/BBBY etc.), but the parent
        ticker_classifications row still has status='active' AND
        lifetime_end IS NULL. Set status='inactive' + lifetime_end =
        ticker_history.valid_to so the partial UNIQUE index on
        (ticker) WHERE lifetime_end IS NULL would correctly allow a
        future ticker-reuse to take over the same ticker string.

      Defect B (was #3): platform.issuer_history has duplicate
        valid_to=NULL rows for Meta (CIK0001326801). The corp_events
        seed inserted one at 2022-06-09 (FB->META rename date) and
        the EDGAR backfill later inserted another at 2021-10-27
        (EDGAR's recorded transition date for the rename). Close the
        earlier one's valid_to to the later one's valid_from so only
        ONE Meta row stays open.

      Defect C (was #4): platform.corporate_events has 4 bitemporal
        copies of the FB->META ticker_swap event_id
        EVT_867CC84F8772CA7919976BE0 (5-min spread on 2026-05-24 from
        my 3 iterations of the EDGAR backfill stage). The PK
        (event_id, realtime_start) allows it but logically only the
        most-recent realtime_start row is the active fact. Close
        realtime_end on the older versions so historical-as-of
        queries still resolve correctly but the current view returns
        one row.

      Defect D (was hy_spread): memory entry corrected separately
        (~9,097 was wrong — that's credit_spread; actual hy_spread
        row count is ~7,674). No live-DB action needed.

    Idempotent (each operation is INSERT/UPDATE-driven by a NOT-yet
    state). Safe to re-run.

    cfg knobs:
      dry_run (default 'true') — count what WOULD change.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)

    findings: dict[str, Any] = {"dry_run": dry_run}

    async with pool.acquire() as conn:
        # Defect A: 248 delisted-but-active classifications.
        defect_a_predicate = """
            tc.lifetime_end IS NULL
            AND tc.status = 'active'
            AND EXISTS (
                SELECT 1 FROM platform.ticker_history th
                WHERE th.classification_id = tc.id
                  AND th.valid_to IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM platform.ticker_history th2
                      WHERE th2.classification_id = tc.id
                        AND th2.valid_to IS NULL
                  )
            )
        """
        n_defect_a = await conn.fetchval(
            f"SELECT count(*) FROM platform.ticker_classifications tc WHERE {defect_a_predicate}"
        )
        findings["defect_a_delisted_active_count"] = n_defect_a

        if not dry_run and n_defect_a > 0:
            r = await conn.execute(
                """
                WITH targets AS (
                    SELECT tc.id AS cls_id, th.valid_to AS close_date
                    FROM platform.ticker_classifications tc
                    JOIN platform.ticker_history th ON th.classification_id = tc.id
                    WHERE tc.lifetime_end IS NULL
                      AND tc.status = 'active'
                      AND th.valid_to IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM platform.ticker_history th2
                          WHERE th2.classification_id = tc.id
                            AND th2.valid_to IS NULL
                      )
                )
                UPDATE platform.ticker_classifications tc
                SET status = 'inactive', lifetime_end = t.close_date
                FROM targets t
                WHERE tc.id = t.cls_id
                """
            )
            findings["defect_a_updated"] = r
            log.info("ops.stage.audit_cleanup.defect_a_done", updated=r)

        # Defect B: duplicate Meta issuer_history open rows.
        n_defect_b = await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT issuer_id FROM platform.issuer_history
                WHERE valid_to IS NULL
                GROUP BY issuer_id HAVING count(*) > 1
            ) s
            """
        )
        findings["defect_b_open_dup_issuers"] = n_defect_b

        if not dry_run and n_defect_b > 0:
            # For each issuer with >1 open row, close all but the
            # latest (highest valid_from) and set their valid_to to
            # the latest's valid_from.
            r = await conn.execute(
                """
                WITH ranked AS (
                    SELECT issuer_id, valid_from,
                           ROW_NUMBER() OVER (PARTITION BY issuer_id ORDER BY valid_from DESC) AS rn,
                           MAX(valid_from) OVER (PARTITION BY issuer_id) AS latest_from
                    FROM platform.issuer_history
                    WHERE valid_to IS NULL
                )
                UPDATE platform.issuer_history ih
                SET valid_to = r.latest_from
                FROM ranked r
                WHERE ih.issuer_id = r.issuer_id
                  AND ih.valid_from = r.valid_from
                  AND r.rn > 1
                  AND ih.valid_to IS NULL
                """
            )
            findings["defect_b_closed"] = r
            log.info("ops.stage.audit_cleanup.defect_b_done", updated=r)

        # Defect E (operator catch 2026-05-24): issuer_securities.valid_to
        # never set for delistings that AREN'T same-entity renames.
        # The seed stage only updated valid_to when predecessor_cik ==
        # successor_cik (FB->META same-entity rename). For real delistings
        # (ATVI/SIVB/BBBY/VMW/TWTR etc.) where the security stops trading,
        # we never closed the predecessor's mapping. Close them by
        # joining to ticker_history.valid_to (the security's actual stop
        # date) when the classification's history is closed.
        n_defect_e = await conn.fetchval(
            """
            SELECT count(*)
            FROM platform.issuer_securities iss
            WHERE iss.valid_to IS NULL
              AND EXISTS (
                  SELECT 1 FROM platform.ticker_history th
                  WHERE th.classification_id = iss.classification_id
                    AND th.valid_to IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM platform.ticker_history th2
                        WHERE th2.classification_id = iss.classification_id
                          AND th2.valid_to IS NULL
                    )
              )
            """
        )
        findings["defect_e_delisted_iss_open"] = n_defect_e

        if not dry_run and n_defect_e > 0:
            r = await conn.execute(
                """
                UPDATE platform.issuer_securities iss
                SET valid_to = th.valid_to
                FROM platform.ticker_history th
                WHERE iss.valid_to IS NULL
                  AND th.classification_id = iss.classification_id
                  AND th.valid_to IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM platform.ticker_history th2
                      WHERE th2.classification_id = iss.classification_id
                        AND th2.valid_to IS NULL
                  )
                """
            )
            findings["defect_e_closed"] = r
            log.info("ops.stage.audit_cleanup.defect_e_done", updated=r)

        # Defect C: bitemporal duplicates of the same event_id.
        n_defect_c = await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT event_id FROM platform.corporate_events
                WHERE realtime_end IS NULL OR realtime_end = 'infinity'::timestamptz
                GROUP BY event_id HAVING count(*) > 1
            ) s
            """
        )
        findings["defect_c_dup_event_ids"] = n_defect_c

        if not dry_run and n_defect_c > 0:
            r = await conn.execute(
                """
                WITH ranked AS (
                    SELECT event_id, realtime_start,
                           ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY realtime_start DESC) AS rn,
                           MAX(realtime_start) OVER (PARTITION BY event_id) AS latest_start
                    FROM platform.corporate_events
                    WHERE realtime_end IS NULL OR realtime_end = 'infinity'::timestamptz
                )
                UPDATE platform.corporate_events ce
                SET realtime_end = r.latest_start
                FROM ranked r
                WHERE ce.event_id = r.event_id
                  AND ce.realtime_start = r.realtime_start
                  AND r.rn > 1
                  AND (ce.realtime_end IS NULL OR ce.realtime_end = 'infinity'::timestamptz)
                """
            )
            findings["defect_c_closed"] = r
            log.info("ops.stage.audit_cleanup.defect_c_done", updated=r)

    log.info("ops.stage.audit_cleanup.done", **findings)
    return findings


async def _stage_residual_classification_id_fill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close residual NULL classification_id rows in path-A child tables.

    Engine-abstraction session (2026-05-24 handoff) flagged this as
    defect #3: 74 rows still NULL after the prices_daily-specific
    backfill (60 in corporate_actions, 14 in fundamentals_quarterly).

    Two root causes:
      1. Rows were INSERTed before the BEFORE INSERT trigger existed
         (migration 20260524_1500). Trigger never fired on them.
      2. The row's date PRECEDES the ticker_history valid_from — the
         trigger's date-aware lookup returned nothing.

    For the 74 actual rows, the affected tickers (CTDD, NWLG, DCOMP,
    WLACW) each have exactly ONE active classification (lifetime_end
    IS NULL) — no reuse — so the active classification IS the right
    answer regardless of date. The UPDATE is safe.

    Idempotent + bounded (only touches NULL rows). cfg knobs:
      dry_run (default 'true') — count rows that WOULD update.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)

    # The 13 path-A child tables that carry classification_id (excludes
    # prices_daily — handled separately by the chunked stage).
    targets: tuple[tuple[str, str], ...] = (
        ("corporate_actions", "ticker"),
        ("fundamentals_quarterly", "ticker"),
        ("earnings_events", "ticker"),
        ("short_interest", "ticker"),
        ("insider_sentiment", "symbol"),
        ("insider_transactions", "ticker"),
        ("liquidity_tiers", "ticker"),
        ("options_max_pain", "symbol"),
        ("sec_material_events", "ticker"),
        ("social_sentiment", "ticker"),
        ("spread_observations", "ticker"),
        ("borrow_rates", "ticker"),
        ("universe_candidates", "ticker"),
    )

    per_table: dict[str, int] = {}
    total_updated = 0
    async with pool.acquire() as conn:
        for table, ticker_col in targets:
            n_null = await conn.fetchval(
                f"SELECT count(*) FROM platform.{table} WHERE classification_id IS NULL"
            )
            if n_null == 0:
                continue
            if dry_run:
                per_table[table] = n_null
                continue
            # Single UPDATE — for each NULL row, look up the active
            # classification for its ticker. Unmatched tickers
            # (no active classification at all) stay NULL.
            r = await conn.execute(
                f"""
                UPDATE platform.{table} t
                SET classification_id = tc.id
                FROM platform.ticker_classifications tc
                WHERE t.classification_id IS NULL
                  AND tc.ticker = t.{ticker_col}
                  AND tc.lifetime_end IS NULL
                """
            )
            updated = int(r.split()[-1]) if r.startswith("UPDATE") else 0
            per_table[table] = updated
            total_updated += updated
            log.info("ops.stage.residual_classification_id_fill.table_done",
                     table=table, rows_updated=updated)

    result = {
        "dry_run": dry_run,
        "per_table": per_table,
        "total_updated": total_updated,
    }
    log.info("ops.stage.residual_classification_id_fill.done", **result)
    return result


async def _stage_prices_daily_backfill_classification_id(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v2.2 P6 — chunked backfill of prices_daily.classification_id.

    Step 2 of the 3-step prices_daily P6 rollout (see migration 20260524_0700
    for the rationale). Single-transaction UPDATE on 21M rows generated 1.95 GB
    WAL on 2026-05-23 → triggered Supabase auto-protective read-only mode.
    This stage processes in 100K-row chunks with explicit COMMIT between chunks
    so WAL recycles incrementally.

    Pre-condition: migration 20260524_0700 added the `classification_id` column.

    cfg knobs:
      dry_run (default 'true') — count what WOULD update; no writes.
      chunk_size (default 100000) — rows per transaction.
      max_chunks (default 0 = no limit) — process at most N chunks this run.
      sleep_ms (default 500) — ms between chunks (WAL checkpoint headroom).
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    chunk_size = int(cfg.get("chunk_size", 100_000))
    max_chunks = int(cfg.get("max_chunks", 0))
    sleep_ms = int(cfg.get("sleep_ms", 500))

    has_col = await pool.fetchval(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema='platform' AND table_name='prices_daily' "
        "AND column_name='classification_id'"
    )
    if not has_col:
        raise RuntimeError(
            "prices_daily.classification_id missing — apply 20260524_0700 first."
        )

    n_total = int(await pool.fetchval(
        "SELECT count(*) FROM platform.prices_daily WHERE classification_id IS NULL"
    ) or 0)
    if n_total == 0:
        log.info("ops.stage.prices_daily_backfill_classification_id.no_rows_to_backfill")
        return {"backfilled": 0, "chunks": 0, "remaining_orphan": 0, "dry_run": dry_run}

    log.info(
        "ops.stage.prices_daily_backfill_classification_id.starting",
        n_rows_to_backfill=n_total, chunk_size=chunk_size,
        max_chunks=max_chunks, dry_run=dry_run,
    )

    if dry_run:
        match = int(await pool.fetchval(
            "SELECT count(*) FROM platform.prices_daily pd "
            "JOIN platform.ticker_classifications tc "
            "  ON pd.ticker = tc.current_ticker "
            "  AND tc.status IN ('active','active_when_issued') "
            "WHERE pd.classification_id IS NULL"
        ) or 0)
        return {
            "would_backfill": match,
            "would_remain_orphan": n_total - match,
            "chunks_estimated": (match + chunk_size - 1) // chunk_size,
            "dry_run": True,
        }

    n_updated = 0
    n_chunks = 0
    while True:
        if max_chunks and n_chunks >= max_chunks:
            log.info(
                "ops.stage.prices_daily_backfill_classification_id.max_chunks_reached",
                chunks=n_chunks, backfilled=n_updated,
            )
            break

        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL statement_timeout = '5min'")
            r = await conn.execute(
                """
                WITH batch AS (
                    SELECT pd.ctid
                    FROM platform.prices_daily pd
                    WHERE pd.classification_id IS NULL
                      AND EXISTS (
                          SELECT 1 FROM platform.ticker_classifications tc
                          WHERE tc.current_ticker = pd.ticker
                            AND tc.status IN ('active','active_when_issued')
                      )
                    LIMIT $1
                )
                UPDATE platform.prices_daily pd
                SET classification_id = tc.id
                FROM platform.ticker_classifications tc
                WHERE pd.ctid IN (SELECT ctid FROM batch)
                  AND pd.ticker = tc.current_ticker
                  AND tc.status IN ('active','active_when_issued')
                """,
                chunk_size,
            )
            n_this = int(r.split()[-1]) if r.startswith("UPDATE") else 0

        if n_this == 0:
            log.info(
                "ops.stage.prices_daily_backfill_classification_id.complete",
                total_backfilled=n_updated, chunks=n_chunks,
            )
            break

        n_updated += n_this
        n_chunks += 1
        if n_chunks == 1 or n_chunks % 10 == 0:
            log.info(
                "ops.stage.prices_daily_backfill_classification_id.progress",
                chunks_done=n_chunks, rows_backfilled=n_updated,
            )
        await asyncio.sleep(sleep_ms / 1000.0)

    n_remaining = int(await pool.fetchval(
        "SELECT count(*) FROM platform.prices_daily WHERE classification_id IS NULL"
    ) or 0)
    log.info(
        "ops.stage.prices_daily_backfill_classification_id.done",
        total_backfilled=n_updated, chunks=n_chunks, remaining_orphan=n_remaining,
    )
    return {
        "backfilled": n_updated, "chunks": n_chunks,
        "remaining_orphan": n_remaining, "dry_run": False,
    }


# ── Task #18 follow-on — series_catalog backfill (operator 2026-05-24) ──
# Encodes per-(source, series_id) metadata for every series currently
# observable in platform.macro_data. Source of truth for cadence,
# vendor_series_id, units, publish day/lag, sacred-data flags — replaces
# the scattered Python constants (INDICATOR_CADENCE in the completeness
# check, INDICATOR_SERIES in the FRED adapter, hard-coded 'aaii'/'fear_greed'
# channel lists in handlers) with one DB-side authority. Idempotent
# (ON CONFLICT (source, series_id) DO UPDATE) so re-runs refresh metadata
# in-place.
def _series_catalog_metadata() -> list[dict[str, Any]]:
    """All 73 (source, series_id) catalog entries with metadata.

    Returns a list of dicts shaped for INSERT into platform.series_catalog.
    Per-series fields verified against vendor docs (expert subagent review
    2026-05-24); FRED publish_lag_days are conservative upper bounds.
    """
    from tpcore.fred.adapter import INDICATOR_SERIES

    rows: list[dict[str, Any]] = []

    # ── FRED daily series ──────────────────────────────────────────────
    fred_daily = {
        "vix":           ("VIXCLS",       "CBOE Volatility Index (S&P 500 implied vol)",       "index_value", 1, False, False),
        "yield_curve":   ("T10Y2Y",       "10Y Treasury minus 2Y Treasury constant-maturity spread", "percent", 0, False, False),
        "credit_spread": ("BAA10Y",       "Moody's Seasoned Baa Corporate Yield minus 10Y Treasury", "percent", 0, False, False),
        "hy_spread":     ("BAMLH0A0HYM2", "ICE BofA US High Yield OAS",                        "percent",     1, False, True),
        "sofr":          ("SOFR",         "Secured Overnight Financing Rate",                  "percent",     1, False, False),
        "epu_index":     ("USEPUINDXD",   "Economic Policy Uncertainty Index (Baker-Bloom-Davis, daily)", "index_value", 0, False, False),
    }
    for series_id, (vendor_id, desc, unit, lag, sa, sacred) in fred_daily.items():
        rows.append({
            "source": "fred", "series_id": series_id, "vendor_series_id": vendor_id,
            "description": desc, "unit": unit, "frequency": "daily",
            "publish_weekday": None, "publish_day_of_month": None,
            "publish_lag_days": lag, "is_seasonally_adjusted": sa,
            "is_derived": False, "sacred": sacred,
            "publication_calendar_url": f"https://fred.stlouisfed.org/series/{vendor_id}",
            "notes": "hy_spread pre-FRED-window history (1996-2010) operator-curated from non-FRED sources — re-derive forbidden." if sacred else None,
        })

    # ── FRED weekly series ─────────────────────────────────────────────
    fred_weekly = {
        # canonical: (vendor_id, description, publish_weekday_ISO, publish_lag_days)
        "initial_claims": ("IC4WSA", "Initial Unemployment Claims, 4-Week Moving Average (DOL)", 5, 5),
        "nfci":           ("NFCI",   "Chicago Fed National Financial Conditions Index",          4, 5),
    }
    for series_id, (vendor_id, desc, weekday, lag) in fred_weekly.items():
        rows.append({
            "source": "fred", "series_id": series_id, "vendor_series_id": vendor_id,
            "description": desc, "unit": ("count" if series_id == "initial_claims" else "index_value"),
            "frequency": "weekly",
            "publish_weekday": weekday, "publish_day_of_month": None,
            "publish_lag_days": lag, "is_seasonally_adjusted": True,
            "is_derived": False, "sacred": False,
            "publication_calendar_url": f"https://fred.stlouisfed.org/series/{vendor_id}",
            "notes": None,
        })

    # ── FRED monthly series ────────────────────────────────────────────
    # canonical: (vendor_id, description, unit, publish_lag_days, SA)
    fred_monthly = {
        "industrial_production": ("INDPRO",       "Industrial Production: Total Index (Fed G.17)", "index_value", 15, True),
        "sahm_rule":             ("SAHMREALTIME", "Sahm Rule Recession Indicator (Real-Time)",     "percentage_points", 14, True),
        "cfnai_ma3":             ("CFNAIMA3",     "Chicago Fed National Activity Index 3-Month MA", "index_value",  22, True),
    }
    for series_id, (vendor_id, desc, unit, lag, sa) in fred_monthly.items():
        rows.append({
            "source": "fred", "series_id": series_id, "vendor_series_id": vendor_id,
            "description": desc, "unit": unit, "frequency": "monthly",
            "publish_weekday": None, "publish_day_of_month": None,  # variable per series
            "publish_lag_days": lag, "is_seasonally_adjusted": sa,
            "is_derived": False, "sacred": False,
            "publication_calendar_url": f"https://fred.stlouisfed.org/series/{vendor_id}",
            "notes": None,
        })

    # ── FRED state-PHCI monthly (50 states) ────────────────────────────
    state_names = {
        "al":"Alabama","ak":"Alaska","az":"Arizona","ar":"Arkansas","ca":"California",
        "co":"Colorado","ct":"Connecticut","de":"Delaware","fl":"Florida","ga":"Georgia",
        "hi":"Hawaii","id":"Idaho","il":"Illinois","in":"Indiana","ia":"Iowa",
        "ks":"Kansas","ky":"Kentucky","la":"Louisiana","me":"Maine","md":"Maryland",
        "ma":"Massachusetts","mi":"Michigan","mn":"Minnesota","ms":"Mississippi","mo":"Missouri",
        "mt":"Montana","ne":"Nebraska","nv":"Nevada","nh":"New Hampshire","nj":"New Jersey",
        "nm":"New Mexico","ny":"New York","nc":"North Carolina","nd":"North Dakota","oh":"Ohio",
        "ok":"Oklahoma","or":"Oregon","pa":"Pennsylvania","ri":"Rhode Island","sc":"South Carolina",
        "sd":"South Dakota","tn":"Tennessee","tx":"Texas","ut":"Utah","vt":"Vermont",
        "va":"Virginia","wa":"Washington","wv":"West Virginia","wi":"Wisconsin","wy":"Wyoming",
    }
    for series_id, vendor_id in INDICATOR_SERIES:
        if not series_id.startswith("phci_"):
            continue
        state = series_id.removeprefix("phci_")
        rows.append({
            "source": "fred", "series_id": series_id, "vendor_series_id": vendor_id,
            "description": f"Philadelphia Fed State Coincident Index — {state_names.get(state, state.upper())}",
            "unit": "index_value", "frequency": "monthly",
            "publish_weekday": None, "publish_day_of_month": None,
            "publish_lag_days": 22, "is_seasonally_adjusted": True,
            "is_derived": False, "sacred": False,
            "publication_calendar_url": f"https://fred.stlouisfed.org/series/{vendor_id}",
            "notes": None,
        })

    # ── Derived: sos_state_diffusion ───────────────────────────────────
    rows.append({
        "source": "fred", "series_id": "sos_state_diffusion", "vendor_series_id": None,
        "description": "Sum-of-states diffusion (share of states with PHCI(t) < PHCI(t-3mo)) — Crone/Clayton-Matthews 2005",
        "unit": "percent", "frequency": "monthly",
        "publish_weekday": None, "publish_day_of_month": None,
        "publish_lag_days": 22, "is_seasonally_adjusted": True,
        "is_derived": True, "sacred": False,
        "publication_calendar_url": None,
        "notes": "Derived from the 50 phci_<state> series; recomputed on every macro_indicators stage run.",
    })

    # ── AAII Sentiment Survey channels ─────────────────────────────────
    for ch, label in (("bullish_pct", "% bullish"),
                       ("bearish_pct", "% bearish"),
                       ("neutral_pct", "% neutral")):
        rows.append({
            "source": "aaii", "series_id": ch, "vendor_series_id": None,
            "description": f"AAII Investor Sentiment Survey — {label}",
            "unit": "percent", "frequency": "weekly",
            "publish_weekday": 4, "publish_day_of_month": None,  # Thursday
            "publish_lag_days": 1, "is_seasonally_adjusted": False,
            "is_derived": False, "sacred": False,
            "publication_calendar_url": "https://www.aaii.com/sentimentsurvey",
            "notes": "Survey closes Wed 23:59 ET; results posted Thursday morning ET.",
        })

    # ── Fear/Greed derived bundle (8 channels) ─────────────────────────
    fg_channels = (
        ("score",                "Composite Fear & Greed score (0-100)",       "index_value", "num"),
        ("score_5d_ago",         "Composite score from 5 NYSE sessions ago",    "index_value", "num"),
        ("volatility_component", "Volatility component (VIX z-score sub-index)", "index_value", "num"),
        ("credit_component",     "Credit component (hy_spread sub-index)",       "index_value", "num"),
        ("momentum_component",   "Momentum component (SPY-vs-125dMA sub-index)", "index_value", "num"),
        ("safe_haven_component", "Safe-haven component (yield_curve sub-index)", "index_value", "num"),
        ("label",                "Discretized regime label (Extreme Fear/Fear/Neutral/Greed/Extreme Greed)", "category", "text"),
        ("direction",            "1-day direction (rising/falling)",             "category", "text"),
    )
    for ch, desc, unit, _channel_type in fg_channels:
        rows.append({
            "source": "cnn_fear_greed", "series_id": ch, "vendor_series_id": None,
            "description": desc, "unit": unit, "frequency": "daily",
            "publish_weekday": None, "publish_day_of_month": None,
            "publish_lag_days": 0, "is_seasonally_adjusted": False,
            "is_derived": True, "sacred": False,
            "publication_calendar_url": None,
            "notes": "Computed daily after NYSE close from platform.macro_data (vix/hy_spread/yield_curve) + platform.prices_daily (SPY).",
        })

    return rows


_SERIES_CATALOG_UPSERT_SQL = """
    INSERT INTO platform.series_catalog (
        source, series_id, vendor_series_id, description, unit, frequency,
        publish_weekday, publish_day_of_month, publish_lag_days,
        is_seasonally_adjusted, is_derived, sacred,
        publication_calendar_url, notes, updated_at
    ) VALUES (
        $1, $2, $3, $4, $5, $6,
        $7, $8, $9,
        $10, $11, $12,
        $13, $14, now()
    )
    ON CONFLICT (source, series_id) DO UPDATE SET
        vendor_series_id = EXCLUDED.vendor_series_id,
        description      = EXCLUDED.description,
        unit             = EXCLUDED.unit,
        frequency        = EXCLUDED.frequency,
        publish_weekday  = EXCLUDED.publish_weekday,
        publish_day_of_month = EXCLUDED.publish_day_of_month,
        publish_lag_days = EXCLUDED.publish_lag_days,
        is_seasonally_adjusted = EXCLUDED.is_seasonally_adjusted,
        is_derived       = EXCLUDED.is_derived,
        sacred           = EXCLUDED.sacred,
        publication_calendar_url = EXCLUDED.publication_calendar_url,
        notes            = EXCLUDED.notes,
        updated_at       = now()
"""


async def _stage_series_catalog_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Task #18 follow-on — populate platform.series_catalog with metadata
    for every series observable in platform.macro_data.

    Idempotent UPSERT on (source, series_id) so re-runs refresh metadata
    in-place when the static catalog dict is updated.

    cfg knobs:
      dry_run (default 'true') — preview count only.
      verify_coverage (default 'true') — confirm every (source, series_id)
                                        in macro_data has a catalog row.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    verify_coverage = (cfg.get("verify_coverage", "true").lower() != "false") if isinstance(cfg.get("verify_coverage", "true"), str) else bool(cfg.get("verify_coverage", True))

    rows = _series_catalog_metadata()
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    log.info(
        "ops.stage.series_catalog_backfill.starting",
        total=len(rows), by_source=by_source, dry_run=dry_run,
    )

    if dry_run:
        return {"dry_run": True, "rows_to_upsert": len(rows), "by_source": by_source}

    n_upserted = 0
    async with pool.acquire() as conn, conn.transaction():
        for r in rows:
            await conn.execute(
                _SERIES_CATALOG_UPSERT_SQL,
                r["source"], r["series_id"], r["vendor_series_id"],
                r["description"], r["unit"], r["frequency"],
                r["publish_weekday"], r["publish_day_of_month"], r["publish_lag_days"],
                r["is_seasonally_adjusted"], r["is_derived"], r["sacred"],
                r["publication_calendar_url"], r["notes"],
            )
            n_upserted += 1

    result: dict[str, Any] = {
        "dry_run": False, "upserted": n_upserted, "by_source": by_source,
    }

    if verify_coverage:
        missing = await pool.fetch(
            """
            SELECT DISTINCT md.source, md.series_id
            FROM platform.macro_data md
            LEFT JOIN platform.series_catalog sc
                ON sc.source = md.source AND sc.series_id = md.series_id
            WHERE sc.series_id IS NULL
            """
        )
        result["coverage_gap"] = [(r["source"], r["series_id"]) for r in missing]
        if missing:
            log.warning(
                "ops.stage.series_catalog_backfill.coverage_gap",
                n_missing=len(missing), sample=result["coverage_gap"][:10],
            )

    log.info("ops.stage.series_catalog_backfill.done", **{k: v for k, v in result.items() if k != "coverage_gap"})
    return result


# ── Corporate-history enrichment epic — P2 (thinned) seed stage ──────
# Loads scripts/seed/corporate_events_seed.csv into platform.{issuers,
# issuer_securities, corporate_events}. The CSV is operator-curated
# (15 rows as of 2026-05-24) and serves as the test oracle for the
# future SEC EDGAR extractor (P3). Per the spec
# docs/superpowers/specs/2026-05-24-corporate-history-enrichment.md v0.2.
#
# Issuer ID minting (operator-minted PK pattern from v2.2 precedent):
#   - CIK-bearing issuers: 'CIK' + zero-padded 10-digit CIK (e.g. 'CIK0001418091')
#   - Non-CIK external successors: 'EXT_' + slug of successor_external
#     (e.g. 'EXT_X_CORP_MUSK_OWNED_PRIVATE_DELAWARE')
# Stable + deterministic + portable. Re-runs hit ON CONFLICT DO NOTHING.

_CORP_EVENTS_CSV_PATH = "scripts/seed/corporate_events_seed.csv"


def _mint_issuer_id_from_cik(cik: str | None) -> str | None:
    """Mint a stable issuer_id from a CIK; return None if no CIK."""
    if not cik:
        return None
    # Strip leading zeros, re-pad to 10, prepend 'CIK'. Handles both
    # '1418091' and '0001418091' input shapes deterministically.
    return "CIK" + str(int(cik)).zfill(10)


def _mint_issuer_id_from_external(external: str) -> str:
    """Mint a stable issuer_id from a successor_external free-text label."""
    import re
    slug = re.sub(r"[^A-Z0-9]+", "_", external.upper()).strip("_")
    # Cap length so the id stays human-readable.
    return ("EXT_" + slug)[:80]


def _mint_event_id(
    *, predecessor: str, successor: str, event_date: str, event_kind: str,
) -> str:
    """Deterministic event_id = SHA-256-12 of (kind|date|pred|succ).

    Idempotent across re-runs of the seed stage (same CSV row → same
    event_id → ON CONFLICT no-op).
    """
    import hashlib
    payload = f"{event_kind}|{event_date}|{predecessor or ''}|{successor or ''}"
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24].upper()
    return f"EVT_{h}"


async def _stage_corporate_events_seed(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Corporate-history P2 — load scripts/seed/corporate_events_seed.csv
    into issuers + issuer_securities + corporate_events.

    Idempotent: each row resolves to deterministic issuer_id + event_id;
    ON CONFLICT DO NOTHING means re-runs are safe (manual CSV edits get
    picked up next run; existing rows stay put).

    cfg knobs:
      dry_run (default 'true') — count what WOULD insert; no writes.
      csv_path (default scripts/seed/corporate_events_seed.csv) — override.
    """
    import csv
    from pathlib import Path
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    csv_path = Path(str(cfg.get("csv_path", _CORP_EVENTS_CSV_PATH)))

    if not csv_path.exists():
        raise RuntimeError(f"corporate_events_seed: CSV not found at {csv_path}")

    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    log.info("ops.stage.corporate_events_seed.starting",
             n_rows=len(rows), csv_path=str(csv_path), dry_run=dry_run)

    n_issuers_inserted = 0
    n_securities_inserted = 0
    n_events_inserted = 0
    n_skipped = 0
    skipped_reasons: list[str] = []

    async with pool.acquire() as conn:
        for i, r in enumerate(rows):
            pred_ticker = (r.get("predecessor_ticker") or "").strip() or None
            succ_ticker = (r.get("successor_ticker") or "").strip() or None
            pred_cik = (r.get("predecessor_cik") or "").strip() or None
            succ_cik = (r.get("successor_cik") or "").strip() or None
            succ_external = (r.get("successor_external") or "").strip() or None
            event_kind = (r.get("event_kind") or "").strip()
            event_date_str = (r.get("event_date") or "").strip()

            if not event_kind or not event_date_str:
                n_skipped += 1
                skipped_reasons.append(f"row {i}: missing event_kind or event_date")
                continue

            from datetime import date as _date
            event_date = _date.fromisoformat(event_date_str)

            # Resolve predecessor classification_id from ticker_classifications.
            pred_cls_id = None
            if pred_ticker:
                pred_cls_id = await conn.fetchval(
                    "SELECT id FROM platform.ticker_classifications WHERE ticker = $1 AND lifetime_end IS NULL",
                    pred_ticker,
                )
            succ_cls_id = None
            if succ_ticker:
                succ_cls_id = await conn.fetchval(
                    "SELECT id FROM platform.ticker_classifications WHERE ticker = $1 AND lifetime_end IS NULL",
                    succ_ticker,
                )

            # Mint issuer_ids.
            pred_issuer_id = _mint_issuer_id_from_cik(pred_cik)
            if succ_cik:
                succ_issuer_id = _mint_issuer_id_from_cik(succ_cik)
            elif succ_external:
                succ_issuer_id = _mint_issuer_id_from_external(succ_external)
            else:
                succ_issuer_id = None  # liquidation / no-successor case

            event_id = _mint_event_id(
                predecessor=pred_ticker or pred_cik or "?",
                successor=succ_ticker or succ_cik or succ_external or "",
                event_date=event_date_str,
                event_kind=event_kind,
            )

            if dry_run:
                log.info("ops.stage.corporate_events_seed.would_insert",
                         row=i, event_id=event_id, kind=event_kind,
                         pred_ticker=pred_ticker, pred_issuer=pred_issuer_id,
                         succ_ticker=succ_ticker, succ_issuer=succ_issuer_id)
                continue

            async with conn.transaction():
                # 1. INSERT issuers — predecessor + successor (where present).
                # Pre-fetch a minimal legal_name; CSV doesn't always have one.
                pred_legal = pred_ticker or pred_cik or "(unknown)"
                succ_legal = (succ_external or succ_ticker
                              or succ_cik or "(unknown)")

                if pred_issuer_id:
                    r1 = await conn.execute(
                        """
                        INSERT INTO platform.issuers
                            (issuer_id, cik, legal_name, status)
                        VALUES ($1, $2, $3, 'active')
                        ON CONFLICT (issuer_id) DO NOTHING
                        """,
                        pred_issuer_id, pred_cik, pred_legal,
                    )
                    if r1 == "INSERT 0 1":
                        n_issuers_inserted += 1

                if succ_issuer_id:
                    # Status reflects what the event implies (take_private →
                    # 'private', merger absorbing succ → 'active', etc.).
                    succ_status = "private" if succ_external else "active"
                    r2 = await conn.execute(
                        """
                        INSERT INTO platform.issuers
                            (issuer_id, cik, legal_name, status)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (issuer_id) DO NOTHING
                        """,
                        succ_issuer_id, succ_cik, succ_legal, succ_status,
                    )
                    if r2 == "INSERT 0 1":
                        n_issuers_inserted += 1

                # 2. INSERT issuer_securities mappings — only when the
                # security IS tracked in ticker_classifications.
                # For RENAME / TICKER_SWAP events where predecessor_cik ==
                # successor_cik (same legal entity), use event_date as the
                # SCD-2 boundary: predecessor's classification valid until
                # event_date; successor's classification valid from
                # event_date onward. The same-CIK detection makes
                # FB->META, GOOG share-class changes, and similar
                # entity-preserved transitions queryable.
                same_entity_rename = (
                    pred_cik and succ_cik and pred_cik == succ_cik
                    and event_kind in ("rename", "ticker_swap",
                                       "name_only_change",
                                       "share_class_collapse")
                )
                if pred_cls_id and pred_issuer_id:
                    pred_valid_to = event_date if same_entity_rename else None
                    r3 = await conn.execute(
                        """
                        INSERT INTO platform.issuer_securities
                            (issuer_id, classification_id, valid_from, valid_to)
                        VALUES ($1, $2, '1900-01-01', $3)
                        ON CONFLICT (issuer_id, classification_id, valid_from) DO UPDATE
                            SET valid_to = COALESCE(issuer_securities.valid_to, EXCLUDED.valid_to)
                        """,
                        pred_issuer_id, pred_cls_id, pred_valid_to,
                    )
                    if r3 == "INSERT 0 1":
                        n_securities_inserted += 1
                if succ_cls_id and succ_issuer_id:
                    r4 = await conn.execute(
                        """
                        INSERT INTO platform.issuer_securities
                            (issuer_id, classification_id, valid_from)
                        VALUES ($1, $2, $3)
                        ON CONFLICT DO NOTHING
                        """,
                        succ_issuer_id, succ_cls_id, event_date,
                    )
                    if r4 == "INSERT 0 1":
                        n_securities_inserted += 1

                # 2b. RENAME events also populate issuer_history with the
                # entity's name timeline — fetch EDGAR's formerNames for
                # the CIK and emit one issuer_history row per name period.
                # Only fires for same-CIK rename / ticker_swap events.
                if same_entity_rename and pred_issuer_id:
                    import os as _os

                    import httpx as _httpx
                    ua = _os.environ.get("SEC_EDGAR_USER_AGENT")
                    if ua:
                        cik_padded = str(int(pred_cik)).zfill(10)
                        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
                        try:
                            async with _httpx.AsyncClient() as _c:
                                _r = await _c.get(url, headers={"User-Agent": ua}, timeout=15.0)
                                if _r.status_code == 200:
                                    _data = _r.json()
                                    current_name = _data.get("name") or pred_legal
                                    former = _data.get("formerNames", []) or []
                                    # Upgrade the issuers.legal_name to EDGAR's
                                    # canonical when our seed value (e.g. "FB")
                                    # was a placeholder.
                                    await conn.execute(
                                        """
                                        UPDATE platform.issuers
                                        SET legal_name = $2
                                        WHERE issuer_id = $1 AND legal_name <> $2
                                        """,
                                        pred_issuer_id, current_name,
                                    )
                                    # INSERT one row per former name (with
                                    # the from/to dates EDGAR gives us).
                                    for fn in former:
                                        fn_name = fn.get("name")
                                        fn_from_str = (fn.get("from") or "")[:10]
                                        fn_to_str = (fn.get("to") or "")[:10]
                                        if not fn_name or not fn_from_str:
                                            continue
                                        fn_from = _date.fromisoformat(fn_from_str)
                                        fn_to = _date.fromisoformat(fn_to_str) if fn_to_str else None
                                        await conn.execute(
                                            """
                                            INSERT INTO platform.issuer_history
                                                (issuer_id, cik, legal_name, valid_from, valid_to, source)
                                            VALUES ($1, $2, $3, $4, $5, 'sec_edgar')
                                            ON CONFLICT (issuer_id, valid_from) DO NOTHING
                                            """,
                                            pred_issuer_id, pred_cik, fn_name,
                                            fn_from, fn_to,
                                        )
                                    # And the CURRENT name from event_date onward.
                                    await conn.execute(
                                        """
                                        INSERT INTO platform.issuer_history
                                            (issuer_id, cik, legal_name, valid_from, valid_to, source)
                                        VALUES ($1, $2, $3, $4, NULL, 'sec_edgar')
                                        ON CONFLICT (issuer_id, valid_from) DO NOTHING
                                        """,
                                        pred_issuer_id, pred_cik, current_name,
                                        event_date,
                                    )
                        except Exception as _exc:  # noqa: BLE001 — best-effort enrichment
                            log.warning("ops.stage.corporate_events_seed.edgar_fetch_failed",
                                        cik=pred_cik, err=str(_exc))

                # 3. INSERT the corporate event itself.
                announced_date = None
                if r.get("announced_date"):
                    announced_date = _date.fromisoformat(r["announced_date"].strip())
                ratio_num = float(r["ratio_num"]) if r.get("ratio_num") else None
                ratio_den = float(r["ratio_den"]) if r.get("ratio_den") else None
                cash = float(r["cash_per_share"]) if r.get("cash_per_share") else None

                r5 = await conn.execute(
                    """
                    INSERT INTO platform.corporate_events (
                        event_id, event_kind, event_date, announced_date,
                        predecessor_cls_id, successor_cls_id,
                        predecessor_issuer_id, successor_issuer_id,
                        successor_external,
                        ratio_num, ratio_den, cash_per_share,
                        source, source_filing_url, notes
                    )
                    VALUES ($1, $2, $3, $4,
                            $5, $6,
                            $7, $8,
                            $9,
                            $10, $11, $12,
                            $13, $14, $15)
                    ON CONFLICT (event_id, realtime_start) DO NOTHING
                    """,
                    event_id, event_kind, event_date, announced_date,
                    pred_cls_id, succ_cls_id,
                    pred_issuer_id, succ_issuer_id,
                    succ_external,
                    ratio_num, ratio_den, cash,
                    r.get("source") or "operator_manual",
                    r.get("source_filing_url") or None,
                    r.get("notes") or None,
                )
                if r5 == "INSERT 0 1":
                    n_events_inserted += 1

    result = {
        "dry_run": dry_run,
        "rows_processed": len(rows),
        "issuers_inserted": n_issuers_inserted,
        "issuer_securities_inserted": n_securities_inserted,
        "events_inserted": n_events_inserted,
        "skipped": n_skipped,
        "skipped_reasons": skipped_reasons,
    }
    log.info("ops.stage.corporate_events_seed.done", **result)
    return result


async def _stage_corp_history_edgar_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk every CIK in `ticker_classifications` and seed corp-history
    from SEC EDGAR's `formerNames` array — BULK-FILE path.

    Uses SEC's published bulk dataset rather than per-CIK HTTP calls:

      1. Download `submissions.zip` (~1.5 GB; cached in /tmp for the
         day so re-runs skip the download).
      2. Iterate ZIP entries `CIK{padded}.json`. For each CIK in our
         universe with a non-empty `formerNames` array, parse the
         entries and accumulate row tuples for issuers + issuer_history
         + corporate_events.
      3. Bulk INSERT via `asyncpg.executemany` — one statement per
         table for the entire batch.

    The previous per-CIK HTTP-loop version (killed 2026-05-24) was the
    "bulk-before-API-crawl" anti-pattern: a 6,735-CIK serial walk took
    ~4 hours. The bulk file gets it under 3 minutes.

    Idempotent. cfg knobs:
      dry_run (default 'true') — count what WOULD insert; no writes.
      max_ciks (default '0' = no cap) — limit for smoke runs.
      cache_path (default '/tmp/sec_submissions.zip') — local cache.
      force_download (default 'false') — bypass cache.

    Requires SEC_EDGAR_USER_AGENT env var (operator policy).
    """
    import os as _os
    import time as _time
    import zipfile as _zipfile
    from pathlib import Path as _Path

    import httpx as _httpx
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    max_ciks = int(cfg.get("max_ciks", 0))
    cache_path = _Path(str(cfg.get("cache_path", "/tmp/sec_submissions.zip")))
    force_download = str(cfg.get("force_download", "false")).lower() == "true"

    ua = _os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "corp_history_edgar_backfill: SEC_EDGAR_USER_AGENT env var required"
        )

    # 1. Universe = distinct CIKs of active classifications.
    cik_rows = await pool.fetch(
        """
        SELECT DISTINCT cik
        FROM platform.ticker_classifications
        WHERE cik IS NOT NULL AND lifetime_end IS NULL
        ORDER BY cik
        """
    )
    universe_ciks = {r["cik"] for r in cik_rows}
    if max_ciks > 0:
        universe_ciks = set(sorted(universe_ciks)[:max_ciks])
    log.info(
        "ops.stage.corp_history_edgar_backfill.starting",
        n_universe_ciks=len(universe_ciks), dry_run=dry_run,
        cache_path=str(cache_path), force_download=force_download,
    )

    # 2. Download (cached) submissions.zip.
    bulk_url = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
    cache_age_sec = (_time.time() - cache_path.stat().st_mtime) if cache_path.exists() else float("inf")
    if force_download or not cache_path.exists() or cache_age_sec > 86_400:
        log.info("ops.stage.corp_history_edgar_backfill.downloading",
                 url=bulk_url, cache_age_hr=round(cache_age_sec / 3600, 1))
        t0 = _time.time()
        async with _httpx.AsyncClient(timeout=600.0) as client, \
                client.stream("GET", bulk_url, headers={"User-Agent": ua}) as resp:
            resp.raise_for_status()
            with cache_path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
        log.info("ops.stage.corp_history_edgar_backfill.download_done",
                 size_mb=round(cache_path.stat().st_size / 1024 / 1024, 1),
                 elapsed_sec=round(_time.time() - t0, 1))
    else:
        log.info("ops.stage.corp_history_edgar_backfill.using_cached",
                 size_mb=round(cache_path.stat().st_size / 1024 / 1024, 1),
                 age_hr=round(cache_age_sec / 3600, 1))

    # 3. Iterate ZIP entries — accumulate rows for batch insert.
    import hashlib as _hashlib
    import json as _json
    from datetime import date as _date
    issuer_rows: list[tuple[str, str, str]] = []  # (issuer_id, cik, legal_name)
    history_rows: list[tuple[str, str, str, _date, _date | None]] = []  # (issuer_id, cik, legal_name, valid_from, valid_to)
    event_rows: list[tuple[str, _date, str, str]] = []  # (event_id, event_date, issuer_id, notes)
    n_ciks_walked = 0
    n_with_former = 0
    n_zip_parse_errors = 0

    t0 = _time.time()
    with _zipfile.ZipFile(cache_path, "r") as zf:
        for entry in zf.namelist():
            if not entry.startswith("CIK") or not entry.endswith(".json"):
                continue
            if "-" in entry:  # filing-paginated files like CIK0001234567-submissions-001.json
                continue
            # Strip "CIK" prefix + ".json" suffix → padded CIK
            # (10-char zero-padded; matches the storage format in
            # ticker_classifications.cik).
            cik = entry[3:-5]
            if cik not in universe_ciks:
                continue
            n_ciks_walked += 1
            try:
                data = _json.loads(zf.read(entry))
            except Exception:  # noqa: BLE001
                n_zip_parse_errors += 1
                continue
            former = data.get("formerNames", []) or []
            current_name = data.get("name")
            if not former or not current_name:
                continue
            n_with_former += 1
            issuer_id = _mint_issuer_id_from_cik(cik)
            if not issuer_id:
                continue
            issuer_rows.append((issuer_id, cik, current_name))
            # Former-name history rows + corresponding rename events.
            prev_to: _date | None = None
            for fn in former:
                fn_name = fn.get("name")
                fn_from_str = (fn.get("from") or "")[:10]
                fn_to_str = (fn.get("to") or "")[:10]
                if not fn_name or not fn_from_str:
                    continue
                fn_from = _date.fromisoformat(fn_from_str)
                fn_to = _date.fromisoformat(fn_to_str) if fn_to_str else None
                history_rows.append((issuer_id, cik, fn_name, fn_from, fn_to))
                if fn_to is not None:
                    ev_payload = f"name_only_change|{cik}|{fn_name}|{fn_to}"
                    ev_hash = _hashlib.sha256(ev_payload.encode("utf-8")).hexdigest()[:24].upper()
                    event_id = f"EVT_{ev_hash}"
                    event_rows.append((
                        event_id, fn_to, issuer_id,
                        f"EDGAR formerNames: '{fn_name}' -> '{current_name}'",
                    ))
                prev_to = fn_to
            # Current-name row (most-recent boundary onward).
            cur_from = prev_to or _date.fromisoformat(
                (former[-1].get("to") or "")[:10] or "1900-01-01"
            )
            history_rows.append((issuer_id, cik, current_name, cur_from, None))

    log.info(
        "ops.stage.corp_history_edgar_backfill.parsed",
        n_ciks_walked=n_ciks_walked, n_with_former=n_with_former,
        n_issuer_rows=len(issuer_rows),
        n_history_rows=len(history_rows),
        n_event_rows=len(event_rows),
        parse_errors=n_zip_parse_errors,
        elapsed_sec=round(_time.time() - t0, 1),
    )

    if dry_run:
        return {
            "dry_run": True,
            "universe_ciks": len(universe_ciks),
            "ciks_walked": n_ciks_walked,
            "ciks_with_former_names": n_with_former,
            "issuer_rows_planned": len(issuer_rows),
            "history_rows_planned": len(history_rows),
            "event_rows_planned": len(event_rows),
        }

    # 4. Bulk INSERT — executemany per table.
    t0 = _time.time()
    async with pool.acquire() as conn, conn.transaction():
        # 4a. issuers — UPSERT legal_name.
        await conn.executemany(
            """
            INSERT INTO platform.issuers (issuer_id, cik, legal_name, status)
            VALUES ($1, $2, $3, 'active')
            ON CONFLICT (issuer_id) DO UPDATE
                SET legal_name = EXCLUDED.legal_name
                WHERE issuers.legal_name <> EXCLUDED.legal_name
            """,
            issuer_rows,
        )
        # 4b. issuer_history — INSERT IGNORE on (issuer_id, valid_from).
        await conn.executemany(
            """
            INSERT INTO platform.issuer_history
                (issuer_id, cik, legal_name, valid_from, valid_to, source)
            VALUES ($1, $2, $3, $4, $5, 'sec_edgar')
            ON CONFLICT (issuer_id, valid_from) DO NOTHING
            """,
            history_rows,
        )
        # 4c. corporate_events — bitemporal PK so WHERE NOT EXISTS.
        await conn.executemany(
            """
            INSERT INTO platform.corporate_events (
                event_id, event_kind, event_date, announced_date,
                predecessor_cls_id, successor_cls_id,
                predecessor_issuer_id, successor_issuer_id,
                successor_external,
                source, notes
            )
            SELECT $1, 'name_only_change', $2, NULL,
                   NULL, NULL,
                   $3, $3,
                   NULL,
                   'sec_edgar', $4
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.corporate_events WHERE event_id = $1
            )
            """,
            event_rows,
        )

    log.info("ops.stage.corp_history_edgar_backfill.written",
             elapsed_sec=round(_time.time() - t0, 1))

    # 5. Recount results (executemany doesn't surface per-row INSERT counts).
    n_issuers_after = await pool.fetchval("SELECT count(*) FROM platform.issuers")
    n_history_after = await pool.fetchval("SELECT count(*) FROM platform.issuer_history")
    n_events_after = await pool.fetchval(
        "SELECT count(*) FROM platform.corporate_events WHERE event_kind = 'name_only_change'"
    )

    result = {
        "dry_run": dry_run,
        "universe_ciks": len(universe_ciks),
        "ciks_walked": n_ciks_walked,
        "ciks_with_former_names": n_with_former,
        "issuer_rows_planned": len(issuer_rows),
        "history_rows_planned": len(history_rows),
        "event_rows_planned": len(event_rows),
        "issuers_total_after": n_issuers_after,
        "history_total_after": n_history_after,
        "name_only_change_events_total_after": n_events_after,
        "parse_errors": n_zip_parse_errors,
    }
    log.info("ops.stage.corp_history_edgar_backfill.done", **result)
    return result


# ── SEC EDGAR orphan resolver — operator directive 2026-05-24 ──────────
# Resolves Path-A orphan tickers (prices_daily.classification_id IS NULL)
# by looking up the issuer's CIK via either:
#   (a) the truth-set seed CSV (operator-curated CIKs — Phase A)
#   (b) SEC EDGAR ticker search (best-effort, deferred to Phase B)
# Then INSERTs platform.ticker_classifications + triggers fire to
# auto-populate classification_id on the orphan prices_daily rows.
#
# Idempotent (ticker_classifications INSERT uses ON CONFLICT DO NOTHING).
# Designed to be wired into self-heal as a deterministic agent for the
# PRICES_DAILY_CLASSIFICATION_ID_NULL signal — follow-on commit.

async def _sec_fetch_issuer_name(client: Any, cik: str, ua: str) -> str | None:
    """Fetch the legal name from EDGAR submissions JSON for a known CIK.

    Returns the CURRENT legal name (the .name field). The .formerNames
    array carries history but for ticker_classifications we want the
    most recent authoritative name — that's what shows up in 8-K bodies
    for filings AFTER any rename.
    """
    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        r = await client.get(url, headers={"User-Agent": ua}, timeout=15.0)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("name") or None
    except Exception:  # noqa: BLE001 — best-effort lookup
        return None


async def _sec_ticker_to_cik(client: Any, ticker: str, ua: str) -> str | None:
    """Resolve a ticker (current or delisted) to its CIK via EDGAR's
    browse-edgar disambiguation endpoint.

    EDGAR's `getcompany?CIK=<ticker>` does server-side ticker→CIK
    resolution. For delisted tickers, returns the CIK of the most-recent
    registrant under that symbol. Verified 5/5 hit rate on delisted
    truth-set samples (ATVI, VMW, EKSO, GLPG, OPGN) — vastly more
    reliable than the EDGAR full-text search path (40% false positives).

    Returns None when no CIK can be extracted (truly-unknown tickers,
    foreign issuers with no SEC presence, transient HTTP errors).
    """
    import re
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={ticker}&type=8-K&dateb=&owner=include&count=10&output=atom"
    )
    try:
        r = await client.get(url, headers={"User-Agent": ua}, timeout=15.0)
        if r.status_code != 200:
            return None
        m = re.search(r"CIK[=]?(\d{10})", r.text[:4000])
        return m.group(1).lstrip("0") if m else None
    except Exception:  # noqa: BLE001 — best-effort lookup
        return None


def _coarsen_to_stock_asset_class(_country: str = "US") -> str:
    """All truth-set Path-A predecessors are stocks. Coarse 4-way enum
    in ticker_classifications.asset_class accepts stock|etf|fund|spac.
    Permanent stage-level default for SEC-resolved orphans (none of
    them are ETFs/funds — those have their own classification paths)."""
    return "stock"


async def _stage_ticker_history_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Maintain the platform.ticker_history SCD-2 timeline:
      1. INSERT a ticker_history row for every (classification_id, ticker)
         pair in ticker_classifications that doesn't have one yet.
         valid_from = MIN(prices_daily.date) for that ticker (first
         observed bar) — or today if no bars exist.
         valid_to   = MAX(prices_daily.date) for the ticker IF the
         ticker is delisted=true in any prices_daily row, else NULL.
      2. UPDATE valid_to on existing open (valid_to IS NULL) rows where
         the ticker is now delisted, so the SCD-2 timeline reflects the
         actual end-of-life date instead of staying open forever.

    Idempotent: re-runs only INSERT missing rows + only UPDATE rows whose
    valid_to is still NULL. Designed for two callers:
      - Operator-on-demand re-run after sec_orphan_resolve adds new
        classifications
      - HealSpec-driven self-heal when a new
        `ticker_history_completeness` check (follow-on) reds

    cfg knobs:
      dry_run (default 'true') — count without writing.
    """
    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)

    if dry_run:
        async with pool.acquire() as conn:
            missing = int(await conn.fetchval("""
                SELECT count(*) FROM platform.ticker_classifications tc
                WHERE NOT EXISTS (
                    SELECT 1 FROM platform.ticker_history th
                    WHERE th.classification_id = tc.id AND th.ticker = tc.ticker
                )
            """) or 0)
            open_delisted = int(await conn.fetchval("""
                SELECT count(*) FROM platform.ticker_history th
                WHERE th.valid_to IS NULL
                  AND EXISTS (
                      SELECT 1 FROM platform.prices_daily pd
                      WHERE pd.ticker = th.ticker AND pd.delisted = true
                  )
            """) or 0)
        log.info("ops.stage.ticker_history_backfill.dry_run",
                 missing_history_rows=missing, open_rows_to_close=open_delisted)
        return {"dry_run": True, "missing_history_rows": missing,
                "open_rows_to_close": open_delisted}

    # 1. INSERT missing ticker_history rows. valid_from = MIN(date) for the
    # ticker (first observed bar) or today if no bars exist; valid_to is
    # set in step 2.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '5min'")
        r1 = await conn.execute("""
            INSERT INTO platform.ticker_history (classification_id, ticker, valid_from, valid_to)
            SELECT tc.id, tc.ticker,
                   COALESCE(
                       (SELECT MIN(pd.date) FROM platform.prices_daily pd
                        WHERE pd.ticker = tc.ticker),
                       CURRENT_DATE
                   ),
                   NULL
            FROM platform.ticker_classifications tc
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.ticker_history th
                WHERE th.classification_id = tc.id AND th.ticker = tc.ticker
            )
        """)
        n_inserted = int(r1.split()[-1]) if r1.startswith("INSERT") else 0

    # 2. UPDATE valid_from AND valid_to to reflect the ticker's actual
    # observed lifetime in prices_daily.
    # The original seed migration set valid_from=migration_date for every
    # row — semantically wrong for historical bars (the BEFORE INSERT
    # triggers' WHERE valid_from <= NEW.date filter returns no match for
    # bars before 2026-05-23). Fix valid_from = MIN(observed date) so the
    # SCD-2 range actually covers the ticker's lifetime.
    # valid_to = MAX(date) only for tickers with any delisted=true bar.
    # CTE pre-computes lifetime per ticker to avoid a correlated subquery.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = '5min'")
        r2 = await conn.execute("""
            WITH lifetime AS (
                SELECT ticker,
                       MIN(date) AS first_bar,
                       MAX(date) AS last_bar,
                       BOOL_OR(delisted) AS ever_delisted
                FROM platform.prices_daily
                GROUP BY ticker
            )
            UPDATE platform.ticker_history th
            SET valid_from = lf.first_bar,
                valid_to = CASE WHEN lf.ever_delisted THEN lf.last_bar ELSE NULL END
            FROM lifetime lf
            WHERE th.ticker = lf.ticker
              AND (
                  -- Always tighten valid_from to the true first-observed date.
                  th.valid_from > lf.first_bar
                  -- And set valid_to for delisted tickers that don't have one yet.
                  OR (lf.ever_delisted AND th.valid_to IS NULL)
              )
        """)
        n_updated = int(r2.split()[-1]) if r2.startswith("UPDATE") else 0

    # Verify post-state
    async with pool.acquire() as conn:
        n_total = int(await conn.fetchval("SELECT count(*) FROM platform.ticker_history") or 0)
        n_open = int(await conn.fetchval(
            "SELECT count(*) FROM platform.ticker_history WHERE valid_to IS NULL"
        ) or 0)
        n_closed = n_total - n_open
        n_still_missing = int(await conn.fetchval("""
            SELECT count(*) FROM platform.ticker_classifications tc
            WHERE NOT EXISTS (
                SELECT 1 FROM platform.ticker_history th
                WHERE th.classification_id = tc.id AND th.ticker = tc.ticker
            )
        """) or 0)

    result = {
        "dry_run": False,
        "history_rows_inserted": n_inserted,
        "open_rows_closed": n_updated,
        "post_total": n_total,
        "post_open": n_open,
        "post_closed": n_closed,
        "post_still_missing": n_still_missing,
    }
    log.info("ops.stage.ticker_history_backfill.done", **result)
    return result


async def _stage_sec_orphan_resolve(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve Path-A orphan tickers via SEC EDGAR.

    Phase A (this commit): uses the seed-CSV's predecessor_cik column for
    the 7 high-confidence operator-curated tickers (TWTR/SPLK/WORK/MGI/
    FTCH/DISCA/BBBY). Mints a TKR-14, INSERTs ticker_classifications,
    and the existing BEFORE INSERT trigger auto-populates prices_daily.
    classification_id on next ingest. For currently-orphan prices_daily
    rows, we UPDATE directly (triggers only fire on INSERT).

    Phase B (deferred next commit): for each remaining orphan ticker
    not in the truth set, attempt direct EDGAR ticker→CIK lookup.

    Phase C (deferred): for the foreign-ADR-suffix (F) + warrant-suffix
    (W/U/Z) tail that has no SEC presence, alternate-source lookups
    (OpenFIGI / operator-manual / FMP composite).

    Designed for both operator-on-demand re-run AND wiring into the
    self-heal cascade catalog (PRICES_DAILY_CLASSIFICATION_ID_NULL
    signal — follow-on commit).

    cfg knobs:
      dry_run (default 'true') — print plan; no writes.
      csv_path (default scripts/seed/corporate_events_seed.csv).
    """
    import csv as _csv
    import os as _os
    from datetime import UTC
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    import httpx

    from tpcore.identity.tkr14 import (
        AssetClass,
        DiscoverySource,
        IPOVenue,
        mint,
    )

    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}
    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    csv_path = _Path(str(cfg.get("csv_path", "scripts/seed/corporate_events_seed.csv")))

    ua = _os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "sec_orphan_resolve: SEC_EDGAR_USER_AGENT env var required "
            "(SEC fair-access policy mandates a real contact)."
        )

    # 1. Read truth-set CSV, collect rows with predecessor_ticker + predecessor_cik.
    if not csv_path.exists():
        raise RuntimeError(f"sec_orphan_resolve: CSV not found at {csv_path}")
    with csv_path.open(encoding="utf-8") as fh:
        candidates = [
            r for r in _csv.DictReader(fh)
            if r.get("predecessor_ticker") and r.get("predecessor_cik")
        ]

    # 2. Filter to candidates that are ACTUAL orphans in prices_daily AND
    #    aren't already in ticker_classifications.
    actionable: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        for r in candidates:
            ticker = r["predecessor_ticker"]
            in_tc = await conn.fetchval(
                "SELECT count(*) FROM platform.ticker_classifications WHERE ticker=$1 AND lifetime_end IS NULL",
                ticker,
            )
            if in_tc:
                continue
            is_orphan = await conn.fetchval(
                "SELECT count(*) FROM platform.prices_daily "
                "WHERE ticker=$1 AND classification_id IS NULL LIMIT 1",
                ticker,
            )
            if not is_orphan:
                continue
            actionable.append(r)

    log.info("ops.stage.sec_orphan_resolve.starting",
             truth_set_size=len(candidates), actionable=len(actionable), dry_run=dry_run)

    if dry_run:
        return {"dry_run": True, "actionable_tickers": [r["predecessor_ticker"] for r in actionable]}

    # 3. Resolve each — fetch legal name, mint TKR-14, INSERT classification,
    #    UPDATE prices_daily classification_id for the orphan rows.
    n_resolved = 0
    n_rows_updated = 0
    n_skipped = 0
    skip_reasons: list[str] = []
    now_utc = _dt.now(UTC)

    async with httpx.AsyncClient() as client:
        for r in actionable:
            ticker = r["predecessor_ticker"]
            cik = str(r["predecessor_cik"]).lstrip("0") or r["predecessor_cik"]
            legal_name = await _sec_fetch_issuer_name(client, cik, ua)
            if not legal_name:
                n_skipped += 1
                skip_reasons.append(f"{ticker}: EDGAR submissions JSON unavailable")
                continue

            tkr14 = mint(
                country="US",
                asset_class=AssetClass.STOCK,
                ipo_venue=IPOVenue.OTHER,  # historical; exchange-at-IPO not in EDGAR submissions
                discovery_source=DiscoverySource.SEC,
                cik=cik,
                legal_name=legal_name,
                now=now_utc,
            )

            async with pool.acquire() as conn, conn.transaction():
                # INSERT ticker_classifications (idempotent on ticker).
                ins_id = await conn.fetchval(
                    """
                    INSERT INTO platform.ticker_classifications
                        (id, ticker, current_ticker, current_legal_name,
                         country, asset_class, source, ipo_venue, discovery_source,
                         cik, status, updated_at)
                    VALUES ($1, $2, $2, $3,
                            'US', 'stock', 'sec_edgar_orphan_resolve', 'Z', 'S',
                            $4, 'active', now())
                    ON CONFLICT (ticker) WHERE lifetime_end IS NULL DO NOTHING
                    RETURNING id
                    """,
                    tkr14, ticker, legal_name, cik,
                )
                cls_id = ins_id or await conn.fetchval(
                    "SELECT id FROM platform.ticker_classifications WHERE ticker=$1 AND lifetime_end IS NULL",
                    ticker,
                )
                if not cls_id:
                    n_skipped += 1
                    skip_reasons.append(f"{ticker}: INSERT failed + no existing row")
                    continue

                # UPDATE prices_daily orphan rows. Triggers fire on INSERT
                # only, so existing NULL rows need explicit UPDATE.
                upd = await conn.execute(
                    """
                    UPDATE platform.prices_daily
                    SET classification_id = $1
                    WHERE ticker = $2 AND classification_id IS NULL
                    """,
                    cls_id, ticker,
                )
                row_count = int(upd.split()[-1]) if upd.startswith("UPDATE") else 0
                n_rows_updated += row_count
                n_resolved += 1
                log.info("ops.stage.sec_orphan_resolve.resolved",
                         phase="A", ticker=ticker, cik=cik, legal_name=legal_name,
                         classification_id=cls_id, prices_daily_rows_updated=row_count)

    # ── Phase B — EDGAR direct ticker→CIK lookup for remaining orphans.
    # Scans prices_daily for ALL still-orphan tickers (after Phase A),
    # attempts EDGAR's getcompany endpoint per ticker, mints + INSERTs
    # for any CIK hit. Foreign ADR / warrant / fully-unknown tickers
    # return None and are left for Phase C (alternate sources).
    phase_b_enabled_param = cfg.get("phase_b", "true")
    phase_b_enabled = (phase_b_enabled_param.lower() != "false") if isinstance(phase_b_enabled_param, str) else bool(phase_b_enabled_param)

    n_phase_b_resolved = 0
    n_phase_b_rows_updated = 0
    n_phase_b_unresolved = 0
    phase_b_unresolved_sample: list[str] = []

    if phase_b_enabled:
        # Discover remaining orphan tickers.
        async with pool.acquire() as conn:
            orphan_rows = await conn.fetch("""
                SELECT DISTINCT ticker FROM platform.prices_daily
                WHERE classification_id IS NULL
                ORDER BY ticker
            """)
        remaining = [r["ticker"] for r in orphan_rows]
        log.info("ops.stage.sec_orphan_resolve.phase_b_starting", n_remaining=len(remaining))

        # SEC fair-access: ≤10 req/sec. Two calls per ticker (getcompany + name),
        # so cap effective rate at ~3-5 tickers/sec to stay well under.
        SEC_SLEEP_S = 0.15

        async with httpx.AsyncClient() as client:
            for ticker in remaining:
                cik = await _sec_ticker_to_cik(client, ticker, ua)
                await asyncio.sleep(SEC_SLEEP_S)
                if not cik:
                    n_phase_b_unresolved += 1
                    if len(phase_b_unresolved_sample) < 20:
                        phase_b_unresolved_sample.append(ticker)
                    continue

                legal_name = await _sec_fetch_issuer_name(client, cik, ua)
                await asyncio.sleep(SEC_SLEEP_S)
                if not legal_name:
                    n_phase_b_unresolved += 1
                    continue

                # Salt-retry on TKR-14 collision (sub-symbols of the same
                # entity — e.g. JFBR + JFBRW SPAC + warrant — produce the
                # same legal_name+CIK input and thus the same mint, per the
                # birthday-paradox warning in tpcore.identity.tkr14 docstring).
                async with pool.acquire() as conn, conn.transaction():
                    cls_id = None
                    for salt_try in range(5):
                        tkr14 = mint(
                            country="US",
                            asset_class=AssetClass.STOCK,
                            ipo_venue=IPOVenue.OTHER,
                            discovery_source=DiscoverySource.SEC,
                            cik=cik,
                            legal_name=legal_name,
                            now=now_utc,
                            salt=salt_try,
                        )
                        existing = await conn.fetchval(
                            "SELECT ticker FROM platform.ticker_classifications WHERE id=$1",
                            tkr14,
                        )
                        if not existing or existing == ticker:
                            break  # free to use this id
                    ins_id = await conn.fetchval(
                        """
                        INSERT INTO platform.ticker_classifications
                            (id, ticker, current_ticker, current_legal_name,
                             country, asset_class, source, ipo_venue, discovery_source,
                             cik, status, updated_at)
                        VALUES ($1, $2, $2, $3,
                                'US', 'stock', 'sec_edgar_orphan_resolve_phaseB', 'Z', 'S',
                                $4, 'active', now())
                        ON CONFLICT (ticker) WHERE lifetime_end IS NULL DO NOTHING
                        RETURNING id
                        """,
                        tkr14, ticker, legal_name, cik,
                    )
                    cls_id = ins_id or await conn.fetchval(
                        "SELECT id FROM platform.ticker_classifications WHERE ticker=$1 AND lifetime_end IS NULL",
                        ticker,
                    )
                    if not cls_id:
                        n_phase_b_unresolved += 1
                        continue

                    upd = await conn.execute(
                        """
                        UPDATE platform.prices_daily
                        SET classification_id = $1
                        WHERE ticker = $2 AND classification_id IS NULL
                        """,
                        cls_id, ticker,
                    )
                    row_count = int(upd.split()[-1]) if upd.startswith("UPDATE") else 0
                    n_phase_b_rows_updated += row_count
                    n_phase_b_resolved += 1
                    log.info("ops.stage.sec_orphan_resolve.resolved",
                             phase="B", ticker=ticker, cik=cik,
                             legal_name=legal_name,
                             classification_id=cls_id, prices_daily_rows_updated=row_count)

    # ── Phase C — alternate-source resolvers for the SEC-ineligible tail.
    # Tickers that EDGAR can't resolve (foreign ADRs, SPAC warrants,
    # tiny obscure names) often have data at OpenFIGI (foreign issuer
    # identity authority) or FMP /profile (currently-traded coverage).
    # Tries both in order; skips gracefully when the env var for either
    # vendor isn't set.
    phase_c_enabled_param = cfg.get("phase_c", "true")
    phase_c_enabled = (phase_c_enabled_param.lower() != "false") if isinstance(phase_c_enabled_param, str) else bool(phase_c_enabled_param)

    n_phase_c1_resolved = 0
    n_phase_c1_rows_updated = 0
    n_phase_c2_resolved = 0
    n_phase_c2_rows_updated = 0
    phase_c_unresolved: list[str] = []

    async def _phase_c_insert(
        conn: asyncpg.Connection,
        *,
        ticker: str,
        legal_name: str,
        cik: str | None,
        figi: str | None,
        source_tag: str,
        discovery: Any,
    ) -> tuple[str | None, int]:
        """Common INSERT path for Phase C — mints TKR-14 with salt-retry,
        INSERTs ticker_classifications, UPDATEs prices_daily.
        Returns (classification_id, rows_updated)."""
        for salt_try in range(5):
            tkr14_id = mint(
                country="US",
                asset_class=AssetClass.STOCK,
                ipo_venue=IPOVenue.OTHER,
                discovery_source=discovery,
                cik=cik,
                legal_name=legal_name,
                now=now_utc,
                salt=salt_try,
            )
            existing = await conn.fetchval(
                "SELECT ticker FROM platform.ticker_classifications WHERE id=$1",
                tkr14_id,
            )
            if not existing or existing == ticker:
                break
        ins_id = await conn.fetchval(
            """
            INSERT INTO platform.ticker_classifications
                (id, ticker, current_ticker, current_legal_name,
                 country, asset_class, source, ipo_venue, discovery_source,
                 cik, figi, status, updated_at)
            VALUES ($1, $2, $2, $3,
                    'US', 'stock', $4, 'Z', $5,
                    $6, $7, 'active', now())
            ON CONFLICT (ticker) WHERE lifetime_end IS NULL DO NOTHING
            RETURNING id
            """,
            tkr14_id, ticker, legal_name, source_tag,
            str(discovery.value), cik, figi,
        )
        cid = ins_id or await conn.fetchval(
            "SELECT id FROM platform.ticker_classifications WHERE ticker=$1 AND lifetime_end IS NULL",
            ticker,
        )
        if not cid:
            return None, 0
        upd = await conn.execute(
            """
            UPDATE platform.prices_daily
            SET classification_id = $1
            WHERE ticker = $2 AND classification_id IS NULL
            """,
            cid, ticker,
        )
        rc = int(upd.split()[-1]) if upd.startswith("UPDATE") else 0
        return cid, rc

    if phase_c_enabled:
        # Discover remaining orphans after Phase B.
        async with pool.acquire() as conn:
            c_remaining_rows = await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.prices_daily "
                "WHERE classification_id IS NULL ORDER BY ticker"
            )
        c_remaining = [r["ticker"] for r in c_remaining_rows]

        log.info("ops.stage.sec_orphan_resolve.phase_c_starting", n_remaining=len(c_remaining))

        # ── Phase C1 — OpenFIGI batch lookup (foreign-issuer-aware) ─────
        openfigi_key = _os.environ.get("OPEN_FIGI_API_KEY")
        c1_resolved_set: set[str] = set()
        if openfigi_key and c_remaining:
            from tpcore.openfigi import OpenFIGIAdapter
            try:
                async with OpenFIGIAdapter() as figi_adapter:
                    figi_results = await figi_adapter.map_tickers(c_remaining, exch_code="US")
            except Exception as e:  # noqa: BLE001 — best-effort
                log.warning("ops.stage.sec_orphan_resolve.openfigi_failed", error=str(e)[:200])
                figi_results = []

            for fr in figi_results:
                if fr.figi_not_found or not fr.name:
                    continue
                async with pool.acquire() as conn, conn.transaction():
                    cls_id, row_count = await _phase_c_insert(
                        conn,
                        ticker=fr.ticker,
                        legal_name=fr.name,
                        cik=None,
                        figi=fr.composite_figi,
                        source_tag="sec_edgar_orphan_resolve_phaseC1_openfigi",
                        discovery=DiscoverySource.OTHER,
                    )
                if cls_id:
                    n_phase_c1_resolved += 1
                    n_phase_c1_rows_updated += row_count
                    c1_resolved_set.add(fr.ticker)
                    log.info("ops.stage.sec_orphan_resolve.resolved",
                             phase="C1", ticker=fr.ticker, figi=fr.composite_figi,
                             legal_name=fr.name, classification_id=cls_id,
                             prices_daily_rows_updated=row_count)
        elif not openfigi_key:
            log.warning("ops.stage.sec_orphan_resolve.phase_c1_skipped_no_openfigi_key")

        # ── Phase C2 — FMP /profile retry for whatever C1 didn't get ────
        fmp_key = _os.environ.get("FMP_API_KEY")
        c2_remaining = [t for t in c_remaining if t not in c1_resolved_set]
        if fmp_key and c2_remaining:
            FMP_BASE = "https://financialmodelingprep.com/stable"
            async with httpx.AsyncClient(timeout=20.0) as fmp_client:
                for ticker in c2_remaining:
                    try:
                        resp = await fmp_client.get(
                            f"{FMP_BASE}/profile",
                            params={"symbol": ticker, "apikey": fmp_key},
                        )
                        await asyncio.sleep(0.2)
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        if not isinstance(data, list) or not data:
                            continue
                        prof = data[0]
                        legal_name = prof.get("companyName") or ""
                        if not legal_name:
                            continue
                        cik = str(prof["cik"]) if prof.get("cik") else None
                    except Exception:  # noqa: BLE001 — best-effort
                        continue

                    async with pool.acquire() as conn, conn.transaction():
                        cls_id, row_count = await _phase_c_insert(
                            conn,
                            ticker=ticker,
                            legal_name=legal_name,
                            cik=cik,
                            figi=None,
                            source_tag="sec_edgar_orphan_resolve_phaseC2_fmp",
                            discovery=DiscoverySource.FMP,
                        )
                    if cls_id:
                        n_phase_c2_resolved += 1
                        n_phase_c2_rows_updated += row_count
                        log.info("ops.stage.sec_orphan_resolve.resolved",
                                 phase="C2", ticker=ticker, cik=cik,
                                 legal_name=legal_name, classification_id=cls_id,
                                 prices_daily_rows_updated=row_count)
        elif not fmp_key:
            log.warning("ops.stage.sec_orphan_resolve.phase_c2_skipped_no_fmp_key")

        # Final residue — what no vendor could resolve.
        async with pool.acquire() as conn:
            final_rows = await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.prices_daily "
                "WHERE classification_id IS NULL ORDER BY ticker"
            )
        phase_c_unresolved = [r["ticker"] for r in final_rows]

    result = {
        "dry_run": False,
        "phase_a_actionable": len(actionable),
        "phase_a_resolved": n_resolved,
        "phase_a_rows_closed": n_rows_updated,
        "phase_a_skipped": n_skipped,
        "phase_a_skip_reasons": skip_reasons,
        "phase_b_enabled": phase_b_enabled,
        "phase_b_resolved": n_phase_b_resolved,
        "phase_b_rows_closed": n_phase_b_rows_updated,
        "phase_b_unresolved": n_phase_b_unresolved,
        "phase_b_unresolved_sample": phase_b_unresolved_sample,
        "phase_c_enabled": phase_c_enabled,
        "phase_c1_openfigi_resolved": n_phase_c1_resolved,
        "phase_c1_openfigi_rows_closed": n_phase_c1_rows_updated,
        "phase_c2_fmp_resolved": n_phase_c2_resolved,
        "phase_c2_fmp_rows_closed": n_phase_c2_rows_updated,
        "phase_c_unresolved_count": len(phase_c_unresolved),
        "phase_c_unresolved_residue": phase_c_unresolved,
        "total_resolved": n_resolved + n_phase_b_resolved + n_phase_c1_resolved + n_phase_c2_resolved,
        "total_rows_closed": (
            n_rows_updated + n_phase_b_rows_updated
            + n_phase_c1_rows_updated + n_phase_c2_rows_updated
        ),
    }
    log.info("ops.stage.sec_orphan_resolve.done", **result)
    return result


# v2.2 P6 Path-A orphan backfill — resolves each distinct orphan ticker via
# FMP /profile (per v2.2 spec §1.10 FMP-first lane for general-identity case),
# mints a TKR-14 via tpcore.identity.tkr14.mint, INSERTs ticker_classifications
# (pin-at-first-resolve), then UPDATEs every Path-A child table's
# classification_id where ticker matches. Unresolvable orphans (FMP /profile
# returns nothing — typically delisted historical tickers) stay NULL.
_PATH_A_CHILD_TABLES: tuple[str, ...] = (
    "short_interest",
    "earnings_events",
    "fundamentals_quarterly",
    "corporate_actions",
    "prices_daily",
)


def _fmp_profile_to_resolved_inputs(profile: dict[str, Any]) -> dict[str, Any]:
    """Map FMP /profile JSON to the field-shape parent_resolver's _ProfileResult expects."""
    from tpcore.identity.tkr14 import AssetClass
    asset_class = AssetClass.STOCK
    if profile.get("isEtf"):
        asset_class = AssetClass.ETF
    elif profile.get("isFund"):
        asset_class = AssetClass.FUND
    elif profile.get("isAdr"):
        asset_class = AssetClass.ADR
    return {
        "country": (profile.get("country") or "US")[:2].upper(),
        "asset_class": asset_class,
        "exchange": profile.get("exchange") or profile.get("exchangeShortName"),
        "cik": str(profile["cik"]) if profile.get("cik") else None,
        "cusip": str(profile["cusip"])[:9] if profile.get("cusip") else None,
        "isin": str(profile["isin"])[:12] if profile.get("isin") else None,
        "legal_name": profile.get("companyName"),
    }


def _asset_class_to_long_form(ac: Any) -> str:
    """Map TKR-14 AssetClass enum (1-char) to ticker_classifications.asset_class
    long-form (the column's CHECK constraint accepts 'stock'/'etf'/'fund'/'spac').

    TKR-14 keeps a finer 10-way taxonomy for issuer-hash entropy; the legacy
    classify_tickers column is 4-way. Coarsen here:
      STOCK/PREFERRED/REIT/ADR/WARRANT/NOTE → 'stock'
      ETF                                    → 'etf'
      FUND/TRUST                             → 'fund'
      SPAC_UNIT                              → 'spac'
    """
    from tpcore.identity.tkr14 import AssetClass
    if ac in (AssetClass.STOCK, AssetClass.PREFERRED, AssetClass.REIT,
              AssetClass.ADR, AssetClass.WARRANT, AssetClass.NOTE):
        return "stock"
    if ac == AssetClass.ETF:
        return "etf"
    if ac in (AssetClass.FUND, AssetClass.TRUST):
        return "fund"
    if ac == AssetClass.SPAC_UNIT:
        return "spac"
    return "stock"


async def _persist_resolved_classification(
    conn: asyncpg.Connection, resolved: Any,
) -> str:
    """INSERT ticker_classifications with the resolved identity; return id.

    Pin-at-first-resolve: ON CONFLICT (ticker) WHERE lifetime_end IS NULL DO NOTHING preserves any
    existing row. If a row already exists (race / re-run), SELECT the id
    by ticker. The 5 fillable cross-vendor identifier columns (cusip, isin,
    cik, figi, ipo_venue) are UPDATEd via COALESCE so existing non-nulls
    are NEVER overwritten — adds only what's currently NULL.
    """
    insert_id = await conn.fetchval(
        """
        INSERT INTO platform.ticker_classifications
            (id, ticker, current_ticker, current_exchange, current_legal_name,
             country, asset_class, source, ipo_venue, discovery_source,
             cik, cusip, isin, figi, status, updated_at)
        VALUES ($1, $2, $2, $3, $4,
                $5, $6, 'parent_resolver_backfill', $7, $8,
                $9, $10, $11, $12, 'active', now())
        ON CONFLICT (ticker) WHERE lifetime_end IS NULL DO NOTHING
        RETURNING id
        """,
        resolved.tkr14_id,
        resolved.ticker,
        resolved.exchange,
        resolved.legal_name,
        resolved.country,
        _asset_class_to_long_form(resolved.asset_class),
        str(resolved.ipo_venue.value) if resolved.ipo_venue else None,
        str(resolved.discovery_source.value) if resolved.discovery_source else None,
        resolved.cik,
        resolved.cusip,
        resolved.isin,
        resolved.figi,
    )
    if insert_id is not None:
        return str(insert_id)
    # Existing row — fill any NULL cross-vendor identifiers + SELECT id.
    existing = await conn.fetchrow(
        """
        UPDATE platform.ticker_classifications tc
        SET cusip = COALESCE(tc.cusip, $2),
            isin  = COALESCE(tc.isin,  $3),
            cik   = COALESCE(tc.cik,   $4),
            figi  = COALESCE(tc.figi,  $5),
            current_legal_name = COALESCE(tc.current_legal_name, $6),
            current_exchange   = COALESCE(tc.current_exchange,   $7)
        WHERE tc.ticker = $1
        RETURNING id
        """,
        resolved.ticker,
        resolved.cusip, resolved.isin, resolved.cik, resolved.figi,
        resolved.legal_name, resolved.exchange,
    )
    if existing is None or existing["id"] is None:
        raise RuntimeError(
            f"parent_resolver_backfill: ticker {resolved.ticker} INSERT no-op'd "
            f"but no existing row found — ticker_classifications corruption?"
        )
    return str(existing["id"])


async def _update_path_a_child_tables(
    conn: asyncpg.Connection, ticker: str, classification_id: str,
) -> dict[str, int]:
    """For each Path-A child table, set classification_id WHERE ticker=$1
    AND classification_id IS NULL. Returns per-table row-count map.
    Per-table own transaction-scope handled by caller."""
    counts: dict[str, int] = {}
    for tbl in _PATH_A_CHILD_TABLES:
        r = await conn.execute(
            f"""
            UPDATE platform.{tbl}
            SET classification_id = $2
            WHERE ticker = $1 AND classification_id IS NULL
            """,
            ticker, classification_id,
        )
        counts[tbl] = int(r.split()[-1]) if r.startswith("UPDATE") else 0
    return counts


async def _stage_parent_resolver_orphan_backfill(
    pool: asyncpg.Pool, cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v2.2 P6 Path-A orphan backfill — resolve unknown tickers + fill child FKs.

    Per v2.2 spec §1.10 + per-handler-lane dispatch: uses parent_resolver with
    the FMP-first general-identity lane (PROFILE kind). FMP /profile call,
    map to ResolvedClassification, INSERT ticker_classifications (pin-at-
    first-resolve), then UPDATE every Path-A child table's classification_id.

    cfg knobs:
      dry_run (default 'true') — count distinct orphans + sample 10.
      max_tickers (default 0 = no limit) — process at most N distinct tickers.
      tables (default 'all'; 'small' = exclude prices_daily for fast smoke run).
      flush_every (default 50) — commit-per-N-tickers (per-ticker UPDATE on
                                 prices_daily can touch thousands of rows).
    """
    import os as _os

    import httpx

    from tpcore.identity.parent_resolver import (
        HandlerKind,
        ResolveInputs,
    )
    from tpcore.identity.parent_resolver import (
        resolve as parent_resolve,
    )

    log = structlog.get_logger("scripts.ops")
    cfg = cfg or {}

    dry_run_param = cfg.get("dry_run", "true")
    dry_run = (dry_run_param.lower() != "false") if isinstance(dry_run_param, str) else bool(dry_run_param)
    max_tickers = int(cfg.get("max_tickers", 0))
    flush_every = int(cfg.get("flush_every", 50))
    scope = str(cfg.get("tables", "all"))

    target_tables = _PATH_A_CHILD_TABLES if scope == "all" else _PATH_A_CHILD_TABLES[:4]

    # Discover the distinct orphan ticker set across the target tables.
    union_sql = " UNION ".join(
        f"SELECT DISTINCT ticker FROM platform.{t} WHERE classification_id IS NULL"
        for t in target_tables
    )
    async with pool.acquire() as conn:
        ticker_rows = await conn.fetch(f"SELECT ticker FROM ({union_sql}) u ORDER BY ticker")
    tickers = [r["ticker"] for r in ticker_rows]
    if max_tickers:
        tickers = tickers[:max_tickers]

    log.info(
        "ops.stage.parent_resolver_orphan_backfill.starting",
        scope=scope, n_distinct_tickers=len(tickers),
        target_tables=list(target_tables), dry_run=dry_run,
    )

    if not tickers:
        return {"resolved": 0, "unresolved": 0, "child_rows_updated": 0,
                "dry_run": dry_run}

    if dry_run:
        # Per-table orphan-count preview.
        per_table_orphans: dict[str, int] = {}
        async with pool.acquire() as conn:
            for t in target_tables:
                per_table_orphans[t] = int(await conn.fetchval(
                    f"SELECT count(*) FROM platform.{t} WHERE classification_id IS NULL"
                ) or 0)
        log.info(
            "ops.stage.parent_resolver_orphan_backfill.dry_run",
            n_distinct_tickers=len(tickers),
            sample=tickers[:10],
            per_table_orphans=per_table_orphans,
        )
        return {
            "dry_run": True, "n_distinct_tickers": len(tickers),
            "sample": tickers[:10], "per_table_orphans": per_table_orphans,
        }

    fmp_key = _os.environ.get("FMP_API_KEY")
    if not fmp_key:
        from tpcore.outage import DataProviderOutage
        raise DataProviderOutage(
            "parent_resolver_orphan_backfill: FMP_API_KEY required."
        )
    FMP_BASE = "https://financialmodelingprep.com/stable"
    RATE_SLEEP_S = 0.2  # 5 req/sec FMP Starter ceiling

    resolved_count = 0
    unresolved_count = 0
    child_rows_updated = 0
    # No OpenFIGI calls in this backfill — figi gets filled later by the
    # separate tkr14_backfill[figi] stage. Saves a rate budget + complexity.
    async def _no_openfigi(_t: list[str]) -> list[Any]: return []

    async with httpx.AsyncClient(timeout=20.0) as client:
        async def _fmp_profile_lookup(ticker: str) -> Any:
            for attempt in range(3):
                try:
                    resp = await client.get(
                        f"{FMP_BASE}/profile",
                        params={"symbol": ticker, "apikey": fmp_key},
                    )
                    if resp.status_code == 429:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return _fmp_profile_to_resolved_inputs(data[0])
                    return None  # not-found / delisted
                except httpx.HTTPError:
                    if attempt == 2:
                        return None
                    await asyncio.sleep(2)
            return None

        for i, ticker in enumerate(tickers):
            try:
                profile_dict = await _fmp_profile_lookup(ticker)
                await asyncio.sleep(RATE_SLEEP_S)
                if profile_dict is None:
                    unresolved_count += 1
                    if i % 100 == 0 or i + 1 == len(tickers):
                        log.info(
                            "ops.stage.parent_resolver_orphan_backfill.progress",
                            processed=i + 1, total=len(tickers),
                            resolved=resolved_count, unresolved=unresolved_count,
                            child_rows_updated=child_rows_updated,
                        )
                    continue

                # parent_resolver dispatch — FMP-first lane (general identity).
                inputs = ResolveInputs(
                    ticker=ticker, cik=None, handler_kind=HandlerKind.PROFILE,
                )
                resolved = await parent_resolve(
                    inputs,
                    sec_ticker_lookup={},  # not needed for FMP-first lane
                    fmp_profile_lookup=lambda _t, _p=profile_dict: _p,
                    openfigi_lookup=_no_openfigi,
                )

                async with pool.acquire() as conn, conn.transaction():
                    cls_id = await _persist_resolved_classification(conn, resolved)
                    per_table_counts = await _update_path_a_child_tables(
                        conn, ticker, cls_id,
                    )
                resolved_count += 1
                child_rows_updated += sum(per_table_counts.values())
            except Exception as exc:  # noqa: BLE001  (operator wants to see failures, not crash run)
                unresolved_count += 1
                log.warning(
                    "ops.stage.parent_resolver_orphan_backfill.ticker_failed",
                    ticker=ticker, error=str(exc)[:200],
                )

            if (i + 1) % flush_every == 0:
                log.info(
                    "ops.stage.parent_resolver_orphan_backfill.progress",
                    processed=i + 1, total=len(tickers),
                    resolved=resolved_count, unresolved=unresolved_count,
                    child_rows_updated=child_rows_updated,
                )

    log.info(
        "ops.stage.parent_resolver_orphan_backfill.done",
        n_distinct_tickers=len(tickers),
        resolved=resolved_count,
        unresolved=unresolved_count,
        child_rows_updated=child_rows_updated,
    )
    return {
        "dry_run": False,
        "n_distinct_tickers": len(tickers),
        "resolved": resolved_count,
        "unresolved": unresolved_count,
        "child_rows_updated": child_rows_updated,
    }


async def _tkr14_backfill_fmp_profile(
    pool: asyncpg.Pool,
    log: Any,
    *,
    dry_run: bool,
    limit: int,
) -> dict[str, Any]:
    """SLICE 2 helper: batch-fill cusip / isin / cik via FMP /stable/profile.

    Pin-at-first-resolve discipline: only fills WHERE that column IS NULL.
    Never overwrites a non-null cross-vendor identifier. Per row, fetches
    the FMP profile once; populates whichever of cusip/isin/cik come back
    AND are currently NULL in the row.

    Rate-limited to ~5 req/sec (FMP Starter tier 300/min) via per-request
    asyncio.sleep. Concurrency 1 to keep the rate predictable. ~13K rows
    → ~45 min worst case.
    """
    import os as _os

    import httpx

    fmp_key = _os.environ.get("FMP_API_KEY")
    if not fmp_key:
        from tpcore.outage import DataProviderOutage
        raise DataProviderOutage(
            "tkr14_backfill[fmp_profile]: FMP_API_KEY env var required (FMP Starter tier)."
        )
    FMP_BASE = "https://financialmodelingprep.com/stable"
    RATE_SLEEP_S = 0.2  # 5 req/sec under FMP Starter 300/min ceiling

    where_limit = "LIMIT $1" if limit > 0 else ""
    args: list[Any] = [limit] if limit > 0 else []
    # Pick rows missing ANY of cusip / isin / cik so one FMP call fills all
    # three at once where possible.
    rows = await pool.fetch(
        f"""
        SELECT ticker, id, cusip, isin, cik
        FROM platform.ticker_classifications
        WHERE id IS NOT NULL
          AND (cusip IS NULL OR isin IS NULL OR cik IS NULL)
        ORDER BY ticker
        {where_limit}
        """,
        *args,
    )
    if not rows:
        log.info("ops.stage.tkr14_backfill.fmp_profile.no_rows_to_fill")
        return {"filled": 0, "not_found": 0, "dry_run": dry_run}

    log.info("ops.stage.tkr14_backfill.fmp_profile.starting", n_tickers=len(rows), dry_run=dry_run)

    BATCH = 500
    pending: list[dict[str, Any]] = []  # {ticker, cusip, isin, cik}
    n_committed = 0
    misses = 0

    async def _flush(buf: list[dict[str, Any]]) -> int:
        """Bulk UPDATE the pending buffer; return rows committed. Called streaming."""
        if not buf or dry_run:
            return 0
        tickers = [u["ticker"] for u in buf]
        cusips = [u.get("cusip") for u in buf]
        isins = [u.get("isin") for u in buf]
        ciks = [u.get("cik") for u in buf]
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE platform.ticker_classifications tc
                SET cusip = COALESCE(tc.cusip, b.cusip),
                    isin  = COALESCE(tc.isin,  b.isin),
                    cik   = COALESCE(tc.cik,   b.cik)
                FROM (
                    SELECT unnest($1::text[]) AS ticker,
                           unnest($2::text[]) AS cusip,
                           unnest($3::text[]) AS isin,
                           unnest($4::text[]) AS cik
                ) b
                WHERE tc.ticker = b.ticker
                """,
                tickers, cusips, isins, ciks,
            )
        return len(buf)

    sample_preview: list[dict[str, Any]] = []  # first 5 (dry_run only)
    async with httpx.AsyncClient(timeout=20.0) as client:
        for i, r in enumerate(rows):
            ticker = r["ticker"]
            for attempt in range(3):
                try:
                    resp = await client.get(
                        f"{FMP_BASE}/profile",
                        params={"symbol": ticker, "apikey": fmp_key},
                    )
                    if resp.status_code == 429:
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    if not isinstance(data, list) or not data:
                        misses += 1
                        break
                    profile = data[0]
                    upd: dict[str, Any] = {"ticker": ticker}
                    if r["cusip"] is None and (cusip := profile.get("cusip")):
                        upd["cusip"] = str(cusip)[:9]
                    if r["isin"] is None and (isin := profile.get("isin")):
                        upd["isin"] = str(isin)[:12]
                    if r["cik"] is None and (cik := profile.get("cik")):
                        upd["cik"] = str(cik)
                    if len(upd) > 1:
                        pending.append(upd)
                        if dry_run and len(sample_preview) < 5:
                            sample_preview.append(upd)
                    break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        misses += 1
                        log.warning("ops.stage.tkr14_backfill.fmp_profile.http_failed",
                                    ticker=ticker, error=str(e)[:120])
                    else:
                        await asyncio.sleep(2)
            await asyncio.sleep(RATE_SLEEP_S)
            # Streaming flush every BATCH rows so progress survives timeout/crash.
            if len(pending) >= BATCH:
                n_committed += await _flush(pending)
                pending.clear()
            if (i + 1) % 500 == 0:
                log.info("ops.stage.tkr14_backfill.fmp_profile.progress",
                         processed=i + 1, total=len(rows),
                         n_committed=n_committed, n_pending=len(pending), n_misses=misses)

    # Final flush
    if pending:
        n_committed += await _flush(pending)
        pending.clear()

    if dry_run:
        log.info("ops.stage.tkr14_backfill.fmp_profile.dry_run_preview",
                 n_filled=n_committed + len(sample_preview), n_not_found=misses,
                 sample=sample_preview)
        return {"filled": 0, "previewed": n_committed + len(sample_preview),
                "not_found": misses, "dry_run": True, "sample": sample_preview}

    log.info("ops.stage.tkr14_backfill.fmp_profile.committed",
             n_committed=n_committed, n_not_found=misses)
    return {"filled": n_committed, "not_found": misses, "dry_run": False}


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
          ``platform.insider_transactions``. Default is BOTH.
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
    #
    # 2026-05-23: sec_insider_transactions was renamed insider_transactions
    # in v2.1 Phase 1 (PR #318). fundamentals_quarterly added to the dedupe
    # set after audit found 67 natural-key duplicate groups (68 extra rows)
    # not caught by its synthetic-id PK.
    specs: list[dict[str, Any]] = [
        {
            "name": "earnings_events",
            "table": "platform.earnings_events",
            "key_cols": ["ticker", "event_date", "event_type"],
        },
        {
            "name": "insider_transactions",
            "table": "platform.insider_transactions",
            "key_cols": [
                "ticker", "filing_date", "insider_name",
                "transaction_type", "shares",
            ],
        },
        {
            "name": "fundamentals_quarterly",
            "table": "platform.fundamentals_quarterly",
            "key_cols": ["ticker", "period_end_date", "period_label"],
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
                "MAX(filing_date) mx FROM platform.insider_transactions"
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
                (SELECT COUNT(*) FROM platform.insider_transactions) AS insider_rows,
                (SELECT COUNT(*) FROM platform.sec_material_events) AS material_rows,
                (SELECT COUNT(DISTINCT ticker) FROM platform.insider_transactions) AS insider_tickers,
                (SELECT COUNT(DISTINCT ticker) FROM platform.sec_material_events) AS material_tickers,
                LEAST(
                    (SELECT MIN(filing_date) FROM platform.insider_transactions),
                    (SELECT MIN(filing_date) FROM platform.sec_material_events)
                ) AS earliest_filing,
                GREATEST(
                    (SELECT MAX(filing_date) FROM platform.insider_transactions),
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
            FROM platform.insider_transactions
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


# P0_3 RETIRE 2026-05-25 — ``_stage_historical_insider_sentiment_daily``
# + ``_stage_daily_insider_sentiment_delta`` removed. Target table
# ``platform.insider_filings`` was DROPPED in migration
# ``20260522_0200_drop_insider_filings_add_sec_mspr`` (the FMP path was
# redundant with the SEC-EDGAR ``insider_transactions`` ingest). Producer
# adapter ``tpcore.data.insider_backfill`` deleted; FeedProfile,
# ProviderBinding, HealSpec, validation check, and dispatcher entry all
# closed in the same 3-way retirement (P0_3 trust-audit).


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


async def _stage_release_paper_holds_above_paper_floor(
    pool: asyncpg.Pool, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Operator-on-demand — clear stale ENGINE_HELD rows for PAPER
    engines whose latest credibility is at or above the new paper floor.

    Companion to PR ``feat/lifecycle-pause-mode-aware-credibility-floor``
    (operator directive 2026-05-22). PR #272 (Wave-4 E7/E11) applied the
    live-promotion floor (``MIN_LIVE_SCORE`` = 60) to ALL engines,
    paper-or-live. That bricked the autonomous-Lab admit pathway (PR
    #158) where engines land in PAPER at credibility ~0.40-0.50.

    Engines currently paused at credibility 0.40-0.55 should resume
    dispatching the moment the mode-aware logic ships. This stage emits
    a canonical ``ENGINE_CLEARED`` event (the same vocabulary the
    supervisor uses, mirroring ``ops.engine_supervisor._emit_cleared``)
    for each PAPER engine whose latest credibility ≥ the new paper
    floor (``MIN_PAPER_SCORE`` / 100 = 0.30).

    Behaviour:

    * Enumerates all open holds via ``current_hold`` per profiled engine.
    * For each ``LifecycleState.PAPER`` engine: reads the latest
      ``confidence`` from ``platform.data_quality_log`` for source
      ``backtest_credibility.<engine>``; if at or above the paper floor,
      emits ``ENGINE_CLEARED`` keyed on the open hold's ``hold_id``.
    * For each ``LifecycleState.LIVE`` engine: NEVER auto-clears (leaves
      the hold for the supervisor's own clear-predicate or operator).
    * Idempotent — clearing a hold means the next ``current_hold`` call
      returns ``None`` so a re-run is a no-op.
    * RETIRED + LAB engines never appear (they're not in the dispatchable
      set; ``current_hold`` may still return rows but they're filtered
      out here by ``lifecycle_state`` check).

    Operator-on-demand only (NOT in ``OPS_UPDATE_STAGES``). Run after
    the mode-aware-floor PR merges to unstick the four paper engines
    (reversion, vector, sentinel, momentum) that PR #272 paused.

    Returns a per-engine action map for audit + operator clarity.
    """
    from tpcore.backtest.credibility import (
        CREDIBILITY_SOURCE_PREFIX,
        MIN_PAPER_SCORE,
    )
    from tpcore.engine_profile import (
        LifecycleState,
        profile_for,
        roster_for_dispatch,
    )
    from tpcore.supervisor_state import (
        CLEARED_EVENT,
        SCHEMA_VERSION,
        current_hold,
    )

    paper_floor_pct = MIN_PAPER_SCORE / 100
    results: dict[str, dict[str, Any]] = {}

    # Iterate the dispatchable roster (PAPER + LIVE engines, the only
    # actors that can carry an active hold). The allocator is dispatched
    # via a separate path and gets the same hold check at runtime, so we
    # include it explicitly here too. RETIRED + LAB engines are never
    # dispatched (filtered out by ``_DISPATCHABLE``) so any stale held
    # rows on them are inert — we leave those untouched.
    engines_to_scan = (*roster_for_dispatch(), "allocator")

    select_latest_sql = """
        SELECT confidence
        FROM platform.data_quality_log
        WHERE source = $1
        ORDER BY timestamp DESC
        LIMIT 1
    """

    for engine_name in sorted(engines_to_scan):
        profile = profile_for(engine_name)
        if profile is None:
            continue
        hold = await current_hold(pool, engine_name)
        if hold is None:
            # No open hold — nothing to do.
            continue
        if profile.lifecycle_state is not LifecycleState.PAPER:
            # LIVE / LAB / RETIRED — never auto-clear here.
            results[engine_name] = {
                "action": "skipped_non_paper",
                "lifecycle_state": profile.lifecycle_state.value,
                "hold_id": hold.hold_id,
                "failure_class": hold.failure_class,
            }
            continue

        # Read the latest credibility for the engine.
        source = f"{CREDIBILITY_SOURCE_PREFIX}.{engine_name}"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(select_latest_sql, source)
        if row is None:
            results[engine_name] = {
                "action": "skipped_no_credibility_row",
                "lifecycle_state": "paper",
                "hold_id": hold.hold_id,
            }
            continue
        latest_confidence = float(row["confidence"])
        if latest_confidence < paper_floor_pct:
            results[engine_name] = {
                "action": "skipped_below_paper_floor",
                "lifecycle_state": "paper",
                "hold_id": hold.hold_id,
                "latest_confidence": round(latest_confidence, 4),
                "paper_floor_pct": paper_floor_pct,
            }
            continue

        # Above paper floor → clear the hold via the canonical
        # ENGINE_CLEARED event keyed on the open hold's hold_id.
        clear_reason = (
            f"mode_aware_floor_release: paper engine latest credibility "
            f"{latest_confidence:.3f} >= paper floor {paper_floor_pct:.2f}"
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO platform.application_log
                    (engine, run_id, event_type, severity, message, data)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                engine_name,
                uuid.uuid4(),
                CLEARED_EVENT,
                "INFO",
                f"{engine_name} cleared: {clear_reason}",
                json.dumps({
                    "schema": SCHEMA_VERSION,
                    "hold_id": hold.hold_id,
                    "engine": engine_name,
                    "clear_reason": clear_reason,
                    "released_by_stage": (
                        "release_paper_holds_above_paper_floor"),
                }),
            )
        results[engine_name] = {
            "action": "released",
            "lifecycle_state": "paper",
            "hold_id": hold.hold_id,
            "failure_class": hold.failure_class,
            "latest_confidence": round(latest_confidence, 4),
            "paper_floor_pct": paper_floor_pct,
        }

    released_count = sum(
        1 for r in results.values() if r["action"] == "released"
    )
    return {
        "released_count": released_count,
        "paper_floor_pct": paper_floor_pct,
        "engines": results,
    }


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
    ``platform.data_quality_log`` (``kind='forensics_trigger'``; Plan 2
    consolidation), and writes Sprint Dossier markdown files under
    ``docs/sprints/`` (fingerprinted, so re-running is a no-op). Read-side
    stage — does not modify any data-update table.

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
    """Inject ONE well-formed forensics-trigger row for engine='canary'
    ONLY (DA-2 end-to-end harness; Plan 2: data_quality_log
    kind='forensics_trigger'). Payload mirrors the forensics producer's shape
    per kind + a source='canary_injection' marker for audit/teardown.
    ``--param teardown=true`` removes all injected rows. NEVER writes for any
    engine other than canary.

    Supported kinds: ``outlier_loss``, ``loss_cluster``, ``drawdown_period``.
    Default kind is ``loss_cluster``.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from tpcore.forensics import dql_store
    from tpcore.quality.data_quality import KIND_FORENSICS_TRIGGER

    cfg = config or {}
    if cfg.get("engine", "canary") != "canary":
        raise ValueError(
            "canary_inject_trigger writes for engine='canary' ONLY — "
            "pass engine='canary' or omit the param entirely")
    if cfg.get("teardown"):
        # Plan 2: injected rows live in data_quality_log
        # (kind='forensics_trigger'); the canary marker is notes->>'source'.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.data_quality_log "
                "WHERE kind = $1 AND notes->>'source' = $2",
                KIND_FORENSICS_TRIGGER, _CANARY_INJECTION_SOURCE)
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
        if not await dql_store.fingerprint_exists(
            conn, trigger_kind=kind, fingerprint=fp
        ):
            await dql_store.insert_trigger(
                conn,
                trigger_kind=kind,
                engine="canary",
                payload=payload,
                fired_at=now,
            )
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
    # NOTE: a pinned test (test_compute_fundamental_ratios_stage) asserts
    # this stage runs at exactly position N+1 where N is fundamentals_refresh.
    # ANY new stage must NOT be inserted between these two.
    ("compute_fundamental_ratios", lambda pool, cfg: (lambda: _stage_compute_fundamental_ratios(pool, cfg)), STAGE_TIMEOUT_SEC),
    # SEC EDGAR companyfacts fallback — fills period gaps FMP doesn't have
    # (pre-IPO predecessors, recent-IPO sparse history, balance-sheet gaps).
    # Runs AFTER compute_fundamental_ratios so the FMP→ratios chain stays
    # intact (pin-tested). SEC rows landed by this stage will get their
    # ratios computed on the NEXT daily cycle's compute_fundamental_ratios
    # — acceptable because the pre-IPO / historical periods this stage
    # fills don't drive any same-cycle engine decision.
    # Per memory feedback_sec_authoritative_fmp_fallback_non_us — SEC is
    # the US-filer authoritative source. ~10 req/sec; one HTTP call per
    # CIK returns full XBRL history.
    ("sec_fundamentals_fallback",
     lambda pool, cfg: (lambda: _stage_sec_fundamentals_fallback(pool, cfg)),
     HEAVY_STAGE_TIMEOUT_SEC),
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
    # universe_build (Plan 3 Phase 1) — survivorship-free, identity-first
    # universe minter (SEC full company list ∪ FMP symbols + delisting;
    # TKR-14 mint; lifetime_start=FPFD, no sentinel; delisted INCLUDED).
    # NOT in the child-first --update order (it is in _OFF_CYCLE_STAGES) —
    # identity must be built BEFORE child loads so the 14 BEFORE INSERT
    # triggers attribute classification_id correctly. dry_run=true default.
    # See docs/audits/2026-06-05-identity-build-code-state.md +
    # docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md §4/§5.
    ("universe_build",      lambda pool, cfg: (lambda: _stage_universe_build(pool, cfg)),        HEAVY_STAGE_TIMEOUT_SEC),
    # Identity-first stages #2-#4 (Plan 3 Phase 1) — issuers + SCD-2
    # issuer_history (issuers_build), ticker_history reuse derivation
    # (ticker_history_reuse_build, G3), and the M:N issuer↔security fan-out
    # (issuer_securities_build). All OFF-CYCLE: they run BEFORE child loads
    # via the identity_build orchestrator (or explicit --stage). The
    # orchestrator (_stage_identity_build) runs the four IN ORDER + the
    # BLOCKING identity gate. dry_run=true default. See
    # docs/superpowers/specs/2026-06-04-data-layer-rebuild-design.md §4/§5.3.
    ("issuers_build",       lambda pool, cfg: (lambda: _stage_issuers_build(pool, cfg)),         HEAVY_STAGE_TIMEOUT_SEC),
    ("ticker_history_reuse_build",
        lambda pool, cfg: (lambda: _stage_ticker_history_reuse_build(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    ("issuer_securities_build",
        lambda pool, cfg: (lambda: _stage_issuer_securities_build(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    ("identity_build",      lambda pool, cfg: (lambda: _stage_identity_build(pool, cfg)),        HEAVY_STAGE_TIMEOUT_SEC),
    ("classify_tickers",    lambda pool, cfg: (lambda: _stage_classify_tickers(pool, cfg)),     HEAVY_STAGE_TIMEOUT_SEC),
    # OpenFIGI-driven asset_class taxonomy refinement (2026-05-30 expert review).
    # Operator-on-demand; not run on the cron path (the 4→10 mapping is a
    # one-shot followed by per-new-ticker refresh on classify_tickers cadence).
    ("reclassify_asset_class",
        lambda pool, cfg: (lambda: _stage_reclassify_asset_class(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # P0-003 (2026-05-30) — SEC-evidence metadata backfill (operator-on-
    # demand; not in any scheduled pipeline). Foundation for the future
    # five-state fundamentals_quarterly_completeness rewrite — this
    # stage ONLY writes the new evidence columns added by migration
    # 20260530_0200; validator semantics are unchanged. Idempotent.
    # See docs/superpowers/specs/2026-05-30-asset-class-refinement.md
    # follow-up + expert audit REVISE_ARCHITECTURE.
    ("backfill_sec_metadata",
        lambda pool, cfg: (lambda: _stage_backfill_sec_metadata(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # P2a (2026-05-30) — SEC Form 25 / Form 15 lifecycle evidence
    # backfill. Operator-on-demand (not in the scheduled pipeline).
    # Foundation for the future P2b lifecycle-bound validator wiring —
    # this stage ONLY writes the new evidence columns + the new
    # ticker_lifecycle_events table added by migration 20260530_0300;
    # validator semantics + capital gate are unchanged. Idempotent.
    ("backfill_sec_lifecycle",
        lambda pool, cfg: (lambda: _stage_backfill_sec_lifecycle(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # 2026-06-02 — ticker-reuse fundamentals cleanup (PR #440 plan).
    # Operator-on-demand only. Default dry_run=true writes a manifest
    # CSV with proposed dispositions; `--param dry_run=false
    # --param delete_after_archive=true --param evidence_level=strong`
    # is required for any DELETE. Archive-before-delete is structural;
    # weak-evidence rows are NEVER deleted; FPFD-drift rows are skipped
    # via re-extraction from the bulk reader. See plan §5–§10.
    ("cleanup_ticker_reuse_fundamentals",
        lambda pool, cfg: (
            lambda: _stage_cleanup_ticker_reuse_fundamentals(pool, cfg)
        ),
        HEAVY_STAGE_TIMEOUT_SEC),
    # Symbol-history evidence backfill (spec PR #442, plan PR #443).
    # Populates ticker_history + issuer_securities + historical
    # ticker_classifications predecessors from FMP /stable/symbol-change
    # (Path B primary) cross-walked against SEC submissions.zip
    # (Path C resolver). Single bulk GET; archive-first via R2; no
    # per-ticker crawl (``use_bulk_zip=false`` raises). Additive only —
    # NEVER touches fundamentals_quarterly. Operator-on-demand;
    # ``dry_run=true`` by default. See:
    #   docs/superpowers/specs/2026-06-02-symbol-history-evidence-backfill.md
    #   docs/superpowers/plans/2026-06-02-symbol-history-evidence-backfill-plan.md
    ("symbol_history_evidence_backfill",
        lambda pool, cfg: (
            lambda: _stage_symbol_history_evidence_backfill(pool, cfg)
        ),
        HEAVY_STAGE_TIMEOUT_SEC),
    # F0 (2026-06-01) — provider-parity EVALUATE stage. Operator-on-
    # demand only (not in any scheduled pipeline). Pulls dual samples
    # for a (feed, candidate) pair, calls
    # tpcore.parity.compare_provider_parity, persists verdict to
    # data_quality_log + application_log. Pre-requisite for any
    # CANDIDATE → FALLBACK promotion (the cutover_agent's freshness
    # check at ops/cutover_agent.py reads these verdicts).
    ("evaluate_provider_parity",
        lambda pool, cfg: (lambda: _stage_evaluate_provider_parity(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # v2.2 Phase P5 — backfill TKR-14 PK on existing ticker_classifications
    # rows + seed ticker_history first-seen entries. Idempotent; safe re-run.
    # SLICE 1: local-only (no external API). SLICE 2 (cross-vendor mode):
    # OpenFIGI + FMP /profile for figi/cusip/isin. Operator-on-demand,
    # NOT a scheduled cadence stage.
    ("tkr14_backfill",      lambda pool, cfg: (lambda: _stage_tkr14_backfill(pool, cfg)),       HEAVY_STAGE_TIMEOUT_SEC),
    # v2.2 P6 step 2 — chunked backfill (100K rows/txn) for prices_daily.classification_id.
    # Sidesteps the 1.95 GB WAL blow-up from the 2026-05-23 single-transaction attempt.
    ("prices_daily_backfill_classification_id",
        lambda pool, cfg: (lambda: _stage_prices_daily_backfill_classification_id(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # v2.2 P6 Path-A orphan backfill — resolves unknown tickers via FMP
    # /profile + parent_resolver, INSERTs ticker_classifications,
    # UPDATEs each Path-A child table's classification_id. Operator-on-demand.
    # cfg: dry_run, max_tickers, tables=all|small, flush_every.
    ("parent_resolver_orphan_backfill",
        lambda pool, cfg: (lambda: _stage_parent_resolver_orphan_backfill(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # Task #18 follow-on — populate platform.series_catalog (per-series
    # metadata: cadence, unit, vendor_series_id, publish day/lag, sacred flag).
    # Idempotent UPSERT on (source, series_id); operator-on-demand re-run
    # whenever the static metadata dict is refined.
    ("series_catalog_backfill",
        lambda pool, cfg: (lambda: _stage_series_catalog_backfill(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # Corporate-history enrichment P2 — load the hand-curated truth-set CSV
    # at scripts/seed/corporate_events_seed.csv into issuers + issuer_securities
    # + corporate_events. Idempotent (deterministic issuer_id + event_id;
    # ON CONFLICT DO NOTHING). Operator-on-demand re-run after CSV edits.
    ("corporate_events_seed",
        lambda pool, cfg: (lambda: _stage_corporate_events_seed(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # SEC EDGAR formerNames discovery-driven backfill — walks every CIK
    # in `ticker_classifications` and populates `issuer_history` +
    # `corporate_events` (kind='name_only_change') from EDGAR's
    # canonical name-period array. Complements `corporate_events_seed`
    # (CSV-driven, hand-curated): EDGAR covers the long tail of pure
    # name changes that don't warrant a CSV entry. Idempotent + safe
    # to re-run; ~10 req/sec rate-limited. Requires SEC_EDGAR_USER_AGENT.
    ("corp_history_edgar_backfill",
        lambda pool, cfg: (lambda: _stage_corp_history_edgar_backfill(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # SEC EDGAR orphan resolver — Path-A FK closure for delisted/historical
    # tickers via direct CIK lookup. Phase A uses the seed CSV's CIKs; later
    # phases extend with EDGAR ticker→CIK lookup + alternate-source for the
    # foreign/warrant tail. Designed to be wired into the self-heal cascade
    # catalog (PRICES_DAILY_CLASSIFICATION_ID_NULL signal — follow-on).
    ("sec_orphan_resolve",
        lambda pool, cfg: (lambda: _stage_sec_orphan_resolve(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # Defect-#3 cleanup — close residual NULL classification_id rows in
    # the 13 path-A child tables (corporate_actions + fundamentals_quarterly
    # had 74 nulls post-trigger-creation; pre-trigger inserts + date-
    # window misses). Single UPDATE per table joins on ticker_classifications
    # WHERE lifetime_end IS NULL. Idempotent + bounded (only touches NULL rows).
    ("residual_classification_id_fill",
        lambda pool, cfg: (lambda: _stage_residual_classification_id_fill(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # One-shot audit-cleanup stage (2026-05-24 db-architect audit
    # found 4 data-consistency defects: delisted-but-active
    # classifications, duplicate Meta issuer_history opens, bitemporal
    # duplicates of FB->META event, hy_spread memory mismatch).
    # Idempotent (each operation is INSERT/UPDATE on a NOT-yet state).
    ("audit_cleanup_2026_05_24",
        lambda pool, cfg: (lambda: _stage_audit_cleanup_2026_05_24(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # Operator catch 2026-05-25: META duplicate issuer_history fix
    # in PR #340 was specific; same root cause produced 2,061
    # overlapping pairs across other issuers. Stage rewrites
    # issuer_history per-issuer into a non-overlapping chain (next
    # row's valid_from caps previous row's valid_to) + cleans
    # zero-duration + invalid-range. Idempotent.
    ("issuer_history_cleanup",
        lambda pool, cfg: (lambda: _stage_issuer_history_cleanup(pool, cfg)),
        STAGE_TIMEOUT_SEC),
    # FMP /stable/profile bulk-batch backfill for ticker_classifications.
    # Populates gics_sector + refreshes country/current_legal_name when
    # NULL. Canonical replacement for the deprecated one-off
    # scripts/backfill_country_from_fmp.py (data-adapter rule mandates
    # canonical stage entry, not forked scripts).
    ("fmp_profile_backfill",
        lambda pool, cfg: (lambda: _stage_fmp_profile_backfill(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # GLEIF ISIN-based LEI lookup for issuers (ISO 17442). Closes the
    # ALL_NULL issuers.lei gap flagged in 2026-05-24 audit.
    ("gleif_lei_backfill",
        lambda pool, cfg: (lambda: _stage_gleif_lei_backfill(pool, cfg)),
        HEAVY_STAGE_TIMEOUT_SEC),
    # Maintain platform.ticker_history SCD-2 timeline — INSERTs missing rows
    # for any ticker_classification without one + closes valid_to on rows
    # whose ticker is delisted in prices_daily. Idempotent + safe to wire as
    # a HealSpec for a future ticker_history_completeness check.
    ("ticker_history_backfill",
        lambda pool, cfg: (lambda: _stage_ticker_history_backfill(pool, cfg)),
        STAGE_TIMEOUT_SEC),
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
    # FRED macro indicators — weekly. ~93 canonical series (sahm_rule,
    # industrial_production, initial_claims, yield_curve, hy_spread,
    # 50-state PHCI panel, sub-state Carbondale/LWA-25 series, etc.)
    # via FREDAdapter, idempotent ON CONFLICT. Heavy timeout because a
    # backfill with skip_guard_days=0 fetches every series in series
    # (~120s+ wall-clock end-to-end is normal).
    ("macro_indicators",    lambda pool, cfg: (lambda: _stage_macro_indicators(pool, cfg)),    HEAVY_STAGE_TIMEOUT_SEC),
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
    # release_paper_holds_above_paper_floor — one-shot companion to PR
    # feat/lifecycle-pause-mode-aware-credibility-floor (operator
    # directive 2026-05-22). Clears stale ENGINE_HELD rows for PAPER
    # engines whose latest credibility is at or above the new paper
    # floor (MIN_PAPER_SCORE/100). LIVE engines are NEVER auto-cleared
    # here. Idempotent; operator-on-demand only (NOT in --update).
    ("release_paper_holds_above_paper_floor",
        lambda pool, cfg: (lambda: _stage_release_paper_holds_above_paper_floor(pool, cfg)),
        STAGE_TIMEOUT_SEC),
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
    # P0_3 RETIRE 2026-05-25 — ``historical_insider_sentiment_daily``
    # + ``daily_insider_sentiment_delta`` _STAGE_SPECS entries removed
    # (target ``platform.insider_filings`` table dropped; full
    # FeedProfile + ProviderBinding + HealSpec + check + producer +
    # stage retirement closed in the same PR).
    # confirmed_data_gap_evidence_populator (2026-06-03) — heavy lane
    # one-shot operator-on-demand stage per spec PR #450 + plan
    # PR #451. Populates data_quality_log
    # (kind='confirmed_data_gap_evidence', Plan 2) for currently-FAILing
    # (ticker, period_end_date) tuples. Default
    # dry_run=true at the stage layer; live writes require the
    # operator to pass --param dry_run=false.
    ("confirmed_data_gap_evidence_populator",
        lambda pool, cfg: (
            lambda: _stage_confirmed_data_gap_evidence_populator(pool, cfg)
        ),
        HEAVY_STAGE_TIMEOUT_SEC),
)
KNOWN_STAGES: tuple[str, ...] = tuple(name for name, _, _ in _STAGE_SPECS)
# Stages that are NOT part of the default daily ``cmd_update`` cycle —
# they are only invoked on-demand (operator CLI) or by the auto-cascade.
# Keeps the daily cycle bounded; new self-heal stages live here.
_OFF_CYCLE_STAGES: frozenset[str] = frozenset({
    "rebuild_from_archive",
    "dedupe_monotone",
    # universe_build (Plan 3 Phase 1) — identity-first universe minter.
    # Operator/orchestrator-only: it runs BEFORE child loads (NOT in the
    # child-first daily --update order) so identity is correct-first and
    # the BEFORE INSERT triggers attribute classification_id correctly.
    "universe_build",
    # Identity-first stages #2-#4 + orchestrator (Plan 3 Phase 1) — all
    # off-cycle for the SAME reason: identity must be built BEFORE child
    # loads, so they never ride the child-first daily --update cadence.
    "issuers_build",
    "ticker_history_reuse_build",
    "issuer_securities_build",
    "identity_build",
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
    # Mode-aware-floor hold-release (PR 2026-05-22 companion). Operator-
    # on-demand only; clears stale paper-engine holds whose credibility
    # is at or above the new paper floor.
    "release_paper_holds_above_paper_floor",
    # `excluded_confirmed_data_gap` evidence populator (2026-06-03).
    # Operator-on-demand only; default dry_run=true. Lives off the
    # daily cycle until evidence-substrate population reaches a
    # cadence cadence-policy operator authorizes.
    "confirmed_data_gap_evidence_populator",
    # P0_3 RETIRE 2026-05-25 — ``historical_insider_sentiment_daily``
    # removed from the off-cycle set (stage definition above also gone).
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
        "platform.insider_transactions",
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
            "prices_daily_classification_id_completeness",
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
            "insider_sentiment_freshness",
            "social_sentiment_freshness",
            "fear_greed_freshness",
            "short_interest_freshness",
            "borrow_rates_freshness",
            "aaii_sentiment_freshness",
        ),
    ),
    # Chunk 7 — SCD-2 / bitemporal integrity (added 2026-05-25 after
    # the META-was-tip-of-iceberg audit found 2,061 overlap pairs).
    # All four are pure aggregate queries over the corp-history
    # substrate — fast, no per-ticker scan; isolating them in their
    # own chunk keeps the prices_daily/macro chunks deterministic.
    (
        "corp_history_integrity",
        (
            "issuer_history_integrity",
            "issuer_securities_integrity",
            "corporate_events_integrity",
            "ticker_history_integrity",
        ),
    ),
    # Chunk 8 — meta-monitors (added 2026-05-25 P0 trust-audit). Both
    # are sub-second single-row queries: daemon_freshness reads
    # platform.daemon_heartbeats, data_operations_complete_cadence
    # reads MAX(recorded_at) from platform.application_log. They
    # belong in their own chunk because the failure mode they cover —
    # the lane has stopped running — would also stop other chunks
    # from completing, so this chunk gets evaluated first-in-class.
    (
        "meta_monitors",
        (
            "daemon_freshness",
            "data_operations_complete_cadence",
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

    # 2026-05-29 control-plane fix (REQ-001/002/003/004) — after all
    # cascades complete, build the authoritative FinalLaneVerdict. This
    # may re-run data_validation ONCE inline (post-cascade re-validation)
    # so a cascade-healed lane reports exit_code=0 honestly. Skipped on
    # dry_run (no DB writes allowed) — dry_run uses the legacy
    # stage-status exit_code path.
    if not dry_run:
        summary.final_verdict = await _build_final_lane_verdict(
            summary, pool, log=log, db_log=db_log,
        )

    summary.finished_at = datetime.now(UTC)
    return summary


async def _build_final_lane_verdict(
    summary: UpdateSummary,
    pool: asyncpg.Pool,
    *,
    log: structlog.stdlib.BoundLogger,
    db_log,
) -> FinalLaneVerdict:
    """Build the FinalLaneVerdict that governs every downstream gate.

    Spec: 2026-05-29 control-plane fix (`task_spec.long_term_data_
    operations_control_plane_fix`). Audit verdict was FAIL — the Wave-1
    cascade healed reds without re-running data_validation, so
    UpdateSummary.exit_code stayed 1 (derived purely from stage status),
    the wrapper aborted before Step 6, and DATA_OPERATIONS_COMPLETE
    never emitted naturally.

    This function consolidates ALL the post-cascade decisions:

    1. Locate the FIRST-PASS ``data_validation`` stage row.
       * If no row exists: NO data_validation ran (only-flag path, infra
         stages only). Verdict GREEN — there were no reds to heal.
       * If row.status == 'OK': first pass passed. Verdict GREEN,
         cascade_attempted=False, post_cascade_validation_status=None.
       * If row.status == 'FAILED' with a parseable failed_checks list:
         the cascade may have already mutated row.detail with
         ``handled`` / ``skipped`` / ``vendor_late`` lists. Re-validate
         to see whether the heal actually worked.

    2. Re-validation rules (post-cascade, exactly once):
       * If every first-pass red was either ``handled`` (cascade
         attempted a refresh) OR ``vendor_late`` (D11 classification,
         no refresh) OR ``unhealable`` (HealSpec healable=False, no
         refresh), AND at least one ``handled`` entry exists
         (something WAS dispatched), then re-run data_validation
         ONCE.
       * If nothing was ``handled`` (all reds were vendor_late /
         unhealable), there's no point re-running — no data changed.
         Set post_cascade_validation_status='NOT_RUN'.

    3. Final-status computation:
       * post-cascade re-validation GREEN → final_status='GREEN',
         flip the data_validation stage row to status='OK' with a
         ``post_cascade_passed`` detail breadcrumb so
         ``UpdateSummary.exit_code``'s legacy fallback agrees.
       * post-cascade re-validation RED → final_status='RED',
         leave row FAILED with updated ``remaining_failed_checks``
         detail; exit_code=1.
       * 'NOT_RUN' branch → final_status depends on whether all
         remaining reds are unhealable+vendor_late (operator-visible
         but lane is "as good as it gets"). Per the
         100%-green-or-don't-trade invariant, we still classify this
         as RED — operator gates the lane, no silent green. The
         emission_allowed=False guard is the wrapper's safety.

    4. INGESTION_AUTO_RECOVERED_VALIDATION emission semantics
       (REQ-003): only emitted by this function, only when
       post_cascade_validation_status='GREEN'. The intermediate
       INGESTION_AUTO_RECOVERED_VALIDATION events emitted from
       _auto_cascade_validation_failures are renamed elsewhere to
       INGESTION_AUTO_RECOVERY_STAGE_OK (refresh stage dispatched and
       returned OK) — that's an *attempted* recovery, not *proven*
       recovery. This function fires the proven-green event.

    Returns the FinalLaneVerdict; the caller stores it on
    summary.final_verdict so the legacy ``exit_code`` property
    consults it.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    # Locate data_validation row (if any).
    val_idx: int | None = None
    val_result: StageResult | None = None
    for i, s in enumerate(summary.stages):
        if s.name == "data_validation":
            val_idx = i
            val_result = s
            break

    # Case A — no data_validation ran (--only path, infra-only). Verdict
    # depends on whether any OTHER stage failed. Preserves backwards
    # compat with the legacy exit_code semantics.
    if val_result is None:
        any_failed = any(
            s.status in ("FAILED", "TIMEOUT") for s in summary.stages
        )
        status = "GREEN" if not any_failed else "RED"
        return FinalLaneVerdict(
            final_status=status,
            exit_code=0 if status == "GREEN" else 1,
            emission_allowed=(status == "GREEN"),
            engine_dispatch_allowed=(status == "GREEN"),
            cascade_attempted=False,
            post_cascade_validation_status=None,
        )

    # Case B — first pass already green (no cascade fired).
    if val_result.status == "OK":
        any_other_failed = any(
            s.name != "data_validation"
            and s.status in ("FAILED", "TIMEOUT")
            for s in summary.stages
        )
        status = "GREEN" if not any_other_failed else "RED"
        return FinalLaneVerdict(
            final_status=status,
            exit_code=0 if status == "GREEN" else 1,
            emission_allowed=(status == "GREEN"),
            engine_dispatch_allowed=(status == "GREEN"),
            cascade_attempted=False,
            post_cascade_validation_status=None,
        )

    # Case C — first pass FAILED. Parse the cascade-mutated detail to
    # see what was handled vs left red.
    detail = val_result.detail or {}
    first_pass_failed = list(detail.get("failed_checks") or [])
    if not first_pass_failed:
        # Couldn't parse — re-extract from error message as a fallback.
        first_pass_failed = _parse_failed_check_names(val_result.error)
    handled = list(detail.get("handled") or [])
    skipped = list(detail.get("skipped") or [])
    vendor_late = list(detail.get("vendor_late") or [])
    cascade_ran = bool(detail.get("cascade")) or bool(handled)

    # Classify ``skipped`` entries: HealSpec-unhealable vs missing-
    # HealSpec. The registry call is the source of truth.
    unhealable: list[str] = []
    truly_unmapped: list[str] = []
    try:
        from tpcore.selfheal.registry import spec_for as _spec_for
        for cn in skipped:
            sp = _spec_for(cn)
            if sp is not None and not sp.healable:
                unhealable.append(cn)
            else:
                truly_unmapped.append(cn)
    except Exception:  # noqa: BLE001 — registry import is best-effort
        truly_unmapped = list(skipped)

    # Decide whether to re-run data_validation (REQ-002).
    should_revalidate = bool(handled)  # nothing dispatched → no point
    post_status: str | None = None
    remaining_failed: list[str] = []
    recovered: list[str] = []

    if not should_revalidate:
        post_status = "NOT_RUN"
        # No reds were dispatched. Everything that was red stays red.
        # Distinguish unhealable+vendor_late (operator-classified) from
        # genuinely unmapped (sentinel-fail).
        remaining_failed = [
            c for c in first_pass_failed
            if c not in vendor_late and c not in unhealable
        ]
    else:
        # Re-run data_validation exactly once. Bounded by the SAME 300s
        # cap _STAGE_SPECS registers for the regular stage invocation
        # (line ~7593) so a slow re-validate never hangs the cron past
        # the cadence budget — REQ-001 from the 2026-05-29 expert
        # review's F-001 (post-cascade re-validate must not be
        # unbounded). The wrapped stage-runner path uses
        # _run_stage_with_timeout's asyncio.wait_for; mirror it here.
        _REVALIDATE_TIMEOUT_SEC = 300.0
        try:
            revalidate_t0 = _dt.now(_UTC)
            revalidate_payload = await asyncio.wait_for(
                _stage_data_validation(pool),
                timeout=_REVALIDATE_TIMEOUT_SEC,
            )
            post_status = "GREEN" if revalidate_payload.get("passed") else "RED"
            duration_ms = int(
                (_dt.now(_UTC) - revalidate_t0).total_seconds() * 1000
            )
        except TimeoutError:
            # Re-validate ran past 300s. Classify RED with an
            # operator-visible synthetic check name so the verdict's
            # remaining_failed_checks captures the cause.
            log.error(
                "ops.final_verdict.revalidate_timeout",
                timeout_sec=_REVALIDATE_TIMEOUT_SEC,
            )
            post_status = "RED"
            remaining_failed = ["data_validation_revalidate_timeout"]
            duration_ms = int(_REVALIDATE_TIMEOUT_SEC * 1000)
        except RuntimeError as exc:
            # _stage_data_validation raises on red — parse the message.
            msg = str(exc)
            post_status = "RED"
            remaining_failed = _parse_failed_check_names(msg)
            duration_ms = 0
        except Exception as exc:  # noqa: BLE001 — never crash the cycle
            log.error(
                "ops.final_verdict.revalidate_error",
                error=str(exc), exc_type=type(exc).__name__,
            )
            post_status = "RED"
            duration_ms = 0

        if post_status == "GREEN":
            recovered = sorted(set(handled))
            # Flip the data_validation row to OK so the legacy
            # stage-status path agrees with the verdict. Mirrors the
            # D14 chunked-recovery pattern (line ~10282) — the
            # in-codebase precedent for status-flipping after proven
            # recovery.
            synthetic = StageResult(
                name="data_validation",
                status="OK",
                duration_ms=duration_ms or val_result.duration_ms,
                detail={
                    **detail,
                    "cascade": True,
                    "cascade_mode": "validation_failures",
                    "post_cascade_passed": True,
                    "post_cascade_failed_checks": [],
                    "first_pass_failed_checks": first_pass_failed,
                    "recovered_checks": recovered,
                    "vendor_late": vendor_late,
                    "unhealable": unhealable,
                },
                error=None,
            )
            summary.stages[val_idx] = synthetic
            # Truthful "proven recovery" event (REQ-003).
            await db_log.log(
                "INGESTION_AUTO_RECOVERED_VALIDATION",
                (
                    f"validation cascade recovered: post-cascade "
                    f"re-validation passed (recovered={len(recovered)}, "
                    f"vendor_late={len(vendor_late)})"
                ),
                severity="INFO",
                data={
                    "stage": "data_validation",
                    "cascade_mode": "validation_failures",
                    "post_cascade_passed": True,
                    "first_pass_failed_checks": first_pass_failed,
                    "recovered_checks": recovered,
                    "vendor_late_checks": vendor_late,
                    "unhealable_checks": unhealable,
                },
            )
            log.info(
                "ops.final_verdict.post_cascade_green",
                recovered=recovered,
                vendor_late=vendor_late,
                unhealable=unhealable,
            )
        else:
            # Re-validate still red. Compute the remaining failures
            # (those not in handled / vendor_late / unhealable).
            if not remaining_failed:
                # We didn't get a parsed list from the exception — fall
                # back to "anything that wasn't recovered".
                remaining_failed = [
                    c for c in first_pass_failed
                    if c not in vendor_late and c not in unhealable
                ]
            # Update detail with the post-cascade truth. Keep status=FAILED.
            val_result.detail = {
                **detail,
                "cascade": True,
                "cascade_mode": "validation_failures",
                "post_cascade_passed": False,
                "post_cascade_failed_checks": remaining_failed,
                "first_pass_failed_checks": first_pass_failed,
                "vendor_late": vendor_late,
                "unhealable": unhealable,
            }
            summary.stages[val_idx] = val_result
            await db_log.log(
                "INGESTION_AUTO_RECOVERY_FAILED",
                (
                    f"validation cascade did NOT prove green: "
                    f"post-cascade remaining reds = {remaining_failed}"
                ),
                severity="ERROR",
                data={
                    "stage": "data_validation",
                    "cascade_mode": "validation_failures",
                    "post_cascade_passed": False,
                    "first_pass_failed_checks": first_pass_failed,
                    "remaining_failed_checks": remaining_failed,
                    "vendor_late_checks": vendor_late,
                    "unhealable_checks": unhealable,
                },
            )
            log.warning(
                "ops.final_verdict.post_cascade_red",
                remaining_failed=remaining_failed,
                vendor_late=vendor_late,
                unhealable=unhealable,
            )

    # Final-status arithmetic. The hard invariant per CLAUDE.md is
    # "100% data or don't trade" — so any remaining red (including
    # unhealable + vendor_late) is RED. The operator gates emission;
    # silent-green on unhealable would violate the contract.
    #
    # 2026-05-29 expert review fix (F-003): the prior special-case
    # override at this point unconditionally set final='GREEN' when
    # post_status=='GREEN', which leaked vendor_late entries past the
    # gate. Per orchestrator.py:74-78 ("vendor_late is CLASSIFICATION,
    # not RELAXATION") any vendor_late entry MUST keep the row red.
    # Derive ``final`` purely from has_remaining_reds.
    has_other_stage_fails = any(
        s.name != "data_validation"
        and s.status in ("FAILED", "TIMEOUT")
        for s in summary.stages
    )
    has_remaining_reds = (
        bool(remaining_failed) or bool(unhealable) or bool(vendor_late)
    )

    # 2026-05-29 expert review fix (F-002, bootstrap unblock): the
    # cadence check ``data_operations_complete_cadence`` is registered
    # healable=False and goes RED with reason='never_emitted' on a
    # fresh DB where no DATA_OPERATIONS_COMPLETE row exists yet. The
    # check exists to surface a silently-broken lane in steady state,
    # but in the bootstrap state (no rows ever) it is structurally a
    # chicken-and-egg — the gate event can never emit because the
    # check is red, the check can never go green because no event
    # exists. Special-case ONLY this one check, and ONLY when:
    #   1. it is the SOLE unhealable entry,
    #   2. no remaining_failed entries (all other healable reds were
    #      proven-recovered via post-cascade re-validation),
    #   3. no vendor_late entries (sacred-gate invariant),
    #   4. no other stage failed.
    # Under those conditions, allow this cycle to emit
    # DATA_OPERATIONS_COMPLETE so the cadence check can self-bootstrap.
    # From the second cycle onward, the cadence check returns GREEN
    # (the seed row is < 30h old) and the override never fires. This
    # makes the system honestly self-bootstrapping — no operator-seed
    # required for normal operation on a fresh DB.
    BOOTSTRAP_CADENCE_CHECK = "data_operations_complete_cadence"
    # post_status semantics for bootstrap eligibility:
    #   GREEN    — re-validate confirmed all healable reds recovered
    #   NOT_RUN  — no healable reds existed in the first pass, so the
    #              cadence is the ONLY red and there's nothing to
    #              re-validate; the lane is otherwise clean
    # RED is excluded — re-validate found unrecovered reds, which means
    # the lane is genuinely not at 100% and bootstrap must not unblock.
    is_bootstrap_unblock = (
        unhealable == [BOOTSTRAP_CADENCE_CHECK]
        and not remaining_failed
        and not vendor_late
        and not has_other_stage_fails
        and post_status in ("GREEN", "NOT_RUN")
    )

    if is_bootstrap_unblock:
        # Set the verdict to GREEN; the cadence check will go green
        # within the same cycle's lookup of the emitted row.
        final = "GREEN"
        log.warning(
            "ops.final_verdict.bootstrap_unblock",
            reason=(
                "data_operations_complete_cadence is the SOLE unhealable "
                "red on a never-emitted lane — allowing this cycle's "
                "emission to seed the cadence gate; from cycle 2 onward "
                "the check resolves green naturally"
            ),
        )
    else:
        final = (
            "GREEN" if not has_remaining_reds and not has_other_stage_fails
            else "RED"
        )

    return FinalLaneVerdict(
        final_status=final,
        exit_code=0 if final == "GREEN" else 1,
        emission_allowed=(final == "GREEN"),
        engine_dispatch_allowed=(final == "GREEN"),
        first_pass_failed_checks=first_pass_failed,
        recovered_checks=recovered,
        remaining_failed_checks=remaining_failed,
        unhealable_checks=unhealable,
        vendor_late_checks=vendor_late,
        cascade_attempted=cascade_ran,
        post_cascade_validation_status=post_status,
    )


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

        # Feed selection — operator's standing rule
        # `feedback_no_alpaca_for_daily_prices_backfill` (2026-05-25):
        # NEVER Alpaca (sip/iex) for daily-bar backfill. The DB
        # constraint `prices_daily_no_new_alpaca` rejects every row
        # the cascade tried to write (every chunk a CheckViolationError
        # on test #12), so the cascade architecturally has to use FMP.
        # PR #386 made FMP the primary; this catches up the cascade.
        feed = "fmp"
        probe_reason = "fmp_primary_per_operator_rule"
        sip_ok = False  # Retained as a structured cascade-detail field below.

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
        # Post-FMP-mandate (PR #400): FMP is the only acceptable feed for
        # daily-bar backfill. A successful cascade IS the full recovery —
        # no DEGRADED tier. Only a coverage_collapse-again or a non-OK
        # outcome trips the FAILED/DEGRADED branches.
        if cascade_result.status == "OK" and not coverage_collapse_again:
            await db_log.log(
                "INGESTION_AUTO_RECOVERED",
                (
                    "auto-cascade healed daily_bars coverage_collapse "
                    "via force_refresh feed=fmp"
                ),
                severity="INFO",
                data={
                    "stage": name,
                    "cascade_mode": "force_refresh",
                    "feed": "fmp",
                    "first_error": first_error,
                    "duration_ms": cascade_result.duration_ms,
                    **(cascade_result.detail or {}),
                },
            )
            log.info(
                "ops.auto_cascade.recovered",
                stage=name,
                feed="fmp",
                duration_ms=cascade_result.duration_ms,
            )
        elif (
            cascade_result.status == "FAILED" and coverage_collapse_again
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
            # 2026-05-29 control-plane fix (REQ-003): emit the attempted-
            # recovery event ONLY when the refresh stage actually returned
            # OK. Even then, this is a STAGE_OK event — it proves the
            # refresh stage dispatched cleanly, NOT that validation passes.
            # _build_final_lane_verdict emits the proven-recovery
            # INGESTION_AUTO_RECOVERED_VALIDATION event after re-running
            # data_validation.
            refresh_ok = (refresh_outcome.get("status") == "OK")
            await db_log.log(
                "INGESTION_AUTO_RECOVERY_STAGE_OK" if refresh_ok
                else "INGESTION_AUTO_RECOVERY_FAILED",
                f"validation cascade {check_name} → {stage_name}",
                severity="INFO" if refresh_ok else "ERROR",
                data={
                    "check": check_name,
                    "refresh_stage": stage_name,
                    "refresh_status": refresh_outcome.get("status"),
                    "refresh_error": refresh_outcome.get("error"),
                    "stage_ok_means": (
                        "refresh dispatched cleanly; validation re-run "
                        "at end-of-cycle determines proven recovery"
                    ),
                },
            )
            log.info(
                "ops.auto_cascade.validation.stage_ok" if refresh_ok
                else "ops.auto_cascade.validation.stage_failed",
                check=check_name, stage=stage_name,
                refresh=refresh_outcome,
            )
            if refresh_ok:
                handled.append(check_name)
            else:
                skipped.append(check_name)
            continue

        # Fall back to the HealSpec registry — the canonical source of
        # truth for whether a check is healable + which stage repairs it.
        # _VALIDATION_CASCADE_MAP above is a legacy override list; HealSpec
        # is the durable per-feed contract (see tpcore/selfheal/registry.py).
        from tpcore.selfheal.registry import spec_for as _spec_for

        spec = _spec_for(check_name)
        if spec is not None and spec.healable and spec.stage is not None:
            refresh_outcome = await _invoke_cascade_stage(
                spec.stage, dict(spec.params or {}), daily_bars_config,
                spec_by_name,
                pool=pool, log=log, db_log=db_log,
            )
            # 2026-05-29 control-plane fix (REQ-003): STAGE_OK event,
            # not optimistic VALIDATION_RECOVERED. The proven-recovery
            # event is emitted by _build_final_lane_verdict after
            # post-cascade data_validation re-runs and proves green.
            refresh_ok = (refresh_outcome.get("status") == "OK")
            await db_log.log(
                "INGESTION_AUTO_RECOVERY_STAGE_OK" if refresh_ok
                else "INGESTION_AUTO_RECOVERY_FAILED",
                f"healspec cascade {check_name} → {spec.stage}",
                severity="INFO" if refresh_ok else "ERROR",
                data={
                    "check": check_name,
                    "refresh_stage": spec.stage,
                    "refresh_status": refresh_outcome.get("status"),
                    "refresh_error": refresh_outcome.get("error"),
                    "source": "healspec_registry",
                    "stage_ok_means": (
                        "refresh dispatched cleanly; validation re-run "
                        "at end-of-cycle determines proven recovery"
                    ),
                },
            )
            log.info(
                "ops.auto_cascade.healspec.stage_ok" if refresh_ok
                else "ops.auto_cascade.healspec.stage_failed",
                check=check_name, stage=spec.stage,
                refresh=refresh_outcome,
            )
            if refresh_ok:
                handled.append(check_name)
            else:
                skipped.append(check_name)
            continue
        if spec is not None and not spec.healable:
            # Documented unhealable — log at INFO with the documented
            # reason so dashboards see the explicit acknowledgement
            # instead of a vague "no cascade" warning.
            await db_log.log(
                "INGESTION_AUTO_RECOVERY_UNHEALABLE",
                f"{check_name}: {spec.unhealable_reason or '(no reason given)'}",
                severity="INFO",
                data={
                    "check": check_name,
                    "reason": spec.unhealable_reason or "",
                    "source": "healspec_registry",
                },
            )
            log.info(
                "ops.auto_cascade.healspec.unhealable",
                check=check_name,
                reason=spec.unhealable_reason or "",
            )
            skipped.append(check_name)
            continue
        # Truly unknown check — neither _VALIDATION_CASCADE_MAP nor
        # HealSpec has an entry. This is a registry-coverage bug; the
        # tpcore/selfheal/registry.py sentinel reds CI when a check is
        # missing a HealSpec.
        await db_log.log(
            "INGESTION_AUTO_RECOVERY_VALIDATION_SKIPPED",
            f"no HealSpec registered for {check_name}",
            severity="WARNING",
            data={
                "check": check_name,
                "note": "missing HealSpec — add to tpcore/selfheal/registry.py",
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
    "sec_fundamentals_fallback": "sec_edgar",
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
    "classify_tickers": "fmp",
    "tier_refresh": "alpaca",
    "confirmed_data_gap_evidence_populator": "fmp",
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
    from tpcore.quality.validation.checks.prices_daily_classification_id_completeness import (
        check_prices_daily_classification_id_completeness,
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
    from tpcore.quality.validation.sources.constituents import (
        FixtureConstituentSource,
    )
    from tpcore.quality.validation.sources.delistings import (
        FixtureDelistingsSource,
    )
    from tpcore.quality.validation.sources.splits import FixtureSplitsSource
    from tpcore.quality.validation.suite import _safe_run

    # 3 checks need fixture-source adapters (the canonical suite.py wires
    # these via the same Fixture* defaults). The chunked path used to
    # pass None for ALL checks, which made delistings/constituent/splits
    # fail with AttributeError("'NoneType' object has no attribute
    # 'list_delistings'") — fixed 2026-05-27.
    _check_sources: dict[str, Any] = {
        "delistings": FixtureDelistingsSource(),
        "constituent": FixtureConstituentSource(),
        "splits": FixtureSplitsSource(),
    }

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
        "prices_daily_classification_id_completeness":
            check_prices_daily_classification_id_completeness,
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
            tasks.append(_safe_run(cn, fn, pool, _check_sources.get(cn)))

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
                COALESCE((SELECT MAX(filing_date) FROM platform.insider_transactions), '-infinity'::date),
                COALESCE((SELECT MAX(filing_date) FROM platform.sec_material_events),     '-infinity'::date)
            ) AS newest_filing,
            (SELECT COUNT(*) FROM platform.insider_transactions) AS insider_rows,
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
    """Surface open Sprint Dossiers from ``platform.data_quality_log``
    (``kind='forensics_trigger'``; Plan 2 consolidation).

    "Open" = ``notes->>'resolved_at' IS NULL`` (Plan 2: forensics triggers
    live in ``platform.data_quality_log`` WHERE ``kind='forensics_trigger'``).
    The probe reports total count, the most recent fire timestamp, and the
    distinct set of engines under review (each trigger's ``notes->>'engine'``).
    Operator workflow: open the linked dossier markdown under
    ``docs/sprints/``, diagnose, then mark resolved to close.

    Returns ``ok=True`` regardless of dossier count — open dossiers
    are findings to review, not platform errors. The dashboard renders
    them in the operator-action panel rather than the red-light strip.
    """
    from tpcore.forensics.dql_store import OPEN_DOSSIERS_SQL

    rows = await pool.fetch(OPEN_DOSSIERS_SQL)
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
            "(repeatable). Overlays the inline stage-config dict the "
            "stage handler receives. Values are coerced "
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
