"""Catalyst scheduler — engine_readiness §10 compliance verifications.

Hermetic source-level scans (no DB / network / live broker). Each test
is a single ``grep`` translated into a concrete assertion against the
checked-in source.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text()


# ─── §10 grep #1 — five BaseEnginePlug subclasses ────────────────────────


def test_exactly_five_base_engine_plug_subclasses():
    import re

    pattern = re.compile(r"class\s+\w+\(BaseEnginePlug\)")
    plug_dir = _REPO_ROOT / "catalyst" / "plugs"
    matches = []
    for p in sorted(plug_dir.glob("*.py")):
        matches.extend(pattern.findall(p.read_text()))
    assert len(matches) == 5, matches


# ─── §10 grep #2 — FilterDiagnostics carried on SIGNAL events ────────────


def test_scheduler_attaches_filter_diagnostics_to_signal():
    src = _read("catalyst/scheduler.py")
    assert "filter_diagnostics" in src
    assert "db_log.signal" in src


# ─── §10 grep #3 — backtest persists the credibility rubric ──────────────


def test_backtest_calls_write_credibility_score():
    src = _read("catalyst/backtest.py")
    assert "write_credibility_score" in src
    assert "engine_name=\"catalyst\"" in src


# ─── §10 grep #4 — scheduler checks is_trading_day before scanning ───────


def test_scheduler_checks_is_trading_day():
    src = _read("catalyst/scheduler.py")
    assert "is_trading_day" in src


# ─── §10 grep #5 — AAR plug uses classify_exit_reason (no hardcode) ──────


def test_aar_plug_uses_classify_exit_reason():
    src = _read("catalyst/plugs/aar_logging.py")
    assert "classify_exit_reason" in src
    # Defensive: forbid a hardcoded literal like `ExitReason.TIME_STOP` /
    # `ExitReason.TAKE_PROFIT` in the AAR plug. (Imports are allowed for
    # the type annotation only.)
    bad_patterns = [
        "exit_reason=ExitReason.TIME_STOP",
        "exit_reason=ExitReason.TAKE_PROFIT",
        "exit_reason=ExitReason.STOP_LOSS",
    ]
    for bad in bad_patterns:
        assert bad not in src, (
            f"AAR plug hardcodes ExitReason literal {bad!r} — use "
            "classify_exit_reason instead (STYLE_GUIDE).")


# ─── §10 grep #6 — scheduler cancels its own stale orders ────────────────


def test_scheduler_cancels_stale_orders():
    src = _read("catalyst/scheduler.py")
    assert "_cancel_stale_catalyst_orders" in src


# ─── §10 grep #9 — STARTUP + SHUTDOWN emitted to application_log ─────────


def test_scheduler_emits_startup_and_shutdown():
    src = _read("catalyst/scheduler.py")
    assert "db_log.startup(" in src
    assert "db_log.shutdown(" in src


# ─── §10 grep #8 — engine prefix registered for catalyst ─────────────────


def test_engine_prefix_registered_for_catalyst():
    from tpcore.order_ids import ENGINE_PREFIX

    assert "catalyst" in ENGINE_PREFIX
    assert ENGINE_PREFIX["catalyst"] == "ct_"


# ─── Scheduler imports + signature smoke ─────────────────────────────────


def test_run_once_is_importable_async_callable():
    from catalyst.scheduler import run_once

    assert callable(run_once)


def test_run_once_returns_non_trading_day_action_offline():
    """run_once early-returns when called on a non-trading day — the
    is_trading_day gate is wired, no DB connection attempted."""
    import asyncio
    from datetime import date

    from catalyst.scheduler import run_once

    # Saturday — guaranteed non-trading day across all years XNYS uses.
    # 2024-05-18 is a Saturday.
    saturday = date(2024, 5, 18)
    result = asyncio.run(run_once(as_of=saturday))
    assert result == {"as_of": "2024-05-18", "action": "non_trading_day"}


# ─── Lab targeting — catalyst is roster-eligible by construction ─────────


@pytest.mark.parametrize("attr", [
    "LAB_TARGET", "default_params",
    "load_catalyst_window_context", "run_catalyst_with_context",
    "run_for_search",
])
def test_backtest_module_exports_lab_dispatch_surface(attr):
    """SP-B / SP-F: the engine's backtest exports the uniform Lab
    dispatch surface — adding catalyst to ``_PROFILE`` (via the ECR)
    plus this LAB_TARGET is ALL that is needed to be Lab-targetable."""
    import catalyst.backtest as bt

    assert hasattr(bt, attr), attr
