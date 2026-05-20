import contextlib
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from tpcore.engine_profile import (
    _PROFILE,
    Cadence,
    EngineProfile,
    FireDecision,
    LifecycleState,
    _cadence_boundary,
    _cadence_window_start,
    profile_for,
    should_fire,
)


def test_profile_for_known_engines():
    assert profile_for("reversion").cadence is Cadence.DAILY
    assert profile_for("vector").cadence is Cadence.DAILY
    assert profile_for("sentinel").cadence is Cadence.DAILY
    assert profile_for("momentum").cadence is Cadence.MONTHLY_FIRST_TRADING_DAY
    assert profile_for("allocator").cadence is Cadence.WEEKLY_FIRST_TRADING_DAY
    assert profile_for("canary").cadence is Cadence.DAILY


def test_profile_for_unknown_returns_none():
    assert profile_for("does_not_exist") is None


def test_profiles_are_frozen_and_self_consistent():
    for name, p in _PROFILE.items():
        assert isinstance(p, EngineProfile)
        assert p.engine == name
        with pytest.raises(ValidationError):
            p.cadence = Cadence.DAILY  # frozen


def test_profile_covers_live_engine_roster():
    # SoT: tpcore.engine_profile._PROFILE (sigma RETIRED, excluded from live)
    live = {"reversion", "vector", "momentum", "sentinel", "canary"}
    missing = live - set(_PROFILE)
    assert not missing, f"engines without an EngineProfile: {missing}"


def test_daily_boundary_true_on_trading_day():
    with patch("tpcore.engine_profile.cal.is_trading_day", return_value=True):
        assert _cadence_boundary(profile_for("reversion"), datetime(2026, 5, 5, 21, 30, tzinfo=UTC)) is True


def test_daily_boundary_false_on_non_trading_day():
    with patch("tpcore.engine_profile.cal.is_trading_day", return_value=False):
        assert _cadence_boundary(profile_for("reversion"), datetime(2026, 5, 9, 21, 30, tzinfo=UTC)) is False


def test_monthly_boundary_true_only_on_first_session_of_month():
    with patch("tpcore.engine_profile.cal.first_session_of_month", return_value=date(2026, 5, 4)):
        p = profile_for("momentum")
        assert _cadence_boundary(p, datetime(2026, 5, 4, 21, 30, tzinfo=UTC)) is True
        assert _cadence_boundary(p, datetime(2026, 5, 5, 21, 30, tzinfo=UTC)) is False


def test_weekly_boundary_true_only_on_first_session_of_week():
    with patch("tpcore.engine_profile.cal.sessions_in_range", return_value=[date(2026, 5, 4), date(2026, 5, 5)]):
        p = profile_for("allocator")
        assert _cadence_boundary(p, datetime(2026, 5, 4, 13, 0, tzinfo=UTC)) is True
        assert _cadence_boundary(p, datetime(2026, 5, 5, 13, 0, tzinfo=UTC)) is False


def test_daily_window_start_is_midnight_utc_of_now_date():
    ws = _cadence_window_start(profile_for("reversion"), datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    assert ws == datetime(2026, 5, 5, 0, 0, tzinfo=UTC)


def test_monthly_window_start_is_first_session_midnight():
    with patch("tpcore.engine_profile.cal.first_session_of_month", return_value=date(2026, 5, 4)):
        ws = _cadence_window_start(profile_for("momentum"), datetime(2026, 5, 4, 21, 30, tzinfo=UTC))
        assert ws == datetime(2026, 5, 4, 0, 0, tzinfo=UTC)


def test_weekly_window_start_is_week_first_session_midnight():
    with patch("tpcore.engine_profile.cal.sessions_in_range", return_value=[date(2026, 5, 4), date(2026, 5, 5)]):
        ws = _cadence_window_start(profile_for("allocator"), datetime(2026, 5, 5, 13, 0, tzinfo=UTC))
        assert ws == datetime(2026, 5, 4, 0, 0, tzinfo=UTC)


class _FakeConn:
    def __init__(self, ran: bool):
        self._ran = ran

    async def fetchval(self, *_a, **_k):
        return 1 if self._ran else None


class _FakePool:
    def __init__(self, ran: bool = False):
        self._ran = ran

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._ran)


