from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from tpcore.engine_profile import (
    _PROFILE,
    Cadence,
    EngineProfile,
    _cadence_boundary,
    _cadence_window_start,
    profile_for,
)


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
