"""Failed-alpha ledger — durable typed research-failure record (F1).

Companion to ``tpcore/backtest/credibility.py``. When an engine /
sweep / signal-class research run fails to clear the credibility gate
(score < 60, or DSR < 0.95, or any of the López de Prado overfitting
tests), the dispositive reason + metrics land here as a TYPED ROW —
not as prose in a doc, not as a JSON blob in ``data_quality_log``.

Why a dedicated table (per F1 operator decision 2026-06-01):

  * Failed-alpha records are core research intelligence. The dashboard
    queries them; future research re-proposes against them; Claude
    (in the heavy-lane review action) consults them to flag a recurring
    blocking-constraint pattern.
  * Real SQL types: ``dsr double precision``, ``n_trials integer``,
    ``credibility_score integer CHECK (0..100)``. Not nested JSON.
  * Real constraints: ``blocking_constraint NOT NULL CHECK enum``,
    ``failure_summary NOT NULL CHECK length > 0``. The schema enforces
    the operator's "no graveyard of unclassified failures" rule.

The substrate is ``platform.failed_alpha_ledger`` (migration
``20260601_0100``). Each row is uniquely identified by
``(engine, sweep_id)`` — re-running the backfill never duplicates.

Operator hard rules enforced HERE (Pydantic) AND at the schema:

  * blocking_constraint MUST be present (Pydantic + SQL NOT NULL +
    CHECK enum).
  * failure_summary MUST be a non-empty string (Pydantic
    field-validator + SQL CHECK length > 0).
  * credibility_score, when present, MUST be 0..100 (Pydantic
    field-validator + SQL CHECK).
  * status MUST be one of the four lifecycle values (Pydantic StrEnum
    + SQL CHECK enum).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


LEDGER_TABLE: str = "platform.failed_alpha_ledger"
LEDGER_SCHEMA_VERSION: int = 1


class BlockingConstraint(StrEnum):
    """Dispositive reason an engine / sweep / signal-class research
    run failed to clear the credibility gate. ORDER REFLECTS rough
    "easiest to fix → hardest to fix" so dashboard sorts make sense.

    Pinned via the SQL CHECK enum at migration ``20260601_0100``;
    re-aligning the enum requires both a Pydantic update + a CHECK-
    constraint replacement (a follow-up migration).
    """

    # Sample-size / signal-density class.
    N_TRADES_LOW = "n_trades_low"
    LIQUIDITY_FAILURE = "liquidity_failure"

    # Statistical-validation class.
    DSR_FAILURE = "dsr_failure"
    PSR_FAILURE = "psr_failure"
    PBO_FAILURE = "pbo_failure"
    MIN_BTL_FAILURE = "min_btl_failure"

    # Structural strategy-design class.
    SIGNAL_CLASS_MISMATCH = "signal_class_mismatch"
    MULTI_GATE_INTERSECTION = "multi_gate_intersection"
    REGIME_FRAGILITY = "regime_fragility"

    # Operational / hygiene class.
    COST_DOMINATED = "cost_dominated"
    DATA_QUALITY_FAILURE = "data_quality_failure"
    LOOKAHEAD_OR_BIAS_RISK = "lookahead_or_bias_risk"
    OPERATOR_REJECTED = "operator_rejected"

    # Deprecated-source class — historical strategies that depended
    # on a feed/signal source that's since been retired.
    DEPRECATED_SIGNAL_SOURCE = "deprecated_signal_source"


class FailedAlphaStatus(StrEnum):
    """Lifecycle state of a failed-alpha row. Default for new rows is
    FAILED — promotes to SHELVED / ARCHIVED / REVISIT_QUEUED as the
    operator makes the call.
    """

    FAILED = "FAILED"            # active failure record; no action taken
    SHELVED = "SHELVED"          # operator parked it pending new evidence
    ARCHIVED = "ARCHIVED"        # engine removed from active set
    REVISIT_QUEUED = "REVISIT_QUEUED"  # blocked on a known precondition


class FailedAlphaRecord(BaseModel):
    """One typed failed-alpha record. The Pydantic model is the
    write-side gate; the SQL CHECK constraints are the read-side
    floor. Both must agree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ─── Identity + provenance ─────────────────────────────────────
    schema_version: int = Field(default=LEDGER_SCHEMA_VERSION)
    engine: str = Field(min_length=1)
    strategy_family: str | None = None
    sweep_id: str = Field(min_length=1)
    run_id: str | None = None
    commit_sha: str | None = None
    source_doc: str | None = None

    # ─── Sweep / data context ──────────────────────────────────────
    data_window_start: date
    data_window_end: date
    universe: str = Field(min_length=1)
    n_trials: int = Field(ge=0)
    n_trades: int | None = Field(default=None, ge=0)
    parameter_count: int | None = Field(default=None, ge=0)

    # ─── Credibility metrics (whichever were computed) ─────────────
    dsr: float | None = None
    psr_at_zero: float | None = None
    pbo: float | None = None
    min_btl_periods: int | None = None
    backtest_periods: int | None = None
    credibility_score: int | None = Field(default=None, ge=0, le=100)
    max_drawdown: float | None = None
    sharpe: float | None = None
    turnover: float | None = None
    cost_assumption_bps: float | None = None

    # ─── Verdict (mandatory) ───────────────────────────────────────
    blocking_constraint: BlockingConstraint
    blocking_metric: str | None = None
    failure_summary: str

    # ─── Revisit + lifecycle ───────────────────────────────────────
    revisit_condition: str | None = None
    status: FailedAlphaStatus = FailedAlphaStatus.FAILED

    # ─── Non-canonical extras (NEVER store core fields here) ───────
    metadata: dict[str, Any] | None = None

    @field_validator("failure_summary")
    @classmethod
    def _summary_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "failure_summary must explain WHY the run failed — "
                "empty/whitespace-only rejected per the 'no graveyard "
                "of unclassified failures' invariant"
            )
        return v

    @field_validator("data_window_end")
    @classmethod
    def _window_ordered(cls, v: date, info: Any) -> date:
        start = info.data.get("data_window_start")
        if start is not None and v < start:
            raise ValueError(
                f"data_window_end ({v}) must be >= data_window_start "
                f"({start})"
            )
        return v


