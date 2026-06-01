#!/usr/bin/env python3
"""F1 backfill — five EDGE_VALIDATION_PLAN.md research-failure records.

The findings in ``docs/EDGE_VALIDATION_PLAN.md`` (2026-05-13/14) are
the operator's first-hand research narrative across Sigma, Reversion,
Vector, and Momentum. F1 makes them queryable: this script inserts the
five canonical records into ``platform.failed_alpha_ledger`` so the
dashboard, future research, and any automated reviewer can read them
without parsing prose.

The records are HAND-CODED here (not scraped) because the doc's
structure is not stable and the metrics + blocking constraints
encode operator judgement the scraper would only approximate.
Each record cites its ``source_doc`` so the prose remains the
narrative anchor.

Usage::

    # Preview (default — never writes):
    python scripts/backfill_failed_alpha_ledger.py

    # Live (idempotent — re-runs skip via ON CONFLICT):
    DATABASE_URL=$DATABASE_URL_IPV4 \\
        python scripts/backfill_failed_alpha_ledger.py --write

Operator hard rule: idempotent. The UNIQUE (engine, sweep_id) index
on the table drives ``ON CONFLICT DO NOTHING`` — re-running this
script is safe.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date

import structlog

from tpcore.forensics.alpha_ledger import (
    BlockingConstraint,
    FailedAlphaRecord,
    FailedAlphaStatus,
    record_failed_alpha,
)

logger = structlog.get_logger(__name__)


# Source narrative: docs/EDGE_VALIDATION_PLAN.md, the
# "Status (2026-05-14 — post FRED + SEC integration, …)" table +
# the "Recalibration sequencing (2026-05-14)" prose.
_SOURCE_DOC = "docs/EDGE_VALIDATION_PLAN.md"
_BACKFILL_WINDOW_START = date(2020, 1, 1)  # tier-aware cost backtest start
_BACKFILL_WINDOW_END = date(2026, 5, 14)
_UNIVERSE = "T1+T2"  # universal across the four engines per the doc


def _build_backfill_records() -> list[FailedAlphaRecord]:
    """Return the five EDGE_VALIDATION_PLAN.md records, hand-coded.

    Modifying any of these requires updating the corresponding test
    assertion in ``tests/test_failed_alpha_ledger.py`` AND adding a
    note to the doc explaining what changed.
    """
    return [
        # Sigma 2026-05-13 — initial sweep, DSR failure.
        FailedAlphaRecord(
            engine="sigma",
            strategy_family="trend_following_with_chop_gate",
            sweep_id="sigma-2026-05-13",
            source_doc=f"{_SOURCE_DOC}#sigma-2026-05-13",
            data_window_start=_BACKFILL_WINDOW_START,
            data_window_end=date(2026, 5, 13),
            universe=_UNIVERSE,
            n_trials=1,
            credibility_score=55,
            blocking_constraint=BlockingConstraint.DSR_FAILURE,
            blocking_metric="DSR<0.95 (top OOS +1.150)",
            failure_summary=(
                "Sigma top OOS score +1.150 (2026-05-13 sweep). DSR < "
                "0.95 after n_trials multiple-testing correction. "
                "Same params Sharpe +1.02 on 2018-21 collapse to -0.84 "
                "on 2019-22 — DSR failure compounds with the regime "
                "fragility seen the next day."
            ),
            revisit_condition=(
                "Regime-aware variant + signal-class change (not "
                "parameter tuning)"
            ),
            status=FailedAlphaStatus.SHELVED,
        ),
        # Sigma 2026-05-14 — follow-up; archived.
        FailedAlphaRecord(
            engine="sigma",
            strategy_family="trend_following_with_chop_gate",
            sweep_id="sigma-2026-05-14",
            source_doc=f"{_SOURCE_DOC}#sigma-2026-05-14",
            data_window_start=_BACKFILL_WINDOW_START,
            data_window_end=_BACKFILL_WINDOW_END,
            universe=_UNIVERSE,
            n_trials=1,
            credibility_score=55,
            blocking_constraint=BlockingConstraint.REGIME_FRAGILITY,
            blocking_metric="chop=47.7 hold=2d stop=1.8% — 2019-22 OOS Sharpe -0.84",
            failure_summary=(
                "Sigma 2026-05-14 follow-up confirmed regime fragility "
                "is the binding constraint, not parameters. Same "
                "(chop=47.7, hold=2d, stop=1.8%) config that produced "
                "Sharpe +1.02 on 2018-21 collapsed to Sharpe -0.84 on "
                "2019-22. No parameter changes would have rescued "
                "this — Sigma archived 2026-05-16 to archive/sigma/ "
                "(EULOGY.md)."
            ),
            revisit_condition=(
                "Different strategy class entirely; not Sigma"
            ),
            status=FailedAlphaStatus.ARCHIVED,
        ),
        # Reversion 2026-05-14 — 100-trial sweep, signal-sparse.
        FailedAlphaRecord(
            engine="reversion",
            strategy_family="mean_reversion_zscore",
            sweep_id="reversion-2026-05-14",
            source_doc=f"{_SOURCE_DOC}#reversion-2026-05-14",
            data_window_start=_BACKFILL_WINDOW_START,
            data_window_end=_BACKFILL_WINDOW_END,
            universe=_UNIVERSE,
            n_trials=150,
            n_trades=2,  # OP doc: "+0.43 on 2 trades"
            credibility_score=45,
            blocking_constraint=BlockingConstraint.N_TRADES_LOW,
            blocking_metric=(
                "150/150 trials credibility=45 ceiling — 1-3 trades/window"
            ),
            failure_summary=(
                "Reversion 100-trial sweep: 150/150 trials produced "
                "credibility = 45 (structural ceiling, not noise). "
                "Signal too sparse on T1+T2 (1-3 trades/window) for "
                "any parameters to clear DSR. The 45 ceiling = same "
                "as Vector → common pattern: T1+T2 is too narrow for "
                "these signal classes to clear DSR's multiple-testing "
                "correction. Reversion shelved at the engine layer."
            ),
            revisit_condition=(
                "Different signal class (e.g. volume-anomaly trigger "
                "instead of Z-score) OR universe expansion to T3+ "
                "(requires fundamentals coverage that doesn't exist "
                "today)"
            ),
            status=FailedAlphaStatus.SHELVED,
        ),
        # Vector 2026-05-14 — pb_ceiling 1.5–3.5 sweep, multi-gate.
        FailedAlphaRecord(
            engine="vector",
            strategy_family="multi_gate_value_with_pb_ceiling",
            sweep_id="vector-2026-05-14",
            source_doc=f"{_SOURCE_DOC}#vector-2026-05-14",
            data_window_start=_BACKFILL_WINDOW_START,
            data_window_end=_BACKFILL_WINDOW_END,
            universe=_UNIVERSE,
            n_trials=150,
            credibility_score=45,
            blocking_constraint=BlockingConstraint.MULTI_GATE_INTERSECTION,
            blocking_metric=(
                "0/150 pass — pb_ceiling sweep 1.5-3.5; 1-5 trades/window regardless"
            ),
            failure_summary=(
                "Vector pb_ceiling sweep (1.5-3.5): 0/150 trials "
                "passed, all credibility=45. Relaxing P/B widened "
                "the upstream pool but downstream gates still cap "
                "trade count at 1-5/window regardless of params. "
                "Multi-gate intersection is too restrictive for T1+T2 "
                "signal density — the binding constraint is strategy "
                "design, not catalyst source (SEC NLP also "
                "DEFERRED — proven not the bottleneck)."
            ),
            revisit_condition=(
                "Drop a gate / change to a different signal class; "
                "OR universe expansion to T3+; OR accept paper "
                "trading + live tracking as the operational gate"
            ),
            status=FailedAlphaStatus.SHELVED,
        ),
        # Momentum 2026-05-14 — paper-trading per spec.
        FailedAlphaRecord(
            engine="momentum",
            strategy_family="cross_sectional_12_1_momentum",
            sweep_id="momentum-2026-05-14",
            source_doc=f"{_SOURCE_DOC}#momentum-2026-05-14",
            data_window_start=_BACKFILL_WINDOW_START,
            data_window_end=_BACKFILL_WINDOW_END,
            universe=_UNIVERSE,
            n_trials=1,
            credibility_score=45,
            blocking_constraint=BlockingConstraint.OPERATOR_REJECTED,
            blocking_metric="paper-trading per momentum spec (top OOS +0.784)",
            failure_summary=(
                "Momentum top OOS +0.784 (2026-05-14). The momentum "
                "spec explicitly mandates paper-trading regardless of "
                "credibility — the engine stays PAPER without a "
                "discretionary live-promotion gate. This is an "
                "OPERATOR-LEVEL decision, not a DSR/PSR/PBO failure "
                "of the math; the strategy proceeds in PAPER + live-"
                "tracking mode by design."
            ),
            revisit_condition=(
                "Operator-directed change to the momentum spec's "
                "PAPER-only mandate"
            ),
            status=FailedAlphaStatus.SHELVED,
        ),
    ]


async def _async_main(write: bool) -> int:
    records = _build_backfill_records()

    if not write:
        # Dry-run: just print what would be inserted.
        print("F1 backfill (dry run — no DB writes)")
        print(
            f"  source_doc base: {_SOURCE_DOC}\n"
            f"  records to insert: {len(records)}\n"
        )
        for r in records:
            print(
                f"  [{r.engine:>10}] sweep_id={r.sweep_id:<25} "
                f"blocking={r.blocking_constraint.value:<27} "
                f"status={r.status.value}"
            )
        return 0

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error(
            "scripts.backfill_failed_alpha_ledger.no_dsn",
            detail="set DATABASE_URL or DATABASE_URL_IPV4 to live-write",
        )
        return 2

    try:
        import asyncpg
    except ImportError as exc:
        logger.error(
            "scripts.backfill_failed_alpha_ledger.no_asyncpg",
            error=str(exc),
        )
        return 3

    pool = await asyncpg.create_pool(dsn, statement_cache_size=0)
    inserted_count = 0
    skipped_count = 0
    try:
        for r in records:
            result = await record_failed_alpha(pool, r)
            if result.inserted:
                inserted_count += 1
                print(
                    f"INSERTED {r.engine}/{r.sweep_id} "
                    f"(id={result.record_id}, "
                    f"blocking={r.blocking_constraint.value})"
                )
            else:
                skipped_count += 1
                print(
                    f"SKIPPED  {r.engine}/{r.sweep_id} "
                    f"(already exists)"
                )
    finally:
        await pool.close()

    print(
        f"\nF1 backfill complete: inserted={inserted_count} "
        f"skipped(idempotent)={skipped_count} total={len(records)}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the five EDGE_VALIDATION_PLAN.md records into "
            "platform.failed_alpha_ledger. Dry run by default."
        ),
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Apply writes. Without this flag, the script previews "
             "the records and exits 0.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_async_main(write=args.write)))


if __name__ == "__main__":  # pragma: no cover
    main()
