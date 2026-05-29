"""Live data-pipeline console substrate.

Replaces the hardcoded ``/api/data-pipeline`` stub that shipped on
2026-05-25 with real Postgres-backed queries + on-demand operator
action endpoints. Authoritative sources:

  * ``platform.application_log`` — event bus (run lifecycle,
    operator-triggered runs, auto-recovery, DATA_OPERATIONS_COMPLETE).
  * ``platform.data_quality_log`` — durable validation detector
    substrate. Latest-per-source rows drive the validation table.
  * ``platform.prices_daily`` — KPI rollups (60-day bar count,
    distinct-tickers tracked).
  * ``platform.daemon_heartbeats`` — daemon liveness for the cycle-
    latency / next-run KPIs.

Architecture (operator-triggered runs):

  1. Browser → ``console-api`` ``POST /api/operations/data-pipeline/
     run-{update,validation,feed}`` with NextAuth session cookie.
  2. ``console-api`` writes an ``OPERATOR_RUN_REQUESTED`` row to
     ``application_log`` (engine='ops_console', run_id=uuid,
     data={actor, action, params}). This is the audit row + the
     dispatch signal.
  3. The deployed ``lane_service`` daemon polls for
     ``OPERATOR_RUN_REQUESTED`` (see ``ops/operator_trigger_lane.py``
     under same-PR scope), acquires
     ``pg_try_advisory_lock(hashtext('data_ops_run'))``, and shells
     out to ``scripts/run_data_operations.sh`` (or
     ``python scripts/ops.py --stage <name>`` for per-feed). On
     completion it emits ``OPERATOR_RUN_COMPLETED`` (or
     ``OPERATOR_RUN_FAILED``) with exit_code + duration.
  4. The browser polls ``GET /api/operations/data-pipeline/jobs/{
     job_id}`` and re-fetches the status endpoint after terminal.

Concurrency: ``pg_advisory_lock`` is the single source of truth.
The console action endpoints DO NOT acquire the lock themselves —
they enqueue the OPERATOR_RUN_REQUESTED row. The lane daemon
arbitrates. A second click while a run is active receives the same
job_id (idempotent insert via the request_key UUID4 from the
browser) and the response indicates queued/conflict.

No false green: this module never derives validation status from
the UI side; the data_quality_log row is dispositive. If the
backend has no row, the check renders ``UNKNOWN``.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg


def _load_json(value: Any) -> dict[str, Any]:
    """asyncpg returns JSONB as str unless the pool installs a codec.
    Defensively coerce both shapes to dict — empty dict on falsey / parse
    failure."""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


# ──────────────────────── constants ────────────────────────


# Validation checks the console renders rows for. Ordered for the
# operator's at-a-glance sweep. Source-name predicate on
# data_quality_log is ``validation.%`` per the durable-detector
# convention; we additionally accept the bare check name (handlers
# upstream are inconsistent — both conventions appear in production).
CONSOLE_VALIDATION_CHECKS: list[str] = [
    "prices_daily_completeness",
    "prices_daily_freshness",
    "fundamentals_quarterly_completeness",
    "corporate_actions_completeness",
    "macro_indicators_completeness",
    "macro_indicators_freshness",
    "earnings_events_freshness",
    "earnings_events_monotone",
    "sec_filings_freshness",
    "sec_insider_monotone",
    "options_max_pain_freshness",
    "ticker_history_integrity",
    "ticker_classifications_coverage",
    "data_operations_complete_cadence",
    "daemon_freshness",
    "aaii_sentiment_freshness",
    "social_sentiment_freshness",
    "short_interest_freshness",
    "tradier_options_chain",
    "alpaca_corporate_actions",
    "ingest_manifest_loaded",
    "ingest_quarantine_review",
    "issuer_history_integrity",
    "issuer_securities_integrity",
    "corporate_events_integrity",
]


# Allowlist for per-feed re-run actions. Keys are user-visible action
# names; values are the canonical ``scripts/ops.py --stage`` name. Any
# stage not in this map cannot be triggered from the console — the
# operator must run it from a wrapper script directly. Sourced from
# the data-adapter pipeline canonical roster.
RUN_FEED_ALLOWLIST: dict[str, str] = {
    "daily_bars": "daily_bars",
    "data_validation": "data_validation",
    "auditheal": "auditheal",
    "ticker_classifications": "ticker_classifications",
    "fundamentals": "fundamentals",
    "corporate_actions": "corporate_actions",
    "macro_data": "macro_data",
    "earnings_events": "earnings_events",
    "sec_filings": "sec_filings",
    "sec_insider": "sec_insider",
    "options_max_pain": "options_max_pain",
    "insider_sentiment": "insider_sentiment",
    "social_sentiment": "social_sentiment",
    "aaii_sentiment": "aaii_sentiment",
    "short_interest": "short_interest",
    "tradier_options": "tradier_options",
    "alpaca_corporate_actions": "alpaca_corporate_actions",
    "forensics": "forensics",
}


# How fresh a DATA_OPERATIONS_COMPLETE row must be before the lane is
# treated as STALE. Daily cadence is 21:30 UTC weekdays. A 30 h window
# is the operator's normal expectation (30 h covers Friday → Monday
# implicitly: Friday's 21:30 row is 6 h old at Saturday 03:30; by
# Monday the next emission is due). Beyond 30 h on a weekday the
# operator should see STALE explicitly.
DATA_OPS_FRESHNESS_HOURS = 30


# Active-run discovery: an OPERATOR_RUN_REQUESTED row without a
# matching terminal (OPERATOR_RUN_COMPLETED / FAILED / ABORTED) within
# the watchdog window is treated as RUNNING; beyond the window it's
# STALE_RUN (operator should manually abort).
ACTIVE_RUN_WATCHDOG_MINUTES = 90


# Server-side identity for OPERATOR_RUN_REQUESTED rows.
OPERATOR_RUN_ENGINE = "ops_console"


# ──────────────────────── status query ────────────────────────


async def fetch_status_payload(pool: asyncpg.Pool) -> dict[str, Any]:
    """Build the GET /api/operations/data-pipeline/status payload from
    LIVE Postgres state. Never falls back to defaults that would imply
    green when the DB is silent — silent → UNKNOWN."""
    async with pool.acquire() as conn:
        kpis = await _fetch_kpis(conn)
        checks = await _fetch_validation_rows(conn)
        self_heal = await _fetch_self_heal_log(conn)
        active_job = await _fetch_active_job(conn)
        latest_doc = await _fetch_latest_data_ops_event(conn)
        forensics_open = await _fetch_forensics_open(conn)
        last_run_id = await _fetch_last_run_id(conn)

    # Top-level lane status derivation.
    lane_status = _derive_lane_status(
        active_job=active_job,
        latest_doc=latest_doc,
        checks=checks,
    )

    return {
        "status": lane_status,
        "last_refreshed_at": datetime.now(UTC).isoformat(),
        "latest_run_id": str(last_run_id) if last_run_id else None,
        "latest_data_ops_event": _build_data_ops_event_block(latest_doc),
        "summary": {
            **kpis,
            "forensics_open": forensics_open,
        },
        "checks": checks,
        "self_heal_log": self_heal,
        "active_job": active_job,
    }


async def _fetch_kpis(conn: asyncpg.Connection) -> dict[str, Any]:
    """Live KPIs from prices_daily + data_quality_log."""
    prices_count = await conn.fetchval(
        "SELECT COUNT(*) FROM platform.prices_daily "
        "WHERE date >= CURRENT_DATE - INTERVAL '60 days'"
    )
    tickers_count = await conn.fetchval(
        "SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily "
        "WHERE date >= CURRENT_DATE - INTERVAL '7 days'"
    )
    # Validation pass/warn/fail rollup from the latest-per-source rows
    # of data_quality_log. PASS = not stale AND confidence >= 1.0;
    # FAIL = stale OR confidence < 0.5; WARN otherwise.
    counts = await conn.fetchrow(
        """
        WITH latest AS (
            SELECT DISTINCT ON (source) source, stale, confidence
            FROM platform.data_quality_log
            WHERE source LIKE 'validation.%'
              AND timestamp >= NOW() - INTERVAL '36 hours'
            ORDER BY source, timestamp DESC
        )
        SELECT
            COUNT(*) FILTER (WHERE NOT stale AND confidence >= 1.0)
                AS passed,
            COUNT(*) FILTER (WHERE NOT stale
                AND confidence >= 0.5 AND confidence < 1.0)
                AS warnings,
            COUNT(*) FILTER (WHERE stale OR confidence < 0.5)
                AS failed,
            COALESCE(MIN(confidence), 0) AS min_confidence
        FROM latest
        """
    )
    # In production aggregations always return one row; defensive
    # None-handling here so a fake pool / migration-state DB doesn't
    # blow up the endpoint.
    if counts is None:
        passed = warnings = failed = 0
        min_conf = 0.0
    else:
        passed = int(counts["passed"] or 0)
        warnings = int(counts["warnings"] or 0)
        failed = int(counts["failed"] or 0)
        min_conf = float(counts["min_confidence"] or 0)
    confidence_pct = f"{int(round(min_conf * 100))}%" if min_conf else "—"

    # Cycle latency = wall-clock between latest INGESTION_START and
    # latest DATA_OPERATIONS_COMPLETE (or current time if no DOC yet).
    cycle_latency = await _fetch_cycle_latency(conn)

    return {
        "passed": passed,
        "warnings": warnings,
        "failed": failed,
        "confidence": confidence_pct,
        "tickers_tracked": int(tickers_count or 0),
        "daily_bars_60d": int(prices_count or 0),
        "cycle_latency": cycle_latency,
    }


async def _fetch_cycle_latency(conn: asyncpg.Connection) -> str:
    """Wall-clock between the latest INGESTION_START and the matching
    INGESTION_COMPLETE / FAILED for the same run_id. Returns a
    human-readable string or '—' if neither side is present."""
    row = await conn.fetchrow(
        """
        WITH starts AS (
            SELECT run_id, recorded_at AS started_at
            FROM platform.application_log
            WHERE event_type = 'INGESTION_START'
            ORDER BY recorded_at DESC
            LIMIT 1
        ),
        ends AS (
            SELECT run_id, MAX(recorded_at) AS ended_at
            FROM platform.application_log
            WHERE event_type IN (
                'INGESTION_COMPLETE',
                'INGESTION_FAILED',
                'DATA_OPERATIONS_COMPLETE'
            )
            GROUP BY run_id
        )
        SELECT s.started_at, e.ended_at
        FROM starts s
        LEFT JOIN ends e ON e.run_id = s.run_id
        """
    )
    if row is None or row["started_at"] is None:
        return "—"
    end = row["ended_at"] or datetime.now(UTC)
    delta = end - row["started_at"]
    secs = int(delta.total_seconds())
    if secs < 0:
        return "—"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


async def _fetch_validation_rows(
    conn: asyncpg.Connection,
) -> list[dict[str, Any]]:
    """Latest-per-source rows for the console-tracked validation checks.
    A row missing from data_quality_log renders as ``UNKNOWN`` rather
    than ``PASS`` — silence is not success.

    Source-name convention (verified 2026-05-29): validation checks are
    written under ``validation.<check_name>`` in data_quality_log. We
    look up BOTH the prefixed and unprefixed name to be robust to
    handler-side inconsistency."""
    candidate_sources: list[str] = []
    for name in CONSOLE_VALIDATION_CHECKS:
        candidate_sources.append(name)
        candidate_sources.append(f"validation.{name}")
    rows = await conn.fetch(
        """
        WITH latest AS (
            SELECT DISTINCT ON (source) source, timestamp, stale,
                confidence, missing_bars, notes
            FROM platform.data_quality_log
            WHERE source = ANY($1::text[])
              AND timestamp >= NOW() - INTERVAL '72 hours'
            ORDER BY source, timestamp DESC
        )
        SELECT * FROM latest ORDER BY source
        """,
        candidate_sources,
    )
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        # Normalize the source key — strip the validation. prefix when
        # present so the CONSOLE_VALIDATION_CHECKS lookup below matches.
        key = r["source"]
        if key.startswith("validation."):
            key = key[len("validation."):]
        seen[key] = dict(r)

    out: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for name in CONSOLE_VALIDATION_CHECKS:
        row = seen.get(name)
        if row is None:
            out.append({
                "name": name,
                "status": "UNKNOWN",
                "rows": None,
                "age": None,
                "notes": "no data_quality_log row in last 72 h",
                "last_checked_at": None,
                "healable": _check_healable(name),
                "actionable": name in RUN_FEED_ALLOWLIST,
                "allowed_actions": _check_allowed_actions(name),
            })
            continue
        status = _classify_validation_row(row)
        out.append({
            "name": name,
            "status": status,
            "rows": int(row["missing_bars"]) if row["missing_bars"] is not None else None,
            "age": _age_str(now, row["timestamp"]),
            "notes": row["notes"] or "",
            "last_checked_at": row["timestamp"].isoformat(),
            "healable": _check_healable(name),
            "actionable": name in RUN_FEED_ALLOWLIST,
            "allowed_actions": _check_allowed_actions(name),
        })
    return out


def _classify_validation_row(row: dict[str, Any]) -> str:
    """Apply the data-acceptance gate rule. Stale OR low confidence is
    FAIL; medium confidence is WARN; high confidence is PASS."""
    stale = bool(row.get("stale"))
    conf = float(row.get("confidence") or 0)
    if stale or conf < 0.5:
        return "FAIL"
    if conf < 1.0:
        return "WARN"
    return "PASS"


def _check_healable(name: str) -> bool:
    """Best-effort lookup against the HealSpec registry. Returns True
    when the check has a healable=True spec, False when healable=False,
    True (optimistic — operator may still want a manual rerun) when
    unmapped. Conservative: leaning toward 'show a Run feed button'
    rather than hiding remediation."""
    try:
        from tpcore.selfheal.registry import spec_for  # type: ignore
        sp = spec_for(name)
        if sp is None:
            return name in RUN_FEED_ALLOWLIST
        return bool(sp.healable)
    except Exception:  # noqa: BLE001 — registry not installed in the
        # console-api deploy is the normal case; degrade gracefully.
        return name in RUN_FEED_ALLOWLIST


def _check_allowed_actions(name: str) -> list[str]:
    """Per-row action list. Always includes ``view_logs``; adds
    ``run_feed`` if the stage is allowlisted; adds ``view_forensics``
    for the forensics row."""
    actions = ["view_logs"]
    if name in RUN_FEED_ALLOWLIST:
        actions.append("run_feed")
    actions.append("run_validation")
    return actions


def _age_str(now: datetime, ts: datetime) -> str:
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "future"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


async def _fetch_self_heal_log(
    conn: asyncpg.Connection,
) -> list[dict[str, Any]]:
    """Self-heal events from application_log — INGESTION_AUTO_* + the
    operator-trigger lifecycle. Truncated to 24 h, ordered desc."""
    rows = await conn.fetch(
        """
        SELECT recorded_at, event_type, message, severity, data
        FROM platform.application_log
        WHERE recorded_at >= NOW() - INTERVAL '24 hours'
          AND (
            event_type LIKE 'INGESTION_AUTO_%'
            OR event_type LIKE 'OPERATOR_RUN_%'
          )
        ORDER BY recorded_at DESC
        LIMIT 30
        """
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "time": r["recorded_at"].isoformat(),
            "stage": _load_json(r["data"]).get("stage")
                or _load_json(r["data"]).get("check") or "—",
            "result": _self_heal_result(r["event_type"]),
            "duration": _self_heal_duration(r["data"]),
            "notes": r["message"] or "",
            "severity": r["severity"],
            "event_type": r["event_type"],
        })
    return out


def _self_heal_result(event_type: str) -> str:
    if "RECOVERED" in event_type or "COMPLETED" in event_type:
        return "HEALED"
    if "FAILED" in event_type or "ABORTED" in event_type:
        return "FAILED"
    if "ESCALATED" in event_type:
        return "ESCALATED"
    if "SKIPPED" in event_type or "STAGE_OK" in event_type:
        return "SKIPPED"
    return "INFO"


def _self_heal_duration(data: Any) -> str | None:
    parsed = _load_json(data)
    if not parsed:
        return None
    for k in ("duration_ms", "duration_seconds", "duration_s"):
        v = parsed.get(k)
        if v is not None:
            if k == "duration_ms":
                return f"{int(v)}ms" if int(v) < 1000 else f"{int(v)//1000}s"
            return f"{int(v)}s"
    return None


async def _fetch_active_job(
    conn: asyncpg.Connection,
) -> dict[str, Any] | None:
    """Returns the currently active OPERATOR_RUN_* run, OR the
    in-flight cron run if any. Returns None when nothing is active."""
    # Active operator-triggered run = OPERATOR_RUN_REQUESTED without
    # OPERATOR_RUN_COMPLETED/FAILED/ABORTED for same run_id.
    op_row = await conn.fetchrow(
        """
        SELECT run_id, recorded_at, message, data
        FROM platform.application_log
        WHERE event_type = 'OPERATOR_RUN_REQUESTED'
          AND recorded_at >= NOW() - INTERVAL '6 hours'
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log t
            WHERE t.run_id = platform.application_log.run_id
              AND t.event_type IN (
                'OPERATOR_RUN_COMPLETED',
                'OPERATOR_RUN_FAILED',
                'OPERATOR_RUN_ABORTED'
              )
          )
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    )
    cron_row = await conn.fetchrow(
        """
        SELECT run_id, recorded_at, data
        FROM platform.application_log
        WHERE event_type = 'INGESTION_START'
          AND recorded_at >= NOW() - INTERVAL '90 minutes'
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log t
            WHERE t.run_id = platform.application_log.run_id
              AND t.event_type IN (
                'INGESTION_COMPLETE',
                'INGESTION_FAILED',
                'DATA_OPERATIONS_COMPLETE'
              )
          )
        ORDER BY recorded_at DESC
        LIMIT 1
        """
    )
    if op_row is None and cron_row is None:
        return None
    chosen = op_row if op_row is not None else cron_row
    is_operator = op_row is not None
    started_at = chosen["recorded_at"]
    now = datetime.now(UTC)
    elapsed = int((now - started_at).total_seconds())
    watchdog = ACTIVE_RUN_WATCHDOG_MINUTES * 60
    if elapsed > watchdog:
        # Stale — beyond the watchdog window, treat as TIMEOUT until
        # the operator manually clears.
        status = "TIMEOUT"
    else:
        status = "RUNNING"
    return {
        "job_id": str(chosen["run_id"]),
        "run_id": str(chosen["run_id"]),
        "type": _load_json(chosen["data"]).get("action") if is_operator
        else "scheduled_cron",
        "status": status,
        "started_at": started_at.isoformat(),
        "updated_at": now.isoformat(),
        "elapsed_seconds": elapsed,
        "current_stage": await _fetch_current_stage(conn, chosen["run_id"]),
        "current_check": None,
        "completed_stages": await _fetch_completed_stages(conn, chosen["run_id"]),
        "pending_stages": [],
        "failed_stage": None,
        "latest_log": await _fetch_latest_log_event(conn, chosen["run_id"]),
        "progress": await _fetch_progress(conn, chosen["run_id"]),
        "triggered_by": "operator" if is_operator else "cron",
    }


