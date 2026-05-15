"""Prices-daily freshness check — catches silent per-ticker refresh drops.

The existing ``row_integrity`` and ``delistings`` checks confirm
``platform.prices_daily`` is *broadly* healthy (no rows are missing
columns, the delisting flags match the source). Neither catches the
specific failure mode where a single high-value ticker silently stops
refreshing while the rest of the universe keeps flowing.

That failure mode bit us on 2026-05-15: SPY drifted 2 trading days
behind TLT/SQQQ because Alpaca returned an empty bars list for it on
one daily-refresh run and the per-ticker error was swallowed (the
handler raises only on HTTPStatusError; an empty 200 OK looks like a
"no new bars to insert" success). Sentinel reads SPY for its VIX proxy
+ rally veto, so a silent SPY gap quietly breaks the engine.

This check enforces two contracts:

1. **Critical-ticker freshness.** A hardcoded list of platform-critical
   tickers (the Sentinel basket, SPY, market-context names) must all
   have a bar within ``CRITICAL_MAX_AGE_DAYS`` (default 5 trading days
   = ~7 calendar days). One missing critical ticker → check fails.

2. **Universe-level staleness.** The fraction of non-delisted tickers
   whose last bar is older than ``UNIVERSE_MAX_AGE_DAYS`` (default 14
   calendar days) must not exceed ``UNIVERSE_STALE_PCT_MAX`` (default
   2.0% — historically we sit at ~0.1%, so 2% is a strong red line).
   Catches widespread daily-bars-handler outages without false-flagging
   on the handful of in-flight delistings.

Stale-but-not-yet-delisted SPACs / funds are auto-promoted by the
``delist_stale`` ops stage; this check assumes that stage has run
recently and therefore treats remaining stale rows as actual problems.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "prices_daily_freshness"

# Tickers every engine + dashboard relies on. Any one missing >5 trading
# days breaks something visible; the check fails immediately on a miss.
CRITICAL_TICKERS: tuple[str, ...] = (
    "SPY",          # Sigma SPY-CHOP regime gate, Sentinel VIX proxy + rally veto
    "QQQ",          # Vector / Momentum regime cross-check
    "TLT",          # Sentinel basket — defensive ETF
    "SQQQ",         # Sentinel basket — tactical inverse
    "SH",           # Sentinel basket
    "PSQ",          # Sentinel basket
    "GLD",          # Sentinel basket
)
CRITICAL_MAX_AGE_DAYS = 5     # calendar days; ~3 trading days
UNIVERSE_MAX_AGE_DAYS = 14    # calendar days
UNIVERSE_STALE_PCT_MAX = 0.02  # 2% of the active universe


_CRITICAL_SQL = """
    SELECT t.ticker, MAX(pd.date) AS last_bar
    FROM UNNEST($1::text[]) AS t(ticker)
    LEFT JOIN platform.prices_daily pd
        ON pd.ticker = t.ticker AND pd.delisted = false
    GROUP BY t.ticker
"""

_UNIVERSE_SQL = """
    WITH per_ticker AS (
        SELECT ticker, MAX(date) AS last_bar
        FROM platform.prices_daily
        WHERE delisted = false
        GROUP BY ticker
    )
    SELECT
        COUNT(*) AS active_tickers,
        COUNT(*) FILTER (WHERE last_bar < CURRENT_DATE - INTERVAL '{max_age} days')
            AS stale_tickers
    FROM per_ticker
"""


async def check_prices_daily_freshness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify per-ticker freshness for the critical roster + universe-wide."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    # ── Critical-ticker check ────────────────────────────────────────────
    async with pool.acquire() as conn:
        crit_rows = await conn.fetch(_CRITICAL_SQL, list(CRITICAL_TICKERS))
        univ_row = await conn.fetchrow(
            _UNIVERSE_SQL.format(max_age=UNIVERSE_MAX_AGE_DAYS)
        )

    from datetime import UTC, datetime
    today = datetime.now(UTC).date()
    for r in crit_rows:
        last_bar = r["last_bar"]
        ticker = r["ticker"]
        if last_bar is None:
            failures.append(FailureDetail(
                ticker=ticker,
                reason="missing_ticker",
                expected=f"recent bar within {CRITICAL_MAX_AGE_DAYS}d",
                observed="ticker has zero rows in prices_daily",
            ))
            continue
        age_days = (today - last_bar).days
        if age_days > CRITICAL_MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker=ticker,
                reason="critical_ticker_stale",
                expected=f"last bar within {CRITICAL_MAX_AGE_DAYS}d",
                observed=f"last bar {last_bar.isoformat()} ({age_days}d ago)",
            ))

    # ── Universe-wide staleness check ────────────────────────────────────
    if univ_row is not None:
        active = int(univ_row["active_tickers"] or 0)
        stale = int(univ_row["stale_tickers"] or 0)
        if active > 0:
            pct = stale / active
            if pct > UNIVERSE_STALE_PCT_MAX:
                failures.append(FailureDetail(
                    ticker="<universe>",
                    reason="universe_stale_excess",
                    expected=f"≤ {UNIVERSE_STALE_PCT_MAX:.1%} of active tickers stale > {UNIVERSE_MAX_AGE_DAYS}d",
                    observed=(
                        f"{stale}/{active} active tickers ({pct:.2%}) have "
                        f"last bar > {UNIVERSE_MAX_AGE_DAYS}d ago — "
                        f"check the daily_bars handler + delist_stale stage"
                    ),
                ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=len(CRITICAL_TICKERS) + 1,  # critical tickers + universe summary
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "CRITICAL_TICKERS",
    "CRITICAL_MAX_AGE_DAYS",
    "UNIVERSE_MAX_AGE_DAYS",
    "UNIVERSE_STALE_PCT_MAX",
    "check_prices_daily_freshness",
]
