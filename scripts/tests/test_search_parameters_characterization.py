"""Characterization oracle for ``scripts/search_parameters.py`` (SDLC SP2 T1, H-S2-4).

``scripts/search_parameters.py`` shipped with ZERO tests. Before the SP2
``LabRun`` extract (T5) can move the orchestration into ``ops/lab/run.py``,
the *current* behaviour must be pinned so the extract is provably
behaviour-preserving. These tests capture what the un-refactored script
DOES (not what it should do):

* ``build_walk_windows`` window slate (non-overlapping train, step advance)
* ``sample_parameters`` seed-determinism + per-engine key coverage
* ``compute_dsr_for_verdict`` bounds + monotone + trial-count deflation
* ``_norm_inv`` known quantiles
* ``period_returns_from_trades`` / ``compute_slice_metrics_from_trades``
* ``rank_candidates`` grouping + descending mean-score sort
* ``write_results_csv`` round-trip
* an ``amain`` SURVIVED smoke with a stubbed context-runner that records
  every per-candidate overrides dict (O2 — successive candidates must not
  leak overrides) and pins the CURRENT
  ``write_credibility_score(engine_name="reversion")`` argument (pre-H-S2-3;
  T6 flips this to ``lab.<candidate>`` and updates THIS assertion in the
  same commit, making the behaviour delta explicit and oracle-pinned).

T5 MIGRATION (binding): when T5 extracts ``amain`` into ``ops.lab.run`` and
turns ``scripts/search_parameters.py`` into a re-export shim, the
``test_amain_smoke_survived_verdict`` monkeypatch targets (``sp._runner_for``,
``sp._context_runner_for``, ``sp._context_loader_for``) MUST be retargeted to
the module that DEFINES amain (``ops.lab.run``) — patch where the name is
used, not where it is re-exported — or this smoke silently no-ops. The
pure-unit tests are shim-safe (re-exported attrs resolve); only the
amain-smoke monkeypatch targets need the T5 retarget. T5's plan step +
commit must do this in the same commit; the oracle must pass identically
pre- and post-extract.
"""
from __future__ import annotations

import json as _json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import search_parameters as sp  # noqa: E402


# ── Trade stub ──────────────────────────────────────────────────────────────
# The real engine trade objects expose ``entry_date`` / ``pnl_pct`` as
# ATTRIBUTES (search_parameters does ``t.entry_date`` / ``float(t.pnl_pct)``,
# never dict subscription). The plan's draft used dicts; aligned to the real
# attribute-access contract.
@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


def test_build_walk_windows_non_overlapping_train():
    w = sp.build_walk_windows(train_start=date(2018, 1, 1),
                              holdout_end=date(2023, 12, 31),
                              step_days=365, train_years=3, holdout_years=1)
    assert len(w) >= 1
    for win in w:
        assert win.train_start < win.train_end <= win.holdout_start < win.holdout_end
    # advancing by step_days
    if len(w) > 1:
        assert (w[1].train_start - w[0].train_start).days == 365


def test_sample_parameters_is_seed_deterministic():
    a = sp.sample_parameters("reversion", 20, seed=7)
    b = sp.sample_parameters("reversion", 20, seed=7)
    c = sp.sample_parameters("reversion", 20, seed=8)
    assert a == b
    assert a != c  # 20-combo list differs across seeds (collision prob ~0 over the continuous 4-tuple domain)
    for combo in a:
        assert set(combo) == set(sp.PARAM_RANGES["reversion"])


def test_compute_dsr_for_verdict_bounds_and_monotone():
    import numpy as np
    rng = np.random.default_rng(0)
    flat = [0.0] * 60
    strong = list(rng.normal(0.02, 0.01, 60))
    d_flat = sp.compute_dsr_for_verdict(flat, n_trials=200)
    d_strong = sp.compute_dsr_for_verdict(strong, n_trials=200)
    assert 0.0 <= d_flat <= 1.0 and 0.0 <= d_strong <= 1.0
    assert d_strong >= d_flat
    # more trials ⇒ deflated (lower) DSR for the same returns
    assert sp.compute_dsr_for_verdict(strong, n_trials=2000) <= d_strong + 1e-9


def test_norm_inv_known_quantiles():
    assert abs(sp._norm_inv(0.5)) < 1e-6
    # Acklam approx is deterministic pure math — this is a mathematical property (Φ⁻¹(0.975)),
    # NOT an implementation snapshot; a refactor to scipy.stats.norm.ppf keeps it within 1e-3.
    assert abs(sp._norm_inv(0.975) - 1.959963985) < 1e-3


def test_period_returns_and_slice_metrics_from_trades():
    trades = [
        _Trade(entry_date=date(2024, 1, 2), pnl_pct=0.03),
        _Trade(entry_date=date(2024, 1, 2), pnl_pct=-0.01),
        _Trade(entry_date=date(2024, 2, 1), pnl_pct=0.02),
    ]
    rets = sp.period_returns_from_trades(trades)
    assert len(rets) == 2  # grouped by entry_date period
    m = sp.compute_slice_metrics_from_trades(trades, span_days=60)
    assert m.n_trades == 3
    assert isinstance(m.sharpe, float) and isinstance(m.profit_factor, float)


