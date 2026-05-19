"""SP-E — the Sentinel-specific proof: its declared non-Sharpe primary
metric (``LabPrimaryMetric.MAXDD_REDUCTION``) ranks candidates CORRECTLY
(a shallower holdout drawdown wins) while the SACRED graduation gate
(DSR≥threshold ∧ cred≥threshold ∧ n_trades≥3) is BYTE-IDENTICAL
regardless of which ranking metric is used — the pluggable metric
changes only WHICH candidate wins, never WHETHER it graduates (SP-D
§1.2, applied to Sentinel's exact bar).

SP-D's `test_lab_sp_d_make_or_break.py` proves the GENERAL
metric-invariance of the gate (a reversion-targeted stub with a
synthetic ``model_copy`` metric). THIS test pins the SP-E deliverable
that SP-D does NOT cover: it drives the REAL
``_run_lab_core`` → ``_build_lab_result`` → ``survived`` pipeline twice
for a FIXED pinned candidate — once under SHARPE and once under the
metric **resolved through the real SP-B roster resolver for
``sentinel``** (`_lab_target_for("sentinel").primary_metric`, NOT a
hardcoded literal — the resolution being MAXDD_REDUCTION is itself
asserted for non-vacuity) — and asserts the real
``(verdict, dsr, credibility_score, winning_params)`` 4-tuple plus
``core.survived`` is byte-identical between the two. It would FAIL if
production's ``survived`` ever became metric-dependent: it invokes the
real gate, it does not re-implement it.

Fully hermetic (the SP-D CI hermeticity lesson): hand-built
TrialResults / stubbed LabTarget callables, no DB / network, no
module-level `import ops.lab.run`, no collection-time `sys.modules`
purge — every import is in-body and the pools are offline stubs.
"""
from __future__ import annotations

import argparse
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


# ────────────────────────────────────────────────────────────────────
# Hermetic offline pool/credibility stubs — the SP-D
# test_lab_sp_d_make_or_break.py _SharedPool/_FakeConn precedent
# (verbatim shape, NOT widened).
# ────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchrow(self, sql: str, *params: object):
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

    async def fetchval(self, sql: str, *params: object):
        s = " ".join(sql.split())
        if "SUM((notes::jsonb->>'trials')::int)" in s:
            src, before = params[0], params[1]
            import json as _j
            return sum(_j.loads(r["notes"])["trials"] for r in self._rows
                       if r["source"] == src and r["timestamp"] < before)
        raise AssertionError(s)


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._c = conn

    async def __aenter__(self) -> _FakeConn:
        return self._c

    async def __aexit__(self, *a: object) -> bool:
        return False


class _SharedPool:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def acquire(self) -> _Acquire:
        return _Acquire(_FakeConn(self.rows))

    async def close(self) -> None:
        ...


class _RR:
    """A deterministic BacktestRunResult-shaped stub: a noise-free
    trade-log keyed off the `choice` param, plus a valid credibility
    rubric (so _build_lab_result has a non-None rubric)."""

    def __init__(self, choice: str, *, final_holdout: bool) -> None:
        from tpcore.backtest.credibility import CredibilityScore

        self.credibility_score = 80
        self.credibility_rubric = CredibilityScore(
            lookahead_clean=True, survivorship_inclusive=True,
            pit_fundamentals=True, regime_coverage=True,
            out_of_sample_validated=True, monte_carlo_drawdown=True,
            score=80)
        returns = _LEGACY if choice == "legacy60" else _VARIANT
        base = date(2022, 1, 3) if final_holdout else date(2021, 1, 4)
        self.trade_log = [
            type("T", (), {"entry_date": base + timedelta(days=30 * i),
                           "pnl_pct": r})()
            for i, r in enumerate(returns)
        ]


