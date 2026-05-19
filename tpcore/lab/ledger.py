"""SP-A — cross-candidate n_trials ledger (engine-FREE, H-S2-1).

The DSR multiple-testing penalty (``compute_dsr_for_verdict``'s
``n_trials``) must reflect *every* configuration the Lab has scored in
pursuit of an edge for a target engine — not one CLI run's ``--trials``.
This module persists each Lab run's trial *spend* as one append-only
``platform.data_quality_log`` row under a disjoint source namespace
``lab_trial_ledger.<target>`` and derives the cumulative count by
SUMming prior spend rows.

Substrate: REUSED ``platform.data_quality_log`` (append-only, PK
``(source, timestamp)``, ``ON CONFLICT DO NOTHING``) via the existing
``DataQualityWriter``/``DataQualityScore`` — NO new table, NO migration
(H-LL-5). The spend is recorded UNCONDITIONALLY at sample time, before
any verdict/abort, so the abort-after-fishing under-count is closed
(H-LL-1). Keyed strictly on the target engine — the coarsest honest key
(H-LL-2). The source is disjoint from ``backtest_credibility.*`` so the
live gate (``graduation_ready``) never sees it (H-LL-4).

Engine-free: imports only ``tpcore.quality.data_quality`` + stdlib
(``check_imports tpcore`` stays green). Pure event-sourced read — same
shape as ``tpcore/supervisor_state.py``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from tpcore.quality.data_quality import DataQualityScore, DataQualityWriter

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

LEDGER_SCHEMA_VERSION = 1
LEDGER_SOURCE_PREFIX = "lab_trial_ledger"


def ledger_source(target: str) -> str:
    """The disjoint source namespace for a target engine's trial spend.

    NEVER ``backtest_credibility.*`` — ``graduation_ready`` reads only
    that prefix, so a ``lab_trial_ledger.*`` row is invisible to the
    live gate by construction (H-LL-4)."""
    return f"{LEDGER_SOURCE_PREFIX}.{target}"


async def record_trial_spend(
    pool: asyncpg.Pool,
    *,
    target: str,
    candidate: str | None,
    trials: int,
    seed: int,
    run_outcome: str = "sampled",
) -> datetime:
    """Emit ONE append-only trial-spend event for this Lab run.

    Unconditional: called right after ``sample_parameters`` succeeds,
    BEFORE the DSR/credibility code and BEFORE every non-result rc
    return — so a run that aborts after fishing still records its spend
    (H-LL-1, the §3.2 spine). ``trials`` is the only load-bearing value
    and is known at sample time. Returns the spend-row timestamp (the
    strict ``<`` boundary the cumulative read uses).

    Append-only ``ON CONFLICT (source, timestamp) DO NOTHING``: a
    pathological same-microsecond collision drops a count (fail-safe
    toward UNDER-count only, not adversarially reachable — H-LL-8); it
    never errors and never double-counts.
    """
    ts = datetime.now(UTC)
    notes = json.dumps(
        {
            "schema": LEDGER_SCHEMA_VERSION,
            "target_engine": target,
            "candidate": candidate,
            "trials": int(trials),
            "seed": int(seed),
            "run_outcome": run_outcome,
        },
        sort_keys=True,
    )
    score = DataQualityScore(
        source=ledger_source(target),
        timestamp=ts,
        latency_ms=0,
        missing_bars=0,
        stale=False,
        confidence=Decimal(0),  # unused for this source (schema 0..1; 0 = N/A)
        notes=notes,
    )
    await DataQualityWriter(pool).write(score)
    return ts


async def cumulative_n_trials(
    pool: asyncpg.Pool,
    target: str,
    before_ts: datetime,
) -> int:
    """Σ of ``trials`` over every PRIOR ``lab_trial_ledger.<target>``
    spend row (``timestamp < before_ts`` — strict, so the run's own
    just-emitted row is excluded; cumulative = all prior spend).

    SUM over an append-only log ⇒ monotone non-decreasing in the number
    of runs against the target (H-LL-2 monotone-harder). 0 for an
    unknown target / first-ever run (then ``n_trials = 0 + args.trials``
    = today's behaviour exactly — strictly additive, no regression).
    """
    sql = """
        SELECT COALESCE(SUM((notes::jsonb->>'trials')::int), 0)
        FROM platform.data_quality_log
        WHERE source = $1 AND timestamp < $2
    """
    async with pool.acquire() as conn:
        total = await conn.fetchval(sql, ledger_source(target), before_ts)
    return int(total or 0)


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "LEDGER_SOURCE_PREFIX",
    "ledger_source",
    "record_trial_spend",
    "cumulative_n_trials",
]
