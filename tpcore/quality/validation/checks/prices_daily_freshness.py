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

from tpcore.feeds import freshness_max_age_days
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
# Single-source-of-truth: tpcore.feeds.profile FeedProfile for prices_daily.
# A future bump in the profile propagates here automatically — no second
# place to forget to update. The 5d default is the SoT-fallback (drift
# sentinel would already be red if the profile entry were missing).
CRITICAL_MAX_AGE_DAYS = freshness_max_age_days("prices_daily", 5)
# Universe-wide max-age — looser than the critical-ticker window because
# some legitimate names (in-flight delistings before the delist_stale ops
# stage picks them up, low-liquidity tail tickers) can sit behind the
# critical window without being broken. 14d is the historical operator-
# tuned value; no separate FeedProfile entry exists for it (the profile
# tracks the critical/SLA threshold, not the universe-tail tolerance). If
# a profile facet ever models the universe-tail separately, this constant
# becomes a second freshness_max_age_days() call.
UNIVERSE_MAX_AGE_DAYS = 14
UNIVERSE_STALE_PCT_MAX = 0.02  # 2% of the active universe

# Coverage-collapse guard (added 2026-05-15 after the daily_bars
# incident). MAX(date) freshness is blind to a universe-wide coverage
# collapse: on 2026-05-11→14 the daily_bars stage stopped completing
# the full ~7,700-ticker universe and bar coverage fell to ~534
# tickers/day, but MAX(date) stayed current because the ~534 survivors
# included recent dates — so every freshness check passed while 91% of
# the universe silently went stale. This guard compares the most-recent
# fully-published trading day's distinct-ticker count against the
# trailing-20-session average; a drop past COVERAGE_COLLAPSE_PCT means
# the ingest is only covering a fraction of the universe even though
# "today" looks fresh.
COVERAGE_TRAILING_SESSIONS = 20
# Tightened 0.30 → 0.02 on 2026-05-25 (operator: "I keep saying 100% for
# the database... who says 70%?"). The earlier 0.30 (70% floor) was set
# 2026-05-15 to catch the catastrophic 91% collapse incident — but the
# threshold itself violated the "100% data or don't trade" rule: 70.01%
# coverage would PASS this gate while still missing ~2,300 tickers.
# 0.02 (98% floor) preserves the early-warning purpose (any catastrophic
# collapse still fails instantly) without tolerating large gaps. The
# actual per-ticker 100% invariant remains enforced by
# `prices_daily_completeness`; this gate is the coarse tripwire below it.
COVERAGE_COLLAPSE_PCT = 0.02   # >2% below trailing avg ticker-count = collapse


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

    # ── Coverage-collapse check ──────────────────────────────────────────
    # Most-recent fully-published session's distinct-ticker count vs the
    # trailing average. Catches the daily_bars-incident class where
    # MAX(date) is current but the ingest only covered a fraction of
    # the universe.
    #
    # Active-universe-aware denominator (operator catch 2026-05-25):
    # a ticker whose classification has `lifetime_end <= pd.date` is
    # RETIRED for that date and must NOT count against coverage. The
    # 2026-05-22 incident exposed this: 4 tickers retired on 2026-05-18
    # still had bars on 2026-05-21 (provider serves zombie post-delist
    # bars), pushing them into the "present prior, absent target" gap
    # even though they shouldn't trade going forward. The EXISTS clause
    # admits a ticker if ANY classification was active (lifetime_end IS
    # NULL OR > date) — supports the ticker-reuse SCD-2 semantics where
    # one ticker string may map to multiple classifications over time.
    async with pool.acquire() as conn:
        cov_rows = await conn.fetch(
            """
            SELECT pd.date, COUNT(DISTINCT pd.ticker) AS n
            FROM platform.prices_daily pd
            WHERE pd.delisted = false
              AND pd.date >= CURRENT_DATE - INTERVAL '40 days'
              AND EXISTS (
                  SELECT 1 FROM platform.ticker_classifications tc
                  WHERE tc.ticker = pd.ticker
                    AND (tc.lifetime_end IS NULL OR tc.lifetime_end > pd.date)
              )
            GROUP BY pd.date
            ORDER BY pd.date DESC
            LIMIT $1
            """,
            COVERAGE_TRAILING_SESSIONS + 1,
        )
    if len(cov_rows) >= 5:
        latest_n = int(cov_rows[0]["n"])
        trailing = [int(r["n"]) for r in cov_rows[1:]]
        if trailing:
            avg_trailing = sum(trailing) / len(trailing)
            if avg_trailing > 0 and latest_n < avg_trailing * (1 - COVERAGE_COLLAPSE_PCT):
                failures.append(FailureDetail(
                    ticker="<universe>",
                    reason="coverage_collapse",
                    expected=(
                        f"latest session ≥ {(1 - COVERAGE_COLLAPSE_PCT):.0%} of "
                        f"trailing-{len(trailing)}-session avg ({avg_trailing:,.0f} tickers)"
                    ),
                    observed=(
                        f"latest session {cov_rows[0]['date']} has {latest_n:,} tickers "
                        f"= {latest_n / avg_trailing:.0%} of trailing avg — daily_bars "
                        f"is only covering a fraction of the universe (MAX(date) is "
                        f"current but coverage collapsed)"
                    ),
                ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=len(CRITICAL_TICKERS) + 2,  # critical tickers + universe-stale + coverage
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
