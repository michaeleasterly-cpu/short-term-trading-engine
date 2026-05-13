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
    classify_coverage_gaps,
    classify_cross_ref,
    classify_daemons,
    classify_forensics,
    classify_fundamentals,
    classify_open_orders,
    classify_universe,
    classify_update_run,
    classify_validation,
    update_required_banner,
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


def test_validation_green_when_latest_runs_passed():
    # Latest run per source: stale=False, confidence=1.0.
    rows = [
        {"source": "validation.delistings", "latest_at": datetime.now(UTC), "stale": False, "confidence": 1.0, "notes": None, "n_failed": 0, "n_runs": 1},
        {"source": "validation.constituent", "latest_at": datetime.now(UTC), "stale": False, "confidence": 1.0, "notes": None, "n_failed": 0, "n_runs": 1},
    ]
    color, summary, detail = classify_validation(rows)
    assert color == "green"
    assert len(detail) == 2
    assert all(c == "green" for _, c, _ in detail)
    assert "all 2 check(s) passed" in summary


def test_validation_red_when_latest_run_failed():
    rows = [
        {"source": "validation.delistings", "latest_at": datetime.now(UTC), "stale": True, "confidence": 0.615, "notes": "[]", "n_failed": 1, "n_runs": 1},
        {"source": "validation.constituent", "latest_at": datetime.now(UTC), "stale": False, "confidence": 1.0, "notes": None, "n_failed": 0, "n_runs": 1},
    ]
    color, summary, detail = classify_validation(rows)
    assert color == "red"
    assert "1/2 check(s) FAILED" in summary
    # The failed source is the red row in detail.
    by_src = {s: c for s, c, _ in detail}
    assert by_src["delistings"] == "red"
    assert by_src["constituent"] == "green"


def test_validation_green_when_stale_history_excluded():
    # The fix: we no longer aggregate over 7 days. If today's run is
    # clean, the dashboard is clean even if last week was red. This was
    # the bug — the dashboard showed AAPL split as red because the rolling
    # window included pre-fix history.
    rows = [
        {"source": "validation.splits", "latest_at": datetime.now(UTC), "stale": False, "confidence": 1.0, "notes": None, "n_failed": 0, "n_runs": 1},
    ]
    color, _, detail = classify_validation(rows)
    assert color == "green"
    assert detail[0][1] == "green"


def test_validation_amber_when_no_runs():
    color, summary, _ = classify_validation([])
    assert color == "amber"
    assert "No validation runs" in summary


# ─── update_required_banner — NYSE-relative staleness signal ────────────────


def test_banner_hidden_when_bars_cover_most_recent_close():
    # Pick a known closed session and bars from that same session.
    # 2026-05-12 was a regular Tuesday session. After its close, bars dated
    # 2026-05-12 should require no banner.
    now = datetime(2026, 5, 12, 22, 0, tzinfo=UTC)  # 18:00 ET
    banner = update_required_banner(datetime(2026, 5, 12).date(), now)
    assert banner is None


def test_banner_required_for_overnight_manila_wakeup():
    # User in Manila wakes up 09:00 local on a Monday — that's UTC 01:00
    # Monday, ET 21:00 Sunday. Friday's session is the most recent close
    # (~28h ago), and bars are from the previous Thursday → 1 trading day
    # behind, well past grace → "required".
    now = datetime(2026, 5, 11, 1, 0, tzinfo=UTC)  # ET Sun 21:00
    banner = update_required_banner(datetime(2026, 5, 7).date(), now)
    assert banner is not None
    severity, message = banner
    assert severity == "required"
    assert "Update required" in message


def test_banner_warn_within_publication_grace():
    # 30 minutes after the close, bars not yet available → warn, not required.
    now = datetime(2026, 5, 12, 20, 30, tzinfo=UTC)  # ~30m after 16:00 ET close
    banner = update_required_banner(datetime(2026, 5, 11).date(), now)
    assert banner is not None
    severity, _ = banner
    assert severity == "warn"


def test_banner_required_when_no_bars_at_all():
    banner = update_required_banner(None)
    assert banner is not None
    severity, message = banner
    assert severity == "required"
    assert "No bars" in message


# ─── Coverage gaps ──────────────────────────────────────────────────────────


def test_coverage_green_under_2pct_gaps():
    # 10/1000 = 1% bar gap, 15/1000 = 1.5% fund gap → green
    color, text = classify_coverage_gaps(bar_gap_count=10, fund_gap_count=15, tier_le_2_total=1000)
    assert color == "green"
    assert "1.0%" in text
    assert "1.5%" in text


def test_coverage_amber_between_2_and_5pct():
    # 30/1000 = 3% → amber
    color, _ = classify_coverage_gaps(bar_gap_count=30, fund_gap_count=10, tier_le_2_total=1000)
    assert color == "amber"


def test_coverage_red_above_5pct():
    color, _ = classify_coverage_gaps(bar_gap_count=60, fund_gap_count=10, tier_le_2_total=1000)
    assert color == "red"
    # Either dimension being above threshold should trigger red.
    color, _ = classify_coverage_gaps(bar_gap_count=10, fund_gap_count=60, tier_le_2_total=1000)
    assert color == "red"


def test_coverage_amber_when_no_universe():
    color, text = classify_coverage_gaps(0, 0, 0)
    assert color == "amber"
    assert "No tier" in text


