"""Tests for ``tpcore.identity.identity_gate`` (Plan 3 Phase 1.4).

Hermetic: a fake asyncpg pool returns scripted counts for the gate's
introspection queries. The gate is the BLOCKING substrate-consistency
check the coordinator runs AFTER the identity build and BEFORE any child
load. It must catch EVERY rot class and never FALSE-POSITIVE on a
consistent substrate.

Probe coverage pinned here (one fail-test per probe, plus the clean pass):
  * sentinel ``lifetime_start = 1900-01-01`` survived (A6 no-sentinel);
  * a classification whose ``lifetime_start`` is AFTER a price bar already
    attributed to it (the too-late-start / out-of-window rot the Phase-5
    re-attribution cures — replaces the prior ``lifetime_start < FPFD`` probe,
    which false-positived on securities that legitimately traded before their
    first SEC filing);
  * ``ticker_history`` cross-classification overlap (NOT caught by the
    per-classification EXCLUDE — must be caught HERE);
  * ``issuer_history`` overlap (issuer_history has no EXCLUDE at all);
  * every cik-bearing classification has an issuer + an issuer_securities
    link;
  * 0 orphan ``classification_id`` in ticker_history / issuer_securities;
  * 0 orphan ``issuer_id`` in issuer_securities / issuer_history;
  * the cik-join is zfill-normalized so an unpadded FMP-fallback
    ``tc.cik`` that has a matching zfill-10 issuer is NOT a false orphan;
  * every etf/etn classification has an ``etf_attributes`` satellite row
    (migration 20260607_0100 physical-entity separation);
  * the ``etf_attributes`` satellite holds ONLY etf/etn attributes (a row
    pointing at a non-etf classification is a mis-scoped write).
"""
from __future__ import annotations

from typing import Any

import pytest

from tpcore.identity.identity_gate import (
    IdentityGateResult,
    evaluate_identity_gate,
)


class _FakePool:
    """Maps a substring-of-SQL → fetchval result for each gate query.

    Each needle must match EXACTLY ONE probe (the test asserts no probe is
    left unanswered + no needle matches two probes), so a vacuous /
    overlapping probe is caught structurally."""

    def __init__(self, answers: dict[str, int]) -> None:
        self._answers = answers
        self.queries: list[str] = []

    async def fetchval(self, sql: str, *args: Any) -> int:
        self.queries.append(sql)
        hits = [val for needle, val in self._answers.items() if needle in sql]
        if not hits:
            raise AssertionError(f"unexpected gate query: {sql[:120]!r}")
        if len(hits) > 1:
            raise AssertionError(
                f"ambiguous needle (matched {len(hits)} answers): {sql[:120]!r}"
            )
        return hits[0]


# Each needle uniquely identifies one probe's SQL.
_CLEAN = {
    "lifetime_start = DATE '1900-01-01'": 0,  # sentinel survivor (A6)
    "pd.date < tc.lifetime_start": 0,  # too-late lifetime_start (out-of-window)
    "ticker_history th1": 0,  # cross-classification overlap probe
    "issuer_history ih1": 0,  # issuer_history overlap probe
    "th.classification_id": 0,  # ticker_history → classification orphan
    "isec.classification_id": 0,  # issuer_securities → classification orphan
    "AS isec_link WHERE": 0,  # cik-bearing classification w/o issuer_securities
    "AS iss_exists WHERE": 0,  # cik-bearing classification w/o issuers row
    "issuer_securities es": 0,  # orphan issuer_id in issuer_securities
    "issuer_history ih_orphan": 0,  # orphan issuer_id in issuer_history
    "etf_attributes ea\n        WHERE": 0,  # etf w/o satellite (NOT EXISTS)
    "WHERE tc.asset_class NOT IN ('etf', 'etn')": 0,  # non-etf in satellite
}


@pytest.mark.asyncio
async def test_clean_substrate_passes() -> None:
    pool = _FakePool(dict(_CLEAN))
    result = await evaluate_identity_gate(pool)
    assert isinstance(result, IdentityGateResult)
    assert result.passed is True
    assert result.violations == {}


@pytest.mark.asyncio
async def test_every_probe_is_queried() -> None:
    """Every needle in _CLEAN must be hit — proves no probe was dropped and
    each probe's SQL is distinct (a vacuous probe would either fail to match
    or collide with another, both AssertionErrors in _FakePool)."""
    pool = _FakePool(dict(_CLEAN))
    await evaluate_identity_gate(pool)
    joined = "\n".join(pool.queries)
    for needle in _CLEAN:
        assert needle in joined, needle


