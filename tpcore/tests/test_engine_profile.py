import pytest
from pydantic import ValidationError

from tpcore.engine_profile import _PROFILE, Cadence, EngineProfile, profile_for


def test_profile_for_known_engines():
    assert profile_for("reversion").cadence is Cadence.DAILY
    assert profile_for("vector").cadence is Cadence.DAILY
    assert profile_for("sentinel").cadence is Cadence.DAILY
    assert profile_for("momentum").cadence is Cadence.MONTHLY_FIRST_TRADING_DAY
    assert profile_for("allocator").cadence is Cadence.WEEKLY_FIRST_TRADING_DAY


def test_profile_for_unknown_returns_none():
    assert profile_for("does_not_exist") is None


def test_profiles_are_frozen_and_self_consistent():
    for name, p in _PROFILE.items():
        assert isinstance(p, EngineProfile)
        assert p.engine == name
        with pytest.raises(ValidationError):
            p.cadence = Cadence.DAILY  # frozen


def test_profile_covers_live_engine_roster():
    # SoT: scripts/run_all_engines.sh:73 (sigma archived — excluded).
    live = {"reversion", "vector", "momentum", "sentinel"}
    missing = live - set(_PROFILE)
    assert not missing, f"engines without an EngineProfile: {missing}"
