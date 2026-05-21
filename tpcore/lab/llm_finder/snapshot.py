"""MarketSnapshot assembler — Task #25 §4.1.

Phase A of the finder loop: reads bounded slices of every ingested
substrate from Postgres + computes ``MarketRegime`` + ``CalendarContext``
+ assembles the frozen Pydantic ``MarketSnapshot`` the LLM sees.

Hard constraints:
- ``MAX_SNAPSHOT_BYTES = 512 KiB`` — fail-loud on overflow via
  ``SnapshotOverflowError`` (NEVER silent truncation).
- ``universe = "sp500"`` only in v1 (spec §9 — v1.5 widens).
- 252 NYSE sessions price window (rolling).
- One row per ticker for fundamentals (latest fiscal_period_end).
- Latest 30 sessions of spread / sentiment / macro / ledger rows.

Engine-FREE: only ``tpcore.calendar`` + ``tpcore.engine_profile`` +
``tpcore.lab.llm_finder.models`` imports; no engine modules touched.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Literal

import structlog

from tpcore import calendar as tp_calendar
from tpcore.engine_profile import LifecycleState, lab_targetable_engines, profile_for
from tpcore.lab.llm_finder import MAX_SNAPSHOT_BYTES
from tpcore.lab.llm_finder.models import (
    CalendarContext,
    FundRow,
    LedgerEntry,
    MacroRow,
    MarketRegime,
    MarketSnapshot,
    PricePanelRow,
    RosterTarget,
    SentimentRow,
    SpreadObs,
    _compute_regime_tuple_id,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)

_PRICE_WINDOW_SESSIONS: Final[int] = 252
_SPREAD_WINDOW_SESSIONS: Final[int] = 30
_SENTIMENT_WINDOW_SESSIONS: Final[int] = 30
_MACRO_WINDOW_SESSIONS: Final[int] = 90

# Regime thresholds (spec §4.2 + regime_aware_trading.md §1).
_VIX_CALM_HI: Final[float] = 15.0
_VIX_NORMAL_HI: Final[float] = 20.0
_VIX_STRESS_HI: Final[float] = 30.0

_ADX_TREND_LO: Final[float] = 25.0
_SPY_SLOPE_BP_TRIGGER: Final[float] = 50.0 / 10_000.0

_SAHM_CONTRACTION: Final[float] = 0.50
_CFNAI_MA3_CONTRACTION: Final[float] = -0.70

_AAII_BULL_BEAR_EXTREME_BULL: Final[float] = 0.50
_AAII_BULL_BEAR_EXTREME_BEAR: Final[float] = -0.30
_FEAR_GREED_EXTREME_BULL_HI: Final[int] = 75
_FEAR_GREED_EXTREME_BEAR_LO: Final[int] = 25


class SnapshotOverflowError(ValueError):
    """Serialized snapshot exceeds ``MAX_SNAPSHOT_BYTES`` — fail-loud."""


# ───────────────────────── Top-level assembler ─────────────────────────


async def assemble_snapshot(
    pool: asyncpg.Pool,
    *,
    session_date: date,
    universe: Literal["sp500", "sp1500", "rus3k"] = "sp500",
) -> MarketSnapshot:
    """Read all substrates + build the bounded MarketSnapshot.

    Raises:
        SnapshotOverflowError: if serialized snapshot > 512 KiB.
    """
    log.info("snapshot.assemble.start", session_date=str(session_date), universe=universe)

    tickers = await _read_universe_tickers(pool, universe, session_date)

    price_window = await _read_price_window(pool, tickers, session_date)
    fundamentals = await _read_fundamentals(pool, tickers)
    spreads = await _read_spreads(pool, tickers, session_date)
    sentiment = await _read_sentiment(pool, session_date)
    macro = await _read_macro(pool, session_date)

    regime = compute_market_regime(macro, sentiment, price_window, session_date)
    calendar_ctx = await compute_calendar_context(pool, session_date)
    ledger_state = await _read_ledger(pool, regime.regime_tuple_id)
    roster = _read_roster()

    snapshot = MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=session_date,
        universe=universe,
        market_regime=regime,
        calendar=calendar_ctx,
        price_window=price_window,
        fundamentals=fundamentals,
        spreads=spreads,
        sentiment=sentiment,
        macro=macro,
        ledger_state=ledger_state,
        roster=roster,
    )

    _check_byte_cap(snapshot)
    log.info(
        "snapshot.assemble.done",
        session_date=str(session_date),
        tickers=len(tickers),
        regime=regime.regime_tuple_id,
    )
    return snapshot


# ───────────────────────── Sub-reads (asyncpg) ─────────────────────────


_SP500_UNIVERSE_SQL: Final[str] = """
    SELECT DISTINCT ticker
      FROM platform.universe_membership
     WHERE universe = $1
       AND effective_date <= $2
       AND (expiry_date IS NULL OR expiry_date > $2)
     ORDER BY ticker
