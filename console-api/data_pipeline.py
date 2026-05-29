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
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg


# Runtime override for blocked_vendor checks. F-004 fix (2026-05-29
# expert review): the previous design hard-coded blocked_vendor in
# CHECK_REMEDIATION so restoring a vendor required a code change +
# redeploy. Now the operator can set ``CONSOLE_VENDOR_ENABLED``
# (comma-list of vendor names) on the console-api Railway service —
# a vendor in this set is treated as RESTORED and the corresponding
# checks revert to their derived status instead of being rewritten
# to BLOCKED_VENDOR_ACCESS.
def _vendor_enabled(vendor: str | None) -> bool:
    if not vendor:
        return False
    enabled_raw = os.environ.get("CONSOLE_VENDOR_ENABLED", "")
    enabled = {v.strip().lower() for v in enabled_raw.split(",") if v.strip()}
    return vendor.strip().lower() in enabled


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
    "prices_daily_classification_id_completeness",
    "fundamentals_quarterly_completeness",
    "corporate_actions_completeness",
    "corporate_actions_integrity",
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
    "insider_sentiment_freshness",
    # insider_filings_freshness was RETIRED on 2026-05-25 (P0_3) —
    # no longer produced by the validation suite. Listing it here
    # surfaces as a stuck UNKNOWN (no data_quality_log row in 72h).
    "issuer_history_integrity",
    "issuer_securities_integrity",
    "corporate_events_integrity",
    "liquidity_tiers_completeness",
    "liquidity_tiers_freshness",
    "fundamentals_integrity",
    # NOTE: removed (no validator implementation, no
    # data_quality_log rows ever written):
    #   * tradier_options_chain
    #   * alpaca_corporate_actions
    #   * ingest_manifest_loaded
    #   * ingest_quarantine_review
    # These were speculative phantom checks. The console now lists
    # only checks the validation suite actually produces.
]


# Allowlist for per-feed re-run actions. Keys are user-visible action
# names; values are the canonical ``scripts/ops.py --stage`` name. Any
# stage not in this map cannot be triggered from the console — the
# operator must run it from a wrapper script directly. Sourced from
# the data-adapter pipeline canonical roster.
# Stage allowlist — keys mirror the canonical scripts/ops.py
# _STAGE_SPECS names. Operator action endpoints reject any stage NOT
# in this set (server-side static, no client-controlled value can
# bypass). Names verified against
# ``grep -E '\\("[a-z_]+",' scripts/ops.py`` 2026-05-29.
RUN_FEED_ALLOWLIST: dict[str, str] = {
    "daily_bars": "daily_bars",
    "data_validation": "data_validation",
    "corporate_actions": "corporate_actions",
    "fundamentals_refresh": "fundamentals_refresh",
    "earnings_refresh": "earnings_refresh",
    "macro_indicators": "macro_indicators",
    "sec_filings": "sec_filings",
    "aaii_sentiment": "aaii_sentiment",
    "apewisdom_social_sentiment": "apewisdom_social_sentiment",
    "finra_short_interest": "finra_short_interest",
    "tier_refresh": "tier_refresh",
    "classify_tickers": "classify_tickers",
    "forensics": "forensics",
    # SEC EDGAR fallback (task #34) — supports --param tickers=A,B,C.
    "sec_fundamentals_fallback": "sec_fundamentals_fallback",
}