@dataclass(frozen=True)
class RecordResult:
    """Outcome of a ``record_failed_alpha`` insert. The frozen
    dataclass mirrors the existing ``HealOneResult`` /
    ``ParityFreshness`` convention so callers can pattern-match."""

    inserted: bool
    engine: str
    sweep_id: str
    record_id: int | None
    reason: str  # operator-readable summary


def ledger_source(engine: str, sweep_id: str) -> str:
    """Optional disjoint audit prefix when an EVENT (not a canonical
    record) needs to be emitted on ``application_log`` — e.g. a
    weekly digest mention. The canonical store is the dedicated
    table; this prefix is only for the audit-trail emission path.

    NEVER collides with ``backtest_credibility.*`` or
    ``lab_trial_ledger.*`` (the live-gate + DSR-multiple-testing
    ledgers respectively)."""
    return f"failed_alpha_ledger.{engine}.{sweep_id}"


_INSERT_SQL = """
    INSERT INTO platform.failed_alpha_ledger (
        engine, strategy_family, sweep_id, run_id, commit_sha,
        source_doc,
        data_window_start, data_window_end, universe,
        n_trials, n_trades, parameter_count,
        dsr, psr_at_zero, pbo,
        min_btl_periods, backtest_periods,
        credibility_score, max_drawdown, sharpe, turnover,
        cost_assumption_bps,
        blocking_constraint, blocking_metric, failure_summary,
        revisit_condition, status, metadata
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6,
        $7, $8, $9,
        $10, $11, $12,
        $13, $14, $15,
        $16, $17,
        $18, $19, $20, $21,
        $22,
        $23, $24, $25,
        $26, $27, $28::jsonb
    )
    ON CONFLICT (engine, sweep_id) DO NOTHING
    RETURNING id
"""


