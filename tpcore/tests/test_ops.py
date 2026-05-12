"""Tests for the pure helpers in ``scripts/ops.py``.

Limited to parsing / formatting / exit-code logic — the orchestration
paths and DB queries are integration-tested by actually running the CLI
(`python scripts/ops.py --check` against the live DB) since mocking
asyncpg pools for them would be more brittle than the code itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import ops  # noqa: E402 — sys.path adjusted above


# ────────────────────────────────────────────────────────────────────────
# _CANDIDATE_RE — parses lines like "Sigma candidates: 4" emitted by
# scripts/simulate_universe.py. Brittle by nature: regression here is
# silent (we'd report 0 candidates for every engine), so it deserves a test.
# ────────────────────────────────────────────────────────────────────────

def test_candidate_re_matches_three_engines():
    stdout = (
        "Universe simulation as of 2026-05-12\n"
        "Sigma candidates: 4\n"
        "Reversion candidates: 0\n"
        "Vector candidates: 2\n"
        "done\n"
    )
    matches = {m.group(1).strip().lower(): int(m.group(2)) for m in ops._CANDIDATE_RE.finditer(stdout)}
    assert matches == {"sigma": 4, "reversion": 0, "vector": 2}


def test_candidate_re_tolerates_indentation_and_singular():
    stdout = (
        "  Sigma candidate: 1\n"      # singular form
        "  Reversion candidates: 12\n"
        "Vector candidates: 0\n"
    )
    matches = {m.group(1).strip().lower(): int(m.group(2)) for m in ops._CANDIDATE_RE.finditer(stdout)}
    assert matches["sigma"] == 1
    assert matches["reversion"] == 12
    assert matches["vector"] == 0


def test_candidate_re_ignores_unrelated_lines():
    stdout = (
        "Loaded 47 tickers\n"
        "Sigma candidates: 3\n"
        "ERROR: something\n"
        "candidates: 99\n"   # no engine name → should not match
    )
    matches = list(ops._CANDIDATE_RE.finditer(stdout))
    assert len(matches) == 1
    assert matches[0].group(1).strip().lower() == "sigma"


# ────────────────────────────────────────────────────────────────────────
# UpdateSummary.exit_code — FAILED/TIMEOUT taints the run; DRY_RUN and
# SKIPPED do not. The CLI's process exit code depends on this.
# ────────────────────────────────────────────────────────────────────────

def _summary(stages):
    import uuid
    from datetime import UTC, datetime
    s = ops.UpdateSummary(run_id=uuid.uuid4(), started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
    s.stages = stages
    return s


def test_exit_code_zero_when_all_ok():
    s = _summary([ops.StageResult("a", "OK", 100), ops.StageResult("b", "OK", 200)])
    assert s.exit_code == 0


def test_exit_code_one_when_any_failed():
    s = _summary([ops.StageResult("a", "OK", 100), ops.StageResult("b", "FAILED", 50, error="boom")])
    assert s.exit_code == 1


def test_exit_code_one_when_any_timeout():
    s = _summary([ops.StageResult("a", "TIMEOUT", 120_000, error="t/o"), ops.StageResult("b", "OK", 50)])
    assert s.exit_code == 1


def test_exit_code_zero_for_dry_run_only():
    s = _summary([ops.StageResult("a", "DRY_RUN", 0), ops.StageResult("b", "DRY_RUN", 0)])
    assert s.exit_code == 0


# ────────────────────────────────────────────────────────────────────────
# UpdateSummary.to_table — terminal-facing format.
# ────────────────────────────────────────────────────────────────────────

def test_to_table_renders_header_and_rows():
    s = _summary([
        ops.StageResult("daily_bars", "OK", 1234, detail={"rows_upserted": 42}),
        ops.StageResult("validation", "FAILED", 999, error="boom"),
    ])
    out = s.to_table()
    lines = out.splitlines()
    assert lines[0].startswith("Stage")
    assert "Status" in lines[0]
    assert "daily_bars" in out
    assert "OK" in out
    assert "FAILED" in out
    assert "boom" in out
    assert "rows_upserted=42" in out


# ────────────────────────────────────────────────────────────────────────
# _format_check_pretty — operator-facing health report.
# ────────────────────────────────────────────────────────────────────────

def test_format_check_pretty_ok_run():
    report = {
        "run_id": "abc",
        "timestamp": "2026-05-12T00:00:00+00:00",
        "ok": True,
        "checks": {
            "db_connectivity": {"ok": True, "result": 1},
            "data_freshness": {"ok": True, "latest_bar": "2026-05-08", "age_days": 4},
        },
    }
    out = ops._format_check_pretty(report)
    assert "Health check OK" in out
    assert "db_connectivity" in out
    assert "[OK]" in out
    assert "latest_bar: 2026-05-08" in out


def test_format_check_pretty_degraded_run_marks_failed_checks():
    report = {
        "run_id": "abc",
        "timestamp": "2026-05-12T00:00:00+00:00",
        "ok": False,
        "checks": {
            "db_connectivity": {"ok": True, "result": 1},
            "recent_errors": {"ok": False, "count": 3, "errors": [
                {"engine": "sigma", "message": "kaboom"},
                {"engine": "vector", "message": "bzzt"},
                {"engine": "ingest", "message": "splat"},
            ]},
        },
    }
    out = ops._format_check_pretty(report)
    assert "DEGRADED" in out
    assert "[!!" in out
    assert "count: 3" in out
    # First few error items are rendered, capped at 5 — here only 3.
    assert "kaboom" in out
    assert "bzzt" in out


def test_format_check_pretty_truncates_long_lists():
    report = {
        "run_id": "abc",
        "timestamp": "2026-05-12T00:00:00+00:00",
        "ok": False,
        "checks": {
            "recent_errors": {
                "ok": False,
                "count": 12,
                "errors": [{"i": i} for i in range(12)],
            },
        },
    }
    out = ops._format_check_pretty(report)
    assert "12 item(s)" in out
    assert "… 7 more" in out


# ────────────────────────────────────────────────────────────────────────
# StageResult defaults — `detail` must be a fresh dict per instance.
# ────────────────────────────────────────────────────────────────────────

def test_stage_result_detail_is_per_instance():
    a = ops.StageResult("a", "OK", 1)
    b = ops.StageResult("b", "OK", 1)
    a.detail["x"] = 1
    assert b.detail == {}