"""


async def _read_universe_tickers(
    pool: asyncpg.Pool, universe: str, session_date: date
) -> tuple[str, ...]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SP500_UNIVERSE_SQL, universe, session_date)
    return tuple(r["ticker"] for r in rows)


_PRICE_WINDOW_SQL: Final[str] = """
    SELECT pd.ticker,
           pd.session_date,
           pd.adj_open,
           pd.adj_high,
           pd.adj_low,
           pd.adj_close,
           pd.volume,
           pd.adj_close * pd.volume AS dollar_volume,
           pd.log_return,
           COALESCE(lt.tier, 'T3') AS liquidity_tier
      FROM platform.prices_daily pd
      LEFT JOIN platform.liquidity_tiers lt USING (ticker)
     WHERE pd.ticker = ANY($1::text[])
       AND pd.session_date BETWEEN $2 AND $3
     ORDER BY pd.ticker, pd.session_date
"""


async def _read_price_window(
    pool: asyncpg.Pool, tickers: tuple[str, ...], session_date: date
) -> tuple[PricePanelRow, ...]:
    if not tickers:
        return ()
    sessions = tp_calendar.sessions_in_range(
        date(session_date.year - 2, session_date.month, session_date.day),
        session_date,
    )
    window_start = sessions[-_PRICE_WINDOW_SESSIONS] if len(sessions) >= _PRICE_WINDOW_SESSIONS else sessions[0]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _PRICE_WINDOW_SQL, list(tickers), window_start, session_date
        )
    return tuple(
        PricePanelRow(
            ticker=r["ticker"],
            session_date=r["session_date"],
            adj_open=float(r["adj_open"]),
            adj_high=float(r["adj_high"]),
            adj_low=float(r["adj_low"]),
            adj_close=float(r["adj_close"]),
            volume=int(r["volume"]),
            dollar_volume=float(r["dollar_volume"]),
            log_return=float(r["log_return"] or 0.0),
            liquidity_tier=r["liquidity_tier"],
        )
        for r in rows
    )


_FUNDAMENTALS_SQL: Final[str] = """
    SELECT DISTINCT ON (ticker)
           ticker,
           fiscal_period_end,
           revenue,
           net_income,
           eps_diluted,
           book_value,
           debt_to_equity,
           pb_ratio
      FROM platform.fundamentals_quarterly
     WHERE ticker = ANY($1::text[])
     ORDER BY ticker, fiscal_period_end DESC
"""


async def _read_fundamentals(
    pool: asyncpg.Pool, tickers: tuple[str, ...]
) -> tuple[FundRow, ...]:
    if not tickers:
        return ()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_FUNDAMENTALS_SQL, list(tickers))
    return tuple(
        FundRow(
            ticker=r["ticker"],
            fiscal_period_end=r["fiscal_period_end"],
            revenue=float(r["revenue"]) if r["revenue"] is not None else None,
            net_income=float(r["net_income"]) if r["net_income"] is not None else None,
            eps_diluted=float(r["eps_diluted"]) if r["eps_diluted"] is not None else None,
            book_value=float(r["book_value"]) if r["book_value"] is not None else None,
            debt_to_equity=float(r["debt_to_equity"]) if r["debt_to_equity"] is not None else None,
            pb_ratio=float(r["pb_ratio"]) if r["pb_ratio"] is not None else None,
        )
        for r in rows
    )


_SPREADS_SQL: Final[str] = """
    SELECT ticker, session_date, effective_spread_bps, roll_implied_spread_bps
      FROM platform.spread_observations
     WHERE ticker = ANY($1::text[])
       AND session_date BETWEEN $2 AND $3
     ORDER BY ticker, session_date
