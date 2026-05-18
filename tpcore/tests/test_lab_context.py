from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from tpcore.backtest.credibility import CredibilityScore
from tpcore.lab.context import LabContext, LabIsolationViolation, assert_not_in_lab, lab_is_active
from tpcore.lab.models import LabCandidate, LabResult, ParamDelta, WalkWindowRecord


def test_lab_is_active_false_by_default():
    assert lab_is_active() is False
    assert_not_in_lab()  # no raise outside a Lab run


async def test_lab_context_sets_and_clears_active():
    assert lab_is_active() is False
    async with LabContext(db_url="postgres://x/y", build_pools=False):
        assert lab_is_active() is True
        with pytest.raises(LabIsolationViolation):
            assert_not_in_lab()
    assert lab_is_active() is False  # cleared on exit (even on exception)


async def test_lab_context_clears_active_on_exception():
    with pytest.raises(RuntimeError):
        async with LabContext(db_url="postgres://x/y", build_pools=False):
            assert lab_is_active() is True
            raise RuntimeError("boom")
    assert lab_is_active() is False


async def test_lab_context_clears_active_on_aenter_pool_failure(monkeypatch):
    """__aenter__ partial-failure: contextvar reset + read_pool closed
    when the second pool build raises (the leak FIX 2 closes)."""
    from tpcore.lab.context import LabContext, lab_is_active
    closed = {"v": False}
    class _FakePool:
        async def close(self): closed["v"] = True
    calls = {"n": 0}
    async def _flaky_build(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakePool()
        raise RuntimeError("second pool build failed")
    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _flaky_build)
    with __import__("pytest").raises(RuntimeError, match="second pool build failed"):
        async with LabContext(db_url="postgres://x/y", build_pools=True):
            pass
    assert lab_is_active() is False   # contextvar reset despite the failure
    assert closed["v"] is True        # the first (read) pool was closed


def test_models_are_frozen_pydantic_v2():
    c = LabCandidate(name="exp1", target_engine="reversion",
                     param_overrides={"z_threshold": 3.2},
                     intent="fold_existing", notes="n")
    with pytest.raises(ValidationError):
        c.name = "x"  # frozen
    rubric = CredibilityScore(
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True,
        score=72)
    r = LabResult(candidate="exp1", target_engine="reversion",
                  intent="fold_existing", verdict="SURVIVED", dsr=0.97,
                  credibility_score=72, credibility_rubric=rubric,
                  held_metrics={"sharpe": 1.1, "profit_factor": 1.6,
                                "max_drawdown": -0.08, "n_trades": 12,
                                "win_rate": 0.55},
                  winning_params={"z_threshold": 3.2},
                  param_diff=[ParamDelta(name="z_threshold", current=3.0,
                                         winning=3.2)],
                  recommended_exit="fold_existing", ranked_alternatives=[],
                  walk_windows=[WalkWindowRecord(
                      train_start=date(2018, 1, 1),
                      train_end=date(2022, 12, 31),
                      holdout_start=date(2023, 1, 1),
                      holdout_end=date(2024, 12, 31))],
                  n_trials=200, seed=0,
                  generated_at=datetime(2026, 5, 18, tzinfo=UTC))
    assert r.verdict == "SURVIVED" and r.recommended_exit == "fold_existing"
    assert r.credibility_rubric.score == 72
    assert r.walk_windows[0].train_start == date(2018, 1, 1)
