"""Sentinel tests for ``_stage_historical_macro_indicators``.

PR fix/feed-audit-wave-1-critical-path-blockers — Wave-1 critical-path
heal for the ``macro_indicators_completeness`` red on ``main``
(initial_claims 1042+ Thursday-anchor false-positives; per-indicator
backfill stage was missing per the audit).

These tests pin the stage's CLI contract (indicator / indicators /
since / until params) and its delegation to ``per_indicator_fred_repull``
without round-tripping through asyncpg or FRED.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# Use importlib to load scripts/ops.py under a private name —
# bypasses the ops-package-shadow rule (ops/*.py exists as a sibling
# Python package). Same pattern as
# tests/test_stage_historical_delisted_universe.py.
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_macro_hist", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_macro_hist"] = ops
_spec.loader.exec_module(ops)


pytestmark = pytest.mark.xdist_group("ops_shadow")


# ── H1 — missing indicator/indicators param raises (no silent green) ──


async def test_H1_missing_indicator_param_raises() -> None:
    pool = AsyncMock()
    with pytest.raises(RuntimeError, match="indicator"):
        await ops._stage_historical_macro_indicators(pool, {})


# ── H2 — single-indicator param dispatches per_indicator_fred_repull ──


async def test_H2_single_indicator_dispatch() -> None:
    pool = AsyncMock()

    async def _fake_repull(
        _pool, indicators, *, start=None, end=None,
    ) -> dict[str, int]:
        return {ind: 100 for ind in indicators}

    with patch(
        "tpcore.fred.targeted_repull.per_indicator_fred_repull",
        new=_fake_repull,
    ):
        result = await ops._stage_historical_macro_indicators(
            pool,
            {"indicator": "initial_claims", "since": "1967-01-01"},
        )
    assert result["indicators"] == ["initial_claims"]
    assert result["rows_per_indicator"] == {"initial_claims": 100}
    assert result["rows_total"] == 100


# ── H3 — comma-separated indicators dispatches the full batch ─────────


async def test_H3_csv_indicators_dispatch() -> None:
    pool = AsyncMock()

    captured: dict[str, Any] = {}

    async def _fake_repull(
        _pool, indicators, *, start=None, end=None,
    ) -> dict[str, int]:
        captured["indicators"] = list(indicators)
        captured["start"] = start
        captured["end"] = end
        return {ind: 10 for ind in indicators}

    with patch(
        "tpcore.fred.targeted_repull.per_indicator_fred_repull",
        new=_fake_repull,
    ):
        await ops._stage_historical_macro_indicators(
            pool,
            {
                "indicators": "vix,credit_spread,initial_claims",
                "since": "2024-01-01",
                "until": "2024-12-31",
            },
        )
    assert captured["indicators"] == ["vix", "credit_spread", "initial_claims"]
    assert captured["start"] == date(2024, 1, 1)
    assert captured["end"] == date(2024, 12, 31)


# ── H4 — rows_total ignores derived-indicator sentinel (-1) ───────────


async def test_H4_rows_total_skips_derived_sentinel() -> None:
    pool = AsyncMock()

    async def _fake_repull(
        _pool, indicators, *, start=None, end=None,
    ) -> dict[str, int]:
        # sos_state_diffusion is derived → adapter returns -1 sentinel.
        return {"vix": 50, "sos_state_diffusion": -1}

    with patch(
        "tpcore.fred.targeted_repull.per_indicator_fred_repull",
        new=_fake_repull,
    ):
        result = await ops._stage_historical_macro_indicators(
            pool, {"indicators": "vix,sos_state_diffusion"},
        )
    # rows_total only sums non-negative counts; -1 sentinel doesn't
    # silently inflate the success metric.
    assert result["rows_total"] == 50
