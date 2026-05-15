"""Unit tests for ``ops/platform_pipeline.py`` flag handling.

Pins the ``--dry-run`` non-destructive contract added 2026-05-15:

* ``--dry-run`` is forwarded to ``ops.py --update`` (so stages
  return ``DRY_RUN`` without invoking the handler).
* The engine sweep is **skipped entirely** under ``--dry-run`` — no
  Alpaca paper orders are submitted.

We don't exercise the real DB / subprocess paths — we monkey-patch
``_run_step`` to capture the argv that would have been spawned, and
assert the structural contract.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "ops"
if str(OPS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(OPS_DIR.parent))

from ops import platform_pipeline  # noqa: E402


@pytest.fixture(autouse=True)
def _set_dsn(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")


@pytest.mark.asyncio
async def test_dry_run_forwards_to_ops_update_and_skips_engine_sweep(monkeypatch):
    """`--dry-run` forwards the flag to ops.py and short-circuits before
    the engine sweep — no second `_run_step` call should fire."""
    monkeypatch.setattr(sys, "argv", ["platform_pipeline.py", "--dry-run", "--force"])
    calls: list[tuple[str, list]] = []

    async def _fake_run_step(name, argv):
        calls.append((name, argv))
        return 0

    monkeypatch.setattr(platform_pipeline, "_run_step", _fake_run_step)
    rc = await platform_pipeline.amain()

    assert rc == 0
    # Exactly one call — the ops update. Engine sweep skipped.
    assert len(calls) == 1
    step_name, argv = calls[0]
    assert step_name == "ops_update"
    assert "--update" in argv
    assert "--dry-run" in argv
    assert "--force" in argv
    assert "--source" in argv
    assert argv[argv.index("--source") + 1] == "platform_pipeline"


@pytest.mark.asyncio
async def test_no_dry_run_runs_both_phases(monkeypatch):
    """Without `--dry-run`, both phases fire and the engine sweep gets
    the argv we expect."""
    monkeypatch.setattr(sys, "argv", ["platform_pipeline.py"])
    calls: list[tuple[str, list]] = []

    async def _fake_run_step(name, argv):
        calls.append((name, argv))
        return 0

    monkeypatch.setattr(platform_pipeline, "_run_step", _fake_run_step)
    rc = await platform_pipeline.amain()

    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0] == "ops_update"
    assert "--dry-run" not in calls[0][1]
    assert calls[1][0] == "engine_sweep"


@pytest.mark.asyncio
async def test_update_failure_short_circuits_engine_sweep(monkeypatch):
    """If ops.py --update exits non-zero, engine sweep must not run
    (regardless of --dry-run setting)."""
    monkeypatch.setattr(sys, "argv", ["platform_pipeline.py"])
    calls: list[tuple[str, list]] = []

    async def _fake_run_step(name, argv):
        calls.append((name, argv))
        return 1 if name == "ops_update" else 0

    monkeypatch.setattr(platform_pipeline, "_run_step", _fake_run_step)
    rc = await platform_pipeline.amain()

    assert rc == 1
    assert len(calls) == 1
    assert calls[0][0] == "ops_update"


@pytest.mark.asyncio
async def test_no_dsn_returns_1(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["platform_pipeline.py"])
    # No _run_step calls should happen — DSN check is first.
    monkeypatch.setattr(platform_pipeline, "_run_step", AsyncMock())
    rc = await platform_pipeline.amain()
    assert rc == 1
