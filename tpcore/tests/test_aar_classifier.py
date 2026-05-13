"""Unit tests for :func:`tpcore.aar.classify_exit_reason`.

These tests pin the behavior shared by every AAR-writing code path.
The trade_monitor delegates to this helper, so the contract here is
the source of truth for what counts as TAKE_PROFIT / STOP_LOSS /
TIME_STOP based on broker fill proximity to the bracket legs.
"""

from __future__ import annotations

from decimal import Decimal

from tpcore.aar import ExitReason, classify_exit_reason


def test_take_profit_within_default_tolerance() -> None:
    # Fill at 190.50, target 190.00 → 50/190.50 ≈ 26 bps < 50 bps default.
    assert classify_exit_reason(
        exit_price=Decimal("190.50"),
        take_profit=Decimal("190.00"),
        stop_loss=Decimal("174.60"),
    ) == ExitReason.TAKE_PROFIT


def test_stop_loss_within_default_tolerance() -> None:
    assert classify_exit_reason(
        exit_price=Decimal("174.40"),
        take_profit=Decimal("190.00"),
        stop_loss=Decimal("174.60"),
    ) == ExitReason.STOP_LOSS


def test_mid_bracket_returns_time_stop() -> None:
    # YUMC-like: trade closed inside the bracket via reconcile.
    assert classify_exit_reason(
        exit_price=Decimal("47.32"),
        take_profit=Decimal("49.72"),
        stop_loss=Decimal("45.49"),
    ) == ExitReason.TIME_STOP


def test_no_brackets_returns_time_stop() -> None:
    assert classify_exit_reason(
        exit_price=Decimal("100.00"),
        take_profit=None,
        stop_loss=None,
    ) == ExitReason.TIME_STOP


def test_only_take_profit_set_and_within_tolerance() -> None:
    assert classify_exit_reason(
        exit_price=Decimal("100.20"),
        take_profit=Decimal("100.00"),
        stop_loss=None,
    ) == ExitReason.TAKE_PROFIT


def test_take_profit_outside_tolerance_falls_through() -> None:
    # Fill at 100.50 vs TP=100.00 → 50/100.5 ≈ 50bps but tolerance is
    # strict (≤). Just past should fall to TIME_STOP.
    assert classify_exit_reason(
        exit_price=Decimal("101.00"),
        take_profit=Decimal("100.00"),
        stop_loss=None,
    ) == ExitReason.TIME_STOP


def test_custom_tolerance_narrows_take_profit_window() -> None:
    # Fill 190.50 vs TP 190.00 — at tolerance 10 bps (≈ 0.19) the gap
    # of 0.50 is outside, so it should fall through to STOP_LOSS check,
    # then TIME_STOP.
    assert classify_exit_reason(
        exit_price=Decimal("190.50"),
        take_profit=Decimal("190.00"),
        stop_loss=Decimal("174.60"),
        tolerance_bps=10,
    ) == ExitReason.TIME_STOP


def test_take_profit_preferred_when_both_within_tolerance() -> None:
    # Degenerate: TP and SL collapse — TP wins (first comparison).
    assert classify_exit_reason(
        exit_price=Decimal("100.00"),
        take_profit=Decimal("100.00"),
        stop_loss=Decimal("100.00"),
    ) == ExitReason.TAKE_PROFIT