async def _fetch_current_stage(
    conn: asyncpg.Connection, run_id: uuid.UUID
) -> str | None:
    row = await conn.fetchrow(
        """
        SELECT data->>'stage' AS stage, event_type, recorded_at
        FROM platform.application_log
        WHERE run_id = $1
          AND (data ? 'stage' OR event_type LIKE '%_STAGE_%')
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        run_id,
    )
    if row is None:
        return None
    return row["stage"]


async def _fetch_completed_stages(
    conn: asyncpg.Connection, run_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT data, event_type, recorded_at
        FROM platform.application_log
        WHERE run_id = $1
          AND event_type IN (
            'INGESTION_COMPLETE',
            'INGESTION_AUTO_RECOVERY_STAGE_OK',
            'INGESTION_AUTO_RECOVERED_VALIDATION',
            'INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED'
          )
        ORDER BY recorded_at ASC
        """,
        run_id,
    )
    out = []
    for r in rows:
        d = _load_json(r["data"])
        out.append({
            "stage": d.get("stage") or d.get("check") or r["event_type"],
            "status": "SUCCESS",
            "started_at": None,
            "completed_at": r["recorded_at"].isoformat(),
            "duration_seconds": None,
            "rows_processed": d.get("rows_processed"),
            "message": None,
        })
    return out


