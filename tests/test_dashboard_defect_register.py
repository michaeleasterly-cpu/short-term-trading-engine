"""Unit tests for the Defect-Register panel classifier (DR3).

Pure: fabricated DefectRow-shaped namespaces. No DB, no Streamlit, no
``ops.defect_register`` import (so the ``ops/`` package-shadow hazard is
not even in play here — the classifier is duck-typed on the DefectRow
field names DR1/DR2 froze). Mirrors tests/test_dashboard_escalation.py.

Pure-component only: ``dashboard.py`` is intentionally NOT import-tested
here because ``streamlit`` is not a CI dependency (it lives only in the
local dev venv) — exactly the tests/test_dashboard_escalation.py
precedent.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from dashboard_components.defect_register import classify_defect_register

_NOW = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)


def _row(ref, *, origin="escalation", lane="engine", summary="boom",
         state="open", opened_at=_NOW, policy=None, fix_ref=None):
    """Shaped exactly like ops.defect_register.DefectRow (the frozen
    DR1/DR2 field contract) — duck-typed so this test never imports the
    ops-package module."""
    return SimpleNamespace(
        defect_ref=ref, origin=origin, lane=lane, summary=summary,
        state=state, opened_at=opened_at, policy=policy, fix_ref=fix_ref)


def test_empty_register_is_green() -> None:
    color, summary, detail = classify_defect_register([])
    assert color == "green"
    assert detail == []
    assert "no" in summary.lower()


def test_open_defect_is_red() -> None:
    color, summary, detail = classify_defect_register(
        [_row("h1", state="open")])
    assert color == "red"
    assert any("h1" in d[0] for d in detail)
    assert any(d[1] == "red" for d in detail)


def test_fixed_only_is_amber_not_red() -> None:
    color, summary, detail = classify_defect_register(
        [_row("#250", origin="review", lane="data", state="fixed",
              fix_ref="#88")])
    assert color == "amber"
    assert all(d[1] != "red" for d in detail)
    assert any("#88" in d[2] for d in detail)


def test_mixed_open_dominates_to_red_and_counts() -> None:
    color, summary, detail = classify_defect_register([
        _row("h1", state="open"),
        _row("#251", origin="review", state="fixed", fix_ref="#87"),
        _row("d2", lane="data", state="open"),
    ])
    assert color == "red"
    assert "2" in summary  # 2 open of 3
    assert len(detail) == 3


def test_detail_is_render_health_row_tuple_contract() -> None:
    """``(label, color, text)`` exactly — so the existing
    ``_render_health_row`` + expander loop renders it unchanged
    (recompute-nothing render-only contract)."""
    _, _, detail = classify_defect_register([_row("h1")])
    for label, color, text in detail:
        assert isinstance(label, str)
        assert color in ("green", "amber", "red")
        assert isinstance(text, str)


def test_review_defect_summary_includes_origin_and_state() -> None:
    color, summary, detail = classify_defect_register(
        [_row("#245", origin="review", lane="risk", state="open",
              summary="RiskGovernor weekly-cap")])
    assert color == "red"
    row = detail[0]
    assert "review" in row[2] or "review" in row[0]
    assert "RiskGovernor weekly-cap" in row[2]
