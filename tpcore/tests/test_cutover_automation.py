"""Automated CUTOVER — overlay resolver + apply + the deterministic pass.

Deterministic, no DB: a fake pool over an in-memory
provider_binding_state + application_log + a red-check set.
``ops.cutover_agent`` is loaded by path to dodge the known
scripts/ops.py ↔ ops/ package shadowing in the pytest session.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

from tpcore import providers
from tpcore.providers import (
    ProviderBinding,
    ProviderStatus,
    apply_cutover,
    plan_cutover,
    resolve_active_provider,
)

_SPEC = importlib.util.spec_from_file_location(
    "_cutover_agent_under_test",
    Path(__file__).resolve().parents[2] / "ops" / "cutover_agent.py",
)
ca = importlib.util.module_from_spec(_SPEC)
sys.modules["_cutover_agent_under_test"] = ca
_SPEC.loader.exec_module(ca)


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    def __init__(self, s: _Store) -> None:
        self._s = s

    async def fetchrow(self, sql: str, *a):
        if "provider_binding_state" in sql and "SELECT" in sql:
            ap = self._s.state.get(a[0])
            return {"active_provider": ap} if ap else None
        return None

    async def fetch(self, sql: str, *a):
        if "data_quality_log" in sql:
            return [{"source": f"validation.{c}"} for c in self._s.red_checks]
        return []

    async def execute(self, sql: str, *a):
        if "INSERT INTO platform.provider_binding_state" in sql:
            self._s.state[a[0]] = a[1]          # feed → active_provider
        elif "INSERT INTO platform.application_log" in sql:
            self._s.applog.append({"event_type": a[2], "message": a[4]})


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Store:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.applog: list[dict] = []
        self.red_checks: set[str] = set()

    def acquire(self):
        return _CM(_Conn(self))


# ── plan_cutover (pure guard) ───────────────────────────────────────


def test_plan_blocks_candidate_real_case() -> None:
    # eco_archive is a real CANDIDATE — not cutover-eligible.
    p = plan_cutover("macro_indicators", "eco_archive")
    assert not p.allowed and "only a FALLBACK" in p.block_reason


def test_plan_blocks_unknown_and_already_active() -> None:
    assert not plan_cutover("nope", "x").allowed
    # ``prices_daily`` ACTIVE provider became fmp in the P0_4 DFCR
    # realignment (2026-05-25). Cutover plan to the current ACTIVE
    # must still be blocked.
    assert not plan_cutover("prices_daily", "fmp").allowed


# ── overlay resolver ────────────────────────────────────────────────


async def test_resolve_uses_code_default_when_no_overlay() -> None:
    s = _Store()
    b = await resolve_active_provider(s, "prices_daily")
    # Code-declared ACTIVE for prices_daily switched alpaca→fmp in
    # the P0_4 DFCR realignment (2026-05-25); alpaca demoted to
    # DEPRECATED.
    assert b is not None and b.provider == "fmp"


async def test_resolve_uses_overlay_when_present(monkeypatch) -> None:
    feed = "synthetic"
    patched = dict(providers.PROVIDER_BINDINGS)
    patched[feed] = [
        ProviderBinding(feed=feed, provider="inc",
                        adapter_module="tpcore.providers",
                        status=ProviderStatus.ACTIVE, evidence="x"),
        ProviderBinding(feed=feed, provider="cand",
                        adapter_module="tpcore.providers",
                        status=ProviderStatus.FALLBACK, evidence="y",
                        parity_verified_at=date(2026, 5, 17)),
    ]
    monkeypatch.setattr(providers, "PROVIDER_BINDINGS", patched)
    s = _Store()
    s.state[feed] = "cand"  # overlay flipped to the fallback
    b = await resolve_active_provider(s, feed)
    assert b is not None and b.provider == "cand"


async def test_apply_cutover_writes_overlay_and_audits_and_blocks_bad() -> None:
    s = _Store()
    # ``prices_daily`` ACTIVE became fmp post-P0_4 (alpaca is now
    # DEPRECATED) — plan_cutover to the ACTIVE incumbent is blocked.
    bad = plan_cutover("prices_daily", "fmp")  # blocked plan: already ACTIVE
    with pytest.raises(ValueError, match="blocked cutover"):
        await apply_cutover(s, bad)
    # A synthetic allowed plan applies + audits.
    plan = providers.CutoverPlan(
        feed="f", to_provider="p", allowed=True,
        changes=(providers.CutoverChange(
            provider="p", from_status=ProviderStatus.FALLBACK,
            to_status=ProviderStatus.ACTIVE),))
    await apply_cutover(s, plan)
    assert s.state["f"] == "p"
    assert any(e["event_type"] == "PROVIDER_CUTOVER" for e in s.applog)


# ── the deterministic pass ──────────────────────────────────────────


@pytest.fixture
def _feed_with_fallback(monkeypatch) -> str:
    feed = "synthfeed"
    patched = dict(providers.PROVIDER_BINDINGS)
    patched[feed] = [
        ProviderBinding(feed=feed, provider="inc",
                        adapter_module="tpcore.providers",
                        status=ProviderStatus.ACTIVE, evidence="x"),
        ProviderBinding(feed=feed, provider="fb",
                        adapter_module="tpcore.providers",
                        status=ProviderStatus.FALLBACK, evidence="parity ok",
                        parity_verified_at=date(2026, 5, 17)),
    ]
    monkeypatch.setattr(providers, "PROVIDER_BINDINGS", patched)
    # The agent maps red check→feed via HEAL_SPECS; inject one.
    from tpcore.selfheal.spec import HealSpec
    hs = dict(ca.HEAL_SPECS)
    hs["synth_check"] = HealSpec(
        check_name="synth_check", source=feed, healable=True,
        stage="daily_bars", params={})
    monkeypatch.setattr(ca, "HEAL_SPECS", hs)
    return feed


async def test_pass_dormant_when_red_but_no_fallback() -> None:
    s = _Store()
    s.red_checks = {"prices_daily_freshness"}  # red, no FALLBACK exists
    r = await ca.run_cutover_pass(s)
    assert r.cutovers == [] and "prices_daily" in r.dormant_feeds


async def test_pass_cuts_over_when_fallback_exists(_feed_with_fallback) -> None:
    s = _Store()
    s.red_checks = {"synth_check"}
    r = await ca.run_cutover_pass(s)
    assert r.cutovers == [f"{_feed_with_fallback}→fb"]
    assert s.state[_feed_with_fallback] == "fb"
    # Idempotent: a second pass sees it already on the fallback.
    r2 = await ca.run_cutover_pass(s)
    assert r2.cutovers == [] and _feed_with_fallback in r2.already_on_fallback