def test_rank_candidates_groups_and_sorts():
    from search_parameters import SliceMetrics, TrialResult

    def tr(tid, params, sharpe):
        return TrialResult(trial_id=tid, window_label="w", parameters=params,
                           holdout=SliceMetrics(n_trades=10, sharpe=sharpe,
                                                profit_factor=1.5,
                                                max_drawdown=-0.1,
                                                win_rate=0.5),
                           full_credibility_score=70, error=None)
    p1 = {"z_threshold": 3.0}
    p2 = {"z_threshold": 2.5}
    ranked = sp.rank_candidates([tr(0, p1, 0.5), tr(1, p1, 1.5), tr(2, p2, 0.2)])
    assert ranked[0][0] == p1  # higher mean score first
    assert ranked[0][2] == 2   # n_windows for p1


def test_write_results_csv_roundtrip(tmp_path):
    from search_parameters import SliceMetrics, TrialResult
    t = TrialResult(trial_id=0, window_label="w1", parameters={"z_threshold": 3.0},
                    holdout=SliceMetrics(n_trades=5, sharpe=1.0, profit_factor=2.0,
                                         max_drawdown=-0.05, win_rate=0.6),
                    full_credibility_score=72, error=None)
    out = tmp_path / "r.csv"
    sp.write_results_csv(out, [t])
    text = out.read_text()
    assert "trial_id" in text and "parameters_json" in text and "0.6" in text


async def test_amain_smoke_survived_verdict(monkeypatch, tmp_path):
    """``amain`` end-to-end with a stubbed context-runner: asserts the
    SURVIVED verdict path + the ``write_credibility_score`` call args
    (O2: successive candidates don't leak overrides — the stub records
    each overrides dict it receives).

    Reality-alignment vs the plan's draft (this is characterization —
    capture what IS):

    * The stub return object exposes ``trade_log`` (not ``trades``) +
      ``credibility_score`` + ``credibility_rubric`` — the exact
      attributes ``_evaluate_candidate_with_context`` and the
      final-held-back block read.
    * Trade objects are attribute-bearing ``_Trade`` (not dicts) — the
      real ``t.entry_date`` / ``t.pnl_pct`` access.
    * ``amain`` has NO ``_resolve_universe`` seam (the plan's draft
      monkeypatched a non-existent symbol). Universe stays ``None``
      because ``_NS.universe_tier_max is None``, so the
      ``_load_universe_by_tier`` DB path is never entered — no universe
      monkeypatch needed.
    * ``amain`` builds the credibility-persist pool via a *local*
      ``import asyncpg; asyncpg.create_pool(...)`` with the dummy DSN, so
      ``asyncpg.create_pool`` is monkeypatched to a fake pool to keep the
      smoke offline.
    * ``write_credibility_score`` real call site is
      ``write_credibility_score(persist_pool, engine_name=args.engine,
      score=final_result.credibility_rubric)`` — no ``timestamp`` kwarg;
      the fake signature matches.
    """
    seen_overrides: list[dict] = []

    class _FakeRubric:
        # `final_result.credibility_rubric is not None` gates the persist
        # block; any non-None object satisfies it.
        score = 80

    class _FakeRunResult:
        credibility_score = 80
        credibility_rubric = _FakeRubric()
        # real code reads `.trade_log` (NOT `.trades`)
        trade_log = [_Trade(entry_date=date(2024, 6, 3), pnl_pct=0.02)
                     for _ in range(8)]

    def _fake_ctx_runner(context, *, overrides=None):
        seen_overrides.append(dict(overrides or {}))
        return _FakeRunResult()

    async def _fake_ctx_loader(*a, **k):
        return object()

    async def _fake_runner(*a, **k):
        return _FakeRunResult()

    # T5 retarget (plan-authorized — see this module's docstring "T5
    # MIGRATION (binding)"): amain now lives in ops.lab.run; sp re-exports
    # it. Patch where the names are USED (the defining module), not where
    # they are re-exported, or this smoke silently no-ops.
    monkeypatch.setattr("ops.lab.run._context_runner_for", lambda e: _fake_ctx_runner)
    monkeypatch.setattr("ops.lab.run._context_loader_for", lambda e: _fake_ctx_loader)
    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _fake_runner)

    persisted: dict = {}

    async def _fake_write(pool, *, engine_name, score):
        persisted["engine_name"] = engine_name
        persisted["score"] = score
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fake_write, raising=True)

    class _FakePool:
        async def close(self):
            return None

    async def _fake_create_pool(*a, **k):
        return _FakePool()

    # `amain` does a local `import asyncpg` then `asyncpg.create_pool(...)`
    # for the credibility-persist pool — patch the module attribute so the
    # smoke never opens a socket against the dummy DSN.
    import asyncpg
    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool,
                        raising=True)

    class _NS:
        engine = "reversion"
        trials = 4
        per_window_trials = 2
        train_start = date(2022, 1, 1)
        holdout_end = date(2023, 12, 31)
        final_holdout_start = date(2024, 1, 1)
        final_holdout_end = date(2024, 12, 31)
        walk_forward_step = 365
        train_years = 1
        holdout_years = 1
        seed = 0
        output = tmp_path / "o.csv"  # real code calls path.parent.mkdir → Path
        database_url = "postgres://x/y"
        dsr_threshold = 0.0
        credibility_threshold = 0
        universe_tier_max = None

    rc = await sp.amain(_NS())
    assert rc == 0  # SURVIVED (thresholds set permissive)
    assert persisted["engine_name"] == "reversion"  # CURRENT behavior (pre-H-S2-3)
    assert len(seen_overrides) >= 2
    assert all(isinstance(o, dict) for o in seen_overrides)
    assert len({_json.dumps(o, sort_keys=True) for o in seen_overrides}) == len(seen_overrides), \
        "successive candidates leaked/shared an overrides dict (O2)"
