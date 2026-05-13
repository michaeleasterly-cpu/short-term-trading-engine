"""Tests for the dashboard's platform-health classifier functions.

The classifiers are pure: input is a single timestamp / count / row list,
output is a (color, text) tuple. Async fetcher hits the DB and is exercised
manually via scripts/run_platform_health_smoke.sh — not in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashboard_components.health import (
    OPS_UPDATE_STAGES,
    classify_bars,
    classify_corp_actions,
    classify_fundamentals,
    classify_universe,
    classify_update_run,
    classify_validation,
)

# ─── Bars freshness ─────────────────────────────────────────────────────────


def test_bars_green_when_today():
    today = datetime.now(UTC).date()
    color, text = classify_bars(today)
    assert color == "green"
    assert today.isoformat() in text


def test_bars_amber_when_2_to_3_days_old():
    color, _ = classify_bars(datetime.now(UTC).date() - timedelta(days=3))
    assert color == "amber"


def test_bars_red_when_stale_or_missing():
    assert classify_bars(None)[0] == "red"
    assert classify_bars(datetime.now(UTC).date() - timedelta(days=10))[0] == "red"


# ─── Fundamentals freshness ─────────────────────────────────────────────────


def test_fundamentals_green_when_within_a_week():
    color, _ = classify_fundamentals(datetime.now(UTC) - timedelta(days=3))
    assert color == "green"


def test_fundamentals_amber_when_one_missed_sunday():
    color, _ = classify_fundamentals(datetime.now(UTC) - timedelta(days=10))
    assert color == "amber"


def test_fundamentals_red_when_two_missed_sundays():
    color, _ = classify_fundamentals(datetime.now(UTC) - timedelta(days=20))
    assert color == "red"


def test_fundamentals_red_when_missing():
    assert classify_fundamentals(None)[0] == "red"


# ─── Corporate actions ──────────────────────────────────────────────────────


def test_corp_actions_green_when_recent():
    color, _ = classify_corp_actions(datetime.now(UTC) - timedelta(hours=12))
    assert color == "green"


def test_corp_actions_red_when_stale():
    color, _ = classify_corp_actions(datetime.now(UTC) - timedelta(days=10))
    assert color == "red"


# ─── Universe pre-screener ──────────────────────────────────────────────────


def test_universe_green_when_today_populated_healthily():
    today = datetime.now(UTC).date()
    color, text = classify_universe(today, 1249)
    assert color == "green"
    assert "1249" in text


def test_universe_red_when_today_underpopulated():
    today = datetime.now(UTC).date()
    color, _ = classify_universe(today, 100)
    assert color == "red"


def test_universe_amber_when_stale_date():
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    color, text = classify_universe(yesterday, 1249)
    assert color == "amber"
    assert "1d ago" in text


def test_universe_red_when_never_populated():
    assert classify_universe(None, 0)[0] == "red"


# ─── Last --update run ──────────────────────────────────────────────────────


def _stage_complete(name: str) -> dict:
    return {"event_type": "INGESTION_COMPLETE", "recorded_at": datetime.now(UTC), "data": {}}


def _stage_failed(name: str, reason: str) -> dict:
    return {
        "event_type": "INGESTION_FAILED",
        "recorded_at": datetime.now(UTC),
        "data": {"reason": reason, "stage": name},
    }


def test_update_run_green_all_stages_complete():
    run = {
        "started_at": datetime.now(UTC) - timedelta(hours=2),
        "stages": {name: _stage_complete(name) for name in OPS_UPDATE_STAGES},
    }
    color, summary, rows = classify_update_run(run)
    assert color == "green"
    assert f"{len(OPS_UPDATE_STAGES)}/{len(OPS_UPDATE_STAGES)}" in summary
    assert all(c == "green" for _, c, _ in rows)


def test_update_run_red_when_any_stage_failed():
    stages = {name: _stage_complete(name) for name in OPS_UPDATE_STAGES}
    stages["corporate_actions"] = _stage_failed("corporate_actions", "ReadError")
    run = {"started_at": datetime.now(UTC) - timedelta(hours=2), "stages": stages}
    color, summary, rows = classify_update_run(run)
    assert color == "red"
    assert "1 stage(s) FAILED" in summary
    failed_rows = [(s, c, t) for s, c, t in rows if c == "red"]
    assert len(failed_rows) == 1
    assert "ReadError" in failed_rows[0][2]


def test_update_run_amber_when_stages_missing():
    # Only two stages present — implies the run is partial / didn't finish.
    stages = {
        "daily_bars": _stage_complete("daily_bars"),
        "data_validation": _stage_complete("data_validation"),
    }
    run = {"started_at": datetime.now(UTC) - timedelta(hours=2), "stages": stages}
    color, summary, rows = classify_update_run(run)
    assert color == "amber"
    assert "missing" in summary
    # Rows for missing stages should be amber.
    amber_rows = [(s, c, t) for s, c, t in rows if c == "amber"]
    assert len(amber_rows) == len(OPS_UPDATE_STAGES) - 2


def test_update_run_red_when_no_run_recorded():
    color, summary, rows = classify_update_run({"started_at": None, "stages": {}})
    assert color == "red"
    assert "No recent run" in summary
    assert rows == []


def test_update_run_handles_string_data_blob():
    # ops.py sometimes stores `data` as a JSON string. Classifier must
    # not crash and must still surface the reason.
    stages = {name: _stage_complete(name) for name in OPS_UPDATE_STAGES}
    stages["fundamentals_refresh"] = {
        "event_type": "INGESTION_FAILED",
        "recorded_at": datetime.now(UTC),
        "data": '{"stage": "fundamentals_refresh", "reason": "timeout"}',
    }
    run = {"started_at": datetime.now(UTC) - timedelta(hours=2), "stages": stages}
    color, summary, rows = classify_update_run(run)
    assert color == "red"
    failed = [(s, c, t) for s, c, t in rows if c == "red"]
    assert "timeout" in failed[0][2]


# ─── Validation suite ───────────────────────────────────────────────────────


def test_validation_green_when_all_sources_passed():
    rows = [
        {"source": "validation.delistings", "latest_at": datetime.now(UTC), "n_failed": 0, "n_runs": 7},
        {"source": "validation.constituent", "latest_at": datetime.now(UTC), "n_failed": 0, "n_runs": 7},
    ]
    color, summary, detail = classify_validation(rows)
    assert color == "green"
    assert len(detail) == 2
    assert all(c == "green" for _, c, _ in detail)


def test_validation_amber_when_one_or_two_failures():
    rows = [
        {"source": "validation.delistings", "latest_at": datetime.now(UTC), "n_failed": 2, "n_runs": 7},
        {"source": "validation.constituent", "latest_at": datetime.now(UTC), "n_failed": 0, "n_runs": 7},
    ]
    color, _, _ = classify_validation(rows)
    assert color == "amber"


def test_validation_red_when_persistent_failures():
    rows = [
        {"source": "validation.delistings", "latest_at": datetime.now(UTC), "n_failed": 10, "n_runs": 14},
    ]
    color, summary, detail = classify_validation(rows)
    assert color == "red"
    assert detail[0][1] == "red"


def test_validation_amber_when_no_runs():
    color, summary, _ = classify_validation([])
    assert color == "amber"
    assert "No validation runs" in summary