def _patch_all(*, boundary=True, closed=True, data_ok=True):
    cm = contextlib.ExitStack()
    cm.enter_context(patch("tpcore.engine_profile._cadence_boundary", return_value=boundary))
    cm.enter_context(patch("tpcore.engine_profile.cal.session_contains", return_value=not closed))
    ag = AsyncMock(return_value=None) if data_ok else AsyncMock(side_effect=RuntimeError("stale"))
    cm.enter_context(patch("tpcore.engine_profile.assert_passed_for_engine", ag))
    cm.enter_context(patch("tpcore.engine_profile.current_hold", new=AsyncMock(return_value=None)))
    return cm


async def test_should_fire_all_green_fires():
    with _patch_all():
        d = await should_fire("reversion", datetime(2026, 5, 5, 21, 30, tzinfo=UTC), _FakePool(ran=False))
    assert isinstance(d, FireDecision)
    assert d.fire is True and d.reason == "ready"
    assert d.checks == {"profiled": True, "dispatchable": True, "cadence": True,
                        "market_closed": True, "supervisor_held": True,
                        "data_ready": True, "not_already_run": True}


async def test_unknown_engine_fail_closed():
    d = await should_fire("nope", datetime(2026, 5, 5, 21, 30, tzinfo=UTC), _FakePool())
    assert d.fire is False and "unprofiled" in d.reason and d.checks["profiled"] is False


