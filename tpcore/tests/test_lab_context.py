import pytest
from pydantic import ValidationError

from tpcore.lab.context import LabContext, LabIsolationViolation, assert_not_in_lab, lab_is_active
from tpcore.lab.models import LabCandidate, LabResult, ParamDelta


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


def test_models_are_frozen_pydantic_v2():
    c = LabCandidate(name="exp1", target_engine="reversion",
                     param_overrides={"z_threshold": 3.2},
                     intent="fold_existing", notes="n")
    with pytest.raises(ValidationError):
        c.name = "x"  # frozen
    r = LabResult(candidate="exp1", target_engine="reversion",
                  intent="fold_existing", verdict="SURVIVED", dsr=0.97,
                  credibility_score=72, credibility_rubric={},
                  held_metrics={"sharpe": 1.1, "profit_factor": 1.6,
                                "max_drawdown": -0.08, "n_trades": 12,
                                "win_rate": 0.55},
                  winning_params={"z_threshold": 3.2},
                  param_diff=[ParamDelta(name="z_threshold", current=3.0,
                                         winning=3.2)],
                  recommended_exit="fold_existing", ranked_alternatives=[],
                  walk_windows=[], n_trials=200, seed=0,
                  generated_at="2026-05-18T00:00:00Z")
    assert r.verdict == "SURVIVED" and r.recommended_exit == "fold_existing"