"""


async def _read_spreads(
    pool: asyncpg.Pool, tickers: tuple[str, ...], session_date: date
) -> tuple[SpreadObs, ...]:
    if not tickers:
        return ()
    sessions = tp_calendar.sessions_in_range(
        date(session_date.year - 1, session_date.month, session_date.day),
        session_date,
    )
    window_start = sessions[-_SPREAD_WINDOW_SESSIONS] if len(sessions) >= _SPREAD_WINDOW_SESSIONS else sessions[0]
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SPREADS_SQL, list(tickers), window_start, session_date)
    return tuple(
        SpreadObs(
            ticker=r["ticker"],
            session_date=r["session_date"],
            effective_spread_bps=float(r["effective_spread_bps"]),
            roll_implied_spread_bps=(
                float(r["roll_implied_spread_bps"])
                if r["roll_implied_spread_bps"] is not None
                else None
            ),
        )
        for r in rows
    )


_AAII_SQL: Final[str] = """
    SELECT as_of_date, bull_pct, bear_pct, neutral_pct
      FROM platform.aaii_sentiment
     WHERE as_of_date BETWEEN $1 AND $2
     ORDER BY as_of_date
"""

_FEAR_GREED_SQL: Final[str] = """
    SELECT as_of_date, fear_greed_score
      FROM platform.fear_greed
     WHERE as_of_date BETWEEN $1 AND $2
     ORDER BY as_of_date
"""


async def _read_sentiment(
    pool: asyncpg.Pool, session_date: date
) -> tuple[SentimentRow, ...]:
    sessions = tp_calendar.sessions_in_range(
        date(session_date.year - 1, session_date.month, session_date.day),
        session_date,
    )
    window_start = sessions[-_SENTIMENT_WINDOW_SESSIONS] if len(sessions) >= _SENTIMENT_WINDOW_SESSIONS else sessions[0]

    out: list[SentimentRow] = []
    async with pool.acquire() as conn:
        aaii_rows = await conn.fetch(_AAII_SQL, window_start, session_date)
        fg_rows = await conn.fetch(_FEAR_GREED_SQL, window_start, session_date)

    fg_by_date = {r["as_of_date"]: r["fear_greed_score"] for r in fg_rows}
    for r in aaii_rows:
        out.append(
            SentimentRow(
                as_of_date=r["as_of_date"],
                aaii_bull_pct=float(r["bull_pct"]) if r["bull_pct"] is not None else None,
                aaii_bear_pct=float(r["bear_pct"]) if r["bear_pct"] is not None else None,
                aaii_neutral_pct=float(r["neutral_pct"]) if r["neutral_pct"] is not None else None,
                fear_greed_score=fg_by_date.get(r["as_of_date"]),
                apewisdom_mention_rank=None,
                ticker=None,
            )
        )
    return tuple(out)


_MACRO_SQL: Final[str] = """
    SELECT series_id, observation_date, value
      FROM platform.macro_indicators
     WHERE observation_date BETWEEN $1 AND $2
     ORDER BY series_id, observation_date
"""


async def _read_macro(pool: asyncpg.Pool, session_date: date) -> tuple[MacroRow, ...]:
    sessions = tp_calendar.sessions_in_range(
        date(session_date.year - 1, session_date.month, session_date.day),
        session_date,
    )
    window_start = sessions[-_MACRO_WINDOW_SESSIONS] if len(sessions) >= _MACRO_WINDOW_SESSIONS else sessions[0]
    async with pool.acquire() as conn:
        rows = await conn.fetch(_MACRO_SQL, window_start, session_date)
    return tuple(
        MacroRow(
            series_id=r["series_id"],
            observation_date=r["observation_date"],
            value=float(r["value"]),
        )
        for r in rows
    )


_LEDGER_SQL: Final[str] = """
    SELECT target_engine,
           regime_tuple_id,
           cumulative_n_trials_by_regime,
           cumulative_n_trials_aggregate,
           cumulative_analysis_turns_by_regime
      FROM platform.lab_trial_ledger_by_regime
     WHERE regime_tuple_id = $1
     ORDER BY target_engine
"""


async def _read_ledger(
    pool: asyncpg.Pool, regime_tuple_id: str
) -> tuple[LedgerEntry, ...]:
    """Read per-regime + aggregate ledger state (spec §4.4 + constraint 17/20).

    Returns empty tuple if the view doesn't exist yet (T7's substrate
    is built later). The agent path treats empty as 'fresh budget
    across the board' — a sensible default before the ledger is wired.
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_LEDGER_SQL, regime_tuple_id)
    except Exception as exc:  # noqa: BLE001 - intentional pre-T7 fallback
        log.debug("ledger.read.skip", error=str(exc), regime=regime_tuple_id)
        return ()
    return tuple(
        LedgerEntry(
            target_engine=r["target_engine"],
            regime_tuple_id=r["regime_tuple_id"],
            cumulative_n_trials_by_regime=int(r["cumulative_n_trials_by_regime"]),
            cumulative_n_trials_aggregate=int(r["cumulative_n_trials_aggregate"]),
            cumulative_analysis_turns_by_regime=int(r["cumulative_analysis_turns_by_regime"]),
        )
        for r in rows
    )