async def test_not_a_cadence_boundary_no_fire():
    with _patch_all(boundary=False):
        d = await should_fire("momentum", datetime(2026, 5, 5, 21, 30, tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason == "not a cadence boundary"


async def test_market_open_no_fire():
    with _patch_all(closed=False):
        d = await should_fire("reversion", datetime(2026, 5, 5, 15, 0, tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason == "market open"


async def test_data_not_ready_no_fire():
    with _patch_all(data_ok=False):
        d = await should_fire("reversion", datetime(2026, 5, 5, 21, 30, tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason.startswith("data not ready")


async def test_already_ran_this_cycle_no_fire():
    with _patch_all():
        d = await should_fire("reversion", datetime(2026, 5, 5, 21, 30, tzinfo=UTC), _FakePool(ran=True))
    assert d.fire is False and d.reason == "already ran this cycle"


async def test_exception_in_check_fails_closed():
    with patch("tpcore.engine_profile._cadence_boundary", side_effect=RuntimeError("boom")):
        d = await should_fire("reversion", datetime(2026, 5, 5, 21, 30, tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason.startswith("error:")


async def test_should_fire_blocks_when_supervisor_held():
    from tpcore.supervisor_state import HoldState

    held = HoldState(hold_id="h-9", failure_class="crashed_startup",
                      reason="stale", held_at=datetime(2026, 5, 5, tzinfo=UTC))
    with _patch_all(), \
         patch("tpcore.engine_profile.current_hold",
               new=AsyncMock(return_value=held)):
        d = await should_fire("reversion",
                              datetime(2026, 5, 5, 21, 30, tzinfo=UTC),
                              _FakePool(ran=False))
    assert d.fire is False
    assert d.reason == "supervisor hold"
    assert d.checks["supervisor_held"] is False
    assert "data_ready" not in d.checks


async def test_should_fire_proceeds_when_not_held():
    with _patch_all(), \
         patch("tpcore.engine_profile.current_hold",
               new=AsyncMock(return_value=None)):
        d = await should_fire("reversion",
                              datetime(2026, 5, 5, 21, 30, tzinfo=UTC),
                              _FakePool(ran=False))
    assert d.fire is True and d.reason == "ready"
    assert d.checks["supervisor_held"] is True


def test_lifecycle_state_enum_values():
    assert {s.value for s in LifecycleState} == {"lab", "paper", "live", "retired"}


def test_profile_has_new_fields_all_entries():
    # 5 live PAPER engines + allocator are PAPER; carver is the first
    # LAB engine (planner-ADDed via ecr_carver.txt; LAB → PAPER is a
    # future automated ECR-MODIFY once the Lab dossier clears the gate);
    # catalyst is the first engine activated via the autonomous Lab
    # criteria path (source=existing_code criteria pass → PAPER, see
    # docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md);
    # sigma is RETIRED; lab is the durable SP2 LAB sentinel (D-SP2-4
    # two-tier registry). planner._apply_add forces
    # allocator_eligible=False on LAB ADDs.
    expected = {
        "allocator": (0, LifecycleState.PAPER, False),
        "reversion": (1, LifecycleState.PAPER, True),
        "vector":    (2, LifecycleState.PAPER, True),
        "momentum":  (3, LifecycleState.PAPER, True),
        "sentinel":  (4, LifecycleState.PAPER, False),
        "canary":    (5, LifecycleState.PAPER, False),
        "carver":    (6, LifecycleState.LAB, False),
        "catalyst":  (7, LifecycleState.PAPER, True),
        "sigma":     (99, LifecycleState.RETIRED, False),
        "lab":       (50, LifecycleState.LAB, False),
    }
    assert set(_PROFILE) == set(expected)
    for name, (order, state, elig) in expected.items():
        p = _PROFILE[name]
        assert p.dispatch_order == order
        assert p.lifecycle_state is state
        assert p.allocator_eligible is elig


def test_profile_for_sigma_returns_retired_profile():
    p = profile_for("sigma")
    assert p is not None and p.lifecycle_state is LifecycleState.RETIRED


def test_engine_profile_rejects_missing_required_fields():
    with pytest.raises(ValidationError):
        EngineProfile(engine="x", cadence=Cadence.DAILY)  # no dispatch_order/lifecycle_state


def test_accessors_return_exact_frozen_literals():
    from tpcore.engine_profile import (
        allocator_eligible_engines,
        archived_engines,
        engine_package_names,
        roster_for_dispatch,
    )
    assert roster_for_dispatch() == ("reversion", "vector", "momentum", "sentinel", "canary", "catalyst")
    assert allocator_eligible_engines() == ("reversion", "vector", "momentum", "catalyst")
    assert archived_engines() == ("sigma",)
    assert engine_package_names() == frozenset(
        {"reversion", "vector", "momentum", "sentinel", "canary", "catalyst"})


def test_roster_excludes_allocator_and_retired():
    from tpcore.engine_profile import roster_for_dispatch
    r = roster_for_dispatch()
    assert "allocator" not in r and "sigma" not in r


def test_dispatch_order_uniqueness_validation():
    from tpcore.engine_profile import _roster_sorted
    bad = {
        "a": EngineProfile(engine="a", cadence=Cadence.DAILY, dispatch_order=1,
                           lifecycle_state=LifecycleState.PAPER),
        "b": EngineProfile(engine="b", cadence=Cadence.DAILY, dispatch_order=1,
                           lifecycle_state=LifecycleState.PAPER),
    }
    with pytest.raises(ValueError, match="duplicate dispatch_order"):
        _roster_sorted(bad)


async def test_should_fire_fails_closed_for_non_dispatchable_lifecycle():
    # sigma is RETIRED in _PROFILE → should_fire must fail-closed even
    # though profile_for now returns a profile (H-B7).
    d = await should_fire("sigma", datetime(2026, 5, 18, 21, 0, tzinfo=UTC), pool=None)
    assert d.fire is False
    assert d.reason == "engine not dispatchable (lifecycle)"
    assert d.checks.get("dispatchable") is False


def test_check_imports_engine_packages_derived_and_drift_fixed():
    from tpcore.scripts.check_imports import ENGINE_PACKAGES
    assert ENGINE_PACKAGES == frozenset(
        {"reversion", "vector", "momentum", "sentinel", "canary", "catalyst"})
    assert "sigma" not in ENGINE_PACKAGES   # archived drift fixed
    assert "canary" in ENGINE_PACKAGES      # missing-live drift fixed


# ─── Declarative data_dependencies (folded 2026-05-20) ────────────────


def test_engine_profile_has_data_dependencies_field():
    """SP-folded 2026-05-20: EngineProfile carries the per-engine data
    precondition declaration as a typed field (frozenset[str], default
    empty). Pin the field exists + is a frozenset so a regression
    would red here, not silently lose the SoT."""
    p = profile_for("reversion")
    assert hasattr(p, "data_dependencies")
    assert isinstance(p.data_dependencies, frozenset)


def test_data_dependencies_byte_equivalent_to_pre_migration_engine_tables():
    """The migration is byte-equivalent (spec §3.2 / §4.1). For every
    engine that pre-migration had an ENGINE_TABLES row, the same
    frozenset is present on EngineProfile.data_dependencies. Pinned
    against the evidence-derived literal that capital_gate.py used
    pre-fold (2026-05-16 evidence)."""
    expected = {
        "reversion": frozenset({"prices_daily", "fundamentals_quarterly"}),
        "vector": frozenset({"prices_daily", "fundamentals_quarterly", "earnings_events"}),
        "momentum": frozenset({"prices_daily", "liquidity_tiers"}),
        "sentinel": frozenset({"prices_daily", "macro_indicators"}),
        "canary": frozenset({"prices_daily"}),
        "catalyst": frozenset({"prices_daily", "sec_insider_transactions"}),
        "allocator": frozenset({"prices_daily"}),
    }
    for name, deps in expected.items():
        assert _PROFILE[name].data_dependencies == deps, (
            f"{name}: data_dependencies drift "
            f"{_PROFILE[name].data_dependencies} != expected {deps}")


def test_dispatchable_engine_declares_data_dependencies():
    """Drift-proof clockwork: every PAPER/LIVE engine in _PROFILE MUST
    declare a non-empty data_dependencies set. The LAB sentinel + LAB
    engines + the RETIRED sigma are exempt (they are never dispatched;
    an empty set is correct). A half-state engine (PAPER/LIVE with
    empty data_dependencies) is a silent un-gated half-state — fails
    CI. Spec §3.5."""
    for name, p in _PROFILE.items():
        if p.lifecycle_state in (LifecycleState.PAPER, LifecycleState.LIVE):
            assert p.data_dependencies, (
                f"{name}: PAPER/LIVE engine with empty data_dependencies — "
                f"declare its `platform.<table>` reads in _PROFILE "
                f"(see the engine's backtest.py / scheduler.py for actual "
                f"reads, evidence-derive the frozenset)")


def test_engine_data_dependencies_accessor_matches_field():
    """The accessor is the canonical single-SoT read; pin it equals the
    field for known engines AND returns the empty frozenset (fail-SAFE)
    for unknown engines — preserving the pre-migration
    `_required_sources` fallback to EXPECTED_SOURCES."""
    from tpcore.engine_profile import engine_data_dependencies
    for name, p in _PROFILE.items():
        assert engine_data_dependencies(name) == p.data_dependencies
    assert engine_data_dependencies("does_not_exist") == frozenset()


def test_engine_tables_shim_removed():
    """§7.4 follow-up (post-#191 + this PR): the back-compat
    `capital_gate.ENGINE_TABLES` shim is gone. Imports of it raise
    AttributeError; consumers now call `engine_data_dependencies()`
    on `tpcore.engine_profile` directly. Prevents a future re-introduction
    of the parallel-SoT read-model."""
    import pytest

    from tpcore.quality.validation import capital_gate
    with pytest.raises(AttributeError):
        _ = capital_gate.ENGINE_TABLES
