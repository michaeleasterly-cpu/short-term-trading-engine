"""Maintenance CLI for the Short-Term Trading Engine platform.

Three top-level commands:

    python scripts/ops.py --update          # run the 5 maintenance stages
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
    row = await pool.fetchrow(
        "SELECT config FROM platform.ingestion_jobs WHERE job_name = 'daily_bars'"
    )
    if row is None:
        raise RuntimeError(
            "ops: platform.ingestion_jobs has no row for job_name='daily_bars'. "
            "Seed it before running --update. Example:\n"
            "  INSERT INTO platform.ingestion_jobs (job_name, schedule, provider, config) "
            "VALUES ('daily_bars', '@daily', 'alpaca', "
            "'{\"universe\": \"all_active\", \"lookback_days\": 7, "
            "\"min_price\": 5, \"min_volume\": 250000}'::jsonb);"
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
    except asyncio.TimeoutError:
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
    from tpcore.ingestion.handlers import handle_daily_bars

    rows = await handle_daily_bars(pool, config)
    return {"rows_upserted": rows or 0, "universe": config.get("universe", "active")}


async def _stage_corporate_actions(pool: asyncpg.Pool) -> dict[str, Any]:
    from tpcore.ingestion.handlers import handle_corporate_actions

    rows = await handle_corporate_actions(pool, {"universe": "all_active"})
    return {"actions_ingested": rows or 0}


async def _stage_fundamentals_refresh(
    pool: asyncpg.Pool, config: dict[str, Any]
) -> dict[str, Any]:
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
        rows, no_data, failures = await cache.backfill_all(tickers=tickers)

    detail = {
        "tickers": len(tickers),
        "rows": rows,
        "no_data": len(no_data),
        "failures": len(failures),
    }
    if failures:
        # Match handler semantics: real FMP failures surface as an error
        # event for the stage, but the pipeline still continues.
        raise RuntimeError(
            f"fundamentals_refresh: {len(failures)} failure(s); "
            f"first={failures[0][0]}: {failures[0][1]}"
        )
    return detail


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


_CANDIDATE_RE = re.compile(r"^\s*(\w[\w ]*?)\s+candidates?:\s*(\d+)", re.MULTILINE)


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
            f"simulate_universe exited {proc.returncode}: "
            f"{stderr.strip()[:200] or 'no stderr'}"
        )
    return {"exit_code": proc.returncode, **{f"{k}_candidates": v for k, v in counts.items()}}


# ────────────────────────────────────────────────────────────────────────
# --update orchestrator
# ────────────────────────────────────────────────────────────────────────

async def cmd_update(
    pool: asyncpg.Pool,
    log: structlog.stdlib.BoundLogger,
    db_log,
    *,
    dry_run: bool,
) -> UpdateSummary:
    started_at = datetime.now(UTC)
    summary = UpdateSummary(run_id=db_log._run_id, started_at=started_at, finished_at=started_at)

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
        summary.stages.append(
            StageResult(name="config_load", status="FAILED", duration_ms=0, error=str(exc))
        )
        summary.finished_at = datetime.now(UTC)
        return summary

    summary.stages.append(
        await _run_stage(
            "daily_bars",
            lambda: _stage_daily_bars(pool, daily_bars_config),
            log=log,
            db_log=db_log,
            dry_run=dry_run,
        )
    )
    summary.stages.append(
        await _run_stage(
            "corporate_actions",
            lambda: _stage_corporate_actions(pool),
            log=log,
            db_log=db_log,
            dry_run=dry_run,
        )
    )
    summary.stages.append(
        await _run_stage(
            "fundamentals_refresh",
            lambda: _stage_fundamentals_refresh(pool, daily_bars_config),
            log=log,
            db_log=db_log,
            dry_run=dry_run,
        )
    )
    summary.stages.append(
        await _run_stage(
            "data_validation",
            lambda: _stage_data_validation(pool),
            log=log,
            db_log=db_log,
            dry_run=dry_run,
        )
    )
    summary.stages.append(
        await _run_stage(
            "universe_simulation",
            _stage_simulate_universe,
            log=log,
            db_log=db_log,
            dry_run=dry_run,
        )
    )
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
    engines = {
        r["engine"]: r["latest_startup"].isoformat() if r["latest_startup"] else None
        for r in rows
    }
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
        (r["job"] or "<no-stage>"): r["latest_complete"].isoformat()
        for r in rows
        if r["latest_complete"]
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
    cohort = [
        r for r in rows
        if (latest_ts - r["timestamp"]).total_seconds() <= 600
    ]
    passed = all(
        float(r["confidence"]) >= 1.0 and (r["notes"] in (None, "[]", []) or r["notes"] == [])
        for r in cohort
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
    ("engine_schedulers", _check_engine_schedulers),
    ("ingestion_engine", _check_ingestion_engine),
    ("validation_suite", _check_validation),
    ("risk_governor", _check_risk_governor),
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
# Argparse + entry point
# ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ops.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/ops.py --update              # run all 5 maintenance stages\n"
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
    p.add_argument("--dry-run", action="store_true", help="log without writing data")
    p.add_argument("--pretty", action="store_true", help="pretty-print --check output")
    return p


async def amain(args: argparse.Namespace) -> int:
    from tpcore.db import build_asyncpg_pool
    from tpcore.logging.db_handler import DBLogHandler

    run_id = uuid.uuid4()
    log = _configure_logging(run_id)

    if args.update or args.full:
        _require_env(["DATABASE_URL", "FMP_API_KEY"])
        _require_alpaca_env()
    else:
        _require_env(["DATABASE_URL"])

    db_url = os.environ["DATABASE_URL"]
    pool = await build_asyncpg_pool(db_url, max_size=4)
    db_log = DBLogHandler(pool, engine=ENGINE_NAME, run_id=run_id)
    started = time.monotonic()
    exit_code = 0

    try:
        await db_log.log(
            "STARTUP",
            f"ops CLI starting (update={args.update} check={args.check} full={args.full} dry_run={args.dry_run})",
            severity="INFO",
            data={
                "argv": sys.argv,
                "dry_run": args.dry_run,
                "mode": "update" if args.update else ("full" if args.full else "check"),
            },
        )
        log.info("ops.start", mode="update" if args.update else ("full" if args.full else "check"))

        update_summary: UpdateSummary | None = None
        if args.update or args.full:
            update_summary = await cmd_update(pool, log, db_log, dry_run=args.dry_run)
            print("\nUPDATE SUMMARY")
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
