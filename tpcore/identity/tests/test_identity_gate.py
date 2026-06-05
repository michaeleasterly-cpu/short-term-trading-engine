"""Tests for ``tpcore.identity.identity_gate`` (Plan 3 Phase 1.4).

Hermetic: a fake asyncpg pool returns scripted counts for the gate's
introspection queries. The gate is the BLOCKING substrate-consistency
check the coordinator runs AFTER the identity build and BEFORE any child
load — 0 NULL lifetime_start, 0 ticker_history overlaps, every cik-bearing
classification has an issuer + an issuer_securities link, 0 orphan
issuer_id.
"""
from __future__ import annotations

from typing import Any

import pytest

from tpcore.identity.identity_gate import (
    IdentityGateResult,
    evaluate_identity_gate,
)


class _FakePool:
    """Maps a substring-of-SQL → fetchval result for each gate query."""

    def __init__(self, answers: dict[str, int]) -> None:
        self._answers = answers
        self.queries: list[str] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.queries.append(sql)
        for needle, val in self._answers.items():
            if needle in sql:
                return val
        raise AssertionError(f"unexpected gate query: {sql[:80]!r}")


_CLEAN = {
    "lifetime_start IS NULL": 0,
    "ticker_history th1": 0,  # overlap probe
    "classification_id IS NULL": 0,  # ticker_history orphan probe
    "AS isec WHERE": 0,  # cik-bearing classification w/o issuer_securities
    "AS iss WHERE": 0,  # cik-bearing classification w/o issuers row
    "issuer_securities es": 0,  # orphan issuer_id in issuer_securities
    "issuer_history ih": 0,  # orphan issuer_id in issuer_history
}


@pytest.mark.asyncio
async def test_clean_substrate_passes() -> None:
    pool = _FakePool(dict(_CLEAN))
    result = await evaluate_identity_gate(pool)
    assert isinstance(result, IdentityGateResult)
    assert result.passed is True
    assert result.violations == {}


@pytest.mark.asyncio
async def test_null_lifetime_start_fails() -> None:
    answers = dict(_CLEAN)
    answers["lifetime_start IS NULL"] = 3
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["null_lifetime_start"] == 3


@pytest.mark.asyncio
async def test_ticker_history_overlap_fails() -> None:
    answers = dict(_CLEAN)
    answers["ticker_history th1"] = 2
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["ticker_history_overlaps"] == 2


@pytest.mark.asyncio
async def test_cik_classification_missing_issuer_fails() -> None:
    answers = dict(_CLEAN)
    answers["AS iss WHERE"] = 5
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["cik_classifications_without_issuer"] == 5


@pytest.mark.asyncio
async def test_cik_classification_missing_issuer_securities_fails() -> None:
    answers = dict(_CLEAN)
    answers["AS isec WHERE"] = 7
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["cik_classifications_without_issuer_securities"] == 7


@pytest.mark.asyncio
async def test_orphan_issuer_id_fails() -> None:
    answers = dict(_CLEAN)
    answers["issuer_securities es"] = 1
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["orphan_issuer_id_in_securities"] == 1


@pytest.mark.asyncio
async def test_assert_raises_on_violation() -> None:
    answers = dict(_CLEAN)
    answers["lifetime_start IS NULL"] = 1
    with pytest.raises(RuntimeError, match="identity gate"):
        await evaluate_identity_gate(_FakePool(answers), raise_on_fail=True)


@pytest.mark.asyncio
async def test_assert_passes_silently_when_clean() -> None:
    result = await evaluate_identity_gate(_FakePool(dict(_CLEAN)), raise_on_fail=True)
    assert result.passed is True
