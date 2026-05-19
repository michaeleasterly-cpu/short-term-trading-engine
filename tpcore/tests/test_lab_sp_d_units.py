"""SP-D §5.5 — focused units. This file grows in Task 7; Task 2 lands
ONLY the pre-SP-D-sidecar forcing regression (RED until §2.4's defaulted
LabResult.primary_metric exists)."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

# The EXACT key set of the verified-real pre-SP-D sidecar
# docs/lab/2026-05-18-exp1-SURVIVED-seed7.json (NO `primary_metric` key).
# Inlined deliberately — NOT read from the live docs/ tree.
_PRE_SP_D_SIDECAR = {
    "candidate": "exp1",
    "target_engine": "reversion",
    "intent": "fold_existing",
    "verdict": "SURVIVED",
    "dsr": 0.97,
    "credibility_score": 72,
    "credibility_rubric": {
        "lookahead_clean": True, "survivorship_inclusive": True,
        "pit_fundamentals": True, "regime_coverage": True,
        "out_of_sample_validated": True, "monte_carlo_drawdown": True,
        "sensitivity_surface_flat": False,
        "monte_carlo_sequence_passed": False,
        "dsr_above_pass_threshold": False,
        "backtest_length_above_minbtl": False,
        "pbo_passes": False, "trades_per_param_passes": False,
        "score": 72, "notes": None,
    },
    "held_metrics": {
        "sharpe": 1.1, "profit_factor": 1.6, "max_drawdown": -0.08,
        "n_trades": 12, "win_rate": 0.55,
    },
    "winning_params": {"z_threshold": 3.2},
    "param_diff": [{"name": "z_threshold", "current": 3.0,
                    "winning": 3.2}],
    "recommended_exit": "fold_existing",
    "ranked_alternatives": [],
    "walk_windows": [],
    "n_trials": 200,
    "seed": 7,
    "generated_at": "2026-05-18T00:00:00Z",
}


def test_pre_sp_d_sidecar_validates_and_defaults_to_sharpe():
    """A pre-SP-D sidecar with NO `primary_metric` key still
    model_validates and resolves to SHARPE (the default). REDs if the
    field is ever made required (§2.4 / §8-A11)."""
    from tpcore.lab.models import LabResult
    from tpcore.lab.target import LabPrimaryMetric

    lr = LabResult.model_validate_json(json.dumps(_PRE_SP_D_SIDECAR))
    assert lr.primary_metric == LabPrimaryMetric.SHARPE


def test_pre_sp_d_sidecar_still_accepted_by_evidence_loader(tmp_path):
    """load_labresult_sidecar over the pre-SP-D shape still ACCEPTs (no
    EvidenceError) — the live SP3 MODIFY-ECR gate is not regressed."""
    from ops.engine_sdlc._evidence import load_labresult_sidecar

    md = tmp_path / "2026-05-18-exp1-SURVIVED-seed7.md"
    md.write_text("# stub dossier\n")
    md.with_suffix(".json").write_text(json.dumps(_PRE_SP_D_SIDECAR))
    lr = load_labresult_sidecar(md)
    assert lr.verdict == "SURVIVED"
    assert lr.dsr == 0.97
    assert lr.credibility_score == 72


def test_score_sharpe_metric_equals_pre_refactor_closed_form():
    import math

    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    m = sp.SliceMetrics(n_trades=10, sharpe=1.4, profit_factor=1.5,
                         max_drawdown=-0.1, win_rate=0.5)
    expected = 1.4 + 0.05 * math.log10(max(10, 1))
    assert sp._score_for_ranking(m, LabPrimaryMetric.SHARPE) == expected
    assert sp._score_for_ranking(m) == expected  # defaulted == SHARPE


def test_score_maxdd_reduction_is_the_drawdown_value_itself():
    # §8-A15: MAXDD_REDUCTION == m.max_drawdown itself (≤0 by the
    # run.py:370 .min() construction). NOT -max_drawdown (sign-inverted).
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    deep = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                            max_drawdown=-0.30, win_rate=0.5)
    shallow = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                              max_drawdown=-0.05, win_rate=0.5)
    assert sp._score_for_ranking(
        deep, LabPrimaryMetric.MAXDD_REDUCTION) == pytest.approx(-0.30)
    assert sp._score_for_ranking(
        shallow, LabPrimaryMetric.MAXDD_REDUCTION) == pytest.approx(-0.05)
    # Shallower (less-negative) drawdown ranks HIGHER under the
    # descending reverse=True sort: -0.05 > -0.30.
    assert sp._score_for_ranking(
        shallow, LabPrimaryMetric.MAXDD_REDUCTION) > sp._score_for_ranking(
        deep, LabPrimaryMetric.MAXDD_REDUCTION)


def test_score_n_trades_floor_is_metric_independent():
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    thin = sp.SliceMetrics(n_trades=2, sharpe=9.9, profit_factor=9.0,
                            max_drawdown=-0.01, win_rate=1.0)
    for mt in (LabPrimaryMetric.SHARPE, LabPrimaryMetric.MAXDD_REDUCTION):
        assert sp._score_for_ranking(thin, mt) == -1.0


def test_non_finite_metric_value_clamps_to_floor_not_nan():
    import math

    import numpy as np

    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    m = sp.SliceMetrics(n_trades=10, sharpe=float("nan"),
                        profit_factor=1.0, max_drawdown=-0.1,
                        win_rate=0.5)
    v = sp._score_for_ranking(m, LabPrimaryMetric.SHARPE)
    assert v == -1.0
    assert math.isfinite(v)
    # never poisons np.mean / the sort
    assert math.isfinite(float(np.mean([v, 1.0, 2.0])))


def test_reserved_metric_score_raises_clear_value_error():
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    m = sp.SliceMetrics(n_trades=10, sharpe=1.0, profit_factor=1.0,
                        max_drawdown=-0.1, win_rate=0.5)
    with pytest.raises(ValueError, match="reserved objective"):
        sp._score_for_ranking(m, LabPrimaryMetric.ULCER)
    with pytest.raises(ValueError, match="reserved objective"):
        sp._score_for_ranking(m, LabPrimaryMetric.INVERSE_ETF_HOLD)


def test_ranking_metrics_table_is_exhaustive_over_the_enum():
    # Hardening: a future LabPrimaryMetric member added without a
    # _RANKING_METRICS entry must red LOUDLY and PRECISELY here, not as a
    # cryptic bare KeyError deep inside _score_for_ranking on a
    # live-money-adjacent ranking path. Also rejects a stray table key
    # with no enum member (set equality both directions).
    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    assert set(sp._RANKING_METRICS) == set(LabPrimaryMetric)


def test_score_maxdd_reduction_zero_drawdown_is_finite_max_and_ranks_first():
    # §8-A15 boundary: a flawless equity curve (max_drawdown == 0.0)
    # scores exactly 0.0 — the MAXIMUM possible MAXDD_REDUCTION value
    # (every real drawdown is <0 by the run.py:370 .min() construction)
    # — and is finite (the _clamp identity holds at the boundary, no
    # nan/inf). A 0.0-DD candidate must therefore rank ABOVE any
    # negative-DD one under the descending reverse=True sort.
    import math

    import ops.lab.run as sp
    from tpcore.lab.target import LabPrimaryMetric

    flawless = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                               max_drawdown=0.0, win_rate=0.5)
    drawn = sp.SliceMetrics(n_trades=10, sharpe=0.1, profit_factor=1.0,
                            max_drawdown=-0.05, win_rate=0.5)
    flawless_score = sp._score_for_ranking(
        flawless, LabPrimaryMetric.MAXDD_REDUCTION)
    assert flawless_score == 0.0
    assert math.isfinite(flawless_score)
    assert flawless_score > sp._score_for_ranking(
        drawn, LabPrimaryMetric.MAXDD_REDUCTION)


async def test_reserved_metric_rejects_before_any_ledger_spend(
        monkeypatch, tmp_path):
    """SP-D §4.3 / §8-A4 — a stub engine declaring ULCER raises the clear
    ValueError BEFORE record_trial_spend (the SP-B 'spend then crash'
    footgun class). Asserts NO lab_trial_ledger row is ever written —
    mirrors test_lab_targeting_consistency.py::
    test_undeclared_target_hard_rejects_before_any_ledger_spend verbatim
    (same _SharedPool/_FakeConn ledger-spy)."""
    import argparse
    from datetime import date

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    # Reuse the make-or-break ledger-spy doubles.
    from tpcore.tests.test_lab_sp_d_make_or_break import _SharedPool

    async def _runner(*a, **k):
        raise AssertionError("must not reach runner — reject is pre-spend")

    async def _loader(*a, **k):
        return object()

    def _ctx_runner(c, *, overrides=None):
        raise AssertionError("must not reach ctx_runner")

    tgt = LabTarget(
        param_ranges={"choice": (0, 1, "choice:A,B")},
        run_for_search=_runner, load_window_context=_loader,
        run_with_context=_ctx_runner, default_params=lambda: {"choice": "A"},
        primary_metric=LabPrimaryMetric.ULCER,
    )
    monkeypatch.setattr("ops.lab.run._lab_target_for", lambda e: tgt)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _loader)
    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)

    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    ns = argparse.Namespace(
        engine="reversion", trials=10, per_window_trials=2,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=0, output=tmp_path / "x.csv",
        database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None)

    async with LabContext(db_url="postgres://fake/db"):
        with pytest.raises(ValueError, match="reserved objective"):
            await lab_run._run_lab_core(ns, candidate="bad_reserved")

    # The reserved-metric reject is PRE-SPEND: no ledger row exists.
    assert not any(
        str(r["source"]).startswith("lab_trial_ledger")
        for r in shared.rows), (
        "a reserved-metric reject must NOT spend SP-A ledger budget "
        "(spec §4.3 / §8-A4)")