def _install_sentinel_stub(monkeypatch):
    """A LabTarget whose callables key a deterministic trade-log off the
    `choice` param — Sentinel's {legacy60, variant55} arms — with NO
    real DB / Sentinel backtest. The credibility write is stubbed True
    so no real pool is touched."""
    from tpcore.lab.target import LabTarget

    def _choice_of(overrides: dict | None) -> str:
        return (overrides or {}).get("choice", "legacy60")

    async def _runner(*, db_url, start, end, overrides, universe):
        return _RR(_choice_of(overrides), final_holdout=True)

    async def _loader(*, db_url, start, end, universe):
        return object()

    def _ctx_runner(context, *, overrides=None):
        return _RR(_choice_of(overrides), final_holdout=False)

    def _default_params() -> dict:
        return {"choice": "legacy60"}

    tgt = LabTarget(
        param_ranges={"choice": (0, 1, "choice:legacy60,variant55")},
        run_for_search=_runner,
        load_window_context=_loader,
        run_with_context=_ctx_runner,
        default_params=_default_params,
    )
    monkeypatch.setattr("ops.lab.run._lab_target_for", lambda e: tgt)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _loader)
    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: _ctx_runner)
    # ops.engine_sdlc.default_params._lab_target_for is a LAZY in-body
    # `from ops.lab.run import _lab_target_for` (not a module attribute),
    # so patching ops.lab.run._lab_target_for above already covers the
    # _build_lab_result default-params seam — no second patch site.

    async def _fw(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fw, raising=True)
    return tgt


def _ns(output, *, seed: int) -> argparse.Namespace:
    return argparse.Namespace(
        engine="sentinel", trials=3, per_window_trials=3,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.0, credibility_threshold=0,
        universe_tier_max=None)


def _candidate(name: str):
    from tpcore.lab.models import LabCandidate
    return LabCandidate(name=name, target_engine="sentinel",
                        param_overrides={}, intent="fold_existing")


def _ecr_tuple(lr) -> tuple[str, float, int, dict]:
    """The EXACT 4-tuple ops/engine_sdlc/planner._validate_modify
    re-derives from a LabResult sidecar (SP-D §0.2a/A12):
    (verdict, dsr, credibility_score, winning_params). The make-or-break
    invariant is that THIS tuple is byte-identical between the SHARPE run
    and the Sentinel-roster-resolved-MAXDD run for a FIXED candidate."""
    return (lr.verdict, lr.dsr, lr.credibility_score, lr.winning_params)


