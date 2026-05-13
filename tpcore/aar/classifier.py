"""Classify an AAR ``exit_reason`` from broker fill data.

Any code that builds an :class:`AfterActionReport` from a tier-2 bracket
fill should call :func:`classify_exit_reason` rather than guessing.
Keeping this in ``tpcore.aar`` (vs trade_monitor) lets engine-specific
AAR writers reuse the same proximity heuristic if they ever close a
position outside the trade_monitor path.
"""

from __future__ import annotations

from decimal import Decimal

from .models import ExitReason

DEFAULT_TOLERANCE_BPS = 50  # 0.5% — wider than typical bracket slippage


def classify_exit_reason(
    *,
    exit_price: Decimal,
    take_profit: Decimal | None,
    stop_loss: Decimal | None,
    tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
) -> ExitReason:
    """Pick the exit reason by checking which bracket leg fired.

    Tier 2 brackets are OCO TP+SL. The actual fill tells us which side
    fired:

    * within ``tolerance_bps`` of the TP target → :data:`TAKE_PROFIT`
    * within ``tolerance_bps`` of the SL trigger → :data:`STOP_LOSS`
    * mid-bracket (no leg matched, e.g., a manual market-close via
      reconcile) → :data:`TIME_STOP` (the closest available bucket
      meaning "exited outside the planned brackets")

    Missing TP and SL both default to :data:`TIME_STOP` — the
    conservative choice when we don't know which leg fired.
    """
    tol = (exit_price * Decimal(tolerance_bps) / Decimal(10000)).copy_abs()
    if take_profit is not None and (exit_price - take_profit).copy_abs() <= tol:
        return ExitReason.TAKE_PROFIT
    if stop_loss is not None and (exit_price - stop_loss).copy_abs() <= tol:
        return ExitReason.STOP_LOSS
    return ExitReason.TIME_STOP


__all__ = ["classify_exit_reason", "DEFAULT_TOLERANCE_BPS"]
