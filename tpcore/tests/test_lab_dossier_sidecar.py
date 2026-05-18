"""T3 — write_lab_dossier emits a sibling <dossier>.json =
LabResult.model_dump_json(); the .md is byte-unchanged; the planner-side
loader model-validates it (extra=forbid) and rejects missing/tampered.
Lazy in-body import (H-S3-10)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tpcore.backtest.credibility import CredibilityScore
from tpcore.lab.models import LabResult, ParamDelta, WalkWindowRecord

# Per the plan's executor note: CredibilityScore has 6 required bool fields
# (no working model_construct fallback under extra="forbid"). Construct a
# minimal valid instance with the REAL required fields — mirrors the
# canonical construction in scripts/tests/test_lab_dossier.py (SP2 oracle).
_RUBRIC = CredibilityScore(
    lookahead_clean=True, survivorship_inclusive=True,
    pit_fundamentals=True, regime_coverage=True,
    out_of_sample_validated=True, monte_carlo_drawdown=True,
    score=64)


def _labresult() -> LabResult:
    from datetime import date
    return LabResult(
        candidate="rev_cand",
        target_engine="reversion",
        intent="fold_existing",
        verdict="SURVIVED",
        dsr=0.97,
        credibility_score=64,
        credibility_rubric=_RUBRIC,
        held_metrics={"n_trades": 12, "sharpe": 1.1},
        winning_params={"z_threshold": 3.1, "max_hold_days": 8},
        param_diff=[ParamDelta(name="z_threshold", current=2.5, winning=3.1)],
        recommended_exit="fold_existing",
        ranked_alternatives=[{"z_threshold": 3.0}],
        walk_windows=[WalkWindowRecord(
            train_start=date(2018, 1, 1), train_end=date(2020, 12, 31),
            holdout_start=date(2021, 1, 1), holdout_end=date(2021, 12, 31))],
        n_trials=4,
        seed=0,
        generated_at=datetime(2026, 5, 18, tzinfo=UTC),
    )


def test_sidecar_roundtrips_labresult_and_md_unchanged(tmp_path, monkeypatch):
    import ops.lab.dossier as dossier
    monkeypatch.setattr(dossier, "LAB_DIR", tmp_path, raising=True)
    r = _labresult()
    md_text = dossier.render_lab_dossier(r)
    p = dossier.write_lab_dossier(r)
    # .md byte-stable: write_lab_dossier writes EXACTLY render_lab_dossier.
    assert p.read_text() == md_text
    sidecar = p.with_suffix(".json")
    assert sidecar.is_file(), "no <dossier>.json sidecar written"
    from ops.engine_sdlc._evidence import load_labresult_sidecar
    loaded = load_labresult_sidecar(p)
    assert loaded == r  # frozen pydantic round-trips by value


def test_loader_rejects_missing_sidecar(tmp_path):
    from ops.engine_sdlc._evidence import EvidenceError, load_labresult_sidecar
    md = tmp_path / "2026-05-18-x-SURVIVED-seed0.md"
    md.write_text("# rendered only, no sidecar")
    with pytest.raises(EvidenceError, match="no LabResult sidecar"):
        load_labresult_sidecar(md)


def test_loader_rejects_tampered_extra_field(tmp_path):
    from ops.engine_sdlc._evidence import EvidenceError, load_labresult_sidecar
    md = tmp_path / "2026-05-18-x-SURVIVED-seed0.md"
    md.write_text("# rendered only")
    sidecar = md.with_suffix(".json")
    payload = json.loads(_labresult().model_dump_json())
    payload["smuggled_field"] = "evil"
    sidecar.write_text(json.dumps(payload))
    with pytest.raises(EvidenceError, match="tampered|extra|forbid"):
        load_labresult_sidecar(md)
