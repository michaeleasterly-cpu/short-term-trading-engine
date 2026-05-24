"""MarketSnapshot assembler tests — Task #25 §10.1.

Covers:
- Regime classification (vol / trend / macro / sentiment) with synthetic substrates
- Cycle-position tagging (earnings season, year-end)
- Calendar-context helpers (third Friday)
- Byte-cap fence (SnapshotOverflowError fires on >512 KiB)
- Roster reads from tpcore.engine_profile (real call, not mocked)

Uses FakePool/FakeConn for the asyncpg-touching paths — same pattern
as the probe tests (sentinel.activation_probe) + ops package tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from tpcore.lab.llm_finder.models import (
    CalendarContext,
    MacroRow,
    MarketRegime,
    MarketSnapshot,
    PricePanelRow,
    RosterTarget,
    SentimentRow,
    _compute_regime_tuple_id,
)
from tpcore.lab.llm_finder.snapshot import (
    SnapshotOverflowError,
    _check_byte_cap,
    _classify_cycle_position,
    _classify_macro_regime,
    _classify_sentiment_regime,
    _classify_trend_regime,
    _classify_vol_regime,
    _read_roster,
    _third_friday,
    compute_market_regime,
)

# ───────────────────────── _classify_vol_regime ─────────────────────────


def test_vol_regime_bands() -> None:
    assert _classify_vol_regime(10.0) == "calm"
    assert _classify_vol_regime(17.0) == "normal"
    assert _classify_vol_regime(25.0) == "stress"
    assert _classify_vol_regime(35.0) == "crisis"


def test_vol_regime_missing_vix_defaults_normal() -> None:
    """Defensive default when VIX unavailable."""
    assert _classify_vol_regime(None) == "normal"


def test_vol_regime_boundary_15() -> None:
    """15.0 is the boundary calm→normal."""
    assert _classify_vol_regime(14.999) == "calm"
    assert _classify_vol_regime(15.0) == "normal"


# ───────────────────────── _classify_macro_regime ─────────────────────────


def test_macro_regime_sahm_contraction() -> None:
    """Sahm ≥ 0.50 → contraction."""
    assert _classify_macro_regime(0.55, None, None) == "contraction"
    assert _classify_macro_regime(0.49, None, None) == "expansion"


def test_macro_regime_cfnai_contraction() -> None:
    """CFNAI-MA3 ≤ -0.70 → contraction."""
    assert _classify_macro_regime(None, -0.80, None) == "contraction"
    assert _classify_macro_regime(None, -0.65, None) == "expansion"


def test_macro_regime_yield_curve_slowing() -> None:
    """Inverted yield curve (<0) → slowing (in absence of sahm/cfnai trigger)."""
    assert _classify_macro_regime(0.20, -0.40, -0.5) == "slowing"


def test_macro_regime_expansion_default() -> None:
    assert _classify_macro_regime(None, None, None) == "expansion"
    assert _classify_macro_regime(0.30, -0.20, 1.5) == "expansion"


# ───────────────────────── _classify_sentiment_regime ─────────────────────────


def _sentiment_row(bull: float, bear: float, fg: int) -> SentimentRow:
    return SentimentRow(
        as_of_date=date(2026, 5, 21),
        aaii_bull_pct=bull,
        aaii_bear_pct=bear,
        aaii_neutral_pct=1.0 - bull - bear,
        fear_greed_score=fg,
        apewisdom_mention_rank=None,
        ticker=None,
    )


def test_sentiment_regime_extreme_bull() -> None:
    """bull-bear > 0.50 AND F&G > 75 → extreme_bull."""
    assert _classify_sentiment_regime(_sentiment_row(0.65, 0.10, 80)) == "extreme_bull"


def test_sentiment_regime_extreme_bear() -> None:
    """bull-bear < -0.30 AND F&G < 25 → extreme_bear."""
    assert _classify_sentiment_regime(_sentiment_row(0.10, 0.50, 20)) == "extreme_bear"


def test_sentiment_regime_neutral_default() -> None:
    """Mid-range readings → neutral."""
    assert _classify_sentiment_regime(_sentiment_row(0.35, 0.30, 50)) == "neutral"
    assert _classify_sentiment_regime(None) == "neutral"


def test_sentiment_regime_partial_data_falls_to_neutral() -> None:
    """Missing one of (bull/bear/FG) → neutral default."""
    partial = SentimentRow(
        as_of_date=date(2026, 5, 21),
        aaii_bull_pct=0.65,
        aaii_bear_pct=0.10,
        aaii_neutral_pct=0.25,
        fear_greed_score=None,
        apewisdom_mention_rank=None,
        ticker=None,
    )
    assert _classify_sentiment_regime(partial) == "neutral"


# ───────────────────────── _classify_trend_regime ─────────────────────────


def _spy_window(closes: list[float]) -> tuple[PricePanelRow, ...]:
    """Build a SPY price window of N consecutive sessions."""
    base = date(2026, 1, 1)
    return tuple(
        PricePanelRow(
            ticker="SPY",
            session_date=date(base.year, base.month, (base.day + i - 1) % 28 + 1),
            adj_open=c,
            adj_high=c * 1.01,
            adj_low=c * 0.99,
            adj_close=c,
            volume=100_000,
            dollar_volume=c * 100_000,
            log_return=0.0,
            liquidity_tier="T1",
        )
        for i, c in enumerate(closes)
    )


def test_trend_regime_range_when_short_window() -> None:
    """< 200 SPY sessions → range default."""
    assert _classify_trend_regime(_spy_window([100.0] * 50)) == "range"
    assert _classify_trend_regime(()) == "range"


def test_trend_regime_range_when_flat() -> None:
    """Flat SPY (slope < 50bp) → range."""
    assert _classify_trend_regime(_spy_window([100.0] * 200)) == "range"


def test_trend_regime_trend_up() -> None:
    """SPY rising >50bp over 200 sessions → trend_up."""
    closes = [100.0 + i * 0.1 for i in range(200)]
    assert _classify_trend_regime(_spy_window(closes)) == "trend_up"


def test_trend_regime_trend_down() -> None:
    """SPY falling >50bp over 200 sessions → trend_down."""
    closes = [120.0 - i * 0.1 for i in range(200)]
    assert _classify_trend_regime(_spy_window(closes)) == "trend_down"


# ───────────────────────── _classify_cycle_position ─────────────────────────


def test_cycle_position_earnings_season() -> None:
    """Q1-end (April-May), Q2-end (Jul-Aug), Q3-end (Oct-Nov), Q4-end (Jan-Feb)."""
    for month in (4, 5, 7, 8, 10, 11, 1, 2):
        assert "earnings_season" in _classify_cycle_position(date(2026, month, 15))


def test_cycle_position_non_earnings() -> None:
    for month in (3, 6, 9):
        assert "earnings_season" not in _classify_cycle_position(date(2026, month, 15))


def test_cycle_position_year_end() -> None:
    """Last 12 days of December = year_end."""
    assert "year_end" in _classify_cycle_position(date(2026, 12, 22))
    assert "year_end" not in _classify_cycle_position(date(2026, 12, 5))


def test_cycle_position_normal_default() -> None:
    """No tags → 'normal' singleton."""
    assert _classify_cycle_position(date(2026, 6, 15)) == ("normal",)


# ───────────────────────── compute_market_regime ─────────────────────────


def test_compute_market_regime_end_to_end() -> None:
    """Full regime build from synthetic substrates."""
    macro = (
        MacroRow(series_id="vix", observation_date=date(2026, 5, 21), value=12.0),
        MacroRow(series_id="sahm_rule", observation_date=date(2026, 5, 21), value=0.20),
        MacroRow(series_id="cfnai_ma3", observation_date=date(2026, 5, 21), value=0.10),
        MacroRow(series_id="yield_curve", observation_date=date(2026, 5, 21), value=1.5),
    )
    sentiment = (_sentiment_row(0.30, 0.30, 50),)
    closes = [100.0 + i * 0.1 for i in range(200)]
    prices = _spy_window(closes)

    regime = compute_market_regime(macro, sentiment, prices, date(2026, 5, 21))
    assert regime.vol_regime == "calm"
    assert regime.trend_regime == "trend_up"
    assert regime.macro_regime == "expansion"
    assert regime.sentiment_regime == "neutral"
    assert regime.regime_tuple_id == _compute_regime_tuple_id("calm", "trend_up", "expansion", "neutral")


def test_compute_market_regime_crisis_contraction() -> None:
    """Stress signal across the board."""
    macro = (
        MacroRow(series_id="vix", observation_date=date(2026, 5, 21), value=35.0),
        MacroRow(series_id="sahm_rule", observation_date=date(2026, 5, 21), value=0.60),
        MacroRow(series_id="cfnai_ma3", observation_date=date(2026, 5, 21), value=-1.0),
        MacroRow(series_id="yield_curve", observation_date=date(2026, 5, 21), value=-0.5),
    )
    sentiment = (_sentiment_row(0.10, 0.50, 20),)
    regime = compute_market_regime(macro, sentiment, (), date(2026, 5, 21))
    assert regime.vol_regime == "crisis"
    assert regime.macro_regime == "contraction"
    assert regime.sentiment_regime == "extreme_bear"


# ───────────────────────── _third_friday ─────────────────────────


def test_third_friday_known_dates() -> None:
    """Third Friday calculation for known months."""
    assert _third_friday(2026, 5) == date(2026, 5, 15)
    assert _third_friday(2026, 12) == date(2026, 12, 18)
    assert _third_friday(2026, 1) == date(2026, 1, 16)


# ───────────────────────── _read_roster ─────────────────────────


def test_read_roster_pulls_from_engine_profile() -> None:
    """Roster reads via lab_targetable_engines (real, not mocked)."""
    roster = _read_roster()
    assert len(roster) >= 1
    assert all(isinstance(r, RosterTarget) for r in roster)
    engines = {r.engine for r in roster}
    # At least one paper engine present (operator-pinned 6 PAPER + carver LAB).
    assert engines & {"momentum", "reversion", "vector", "sentinel", "canary", "catalyst"}


# ───────────────────────── _check_byte_cap ─────────────────────────


def _build_minimal_snapshot(price_window_rows: int = 0) -> MarketSnapshot:
    regime = MarketRegime(
        vol_regime="normal",
        trend_regime="range",
        macro_regime="expansion",
        sentiment_regime="neutral",
        cycle_position=("normal",),
        regime_tuple_id=_compute_regime_tuple_id("normal", "range", "expansion", "neutral"),
    )
    return MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=date(2026, 5, 21),
        universe="sp500",
        market_regime=regime,
        calendar=CalendarContext(
            session_date=date(2026, 5, 21),
            is_earnings_season=False,
            is_fomc_week=False,
            is_opex_week=False,
            is_year_end_week=False,
            days_to_next_fomc=0,
            days_to_next_earnings_season=0,
        ),
        price_window=tuple(
            PricePanelRow(
                ticker=f"TICK{i:04d}",
                session_date=date(2026, 5, 21),
                adj_open=100.0,
                adj_high=101.0,
                adj_low=99.0,
                adj_close=100.5,
                volume=1_000_000,
                dollar_volume=100_500_000.0,
                log_return=0.005,
                liquidity_tier="T1",
            )
            for i in range(price_window_rows)
        ),
        fundamentals=(),
        spreads=(),
        sentiment=(),
        macro=(),
        ledger_state=(),
        roster=(),
    )


def test_byte_cap_passes_for_small_snapshot() -> None:
    """Empty snapshot well under 512 KiB."""
    snap = _build_minimal_snapshot(price_window_rows=0)
    _check_byte_cap(snap)


def test_byte_cap_fails_on_overflow() -> None:
    """A 4000-row price window serializes past 512 KiB → fail-loud."""
    # Each PricePanelRow JSON is ~130 bytes; 4000 rows ≈ 520 KiB.
    snap = _build_minimal_snapshot(price_window_rows=4000)
    with pytest.raises(SnapshotOverflowError, match="cap is"):
        _check_byte_cap(snap)


# ───────────────────────── FakePool integration smoke ─────────────────────────


def _macro_data_rows_from_fixtures(
    fixtures: dict[str, list[dict[str, Any]]],
    args: tuple[Any, ...],
) -> list[dict[str, Any]]:
    """Translate the legacy fixture shape into platform.macro_data rows.

    Post-refactor (PR-5), snapshot.py + sentinel read macro_data via
    MacroRepo. The SQL is `platform.macro_data ... source = $4` (or no
    source filter). args[3] holds the source when filtered; route the
    matching legacy fixture and reshape.
    """
    source = args[3] if len(args) >= 4 else None
    if source == "fred":
        bucket_key = "macro"
    elif source == "aaii":
        bucket_key = "aaii"
    elif source == "cnn_fear_greed":
        bucket_key = "fear_greed"
    else:
        return []
    rows: list[dict[str, Any]] = []
    for r in fixtures.get(bucket_key, []):
        # Legacy fixture shape carries either (series_id, observation_date,
        # value) for macro/macro_indicators or (date, bullish_pct, ...)
        # for the aaii/fear_greed wide rows. The new repo returns long
        # shape with value_num.
        if "series_id" in r:
            rows.append(
                {
                    "series_id": r["series_id"],
                    "observed_date": r["observation_date"],
                    "value_num": r.get("value"),
                    "value_text": None,
                    "source": source,
                }
            )
        elif source == "aaii":
            for s in ("bullish_pct", "bearish_pct", "neutral_pct"):
                if s in r and r[s] is not None:
                    rows.append(
                        {
                            "series_id": s,
                            "observed_date": r.get("date") or r.get("observation_date"),
                            "value_num": r[s],
                            "value_text": None,
                            "source": "aaii",
                        }
                    )
        elif source == "cnn_fear_greed":
            if "score" in r:
                rows.append(
                    {
                        "series_id": "score",
                        "observed_date": r.get("date") or r.get("observation_date"),
                        "value_num": r["score"],
                        "value_text": None,
                        "source": "cnn_fear_greed",
                    }
                )
    return rows


class _FakeConn:
    def __init__(self, fixtures: dict[str, list[dict[str, Any]]]) -> None:
        self._fixtures = fixtures

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        # Route by SQL fragment.
        if "universe_membership" in sql:
            return self._fixtures.get("universe", [])
        if "prices_daily" in sql:
            return self._fixtures.get("prices", [])
        if "fundamentals_quarterly" in sql:
            return self._fixtures.get("fundamentals", [])
        if "spread_observations" in sql:
            return self._fixtures.get("spreads", [])
        if "lab_trial_ledger_by_regime" in sql:
            return self._fixtures.get("ledger", [])
        if "earnings_events" in sql:
            return self._fixtures.get("earnings", [])
        # Post-refactor macro reads hit platform.macro_data. The fake
        # routes by the bound source (4th positional arg of the
        # source-filtered MacroRepo SQL) so test fixtures stay shaped
        # the same as before the refactor.
        if "platform.macro_data" in sql:
            return _macro_data_rows_from_fixtures(self._fixtures, args)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


class _AcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, fixtures: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._fixtures = fixtures or {}

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_FakeConn(self._fixtures))


@pytest.mark.asyncio
async def test_assemble_snapshot_empty_substrates() -> None:
    """Degenerate path: empty universe + empty substrates → still assembles."""
    from tpcore.lab.llm_finder.snapshot import assemble_snapshot

    pool = _FakePool(fixtures={})
    snap = await assemble_snapshot(pool, session_date=date(2026, 5, 21))  # type: ignore[arg-type]

    assert snap.universe == "sp500"
    assert snap.session_date == date(2026, 5, 21)
    assert snap.price_window == ()
    assert snap.market_regime.vol_regime == "normal"  # default w/o VIX
    assert snap.market_regime.trend_regime == "range"  # default w/o SPY


@pytest.mark.asyncio
async def test_assemble_snapshot_with_macro_signal() -> None:
    """VIX → vol_regime classification through to the assembled snapshot."""
    from tpcore.lab.llm_finder.snapshot import assemble_snapshot

    pool = _FakePool(
        fixtures={
            "macro": [
                {"series_id": "vix", "observation_date": date(2026, 5, 21), "value": 35.0},
            ],
        }
    )
    snap = await assemble_snapshot(pool, session_date=date(2026, 5, 21))  # type: ignore[arg-type]
    assert snap.market_regime.vol_regime == "crisis"
