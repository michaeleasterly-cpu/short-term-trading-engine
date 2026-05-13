"""Momentum — Plug 3: Execution & Risk Scaling.

Given a ranked candidate list + account capital + current Alpaca portfolio,
produce a batch of order payloads that moves the portfolio toward the
target. Key behaviours:

* **Top decile cut**: rank-based; ``top_decile_pct`` defaults to 0.10.
* **Equal-weight by count**: each target name gets ``equity * cap`` capital.
  ``cap = min(PER_NAME_CAP_PCT, 1.0/n_targets)`` so a 130-name decile is
  capped at 1/130 ≈ 0.77% per name (well under the 1% concentration cap),
  but a tiny universe of <100 names would still respect the 1% cap.
* **Tier filter**: any candidate with tier > ``MAX_TIER_FOR_TRADING`` is
  silently dropped — illiquid names shouldn't enter the portfolio even if
  their momentum score qualifies.
* **Cost gate**: each candidate's expected edge (its 12-1 score / hold-period
  factor) is compared against the tier-aware round-trip cost via the
  platform :class:`tpcore.risk.RiskGovernor`. Names where cost > edge are
  dropped before sizing.
* **Diff against current holdings**: produces OPEN / INCREASE / DECREASE /
  CLOSE / HOLD orders. HOLD is a no-op (no order generated) and is only
  recorded in the decision summary.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

import structlog

from momentum.models import (
    HOLD_DAYS,
    MAX_TIER_FOR_TRADING,
    PER_NAME_CAP_PCT,
    TOP_DECILE_PCT,
    MomentumCandidate,
    RebalanceAction,
    RebalanceDecision,
    RebalanceOrder,
    TargetPosition,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.risk.governor import RiskGovernor

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = structlog.get_logger(__name__)


class MomentumExecutionRisk(BaseEnginePlug):
    """Plug 3 of Momentum."""

    engine_name = "momentum"

    def __init__(
        self,
        *,
        governor: RiskGovernor,
        top_decile_pct: float = TOP_DECILE_PCT,
        per_name_cap_pct: Decimal = PER_NAME_CAP_PCT,
        max_tier: int = MAX_TIER_FOR_TRADING,
        hold_days: int = HOLD_DAYS,
    ) -> None:
        self._governor = governor
        self._top_decile_pct = top_decile_pct
        self._per_name_cap_pct = per_name_cap_pct
        self._max_tier = max_tier
        self._hold_days = hold_days

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "execution_risk",
            "ok": True,
            "details": {
                "top_decile_pct": self._top_decile_pct,
                "per_name_cap_pct": str(self._per_name_cap_pct),
                "max_tier": self._max_tier,
            },
        }

    async def build_decision(
        self,
        *,
        candidates: list[MomentumCandidate],
        equity_usd: Decimal,
        current_holdings: dict[str, int],
        as_of,
    ) -> RebalanceDecision:
        """Build the full rebalance plan: targets + orders + summary counts.

        ``current_holdings`` maps ``ticker → current_shares``. Names appearing
        there but not in the new targets get ``CLOSE`` orders."""
        # Filter by tier + cost gate first, then rank-cut.
        screened = await self._screen(candidates)
        n_decile = max(1, int(len(screened) * self._top_decile_pct))
        targets_raw = screened[:n_decile]

        per_name_cap = min(self._per_name_cap_pct, Decimal(1) / Decimal(max(len(targets_raw), 1)))
        target_capital_per = (equity_usd * per_name_cap).quantize(Decimal("0.01"))

        targets: list[TargetPosition] = []
        for cand in targets_raw:
            if cand.last_close <= 0:
                continue
            shares = int(
                (target_capital_per / cand.last_close).to_integral_value(rounding=ROUND_DOWN)
            )
            if shares < 1:
                continue
            notional = (Decimal(shares) * cand.last_close).quantize(Decimal("0.01"))
            targets.append(
                TargetPosition(
                    ticker=cand.ticker,
                    target_notional_usd=notional,
                    target_shares=shares,
                    last_close=cand.last_close,
                    momentum_score=cand.momentum_score,
                )
            )

        target_by_ticker = {t.ticker: t for t in targets}
        all_tickers = set(target_by_ticker) | set(current_holdings)
        orders: list[RebalanceOrder] = []
        n_open = n_close = n_increase = n_decrease = n_hold = 0
        total_buy = Decimal("0")
        total_sell = Decimal("0")
        now = datetime.now(UTC)

        for ticker in sorted(all_tickers):
            current = int(current_holdings.get(ticker, 0))
            tgt = target_by_ticker.get(ticker)
            target_shares = tgt.target_shares if tgt else 0
            delta = target_shares - current

            if delta == 0:
                if current > 0:
                    n_hold += 1
                continue

            side = "buy" if delta > 0 else "sell"
            qty = abs(delta)
            if tgt is not None:
                price = tgt.last_close
            else:
                # Closing a name we hold but don't have a current candidate row
                # for — use the candidate's last_close if present, else assume
                # zero notional for the summary (the broker will set the fill).
                price = Decimal("0")
            notional = (Decimal(qty) * price).quantize(Decimal("0.01"))
            if side == "buy":
                total_buy += notional
            else:
                total_sell += notional

            if tgt is None and current > 0:
                action = RebalanceAction.CLOSE
                n_close += 1
            elif current == 0 and tgt is not None:
                action = RebalanceAction.OPEN
                n_open += 1
            elif delta > 0:
                action = RebalanceAction.INCREASE
                n_increase += 1
            else:
                action = RebalanceAction.DECREASE
                n_decrease += 1

            orders.append(
                RebalanceOrder(
                    ticker=ticker,
                    action=action,
                    qty=qty,
                    side=side,
                    order_payload=self._market_order_payload(ticker, qty, side, now),
                    notional_usd=notional,
                    constructed_at=now,
                )
            )

        logger.info(
            "momentum.exec.decision_built",
            as_of=str(as_of),
            n_candidates_pre_filter=len(candidates),
            n_screened=len(screened),
            n_targets=len(targets),
            n_open=n_open, n_close=n_close, n_increase=n_increase,
            n_decrease=n_decrease, n_hold=n_hold,
            total_buy_usd=str(total_buy), total_sell_usd=str(total_sell),
        )

        return RebalanceDecision(
            as_of=as_of,
            targets=targets,
            orders=orders,
            total_buy_notional_usd=total_buy,
            total_sell_notional_usd=total_sell,
            n_open=n_open, n_close=n_close, n_increase=n_increase,
            n_decrease=n_decrease, n_hold=n_hold,
        )

    async def _screen(self, candidates: list[MomentumCandidate]) -> list[MomentumCandidate]:
        """Apply tier filter + cost gate. Order preserved.

        Bulk-loads the entire liquidity_tiers cost map once via
        ``load_tier_costs`` instead of calling ``governor.check_cost`` per
        candidate. The per-call pattern was acquiring a fresh pooler
        connection for each ticker — at 800+ candidates the Supabase pooler
        reset the connection mid-loop. Reading the table once and screening
        in-process is both correct and ~100× faster.
        """
        from tpcore.backtest.cost_model import (
            DEFAULT_ROUND_TRIP_COST_PCT,
            load_tier_costs,
        )

        # The governor's check_cost reads from the same pool the execution
        # plug was constructed with — reuse it for the bulk load.
        pool = getattr(self._governor, "_pool", None)
        tier_costs: dict[str, float] = {}
        if pool is not None:
            tier_costs = await load_tier_costs(pool)

        default_cost = float(DEFAULT_ROUND_TRIP_COST_PCT)
        out: list[MomentumCandidate] = []
        for c in candidates:
            if c.tier > self._max_tier:
                continue
            # Expected edge for the hold-period (rough): the 12-month return
            # score apportioned to a 1-month hold. Bound at 0 so a negative-
            # momentum candidate (shouldn't be in the top decile anyway) can't
            # produce a negative expected edge that "passes" the gate.
            expected_edge_pct = max(0.0, float(c.momentum_score) / 12.0)
            ticker_cost = tier_costs.get(c.ticker, default_cost)
            if ticker_cost > expected_edge_pct:
                continue
            out.append(c)
        return out

    @staticmethod
    def _market_order_payload(
        ticker: str, qty: int, side: str, constructed_at: datetime,
    ) -> dict:
        """Alpaca v2 ``POST /v2/orders`` body for a vanilla day-market order.

        Day-TIF on purpose: Momentum's rebalance fires at the open of a
        trading day; if a partial fill leaks across the close we re-derive
        the target next month rather than ride a half-filled bracket
        overnight."""
        client_oid = f"mo_{ticker}_{int(constructed_at.timestamp())}"
        return {
            "symbol": ticker,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_oid,
        }
