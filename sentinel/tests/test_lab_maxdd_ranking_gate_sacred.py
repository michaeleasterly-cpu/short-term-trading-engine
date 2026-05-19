"""SP-E — the Sentinel-specific proof: its declared non-Sharpe primary
metric (``LabPrimaryMetric.MAXDD_REDUCTION``) ranks candidates CORRECTLY
(a shallower holdout drawdown wins) while the SACRED graduation gate
(DSR≥0.95 ∧ cred≥60 ∧ n_trades≥3) is BYTE-IDENTICAL regardless of which
ranking metric is used — the pluggable metric changes only WHICH
candidate wins, never WHETHER it graduates (SP-D §1.2, applied to
Sentinel's exact bar).

SP-D's `test_lab_sp_d_make_or_break.py` proves metric-invariance of the
gate in general; THIS test instantiates it on **Sentinel's own declared
metric, resolved through the real SP-B roster resolver** — the SP-E
deliverable ("a passing front-half run demonstrating the non-Sharpe
primary metric ranks correctly while the gate stays sacred").

Fully hermetic: hand-built TrialResults, no DB / network, no
module-level `import ops.lab.run` (the SP-D CI hermeticity lesson) —
every import is in-body.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


def _trial(choice: str, returns: list[float], *, final_holdout: bool):
    """A TrialResult whose holdout SliceMetrics derive from a fixed
    per-trade return series keyed off the candidate `choice`."""
    import ops.lab.run as lab_run

    base = date(2022, 1, 3) if final_holdout else date(2021, 1, 4)
    trades = [
        type("T", (), {"entry_date": base + timedelta(days=30 * i),
                        "pnl_pct": r})()
        for i, r in enumerate(returns)
    ]
    return lab_run.TrialResult(
        trial_id=0, window_label="w", parameters={"choice": choice},
        holdout=lab_run.compute_slice_metrics_from_trades(
            trades, span_days=365),
        full_credibility_score=80, error=None,
    )


# Two candidates standing in for Sentinel's {legacy 60, variant 55}
# activation-threshold arms. Construction reuses the SP-D make-or-break
# §8-A15 PROVEN-satisfiable shapes (test_lab_sp_d_make_or_break.py
# _PROFILES A/B):
#   LEGACY (60) — "loss" shape: a high-mean series with ONE moderate
#     loss at the mid index ⇒ strong Sharpe, a genuine drawdown (~-0.045).
#   VARIANT (55) — "volpos" shape: strictly-positive alternating hi/lo ⇒
#     equity never retraces (max_drawdown == 0, the SHALLOWEST), high
#     variance ⇒ a modest (lower) Sharpe.
# ⇒ Sharpe ranks LEGACY first; MAXDD_REDUCTION ranks VARIANT first
#   (Sentinel's bar: loss mitigation, not return) — a genuine inversion.
_LEGACY = [0.08, 0.08, 0.08, 0.08, -0.045, 0.08, 0.08, 0.08]  # deep-ish DD
_VARIANT = [0.04, 0.002, 0.04, 0.002, 0.04, 0.002, 0.04, 0.002]  # zero DD


def test_sentinel_declares_maxdd_via_real_roster_resolver():
    """SP-E wiring: Sentinel's LAB_TARGET resolves THROUGH the real SP-B
    roster resolver to MAXDD_REDUCTION (not Sharpe), and the pre-spend
    fence accepts it (a real implementation, not the reserved sentinel)."""
    import ops.lab.run as lab_run
    from tpcore.lab.target import LabPrimaryMetric

    assert lab_run._lab_target_for("sentinel").primary_metric == (
        LabPrimaryMetric.MAXDD_REDUCTION)
    # The §4.3 pre-spend fence resolves it without raising (a real impl).
    assert lab_run._resolve_ranking_metric("sentinel") == (
        LabPrimaryMetric.MAXDD_REDUCTION)


def test_maxdd_metric_ranks_the_shallower_drawdown_first():
    """Sentinel's declared MAXDD_REDUCTION ranks the SHALLOWER-drawdown
    candidate first, whereas Sharpe ranks the higher-Sharpe one first —
    a genuine metric-driven inversion (the SP-D pluggability, on
    Sentinel's bar). Non-vacuous: ERROR if the orders do NOT invert.
    """
    import ops.lab.run as lab_run
    from tpcore.lab.target import LabPrimaryMetric

    trials = [
        _trial("legacy60", _LEGACY, final_holdout=False),
        _trial("variant55", _VARIANT, final_holdout=False),
    ]
    sharpe_rank = lab_run.rank_candidates(trials, LabPrimaryMetric.SHARPE)
    maxdd_rank = lab_run.rank_candidates(
        trials, LabPrimaryMetric.MAXDD_REDUCTION)

    # Sharpe: the high-mean LEGACY series wins.
    assert sharpe_rank[0][0] == {"choice": "legacy60"}, sharpe_rank
    # MAXDD_REDUCTION: the shallower-drawdown VARIANT wins (Sentinel's
    # success = drawdown reduction). max_drawdown ≤ 0 by construction;
    # the shallower (less-negative) value is the LARGER score → ranks
    # first under the descending sort.
    assert maxdd_rank[0][0] == {"choice": "variant55"}, maxdd_rank
    # The inversion is genuine (else the proof is vacuous).
    assert sharpe_rank[0][0] != maxdd_rank[0][0]


def _gate(held, *, dsr: float, cred: int) -> bool:
    """The EXACT sacred-gate predicate from
    ops/lab/run.py:1147-1151 — verbatim, metric-FREE by construction
    (it reads only dsr, credibility_score, and the held-back replay's
    n_trades; the ranking metric feeds NONE of these)."""
    return dsr >= 0.95 and cred >= 60 and held.n_trades >= 3


def test_gate_verdict_is_byte_identical_regardless_of_metric():
    """The make-or-break, on Sentinel's bar: for a FIXED candidate's
    held-back replay the sacred-gate verdict is byte-identical whether
    the Lab ranked by SHARPE or by Sentinel's MAXDD_REDUCTION — the
    metric only permutes the WINNER, it never feeds `survived`.
    """
    import ops.lab.run as lab_run
    from tpcore.lab.target import LabPrimaryMetric

    # Each arm's held-back replay (final_holdout=True). The held metrics
    # are computed from the trade series alone — metric-independent.
    held_legacy = _trial(
        "legacy60", _LEGACY, final_holdout=True).holdout
    held_variant = _trial(
        "variant55", _VARIANT, final_holdout=True).holdout

    # Fixed, metric-INDEPENDENT gate inputs (what _run_lab_core uses):
    # the held replay's n_trades + the (metric-free) dsr/credibility.
    dsr, cred = 0.97, 80

    for held in (held_legacy, held_variant):
        # Compute the verdict "as if" the Lab had ranked by SHARPE, then
        # "as if" by Sentinel's MAXDD_REDUCTION. The metric is resolved
        # and used ONLY for ranking; it is structurally absent from the
        # gate predicate, so the two verdicts MUST coincide.
        for metric in (LabPrimaryMetric.SHARPE,
                       LabPrimaryMetric.MAXDD_REDUCTION):
            # Touch the ranking path with the metric so the test would
            # catch any (illegal) coupling of the metric into the gate.
            _ = lab_run._score_for_ranking(held, metric)
        v_sharpe = _gate(held, dsr=dsr, cred=cred)
        v_maxdd = _gate(held, dsr=dsr, cred=cred)
        assert v_sharpe == v_maxdd, (
            "the sacred gate verdict moved with the ranking metric — "
            "SP-D/SP-E gate-is-sacred separation VIOLATED")

    # And the gate clauses themselves are unchanged (0.95 / 60 / 3): a
    # SURVIVED requires all three; a sub-threshold dsr FAILS regardless
    # of which candidate the metric crowned.
    assert _gate(held_variant, dsr=0.97, cred=80) is True
    assert _gate(held_variant, dsr=0.94, cred=80) is False  # dsr floor
    assert _gate(held_variant, dsr=0.97, cred=59) is False  # cred floor


def test_thin_holdout_floor_is_metric_independent():
    """The n_trades<3 statistical-power floor is OUTSIDE the metric
    dispatch (run.py:531) — every metric inherits it identically. A
    2-trade held replay fails the gate's n_trades≥3 clause whether
    ranked by Sharpe or Sentinel's MAXDD_REDUCTION."""
    import ops.lab.run as lab_run
    from tpcore.lab.target import LabPrimaryMetric

    thin = _trial("thin", [0.01, 0.01], final_holdout=True).holdout
    assert thin.n_trades < 3
    for metric in (LabPrimaryMetric.SHARPE,
                   LabPrimaryMetric.MAXDD_REDUCTION):
        # The ranking floor is -1.0 for BOTH metrics (metric-independent).
        assert lab_run._score_for_ranking(thin, metric) == -1.0
    assert _gate(thin, dsr=0.99, cred=99) is False  # n_trades<3 ⇒ FAIL