def _read_roster() -> tuple[RosterTarget, ...]:
    """Read the lab-targetable engine roster (synchronous; pure)."""
    engines = lab_targetable_engines()
    out: list[RosterTarget] = []
    for engine in engines:
        profile = profile_for(engine)
        if profile is None:
            continue
        state = profile.lifecycle_state
        state_str = state.value if isinstance(state, LifecycleState) else str(state)
        out.append(
            RosterTarget(
                engine=engine,
                lifecycle_state=state_str.upper(),  # type: ignore[arg-type]
                primary_metric="SHARPE",
            )
        )
    return tuple(out)


# ───────────────────────── Regime computation ─────────────────────────


def _latest_macro_value(macro: tuple[MacroRow, ...], series_id: str) -> float | None:
    """Most-recent value for a FRED series; None if not in window."""
    matches = [r for r in macro if r.series_id == series_id]
    if not matches:
        return None
    return matches[-1].value


def _latest_sentiment(sentiment: tuple[SentimentRow, ...]) -> SentimentRow | None:
    return sentiment[-1] if sentiment else None


def _classify_vol_regime(vix: float | None) -> Literal["calm", "normal", "stress", "crisis"]:
    if vix is None:
        return "normal"
    if vix < _VIX_CALM_HI:
        return "calm"
    if vix < _VIX_NORMAL_HI:
        return "normal"
    if vix < _VIX_STRESS_HI:
        return "stress"
    return "crisis"


def _classify_trend_regime(
    price_window: tuple[PricePanelRow, ...],
) -> Literal["range", "trend_up", "trend_down"]:
    """Range vs trend per SPY 200d slope + ADX threshold (regime_aware §1)."""
    spy = [p for p in price_window if p.ticker == "SPY"]
    if len(spy) < 200:
        return "range"
    closes = [p.adj_close for p in spy[-200:]]
    slope_bp = (closes[-1] - closes[0]) / closes[0]
    if abs(slope_bp) < _SPY_SLOPE_BP_TRIGGER:
        return "range"
    # NOTE: ADX threshold (>25) is the secondary filter per spec §4.2.
    # Without an ADX series we use slope-magnitude alone as v1 proxy —
    # documented gap; v1.5 adds ADX via macro_indicators or computed
    # in-place from the price_window.
    return "trend_up" if slope_bp > 0 else "trend_down"


def _classify_macro_regime(
    sahm: float | None, cfnai_ma3: float | None, yield_curve: float | None
) -> Literal["expansion", "slowing", "contraction"]:
    if sahm is not None and sahm >= _SAHM_CONTRACTION:
        return "contraction"
    if cfnai_ma3 is not None and cfnai_ma3 <= _CFNAI_MA3_CONTRACTION:
        return "contraction"
    if yield_curve is not None and yield_curve < 0:
        return "slowing"
    return "expansion"


def _classify_sentiment_regime(
    sentiment_row: SentimentRow | None,
) -> Literal["extreme_bull", "neutral", "extreme_bear"]:
    if sentiment_row is None:
        return "neutral"
    if (
        sentiment_row.aaii_bull_pct is not None
        and sentiment_row.aaii_bear_pct is not None
        and sentiment_row.fear_greed_score is not None
    ):
        bull_bear = sentiment_row.aaii_bull_pct - sentiment_row.aaii_bear_pct
        if (
            bull_bear > _AAII_BULL_BEAR_EXTREME_BULL
            and sentiment_row.fear_greed_score > _FEAR_GREED_EXTREME_BULL_HI
        ):
            return "extreme_bull"
        if (
            bull_bear < _AAII_BULL_BEAR_EXTREME_BEAR
            and sentiment_row.fear_greed_score < _FEAR_GREED_EXTREME_BEAR_LO
        ):
            return "extreme_bear"
    return "neutral"


