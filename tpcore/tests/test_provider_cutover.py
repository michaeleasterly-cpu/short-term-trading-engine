"""Phase 5 — cutover transition-guard tests.

`plan_cutover` is pure: it validates a proposed snap-in and returns the
exact legal status changes, or an honest block reason. It never mutates
the frozen SoT and never trades — applying a plan is the operator's
reviewed PR (spec: structural, like engine archival).
"""
from __future__ import annotations

import pytest

from tpcore import providers
from tpcore.providers import (
    ProviderBinding,
    ProviderStatus,
    plan_cutover,
)

# ── Blocked paths against the LIVE registry (honest real cases) ──────


def test_unknown_feed_blocked() -> None:
    p = plan_cutover("no_such_feed", "whoever")
    assert not p.allowed and "unknown feed" in p.block_reason


def test_unknown_provider_blocked() -> None:
    p = plan_cutover("prices_daily", "not_a_provider")
    assert not p.allowed and "not a bound provider" in p.block_reason


def test_already_active_blocked() -> None:
    p = plan_cutover("prices_daily", "alpaca")  # alpaca is ACTIVE
    assert not p.allowed and "already ACTIVE" in p.block_reason


def test_candidate_not_cutover_eligible() -> None:
    """eco_archive is a real CANDIDATE — must NOT be cutover-eligible
    (skipping EVALUATE/parity is the silent-degradation class)."""
    p = plan_cutover("macro_indicators", "eco_archive")
    assert not p.allowed
    assert "only a FALLBACK" in p.block_reason
    assert "EVALUATE" in p.block_reason


# ── Allowed path — synthetic FALLBACK (none exist in the live SoT) ───


@pytest.fixture
def _two_provider_feed(monkeypatch) -> str:
    from datetime import date

    feed = "synthetic_feed"
    bindings = [
        ProviderBinding(
            feed=feed, provider="incumbent",
            adapter_module="tpcore.providers",
            status=ProviderStatus.ACTIVE, evidence="incumbent",
        ),
        ProviderBinding(
            feed=feed, provider="challenger",
            adapter_module="tpcore.providers",
            status=ProviderStatus.FALLBACK, evidence="parity-verified",
            parity_verified_at=date(2026, 5, 17),
        ),
    ]
    patched = dict(providers.PROVIDER_BINDINGS)
    patched[feed] = bindings
    monkeypatch.setattr(providers, "PROVIDER_BINDINGS", patched)
    return feed


def test_fallback_promotion_demotes_incumbent_to_fallback(_two_provider_feed) -> None:
    p = plan_cutover(_two_provider_feed, "challenger")
    assert p.allowed
    ch = {c.provider: c for c in p.changes}
    assert ch["challenger"].from_status is ProviderStatus.FALLBACK
    assert ch["challenger"].to_status is ProviderStatus.ACTIVE
    assert ch["incumbent"].from_status is ProviderStatus.ACTIVE
    assert ch["incumbent"].to_status is ProviderStatus.FALLBACK  # reversible
    # exactly-one-ACTIVE preserved by the plan.
    actives = [c for c in p.changes if c.to_status is ProviderStatus.ACTIVE]
    assert len(actives) == 1


def test_retire_incumbent_flag_retires_old_active(_two_provider_feed) -> None:
    p = plan_cutover(_two_provider_feed, "challenger", retire_incumbent=True)
    assert p.allowed
    ch = {c.provider: c for c in p.changes}
    assert ch["incumbent"].to_status is ProviderStatus.RETIRED
    assert "RETIRED" in p.summary or "retired" in p.summary
