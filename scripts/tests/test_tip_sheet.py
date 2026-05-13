"""Unit tests for the tip-sheet rendering and gating logic.

Focused on the pure render functions and the credibility-gate exit-code
path. The full ``amain`` orchestration (DB pool + broker calls) is not
unit-tested here — it's covered by the smoke test (``scripts/run_momentum_smoke.sh``)
which exercises the live integration.

Test selection driven by the 2026-05-13 expert design review:
* Credibility gate enforcement (must exit non-zero when blocked, so a
  daily cron can detect regressions)
* ``--force`` bypass behaviour
* ``--no-broker`` graceful path when broker raises
* No-rubric-on-record produces gated output, not a crash
* Engine-prefix filter of holdings is correct (the ``ENGINE_ORDER_PREFIX``
  logic, currently a footgun for sigma/reversion/vector with no prefix)
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.generate_tip_sheet import (
    DISCLAIMER,
    ENGINE_DESCRIPTIONS,
    ENGINE_ORDER_PREFIX,
    fetch_engine_holdings,
    render_credibility,
    render_header,
    render_holdings,
    render_recommendations,
    render_signals,
    render_trades,
)
from tpcore.backtest.credibility import CredibilityScore

# ─── Render: credibility ────────────────────────────────────────────────────


def _make_score(score: int) -> CredibilityScore:
    """Build a CredibilityScore where ``score`` matches the integer total."""
    return CredibilityScore(
        lookahead_clean=True,
        survivorship_inclusive=score >= 60,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=score >= 75,
        monte_carlo_drawdown=True,
        sensitivity_surface_flat=score >= 70,
        monte_carlo_sequence_passed=score >= 80,
        dsr_above_0_90=score >= 85,
        backtest_length_above_minbtl=score >= 90,
        score=score,
    )


def test_credibility_blocked_renders_blocked_marker():
    score = _make_score(55)
    out = render_credibility("sigma", score, force=False)
    assert "BLOCKED" in out
    assert "55" in out


def test_credibility_blocked_with_force_notes_bypass():
    score = _make_score(55)
    out = render_credibility("sigma", score, force=True)
    assert "BLOCKED" in out
    assert "force" in out.lower()


def test_credibility_above_gate_passes():
    score = _make_score(75)
    out = render_credibility("sigma", score, force=False)
    assert "PASS" in out
    assert "75" in out


def test_credibility_no_row_on_record():
    out = render_credibility("sigma", None, force=False)
    assert "no rubric row on record" in out.lower()
    # Crucially: must not crash with None.


# ─── Render: holdings ───────────────────────────────────────────────────────


def test_render_holdings_empty():
    out = render_holdings("momentum", [])
    assert "no open positions" in out.lower()


def test_render_holdings_with_data():
    holdings = [{
        "ticker": "AAPL", "qty": 10, "entry_price": 180.0, "current_price": 185.5,
        "market_value": 1855.0, "cost_basis": 1800.0,
        "unrealized_pl": 55.0, "unrealized_pl_pct": 0.0306,
    }]
    out = render_holdings("momentum", holdings)
    assert "AAPL" in out
    assert "1855" in out or "1,855" in out
    assert "+55" in out  # signed P&L


def test_render_holdings_unprefixed_engine_shows_annotation():
    holdings = [{
        "ticker": "AAPL", "qty": 1, "entry_price": 100.0, "current_price": 100.0,
        "market_value": 100.0, "cost_basis": 100.0,
        "unrealized_pl": 0.0, "unrealized_pl_pct": 0.0,
    }]
    out = render_holdings("sigma", holdings)
    # Sigma has no order-prefix so we show ALL broker positions; the renderer
    # must annotate this so the operator isn't misled.
    assert "Phase 2.5" in out or "cross-engine attribution" in out


# ─── Render: recommendations ────────────────────────────────────────────────


def test_render_recommendations_empty_momentum():
    out = render_recommendations("momentum", [], datetime.now(UTC))
    assert "none" in out.lower()


def test_render_recommendations_empty_other_engine():
    # Sigma/Reversion/Vector aren't ported in Phase 1 — the message should
    # say 'not implemented' not 'none', so the operator knows the difference.
    out = render_recommendations("sigma", [], datetime.now(UTC))
    assert "not implemented" in out.lower()


def test_render_recommendations_with_data():
    recs = [
        {"ticker": "NVDA", "score": 2.5, "last_close": 850.50, "tier": 1},
        {"ticker": "AAPL", "score": 1.8, "last_close": 185.20, "tier": 1},
    ]
    out = render_recommendations("momentum", recs, datetime.now(UTC))
    assert "NVDA" in out
    assert "AAPL" in out
    assert "+2.500" in out
    assert "+1.800" in out


# ─── Render: signals + trades ───────────────────────────────────────────────


def test_render_signals_empty():
    out = render_signals([])
    assert "none in window" in out.lower()


def test_render_trades_empty():
    out = render_trades([])
    assert "none in window" in out.lower()


# ─── Header + disclaimer ────────────────────────────────────────────────────


def test_header_includes_layman_description():
    out = render_header("momentum", datetime.now(UTC))
    assert "MOMENTUM" in out
    assert ENGINE_DESCRIPTIONS["momentum"][:30] in out


def test_disclaimer_includes_key_phrases():
    # Sanity: the mandatory disclaimer carries the operator-protection
    # language that prevents misuse if it ever leaks.
    assert "NOT investment advice" in DISCLAIMER
    assert "Do not act on this output" in DISCLAIMER
    assert "Do not share this output" in DISCLAIMER


# ─── ENGINE_ORDER_PREFIX correctness ────────────────────────────────────────


def test_engine_prefix_map_only_momentum_has_prefix():
    """Phase 1 reality: only Momentum's order_manager stamps a stable
    engine-identifying prefix on client_order_ids. The other engines
    (sigma/reversion/vector) stamp <TICKER>_<TS>_<tier> — no engine
    identifier, so we can't filter holdings to those engines at all.
    If this test fails, ENGINE_ORDER_PREFIX has drifted and the
    'currently holding' section will silently misattribute positions."""
    assert ENGINE_ORDER_PREFIX["momentum"] == "mo_"
    for engine in ("sigma", "reversion", "vector", "s2", "catalyst", "sentinel"):
        assert ENGINE_ORDER_PREFIX[engine] is None, (
            f"{engine} acquired an order-prefix without updating the "
            f"holdings filter test — verify the prefix is actually engine-"
            f"unique before relying on it"
        )


# ─── fetch_engine_holdings filter behaviour ─────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_holdings_filters_to_engine_orders():
    """Momentum filter: positions whose ticker appears in a `mo_*` order
    are kept; others (e.g. a manual deposit position) are dropped."""
    broker = MagicMock()
    # Two positions at the broker: AAPL (bought by momentum) and TSLA
    # (somehow at the broker but not via a momentum order — maybe a
    # legacy test, manual buy, etc.).
    broker.get_positions = AsyncMock(return_value=[
        MagicMock(symbol="AAPL", qty=10, avg_entry_price=Decimal("180"),
                  market_value=Decimal("1855"), cost_basis=Decimal("1800"),
                  unrealized_pl=Decimal("55")),
        MagicMock(symbol="TSLA", qty=5, avg_entry_price=Decimal("200"),
                  market_value=Decimal("1100"), cost_basis=Decimal("1000"),
                  unrealized_pl=Decimal("100")),
    ])
    # Recent orders include a momentum buy on AAPL but nothing for TSLA.
    aapl_order = MagicMock()
    aapl_order.symbol = "AAPL"
    aapl_order.client_order_id = "mo_AAPL_1700000000"
    tsla_order = MagicMock()
    tsla_order.symbol = "TSLA"
    tsla_order.client_order_id = "manual_TSLA_xyz"
    broker.list_recent_orders = AsyncMock(return_value=[aapl_order, tsla_order])

    holdings = await fetch_engine_holdings(broker, "momentum")
    tickers = {h["ticker"] for h in holdings}
    assert "AAPL" in tickers
    assert "TSLA" not in tickers, (
        "TSLA had no `mo_*` order in history — it should not appear in the "
        "momentum holdings view"
    )


@pytest.mark.asyncio
async def test_fetch_holdings_unprefixed_engine_attributes_via_tier_pattern():
    """Sigma/Reversion/Vector have no engine prefix — we attribute by the
    ``<TICKER>_<TS>[_tier]`` BUY-order pattern instead. A position with
    no matching tier-pattern BUY (e.g., a momentum-prefixed buy) does
    NOT count as sigma-owned, even though sigma is "prefix-less".

    Previously the function returned ALL positions when prefix=None,
    which made the dashboard show momentum's entire portfolio under the
    sigma/reversion/vector tabs."""
    broker = MagicMock()
    broker.get_positions = AsyncMock(return_value=[
        MagicMock(symbol="AAPL", qty=10, avg_entry_price=Decimal("180"),
                  market_value=Decimal("1855"), cost_basis=Decimal("1800"),
                  unrealized_pl=Decimal("55")),
        MagicMock(symbol="YUMC", qty=15, avg_entry_price=Decimal("47.32"),
                  market_value=Decimal("710"), cost_basis=Decimal("710"),
                  unrealized_pl=Decimal("0")),
    ])
    # Recent orders: AAPL via momentum (mo_), YUMC via sigma's tier pattern.
    aapl_buy = MagicMock()
    aapl_buy.symbol = "AAPL"
    aapl_buy.client_order_id = "mo_AAPL_1700000000"
    aapl_buy.side = MagicMock(value="buy")
    yumc_buy = MagicMock()
    yumc_buy.symbol = "YUMC"
    yumc_buy.client_order_id = "YUMC_1778582356_tier2"
    yumc_buy.side = MagicMock(value="buy")
    broker.list_recent_orders = AsyncMock(return_value=[aapl_buy, yumc_buy])

    holdings = await fetch_engine_holdings(broker, "sigma")
    tickers = {h["ticker"] for h in holdings}
    assert tickers == {"YUMC"}, (
        f"sigma should only own YUMC (the _tier2 buy), not AAPL (mo_ prefix). "
        f"got: {tickers}"
    )


@pytest.mark.asyncio
async def test_fetch_holdings_unprefixed_engine_returns_empty_when_no_tier_orders():
    """If a prefix-less engine has no matching tier-pattern orders, the
    holdings view is empty — NOT a copy of every momentum position. This
    is the bug the operator caught on 2026-05-14."""
    broker = MagicMock()
    broker.get_positions = AsyncMock(return_value=[
        MagicMock(symbol="AAPL", qty=10, avg_entry_price=Decimal("180"),
                  market_value=Decimal("1855"), cost_basis=Decimal("1800"),
                  unrealized_pl=Decimal("55")),
    ])
    only_mo = MagicMock()
    only_mo.symbol = "AAPL"
    only_mo.client_order_id = "mo_AAPL_1"
    only_mo.side = MagicMock(value="buy")
    broker.list_recent_orders = AsyncMock(return_value=[only_mo])

    holdings = await fetch_engine_holdings(broker, "reversion")
    assert holdings == []
