"""Sentinel — Plug 4: Capital Gate.

Per master plan §4.6:

* Permanent allocation cap: 20% of platform capital.
* Pre-graduation cap: 10%.
* Graduation: ≥ 1 full activation cycle (trigger → ACTIVE → deactivated)
  AND profit factor > 1.5 AND max drawdown < 20% during the cycle.
  No per-trade win-rate requirement — Sentinel may only fire once per
  recession.

The gate has two responsibilities:

1. *Sizing* — return the active cap (10% or 20%) based on graduation.
   Consumed by :class:`SentinelExecutionRisk` via its ``graduated``
   constructor flag.
2. *Approval* — verify a proposed rebalance's total notional doesn't
   exceed the cap times platform capital. Run by the scheduler before
   submitting orders.

Pure module — no DB. Persisted graduation state belongs in
``platform.risk_state`` (handled by the operator workflow that flips
the engine; reading it is left to whoever wires the live scheduler).
"""
from __future__ import annotations

from decimal import Decimal

import structlog

from sentinel.models import (
    GRAD_MAX_DRAWDOWN,
    GRAD_MIN_CYCLES,
    GRAD_MIN_PROFIT_FACTOR,
    PERMANENT_CAP_PCT,
    PRE_GRADUATION_CAP_PCT,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class SentinelCapitalGate(BaseEnginePlug):
    """Allocation gate — sizes Sentinel within its capital cap.

    The cap is fixed; the only mutable input is graduation status.
    Wire ``graduated=True`` after the live engine clears the per-cycle
    graduation criteria (see :func:`evaluate_graduation` below).
    """

    engine_name = "sentinel"

    def __init__(self, *, graduated: bool = False) -> None:
        self._graduated = graduated

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "capital_gate",
            "ok": True,
            "details": {"graduated": self._graduated, "cap_pct": str(self.cap_pct)},
        }

    @property
    def cap_pct(self) -> Decimal:
        return PERMANENT_CAP_PCT if self._graduated else PRE_GRADUATION_CAP_PCT

    def deployable_usd(self, platform_equity_usd: Decimal) -> Decimal:
        """Return the dollar ceiling for Sentinel's basket."""
        if platform_equity_usd <= 0:
            return Decimal("0")
        return (platform_equity_usd * self.cap_pct).quantize(Decimal("0.01"))

    def check_rebalance(
        self,
        proposed_notional_usd: Decimal,
        platform_equity_usd: Decimal,
    ) -> bool:
        """Return True iff the proposed buy notional fits inside the cap.

        Used before order submission to short-circuit obviously-broken
        execution plans (price-explosion fat-finger, missing scaling
        factor, etc.). A rejected rebalance is logged at WARNING and
        the cycle is skipped — Sentinel will try again the next day.
        """
        ceiling = self.deployable_usd(platform_equity_usd)
        if proposed_notional_usd > ceiling:
            logger.warning(
                "sentinel.capital_gate.rejected",
                proposed=str(proposed_notional_usd),
                ceiling=str(ceiling),
                cap_pct=str(self.cap_pct),
            )
            return False
        return True


def evaluate_graduation(
    *,
    completed_cycles: int,
    profit_factor: float | None,
    max_drawdown_pct: float | None,
) -> tuple[bool, str]:
    """Pure function — does this engine meet the spec §4.6 graduation bar?

    Returns ``(passed, reason)``. ``reason`` is a human-readable
    explanation of the first failing criterion, or 'OK' on pass.

    Inputs come from the live AAR rollup. ``max_drawdown_pct`` is a
    fraction (e.g. 0.18 = 18%), matching how other engines log it.
    """
    if completed_cycles < GRAD_MIN_CYCLES:
        return False, f"too few completed cycles: {completed_cycles} < {GRAD_MIN_CYCLES}"
    if profit_factor is None or profit_factor < GRAD_MIN_PROFIT_FACTOR:
        return False, f"profit factor {profit_factor} < {GRAD_MIN_PROFIT_FACTOR}"
    if max_drawdown_pct is None:
        return False, "max drawdown unavailable"
    if Decimal(str(max_drawdown_pct)) > GRAD_MAX_DRAWDOWN:
        return False, f"max drawdown {max_drawdown_pct} > {GRAD_MAX_DRAWDOWN}"
    return True, "OK"


__all__ = ["SentinelCapitalGate", "evaluate_graduation"]