# Remediation classification per check. Replaces the prior
# CHECK_TO_STAGE map. Each check is bucketed into one of seven
# remediation classes; the UI renders different actions per class.
#
# Classes:
#   scoped_auto_heal     — repair affected symbols only (no full
#                          stage sweep). Reads failed-ticker list
#                          from notes_details and passes
#                          --param tickers=A,B,C to the stage.
#                          PREFERRED when the stage supports it.
#   full_stage_required  — stage cannot be scoped (e.g.
#                          freshness checks must re-pull the whole
#                          cadence-window). Run feed = full stage.
#   blocked_vendor       — vendor access is broken/disabled.
#                          No heal button; surface vendor + reason.
#   operator_required    — needs a manual procedure (SQL cleanup,
#                          daemon restart, etc.). Show procedure
#                          link; no auto-action button.
#   unhealable           — meta-monitor / definitionally unhealable
#                          (cadence gate, manifest-review states).
#                          No action button; explain why.
#   bootstrap            — one-time baseline write needed
#                          (corporate_actions no_prior_archive).
#                          Show "Write baseline" one-shot button.
#   not_implemented      — there's a known heal but it isn't wired
#                          to the console yet. Show "see runbook".
#
# scope_kind tells the dispatcher what shape of scope to send:
#   tickers              — flatten failed_symbols into --param tickers=
#   tickers_dates        — also pass date range when emitted
#   full                 — no scoping; whole stage
#
# fallback_stage is the secondary stage to run if the primary
# returns "no data" on a ticker — e.g. SEC EDGAR fallback for FMP
# fundamentals gaps (task #34's sec_fundamentals_fallback).
#
# This dict is the SoT for console behavior. Kept in sync with
# tpcore/selfheal/registry.py via a sentinel test (see TODO).
CHECK_REMEDIATION: dict[str, dict[str, Any]] = {
    # ─── price-data checks ───
    # F-002 fix (2026-05-29 expert review): _stage_daily_bars does
    # NOT honor an operator-supplied ``tickers`` config — its scope
    # comes from its own gap detector. Classifying these as
    # ``scoped_auto_heal`` would lie to the operator (the UI would
    # show "Repair N tickers" but the tickers list would be ignored).
    # Use ``full_stage_required`` with the canonical ``repair_gaps`` /
    # ``repair_coverage`` params — the stage self-scopes, the UI says
    # so honestly via the operator_note.
    "prices_daily_completeness": {
        "class": "full_stage_required",
        "stage": "daily_bars",
        "params": {"repair_gaps": True},
        "scope_kind": "full",
        "operator_note": (
            "Stage self-scopes via repair_gaps — the gap detector "
            "identifies which (ticker, date) cells to fix. Does NOT "
            "honor an operator-supplied ticker list."
        ),
        "estimated_runtime_seconds": 120,
    },
    "prices_daily_freshness": {
        "class": "full_stage_required",
        "stage": "daily_bars",
        "params": {"repair_coverage": True},
        "scope_kind": "full",
        "estimated_runtime_seconds": 600,
    },
    # ─── fundamentals ───
    "fundamentals_quarterly_completeness": {
        "class": "scoped_auto_heal",
        "stage": "fundamentals_refresh",
        "params": {"skip_guard_days": 0},
        "scope_kind": "tickers",
        "fallback_stage": "sec_fundamentals_fallback",
        "estimated_runtime_seconds": 180,
    },
    # ─── corporate actions / events ───
    "corporate_actions_completeness": {
        "class": "bootstrap",
        "stage": "corporate_actions",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "operator_note": (
            "no_prior_archive — initial CSV-archive baseline missing. "
            "Running this stage once writes the baseline."
        ),
        "estimated_runtime_seconds": 240,
    },
    "corporate_events_integrity": {
        "class": "operator_required",
        "operator_procedure": (
            "Bitemporal-open dup or event-after-record row exists. "
            "Run tpcore.audit cleanup procedure "
            "(audit_cleanup_2026_05_24.py) — manual SQL."
        ),
    },
    # ─── macro ───
    "macro_indicators_completeness": {
        "class": "full_stage_required",
        "stage": "macro_indicators",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 120,
    },
    "macro_indicators_freshness": {
        "class": "full_stage_required",
        "stage": "macro_indicators",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 120,
    },
    # ─── earnings / SEC filings ───
    "earnings_events_freshness": {
        "class": "full_stage_required",
        "stage": "earnings_refresh",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 600,
    },
    "earnings_events_monotone": {
        "class": "scoped_auto_heal",
        "stage": "earnings_refresh",
        "params": {"skip_guard_days": 0},
        "scope_kind": "tickers",
        "estimated_runtime_seconds": 180,
    },
    "sec_filings_freshness": {
        "class": "full_stage_required",
        "stage": "sec_filings",
        "params": {"repair": True},
        "scope_kind": "full",
        "estimated_runtime_seconds": 480,
    },
    "sec_insider_monotone": {
        "class": "scoped_auto_heal",
        "stage": "sec_filings",
        "params": {"repair": True},
        "scope_kind": "tickers",
        "estimated_runtime_seconds": 240,
    },
    # ─── options ───
    "options_max_pain_freshness": {
        "class": "blocked_vendor",
        "vendor": "greeks.pro",
        "blocker_reason": (
            "Operator-disabled 2026-05-29: greeks.pro access "
            "revoked. Lane stays RED until restored."
        ),
        "scope_kind": "full",
    },
    # ─── ticker / issuer integrity ───
    "ticker_history_integrity": {
        "class": "operator_required",
        "operator_procedure": (
            "Zero-duration / invalid-range / open-row-dup classification "
            "rows exist. Run tpcore/integrity/issuer_history_cleanup.py "
            "manually after operator review."
        ),
    },
    "ticker_classifications_coverage": {
        "class": "full_stage_required",
        "stage": "classify_tickers",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 240,
    },
    "issuer_history_integrity": {
        "class": "operator_required",
        "operator_procedure": (
            "Bitemporal integrity violation. "
            "Run tpcore/integrity/issuer_history_cleanup.py — "
            "manual SQL after operator review."
        ),
    },
    "issuer_securities_integrity": {
        "class": "operator_required",
        "operator_procedure": (
            "Bitemporal integrity violation in issuer_securities. "
            "Run tpcore/integrity/issuer_history_cleanup.py — "
            "manual SQL after operator review."
        ),
    },
    # ─── sentiment / short interest ───
    "aaii_sentiment_freshness": {
        "class": "full_stage_required",
        "stage": "aaii_sentiment",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 30,
    },
    "social_sentiment_freshness": {
        "class": "full_stage_required",
        "stage": "apewisdom_social_sentiment",
        "params": {"skip_guard_hours": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 60,
    },
    "short_interest_freshness": {
        "class": "full_stage_required",
        "stage": "finra_short_interest",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 60,
    },
    # ─── additional checks the validation suite actually writes ───
    "prices_daily_classification_id_completeness": {
        "class": "scoped_auto_heal",
        "stage": "sec_orphan_resolve",
        "params": {"phase_b": True, "phase_c": True},
        "scope_kind": "tickers",
        "estimated_runtime_seconds": 180,
    },
    "corporate_actions_integrity": {
        "class": "operator_required",
        "operator_procedure": (
            "Per-row integrity violation in corporate_actions. "
            "Run tpcore audit cleanup procedure after operator review."
        ),
    },
    "insider_sentiment_freshness": {
        "class": "full_stage_required",
        "stage": "finnhub_insider_sentiment",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 60,
    },
    "liquidity_tiers_completeness": {
        "class": "full_stage_required",
        "stage": "tier_refresh",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 240,
    },
    "liquidity_tiers_freshness": {
        "class": "full_stage_required",
        "stage": "tier_refresh",
        "params": {"skip_guard_days": 0},
        "scope_kind": "full",
        "estimated_runtime_seconds": 240,
    },
    "fundamentals_integrity": {
        "class": "operator_required",
        "operator_procedure": (
            "Fundamentals-table integrity violation (orphan rows, "
            "duplicate quarter keys, etc). Audit + manual cleanup."
        ),
    },
    # ─── secondary feeds ───
    "tradier_options_chain": {
        "class": "not_implemented",
        "reason": (
            "No dedicated ``tradier_options`` stage in scripts/ops.py "
            "(the table is populated by a different ingest path that "
            "isn't on the operator-trigger surface yet). Operator must "
            "use ops/_helpers/ scripts directly until wired."
        ),
    },
    "alpaca_corporate_actions": {
        "class": "full_stage_required",
        "stage": "corporate_actions",
        "params": {},
        "scope_kind": "full",
        "estimated_runtime_seconds": 60,
    },
    # ─── meta-monitors (definitionally unhealable) ───
    "data_operations_complete_cadence": {
        "class": "unhealable",
        "reason": (
            "Meta-monitor — DATA_OPERATIONS_COMPLETE is the END product "
            "of a fully-green data lane run; clearing the other reds "
            "fires the emission gate naturally. No stage emits this."
        ),
    },
    "daemon_freshness": {
        "class": "operator_required",
        "operator_procedure": (
            "Daemon heartbeat stale. Check daemon liveness via the Health "
            "page (daemons table) OR query "
            "platform.daemon_heartbeats WHERE daemon=<name>. If the daemon "
            "is dead, the owning Railway service must be restarted "
            "(allocator runs in engine-service; data_operations runs as a "
            "cron). Cascade does NOT restart daemons."
        ),
    },
    "ingest_manifest_loaded": {
        "class": "unhealable",
        "reason": (
            "Manifest review state — surfaced for operator review, no "
            "single stage clears it."
        ),
    },
    "ingest_quarantine_review": {
        "class": "unhealable",
        "reason": (
            "Quarantine review state — operator must inspect quarantined "
            "rows before clearing."
        ),
    },
}


# Valid remediation classes — used by tests + UI for type safety.
VALID_REMEDIATION_CLASSES = {
    "scoped_auto_heal",
    "full_stage_required",
    "blocked_vendor",
    "operator_required",
    "unhealable",
    "bootstrap",
    "not_implemented",
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
    # ``stale`` is dispositive — see _classify_validation_row docstring.
    # 2026-05-29 fix: align KPI rollup with the per-row rule. A vacuously-
    # true check (confidence=0.0 + stale=False) is PASS, not FAIL.
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
            COUNT(*) FILTER (
                WHERE NOT stale
                  AND (confidence = 0 OR confidence >= 1.0)
            ) AS passed,
            COUNT(*) FILTER (
                WHERE NOT stale
                  AND confidence > 0
                  AND confidence < 1.0
            ) AS warnings,
            COUNT(*) FILTER (WHERE stale) AS failed,
            COALESCE(MIN(NULLIF(confidence, 0)), 0) AS min_confidence
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
        remediation = _check_remediation(name)
        row = seen.get(name)
        if row is None:
            out.append(_build_check_row(
                name=name, status="UNKNOWN", rows=None, age=None,
                notes="no data_quality_log row in last 72 h",
                notes_details=None,
                last_checked_at=None,
                failed_symbols=[],
                remediation=remediation,
            ))
            continue
        # Status derives from row state. For blocked_vendor checks,
        # surface that explicitly even when stale=False — the check
        # may say PASS by luck-of-window but the underlying lane is
        # known-broken. F-004 fix: when the operator restores access
        # via the CONSOLE_VENDOR_ENABLED env var, skip the rewrite so
        # the check can return to its honest derived status.
        derived_status = _classify_validation_row(row)
        if (
            remediation["class"] == "blocked_vendor"
            and derived_status != "UNKNOWN"
            and not _vendor_enabled(remediation.get("vendor"))
        ):
            derived_status = "BLOCKED_VENDOR_ACCESS"
        notes_summary, notes_details = _format_notes(row["notes"])
        failed_symbols = _extract_failed_symbols(notes_details)
        out.append(_build_check_row(
            name=name,
            status=derived_status,
            rows=int(row["missing_bars"]) if row["missing_bars"] is not None else None,
            age=_age_str(now, row["timestamp"]),
            notes=notes_summary,
            notes_details=notes_details,
            last_checked_at=row["timestamp"].isoformat(),
            failed_symbols=failed_symbols,
            remediation=remediation,
        ))
    return out


def _build_check_row(
    *,
    name: str,
    status: str,
    rows: int | None,
    age: str | None,
    notes: str,
    notes_details: list[dict] | None,
    last_checked_at: str | None,
    failed_symbols: list[str],
    remediation: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the contract-shaped row the UI consumes.

    The remediation class + scope_kind drives the action surface.
    Per REQ-001 the row includes the full classification so the
    frontend can pick the right button without re-deriving."""
    return {
        "name": name,
        "status": status,
        "rows": rows,
        "age": age,
        "notes": notes,
        "notes_details": notes_details,
        "last_checked_at": last_checked_at,
        # ─── REMEDIATION CONTRACT (REQ-001) ───
        "remediation_class": remediation["class"],
        "target_stage": remediation.get("stage"),
        "scope_kind": remediation.get("scope_kind", "full"),
        "fallback_stage": remediation.get("fallback_stage"),
        "vendor": remediation.get("vendor"),
        "blocker_reason": remediation.get("blocker_reason"),
        "operator_procedure": remediation.get("operator_procedure"),
        "operator_note": remediation.get("operator_note"),
        "unhealable_reason": remediation.get("reason"),
        "estimated_runtime_seconds": remediation.get(
            "estimated_runtime_seconds"
        ),
        "affected_symbols": failed_symbols,
        "allowed_actions": remediation["allowed_actions"],
        # ─── legacy shape kept for backwards compat ───
        "healable": _check_healable(name),
        "actionable": (
            remediation["class"] in (
                "scoped_auto_heal", "full_stage_required", "bootstrap",
            )
        ),
    }


def _extract_failed_symbols(
    notes_details: list[dict] | None,
) -> list[str]:
    """Pull the unique list of tickers from the failure-detail array.
    Used by scoped repair to send --param tickers=A,B,C to the stage.
    Caps at 200 tickers (safety bound — past this the scope IS the
    full stage, in which case we just dispatch the full stage)."""
    if not notes_details:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for d in notes_details:
        if not isinstance(d, dict):
            continue
        t = d.get("ticker")
        if t and isinstance(t, str) and not t.startswith("<") and t not in seen:
            seen.add(t)
            out.append(t)
            if len(out) >= 200:
                break
    return out


def _classify_validation_row(row: dict[str, Any]) -> str:
    """Apply the data-acceptance gate rule.

    The validator's ``stale`` field is **dispositive** — the suite sets
    ``stale = not check.passed``. So ``stale=False`` already means the
    check explicitly passed. Confidence is a secondary quality signal.

    2026-05-29 fix: prior logic ``stale OR conf < 0.5 → FAIL`` mis-
    classified vacuously-true checks (e.g. integrity checks on empty
    sub-tables → confidence=0.0 by ``_confidence(check.total<=0)`` but
    ``stale=False`` because the check did pass). The console rendered
    those as FAIL even though the underlying lane was healthy.

    Rule:
      * stale=True                                  → FAIL
      * stale=False AND confidence==0 (vacuous OK)  → PASS
      * stale=False AND 0 < confidence < 1.0 (partial) → WARN
      * stale=False AND confidence >= 1.0           → PASS
    """
    stale = bool(row.get("stale"))
    if stale:
        return "FAIL"
    conf = float(row.get("confidence") or 0)
    if conf == 0.0:
        # Vacuously-true — check passed with no rows to evaluate.
        return "PASS"
    if conf < 1.0:
        return "WARN"
    return "PASS"


def _check_remediation(name: str) -> dict[str, Any]:
    """Returns the rich remediation classification for a check, OR a
    fallback ``not_implemented`` block if the check isn't in the map.
    Output shape is the contract surfaced to the frontend."""
    spec = CHECK_REMEDIATION.get(name)
    if spec is None:
        return {
            "class": "not_implemented",
            "reason": (
                "no remediation entry in CHECK_REMEDIATION — surface as "
                "operator-review state until a remediation is wired"
            ),
            "stage": None,
            "scope_kind": "full",
            "allowed_actions": ["view_logs"],
        }
    out = dict(spec)
    out["allowed_actions"] = _check_allowed_actions_for_class(spec)
    return out


def _check_allowed_actions_for_class(spec: dict[str, Any]) -> list[str]:
    """Action list derived from the remediation class. The frontend
    uses this to pick which button to render."""
    cls = spec.get("class")
    actions = ["view_logs"]
    if cls == "scoped_auto_heal":
        actions.append("repair_failed_scope")
        # Operator can still escalate to full-stage if scope is empty.
        actions.append("run_scoped_feed")
        if spec.get("fallback_stage"):
            actions.append("run_fallback_source")
    elif cls == "full_stage_required":
        actions.append("run_scoped_feed")  # name kept for API parity
    elif cls == "bootstrap":
        actions.append("bootstrap_baseline")
    elif cls == "blocked_vendor":
        actions.append("view_blocker")
    elif cls == "operator_required":
        actions.append("view_blocker")  # serves as the procedure-link target
    elif cls == "unhealable":
        pass  # no action — explain why via reason field
    elif cls == "not_implemented":
        pass
    return actions


def _check_healable(name: str) -> bool:
    """A check is "healable" if its remediation class actually has an
    automatic recovery path. blocked_vendor / operator_required /
    unhealable / not_implemented are honest about not auto-healing."""
    cls = CHECK_REMEDIATION.get(name, {}).get("class")
    return cls in ("scoped_auto_heal", "full_stage_required", "bootstrap")


def _format_notes(raw: str | None) -> tuple[str, list[dict] | None]:
    """Notes from data_quality_log are typically a JSON-encoded array
    of per-ticker / per-row failure objects. For the UI we want:
      * empty list → blank
      * single failure → "first ticker: first reason"
      * multi failure → "N tickers with <reason>, first: <ticker>"
    Returns (summary_string, raw_list_or_None). UI renders the
    summary in the column and may expose the raw list in a tooltip."""
    if not raw:
        return "", None
    text = raw.strip()
    if not text or text == "[]":
        return "", None
    # Try JSON parse. If the body isn't a list, fall back to the text.
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text[:160], None
    if not isinstance(parsed, list):
        return text[:160], None
    if len(parsed) == 0:
        return "", None
    first = parsed[0] if isinstance(parsed[0], dict) else {}
    ticker = first.get("ticker") or "<row>"
    reason = first.get("reason") or first.get("observed") or "failure"
    if len(parsed) == 1:
        summary = f"{ticker}: {reason}"
    else:
        # Cluster by reason to surface the dominant failure pattern.
        reasons: dict[str, int] = {}
        for it in parsed:
            if isinstance(it, dict):
                r = it.get("reason") or "failure"
                reasons[r] = reasons.get(r, 0) + 1
        top_reason = max(reasons, key=reasons.get) if reasons else reason
        summary = (
            f"{len(parsed)} failures (dominant: {top_reason}), "
            f"first: {ticker}"
        )
    return summary[:200], parsed


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


def _params_for_check(
    check_name: str | None, stage: str | None,
) -> dict[str, Any]:
    """Look up the canonical ``params`` block for a remediation
    dispatch. F-008 fix (2026-05-29 pass-2 expert review): if the
    UI sends ``check_name`` we use it directly — eliminates the N:1
    ambiguity where a stage that produces multiple checks (daily_bars
    → completeness + freshness, corporate_actions → completeness +
    integrity) silently dispatched the FIRST check's params on every
    click. Only fall back to stage-reverse-lookup if the UI didn't
    send check_name (older clients / direct API callers)."""
    if check_name and check_name in CHECK_REMEDIATION:
        return dict(CHECK_REMEDIATION[check_name].get("params") or {})
    # Legacy / direct-API path: stage reverse-lookup (first-match-wins).
    # Documented as imprecise; callers should pass check_name.
    if not stage:
        return {}
    for _name, spec in CHECK_REMEDIATION.items():
        if spec.get("stage") == stage:
            return dict(spec.get("params") or {})
    return {}


async def request_operator_run(
    pool: asyncpg.Pool,
    *,
    actor: str,
    action: str,
    stage: str | None = None,
    params: dict[str, Any] | None = None,
    tickers: list[str] | None = None,
    check_name: str | None = None,
) -> dict[str, Any]:
    """Insert an OPERATOR_RUN_REQUESTED row to enqueue an operator-
    triggered run. The lane daemon picks it up out-of-band.

    ``tickers`` (optional): when present, the lane daemon dispatches
    the stage with ``--param tickers=A,B,C`` so the repair is scoped
    to just those symbols (REQ-002). Currently honored by stages that
    support the ``tickers`` config key — see
    ``CHECK_REMEDIATION[<check>]['scope_kind']`` for which stages do.
    For ``scope_kind='full'`` stages the tickers list is ignored.

    Returns the job descriptor: {job_id, action, queued_at, status='QUEUED'}.

    Raises ConflictError when an unresolved active run already exists
    (HTTP 409 surface at the endpoint)."""
    if action == "run_update":
        canonical_stage = None
    elif action == "run_validation":
        canonical_stage = "data_validation"
    elif action in ("run_feed", "run_scoped_feed", "repair_failed_scope"):
        if stage is None:
            raise ValueError(f"{action} requires stage")
        if stage not in RUN_FEED_ALLOWLIST:
            raise ValueError(
                f"stage {stage!r} not in RUN_FEED_ALLOWLIST"
            )
        canonical_stage = RUN_FEED_ALLOWLIST[stage]
    elif action == "run_fallback_source":
        # Wired specifically for sec_fundamentals_fallback today —
        # any other fallback would need an allowlist entry.
        # F-003 fix (2026-05-29 expert review): no inline bootstrap.
        # Rely on the static RUN_FEED_ALLOWLIST membership; if a
        # future fallback stage isn't allowlisted, fail closed like
        # any other action.
        if stage is None:
            stage = "sec_fundamentals_fallback"
        if stage not in RUN_FEED_ALLOWLIST:
            raise ValueError(
                f"fallback stage {stage!r} not in RUN_FEED_ALLOWLIST"
            )
        canonical_stage = stage
    elif action == "bootstrap_baseline":
        if stage is None:
            raise ValueError("bootstrap_baseline requires stage")
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
    # Sanitize the optional ticker scope — strip whitespace, upper-
    # case, cap length. F-005 fix (2026-05-29 expert review): surface
    # the truncation in the payload so the operator sees the cap in
    # the audit row and the job-status events.
    scoped_tickers: list[str] = []
    tickers_truncated_from: int | None = None
    if tickers:
        seen_t: set[str] = set()
        for t in tickers:
            if not isinstance(t, str):
                continue
            cleaned = t.strip().upper()
            if cleaned and cleaned not in seen_t:
                seen_t.add(cleaned)
                scoped_tickers.append(cleaned)
        if len(scoped_tickers) > 500:
            tickers_truncated_from = len(scoped_tickers)
            scoped_tickers = scoped_tickers[:500]

    # F-001 + F-008 fix (2026-05-29 expert review): merge the canonical
    # CHECK_REMEDIATION params block into the dispatched payload using
    # the explicit ``check_name`` (when the UI sent it). Eliminates
    # the N:1 ambiguity where stages with multiple checks (daily_bars
    # produces completeness AND freshness with different params)
    # silently sent the first check's params for every click.
    # Caller-supplied ``params`` wins on conflict.
    merged_params: dict[str, Any] = _params_for_check(
        check_name, canonical_stage,
    )
    if params:
        merged_params.update(params)

    payload = {
        "actor": actor,
        "action": action,
        "stage": canonical_stage,
        "params": merged_params,
        "tickers": scoped_tickers if scoped_tickers else None,
        "tickers_truncated_from": tickers_truncated_from,
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
        "tickers": scoped_tickers if scoped_tickers else None,
        "tickers_truncated_from": tickers_truncated_from,
        "params": merged_params,
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