# ─── Open orders ────────────────────────────────────────────────────────────


def test_open_orders_green_when_no_pending():
    color, text = classify_open_orders(pending_count=0, stale_24h_count=0)
    assert color == "green"
    assert "No pending" in text


def test_open_orders_green_when_pending_but_fresh():
    color, text = classify_open_orders(pending_count=3, stale_24h_count=0)
    assert color == "green"
    assert "all <24h" in text


def test_open_orders_amber_when_one_stale():
    color, text = classify_open_orders(pending_count=1, stale_24h_count=1)
    assert color == "amber"
    assert "1 older than 24h" in text


def test_open_orders_red_when_multiple_stale():
    color, text = classify_open_orders(pending_count=5, stale_24h_count=3)
    assert color == "red"
    assert "3 older than 24h" in text


# ─── Forensics ──────────────────────────────────────────────────────────────


def test_forensics_green_when_no_open_triggers():
    color, summary = classify_forensics({"by_kind": [], "recent": []})
    assert color == "green"
    assert "No open" in summary


def test_forensics_amber_when_recent_trigger():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    state = {
        "by_kind": [
            {"kind": "outlier_loss", "open_count": 1, "oldest_open_at": now - timedelta(days=2)},
        ],
        "recent": [{"id": 1}],
    }
    color, summary = classify_forensics(state)
    assert color == "amber"
    assert "outlier_loss=1" in summary
    assert "2d" in summary


def test_forensics_red_when_stale_trigger_unresolved():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    state = {
        "by_kind": [
            {"kind": "drawdown_period", "open_count": 2, "oldest_open_at": now - timedelta(days=20)},
        ],
        "recent": [],
    }
    color, summary = classify_forensics(state)
    assert color == "red"
    assert "20d unresolved" in summary


# ─── Cross-reference roll-up ────────────────────────────────────────────────


def test_cross_ref_green_when_no_findings():
    color, summary, detail = classify_cross_ref([])
    assert color == "green"
    assert "clean" in summary
    assert detail == []


def test_cross_ref_green_when_all_zero():
    findings = [
        {"check": "ticker_not_in_prices", "table": "fundamentals_quarterly", "count": 0},
        {"check": "expired", "table": "tradier_options_chains", "count": 0},
    ]
    color, summary, detail = classify_cross_ref(findings)
    assert color == "green"
    assert all(c == "green" for _, c, _ in detail)


def test_cross_ref_red_when_any_nonzero():
    findings = [
        {"check": "ticker_not_in_prices", "table": "fundamentals_quarterly", "count": 0},
        {"check": "expired", "table": "tradier_options_chains", "count": 2494},
    ]
    color, summary, detail = classify_cross_ref(findings)
    assert color == "red"
    assert "1/2" in summary
    by = {f"{x[0]}": x for x in detail}
    assert by["tradier_options_chains.expired"][1] == "red"
    assert by["fundamentals_quarterly.ticker_not_in_prices"][1] == "green"


# ─── Daemon roll-up ─────────────────────────────────────────────────────────


def test_daemons_red_when_any_not_installed():
    daemons = [
        {"name": "trade_monitor", "installed": False, "kind": "persistent",
         "next_run_hint": "Mon-Sat persistent"},
        {"name": "post_close", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon-Fri 21:30 UTC"},
        {"name": "allocator", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon 13:00 UTC"},
    ]
    color, summary, detail = classify_daemons(daemons)
    assert color == "red"
    assert "1/3 daemon(s) not installed" in summary
    assert detail[0][1] == "red"


def test_daemons_amber_when_persistent_silent_24h():
    daemons = [
        {"name": "trade_monitor", "installed": True, "kind": "persistent",
         "last_log_age_sec": 36 * 3600, "next_run_hint": ""},
        {"name": "post_close", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon-Fri 21:30 UTC"},
        {"name": "allocator", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon 13:00 UTC"},
    ]
    color, _, detail = classify_daemons(daemons)
    assert color == "amber"
    by = {row[0]: row for row in detail}
    assert by["trade_monitor"][1] == "amber"


def test_daemons_amber_when_scheduled_last_exit_nonzero():
    daemons = [
        {"name": "trade_monitor", "installed": True, "kind": "persistent",
         "last_log_age_sec": 60, "next_run_hint": ""},
        {"name": "post_close", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 1,
         "next_run_hint": "Mon-Fri 21:30 UTC"},
        {"name": "allocator", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon 13:00 UTC"},
    ]
    color, _, detail = classify_daemons(daemons)
    assert color == "amber"
    by = {row[0]: row for row in detail}
    assert by["post_close"][1] == "amber"


def test_daemons_green_when_all_healthy():
    daemons = [
        {"name": "trade_monitor", "installed": True, "kind": "persistent",
         "last_log_age_sec": 60, "next_run_hint": ""},
        {"name": "post_close", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon-Fri 21:30 UTC"},
        {"name": "allocator", "installed": True, "kind": "scheduled",
         "last_run_at": datetime.now(UTC), "last_exit": 0,
         "next_run_hint": "Mon 13:00 UTC"},
    ]
    color, summary, _ = classify_daemons(daemons)
    assert color == "green"
    assert "3 daemons installed and healthy" in summary


def test_daemons_empty_list_is_red():
    color, summary, detail = classify_daemons([])
    assert color == "red"
    assert detail == []
