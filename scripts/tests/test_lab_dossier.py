import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ops.lab.dossier import render_lab_dossier, write_lab_dossier  # noqa: E402
from tpcore.backtest.credibility import CredibilityScore  # noqa: E402
from tpcore.lab.models import LabResult, ParamDelta  # noqa: E402

# T3 shipped `tpcore/lab/models.py` with `credibility_rubric: CredibilityScore`
# (a typed pydantic-v2 model, not a free dict). Align the fixture seam to the
# real shipped symbol — mirroring the canonical construction in the T3 test
# `tpcore/tests/test_lab_context.py` — while keeping every Task-8 assertion
# (deterministic render, path scheme + seed discriminator, idempotent write)
# verbatim. The dossier itself stays a pure function of `r`.
_RUBRIC = CredibilityScore(
    lookahead_clean=True, survivorship_inclusive=True,
    pit_fundamentals=True, regime_coverage=True,
    out_of_sample_validated=True, monte_carlo_drawdown=True,
    score=72)

_R = LabResult(candidate="exp1", target_engine="reversion",
               intent="fold_existing", verdict="SURVIVED", dsr=0.97,
               credibility_score=72, credibility_rubric=_RUBRIC,
               held_metrics={"sharpe": 1.1, "profit_factor": 1.6,
                             "max_drawdown": -0.08, "n_trades": 12,
                             "win_rate": 0.55},
               winning_params={"z_threshold": 3.2},
               param_diff=[ParamDelta(name="z_threshold", current=3.0,
                                      winning=3.2)],
               recommended_exit="fold_existing", ranked_alternatives=[],
               walk_windows=[], n_trials=200, seed=7,
               generated_at=datetime(2026, 5, 18, tzinfo=UTC))


def test_render_is_deterministic_and_actionable():
    a = render_lab_dossier(_R)
    b = render_lab_dossier(_R)
    assert a == b
    assert "SURVIVED" in a and "fold_existing" in a
    assert "z_threshold" in a and "3.0" in a and "3.2" in a  # the diff


def test_path_scheme_and_idempotent_write(tmp_path, monkeypatch):
    monkeypatch.setattr("ops.lab.dossier.LAB_DIR", tmp_path)
    p1 = write_lab_dossier(_R)
    p2 = write_lab_dossier(_R)
    assert p1 == p2  # same candidate+verdict+seed ⇒ same path
    assert p1.name == "2026-05-18-exp1-SURVIVED-seed7.md"  # O4 discriminator
    assert p1.read_text() == render_lab_dossier(_R)
