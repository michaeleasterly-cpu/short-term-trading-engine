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
import math
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


# Spec §5.2 step-0 / §8-A15 construction. The corrected MAXDD_REDUCTION
# mapping is `m.max_drawdown` itself (≤0 by run.py:370 construction);
# under rank_candidates' descending reverse=True sort a SHALLOWER
# (less-negative) drawdown ranks FIRST. The lever set is offline-proven
# satisfiable under that corrected mapping:
#
#   WINDOWED (final_holdout=False, span_days=365, n=8 for all three):
#     A: sharpe_score=4.1666  max_drawdown=-0.045   <- SHARPE winner
#     B: sharpe_score=2.9704  max_drawdown= 0.000   <- MAXDD winner
#     C: sharpe_score=2.2459  max_drawdown=-0.015   <- wins NEITHER
#   ⇒ SHARPE order A>B>C (A strictly max); MAXDD order B>C>A (strict).
#   FINAL HOLDOUT: A,B n_trades=8 (survive); C n_trades=2 (<3 ⇒ the
#   metric-blind sacred-gate FAIL lever, §5.2). C's WINDOWED replay still
#   has n=8 + a finite score so it is a real `ranked` member, not pre-
#   killed by the n_trades<3 -> -1.0 ranking floor.
#
# The earlier set (A:0.030+-0.18, B:0.012+-0.01) was UNSATISFIABLE under
# the corrected mapping (a deep loss tanks Sharpe so B outscored A on
# Sharpe; C's zero-DD windowed slice won corrected MAXDD) — §8-A15.
#
#   kind="loss":   constant `ret`, one `loss` at the mid index.
#   kind="volpos": alternating hi/lo, strictly positive ⇒ zero drawdown,
#                  high variance ⇒ a modest (not blown-up) Sharpe.
_PROFILES = {
    "A": {"kind": "loss", "ret": 0.080, "loss": -0.045, "dd_trades": 8},
    "B": {"kind": "volpos", "hi": 0.040, "lo": 0.002, "dd_trades": 8},
    "C": {"kind": "loss", "ret": 0.020, "loss": -0.015, "dd_trades": 2},
}


def _trade_log(choice: str, *, final_holdout: bool) -> list[_Trade]:
    p = _PROFILES[choice]
    n = p["dd_trades"] if final_holdout else 8
    base = date(2022, 1, 3) if final_holdout else date(2019, 1, 3)
    log = []
    for i in range(n):
        if p["kind"] == "volpos":
            # Strictly positive ⇒ equity never retraces ⇒ max_drawdown==0
            # (shallowest); the hi/lo spread keeps Sharpe modest, NOT the
            # near-constant blow-up that broke the earlier construction.
            r = p["hi"] if i % 2 == 0 else p["lo"]
        else:
            r = p["ret"]
            # One moderate loss at the mid index: deep-ish drawdown that
            # still leaves a high-mean series a strong Sharpe.
            if i == n // 2:
                r = p["loss"]
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
    # §8-A15: pin the *strict* disagreement, not just the winners. A
    # future lever drift that re-introduces a tie (A==C on Sharpe) or
    # collapses the MAXDD order must ERROR loudly here, never silently
    # pass on Timsort insertion-order luck.
    sharpe_score = {tuple(sorted(p.items())): s for p, s, _ in sharpe_rank}
    maxdd_score = {tuple(sorted(p.items())): s for p, s, _ in maxdd_rank}
    a, b, c = (("choice", "A"),), (("choice", "B"),), (("choice", "C"),)
    if not (sharpe_score[a] > sharpe_score[b]
            and sharpe_score[a] > sharpe_score[c]):
        pytest.fail("VACUOUS: A's SHARPE score is not STRICTLY maximal "
                    f"(A={sharpe_score[a]} B={sharpe_score[b]} "
                    f"C={sharpe_score[c]}) — a tie makes the winner "
                    "Timsort-order-dependent; proof would be flaky")
    # Corrected MAXDD_REDUCTION score == m.max_drawdown itself (≤0);
    # shallower = larger. Strict order must be B > C > A.
    if not (maxdd_score[b] > maxdd_score[c] > maxdd_score[a]):
        pytest.fail("VACUOUS: corrected-MAXDD score order is not STRICTLY "
                    f"B>C>A (A={maxdd_score[a]} B={maxdd_score[b]} "
                    f"C={maxdd_score[c]}) — the drawdown contrast that "
                    "makes the winners invert is gone")
    # C's final-holdout replay must fail the gate via n_trades<3.
    held = lab_run.compute_slice_metrics_from_trades(
        _trade_log("C", final_holdout=True), span_days=365)
    if held.n_trades >= 3:
        pytest.fail("VACUOUS: C's final-holdout replay has n_trades>=3 — "
                    "the metric-blind fail lever is gone")
    # C's WINDOWED replay must still be a real ranked member (n>=3 ⇒ NOT
    # pre-killed by the n_trades<3 -> -1.0 floor), else the windowed
    # MAXDD ranking would never even consider C.
    c_windowed = lab_run.compute_slice_metrics_from_trades(
        _trade_log("C", final_holdout=False), span_days=365)
    if c_windowed.n_trades < 3:
        pytest.fail("VACUOUS: C's WINDOWED replay has n_trades<3 — C is "
                    "pre-killed by the ranking floor, not a real member")