async def test_gate_4tuple_is_byte_identical_through_the_real_gate():
    """The make-or-break, on Sentinel's bar, through the REAL gate.

    For a FIXED pinned candidate, run the WHOLE
    ``_run_lab_core`` → ``_build_lab_result`` → ``survived`` pipeline
    twice:
      * SHARPE — ``LabPrimaryMetric.SHARPE``;
      * MAXDD — the metric **resolved through the real SP-B roster
        resolver for ``sentinel``** (`_lab_target_for("sentinel")
        .primary_metric`, asserted == MAXDD_REDUCTION for non-vacuity;
        NOT a hardcoded literal — that would re-introduce a tautology).
    Assert ``core.survived`` AND the ECR 4-tuple
    ``(verdict, dsr, credibility_score, winning_params)`` is
    byte-identical between the two — the metric only permutes the WINNER,
    it never feeds the production ``survived`` predicate. This invokes
    the real gate; it does NOT re-implement it, so it FAILS loudly if
    production's ``survived`` ever became metric-dependent.
    """
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext
    from tpcore.lab.target import LabPrimaryMetric

    # SP-E NON-VACUITY: the MAXDD arm's metric is taken from the REAL
    # roster resolver, not hardcoded. Pin that it genuinely resolves to
    # Sentinel's declared non-Sharpe bar (else the two arms would be the
    # same metric and the invariant trivially true).
    sentinel_metric = lab_run._lab_target_for("sentinel").primary_metric
    assert sentinel_metric == LabPrimaryMetric.MAXDD_REDUCTION
    assert sentinel_metric != LabPrimaryMetric.SHARPE

    async def _run_pinned(*, metric: LabPrimaryMetric, pinned: str, tag: str,
                          tmp):
        tgt = _install_sentinel_stub(monkeypatch_holder[0])
        monkeypatch_holder[0].setattr(
            "ops.lab.run._lab_target_for",
            lambda e: tgt.model_copy(update={"primary_metric": metric}))
        real_rank = lab_run.rank_candidates

        def _pinned_rank(trials, metric=LabPrimaryMetric.SHARPE):
            ranked = real_rank(trials, metric)
            head = [r for r in ranked if r[0] == {"choice": pinned}]
            rest = [r for r in ranked if r[0] != {"choice": pinned}]
            return head + rest

        monkeypatch_holder[0].setattr(
            "ops.lab.run.rank_candidates", _pinned_rank)
        shared = _SharedPool()

        async def _fb(url, *, read_only, **k):
            return shared

        monkeypatch_holder[0].setattr(
            "tpcore.db.build_asyncpg_pool", _fb, raising=True)
        async with LabContext(db_url="postgres://fake/db"):
            core = await lab_run._run_lab_core(
                _ns(tmp / f"{tag}.csv", seed=7),
                candidate=f"exp_{tag}")
        assert not isinstance(core, int)
        lr = lab_run._build_lab_result(
            candidate=_candidate(f"exp-{tag}"), core=core,
            args=_ns(tmp / f"{tag}2.csv", seed=7))
        return core, lr

    import tempfile
    from pathlib import Path

    import pytest as _pytest

    monkeypatch_holder: list = [None]
    with _pytest.MonkeyPatch.context() as mp:
        monkeypatch_holder[0] = mp
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for pinned in ("legacy60", "variant55"):
                core_s, lr_s = await _run_pinned(
                    metric=LabPrimaryMetric.SHARPE, pinned=pinned,
                    tag=f"sharpe_{pinned}", tmp=tmp)
                core_m, lr_m = await _run_pinned(
                    metric=sentinel_metric, pinned=pinned,
                    tag=f"maxdd_{pinned}", tmp=tmp)

                # The REAL production `survived` predicate, byte-identical.
                assert core_s.survived == core_m.survived, (
                    f"{pinned}: production core.survived moved with the "
                    f"ranking metric ({core_s.survived} vs "
                    f"{core_m.survived}) — SP-D/SP-E gate-is-sacred "
                    "separation VIOLATED")
                # The ECR 4-tuple re-derived from the REAL LabResult.
                t_s, t_m = _ecr_tuple(lr_s), _ecr_tuple(lr_m)
                assert t_s == t_m, (
                    f"{pinned}: the ECR gate 4-tuple moved with the "
                    f"ranking metric ({t_s!r} vs {t_m!r}) — the sacred "
                    "gate is NOT metric-invariant")


def test_thin_holdout_floor_is_metric_independent():
    """The n_trades<3 statistical-power floor is OUTSIDE the metric
    dispatch (run.py:532) — every metric inherits it identically. The
    real ``_score_for_ranking`` returns the -1.0 floor for a 2-trade
    held replay under BOTH Sharpe and Sentinel's MAXDD_REDUCTION
    (metric-independent)."""
    import ops.lab.run as lab_run
    from tpcore.lab.target import LabPrimaryMetric

    thin = _trial("thin", [0.01, 0.01], final_holdout=True).holdout
    assert thin.n_trades < 3
    for metric in (LabPrimaryMetric.SHARPE,
                   LabPrimaryMetric.MAXDD_REDUCTION):
        # The ranking floor is -1.0 for BOTH metrics (metric-independent);
        # the production gate's own n_trades≥3 clause (a sub-3 ⇒ FAIL) is
        # exercised through the REAL `survived` path by
        # test_gate_4tuple_is_byte_identical_through_the_real_gate and
        # SP-D's make-or-break — not re-implemented here.
        assert lab_run._score_for_ranking(thin, metric) == -1.0
