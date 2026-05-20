def test_canary_in_roster():
    from ops.engine_dispatch import ROSTER
    assert "canary" in ROSTER


def test_canary_profiled_daily():
    from tpcore.engine_profile import Cadence, profile_for
    p = profile_for("canary")
    assert p is not None and p.cadence is Cadence.DAILY


def test_canary_data_gate_is_prices_daily():
    from tpcore.engine_profile import engine_data_dependencies
    assert engine_data_dependencies("canary") == frozenset({"prices_daily"})


def test_canary_excluded_from_allocator_inverse_vol_pool():
    import inspect

    from tpcore.allocator.service import AllocatorService
    sig = inspect.signature(AllocatorService.__init__)
    default_engines = sig.parameters["engines"].default
    assert "canary" not in default_engines


def test_canary_limits_one_position():
    from tpcore.risk.limits_profile import limits_for
    assert limits_for("canary").max_open_positions == 1