async def _fetch_latest_log_event(
    conn: asyncpg.Connection, run_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT recorded_at, event_type, severity, message
        FROM platform.application_log
        WHERE run_id = $1
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        run_id,
    )
    if row is None:
        return None
    return {
        "time": row["recorded_at"].isoformat(),
        "event_type": row["event_type"],
        "severity": row["severity"],
        "message": row["message"] or "",
    }


async def _fetch_progress(
    conn: asyncpg.Connection, run_id: uuid.UUID
) -> dict[str, Any]:
    # The data-ops script has 15 declared stages. Without per-stage
    # START events we can't compute a true percent — fall back to a
    # stage-count proxy.
    completed = await conn.fetchval(
        """
        SELECT COUNT(DISTINCT data->>'stage')
        FROM platform.application_log
        WHERE run_id = $1
          AND event_type = 'INGESTION_COMPLETE'
        """,
        run_id,
    )
    completed = int(completed or 0)
    total = 15
    percent = int((completed / total) * 100) if total else None
    return {
        "stages_total": total,
        "stages_completed": completed,
        "percent": min(percent or 0, 99) if completed < total else 100,
        "label": f"{completed} / {total} stages",
    }


async def _fetch_latest_data_ops_event(
    conn: asyncpg.Connection,
) -> datetime | None:
    return await conn.fetchval(
        "SELECT MAX(recorded_at) FROM platform.application_log "
        "WHERE event_type = 'DATA_OPERATIONS_COMPLETE'"
    )


