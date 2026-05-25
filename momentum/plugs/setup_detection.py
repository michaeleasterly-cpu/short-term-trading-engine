"""Momentum — Plug 1: Setup Detection.

Cross-sectional 12-1 momentum scoring. For an ``as_of`` rebalance date:

1. Pull universe from ``platform.liquidity_tiers`` (default T1+T2; configurable).
2. Pull each ticker's price history covering [as_of - lookback - skip, as_of].
3. Compute ``score = price(as_of - skip) / price(as_of - skip - lookback) - 1``.
4. Drop tickers missing either reference bar (continuity check — momentum is
   especially sensitive to gappy data, and our prices_daily isn't fully
   survivorship-clean).
5. Return all candidates sorted descending by score. The orchestrator
   downstream (ExecutionRisk) applies the top-decile cut.

No fundamental or catalyst-event dependency — bars only. The plug is
intentionally minimal because the strategy is intentionally minimal: one
signal (12-1 return), four configuration knobs (lookback, skip, hold,
top-decile fraction).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from momentum.models import (
    LOOKBACK_DAYS,
    MAX_TIER_FOR_TRADING,
    SKIP_DAYS,
    MomentumCandidate,
    is_tradeable_common_stock,
)
from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.data.repositories import PricesRepo, UniverseRepo
from tpcore.identity.dispatcher import IdentityDispatcher
from tpcore.interfaces.engine_plug import BaseEnginePlug

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class MomentumSetupDetection(BaseEnginePlug):
    """Plug 1 of Momentum."""

    engine_name = "momentum"

    def __init__(
        self,
        *,
        lookback_days: int = LOOKBACK_DAYS,
        skip_days: int = SKIP_DAYS,
        max_tier: int = MAX_TIER_FOR_TRADING,
    ) -> None:
        self._lookback = lookback_days
        self._skip = skip_days
        self._max_tier = max_tier

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {
                "lookback_days": self._lookback,
                "skip_days": self._skip,
                "max_tier": self._max_tier,
            },
        }

    async def scan(self, pool: asyncpg.Pool, as_of: date) -> list[MomentumCandidate]:
        """Rank the universe at ``as_of`` and return all qualifying candidates,
        sorted descending by ``momentum_score``. Empty list if the universe
        is empty or no ticker has enough history."""
        universe = await self._load_universe(pool, as_of)
        if not universe:
            logger.warning("momentum.setup.empty_universe", max_tier=self._max_tier)
            return []
        # Load bars covering the lookback window. We need one bar at
        # (as_of - skip) and one at (as_of - skip - lookback); add ~30 calendar
        # days of buffer to handle weekends + holidays.
        load_start = as_of - timedelta(days=self._lookback + self._skip + 30)
        bars_by_ticker = await self._load_bars(pool, list(universe), load_start, as_of)

        diag = FilterDiagnostics(
            universe_total=len(universe),
            momentum_history_blocked=0,
            momentum_score_blocked=0,
            momentum_tradability_blocked=0,
        )
        candidates: list[MomentumCandidate] = []
        tiers = await self._load_tier_map(pool)
        for ticker, bars in bars_by_ticker.items():
            if len(bars) < (self._lookback + self._skip) // 2:
                # Heuristic continuity gate: we expect at least half the calendar
                # window to be filled by trading days. If a ticker is missing a
                # large chunk, treat it as suspicious (delisting? halt?) and skip.
                diag.momentum_history_blocked = (diag.momentum_history_blocked or 0) + 1
                continue
            score = self._score_one(bars, as_of)
            if score is None:
                diag.momentum_score_blocked = (diag.momentum_score_blocked or 0) + 1
                continue
            last_close = Decimal(str(bars[-1]["close"])).quantize(Decimal("0.01"))
            # Tradability filter — drop warrants, preferreds, units, and
            # sub-$5 names regardless of score. See momentum/models.py.
            if not is_tradeable_common_stock(ticker, last_close):
                diag.momentum_tradability_blocked = (diag.momentum_tradability_blocked or 0) + 1
                continue
            candidates.append(
                MomentumCandidate(
                    ticker=ticker,
                    as_of=as_of,
                    momentum_score=float(score),
                    last_close=last_close,
                    tier=int(tiers.get(ticker, self._max_tier)),
                )
            )
        diag.candidates_passed = len(candidates)
        # Attach the same diag instance to every candidate so the scheduler
        # can pass it through to SIGNAL events as extra_data.
        candidates = [c.model_copy(update={"filter_diagnostics": diag}) for c in candidates]
        candidates.sort(key=lambda c: c.momentum_score, reverse=True)
        logger.info(
            "momentum.setup.ranked",
            n_universe=len(universe),
            n_candidates=len(candidates),
            top_score=candidates[0].momentum_score if candidates else None,
            bottom_score=candidates[-1].momentum_score if candidates else None,
        )
        return candidates

    async def _load_universe(self, pool: asyncpg.Pool, as_of: date) -> set[str]:
        """Universe for ``as_of``.

        Primary path reads ``platform.universe_candidates`` (populated daily by
        ``tpcore.universe.prescreener``). If no rows are present for ``as_of``
        — the prescreener hasn't run yet, or the date is older than the table
        — fall back to ``platform.liquidity_tiers`` so the engine remains
        operable. The backtest path has its own loader in ``momentum/backtest.py``
        and is unaffected by this.
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ticker FROM platform.universe_candidates
                WHERE engine = 'momentum' AND as_of_date = $1
                ORDER BY ticker
                """,
                as_of,
            )
            if rows:
                return {r["ticker"] for r in rows}
            logger.info(
                "momentum.setup.universe_fallback",
                as_of=as_of.isoformat(),
                reason="no universe_candidates rows for as_of",
            )
        # Fallback: enumerate the universe via UniverseRepo (PR-16).
        # UniverseRepo reads platform.v_universe which joins
        # ticker_classifications × ticker_history × liquidity_tiers.
        repo = UniverseRepo(pool)
        universe_rows = await repo.enumerate(max_liquidity_tier=self._max_tier)
        return {r.current_ticker for r in universe_rows if r.current_ticker is not None}

    async def _load_tier_map(self, pool: asyncpg.Pool) -> dict[str, int]:
        """Full tier map ticker → tier.

        Edge adapter: queries UniverseRepo for every cid with a tier
        assigned, then projects to ticker → tier. Excludes rows with
        no liquidity_tier (untracked instruments).
        """
        repo = UniverseRepo(pool)
        rows = await repo.enumerate(max_liquidity_tier=999, include_untracked_liquidity=False)
        return {
            r.current_ticker: int(r.liquidity_tier)
            for r in rows
            if r.current_ticker is not None and r.liquidity_tier is not None
        }

    async def _load_bars(
        self,
        pool: asyncpg.Pool,
        tickers: list[str],
        start: date,
        end: date,
    ) -> dict[str, list[dict]]:
        """Edge adapter: ticker list in, ticker-keyed dict[ticker, [{date,close}]] out.

        Dispatches each ticker → classification_id (PR-16) and fetches
        via PricesRepo.get_window_batch by cid. Live-path conversion
        (momentum is the actively-trading paper engine — public method
        signature preserved so the plug's caller contract is unchanged).
        """
        dispatcher = IdentityDispatcher(pool)
        repo = PricesRepo(pool)
        cid_to_ticker: dict[str, str] = {}
        for t in tickers:
            cid = await dispatcher.ticker_to_classification_id(t)
            if cid is not None:
                cid_to_ticker[cid] = t
        out: dict[str, list[dict]] = {}
        if not cid_to_ticker:
            return out
        bars_by_cid = await repo.get_window_batch(list(cid_to_ticker), start, end)
        for cid, bars in bars_by_cid.items():
            ticker = cid_to_ticker[cid]
            for b in sorted(bars, key=lambda x: x.date):
                out.setdefault(ticker, []).append({"date": b.date, "close": float(b.close)})
        return out

    def _score_one(self, bars: list[dict], as_of: date) -> float | None:
        """12-1 momentum: ``price(as_of - skip) / price(as_of - skip - lookback) - 1``.

        Walks the bar list once. Returns None if either reference date is
        missing or either price is non-positive."""
        if not bars:
            return None
        # bars are ordered by date ascending; use the last bar's date as the
        # actual ``as_of`` (calendar ``as_of`` may be a weekend/holiday).
        end_idx = len(bars) - 1
        # Step back ``skip`` calendar days from the last bar; find the latest
        # bar at-or-before that target date.
        skip_target = bars[end_idx]["date"] - timedelta(days=self._skip)
        skip_idx = self._latest_at_or_before(bars, skip_target, end_idx)
        if skip_idx is None:
            return None
        lookback_target = bars[skip_idx]["date"] - timedelta(days=self._lookback)
        lookback_idx = self._latest_at_or_before(bars, lookback_target, skip_idx)
        if lookback_idx is None:
            return None
        p_now = float(bars[skip_idx]["close"])
        p_then = float(bars[lookback_idx]["close"])
        if p_then <= 0 or math.isnan(p_now) or math.isnan(p_then):
            return None
        return (p_now / p_then) - 1.0

    @staticmethod
    def _latest_at_or_before(bars: list[dict], target: date, hi: int) -> int | None:
        """Binary-search variant — given a sorted-by-date bar list, return the
        largest index ``i ≤ hi`` whose date is at-or-before ``target``."""
        lo = 0
        result: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if bars[mid]["date"] <= target:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result
