"""One-shot: run Reversion against the paper account via the canonical scheduler.

Strategy
--------
The full canonical pipeline (setup_detection → lifecycle → execution →
order_manager → broker → AAR) is what we want — the order manager is the
only submitter that handles Reversion's Tier 2 leg correctly (reactively,
after Tier 1 fills). But the engine's setup_detection plug iterates
per-ticker against the data adapter (one round-trip per ticker × ~250 ms
via the Supabase pooler × 7,694-ticker universe = ~30 minutes). That's
the same N+1 anti-pattern we already fixed in ``simulate_universe.py``.

(Sigma was archived 2026-05-16 after its failed-expansion final test
failed the gate — see ``archive/sigma/EULOGY.md``. This entrypoint is
Reversion-only; Vector remains deferred pending P/B-gate recalibration.)

Workaround until ``setup_detection`` is batched at the engine layer:

1. Read the most recent ``UNIVERSE_SIMULATION`` row from
   ``platform.application_log`` for today's Reversion candidate list
   (~4 tickers, plus SPY for Reversion's market context).
2. Bulk-fetch the 200-day bar window for that combined set in **one
   SQL query**.
3. Inject a ``_PreloadedDataAdapter`` that:
       - returns the scoped candidate list from ``get_universe_symbols``
       - serves ``get_daily_bars`` from the in-memory dict (no DB hits)
4. Run the engine's canonical ``Scheduler.run_once()`` with that data
   adapter injected. The scheduler still opens its own pool for
   ``risk_state`` / ``aar_writer`` — keeping that isolation matches the
   production path.

Position sizing stays at the engine default ($1500 pre-grad cap, 20 %
of equity for Reversion). Lowering to $100 specifically is a constants
change in ``reversion.models``, not an out-of-band override.

Idempotency: the engine's order manager reconciles against
``platform.risk_state`` and the broker's open positions before
submitting, so re-runs do not double up.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 \\
      ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \\
      python scripts/start_paper_trading.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from typing import TYPE_CHECKING

from reversion.scheduler import ReversionScheduler
from tpcore.data.postgres_data_adapter import PostgresDataAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.interfaces.data import Bar
from tpcore.logging.db_handler import DBLogHandler

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

    from reversion.scheduler import RunSummary as RevSummary

logger = logging.getLogger("scripts.start_paper_trading")

# Pull 200 calendar days to cover Reversion's LOOKBACK_DAYS + 30 plus
# headroom for the market-context window (`_market_context`).
PRELOAD_LOOKBACK_CALENDAR_DAYS = 200

# Reversion's market-context needs SPY regardless of whether SPY is in
# the candidate list, so always include it in the preload.
SPY = "SPY"

_LATEST_SIM_SQL = """
    SELECT data
    FROM platform.application_log
    WHERE event_type = 'UNIVERSE_SIMULATION'
    ORDER BY recorded_at DESC
    LIMIT 1
"""

_BARS_BULK_SQL = """
    SELECT ticker, date, open, high, low, close, volume, adjusted_close
    FROM platform.prices_daily
    WHERE ticker = ANY($1::text[])
      AND date >= $2
      AND date <= $3
    ORDER BY ticker, date
"""


class _PreloadedDataAdapter(PostgresDataAdapter):
    """``PostgresDataAdapter`` that serves a fixed universe + pre-fetched bars.

    ``get_universe_symbols`` returns the scoped list passed at
    construction. ``get_daily_bars`` filters the pre-loaded in-memory dict
    by the requested ``[start, end]`` window. Other adapter methods fall
    through to the inherited Postgres implementation, so things like
    delisting flags continue to work if a plug asks for them.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        universe: list[str],
        bars_by_ticker: dict[str, list[Bar]],
    ) -> None:
        super().__init__(pool)
        self._scoped_universe = list(universe)
        self._bars_by_ticker = bars_by_ticker

    async def get_universe_symbols(self) -> list[str]:  # noqa: D401
        return list(self._scoped_universe)

    async def get_daily_bars(
        self,
        symbol: str,
        start: date_t,
        end: date_t | None = None,
        as_of: date_t | None = None,
    ) -> list[Bar]:
        bars = self._bars_by_ticker.get(symbol)
        if bars is None:
            return []
        clamp_end = end
        if as_of is not None and (clamp_end is None or as_of < clamp_end):
            clamp_end = as_of
        out: list[Bar] = []
        for bar in bars:
            bar_date = bar.ts.date()
            if bar_date < start:
                continue
            if clamp_end is not None and bar_date > clamp_end:
                continue
            out.append(bar)
        return out


