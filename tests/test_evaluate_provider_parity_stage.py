"""F0 (2026-06-01) — ``_stage_evaluate_provider_parity`` tests.

Hermetic — uses the stage's ``incumbent_samples`` / ``candidate_samples``
test-hook to bypass the live dual-pull. Asserts:

  * the verdict produced by ``compare_provider_parity()`` flows through
    to the stage's return payload (PASS / FAIL / NOT_EVALUABLE) and
    drives the operator-facing ``next_action`` string,
  * ``dry_run=True`` (the default) NEVER writes to the DB,
  * ``dry_run=False`` writes exactly one row to
    ``platform.data_quality_log`` AND one event to
    ``platform.application_log``,
  * ``data_quality_log.source`` = ``evaluate.{feed}.{candidate}`` (the
    string the cutover_agent's freshness gate keys on),
  * the stage is discoverable via ``_STAGE_SPECS``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_pool() -> MagicMock:
    """asyncpg.Pool stub recording every ``execute`` / ``fetchrow``
    call so the tests can assert what was (or wasn't) persisted."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.conn_for_assertions = conn
    return pool


def _samples(*, day_offset: int, n: int = 30, base: float = 100.0,
             noise: float = 0.0) -> list:
    """Build ``n`` synthetic ParitySample rows."""
    from tpcore.parity import ParitySample
    today = datetime.now(UTC).date()
    return [
        ParitySample(
            key=f"AAPL|{(today - timedelta(days=day_offset + i)).isoformat()}",
            asof=today - timedelta(days=day_offset + i),
            value=base + i * 0.01 + noise,
        )
        for i in range(n)
    ]


@pytest.fixture
def stub_active_provider(monkeypatch):
    """Force ``tpcore.providers.active_provider`` to return a synthetic
    incumbent binding so the stage can resolve a feed without the live
    PROVIDER_BINDINGS registry."""
    from tpcore.providers import ProviderBinding, ProviderStatus

    def _stub(feed: str):
        if feed == "daily_bars":
            return ProviderBinding(
                feed="daily_bars",
                provider="incumbent_test",
                adapter_module="tpcore.providers",
                status=ProviderStatus.ACTIVE,
                evidence="synthetic test fixture",
            )
        return None

    monkeypatch.setattr("tpcore.providers.active_provider", _stub)
    return _stub


# ─── TEST-F0-A — PASS persists to data_quality_log + application_log
# ─── (the dispositive happy path)


@pytest.mark.asyncio
async def test_evaluate_pass_persists_data_quality_log(
    stub_active_provider,
) -> None:
    from scripts.ops import _stage_evaluate_provider_parity

    # Identical samples → PASS at PRICE tolerance.
    inc = _samples(day_offset=0, n=30)
    cand = _samples(day_offset=0, n=30)

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",
        "candidate": "challenger_test",
        "dry_run": False,
        "incumbent_samples": inc,
        "candidate_samples": cand,
        "test_feed_class": "price",
    })

    assert result["verdict"] == "pass"
    assert result["data_quality_log_written"] is True
    assert result["application_log_written"] is True
    assert "DFCR" in result["next_action"]
    assert result["coverage_ratio"] == 1.0

    # Inspect the persisted rows.
    conn = pool.conn_for_assertions
    # 2 execute calls: data_quality_log + application_log.
    assert conn.execute.await_count == 2
    dq_call = conn.execute.await_args_list[0]
    app_call = conn.execute.await_args_list[1]
    assert "data_quality_log" in dq_call.args[0]
    assert "application_log" in app_call.args[0]
    # source = "evaluate.daily_bars.challenger_test"
    assert dq_call.args[1] == "evaluate.daily_bars.challenger_test"
    # PASS verdict → confidence = 1.0.
    assert dq_call.args[2] == 1.0
    # application_log event_type.
    assert app_call.args[3] == "PROVIDER_PARITY_EVALUATED"


# ─── TEST-F0-B — FAIL on coverage gap


@pytest.mark.asyncio
async def test_evaluate_fail_on_coverage_gap(
    stub_active_provider,
) -> None:
    """PRICE has ``coverage_min_ratio=1.0`` — even one missing key is
    a coverage failure. Mimics the audit's 506/7650 silent-shrinkage
    class."""
    from scripts.ops import _stage_evaluate_provider_parity

    inc = _samples(day_offset=0, n=30)
    # Candidate covers only 20 of 30 incumbent keys → coverage 0.67.
    cand = _samples(day_offset=0, n=20)

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",
        "candidate": "shrinkage_test",
        "dry_run": False,
        "incumbent_samples": inc,
        "candidate_samples": cand,
        "test_feed_class": "price",
    })

    assert result["verdict"] == "fail"
    assert "coverage" in result["evidence"].lower()
    assert "BLOCK" in result["next_action"] or "block" in result["next_action"]


# ─── TEST-F0-C — FAIL on value drift


@pytest.mark.asyncio
async def test_evaluate_fail_on_value_drift(stub_active_provider) -> None:
    """PRICE has ``value_rel_tol=1e-4`` — a 1% drift is well above
    tolerance → accuracy FAIL."""
    from scripts.ops import _stage_evaluate_provider_parity

    inc = _samples(day_offset=0, n=30, base=100.0)
    cand = _samples(day_offset=0, n=30, base=100.0, noise=1.0)  # +1.0 ≈ 1% off

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",
        "candidate": "drift_test",
        "dry_run": False,
        "incumbent_samples": inc,
        "candidate_samples": cand,
        "test_feed_class": "price",
    })

    assert result["verdict"] == "fail"
    assert "accuracy" in result["evidence"].lower()


