"""Carver — Plug 3: Execution & Risk (vol-target sizing + speed limit + payloads).

For each ``CarverAssessment`` (one per ticker), compute the target position
notional via the Carver formula::

    position_notional = (combined_forecast / FORECAST_TARGET_ABS)
                      * (daily_cash_vol_target / instrument_daily_cash_vol)

where ``daily_cash_vol_target = engine_equity * annualized_vol_target /
sqrt(252)``. Long-only: negative combined forecasts size to zero.

Speed limit (spec Section 4.2): a candidate whose direction-flip count
over the trailing 365 days is at or above ``MAX_TRADES_PER_INSTRUMENT_PER_YEAR``
is suppressed. The counter lives in ``platform.application_log`` keyed by
``CARVER_FLIP`` events; the lifecycle plug owns the read/write.

Output: ``RebalanceDecision`` (targets + day-market order payloads).
Day-market only — no per-name TP/SL between rebalances.
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_t
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

import structlog

from carver.models import (
    FORECAST_TARGET_ABS,
    MAX_TRADES_PER_INSTRUMENT_PER_YEAR,
    CarverAssessment,
    CarverTarget,
    RebalanceAction,
    RebalanceDecision,
    RebalanceOrder,
)
from tpcore.exceptions import SizingError
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.order_ids import build_cid

logger = structlog.get_logger(__name__)

# sqrt(252) — annualized -> daily vol conversion factor.
_SQRT_252 = Decimal("15.874507866387544")  # accurate to 15 digits


class _LifecycleQuery(Protocol):
    """Minimal lifecycle-plug interface used by execution risk.

    See ``carver.plugs.lifecycle_analysis.CarverLifecycleAnalysis``."""

    async def flips_in_window(
        self,
        pool: object,
        ticker: str,
        as_of: date_t,
        days: int = 365,
    ) -> int: ...  # pragma: no cover


class _SizedCandidate:
    """Internal: a candidate + the qty/notional/payload the plug computed."""

    __slots__ = ("assessment", "qty", "notional_usd", "order_payload", "combined_forecast")

    def __init__(
        self,
        assessment: CarverAssessment,
        qty: int,
        notional_usd: Decimal,
        order_payload: dict,
    ) -> None:
        self.assessment = assessment
        self.qty = qty
        self.notional_usd = notional_usd
        self.order_payload = order_payload
        self.combined_forecast = assessment.combined_capped


# ── Pure-math helpers (testable without instantiating the plug) ─────────


def _position_notional(
    *,
    combined_forecast: float,
    daily_cash_vol_target: Decimal,
    instrument_daily_cash_vol: Decimal,
) -> Decimal:
    """Carver sizing formula — spec Section 4.2.

    Raises ``SizingError`` if ``instrument_daily_cash_vol <= 0``.
    """
    if instrument_daily_cash_vol <= 0:
        raise SizingError("instrument daily cash vol must be > 0")
    scale = Decimal(str(combined_forecast)) / Decimal(FORECAST_TARGET_ABS)
    return (scale * (daily_cash_vol_target / instrument_daily_cash_vol)).quantize(
        Decimal("0.01")
    )


def _build_market_order_payload(
    *, ticker: str, qty: int, side: str, constructed_at: datetime,
) -> dict:
    """Day-market order payload with carver's ``cv_`` client_order_id prefix."""
    cid = build_cid(engine="carver", ticker=ticker, constructed_at=constructed_at)
    return {
        "client_order_id": cid,
        "symbol": ticker,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }


# ── The plug ────────────────────────────────────────────────────────────


