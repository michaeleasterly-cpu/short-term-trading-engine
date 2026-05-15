"""Sentinel — Plug 3: Execution & Risk (basket construction + orders).

Translates a daily :class:`SentinelState` into a :class:`SentinelDecision`
with target weights, target notionals, target shares, and the diff
orders against the current basket. Pure, no DB.

Sequence per day:

1. Start with the 5-ETF default basket weights.
2. Drop tickers without price history; renormalize the rest to 100%.
3. Apply master-plan §4.6 overrides (shallow recession, VIX breaker,
   SQQQ eligibility) via :func:`apply_basket_overrides`.
4. Multiply weights by ``(1 - fade_factor)`` if the engine is FADING.
5. Apply the capital cap (10% pre-graduation, 20% permanent) to total
   deployable equity.
6. Per-ticker: ``shares = floor(weight × deployable_equity / price)``.
7. Diff vs current holdings → BUY / SELL orders.

DORMANT / EXITED / WATCH days produce ``targets=[]`` and orders that
liquidate any leftover position back to zero. ACTIVE / FADING days
produce the live target basket scaled by the cap.
"""
from __future__ import annotations

from datetime import date as date_t
from decimal import ROUND_DOWN, Decimal

import structlog

from sentinel.models import (
    BASKET_WEIGHTS_DEFAULT,
    PERMANENT_CAP_PCT,
    PRE_GRADUATION_CAP_PCT,
    SentinelDecision,
    SentinelOrder,
    SentinelPhase,
    SentinelState,
    SentinelTarget,
    apply_basket_overrides,
    apply_missing_etf_fallback,
)

logger = structlog.get_logger(__name__)


class SentinelExecutionRisk:
    """Plug 3 — basket construction + order diffing.

    ``graduated`` toggles between the 10% pre-graduation cap and the
    20% permanent cap. Mirrors Momentum's pre-graduation discipline.
    """

    def __init__(self, *, graduated: bool = False) -> None:
        self._graduated = graduated

    @property
    def allocation_cap(self) -> Decimal:
        return PERMANENT_CAP_PCT if self._graduated else PRE_GRADUATION_CAP_PCT

    def build_decision(
        self,
        *,
        as_of: date_t,
        state: SentinelState,
        equity_usd: Decimal,
        prices: dict[str, Decimal],
        current_holdings: dict[str, int],
    ) -> SentinelDecision:
        """Build the basket decision for one day.

        ``prices`` is the latest close per ticker (must be > 0 to count
        as available). ``current_holdings`` is ``{ticker: qty}`` for the
        Sentinel-owned positions only — the caller is responsible for
        filtering to Sentinel's book before passing in.
        """
        available = frozenset(t for t, p in prices.items() if p > 0)
        missing = tuple(sorted(set(BASKET_WEIGHTS_DEFAULT.keys()) - available))

        # When the engine is DORMANT, WATCH, or EXITED: target is zero
        # everywhere; orders close any residual holdings.
        if state.phase in (SentinelPhase.DORMANT, SentinelPhase.WATCH, SentinelPhase.EXITED):
            orders = self._close_orders(current_holdings, prices)
            return SentinelDecision(
                as_of=as_of,
                state=state,
                allocation_cap_pct=self.allocation_cap,
                deployable_equity_usd=Decimal("0"),
                targets=[],
                orders=orders,
                missing_etfs=missing,
            )

        # ACTIVE / FADING — build the target basket.
        weights = apply_missing_etf_fallback(BASKET_WEIGHTS_DEFAULT, available)
        weights = apply_basket_overrides(
            weights,
            shallow_recession=state.shallow_recession_override,
            vix_circuit_breaker=state.vix_circuit_breaker,
            sqqq_eligible=state.sqqq_eligible,
        )
        if not weights:
            # No tradeable ETFs — close any residual and call it.
            orders = self._close_orders(current_holdings, prices)
            return SentinelDecision(
                as_of=as_of,
                state=state,
                allocation_cap_pct=self.allocation_cap,
                deployable_equity_usd=Decimal("0"),
                targets=[],
                orders=orders,
                missing_etfs=missing,
            )

        # Apply the fade factor — multiplied into every weight when FADING.
        if state.phase == SentinelPhase.FADING:
            scale = Decimal("1") - state.fade_factor
            if scale < 0:
                scale = Decimal("0")
            weights = {t: w * scale for t, w in weights.items()}

        deployable = (equity_usd * self.allocation_cap).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN,
        )
        targets: list[SentinelTarget] = []
        for t, w in sorted(weights.items()):
            target_notional = (deployable * w).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            price = prices[t]
            shares = int((target_notional / price).quantize(Decimal("1"), rounding=ROUND_DOWN))
            if shares <= 0:
                continue
            targets.append(SentinelTarget(
                ticker=t,
                target_weight=w,
                target_notional_usd=target_notional,
                target_shares=shares,
                last_price=price,
            ))

        orders = self._diff_orders(targets, current_holdings, prices)

        return SentinelDecision(
            as_of=as_of,
            state=state,
            allocation_cap_pct=self.allocation_cap,
            deployable_equity_usd=deployable,
            targets=targets,
            orders=orders,
            missing_etfs=missing,
        )

    @staticmethod
    def _close_orders(
        current_holdings: dict[str, int],
        prices: dict[str, Decimal],
    ) -> list[SentinelOrder]:
        out: list[SentinelOrder] = []
        for t, qty in sorted(current_holdings.items()):
            if qty <= 0:
                continue
            price = prices.get(t)
            if price is None or price <= 0:
                # No close price — skip rather than synthesize. The order
                # gets resubmitted on the next session that prices arrive.
                continue
            out.append(SentinelOrder(
                ticker=t,
                side="sell",
                qty=int(qty),
                notional_usd=(price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_DOWN),
            ))
        return out

    @staticmethod
    def _diff_orders(
        targets: list[SentinelTarget],
        current_holdings: dict[str, int],
        prices: dict[str, Decimal],
    ) -> list[SentinelOrder]:
        target_by_ticker = {t.ticker: t for t in targets}
        all_tickers = set(target_by_ticker) | set(current_holdings)
        orders: list[SentinelOrder] = []
        for t in sorted(all_tickers):
            tgt = target_by_ticker.get(t)
            cur = int(current_holdings.get(t, 0) or 0)
            tgt_qty = tgt.target_shares if tgt is not None else 0
            delta = tgt_qty - cur
            if delta == 0:
                continue
            price = prices.get(t) or (tgt.last_price if tgt else None)
            if price is None or price <= 0:
                continue
            side = "buy" if delta > 0 else "sell"
            qty = abs(delta)
            orders.append(SentinelOrder(
                ticker=t,
                side=side,
                qty=qty,
                notional_usd=(price * Decimal(qty)).quantize(Decimal("0.01"), rounding=ROUND_DOWN),
            ))
        return orders


__all__ = ["SentinelExecutionRisk"]