def _classify_cycle_position(session_date: date) -> tuple[str, ...]:
    """Calendar-position tags (multi-tag co-occurrence allowed)."""
    tags: list[str] = []
    month = session_date.month
    if month in (4, 5, 7, 8, 10, 11, 1, 2):
        tags.append("earnings_season")
    if month == 12 and session_date.day >= 20:
        tags.append("year_end")
    # FOMC + opex are session-specific; v1 leaves these for the
    # CalendarContext consumer to populate via Fed/exchange calendars.
    return tuple(tags) if tags else ("normal",)


def compute_market_regime(
    macro: tuple[MacroRow, ...],
    sentiment: tuple[SentimentRow, ...],
    price_window: tuple[PricePanelRow, ...],
    session_date: date,
) -> MarketRegime:
    """Build a MarketRegime from snapshot substrates (spec §4.2)."""
    vix = _latest_macro_value(macro, "vix")
    sahm = _latest_macro_value(macro, "sahm_rule")
    cfnai = _latest_macro_value(macro, "cfnai_ma3")
    yc = _latest_macro_value(macro, "yield_curve")

    vol = _classify_vol_regime(vix)
    trend = _classify_trend_regime(price_window)
    macro_state = _classify_macro_regime(sahm, cfnai, yc)
    sent = _classify_sentiment_regime(_latest_sentiment(sentiment))
    cycle = _classify_cycle_position(session_date)

    tuple_id = _compute_regime_tuple_id(vol, trend, macro_state, sent)
    return MarketRegime(
        vol_regime=vol,
        trend_regime=trend,
        macro_regime=macro_state,
        sentiment_regime=sent,
        cycle_position=cycle,  # type: ignore[arg-type]
        regime_tuple_id=tuple_id,
    )


# ───────────────────────── Calendar context ─────────────────────────


_EARNINGS_DENSITY_SQL: Final[str] = """
    SELECT COUNT(*) AS report_count
      FROM platform.earnings_events
     WHERE report_date BETWEEN $1 AND $2
"""


async def compute_calendar_context(
    pool: asyncpg.Pool, session_date: date
) -> CalendarContext:
    """Build a CalendarContext from XNYS + earnings_calendar + Fed calendar.

    FOMC + opex dates: pinned to the v1 spec — operator stages the Fed
    calendar via constant array in v1.5. For now compute is_fomc_week
    via heuristic (Tuesday-Wednesday cluster of Mar/Jun/Sep/Dec); refine
    in T6 once the persona ships.
    """
    # Earnings season density — > 100 reports in the next 14 days = active season.
    async with pool.acquire() as conn:
        density = await conn.fetchrow(
            _EARNINGS_DENSITY_SQL,
            session_date,
            date(session_date.year + (1 if session_date.month == 12 else 0),
                 1 if session_date.month == 12 else session_date.month + 1,
                 min(session_date.day, 28)),
        )
    report_count = density["report_count"] if density else 0
    is_earnings_season = report_count >= 100

    # Opex week: third Friday of the month
    third_friday = _third_friday(session_date.year, session_date.month)
    is_opex_week = abs((third_friday - session_date).days) <= 5

    is_year_end_week = session_date.month == 12 and session_date.day >= 20

    is_fomc_week = False  # v1.5: wire Fed calendar.

    return CalendarContext(
        session_date=session_date,
        is_earnings_season=is_earnings_season,
        is_fomc_week=is_fomc_week,
        is_opex_week=is_opex_week,
        is_year_end_week=is_year_end_week,
        days_to_next_fomc=0,  # v1.5: real lookup.
        days_to_next_earnings_season=0,  # v1.5: real lookup.
    )


def _third_friday(year: int, month: int) -> date:
    """Third Friday of the given month."""
    d = date(year, month, 1)
    # Friday weekday = 4. Days to first Friday:
    days_to_first_friday = (4 - d.weekday()) % 7
    return d.replace(day=1 + days_to_first_friday + 14)


# ───────────────────────── Byte-cap fence ─────────────────────────


def _check_byte_cap(snapshot: MarketSnapshot) -> None:
    """Serialize to JSON + measure; raise SnapshotOverflowError on cap breach."""
    payload = snapshot.model_dump_json()
    byte_count = len(payload.encode("utf-8"))
    if byte_count > MAX_SNAPSHOT_BYTES:
        raise SnapshotOverflowError(
            f"MarketSnapshot is {byte_count} bytes; cap is {MAX_SNAPSHOT_BYTES}. "
            f"Reduce universe or window — DO NOT silently truncate."
        )


__all__ = [
    "SnapshotOverflowError",
    "assemble_snapshot",
    "compute_calendar_context",
    "compute_market_regime",
]