class CarverExecutionRisk(BaseEnginePlug):
    """Plug 3 of Carver — vol-target sizing + speed limit + day-market payloads."""

    engine_name = "carver"

    def __init__(
        self,
        *,
        max_trades_per_instrument_per_year: int = MAX_TRADES_PER_INSTRUMENT_PER_YEAR,
    ) -> None:
        self._speed_limit = max_trades_per_instrument_per_year

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "execution_risk",
            "ok": True,
            "details": {
                "max_trades_per_instrument_per_year": self._speed_limit,
            },
        }

    def size_one(
        self,
        candidate: CarverAssessment,
        *,
        engine_equity_usd: Decimal,
        annualized_vol_target: Decimal,
        constructed_at: datetime | None = None,
    ) -> _SizedCandidate | None:
        """Size one candidate.

        Returns ``None`` if combined_forecast <= 0 (long-only) or qty < 1.
        Raises ``SizingError`` if the instrument price is non-positive.
        """
        if candidate.instrument_price_usd <= 0:
            raise SizingError(
                f"instrument price for {candidate.ticker} must be > 0; "
                f"got {candidate.instrument_price_usd!s}"
            )
        cf = candidate.combined_capped
        if cf <= 0:
            return None  # long-only: negative forecast -> no position
        instrument_daily_cash_vol = (
            Decimal(str(candidate.instrument_daily_vol_pct))
            * candidate.instrument_price_usd
        )
        if instrument_daily_cash_vol <= 0:
            return None  # zero-vol instrument (data gap); skip silently
        daily_cash_vol_target = (
            engine_equity_usd * annualized_vol_target / _SQRT_252
        )
        try:
            notional = _position_notional(
                combined_forecast=cf,
                daily_cash_vol_target=daily_cash_vol_target,
                instrument_daily_cash_vol=instrument_daily_cash_vol,
            )
        except SizingError:
            raise
        # Long-only -> integer share count via floor.
        qty = int(
            (notional / candidate.instrument_price_usd).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        if qty < 1:
            return None
        notional_actual = (
            Decimal(qty) * candidate.instrument_price_usd
        ).quantize(Decimal("0.01"))
        payload = _build_market_order_payload(
            ticker=candidate.ticker,
            qty=qty,
            side="buy",
            constructed_at=constructed_at or datetime.now(UTC),
        )
        return _SizedCandidate(
            assessment=candidate,
            qty=qty,
            notional_usd=notional_actual,
            order_payload=payload,
        )

    async def decide(
        self,
        *,
        candidates: list[CarverAssessment],
        engine_equity_usd: Decimal,
        current_holdings: dict[str, int],
        lifecycle: _LifecycleQuery,
        pool: object,
        as_of: date_t,
        annualized_vol_target: Decimal = Decimal("0.25"),
    ) -> RebalanceDecision:
        """Build the full rebalance decision: targets + orders + count buckets.

        Pure orchestration on top of ``size_one`` + ``lifecycle.flips_in_window``.
        The scheduler hands us ``current_holdings`` (``cv_``-prefixed only)
        and gates orders through ``tpcore.risk.batch_gate`` downstream.
        """
        sized: list[_SizedCandidate] = []
        for cand in candidates:
            # Speed-limit gate — suppress the 13th+ flip in a year.
            try:
                flips = await lifecycle.flips_in_window(
                    pool, cand.ticker, as_of, days=365,
                )
            except Exception as exc:  # noqa: BLE001 — degrade gracefully if log gone
                logger.warning(
                    "carver.execution.flips_lookup_failed",
                    ticker=cand.ticker, error=str(exc)[:200],
                )
                flips = 0
            if flips >= self._speed_limit:
                logger.info(
                    "carver.execution.speed_limit_blocked",
                    ticker=cand.ticker, flips=flips, cap=self._speed_limit,
                )
                continue
            try:
                row = self.size_one(
                    cand,
                    engine_equity_usd=engine_equity_usd,
                    annualized_vol_target=annualized_vol_target,
                )
            except SizingError as exc:
                logger.warning(
                    "carver.execution.sizing_error",
                    ticker=cand.ticker, error=str(exc),
                )
                continue
            if row is None:
                continue
            sized.append(row)

        # Build targets + diff against current_holdings.
        target_by_ticker = {s.assessment.ticker: s for s in sized}
        all_tickers = set(target_by_ticker) | set(current_holdings)
        targets: list[CarverTarget] = []
        orders: list[RebalanceOrder] = []
        n_open = n_close = n_increase = n_decrease = n_hold = 0
        total_buy_notional_usd = Decimal("0")

        for s in sized:
            targets.append(
                CarverTarget(
                    ticker=s.assessment.ticker,
                    target_shares=s.qty,
                    target_notional_usd=s.notional_usd,
                    combined_forecast=s.combined_forecast,
                )
            )

        for ticker in sorted(all_tickers):
            current = int(current_holdings.get(ticker, 0))
            sized_row = target_by_ticker.get(ticker)
            target_shares = sized_row.qty if sized_row else 0
            delta = target_shares - current
            if delta == 0:
                if current > 0:
                    n_hold += 1
                continue
            side = "buy" if delta > 0 else "sell"
            qty = abs(delta)
            if sized_row is None:
                # Closing a name we hold but don't have a fresh sized row for.
                action = RebalanceAction.CLOSE
                n_close += 1
                price = Decimal("0")
                payload = _build_market_order_payload(
                    ticker=ticker, qty=qty, side="sell",
                    constructed_at=datetime.now(UTC),
                )
            else:
                price = sized_row.assessment.instrument_price_usd
                if current == 0:
                    action = RebalanceAction.OPEN
                    n_open += 1
                    payload = sized_row.order_payload
                elif delta > 0:
                    action = RebalanceAction.INCREASE
                    n_increase += 1
                    payload = _build_market_order_payload(
                        ticker=ticker, qty=qty, side="buy",
                        constructed_at=datetime.now(UTC),
                    )
                else:
                    action = RebalanceAction.DECREASE
                    n_decrease += 1
                    payload = _build_market_order_payload(
                        ticker=ticker, qty=qty, side="sell",
                        constructed_at=datetime.now(UTC),
                    )
            notional = (Decimal(qty) * price).quantize(Decimal("0.01"))
            if side == "buy":
                total_buy_notional_usd += notional
            orders.append(
                RebalanceOrder(
                    ticker=ticker,
                    action=action,
                    side=side,
                    qty=qty,
                    notional_usd=notional,
                    order_payload=payload,
                )
            )

        logger.info(
            "carver.execution.decided",
            as_of=as_of.isoformat(),
            n_candidates=len(candidates),
            n_sized=len(sized),
            n_open=n_open, n_close=n_close,
            n_increase=n_increase, n_decrease=n_decrease, n_hold=n_hold,
        )
        return RebalanceDecision(
            targets=targets,
            orders=orders,
            n_open=n_open,
            n_close=n_close,
            n_increase=n_increase,
            n_decrease=n_decrease,
            n_hold=n_hold,
            total_buy_notional_usd=total_buy_notional_usd,
        )


__all__ = [
    "CarverExecutionRisk",
    "_build_market_order_payload",
    "_position_notional",
]