def _ecr_tuple(lr) -> tuple[str, float, int, dict]:
    """The EXACT 4-tuple ops/engine_sdlc/planner._validate_modify
    re-derives from a LabResult sidecar (§0.2a/A12): verdict, dsr,
    credibility_score, winning_params. The make-or-break invariant is
    that THIS tuple is byte-identical between the SHARPE run and the
    MAXDD run for a FIXED candidate — the gate must not move when only
    the ranking metric changes."""
    return (lr.verdict, lr.dsr, lr.credibility_score, lr.winning_params)


async def test_make_or_break_gate_invariant_over_ecr_tuple(
        monkeypatch, tmp_path):
    """Steps 2-4 + §0.2a/A12: run the WHOLE pipeline twice (SHARPE vs
    MAXDD_REDUCTION). For a FIXED candidate the ECR-re-derived 4-tuple
    (verdict, dsr, credibility_score, winning_params) is BYTE-IDENTICAL
    between the two metric runs — the gate verdict does NOT move when
    only the ranking metric changes (that IS the make-or-break). Only
    WHICH candidate sits at ranked[0] (the headline) differs."""
    core_s, lr_s = await _run_once(
        monkeypatch, tmp_path, metric_name="sharpe", seed=1)
    core_m, lr_m = await _run_once(
        monkeypatch, tmp_path, metric_name="maxdd_reduction", seed=1)

    # Step 4 (pluggability): the metric genuinely re-orders — the two
    # runs' headline winners DIFFER (else the proof is vacuous: nothing
    # was permuted, so metric-invariance would be trivially true).
    assert lr_s.winning_params != lr_m.winning_params
    assert lr_s.winning_params == {"choice": "A"}
    assert lr_m.winning_params == {"choice": "B"}

    # §0.2a/A12 CORE INVARIANT — the make-or-break itself. For each fixed
    # candidate independently driven as the winner through the FULL
    # _build_lab_result gate path, the ECR-re-derived 4-tuple is
    # byte-identical between the SHARPE-run context and the MAXDD-run
    # context. We pin the winner deterministically by monkeypatching
    # rank_candidates so ranked[0] is the chosen candidate, then run the
    # pipeline once per (candidate, metric) and compare the 4-tuples.
    from tpcore.lab.target import LabPrimaryMetric

    async def _run_pinned(*, metric_name: str, pinned: str):
        tgt = _install_choice_stub(monkeypatch)
        monkeypatch.setattr(
            "ops.lab.run._lab_target_for",
            lambda e: tgt.model_copy(
                update={"primary_metric": LabPrimaryMetric(metric_name)}))
        real_rank = lab_run.rank_candidates

        def _pinned_rank(trials, metric=LabPrimaryMetric.SHARPE):
            ranked = real_rank(trials, metric)
            head = [r for r in ranked if r[0] == {"choice": pinned}]
            rest = [r for r in ranked if r[0] != {"choice": pinned}]
            return head + rest

        monkeypatch.setattr("ops.lab.run.rank_candidates", _pinned_rank)
        shared = _SharedPool()

        async def _fb(url, *, read_only, **k):
            return shared

        monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb,
                            raising=True)
        async with LabContext(db_url="postgres://fake/db"):
            core = await lab_run._run_lab_core(
                _ns(tmp_path / f"{metric_name}_{pinned}.csv", seed=1),
                candidate=f"exp_{metric_name}_{pinned}")
        assert not isinstance(core, int)
        lr = lab_run._build_lab_result(
            candidate=_candidate(f"exp-{metric_name}-{pinned}"), core=core,
            args=_ns(tmp_path / f"{metric_name}_{pinned}2.csv", seed=1))
        return lr

    for pinned in ("A", "B", "C"):
        lr_sharpe = await _run_pinned(metric_name="sharpe", pinned=pinned)
        lr_maxdd = await _run_pinned(
            metric_name="maxdd_reduction", pinned=pinned)
        t_s = _ecr_tuple(lr_sharpe)
        t_m = _ecr_tuple(lr_maxdd)
        # verdict / credibility_score / winning_params: EXACT equality.
        assert t_s[0] == t_m[0], (
            f"{pinned}: verdict moved with the ranking metric "
            f"({t_s[0]!r} vs {t_m[0]!r}) — gate is NOT metric-invariant")
        assert t_s[2] == t_m[2], (
            f"{pinned}: credibility_score moved ({t_s[2]} vs {t_m[2]})")
        assert t_s[3] == t_m[3], (
            f"{pinned}: winning_params moved ({t_s[3]} vs {t_m[3]})")
        # dsr: NaN must FAIL (not pass). Exact equality is the contract —
        # same final-holdout replay, same compute_dsr_for_verdict, same
        # n_trials; the tolerance is 0.0 (bit-identical) because nothing
        # metric-dependent feeds the DSR computation. A tolerance is
        # justified ONLY if a future float-path change makes it ≤1 ULP;
        # today it is provably exact, so we assert exact and additionally
        # reject NaN explicitly (== would silently pass NaN!=NaN as
        # "not equal" → assertion failure, which is the desired FAIL, but
        # we make the NaN rejection explicit and loud).
        assert not math.isnan(t_s[1]) and not math.isnan(t_m[1]), (
            f"{pinned}: dsr is NaN ({t_s[1]} / {t_m[1]}) — a degenerate "
            "gate number must FAIL the invariant, never pass")
        assert t_s[1] == t_m[1], (
            f"{pinned}: dsr moved with the ranking metric "
            f"({t_s[1]!r} vs {t_m[1]!r}) — gate is NOT metric-invariant")

    # The ONLY thing the metric changed is which candidate is the
    # headline — the gate 4-tuple for any fixed candidate is invariant.
    assert _ecr_tuple(lr_s) != _ecr_tuple(lr_m)  # headlines differ
    assert lr_s.winning_params != lr_m.winning_params


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