async def _fetch_forensics_open(conn: asyncpg.Connection) -> int:
    """F-006 fix (2026-05-29 expert review): the rest of
    fetch_status_payload defensively handles missing tables / NULL
    rows; this one query was unguarded against a DB where
    forensics_triggers doesn't exist (would 500 the whole endpoint).
    Match the defensive pattern — on UndefinedTableError (or any
    Postgres exception), return 0 and let the lane render."""
    try:
        val = await conn.fetchval(
            "SELECT COUNT(*) FROM platform.forensics_triggers "
            "WHERE resolved_at IS NULL"
        )
    except Exception:  # noqa: BLE001 — degrade gracefully on missing table
        return 0
    return int(val or 0)


async def _fetch_last_run_id(
    conn: asyncpg.Connection,
) -> uuid.UUID | None:
    return await conn.fetchval(
        "SELECT run_id FROM platform.application_log "
        "WHERE event_type IN ('DATA_OPERATIONS_COMPLETE', 'INGESTION_START') "
        "ORDER BY recorded_at DESC LIMIT 1"
    )


def _build_data_ops_event_block(
    latest_doc: datetime | None,
) -> dict[str, Any]:
    if latest_doc is None:
        return {
            "recorded_at": None,
            "event_type": "DATA_OPERATIONS_COMPLETE",
            "status": "MISSING",
        }
    now = datetime.now(UTC)
    delta = now - latest_doc
    status = "STALE" if delta.total_seconds() > DATA_OPS_FRESHNESS_HOURS * 3600 else "OK"
    return {
        "recorded_at": latest_doc.isoformat(),
        "event_type": "DATA_OPERATIONS_COMPLETE",
        "status": status,
    }


