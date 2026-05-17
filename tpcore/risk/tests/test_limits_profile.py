from decimal import Decimal

from tpcore.risk.limits_profile import limits_for


def test_momentum_basket_sized_limits():
    lim = limits_for("momentum")
    assert lim.max_open_positions >= 130  # decile basket fits


def test_sentinel_basket_sized_limits():
    assert limits_for("sentinel").max_open_positions >= 5


def test_per_trade_engines_use_default():
    assert limits_for("reversion").max_open_positions == 8
    assert limits_for("vector").max_open_positions == 8


def test_unknown_engine_returns_default():
    assert limits_for("does_not_exist").max_open_positions == 8


def test_loss_caps_stay_platform_uniform():
    lim = limits_for("momentum")
    assert lim.daily_loss_pct == Decimal("0.05")
    assert lim.weekly_loss_pct == Decimal("0.10")
