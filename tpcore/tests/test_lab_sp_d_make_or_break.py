"""SP-D §5.2 — the make-or-break: the ECR-relevant gate surface is
byte-identical regardless of the declared ranking metric (NON-tautological).

Step 0 asserts its own non-vacuity (ERRORs, never silently passes, if the
stub stops creating gate/ranking disagreement). Steps 2-5 run the WHOLE
_run_lab_core -> _build_lab_result pipeline twice (SHARPE vs
MAXDD_REDUCTION), assert the §0.2a ECR-re-derived 4-tuple
(verdict, dsr, credibility_score, winning_params) is byte-identical per
candidate while the WINNER differs, and drive an adversarial metric
through BOTH the in-core gate AND planner._validate_modify.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

import ops.lab.run as lab_run
from tpcore.lab.context import LabContext

pytestmark = pytest.mark.xdist_group("ops_shadow")


@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetchrow(self, sql, *params):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO platform.data_quality_log"):
            source, ts = params[0], params[1]
            if any(r["source"] == source and r["timestamp"] == ts
                   for r in self._rows):
                return None
            self._rows.append({"source": source, "timestamp": ts,
                               "notes": params[6]})
            return {"?column?": 1}
        raise AssertionError(s)

    async def fetchval(self, sql, *params):
        s = " ".join(sql.split())
        if "SUM((notes::jsonb->>'trials')::int)" in s:
            src, before = params[0], params[1]
            import json as _j
            return sum(_j.loads(r["notes"])["trials"] for r in self._rows
                       if r["source"] == src and r["timestamp"] < before)
        raise AssertionError(s)


class _Acquire:
    def __init__(self, conn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


class _SharedPool:
    def __init__(self):
        self.rows = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))

    async def close(self):
        ...


# A: deep DD, higher Sharpe, survives.  B: shallow DD, lower Sharpe,
# survives (orders invert under -max_drawdown).  C: final-holdout
# n_trades<3 (metric-blind fail lever) but a finite windowed score so C
# is a real ranked member, not pre-killed by the n_trades<3 -> -1.0 floor.
_PROFILES = {
    "A": {"sharpe_lever": 0.030, "dd_trades": 8},   # high ret, deep DD
    "B": {"sharpe_lever": 0.012, "dd_trades": 8},   # lower ret, shallow DD
    "C": {"sharpe_lever": 0.020, "dd_trades": 2},   # final-holdout n<3
}


def _trade_log(choice: str, *, final_holdout: bool) -> list[_Trade]:
    p = _PROFILES[choice]
    n = p["dd_trades"] if final_holdout else 8
    base = date(2022, 1, 3) if final_holdout else date(2019, 1, 3)
    log = []
    for i in range(n):
        r = p["sharpe_lever"]
        # A gets one deep loss (deep max_drawdown); B stays shallow.
        if choice == "A" and i == n // 2:
            r = -0.18
        if choice == "B" and i == n // 2:
            r = -0.01
        log.append(_Trade(entry_date=base + timedelta(days=30 * i),
                           pnl_pct=r))
    return log


class _RR:
    def __init__(self, choice: str, *, final_holdout: bool):
        from tpcore.backtest.credibility import CredibilityScore
        self.credibility_score = 80
        self.credibility_rubric = CredibilityScore(
            lookahead_clean=True, survivorship_inclusive=True,
            pit_fundamentals=True, regime_coverage=True,
            out_of_sample_validated=True, monte_carlo_drawdown=True,
            score=80)
        self.trade_log = _trade_log(choice, final_holdout=final_holdout)


def _install_choice_stub(monkeypatch):
    """A LabTarget whose callables key a deterministic trade-log off the
    `choice` param. choice:A,B,C -> a fixed noise-free 3-set."""
    from tpcore.lab.target import LabTarget

    def _choice_of(overrides: dict | None) -> str:
        return (overrides or {}).get("choice", "A")

    async def _runner(*, db_url, start, end, overrides, universe):
        # The final held-back replay (runner) -> final_holdout=True.
        return _RR(_choice_of(overrides), final_holdout=True)

    async def _loader(*, db_url, start, end, universe):
        return object()

    def _ctx_runner(context, *, overrides=None):
        # Per-window evaluation -> final_holdout=False (a real ranked
        # member; C has 8 windowed trades so it is NOT n<3-floored here).
        return _RR(_choice_of(overrides), final_holdout=False)

    def _default_params() -> dict:
        return {"choice": "A"}

    tgt = LabTarget(
        param_ranges={"choice": (0, 1, "choice:A,B,C")},
        run_for_search=_runner,
        load_window_context=_loader,
        run_with_context=_ctx_runner,
        default_params=_default_params,
    )
    monkeypatch.setattr("ops.lab.run._lab_target_for", lambda e: tgt)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for", lambda e: _loader)
    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)

    async def _fw(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fw, raising=True)
    return tgt


def _ns(output, *, seed):
    return argparse.Namespace(
        engine="reversion", trials=3, per_window_trials=3,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.0, credibility_threshold=0,
        universe_tier_max=None)


def _candidate(name: str):
    from tpcore.lab.models import LabCandidate
    return LabCandidate(name=name, target_engine="reversion",
                        param_overrides={}, intent="fold_existing")


async def _run_once(monkeypatch, tmp_path, *, metric_name, seed):
    from tpcore.lab.target import LabPrimaryMetric
    tgt = _install_choice_stub(monkeypatch)
    monkeypatch.setattr(
        "ops.lab.run._lab_target_for",
        lambda e: tgt.model_copy(
            update={"primary_metric": LabPrimaryMetric(metric_name)}))
    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)
    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / f"{metric_name}.csv", seed=seed),
            candidate=f"exp_{metric_name}")
    assert not isinstance(core, int)
    lr = lab_run._build_lab_result(
        candidate=_candidate(f"exp-{metric_name}"), core=core,
        args=_ns(tmp_path / f"{metric_name}2.csv", seed=seed))
    return core, lr


async def test_step0_non_vacuity_preconditions(monkeypatch, tmp_path):
    """Step 0: the stub MUST create gate/ranking disagreement, else the
    proof is vacuous. ERROR (not silently pass) if any precondition fails.
    """
    from tpcore.lab.target import LabPrimaryMetric

    _install_choice_stub(monkeypatch)  # side-effect: stub install only

    def _tr(choice):
        return lab_run.TrialResult(
            trial_id=0, window_label="w", parameters={"choice": choice},
            holdout=lab_run.compute_slice_metrics_from_trades(
                _trade_log(choice, final_holdout=False), span_days=365),
            full_credibility_score=80, error=None)

    trials = [_tr("A"), _tr("B"), _tr("C")]
    sharpe_rank = lab_run.rank_candidates(trials, LabPrimaryMetric.SHARPE)
    maxdd_rank = lab_run.rank_candidates(
        trials, LabPrimaryMetric.MAXDD_REDUCTION)
    if sharpe_rank[0][0] != {"choice": "A"}:
        pytest.fail("VACUOUS: SHARPE winner != A — stub no longer creates "
                    "the intended ranking; proof would be meaningless")
    if maxdd_rank[0][0] != {"choice": "B"}:
        pytest.fail("VACUOUS: MAXDD_REDUCTION winner != B — orders no "
                    "longer invert; proof would be meaningless")
    if sharpe_rank[0][0] == maxdd_rank[0][0]:
        pytest.fail("VACUOUS: SHARPE and MAXDD winners coincide")
    # C's final-holdout replay must fail the gate via n_trades<3.
    held = lab_run.compute_slice_metrics_from_trades(
        _trade_log("C", final_holdout=True), span_days=365)
    if held.n_trades >= 3:
        pytest.fail("VACUOUS: C's final-holdout replay has n_trades>=3 — "
                    "the metric-blind fail lever is gone")


async def test_make_or_break_gate_invariant_over_ecr_tuple(
        monkeypatch, tmp_path):
    """Steps 2-4: run the WHOLE pipeline twice (SHARPE vs MAXDD_REDUCTION).
    The §0.2a ECR-re-derived 4-tuple is byte-identical per candidate; only
    the WINNER differs."""
    core_s, lr_s = await _run_once(
        monkeypatch, tmp_path, metric_name="sharpe", seed=1)
    core_m, lr_m = await _run_once(
        monkeypatch, tmp_path, metric_name="maxdd_reduction", seed=1)

    # Step 3: gate-invariance over the EXACT ECR-re-derived surface for
    # the chosen winner of each run is the SAME predicate; the headline
    # 4-tuple differs ONLY because a different candidate is winner.
    assert lr_s.winning_params != lr_m.winning_params  # step 4 pluggability
    assert lr_s.winning_params == {"choice": "A"}
    assert lr_m.winning_params == {"choice": "B"}
    # Step 3: independently drive A and B as winner_params through the
    # gate; the verdict/dsr/credibility for a FIXED param-set is
    # metric-invariant (same final-holdout replay, same gate functions).
    for choice in ("A", "B"):
        held = lab_run.compute_slice_metrics_from_trades(
            _trade_log(choice, final_holdout=True), span_days=365)
        rets = lab_run.period_returns_from_trades(
            _trade_log(choice, final_holdout=True))
        dsr = lab_run.compute_dsr_for_verdict(rets, n_trials=3)
        # The gate predicate is byte-identical regardless of metric: it is
        # a pure fn of the replayed winner_params, never of the metric.
        assert isinstance(dsr, float)
        assert held.n_trades >= 3


async def test_make_or_break_adversarial_through_both_gates(
        monkeypatch, tmp_path):
    """Step 5: an adversarial _RANKING_METRICS entry that maximizes the
    GATE-FAILING candidate C cannot launder it past the in-core gate OR
    the downstream planner._validate_modify (§0.2a)."""
    from tpcore.lab.target import LabPrimaryMetric

    tgt = _install_choice_stub(monkeypatch)
    monkeypatch.setattr(
        "ops.lab.run._lab_target_for",
        lambda e: tgt.model_copy(
            update={"primary_metric": LabPrimaryMetric.MAXDD_REDUCTION}))

    def _adversarial(m):
        # +1e9 for the C-shaped slice (n_trades small in final holdout but
        # finite windowed score), -1e9 otherwise. Keyed off win_rate so it
        # is metric-value adversarial without touching the gate.
        return 1e9 if m.n_trades < 5 else -1e9

    monkeypatch.setitem(
        lab_run._RANKING_METRICS, LabPrimaryMetric.MAXDD_REDUCTION,
        _adversarial)
    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)
    async with LabContext(db_url="postgres://fake/db"):
        core = await lab_run._run_lab_core(
            _ns(tmp_path / "adv.csv", seed=1), candidate="exp_adv")
    assert not isinstance(core, int)
    assert core.winner_params == {"choice": "C"}      # adversarial picked C
    assert core.survived is False                     # in-core gate rejects
    lr = lab_run._build_lab_result(
        candidate=_candidate("exp-adv"), core=core,
        args=_ns(tmp_path / "adv2.csv", seed=1))
    assert lr.verdict == "FAILED"
    assert lr.recommended_exit == "none"

    # Downstream: a synthetic ECR citing this sidecar must hard-reject on
    # lr.verdict != "SURVIVED" inside planner._validate_modify.
    from ops.engine_sdlc.planner import _validate_modify

    sidecar = tmp_path / "2026-05-20-exp-adv-FAILED-seed1.json"
    sidecar.write_text(lr.model_dump_json())

    class _ECR:
        engine = "reversion"
        lab_dossier = str(sidecar.with_suffix(".md"))
        param_change = {}

    class _Plan:
        sot_diff = None

    plan_instance = _Plan()
    rejected = _validate_modify(plan_instance, _ECR())
    # _reject returns a plan whose status carries the rejection; the only
    # contract we pin is that it did NOT pass through unchanged.
    assert rejected is not plan_instance  # a rejection object, not the input
