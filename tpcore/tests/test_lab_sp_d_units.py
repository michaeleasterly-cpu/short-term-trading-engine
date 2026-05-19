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