async def _load_candidate_list(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LATEST_SIM_SQL)
    if row is None:
        raise RuntimeError(
            "No UNIVERSE_SIMULATION event found — run scripts/simulate_universe.py first."
        )
    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)
    return list(data.get("reversion_candidates") or [])


async def _preload_bars(
    pool: asyncpg.Pool, tickers: list[str], *, as_of: date_t
) -> dict[str, list[Bar]]:
    """One SQL round-trip; group bars by ticker into a dict of Bar lists."""
    if not tickers:
        return {}
    start = as_of - timedelta(days=PRELOAD_LOOKBACK_CALENDAR_DAYS)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_BARS_BULK_SQL, tickers, start, as_of)
    out: dict[str, list[Bar]] = {}
    for r in rows:
        bar = Bar(
            symbol=r["ticker"],
            ts=datetime(r["date"].year, r["date"].month, r["date"].day, tzinfo=UTC),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=int(r["volume"]),
            adjusted_close=r["adjusted_close"],
        )
        out.setdefault(r["ticker"], []).append(bar)
    return out


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    if not os.getenv("ALPACA_KEY") or not os.getenv("ALPACA_SECRET"):
        print("FAILED — ALPACA_KEY/ALPACA_SECRET not set", file=sys.stderr)
        return 1
    os.environ["DATABASE_URL"] = db_url

    pool = await build_asyncpg_pool(db_url, max_size=4)
    rev_summary: RevSummary | None = None
    rev_error: str | None = None
    as_of = datetime.now(UTC).date()

    try:
        try:
            reversion_candidates = await _load_candidate_list(pool)
        except RuntimeError as exc:
            print(f"FAILED — {exc}", file=sys.stderr)
            return 1

        combined = sorted({SPY, *reversion_candidates})
        logger.info(
            "paper_trading.preload reversion=%d combined=%d",
            len(reversion_candidates),
            len(combined),
        )
        bars_by_ticker = await _preload_bars(pool, combined, as_of=as_of)
        logger.info(
            "paper_trading.preload_done tickers_with_bars=%d total_bars=%d",
            len(bars_by_ticker),
            sum(len(v) for v in bars_by_ticker.values()),
        )

        # Reversion
        try:
            rev_data = _PreloadedDataAdapter(
                pool,
                universe=[t for t in reversion_candidates if t in bars_by_ticker],
                bars_by_ticker=bars_by_ticker,
            )
            rev_summary = await ReversionScheduler(data=rev_data).run_once(as_of=as_of)
        except Exception as exc:
            rev_error = str(exc)
            logger.exception("paper_trading.reversion_failed")

        # Single PAPER_TRADING_START summary row under engine='paper_trading'.
        log_handler = DBLogHandler(pool=pool, engine="paper_trading", run_id=uuid.uuid4())
        await log_handler.log(
            "PAPER_TRADING_START",
            (
                f"reversion={getattr(rev_summary, 'n_submitted', 0)}/"
                f"{getattr(rev_summary, 'n_candidates', 0)}"
            ),
            "ERROR" if rev_error else "INFO",
            {
                "test_trade": True,
                "as_of": as_of.isoformat(),
                "reversion_universe_size": len(reversion_candidates),
                "reversion_candidates_active": getattr(rev_summary, "n_candidates", 0),
                "reversion_submitted": getattr(rev_summary, "n_submitted", 0),
                "reversion_new_aars": len(getattr(rev_summary, "aars", []) or []),
                "reversion_error": rev_error,
                "vector_status": "deferred (P/B gate pending backtest recalibration)",
            },
        )
    finally:
        await pool.close()

    rev_sub = getattr(rev_summary, "n_submitted", 0)
    rev_cand = getattr(rev_summary, "n_candidates", 0)

    print()
    print(f"PAPER TRADING START — {as_of.isoformat()}")
    print(f"  Reversion orders submitted: {rev_sub}  (setup-detection candidates: {rev_cand})")
    if rev_error:
        print(f"    reversion scheduler error: {rev_error}")
    print( "  Vector:                     DEFERRED (P/B gate pending backtest recalibration)")
    print(f"  Total:                      {rev_sub}")
    print( "  Check the Alpaca paper dashboard for fills.")

    return 1 if rev_error else 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
