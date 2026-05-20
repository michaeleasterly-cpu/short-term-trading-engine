"""SP-G — ledger budget gate tests.

The gate is a pre-emission check that reads the SP-A cumulative ledger
and rejects when ``cumulative + expected_trials > quota``. The
Anthropic SDK is NEVER invoked on the rejected path.

Substrate seam: ``cumulative_n_trials`` is mocked here (the SP-A unit
tests already validate the actual ledger read against a real
``data_quality_log`` row). The gate itself is the unit under test.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tpcore.lab.llm_emitter import ledger_gate
from tpcore.lab.llm_emitter.ledger_gate import (
    EMISSION_QUOTA_PER_TARGET,
    LedgerBudgetExhausted,
    check_budget,
)


class _FakePool:
    """Stand-in for asyncpg.Pool — only consumed by the mocked
    ``cumulative_n_trials``; the gate never touches it directly."""


@pytest.fixture(autouse=True)
def _patch_cumulative(monkeypatch):
    """Each test sets ``return_value`` on the fake; default 0."""
    state: dict[str, Any] = {"value": 0, "calls": []}

    async def fake_cumulative(pool, target, before_ts):  # noqa: ANN001
        state["calls"].append((pool, target, before_ts))
        return state["value"]

    monkeypatch.setattr(ledger_gate, "cumulative_n_trials", fake_cumulative)
    return state


def test_emission_quota_default_is_20() -> None:
    """Q2 (operator-confirmed): default = 20 per target."""
    assert EMISSION_QUOTA_PER_TARGET == 20


async def test_check_budget_accepts_when_under_quota(_patch_cumulative) -> None:
    _patch_cumulative["value"] = 5
    pool = _FakePool()

    cumulative = await check_budget(
        pool, target="sentinel", expected_trials=10, quota=20
    )
    assert cumulative == 5


async def test_check_budget_accepts_at_exact_quota(_patch_cumulative) -> None:
    _patch_cumulative["value"] = 10
    cumulative = await check_budget(
        _FakePool(), target="sentinel", expected_trials=10, quota=20
    )
    assert cumulative == 10  # cumulative + expected == quota (still ok)


async def test_check_budget_rejects_when_over_quota(_patch_cumulative) -> None:
    _patch_cumulative["value"] = 15
    with pytest.raises(LedgerBudgetExhausted) as ei:
        await check_budget(
            _FakePool(), target="sentinel", expected_trials=10, quota=20
        )
    assert ei.value.target == "sentinel"
    assert ei.value.cumulative == 15
    assert ei.value.expected == 10
    assert ei.value.quota == 20


async def test_check_budget_rejects_when_already_at_quota(_patch_cumulative) -> None:
    """cumulative=20 + expected=1 > quota=20 → rejected."""
    _patch_cumulative["value"] = 20
    with pytest.raises(LedgerBudgetExhausted):
        await check_budget(
            _FakePool(), target="sentinel", expected_trials=1, quota=20
        )


async def test_check_budget_uses_provided_now(_patch_cumulative) -> None:
    """``now`` is the strict ``<`` boundary handed to ``cumulative_n_trials``
    (the SP-A read shape) — exposed for deterministic tests."""
    fixed = datetime(2026, 5, 20, 12, tzinfo=UTC)
    await check_budget(
        _FakePool(),
        target="sentinel",
        expected_trials=1,
        quota=20,
        now=fixed,
    )
    assert _patch_cumulative["calls"][0][2] == fixed


async def test_check_budget_uses_default_quota_when_unspecified(_patch_cumulative) -> None:
    _patch_cumulative["value"] = EMISSION_QUOTA_PER_TARGET  # at quota
    with pytest.raises(LedgerBudgetExhausted) as ei:
        await check_budget(_FakePool(), target="sentinel", expected_trials=1)
    assert ei.value.quota == EMISSION_QUOTA_PER_TARGET


async def test_check_budget_rejects_zero_expected_trials() -> None:
    with pytest.raises(ValueError):
        await check_budget(_FakePool(), target="sentinel", expected_trials=0)


async def test_check_budget_rejects_negative_quota() -> None:
    with pytest.raises(ValueError):
        await check_budget(
            _FakePool(), target="sentinel", expected_trials=1, quota=-1
        )


async def test_ledger_budget_exhausted_carries_structured_payload() -> None:
    e = LedgerBudgetExhausted(
        target="sentinel", cumulative=15, expected=10, quota=20
    )
    assert e.target == "sentinel"
    assert e.cumulative == 15
    assert e.expected == 10
    assert e.quota == 20
    assert "rate-limit fence" in str(e)