# ─── TEST-F0-D — FILING presence-only parity passes without values


@pytest.mark.asyncio
async def test_evaluate_filing_presence_only_passes_without_values(
    stub_active_provider,
) -> None:
    """FILING has ``value_rel_tol=None`` (presence-only). Coverage at
    ≥95% with no value comparison → PASS even when values differ."""
    from scripts.ops import _stage_evaluate_provider_parity
    from tpcore.parity import ParitySample

    today = datetime.now(UTC).date()
    inc = [
        ParitySample(key=f"K{i}", asof=today - timedelta(days=i),
                     value=float(i))
        for i in range(20)
    ]
    cand = [
        ParitySample(key=f"K{i}", asof=today - timedelta(days=i),
                     value=float(i * 1000))  # ridiculous value drift — IGNORED
        for i in range(20)  # full coverage
    ]

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",  # feed name not class-dispatched in this branch
        "candidate": "filing_test",
        "dry_run": False,
        "incumbent_samples": inc,
        "candidate_samples": cand,
        "test_feed_class": "filing",
    })

    assert result["verdict"] == "pass"
    # accuracy_ratio is None for presence-only.
    assert result["accuracy_ratio"] is None


# ─── TEST-F0-E — DERIVED feed always NOT_EVALUABLE


@pytest.mark.asyncio
async def test_evaluate_derived_feed_not_evaluable(
    stub_active_provider,
) -> None:
    """DERIVED feeds have no external provider — always NOT_EVALUABLE.
    Honest non-verdict, NOT a silent pass."""
    from scripts.ops import _stage_evaluate_provider_parity

    inc = _samples(day_offset=0, n=10)
    cand = _samples(day_offset=0, n=10)

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",
        "candidate": "derived_test",
        "dry_run": False,
        "incumbent_samples": inc,
        "candidate_samples": cand,
        "test_feed_class": "derived",
    })

    assert result["verdict"] == "not_evaluable"
    assert "derived" in result["evidence"].lower()


# ─── TEST-F0-F — dry_run persists nothing


@pytest.mark.asyncio
async def test_evaluate_dry_run_persists_nothing(
    stub_active_provider,
) -> None:
    """Operator hard rule: ``dry_run=True`` (the default) MUST NOT
    write to the DB. The stage prints the verdict, no rows persisted."""
    from scripts.ops import _stage_evaluate_provider_parity

    inc = _samples(day_offset=0, n=30)
    cand = _samples(day_offset=0, n=30)

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",
        "candidate": "challenger_test",
        # dry_run defaults True; not passing it is the same as True.
        "incumbent_samples": inc,
        "candidate_samples": cand,
        "test_feed_class": "price",
    })

    assert result["verdict"] == "pass"
    assert result["dry_run"] is True
    assert result["data_quality_log_written"] is False
    assert result["application_log_written"] is False

    conn = pool.conn_for_assertions
    # NO execute calls in dry_run mode.
    assert conn.execute.await_count == 0


# ─── TEST-F0-G — stage registered


def test_evaluate_stage_registered_in_stage_specs() -> None:
    """The stage must be discoverable via _STAGE_SPECS — without this
    sentinel, ``python scripts/ops.py --stage
    evaluate_provider_parity`` 404s at the CLI."""
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}
    assert "evaluate_provider_parity" in names


# ─── TEST-F0-H — same incumbent + candidate (defensive) → NOT_EVALUABLE


@pytest.mark.asyncio
async def test_evaluate_candidate_is_incumbent_not_evaluable(
    stub_active_provider,
) -> None:
    """Operator shouldn't be able to ``evaluate`` a provider against
    itself — defend against an obvious copy-paste error in the
    --param flags by returning NOT_EVALUABLE early."""
    from scripts.ops import _stage_evaluate_provider_parity

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "daily_bars",
        "candidate": "incumbent_test",  # same as the stubbed incumbent
        "dry_run": False,
    })

    assert result["verdict"] == "not_evaluable"
    assert "already the ACTIVE" in result["evidence"]


# ─── TEST-F0-I — unsupported feed → NOT_EVALUABLE with helpful message


@pytest.mark.asyncio
async def test_evaluate_unsupported_feed_blocks_with_message(
    stub_active_provider, monkeypatch,
) -> None:
    """Feeds not in ``_FEED_PARITY_DISPATCH`` yet (today: everything
    except daily_bars) should surface a clear NOT_EVALUABLE verdict
    naming the missing dispatch entry — never a silent pass and never
    a crash."""
    from tpcore.providers import ProviderBinding, ProviderStatus

    def _stub(feed: str):
        return ProviderBinding(
            feed=feed, provider="incumbent_test",
            adapter_module="tpcore.providers",
            status=ProviderStatus.ACTIVE,
            evidence="synthetic test fixture",
        )

    monkeypatch.setattr("tpcore.providers.active_provider", _stub)
    from scripts.ops import _stage_evaluate_provider_parity

    pool = _mock_pool()
    result = await _stage_evaluate_provider_parity(pool, {
        "feed": "fundamentals_quarterly",  # not in dispatch today
        "candidate": "challenger_test",
        "dry_run": False,
    })

    assert result["verdict"] == "not_evaluable"
    assert (
        "_FEED_PARITY_DISPATCH" in result["evidence"]
        or "fundamentals_quarterly" in result["evidence"]
    )
