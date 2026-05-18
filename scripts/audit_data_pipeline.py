"""Comprehensive 4-phase DATA pipeline audit.

Scope: the **data** pipeline only (ingestion → platform tables →
validation → freshness/anomalies). This is NOT the engine pipeline
(covered by the smoke tests) nor the AAR pipeline (covered by
``test_aar_pipeline`` + the live forensics scanner) — engines/AAR
appear here only as data artifacts (SIGNAL/aar_events observability).

Single CLI that audits every layer of the data pipeline across four
categories of knowledge:

* **known_knowns**     — explicit checks: row counts, freshness vs.
  threshold for every data source (incl. the 2026-05-16 feeds:
  options_max_pain, social_sentiment, fear_greed, short_interest,
  borrow_rates, aaii_sentiment + the period-keyed insider_sentiment),
  validation status, ingestion-job state, Sentinel basket presence,
  hy_spread → credit_spread compliance. Freshness thresholds for the
  new feeds come from the canonical tpcore.feeds profile so the audit
  and validation suite never disagree.
* **known_unknowns**   — documented gaps we measure: GLD AR-estimator
  noise, hy_spread freeze post-truncation, prices_daily multi-day gaps,
  ETF AR mis-estimation in T3/T4.
* **unknown_knowns**   — data we already collect but don't surface:
  filter-diagnostics distribution per engine (from SIGNAL events),
  cross-engine ticker overlap, application_log event-type distribution,
  empty platform tables, macro indicator pairwise correlations.
* **unknown_unknowns** — anomaly heuristics: row-count velocity 7d vs
  prior, macro 3σ stoppage, liquidity-tier distribution shift, engine
  signal silence, DB size growth, correlated multi-source staleness.

Findings are printed to stdout (formatted or JSON) and persisted to
``platform.data_quality_log`` under
``source='data_pipeline_audit.<phase>.<check_name>'`` so the dashboard
can read them.

**Canonical audit command.** When the operator asks for a data
pipeline audit (or just "audit pipeline"), run this — do NOT re-audit
manually. See CLAUDE.md session rule.

Exit code: 0 on success; non-zero only if ``--strict`` is set and any
phase has FAIL severity (default is informational across phases other
than known_knowns).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.feeds import freshness_max_age_days

if TYPE_CHECKING:
    from tpcore.ingestion.csv_archive import ShrinkageReport

logger = structlog.get_logger(__name__)


# ─── Data-source registry ──────────────────────────────────────────────


@dataclass(frozen=True)
class DataSource:
    name: str
    table: str
    freshness_days: int
    where_clause: str = ""  # optional WHERE filter (e.g. for credit_spread)
    timestamp_col: str = "date"  # column to compute freshness from


DATA_SOURCES: tuple[DataSource, ...] = (
    DataSource("daily_bars",          "platform.prices_daily",            freshness_days=4),
    DataSource("corporate_actions",   "platform.corporate_actions",       freshness_days=7, timestamp_col="action_date"),
    DataSource("fundamentals",        "platform.fundamentals_quarterly",  freshness_days=120, timestamp_col="period_end_date"),
    DataSource("earnings_events",     "platform.earnings_events",         freshness_days=90, timestamp_col="event_date"),
    DataSource("sec_filings",         "platform.sec_insider_transactions", freshness_days=14, timestamp_col="filing_date"),
    DataSource("macro_indicators",    "platform.macro_indicators",        freshness_days=90),
    DataSource("credit_spread",       "platform.macro_indicators",        freshness_days=14,
               where_clause="WHERE indicator='credit_spread'"),
    DataSource("spread_observations", "platform.spread_observations",     freshness_days=90, timestamp_col="observed_at"),
    DataSource("ticker_classifications", "platform.ticker_classifications", freshness_days=30, timestamp_col="last_updated"),
    # ── Cross-sectional / sentiment / macro feeds shipped 2026-05-16.
    # Freshness thresholds come from the SAME canonical tpcore.feeds
    # profile the validation suite uses (freshness_max_age_days, same
    # fallbacks as tpcore/quality/validation/checks/<feed>_freshness.py)
    # so the audit and validation can never disagree on staleness. Adding
    # them here also enrolls them in the unknown_unknowns
    # correlated-staleness sweep automatically. This is independent
    # defence-in-depth on top of the known_knowns validation_status check
    # (which already surfaces validation.<feed>_freshness reds).
    DataSource("options_max_pain",    "platform.options_max_pain",        freshness_days=freshness_max_age_days("greeks_max_pain", 7),       timestamp_col="observed_date"),
    DataSource("social_sentiment",    "platform.social_sentiment",        freshness_days=freshness_max_age_days("apewisdom_social_sentiment", 7)),
    DataSource("fear_greed",          "platform.fear_greed",              freshness_days=5),  # validation gate is 3 NYSE sessions; 5 calendar days ≈ session + weekend/holiday pad (advisory defence — validation is the hard gate)
    DataSource("short_interest",      "platform.short_interest",          freshness_days=freshness_max_age_days("finra_short_interest", 35), timestamp_col="settlement_date"),
    DataSource("borrow_rates",        "platform.borrow_rates",            freshness_days=freshness_max_age_days("iborrowdesk_borrow_rates", 5)),
    DataSource("aaii_sentiment",      "platform.aaii_sentiment",          freshness_days=freshness_max_age_days("aaii_sentiment", 10)),
    # NOTE: insider_sentiment (Finnhub MSPR) is period-keyed (year, month)
    # with NO date/timestamp column — it cannot use this date-based
    # MAX()/age loop without fabricating a date. It gets a dedicated
    # period-aware known_knowns check (insider_sentiment_period) that
    # mirrors tpcore.quality.validation.checks.insider_sentiment_freshness.
)


# ─── Finding model ─────────────────────────────────────────────────────


@dataclass
class AuditFinding:
    phase: str
    check_name: str
    source: str
    severity: str  # OK | WARN | FAIL
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    recommended_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Decimal isn't JSON-serializable by default; coerce in evidence.
        d["evidence"] = _jsonify(d["evidence"])
        return d


def _jsonify(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, )):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    return obj


# ─── Streaming sink ────────────────────────────────────────────────────


class _FindingSink(list):
    """``list`` subclass whose ``.append`` fires a callback per finding.

    Lets every existing ``findings.append(...)`` site stream + persist
    incrementally with zero changes to the ~40 call sites. ``amain``
    injects a sink whose hook renders the finding to stdout and writes
    its data_quality_log row immediately, so a long audit shows
    progress as it goes and a crash mid-run still persists everything
    completed so far.
    """

    def __init__(self, on_append=None) -> None:
        super().__init__()
        self._on_append = on_append

    def append(self, item) -> None:  # type: ignore[override]
        super().append(item)
        if self._on_append is not None:
            self._on_append(item)


# ─── CSV-first archive guardrail ───────────────────────────────────────


# The 5 ingest sources retrofitted with the CSV-first archive guardrail
# (shipped 2026-05-15 — see tpcore.ingestion.csv_archive). Both the
# csv_archive_presence check and the (disk-based) shrinkage_detector
# iterate this single tuple — they must never diverge.
ARCHIVE_SOURCES = (
    "fred_macro", "alpaca_corporate_actions", "alpaca_daily_bars",
    "fmp_fundamentals", "fmp_earnings_events",
)

# row_velocity sporadic-cadence severe-degradation thresholds (#248).
#
# CLUSTER-ROBUST WINDOWS (revised after the #248 spec review found a
# Critical false-positive). The two sporadic targets are *clustered*,
# not uniform-rate: corporate_actions is event-driven (splits/divs/
# earnings cluster around ex-div/earnings dates) and
# fundamentals_quarterly arrives in ~4 dense filing seasons per year
# (10-Q/10-K windows ~Jan/Apr/Jul/Oct), each ~6 weeks, with only
# stragglers/amendments between. A *short* recent window (the original
# 30d) can fall ENTIRELY inside a legitimate inter-cluster off-season
# — a quarterly off-season exceeds 30d — so a 30d-vs-rate-normalized
# predicate WARNs on perfectly healthy data (reviewer's verified
# scenario: prior≈100, a 3-row straggler lull → WARN). A constant
# tweak cannot fix this: a clustered lull can be ~0 *regardless* of
# prior magnitude or floor, because the recent window is shorter than
# the off-season.
#
# The only non-flaky partial predicate for clustered cadence uses a
# recent window guaranteed to span ≥1 full expected season. A 180d
# recent window CANNOT fit inside any off-season for either table:
# 180d necessarily contains ≥1 full quarterly filing season (and, for
# corporate_actions, ≥1 earnings season + ≥2 dividend quarters). A
# *sustained* near-zero over 180d while the prior FULL YEAR shows the
# regular seasonal cycle is therefore unambiguous — a healthy
# quarterly/event table physically cannot produce ~nothing across a
# 180d span (it would have hit a season). Detection latency ~one
# quarter is acceptable: this is *sustained* degradation by
# definition; acute staleness is already covered by
# freshness/selfheal.
#
# Windows: recent = last 180d; prior = the 365d band from 180d–545d
# ago (a full prior year → the complete annual seasonal baseline).
# Rate-normalized 180d expectation = prior * (180/365).
# SPORADIC_SEVERE_FRAC=0.10: 180d at <10% of the full-year
# rate-normalized expectation is unreachable by any legitimate
# seasonal pattern (any healthy 180d window hits ≥1 full season →
# far more than 10% of the annual rate). The prior floor requires
# ≥1 year of real history (≥40 rows in the 365d prior band) before
# the ratio is statistically meaningful.
SPORADIC_RECENT_DAYS = 180
SPORADIC_PRIOR_DAYS = 545  # prior band = (PRIOR_DAYS, RECENT_DAYS] ago
SPORADIC_SEVERE_FRAC = 0.10
SPORADIC_PRIOR_FLOOR = 40
# Rate-normalization factor: recent window length / prior band length.
SPORADIC_PRIOR_BAND_DAYS = SPORADIC_PRIOR_DAYS - SPORADIC_RECENT_DAYS  # 365
SPORADIC_RATE_FACTOR = SPORADIC_RECENT_DAYS / SPORADIC_PRIOR_BAND_DAYS


def _detect_archive_shrinkage() -> tuple[list[ShrinkageReport], list[dict]]:
    """Compare each archive source's latest snapshot to its predecessor.

    Pool-free and disk-only: this is the *real* persisted evidence of a
    vendor truncation (the on-disk ``.csv.gz`` archive), unlike the old
    application_log query which keyed off a structlog event that has no
    structlog→DB bridge in this repo (it could never fire).

    Returns ``(reports, uncheckable)``:

    * ``reports`` — one :class:`ShrinkageReport` per source that had a
      prior archive to *genuinely compare* against.
    * ``uncheckable`` — one ``{"source", "reason"}`` dict per source
      that could NOT be compared (no archive dir / no latest snapshot /
      no prior snapshot / ``prev_rows==0``). Surfacing these is what
      stops an empty/fresh ``data/`` reporting a silent green "I checked
      nothing" all-clear on a live-money data-integrity guardrail.
    """
    from tpcore.ingestion.csv_archive import (
        count_archive_rows,
        detect_shrinkage,
        latest_archive,
    )

    reports: list[ShrinkageReport] = []
    uncheckable: list[dict] = []
    for src in ARCHIVE_SOURCES:
        try:
            la = latest_archive(src)
            if la is None:
                uncheckable.append(
                    {"source": src, "reason": "no archive snapshot on disk"}
                )
                continue
            current_rows = count_archive_rows(la)
            report = detect_shrinkage(src, current_rows, exclude_path=la)
        except Exception:  # noqa: BLE001 — a broken archive must not abort the audit
            uncheckable.append(
                {"source": src, "reason": "archive read raised — broken/unreadable"}
            )
            continue
        if report is not None:
            reports.append(report)
        else:
            # detect_shrinkage → None means no prior snapshot to compare
            # against (or prev_rows==0): uncheckable, NOT a clean compare.
            uncheckable.append(
                {"source": src, "reason": "no prior snapshot to compare against"}
            )
    return reports, uncheckable


def _append_shrinkage_finding(
    findings: list[AuditFinding],
    reports: list[ShrinkageReport],
    uncheckable: list[dict] | None = None,
) -> None:
    """Render the disk-based shrinkage check into an AuditFinding.

    Preserves the original finding shape exactly (phase/check_name/
    source/severity/summary/evidence/recommended_action). Severity
    precedence is FAIL > WARN > OK:

    * any source shrank > 20%  → **FAIL** (unchanged behaviour/shape).
    * else any source was *uncheckable* (empty/fresh ``data/``, <2
      snapshots, ``prev_rows==0``) → **WARN** — "not green, needs
      attention" in this audit's OK|WARN|FAIL vocabulary. A WARN is NOT
      treated as green, so an empty archive root can no longer report a
      silent all-clear on a live-money data-integrity guardrail.
    * else (ALL sources genuinely compared, none over) → **OK**. Only
      now is green honest.
    """
    uncheckable = uncheckable or []
    over = [r for r in reports if r.over_threshold]
    if over:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="shrinkage_detector",
            source="csv_archive", severity="FAIL",
            summary=(
                f"{len(over)} full-snapshot source(s) shrank > 20% vs the "
                f"prior CSV archive — vendor truncation"
            ),
            evidence={"over_threshold": [
                {
                    "source": r.source,
                    "previous_rows": r.previous_rows,
                    "current_rows": r.current_rows,
                    "shrinkage_pct": round(r.shrinkage_pct, 4),
                    "previous_archive": r.previous_archive,
                }
                for r in over
            ]},
            recommended_action="inspect data/<source>_archive/ — vendor likely revoked history; the prior archive is the recovery source",
        ))
    elif uncheckable:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="shrinkage_detector",
            source="csv_archive", severity="WARN",
            summary=(
                f"{len(uncheckable)} of {len(ARCHIVE_SOURCES)} full-snapshot "
                f"source(s) UNCHECKABLE for shrinkage — green here would be a "
                f"false all-clear (nothing was actually compared)"
            ),
            evidence={
                "uncheckable": list(uncheckable),
                "compared": [
                    {
                        "source": r.source,
                        "previous_rows": r.previous_rows,
                        "current_rows": r.current_rows,
                        "shrinkage_pct": round(r.shrinkage_pct, 4),
                    }
                    for r in reports
                ],
            },
            recommended_action="confirm data/<source>_archive/ exists and holds ≥2 .csv.gz snapshots (fresh container / empty data dir / first-run source); the shrinkage guardrail cannot defend a source it has never compared",
        ))
    else:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="shrinkage_detector",
            source="csv_archive", severity="OK",
            summary="no full-snapshot source shrank > 20% vs its prior CSV archive",
            evidence={"compared": [
                {
                    "source": r.source,
                    "previous_rows": r.previous_rows,
                    "current_rows": r.current_rows,
                    "shrinkage_pct": round(r.shrinkage_pct, 4),
                }
                for r in reports
            ]},
        ))


async def _adapter_contract_findings(pool) -> list[AuditFinding]:
    """#186(6) thin Step-4c check. The producer raise is authoritative;
    this adds (1) registry coverage, (2) guard_pending visibility,
    (3) recent unacknowledged adapter_contract_drift escalations. It
    CANNOT re-derive drift post-cycle (adapter output is gone) —
    deliberately thinner than shrinkage_detector."""
    from tpcore.ingestion.adapter_contract import (
        ADAPTER_CONTRACTS,
        contract_drift,
    )

    out: list[AuditFinding] = []
    missing, extra = contract_drift()
    if missing or extra:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="registry", severity="FAIL",
            summary=f"ADAPTER_CONTRACTS drift: missing={sorted(missing)} "
                    f"extra={sorted(extra)}"))
    else:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="registry", severity="OK",
            summary="ADAPTER_CONTRACTS in lockstep with CSV-first feeds"))

    pending = sorted(f for f, c in ADAPTER_CONTRACTS.items()
                     if c.guard_pending)
    if pending:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="guard_pending", severity="WARN",
            summary=f"guard_pending (declared, enforced wiring not yet "
                    f"rolled out): {pending}"))

    async with pool.acquire() as conn:
        # `_run_stage` (ops.py bare `except Exception`) records the exception
        # class name in data->>'exception_type', not data->>'reason'.  The
        # `reason` OR-branch is forward-compat for any future explicit reason
        # field.  Note: the 24h window is the interim "recent escalation"
        # signal — there is no acknowledge/clear primitive in the data lane
        # yet; a proper ack/auto-clear arrives with the Data Supervisor
        # (Escalation & Hardening Ladder rung 2).  The check therefore stays
        # FAIL for up to 24h after a drift even once resolved — intended.
        n = await conn.fetchval("""
            SELECT COUNT(*)
            FROM platform.application_log
            WHERE event_type = 'INGESTION_FAILED'
              AND (data->>'exception_type' = 'AdapterContractDrift'
                   OR data->>'reason' = 'adapter_contract_drift')
              AND recorded_at > NOW() - INTERVAL '24 hours'
        """)
    if n and int(n) > 0:
        out.append(AuditFinding(
            phase="known_knowns", check_name="adapter_contract",
            source="escalation", severity="FAIL",
            summary=f"{int(n)} adapter_contract_drift escalation(s) in "
                    f"the last 24h — a vendor contract changed"))
    return out


# ─── Phase 1: known_knowns ─────────────────────────────────────────────


async def run_known_knowns(pool, sink: _FindingSink | None = None) -> list[AuditFinding]:
    findings: list[AuditFinding] = sink if sink is not None else []

    # Row counts + freshness per data source.
    for ds in DATA_SOURCES:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT COUNT(*) AS n, MAX({ds.timestamp_col}) AS mx FROM {ds.table} {ds.where_clause}"
            )
        n = int(row["n"] or 0)
        mx = row["mx"]
        if n == 0:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="row_count",
                source=ds.name, severity="FAIL",
                summary=f"{ds.name}: 0 rows",
                evidence={"table": ds.table, "rows": 0},
                recommended_action=f"run the {ds.name} ingestion stage",
            ))
            continue
        if mx is None:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="freshness",
                source=ds.name, severity="WARN",
                summary=f"{ds.name}: {n:,} rows but {ds.timestamp_col} all NULL",
                evidence={"rows": n},
            ))
            continue
        # Freshness severity
        as_of = datetime.now(UTC)
        if isinstance(mx, datetime):
            age_days = (as_of - mx).days
        else:
            age_days = (as_of.date() - mx).days
        severity = "OK" if age_days <= ds.freshness_days else ("WARN" if age_days <= 2 * ds.freshness_days else "FAIL")
        findings.append(AuditFinding(
            phase="known_knowns", check_name="freshness",
            source=ds.name, severity=severity,
            summary=f"{ds.name}: {n:,} rows, newest {mx} ({age_days}d ago, threshold {ds.freshness_days}d)",
            evidence={"rows": n, "newest": str(mx), "age_days": age_days, "threshold_days": ds.freshness_days},
        ))

    # Most-recent validation status per source.
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH latest AS (
                SELECT source, MAX(timestamp) AS t
                FROM platform.data_quality_log
                WHERE source LIKE 'validation.%'
                GROUP BY source
            )
            SELECT q.source, q.timestamp, q.stale, q.confidence
            FROM platform.data_quality_log q
            JOIN latest l ON l.source=q.source AND l.t=q.timestamp
            ORDER BY q.source
        """)
    if not rows:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="validation_status",
            source="validation_suite", severity="FAIL",
            summary="no validation rows in data_quality_log",
            recommended_action="run python -m tpcore.quality.validation",
        ))
    else:
        red = [r for r in rows if r["stale"] or (r["confidence"] is not None and float(r["confidence"]) < 1.0)]
        for r in red:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="validation_status",
                source=r["source"], severity="FAIL",
                summary=f"{r['source']}: stale={r['stale']} confidence={r['confidence']}",
                evidence={"timestamp": str(r["timestamp"])},
            ))
        if not red:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="validation_status",
                source="validation_suite", severity="OK",
                summary=f"all {len(rows)} validation checks green",
            ))

    # Ingestion-job state.
    #
    # ``platform.ingestion_jobs`` is the *Railway* ingestion-engine's
    # bookkeeping table. Railway has been paused since 2026-05-12, so
    # those rows are frozen at whatever state the daemon last left them
    # — a `last_status='failed'` there is NOT necessarily an active
    # failure. The authoritative signal for the local execution
    # environment is the most-recent successful INGESTION_COMPLETE
    # event in ``application_log`` for that stage. So a frozen "failed"
    # row whose stage has since completed successfully via the local
    # ops pipeline is *resolved*, not failing. Only a job with NO
    # superseding local success is a true FAIL.
    async with pool.acquire() as conn:
        jobs = await conn.fetch("""
            SELECT job_name, enabled, last_run_at, last_status, last_error
            FROM platform.ingestion_jobs ORDER BY job_name
        """)
        local_success = await conn.fetch("""
            SELECT data->>'stage' AS stage, MAX(recorded_at) AS last_ok
            FROM platform.application_log
            WHERE event_type = 'INGESTION_COMPLETE'
              AND recorded_at > NOW() - INTERVAL '7 days'
            GROUP BY data->>'stage'
        """)
    last_ok_by_stage = {r["stage"]: r["last_ok"] for r in local_success if r["stage"]}
    if jobs:
        disabled = [j for j in jobs if not j["enabled"]]
        raw_failed = [j for j in jobs if j["last_status"] not in (None, "success", "completed", "skipped")]
        truly_failed = []
        stale_resolved = []
        for j in raw_failed:
            local_ok = last_ok_by_stage.get(j["job_name"])
            if local_ok is not None and (
                j["last_run_at"] is None or local_ok > j["last_run_at"]
            ):
                stale_resolved.append((j, local_ok))
            else:
                truly_failed.append(j)
        if disabled:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="ingestion_jobs",
                source="ingestion_jobs", severity="WARN",
                summary=f"{len(disabled)} job(s) disabled",
                evidence={"jobs": [j["job_name"] for j in disabled]},
            ))
        if truly_failed:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="ingestion_jobs",
                source="ingestion_jobs", severity="FAIL",
                summary=f"{len(truly_failed)} job(s) failed with no superseding local success",
                evidence={"jobs": [(j["job_name"], j["last_status"], (j["last_error"] or "")[:120]) for j in truly_failed]},
                recommended_action="re-run the stage via scripts/run_data_operations.sh and inspect the error",
            ))
        if stale_resolved:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="ingestion_jobs",
                source="ingestion_jobs", severity="OK",
                summary=(
                    f"{len(stale_resolved)} frozen Railway-era failure(s) superseded by "
                    f"successful local INGESTION_COMPLETE — resolved, not failing"
                ),
                evidence={"resolved": [
                    {"job": j["job_name"], "frozen_status": j["last_status"],
                     "frozen_at": str(j["last_run_at"]), "local_success_at": str(ok)}
                    for j, ok in stale_resolved
                ]},
            ))
        if not (disabled or truly_failed):
            findings.append(AuditFinding(
                phase="known_knowns", check_name="ingestion_jobs",
                source="ingestion_jobs", severity="OK",
                summary=(
                    f"all {len(jobs)} ingestion jobs healthy"
                    + (f" ({len(stale_resolved)} frozen Railway-era rows superseded by local runs)" if stale_resolved else "")
                ),
            ))

    # Sentinel basket presence.
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT t.ticker, COALESCE(s.n, 0) AS n, s.last_bar
            FROM UNNEST($1::text[]) AS t(ticker)
            LEFT JOIN (
                SELECT ticker, COUNT(*) AS n, MAX(date) AS last_bar
                FROM platform.prices_daily WHERE delisted=false
                GROUP BY ticker
            ) s ON s.ticker = t.ticker
        """, ["SH", "PSQ", "GLD", "TLT", "SQQQ"])
    missing = [r for r in rows if r["n"] == 0]
    if missing:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="sentinel_basket",
            source="sentinel", severity="FAIL",
            summary=f"{len(missing)}/5 Sentinel basket ETFs missing from prices_daily",
            evidence={"missing": [r["ticker"] for r in missing]},
            recommended_action="run scripts/run_backfill_sentinel_etfs.sh",
        ))
    else:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="sentinel_basket",
            source="sentinel", severity="OK",
            summary="all 5 Sentinel basket ETFs present",
            evidence={t["ticker"]: {"rows": t["n"], "last_bar": str(t["last_bar"])} for t in rows},
        ))

    # credit_spread with BAA10Y history from 1996.
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT MIN(date) AS mn, MAX(date) AS mx, COUNT(*) AS n
            FROM platform.macro_indicators WHERE indicator='credit_spread'
        """)
    if row["n"] == 0:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="credit_spread_history",
            source="macro_indicators", severity="FAIL",
            summary="credit_spread has 0 rows",
            recommended_action="run macro_indicators stage with start_date=1996-01-01",
        ))
    else:
        mn = row["mn"]
        starts_in_1996 = mn is not None and mn.year <= 1996
        severity = "OK" if starts_in_1996 else "WARN"
        findings.append(AuditFinding(
            phase="known_knowns", check_name="credit_spread_history",
            source="macro_indicators", severity=severity,
            summary=f"credit_spread: {row['n']:,} rows, {mn} → {row['mx']}",
            evidence={"rows": row["n"], "starts_in_1996": starts_in_1996},
        ))

    # Active-code zero-hits for hy_spread.
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # --include='*.py' excludes pyc-cache binary-file matches.
        # --exclude-dir='__pycache__' belt + suspenders.
        result = subprocess.run(
            ["grep", "-rnE", "--include=*.py", "--exclude-dir=__pycache__",
             "hy_spread", "tpcore/fred/", "sentinel/plugs/", "scripts/ops.py"],
            capture_output=True, text=True, timeout=10, cwd=repo_root,
        )
        leaks = []
        for line in result.stdout.splitlines():
            if line.startswith("Binary file"):
                continue
            content = line.split(":", 2)[-1].lstrip()
            if not content:
                continue
            # Skip lines that are clearly docstring/comment context:
            # leading comment chars OR the symbol appears only inside
            # rst-backticks / quoted-string context (cross-reference
            # in a docstring, not an import or call).
            if content.startswith(("#", "\"\"\"", "'''", "\"", "*", "//", "-")):
                continue
            if "``hy_spread``" in content or "'hy_spread'" in content or '"hy_spread"' in content:
                continue
            leaks.append(line)
        severity = "OK" if not leaks else "WARN"
        findings.append(AuditFinding(
            phase="known_knowns", check_name="hy_spread_decommission",
            source="codebase", severity=severity,
            summary=(
                "no active hy_spread refs in adapter/plugs/ops"
                if not leaks else f"{len(leaks)} potential hy_spread leak(s)"
            ),
            evidence={"leaks": leaks[:5]} if leaks else {},
        ))
    except Exception as exc:  # noqa: BLE001
        findings.append(AuditFinding(
            phase="known_knowns", check_name="hy_spread_decommission",
            source="codebase", severity="WARN",
            summary=f"grep failed: {exc}",
        ))

    # CSV-first archive presence (guardrail shipped 2026-05-15 — moved
    # from "vendor truncation is a blindspot" to "we detect it"). For
    # each of the 5 retrofitted sources, verify the archive directory
    # has at least one .csv.gz.
    from tpcore.ingestion.csv_archive import latest_archive
    missing_archive = []
    archive_state: dict[str, str] = {}
    for src in ARCHIVE_SOURCES:
        try:
            la = latest_archive(src)
        except Exception:  # noqa: BLE001
            la = None
        if la is None:
            missing_archive.append(src)
        else:
            archive_state[src] = la.name
    if missing_archive:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="csv_archive_presence",
            source="csv_archive", severity="WARN",
            summary=f"{len(missing_archive)}/5 ingest sources have no CSV archive yet",
            evidence={"missing": missing_archive, "present": archive_state},
            recommended_action="run scripts/run_dump_baseline_archives.sh (full-snapshot) or wait for the next ingest of the incremental sources",
        ))
    else:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="csv_archive_presence",
            source="csv_archive", severity="OK",
            summary="all 5 ingest sources have a CSV archive on disk",
            evidence=archive_state,
        ))

    # Shrinkage-detector state — the BAMLH0A0HYM2 detector, re-keyed off
    # the *real* persisted evidence: the on-disk CSV archive. (The old
    # implementation queried platform.application_log for a structlog
    # event with no structlog→DB bridge in this repo — it was vacuous
    # and could never fire.) Compares each full-snapshot source's latest
    # archive to its predecessor; FAIL if any shrank > 20%.
    _shrink_reports, _shrink_uncheckable = _detect_archive_shrinkage()
    _append_shrinkage_finding(findings, _shrink_reports, _shrink_uncheckable)

    # insider_sentiment (Finnhub MSPR) — period-keyed, NOT date-keyed, so
    # it cannot ride the DATA_SOURCES date loop. Dedicated check mirroring
    # tpcore.quality.validation.checks.insider_sentiment_freshness:
    # newest (year*12+month) must be ≤ MAX_AGE_MONTHS old.
    INSIDER_MAX_AGE_MONTHS = 3
    async with pool.acquire() as conn:
        ins = await conn.fetchrow("""
            SELECT MAX(year * 12 + month) AS newest_period, COUNT(*) AS n
            FROM platform.insider_sentiment
        """)
    if ins is None or int(ins["n"] or 0) == 0:
        findings.append(AuditFinding(
            phase="known_knowns", check_name="insider_sentiment_period",
            source="insider_sentiment", severity="FAIL",
            summary="insider_sentiment: 0 rows",
            evidence={"table": "platform.insider_sentiment", "rows": 0},
            recommended_action="run the finnhub_insider_sentiment ingestion stage",
        ))
    else:
        now = datetime.now(UTC)
        now_period = now.year * 12 + now.month
        age_months = now_period - int(ins["newest_period"])
        severity = "OK" if age_months <= INSIDER_MAX_AGE_MONTHS else "FAIL"
        findings.append(AuditFinding(
            phase="known_knowns", check_name="insider_sentiment_period",
            source="insider_sentiment", severity=severity,
            summary=(
                f"insider_sentiment: {int(ins['n']):,} rows, newest period "
                f"{age_months} month(s) old (threshold {INSIDER_MAX_AGE_MONTHS})"
            ),
            evidence={"rows": int(ins["n"]), "age_months": age_months,
                      "threshold_months": INSIDER_MAX_AGE_MONTHS},
            recommended_action=(
                None if severity == "OK"
                else "re-run the finnhub_insider_sentiment stage — MSPR data is stale"
            ),
        ))
    # Governor enforcement (regression guard): for each engine, if it
    # produced submit/signal activity in application_log, did the
    # RiskGovernor *actually run* for it?
    #
    # Governor activity is NOT in application_log — the governor emits
    # pure structlog events and no structlog→DB processor exists in this
    # repo, so a message-count on application_log was always 0 (vacuous).
    # The real persisted evidence is ``platform.risk_state``: both
    # ``RiskGovernor.register_engine`` and ``RiskGovernor.record_fill``
    # upsert that engine's row via the store's ``put``, which sets
    # ``updated_at = now()``. So the truth test is: an engine that
    # submitted/signalled (real DBLogHandler event_types ``SIGNAL`` /
    # ``ORDER_SUBMITTED`` / ``FILL_CONFIRMED``) MUST have a risk_state
    # row whose ``updated_at`` is no older than its most recent submit —
    # otherwise the governor never ran for that submit (possible bypass).
    # Engines are pre-graduation (paper, not trading) today, so the
    # expected normal state is "no submits at all" → OK, never FAIL.
    _SUBMIT_EVENT_TYPES = ("SIGNAL", "ORDER_SUBMITTED", "FILL_CONFIRMED")
    for engine in ("reversion", "vector", "momentum", "sentinel"):
        async with pool.acquire() as conn:
            log_row = await conn.fetchrow("""
                SELECT
                  COUNT(*)        AS submit,
                  MAX(recorded_at) AS last_submit_at
                FROM platform.application_log
                WHERE engine = $1
                  AND event_type = ANY($2::text[])
                  AND recorded_at > NOW() - INTERVAL '30 days'
            """, engine, list(_SUBMIT_EVENT_TYPES))
            gov_row = await conn.fetchrow(
                "SELECT engine, updated_at FROM platform.risk_state WHERE engine = $1",
                engine,
            )
        submit = int(log_row["submit"] or 0)
        last_submit_at = log_row["last_submit_at"]
        gov_updated_at = gov_row["updated_at"] if gov_row is not None else None
        evidence: dict[str, Any] = {
            "engine": engine,
            "submit_signal_30d": submit,
            "last_submit_at": str(last_submit_at) if last_submit_at is not None else None,
            "risk_state_updated_at": str(gov_updated_at) if gov_updated_at is not None else "missing",
        }
        if submit > 0 and (gov_row is None or (last_submit_at is not None and gov_updated_at < last_submit_at)):
            findings.append(AuditFinding(
                phase="known_knowns", check_name="governor_enforcement",
                source=f"engine:{engine}", severity="WARN",
                summary=f"{engine}: engine submitted but governor state not updated since — possible bypass "
                        f"({submit} submit/signal event(s) in 30d, "
                        f"risk_state {'missing' if gov_row is None else 'stale'})",
                evidence=evidence,
                recommended_action=f"verify {engine} trade path runs through tpcore.risk.RiskGovernor.check_trade()/record_fill() "
                                   f"(OrderManager) or tpcore.risk.batch_gate.gate_batch_order() (batch scheduler)",
            ))
        elif gov_row is not None and submit == 0:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="governor_enforcement",
                source=f"engine:{engine}", severity="OK",
                summary=f"{engine}: governor registered; no submits in 30d (pre-graduation)",
                evidence=evidence,
            ))
        elif submit > 0:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="governor_enforcement",
                source=f"engine:{engine}", severity="OK",
                summary=f"{engine}: governor state current with submit activity (enforcement live) "
                        f"({submit} submit/signal event(s) in 30d)",
                evidence=evidence,
            ))
        else:
            findings.append(AuditFinding(
                phase="known_knowns", check_name="governor_enforcement",
                source=f"engine:{engine}", severity="OK",
                summary=f"{engine}: no activity (pre-graduation / paper — expected)",
                evidence=evidence,
            ))

    for f in await _adapter_contract_findings(pool):
        findings.append(f)

    return findings