@pytest.mark.asyncio
async def test_sentinel_lifetime_start_fails() -> None:
    answers = dict(_CLEAN)
    answers["lifetime_start = DATE '1900-01-01'"] = 4
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["sentinel_lifetime_start"] == 4


@pytest.mark.asyncio
async def test_lifetime_start_after_earliest_bar_fails() -> None:
    answers = dict(_CLEAN)
    answers["pd.date < tc.lifetime_start"] = 6
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["lifetime_start_after_earliest_bar"] == 6


@pytest.mark.asyncio
async def test_ticker_history_overlap_fails() -> None:
    answers = dict(_CLEAN)
    answers["ticker_history th1"] = 2
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["ticker_history_overlaps"] == 2


@pytest.mark.asyncio
async def test_issuer_history_overlap_fails() -> None:
    answers = dict(_CLEAN)
    answers["issuer_history ih1"] = 3
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["issuer_history_overlaps"] == 3


@pytest.mark.asyncio
async def test_ticker_history_orphan_classification_fails() -> None:
    answers = dict(_CLEAN)
    answers["th.classification_id"] = 1
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["orphan_classification_in_ticker_history"] == 1


@pytest.mark.asyncio
async def test_issuer_securities_orphan_classification_fails() -> None:
    answers = dict(_CLEAN)
    answers["isec.classification_id"] = 2
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert (
        result.violations["orphan_classification_in_issuer_securities"] == 2
    )


@pytest.mark.asyncio
async def test_cik_classification_missing_issuer_fails() -> None:
    answers = dict(_CLEAN)
    answers["AS iss_exists WHERE"] = 5
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["cik_classifications_without_issuer"] == 5


@pytest.mark.asyncio
async def test_cik_classification_missing_issuer_securities_fails() -> None:
    answers = dict(_CLEAN)
    answers["AS isec_link WHERE"] = 7
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["cik_classifications_without_issuer_securities"] == 7


@pytest.mark.asyncio
async def test_orphan_issuer_id_in_securities_fails() -> None:
    answers = dict(_CLEAN)
    answers["issuer_securities es"] = 1
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["orphan_issuer_id_in_securities"] == 1


@pytest.mark.asyncio
async def test_orphan_issuer_id_in_history_fails() -> None:
    answers = dict(_CLEAN)
    answers["issuer_history ih_orphan"] = 1
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["orphan_issuer_id_in_history"] == 1


@pytest.mark.asyncio
async def test_etf_without_attributes_fails() -> None:
    answers = dict(_CLEAN)
    answers["etf_attributes ea\n        WHERE"] = 12
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["etf_without_attributes"] == 12


@pytest.mark.asyncio
async def test_etf_attributes_non_etf_fails() -> None:
    answers = dict(_CLEAN)
    answers["WHERE tc.asset_class NOT IN ('etf', 'etn')"] = 3
    result = await evaluate_identity_gate(_FakePool(answers))
    assert result.passed is False
    assert result.violations["etf_attributes_non_etf"] == 3


def test_cik_join_is_zfill_normalized_both_sides() -> None:
    """The cik-join must normalize BOTH ``issuers.cik`` and
    ``ticker_classifications.cik`` so an FMP-fallback unpadded ``tc.cik``
    that has a matching zfill-10 issuer is NOT reported as a false orphan
    (the FMP writer at scripts/ops.py SET cik='fmp' may land an unpadded
    value). We assert on the SQL text: the normalization predicate appears
    on both the issuers side and the tc side of the NOT-EXISTS join."""
    from tpcore.identity import identity_gate

    sql = identity_gate._CLASSIFICATION_WITHOUT_ISSUER_SQL
    norm = "lpad(regexp_replace("
    # Normalized on both sides of the equality.
    assert sql.count(norm) >= 2, sql
    assert "iss_exists.cik" in sql and "tc.cik" in sql


@pytest.mark.asyncio
async def test_assert_raises_on_violation() -> None:
    answers = dict(_CLEAN)
    answers["lifetime_start = DATE '1900-01-01'"] = 1
    with pytest.raises(RuntimeError, match="identity gate"):
        await evaluate_identity_gate(_FakePool(answers), raise_on_fail=True)


@pytest.mark.asyncio
async def test_assert_passes_silently_when_clean() -> None:
    result = await evaluate_identity_gate(_FakePool(dict(_CLEAN)), raise_on_fail=True)
    assert result.passed is True
