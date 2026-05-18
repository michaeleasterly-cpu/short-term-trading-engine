from decimal import Decimal

from tpcore.risk.governor import RiskLimits
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


# ── #251 A1.1: reconcile_open_floor flag ────────────────────────────────────


def test_reconcile_open_floor_true_for_batch_engines():
    """Only the batch engines (momentum/sentinel) opt into the broker-floor raise."""
    assert limits_for("momentum").reconcile_open_floor is True
    assert limits_for("sentinel").reconcile_open_floor is True


def test_reconcile_open_floor_false_for_every_other_engine():
    """Per-trade + heartbeat engines must NOT raise off the cross-engine broker sum."""
    assert limits_for("reversion").reconcile_open_floor is False
    assert limits_for("vector").reconcile_open_floor is False
    assert limits_for("canary").reconcile_open_floor is False


def test_reconcile_open_floor_defaults_false_for_unknown_engine():
    assert limits_for("does_not_exist").reconcile_open_floor is False
    # And the raw RiskLimits default is False (additive, opt-in).
    assert RiskLimits().reconcile_open_floor is False


def test_a1_1_does_not_change_existing_limit_values():
    """Additive field only — no existing cap/pct moved."""
    m = limits_for("momentum")
    assert m.max_open_positions == 200
    assert limits_for("sentinel").max_open_positions == 5
    assert limits_for("canary").max_open_positions == 1
    assert limits_for("reversion").max_open_positions == 8
    assert m.daily_loss_pct == Decimal("0.05")
    assert m.weekly_loss_pct == Decimal("0.10")
    assert m.platform_net_long_cap_pct == Decimal("0.60")