def _derive_lane_status(
    *,
    active_job: dict | None,
    latest_doc: datetime | None,
    checks: list[dict[str, Any]],
) -> str:
    if active_job is not None and active_job.get("status") == "RUNNING":
        return "RUNNING"
    if active_job is not None and active_job.get("status") == "TIMEOUT":
        return "RED"
    # No active run — derive from check rollup.
    statuses = [c["status"] for c in checks]
    if all(s == "UNKNOWN" for s in statuses):
        return "UNKNOWN"
    if any(s == "FAIL" for s in statuses):
        return "RED"
    # Even if no FAIL, a missing/stale DATA_OPERATIONS_COMPLETE means
    # the gate hasn't emitted recently. Per the no-false-green rule,
    # surface as WARNING (not RED — checks themselves are green).
    if latest_doc is None:
        return "WARNING"
    now = datetime.now(UTC)
    if (now - latest_doc).total_seconds() > DATA_OPS_FRESHNESS_HOURS * 3600:
        return "WARNING"
    if any(s == "WARN" or s == "UNKNOWN" for s in statuses):
        return "WARNING"
    return "GREEN"


# ──────────────────────── operator actions ────────────────────────


class ConflictError(Exception):
    """Raised when an operator action would overlap an active run."""


async def request_operator_run(
    pool: asyncpg.Pool,
    *,
    actor: str,
    action: str,
    stage: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert an OPERATOR_RUN_REQUESTED row to enqueue an operator-
    triggered run. The lane daemon picks it up out-of-band.

    Returns the job descriptor: {job_id, action, queued_at, status='QUEUED'}.

    Raises ConflictError when an unresolved active run already exists
    (HTTP 409 surface at the endpoint)."""
    if action == "run_update":
        canonical_stage = None
    elif action == "run_validation":
        canonical_stage = "data_validation"
    elif action == "run_feed":
        if stage is None:
            raise ValueError("run_feed requires stage")
        if stage not in RUN_FEED_ALLOWLIST:
            raise ValueError(
                f"stage {stage!r} not in RUN_FEED_ALLOWLIST"
            )
        canonical_stage = RUN_FEED_ALLOWLIST[stage]
    else:
        raise ValueError(f"unknown action {action!r}")

    job_id = uuid.uuid4()
    # F-003 fix: single timestamp shared between the durable audit row
    # and the response descriptor. Without this the row's
    # ``requested_at`` (inside jsonb payload) and the response's
    # ``queued_at`` diverge by milliseconds; the audit trail and the
    # UI then disagree about the request time.
    requested_at = datetime.now(UTC).isoformat()
    payload = {
        "actor": actor,
        "action": action,
        "stage": canonical_stage,
        "params": params or {},
        "source": "console",
        "requested_at": requested_at,
    }
    async with pool.acquire() as conn:
        # Concurrency check — block if an unresolved active run exists.
        active = await _fetch_active_job(conn)
        if active is not None and active.get("status") == "RUNNING":
            raise ConflictError(
                {
                    "code": "active_run",
                    "active_job": active,
                    "message": (
                        f"a {active.get('type')} run is already active "
                        f"(started {active.get('started_at')})"
                    ),
                }
            )
        # F-001 fix: asyncpg does NOT auto-encode dict→jsonb without a
        # registered codec (console-api's pool registers none). Pass
        # json.dumps + explicit $4::jsonb cast — mirrors the production
        # idiom in tpcore/order_management/execution_risk_skip.py:58-62.
        # Without this the first real operator click 500s with
        # ``asyncpg.exceptions.DataError: invalid input for query
        # argument $4: <dict>``.
        await conn.execute(
            """
            INSERT INTO platform.application_log (
                engine, run_id, event_type, severity, message, data
            ) VALUES ($1, $2, 'OPERATOR_RUN_REQUESTED', 'INFO', $3, $4::jsonb)
            """,
            OPERATOR_RUN_ENGINE,
            job_id,
            f"operator {actor} requested {action}"
            + (f" stage={canonical_stage}" if canonical_stage else ""),
            json.dumps(payload, default=str),
        )
    return {
        "job_id": str(job_id),
        "run_id": str(job_id),
        "action": action,
        "stage": canonical_stage,
        "status": "QUEUED",
        "queued_at": requested_at,
    }


async def fetch_job_status(
    pool: asyncpg.Pool, job_id: str,
) -> dict[str, Any] | None:
    """Return the current state of an OPERATOR_RUN_* job by job_id."""
    try:
        run_uuid = uuid.UUID(job_id)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid job_id: {job_id}") from e
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT recorded_at, event_type, severity, message, data
            FROM platform.application_log
            WHERE run_id = $1
            ORDER BY recorded_at ASC
            """,
            run_uuid,
        )
    if not rows:
        return None
    # Latest event drives status.
    latest = rows[-1]
    if latest["event_type"] == "OPERATOR_RUN_REQUESTED":
        status = "QUEUED"
    elif latest["event_type"] == "OPERATOR_RUN_STARTED":
        status = "RUNNING"
    elif latest["event_type"] == "OPERATOR_RUN_COMPLETED":
        status = "SUCCESS"
    elif latest["event_type"] == "OPERATOR_RUN_FAILED":
        status = "FAILED"
    elif latest["event_type"] == "OPERATOR_RUN_ABORTED":
        status = "ABORTED"
    else:
        # In-flight script events — still RUNNING.
        status = "RUNNING"
    first = rows[0]
    return {
        "job_id": job_id,
        "run_id": job_id,
        "status": status,
        "started_at": first["recorded_at"].isoformat(),
        "updated_at": latest["recorded_at"].isoformat(),
        "elapsed_seconds": int(
            (latest["recorded_at"] - first["recorded_at"]).total_seconds()
        ),
        "events": [
            {
                "time": r["recorded_at"].isoformat(),
                "event_type": r["event_type"],
                "severity": r["severity"],
                "message": r["message"] or "",
            }
            for r in rows[-30:]
        ],
    }


async def abort_operator_run(
    pool: asyncpg.Pool, *, actor: str, job_id: str,
) -> dict[str, Any]:
    """Write an OPERATOR_RUN_ABORTED row for the given job. Does NOT
    SIGTERM the subprocess — the lane daemon reads the row on its next
    poll and stops the running subprocess. Operators use abort to
    clear stale runs."""
    try:
        run_uuid = uuid.UUID(job_id)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid job_id: {job_id}") from e
    payload = {
        "actor": actor,
        "aborted_at": datetime.now(UTC).isoformat(),
        "source": "console",
    }
    async with pool.acquire() as conn:
        # F-001 fix: same jsonb cast + json.dumps as request_operator_run.
        await conn.execute(
            """
            INSERT INTO platform.application_log (
                engine, run_id, event_type, severity, message, data
            ) VALUES ($1, $2, 'OPERATOR_RUN_ABORTED', 'WARNING', $3, $4::jsonb)
            """,
            OPERATOR_RUN_ENGINE,
            run_uuid,
            f"operator {actor} requested abort",
            json.dumps(payload, default=str),
        )
    return {"job_id": job_id, "status": "ABORTED"}
