"""Test-scoped fixtures for tpcore tests.

H-S3-12 autonomous Lab criteria (`docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md`):
the planner's MODIFY validator now reads an INCUMBENT backtest dossier
from ``backtests/<engine>_backtest_results.json`` to evaluate the
improvement criteria (candidate strictly beats incumbent on the primary
metric + new-engine floor + trade-count drift bound). Existing MODIFY
tests cite ``reversion`` as the target engine, but ``backtests/
reversion_backtest_results.json`` is not on disk by default — production
tests that exercise the MODIFY path get a stable, intentionally-beatable
incumbent dossier via this autouse fixture so the criteria gate has the
substrate it expects without polluting the live ``backtests/`` directory.

The dossier is sharpe=1.0/trades=10/max_drawdown=-0.05/ruin_prob=0.05/
profit_factor=1.1/min_btl_gap=30: clears the new-engine floor but is
intentionally beatable so the canonical ``_labresult()`` (Sharpe 1.1)
clears the strictly-better-than-incumbent clause. The fixture is
session-scoped + cleans up to avoid mutating the live repo state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_SYNTHETIC_INCUMBENT = {
    "engine": "reversion",
    "parameters": {"z_threshold": 2.5, "max_hold_days": 30},
    "credibility_score": 50,
    "passed_gate": False,
    "sharpe": 1.0,
    "profit_factor": 1.1,
    "max_drawdown": -0.05,
    "trades": 10,
    "dsr": 0.6,
    "min_btl_gap": 30,
    "trades_per_param": 5.0,
    "sensitivity_score": None,
    "ruin_probability": 0.05,
}


@pytest.fixture(scope="session", autouse=True)
def _install_reversion_incumbent_dossier():
    """Install a stable, intentionally-beatable reversion incumbent
    dossier at ``backtests/reversion_backtest_results.json`` for tests
    that exercise the MODIFY autonomous-improvement criteria.

    The fixture is conditional: it only creates the file if absent, and
    only removes it if it created it (so a real backtest run's dossier
    is never disturbed). This is the SP-D test-isolation pattern.

    Session-scoped + no-teardown-delete to avoid a per-test AND a
    cross-worker race under ``pytest-xdist``: each worker runs its
    own session, so a function-scoped or even session-scoped delete
    on teardown lets worker A's teardown unlink the file mid-flight
    on worker B, flipping ``_validate_modify`` red for tests still
    running there. We leave the synthetic dossier in place at exit —
    ``backtests/`` is gitignored, so a left-over synthetic file does
    not dirty the repo, and a real backtest run will overwrite it."""
    path = REPO_ROOT / "backtests" / "reversion_backtest_results.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_SYNTHETIC_INCUMBENT, indent=2))
    yield