# ─── Phase 2: known_unknowns ───────────────────────────────────────────


async def run_known_unknowns(pool, sink: _FindingSink | None = None) -> list[AuditFinding]:
    findings: list[AuditFinding] = sink if sink is not None else []

    # GLD tier T4 (known AR-estimator limitation).
    async with pool.acquire() as conn:
        gld = await conn.fetchrow(
            "SELECT tier, median_spread_pct FROM platform.liquidity_tiers WHERE ticker='GLD'"
        )
    if gld is None:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="gld_tier_quirk",
            source="liquidity_tiers", severity="WARN",
            summary="GLD missing from liquidity_tiers",
        ))
    else:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="gld_tier_quirk",
            source="liquidity_tiers", severity="OK",
            summary=f"GLD tier T{gld['tier']} (median spread {float(gld['median_spread_pct']):.4%}) — known AR over-attribution; not a blocker",
            evidence={"tier": gld["tier"], "median_spread_pct": float(gld["median_spread_pct"])},
        ))

    # hy_spread freeze — no new rows since BAA10Y swap.
    async with pool.acquire() as conn:
        hy = await conn.fetchrow("""
            SELECT MAX(date) AS mx, COUNT(*) AS n
            FROM platform.macro_indicators WHERE indicator='hy_spread'
        """)
    if hy["n"] == 0:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="hy_spread_freeze",
            source="macro_indicators", severity="OK",
            summary="hy_spread table empty (post-truncation, retained for reference but not seeded)",
        ))
    else:
        days_since = (datetime.now(UTC).date() - hy["mx"]).days
        ok = days_since >= 3  # should be frozen — no new rows
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="hy_spread_freeze",
            source="macro_indicators", severity="OK" if ok else "WARN",
            summary=f"hy_spread frozen at {hy['mx']} ({days_since}d ago, {hy['n']:,} rows preserved)",
            evidence={"last_date": str(hy["mx"]), "rows": hy["n"], "days_since": days_since},
            recommended_action=None if ok else "verify hy_spread isn't being refreshed by mistake",
        ))

    # Multi-day gaps in prices_daily — a CURRENT hole on a GENUINELY
    # LIQUID name (recalibrated 2026-05-15, expert pass).
    #
    # tier<=2 + asset_class='stock' alone was insufficient: it still
    # caught (a) thin tier-1 names whose spread is tight but which
    # barely trade (MAYS avg vol 2,693; KELYB 18K) — expected sparsity;
    # (b) post-IPO halts (SHAZ); (c) historical exchange halts on
    # collapsing micro-caps (AREB: 38-day halt during a $9→$0.17
    # implosion, resumed weeks ago). None are ingestion failures. The
    # check's ONLY real target is the SPY-incident class: a name that
    # actually trades every day silently stops getting bars *right now*.
    # That is isolated by THREE conditions together:
    #   1. tier<=2 AND asset_class='stock'         (not SPAC/fund)
    #   2. avg daily volume (60d) >= 500,000       (genuinely liquid —
    #      excludes thin tier-1 + most post-IPO/halted micro-caps)
    #   3. gap_end within the last 14 calendar days (a CURRENT hole, not
    #      a historical halt that already resolved — AREB's gap ended
    #      weeks ago and must not flag)
    # A liquid name with a brand-new multi-day hole is unambiguously an
    # ingest failure worth acting on; everything else is market reality.
    async with pool.acquire() as conn:
        gap_rows = await conn.fetch("""
            WITH liquid AS (
                SELECT lt.ticker
                FROM platform.liquidity_tiers lt
                JOIN platform.ticker_classifications tc ON tc.ticker = lt.ticker
                WHERE lt.tier <= 2 AND tc.asset_class = 'stock'
            ),
            vol AS (
                SELECT pd.ticker, AVG(pd.volume) AS avg_vol_60d
                FROM platform.prices_daily pd
                JOIN liquid USING (ticker)
                WHERE pd.delisted = false
                  AND pd.date >= CURRENT_DATE - INTERVAL '60 days'
                GROUP BY pd.ticker
                HAVING AVG(pd.volume) >= 500000
            ),
            per_ticker AS (
                SELECT pd.ticker, pd.date,
                       LAG(pd.date) OVER (PARTITION BY pd.ticker ORDER BY pd.date) AS prev_date
                FROM platform.prices_daily pd
                JOIN vol USING (ticker)
                WHERE pd.delisted = false
                  AND pd.date >= CURRENT_DATE - INTERVAL '180 days'
            )
            SELECT ticker, date AS gap_end, prev_date AS gap_start,
                   (date - prev_date) AS gap_days
            FROM per_ticker
            WHERE prev_date IS NOT NULL
              AND (date - prev_date) > 7                       -- > ~5 trading days
              AND date >= CURRENT_DATE - INTERVAL '14 days'    -- CURRENT hole only
            ORDER BY (date - prev_date) DESC
            LIMIT 25
        """)
    if not gap_rows:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="prices_daily_gaps",
            source="prices_daily", severity="OK",
            summary=(
                "no CURRENT (≤14d) multi-day gap on any genuinely-liquid "
                "name (T1/T2 stock, 60d avg vol ≥ 500k) — thin/halted/"
                "post-IPO sparsity excluded by design"
            ),
        ))
    else:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="prices_daily_gaps",
            source="prices_daily", severity="WARN",
            summary=f"{len(gap_rows)} liquid name(s) with a CURRENT multi-day bar gap — ingestion failure",
            evidence={"gaps": [
                {"ticker": r["ticker"], "gap_start": str(r["gap_start"]), "gap_end": str(r["gap_end"]), "gap_days": int(r["gap_days"].days if hasattr(r["gap_days"], 'days') else r["gap_days"])}
                for r in gap_rows[:10]
            ]},
            recommended_action="genuinely-liquid name with a fresh gap = real ingest failure — `ops.py --stage daily_bars --param universe=active --param lookback_days=14 --param end_offset_days=1 --param force_refresh=true --force`",
        ))

    # ETF AR mis-estimation (T3/T4 ETFs with median_spread > 0.5%).
    async with pool.acquire() as conn:
        etfs = await conn.fetch("""
            SELECT lt.ticker, lt.tier, lt.median_spread_pct
            FROM platform.liquidity_tiers lt
            JOIN platform.ticker_classifications tc ON tc.ticker=lt.ticker
            WHERE tc.asset_class='etf' AND lt.tier >= 3 AND lt.median_spread_pct > 0.005
            ORDER BY lt.median_spread_pct DESC LIMIT 20
        """)
    if etfs:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="etf_ar_noise",
            source="liquidity_tiers", severity="OK",
            summary=f"{len(etfs)} ETF(s) in T3/T4 with median_spread > 0.5% — known AR over-attribution for wide-range ETFs",
            evidence={"top": [{"ticker": e["ticker"], "tier": e["tier"], "median_spread_pct": float(e["median_spread_pct"])} for e in etfs[:10]]},
            recommended_action="future calibration pass on AR estimator's ETF treatment",
        ))
    else:
        findings.append(AuditFinding(
            phase="known_unknowns", check_name="etf_ar_noise",
            source="liquidity_tiers", severity="OK",
            summary="no T3/T4 ETFs exceed the AR-noise threshold",
        ))

    return findings