async def record_failed_alpha(
    pool: asyncpg.Pool, record: FailedAlphaRecord,
) -> RecordResult:
    """Insert ``record`` into ``platform.failed_alpha_ledger``.

    Idempotent: the UNIQUE (engine, sweep_id) index drives an
    ``ON CONFLICT DO NOTHING`` — re-running the backfill never
    duplicates. Returns ``RecordResult(inserted=False, …)`` when the
    row was already present.

    Operator hard rules enforced UPSTREAM (Pydantic): every record
    has a non-null ``blocking_constraint`` + non-empty
    ``failure_summary`` by construction.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_SQL,
            record.engine,
            record.strategy_family,
            record.sweep_id,
            record.run_id,
            record.commit_sha,
            record.source_doc,
            record.data_window_start,
            record.data_window_end,
            record.universe,
            record.n_trials,
            record.n_trades,
            record.parameter_count,
            record.dsr,
            record.psr_at_zero,
            record.pbo,
            record.min_btl_periods,
            record.backtest_periods,
            record.credibility_score,
            record.max_drawdown,
            record.sharpe,
            record.turnover,
            record.cost_assumption_bps,
            record.blocking_constraint.value,
            record.blocking_metric,
            record.failure_summary,
            record.revisit_condition,
            record.status.value,
            json.dumps(record.metadata) if record.metadata else None,
        )

    if row is None:
        logger.info(
            "tpcore.forensics.failed_alpha.already_exists",
            engine=record.engine, sweep_id=record.sweep_id,
        )
        return RecordResult(
            inserted=False,
            engine=record.engine,
            sweep_id=record.sweep_id,
            record_id=None,
            reason=(
                f"(engine={record.engine!r}, sweep_id={record.sweep_id!r}) "
                "already in failed_alpha_ledger — skipped on conflict"
            ),
        )
    logger.info(
        "tpcore.forensics.failed_alpha.recorded",
        engine=record.engine, sweep_id=record.sweep_id,
        blocking_constraint=record.blocking_constraint.value,
        record_id=int(row["id"]),
    )
    return RecordResult(
        inserted=True,
        engine=record.engine,
        sweep_id=record.sweep_id,
        record_id=int(row["id"]),
        reason=(
            f"recorded blocking_constraint="
            f"{record.blocking_constraint.value}"
        ),
    )


async def list_failed_alpha(
    pool: asyncpg.Pool, *, engine: str | None = None,
    blocking_constraint: BlockingConstraint | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Operator-facing read. Used by the dashboard's research view
    and by any future automated reviewer (the heavy-lane Claude
    action can quote prior failures here when assessing a new sweep).

    Returns latest first by ``recorded_at``. ``limit`` caps at 100
    by default to keep dashboard renders snappy.
    """
    where_parts: list[str] = []
    args: list[Any] = []
    n = 1
    if engine is not None:
        where_parts.append(f"engine = ${n}")
        args.append(engine)
        n += 1
    if blocking_constraint is not None:
        where_parts.append(f"blocking_constraint = ${n}")
        args.append(blocking_constraint.value)
        n += 1
    where_clause = (
        f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    )
    args.append(int(limit))
    sql = (
        "SELECT id, recorded_at, engine, sweep_id, "
        "blocking_constraint, blocking_metric, failure_summary, "
        "credibility_score, dsr, status "
        f"FROM {LEDGER_TABLE} {where_clause} "
        f"ORDER BY recorded_at DESC LIMIT ${n}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "recorded_at": r["recorded_at"].astimezone(UTC).isoformat()
            if r["recorded_at"] is not None else None,
            "engine": r["engine"],
            "sweep_id": r["sweep_id"],
            "blocking_constraint": r["blocking_constraint"],
            "blocking_metric": r["blocking_metric"],
            "failure_summary": r["failure_summary"],
            "credibility_score": (
                int(r["credibility_score"])
                if r["credibility_score"] is not None else None
            ),
            "dsr": (
                float(r["dsr"]) if r["dsr"] is not None else None
            ),
            "status": r["status"],
        })
    return out


__all__ = [
    "BlockingConstraint",
    "FailedAlphaRecord",
    "FailedAlphaStatus",
    "LEDGER_SCHEMA_VERSION",
    "LEDGER_TABLE",
    "RecordResult",
    "ledger_source",
    "list_failed_alpha",
    "record_failed_alpha",
]


# Silence unused-import warning while keeping ``datetime`` on hand for
# downstream callers that import it via this module.
_ = datetime
