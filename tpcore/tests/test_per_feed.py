"""Unit tests for the per-feed validate + bounded self-heal helpers
(#185 Phase 1 dark helpers + Phase 2 on-completion hook).

Pure: the canonical check fn and the HealSpec are injected via
monkeypatch, ``run_stage`` is a fake recorder, the pool is an inert
sentinel — no DB, no subprocess. Mirrors test_selfheal.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tpcore.feeds.dispatcher import FEED_STAGE
from tpcore.quality.validation.suite import KNOWN_CHECK_NAMES
from tpcore.selfheal import per_feed
from tpcore.selfheal.per_feed import (
    feed_checks,
    heal_one,
    is_leaf_feed,
    on_stage_complete,
    validate_and_heal_feed,
    validate_feed,
    validate_one,
)
from tpcore.selfheal.registry import HEAL_SPECS
from tpcore.selfheal.spec import HealSpec

_POOL = object()


def _cr(passed: bool) -> SimpleNamespace:
    return SimpleNamespace(passed=passed)


def _spec(*, healable: bool = True, max_attempts: int = 2) -> HealSpec:
    return HealSpec(
        check_name="X",
        source="prices_daily",
        healable=healable,
        stage="daily_bars",
        params={"k": "v"},
        max_attempts=max_attempts,
        unhealable_reason="" if healable else "permanent reconcile failure",
        depends_on=(),
    )


def _patch_feed(monkeypatch, feed: str, overrides: dict) -> None:
    """Patch every canonical check fn of ``feed``; unspecified → green."""
    async def _ok(pool):
        return _cr(True)

    for cn in feed_checks(feed):
        monkeypatch.setitem(per_feed._CHECK_FN, cn, overrides.get(cn, _ok))


def _rs(*, fail_rc: int = 0):
    calls: list[tuple[str, dict]] = []

    async def run_stage(stage: str, params: dict) -> int:
        calls.append((stage, dict(params)))
        return fail_rc

    run_stage.calls = calls  # type: ignore[attr-defined]
    return run_stage


# --- drift guard (clockwork: a new check fails the build) -------------

def test_check_registry_no_drift() -> None:
    assert set(per_feed._CHECK_FN) == set(KNOWN_CHECK_NAMES)
    assert set(per_feed._CHECK_FN) == set(HEAL_SPECS)


def test_feed_checks_resolves_via_healspec_source() -> None:
    expected = sorted(
        c for c, s in HEAL_SPECS.items() if s.source == "prices_daily"
    )
    assert feed_checks("prices_daily") == expected
    assert "prices_daily_completeness" in feed_checks("prices_daily")
    assert feed_checks("not_a_feed") == []


# --- validate_one / validate_feed -------------------------------------

async def test_validate_one_green_then_red(monkeypatch) -> None:
    async def ok(pool):
        return _cr(True)

    async def bad(pool):
        return _cr(False)

    monkeypatch.setitem(per_feed._CHECK_FN, "row_integrity", ok)
    assert (await validate_one(_POOL, "row_integrity")).passed
    monkeypatch.setitem(per_feed._CHECK_FN, "row_integrity", bad)
    assert not (await validate_one(_POOL, "row_integrity")).passed


async def test_validate_one_unknown_raises() -> None:
    with pytest.raises(KeyError):
        await validate_one(_POOL, "no_such_check")


async def test_validate_feed_aggregates_reds(monkeypatch) -> None:
    async def bad(pool):
        return _cr(False)

    _patch_feed(monkeypatch, "prices_daily", {"prices_daily_completeness": bad})
    all_ok, red = await validate_feed(_POOL, "prices_daily")
    assert all_ok is False
    assert red == ["prices_daily_completeness"]


# --- heal_one ---------------------------------------------------------

async def test_heal_one_unknown_spec(monkeypatch) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: None)
    r = await heal_one(_POOL, "X", _rs())
    assert not r.healed and r.attempts == 0
    assert "no HealSpec" in (r.escalated_reason or "")


async def test_heal_one_unhealable_escalates_without_repair(
    monkeypatch,
) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: _spec(healable=False))
    rs = _rs()
    r = await heal_one(_POOL, "X", rs)
    assert not r.healed and r.attempts == 0
    assert "permanent reconcile failure" in (r.escalated_reason or "")
    assert rs.calls == []  # never attempted a repair


async def test_heal_one_heals_first_attempt(monkeypatch) -> None:
    sp = _spec(max_attempts=3)
    monkeypatch.setattr(per_feed, "spec_for", lambda c: sp)
    rs = _rs()

    async def chk(pool):
        return _cr(len(rs.calls) >= 1)  # green only after the repair ran

    monkeypatch.setitem(per_feed._CHECK_FN, "X", chk)
    r = await heal_one(_POOL, "X", rs)
    assert r.healed and r.attempts == 1
    assert rs.calls == [("daily_bars", {"k": "v"})]


async def test_heal_one_failed_repair_escalates(monkeypatch) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: _spec())

    async def chk(pool):
        return _cr(False)

    monkeypatch.setitem(per_feed._CHECK_FN, "X", chk)
    r = await heal_one(_POOL, "X", _rs(fail_rc=2))
    assert not r.healed and r.attempts == 1
    assert "exited 2" in (r.escalated_reason or "")


async def test_heal_one_exhausts_bounded(monkeypatch) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: _spec(max_attempts=2))

    async def chk(pool):
        return _cr(False)  # repair "succeeds" but never clears

    monkeypatch.setitem(per_feed._CHECK_FN, "X", chk)
    rs = _rs()
    r = await heal_one(_POOL, "X", rs)
    assert not r.healed and r.attempts == 2
    assert "after 2 bounded repair attempts" in (r.escalated_reason or "")
    assert len(rs.calls) == 2  # bounded, did not loop forever


# --- validate_and_heal_feed (the dark on-completion unit) -------------

async def test_vahf_green_is_noop(monkeypatch) -> None:
    _patch_feed(monkeypatch, "prices_daily", {})
    rs = _rs()
    out = await validate_and_heal_feed(_POOL, "prices_daily", rs)
    assert out.green and out.healed == [] and out.escalated == []
    assert rs.calls == []  # idempotent: green feed never repairs


async def test_vahf_red_heals_to_green(monkeypatch) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: _spec())
    rs = _rs()

    async def comp(pool):
        return _cr(len(rs.calls) >= 1)

    _patch_feed(monkeypatch, "prices_daily", {"prices_daily_completeness": comp})
    out = await validate_and_heal_feed(_POOL, "prices_daily", rs)
    assert out.green
    assert out.healed == ["prices_daily_completeness"]
    assert out.escalated == []


async def test_vahf_unhealable_escalates_honestly(monkeypatch) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: _spec(healable=False))

    async def bad(pool):
        return _cr(False)

    _patch_feed(monkeypatch, "prices_daily", {"prices_daily_completeness": bad})
    out = await validate_and_heal_feed(_POOL, "prices_daily", _rs())
    assert not out.green and out.healed == []
    assert out.escalated == [
        ("prices_daily_completeness", "permanent reconcile failure")
    ]


# --- Phase 2: stage→feed resolution + on_stage_complete --------------

def test_stage_feed_coverage_no_drift() -> None:
    # _STAGE_FEED is exactly the reverse of the FEED_STAGE SoT, and
    # every mapped feed resolves to >=1 canonical check (clockwork: a
    # misaligned new feed/stage fails the build, not silently no-ops).
    assert per_feed._STAGE_FEED == {s: f for f, s in FEED_STAGE.items()}
    for stage, feed in per_feed._STAGE_FEED.items():
        assert feed_checks(feed), f"{stage}->{feed} resolves to no check"


def test_is_leaf_feed_split() -> None:
    assert is_leaf_feed("prices_daily") is True
    assert is_leaf_feed("fear_greed") is False  # derived (depends_on)


async def test_on_stage_complete_infra_stage_is_noop() -> None:
    # data_validation/forensics are not in the feed map → no-op.
    assert await on_stage_complete(_POOL, "data_validation", "rid") is None


async def test_on_stage_complete_derived_feed_deferred() -> None:
    # fear_greed is derived → Phase 3 territory, not validated here.
    assert await on_stage_complete(_POOL, "fear_greed", "rid") is None


async def test_on_stage_complete_leaf_green(monkeypatch) -> None:
    _patch_feed(monkeypatch, "prices_daily", {})

    async def _never(stage, params):  # invoking it = shelled out on green
        raise AssertionError("repair runner invoked but feed was green")

    monkeypatch.setattr(
        per_feed, "make_canonical_runner", lambda run_id: _never
    )
    out = await on_stage_complete(_POOL, "daily_bars", "rid")
    assert out is not None and out.green and out.feed == "prices_daily"
    assert out.healed == [] and out.escalated == []


async def test_on_stage_complete_leaf_red_heals(monkeypatch) -> None:
    monkeypatch.setattr(per_feed, "spec_for", lambda c: _spec())
    rs = _rs()

    async def comp(pool):
        return _cr(len(rs.calls) >= 1)

    _patch_feed(monkeypatch, "prices_daily", {"prices_daily_completeness": comp})
    monkeypatch.setattr(per_feed, "make_canonical_runner", lambda run_id: rs)
    out = await on_stage_complete(_POOL, "daily_bars", "rid")
    assert out is not None and out.green
    assert out.healed == ["prices_daily_completeness"]
    assert rs.calls == [("daily_bars", {"k": "v"})]