# ─── Phase 3: unknown_knowns ───────────────────────────────────────────


async def run_unknown_knowns(pool, sink: _FindingSink | None = None) -> list[AuditFinding]:
    findings: list[AuditFinding] = sink if sink is not None else []

    # Filter-diagnostics distribution from SIGNAL events (last 30 days).
    async with pool.acquire() as conn:
        sig_rows = await conn.fetch("""
            SELECT engine, data
            FROM platform.application_log
            WHERE event_type='SIGNAL'
              AND recorded_at > NOW() - INTERVAL '30 days'
              AND data ? 'filter_diagnostics'
            ORDER BY recorded_at DESC LIMIT 500
        """)
    if not sig_rows:
        findings.append(AuditFinding(
            phase="unknown_knowns", check_name="filter_diagnostics",
            source="application_log", severity="WARN",
            summary="no SIGNAL events with filter_diagnostics in the last 30 days",
            recommended_action="verify engines populate FilterDiagnostics in setup_detection",
        ))
    else:
        # Aggregate per engine — count blocks for each gate across signals.
        per_engine: dict[str, dict[str, int]] = {}
        for r in sig_rows:
            eng = r["engine"]
            diag = r["data"].get("filter_diagnostics", {}) if isinstance(r["data"], dict) else {}
            if not isinstance(diag, dict):
                continue
            bucket = per_engine.setdefault(eng, {})
            for k, v in diag.items():
                if k.endswith("_blocked") and isinstance(v, int):
                    bucket[k] = bucket.get(k, 0) + v
        if not per_engine:
            findings.append(AuditFinding(
                phase="unknown_knowns", check_name="filter_diagnostics",
                source="application_log", severity="WARN",
                summary=f"{len(sig_rows)} SIGNAL events but no filter_diagnostics shape recognised",
            ))
        else:
            findings.append(AuditFinding(
                phase="unknown_knowns", check_name="filter_diagnostics",
                source="application_log", severity="OK",
                summary=f"filter-block distribution across {len(per_engine)} engine(s) ({len(sig_rows)} signals)",
                evidence={
                    eng: dict(sorted(gates.items(), key=lambda kv: -kv[1])[:5])
                    for eng, gates in per_engine.items()
                },
            ))

    # Cross-engine ticker overlap from AARs (last 90 days).
    async with pool.acquire() as conn:
        overlap_rows = await conn.fetch("""
            SELECT ticker, ARRAY_AGG(DISTINCT engine ORDER BY engine) AS engines, COUNT(*) AS n
            FROM platform.aar_events
            WHERE recorded_at > NOW() - INTERVAL '90 days'
            GROUP BY ticker
            HAVING COUNT(DISTINCT engine) > 1
            ORDER BY COUNT(DISTINCT engine) DESC, COUNT(*) DESC
            LIMIT 25
        """)
    if not overlap_rows:
        findings.append(AuditFinding(
            phase="unknown_knowns", check_name="cross_engine_overlap",
            source="aar_events", severity="OK",
            summary="no cross-engine ticker overlap in last 90 days",
        ))
    else:
        findings.append(AuditFinding(
            phase="unknown_knowns", check_name="cross_engine_overlap",
            source="aar_events", severity="WARN",
            summary=f"{len(overlap_rows)} ticker(s) traded by multiple engines in last 90 days",
            evidence={"top": [
                {"ticker": r["ticker"], "engines": list(r["engines"]), "trade_count": int(r["n"])}
                for r in overlap_rows[:10]
            ]},
            recommended_action="review allocator + engine universe definitions for unintended overlap",
        ))

    # Application log event-type distribution (last 30 days).
    async with pool.acquire() as conn:
        et_rows = await conn.fetch("""
            SELECT event_type, COUNT(*) AS n
            FROM platform.application_log
            WHERE recorded_at > NOW() - INTERVAL '30 days'
            GROUP BY event_type ORDER BY n DESC
        """)
    if et_rows:
        total = sum(r["n"] for r in et_rows)
        dominant = [r for r in et_rows if r["n"] > total * 0.5]
        top5 = et_rows[:5]
        findings.append(AuditFinding(
            phase="unknown_knowns", check_name="event_type_distribution",
            source="application_log", severity="WARN" if dominant else "OK",
            summary=f"{total:,} events / {len(et_rows)} distinct event_types in last 30d",
            evidence={
                "top5": {r["event_type"]: int(r["n"]) for r in top5},
                "dominant_50pct": [r["event_type"] for r in dominant],
            },
        ))

    # Empty platform tables (unknown-knowns: data shapes we have but never populate).
    async with pool.acquire() as conn:
        tables = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='platform' AND table_type='BASE TABLE'
        """)
    empty: list[str] = []
    for t in tables:
        try:
            async with pool.acquire() as conn:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM platform.{t['table_name']}")
            if int(n or 0) == 0:
                empty.append(t["table_name"])
        except Exception:  # noqa: BLE001
            continue
    # Expected empties (no rows is normal until live trading writes them).
    # ``aar_events`` is empty by design pre-graduation: engines that
    # haven't cleared the DSR/credibility gate are paper-only and don't
    # close positions yet. Will populate as engines graduate to live.
    EXPECTED_EMPTY = {
        "forensics_triggers", "open_orders", "tax_lots",
        "execution_quality_log", "allocations", "parity_drift_log",
        "universe_candidates", "alembic_version", "aar_events",
    }
    unexpected = [t for t in empty if t not in EXPECTED_EMPTY]
    if not unexpected:
        findings.append(AuditFinding(
            phase="unknown_knowns", check_name="empty_tables",
            source="information_schema", severity="OK",
            summary=f"{len(empty)} empty table(s), all expected",
            evidence={"empty": empty},
        ))
    else:
        findings.append(AuditFinding(
            phase="unknown_knowns", check_name="empty_tables",
            source="information_schema", severity="WARN",
            summary=f"{len(unexpected)} unexpected empty table(s)",
            evidence={"unexpected_empty": unexpected, "expected_empty": [t for t in empty if t in EXPECTED_EMPTY]},
        ))

    # Macro indicator pairwise correlations (last 90 days).
    async with pool.acquire() as conn:
        macro_rows = await conn.fetch("""
            SELECT indicator, date, value FROM platform.macro_indicators
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
              AND indicator IN ('sahm_rule','industrial_production','initial_claims','yield_curve','credit_spread')
            ORDER BY indicator, date
        """)
    if macro_rows:
        # Build per-indicator series, then forward-fill align on common dates.
        import pandas as pd  # local import — pandas already in deps
        df = pd.DataFrame(
            [{"indicator": r["indicator"], "date": r["date"], "value": float(r["value"])} for r in macro_rows]
        )
        pivot = df.pivot(index="date", columns="indicator", values="value").sort_index().ffill().dropna()
        if len(pivot) >= 10:
            corr = pivot.corr().round(2)
            inversions: list[dict[str, Any]] = []
            # Heuristic priors — historically yield_curve and credit_spread positively correlate (both stress signals).
            # Sahm + initial_claims positively. Flag big sign reversals from these defaults.
            EXPECTED_POSITIVE = [("yield_curve", "credit_spread"), ("sahm_rule", "initial_claims")]
            for a, b in EXPECTED_POSITIVE:
                if a in corr.columns and b in corr.columns:
                    c = float(corr.loc[a, b])
                    if c < -0.3:
                        inversions.append({"pair": [a, b], "correlation": c})
            findings.append(AuditFinding(
                phase="unknown_knowns", check_name="macro_correlations",
                source="macro_indicators", severity="WARN" if inversions else "OK",
                summary=f"macro pairwise correlations computed across {len(pivot)} aligned days",
                evidence={
                    "correlation_matrix": {a: {b: float(corr.loc[a, b]) for b in corr.columns} for a in corr.columns},
                    "inversions": inversions,
                },
            ))

    return findings


# ─── Phase 4: unknown_unknowns ─────────────────────────────────────────


async def run_unknown_unknowns(pool, sink: _FindingSink | None = None) -> list[AuditFinding]:
    findings: list[AuditFinding] = sink if sink is not None else []

    # Row-count velocity — CADENCE-AWARE (recalibrated 2026-05-15).
    #
    # A flat 7d-vs-prior-7d delta is the right signal for daily-cadence
    # tables (it correctly caught the prices_daily coverage collapse:
    # daily bars should accrue ~continuously, so a sharp drop = real
    # ingest failure). It is structural NOISE for sporadic-cadence
    # tables — corporate_actions (event-driven: splits/dividends
    # cluster around ex-div/earnings) and fundamentals_quarterly
    # (quarterly filings) legitimately swing 80%+ week to week. For
    # those the only real failure is *sustained silence*: zero rows
    # over 30d while the 90d history shows regular activity = a stalled
    # ingest. (table, timestamp_col, cadence).
    velocity_targets = [
        ("prices_daily", "date", "daily"),
        ("sec_insider_transactions", "filing_date", "daily"),
        ("aar_events", "recorded_at", "daily"),
        ("application_log", "recorded_at", "daily"),
        ("corporate_actions", "action_date", "sporadic"),
        ("fundamentals_quarterly", "filing_date", "sporadic"),
    ]
    for table, col, cadence in velocity_targets:
        async with pool.acquire() as conn:
            try:
                if cadence == "daily":
                    row = await conn.fetchrow(f"""
                        SELECT
                            COUNT(*) FILTER (WHERE {col} > NOW() - INTERVAL '7 days') AS recent,
                            COUNT(*) FILTER (WHERE {col} > NOW() - INTERVAL '14 days'
                                             AND {col} <= NOW() - INTERVAL '7 days') AS prior
                        FROM platform.{table}
                    """)
                else:
                    row = await conn.fetchrow(f"""
                        SELECT
                            COUNT(*) FILTER (
                                WHERE {col} > NOW() - INTERVAL '{SPORADIC_RECENT_DAYS} days'
                            ) AS recent,
                            COUNT(*) FILTER (
                                WHERE {col} > NOW() - INTERVAL '{SPORADIC_PRIOR_DAYS} days'
                                  AND {col} <= NOW() - INTERVAL '{SPORADIC_RECENT_DAYS} days'
                            ) AS prior
                        FROM platform.{table}
                    """)
            except Exception as exc:  # noqa: BLE001
                findings.append(AuditFinding(
                    phase="unknown_unknowns", check_name="row_velocity",
                    source=table, severity="WARN",
                    summary=f"velocity check skipped: {exc}"[:120],
                ))
                continue
        recent = int(row["recent"] or 0)
        prior = int(row["prior"] or 0)
        if prior == 0 and recent == 0:
            continue
        if cadence == "daily":
            change_pct = float("inf") if prior == 0 else (recent - prior) / prior
            severity = "WARN" if (abs(change_pct) > 0.5 and prior > 100) else "OK"
            findings.append(AuditFinding(
                phase="unknown_unknowns", check_name="row_velocity",
                source=table, severity=severity,
                summary=f"{table}: {recent:,} rows last 7d vs {prior:,} prior 7d ({change_pct:+.1%})",
                evidence={"recent_7d": recent, "prior_7d": prior,
                          "change_pct": change_pct if change_pct != float('inf') else None,
                          "cadence": "daily"},
            ))
        else:
            # Sporadic: WARN on sustained silence OR severe sustained
            # partial degradation, both over a CLUSTER-ROBUST window.
            # Total silence (zero rows in the recent window while
            # history shows activity) = a fully stalled ingest. Severe
            # partial = recent far below the rate-normalized
            # expectation over a window that, for clustered cadence,
            # is guaranteed to span ≥1 full season (so it cannot be a
            # legitimate inter-cluster lull) while a few stragglers
            # still trickle in (not zero, so not "silent"). See the
            # SPORADIC_* rationale block above for why the recent
            # window is 180d and the baseline a full prior year.
            silent = recent == 0 and prior > 0
            expected = prior * SPORADIC_RATE_FACTOR  # rate-normalized recent expectation
            severe_partial = (
                not silent
                and prior >= SPORADIC_PRIOR_FLOOR
                and recent < expected * SPORADIC_SEVERE_FRAC
            )
            if silent:
                summary = (
                    f"{table} (sporadic): {recent:,} rows last 30d vs "
                    f"{prior:,} prior 90d — SILENT (stalled ingest?)"
                )
                recommended_action = (
                    f"re-run the {table} stage — zero rows in 30d "
                    f"but history shows activity"
                )
            elif severe_partial:
                summary = (
                    f"{table} (sporadic): severe sustained degradation: "
                    f"{recent:,} in {SPORADIC_RECENT_DAYS}d vs ~{expected:.0f} "
                    f"rate-normalized expectation from {prior:,} prior-"
                    f"{SPORADIC_PRIOR_BAND_DAYS}d"
                )
                recommended_action = (
                    f"investigate the {table} ingest — sustained rate "
                    f"collapse to <{SPORADIC_SEVERE_FRAC:.0%} of the "
                    f"rate-normalized {SPORADIC_PRIOR_BAND_DAYS}d "
                    f"expectation over a full-season window (not zero, "
                    f"so the silence check did not fire)"
                )
            else:
                summary = (
                    f"{table} (sporadic): {recent:,} rows last 30d vs "
                    f"{prior:,} prior 90d — within event-cadence variance"
                )
                recommended_action = None
            findings.append(AuditFinding(
                phase="unknown_unknowns", check_name="row_velocity",
                source=table,
                severity="WARN" if (silent or severe_partial) else "OK",
                summary=summary,
                evidence={"recent_30d": recent, "prior_90d": prior, "cadence": "sporadic"},
                recommended_action=recommended_action,
            ))

    # Sudden macro stoppage — most recent value > 3σ from 90-day mean.
    async with pool.acquire() as conn:
        macro_rows = await conn.fetch("""
            SELECT indicator, date, value FROM platform.macro_indicators
            WHERE date >= CURRENT_DATE - INTERVAL '120 days'
            ORDER BY indicator, date
        """)
    series: dict[str, list[tuple[Any, float]]] = {}
    for r in macro_rows:
        series.setdefault(r["indicator"], []).append((r["date"], float(r["value"])))
    for ind, pts in series.items():
        if len(pts) < 20:
            continue
        values = [v for _, v in pts]
        latest_v = values[-1]
        mean = statistics.mean(values[:-1])
        stdev = statistics.pstdev(values[:-1])
        if stdev == 0:
            continue
        z = (latest_v - mean) / stdev
        if abs(z) > 3.0:
            findings.append(AuditFinding(
                phase="unknown_unknowns", check_name="macro_stoppage_3sigma",
                source=f"macro:{ind}", severity="WARN",
                summary=f"{ind} latest {latest_v:.3f} is {z:+.2f}σ from 90-day mean ({mean:.3f}±{stdev:.3f})",
                evidence={"indicator": ind, "z": z, "latest": latest_v, "mean": mean, "stdev": stdev},
            ))

    # Liquidity tier distribution shift (current vs ~30 days ago).
    async with pool.acquire() as conn:
        cur = await conn.fetch("""
            SELECT tier, COUNT(*) AS n FROM platform.liquidity_tiers
            WHERE last_updated > NOW() - INTERVAL '40 days'
            GROUP BY tier ORDER BY tier
        """)
    if cur:
        total = sum(r["n"] for r in cur) or 1
        dist = {int(r["tier"]): int(r["n"]) for r in cur}
        findings.append(AuditFinding(
            phase="unknown_unknowns", check_name="tier_distribution",
            source="liquidity_tiers", severity="OK",
            summary=f"tier distribution: {dict(sorted(dist.items()))} (total {total})",
            evidence={"distribution": dist, "pct": {k: round(v/total, 3) for k, v in dist.items()}},
        ))

    # Engine signal silence (zero signals last 7d but had signals in prior 23d).
    async with pool.acquire() as conn:
        sig_rows = await conn.fetch("""
            SELECT engine,
                   COUNT(*) FILTER (WHERE recorded_at > NOW() - INTERVAL '7 days') AS recent,
                   COUNT(*) FILTER (WHERE recorded_at > NOW() - INTERVAL '30 days'
                                    AND recorded_at <= NOW() - INTERVAL '7 days') AS prior
            FROM platform.application_log
            WHERE event_type='SIGNAL'
              AND recorded_at > NOW() - INTERVAL '30 days'
            GROUP BY engine ORDER BY engine
        """)
    for r in sig_rows:
        recent, prior = int(r["recent"] or 0), int(r["prior"] or 0)
        if recent == 0 and prior > 0:
            findings.append(AuditFinding(
                phase="unknown_unknowns", check_name="signal_silence",
                source=f"engine:{r['engine']}", severity="WARN",
                summary=f"{r['engine']}: 0 signals last 7d (had {prior} in prior 23d) — possible silent universe failure",
                evidence={"engine": r["engine"], "recent_7d": 0, "prior_23d": prior},
                recommended_action=f"check {r['engine']} setup_detection + upstream data",
            ))

    # Database size growth (best-effort; depends on permissions).
    try:
        async with pool.acquire() as conn:
            size_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")
            findings.append(AuditFinding(
                phase="unknown_unknowns", check_name="db_size",
                source="postgres", severity="OK",
                summary=f"db size: {int(size_bytes)/1e9:.2f} GB",
                evidence={"size_bytes": int(size_bytes)},
            ))
    except Exception as exc:  # noqa: BLE001
        findings.append(AuditFinding(
            phase="unknown_unknowns", check_name="db_size",
            source="postgres", severity="WARN",
            summary=f"pg_database_size unavailable: {str(exc)[:120]}",
        ))

    # Correlated multi-source staleness.
    async with pool.acquire() as conn:
        stale_sources = []
        for ds in DATA_SOURCES:
            row = await conn.fetchrow(
                f"SELECT MAX({ds.timestamp_col}) AS mx FROM {ds.table} {ds.where_clause}"
            )
            mx = row["mx"]
            if mx is None:
                continue
            age_days = (datetime.now(UTC).date() - (mx.date() if isinstance(mx, datetime) else mx)).days
            if age_days > ds.freshness_days:
                stale_sources.append((ds.name, age_days))
    if len(stale_sources) >= 3:
        findings.append(AuditFinding(
            phase="unknown_unknowns", check_name="correlated_staleness",
            source="multi_source", severity="FAIL",
            summary=f"{len(stale_sources)} sources stale simultaneously — likely upstream outage, not individual failures",
            evidence={"stale": stale_sources},
            recommended_action="check Alpaca/FMP/FRED reachability before remediating per-source",
        ))
    elif stale_sources:
        findings.append(AuditFinding(
            phase="unknown_unknowns", check_name="correlated_staleness",
            source="multi_source", severity="OK",
            summary=f"{len(stale_sources)} source(s) individually stale (no correlation pattern)",
            evidence={"stale": stale_sources},
        ))
    else:
        findings.append(AuditFinding(
            phase="unknown_unknowns", check_name="correlated_staleness",
            source="multi_source", severity="OK",
            summary="all data sources within freshness thresholds",
        ))

    return findings


# ─── Output + persistence ──────────────────────────────────────────────


# Output is now streamed per-finding in ``amain`` (see ``_on_finding``);
# the old batch renderer was removed when streaming landed (2026-05-15).


async def _persist(pool, findings: list[AuditFinding], run_ts: datetime) -> None:
    """Write each finding as its own row in ``platform.data_quality_log``.

    Source key: ``data_pipeline_audit.<phase>.<check_name>.<source>`` —
    includes the finding's source so multiple findings under the same
    (phase, check_name) — e.g. one freshness row per data source —
    don't collide on the ``(source, timestamp)`` unique constraint.
    """
    rows = []
    for f in findings:
        confidence = {"OK": Decimal("1.000"), "WARN": Decimal("0.700"), "FAIL": Decimal("0.000")}[f.severity]
        # Replace any chars that make the source key awkward to parse later.
        clean_source = f.source.replace(":", "-")
        rows.append((
            f"data_pipeline_audit.{f.phase}.{f.check_name}.{clean_source}",
            run_ts,
            0,
            0,
            f.severity != "OK",
            confidence,
            json.dumps({
                "summary": f.summary, "severity": f.severity,
                "evidence": _jsonify(f.evidence),
                "recommended_action": f.recommended_action,
            })[:8000],
        ))
    if not rows:
        return
    async with pool.acquire() as conn:
        # ON CONFLICT DO NOTHING — re-running the audit within the same
        # second is rare but a no-op if it happens (idempotent).
        await conn.executemany(
            """
            INSERT INTO platform.data_quality_log
                (source, timestamp, latency_ms, missing_bars, stale, confidence, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (source, timestamp) DO NOTHING
            """,
            rows,
        )


# ─── Main ──────────────────────────────────────────────────────────────


PHASES = {
    "known_knowns":     run_known_knowns,
    "known_unknowns":   run_known_unknowns,
    "unknown_knowns":   run_unknown_knowns,
    "unknown_unknowns": run_unknown_unknowns,
}


_SEV_GLYPH = {"OK": "🟢", "WARN": "🟡", "FAIL": "🔴"}


async def amain(args: argparse.Namespace) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    run_ts = datetime.now(UTC)
    try:
        phases = [args.phase] if args.phase else list(PHASES.keys())
        for ph in phases:
            if ph not in PHASES:
                print(f"unknown phase: {ph}", file=sys.stderr)
                return 1

        all_findings: list[AuditFinding] = []
        stream_human = not args.json and not args.silent

        if stream_human:
            print(f"\nPIPELINE AUDIT — {run_ts:%Y-%m-%d %H:%M UTC}  (streaming)")
            print("=" * 76)

        # Per-finding hook: render to stdout + persist immediately so a
        # long audit shows progress and a mid-run crash still saves
        # everything completed so far.
        def _on_finding(f: AuditFinding) -> None:
            if args.source and f.source != args.source:
                return
            all_findings.append(f)
            if stream_human:
                glyph = _SEV_GLYPH.get(f.severity, "?")
                print(f"  {glyph} [{f.phase:<16}] {f.check_name:<26} {f.source:<22} {f.summary}")
                if f.recommended_action and f.severity != "OK":
                    print(f"        → {f.recommended_action}")
                sys.stdout.flush()
            # Fire-and-await the single-row persist. Cheap (ON CONFLICT
            # DO NOTHING); keeps the data_quality_log current as we go.
            persist_queue.append(f)

        persist_queue: list[AuditFinding] = []

        for ph in phases:
            if stream_human:
                print(f"\n### {ph}")
            sink = _FindingSink(on_append=_on_finding)
            await PHASES[ph](pool, sink)
            # Flush this phase's findings to data_quality_log now —
            # crash-safe across phase boundaries.
            if persist_queue:
                await _persist(pool, persist_queue, run_ts)
                persist_queue = []

        if args.json:
            print(json.dumps([f.to_dict() for f in all_findings], indent=2, default=str))
        elif not args.silent:
            counts = {s: sum(1 for f in all_findings if f.severity == s) for s in ("OK", "WARN", "FAIL")}
            print("\n" + "=" * 76)
            print(f"  TOTAL: {len(all_findings)}  🟢 {counts['OK']}  🟡 {counts['WARN']}  🔴 {counts['FAIL']}")

        exit_code = 0
        if args.strict and any(f.severity == "FAIL" for f in all_findings):
            exit_code = 2
        elif any(f.severity == "FAIL" and f.phase == "known_knowns" for f in all_findings):
            exit_code = 1
        return exit_code
    finally:
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--source", default=None, help="filter findings to a single source name")
    p.add_argument("--phase", choices=list(PHASES.keys()), default=None,
                   help="audit only one phase")
    p.add_argument("--silent", action="store_true", help="suppress stdout, only persist to data_quality_log")
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero on any FAIL across all phases (default: only known_knowns failures)")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
