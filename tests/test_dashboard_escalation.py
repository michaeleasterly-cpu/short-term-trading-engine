"""Unit tests for the escalation/integrity audit classifiers.

Pure: fabricated row dicts / string lists. No DB, no Streamlit.
Mirrors tests/test_dashboard_platform_health.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashboard_components.escalation import (
    classify_cross_table_audit,
    classify_recent_escalations,
    classify_source_holds,
    classify_undispositioned,
)


def test_source_holds_none_is_green() -> None:
    color, summary, detail = classify_source_holds([])
    assert color == "green" and detail == []
    assert "no" in summary.lower()


def test_source_holds_open_is_red_with_age() -> None:
    held_at = datetime.now(UTC) - timedelta(hours=30)
    color, summary, detail = classify_source_holds(
        [{"source": "validation:prices_daily", "held_at": held_at,
          "reason": "stuck"}]
    )
    assert color == "red"
    assert any("prices_daily" in row[0] for row in detail)
    assert any("30h" in row[2] or "1d" in row[2] for row in detail)


def test_undispositioned_empty_is_green() -> None:
    color, summary, detail = classify_undispositioned([])
    assert color == "green" and detail == []


def test_undispositioned_nonempty_is_red_verbatim() -> None:
    items = [
        "2026-05-01 [DATA_SOURCE_ESCALATED] ref=h1 stuck | "
        "policy:escalate_operator — operator dispositions via digest",
    ]
    color, summary, detail = classify_undispositioned(items)
    assert color == "red"
    assert detail[0][2] == items[0]
    assert "1" in summary


def test_cross_table_audit_all_green() -> None:
    color, summary, detail = classify_cross_table_audit(
        [{"source": "cross_table_audit.tradier_options_chains.x",
          "stale": False, "confidence": 1.0}]
    )
    assert color == "green"


def test_cross_table_audit_red_on_stale_or_low_conf() -> None:
    color, summary, detail = classify_cross_table_audit(
        [{"source": "cross_table_audit.t.a", "stale": False,
          "confidence": 0.0},
         {"source": "cross_table_audit.t.b", "stale": True,
          "confidence": 1.0}]
    )
    assert color == "red"
    assert len([r for r in detail if r[1] == "red"]) == 2


def test_cross_table_audit_no_rows_amber() -> None:
    color, summary, detail = classify_cross_table_audit([])
    assert color == "amber"


def test_recent_escalations_resolved_vs_open() -> None:
    color, summary, detail = classify_recent_escalations(
        [{"etype": "DATA_REPAIR_ESCALATED", "ref": "r1",
          "recorded_at": datetime.now(UTC), "message": "m1",
          "resolved": True},
         {"etype": "DATA_REPAIR_ESCALATED", "ref": "r2",
          "recorded_at": datetime.now(UTC), "message": "m2",
          "resolved": False}]
    )
    assert color == "red"
    assert any("OPEN" in row[2] for row in detail)
    assert any("resolved" in row[2].lower() for row in detail)


def test_recent_escalations_none_is_green() -> None:
    color, summary, detail = classify_recent_escalations([])
    assert color == "green"
