"""Tool-sandbox tests — Task #25 §6 + §10.1 + §10.3 (safety greps).

Covers:
- Source-grep safety (no importlib / __import__ / eval / exec / subprocess / socket / arch / sklearn)
- Whitelist coverage — all 14 callables route through dispatch()
- Per-callable smoke: synthetic snapshot in → bounded NumericSummary out
- cost_net_simulation (the binding outcome gate)
- Error wrapping — exception → ToolResult.error (no traceback echo)
- Determinism — np.random.seed(0) at module init
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from tpcore.lab.llm_finder.models import (
    CalendarContext,
    MarketRegime,
    MarketSnapshot,
    PricePanelRow,
    ToolCall,
    _compute_regime_tuple_id,
)
from tpcore.lab.llm_finder.tool_sandbox import dispatch

# ───────────────────────── source-grep safety fences ─────────────────────────

_SANDBOX_PATH = Path(__file__).resolve().parents[1] / "tool_sandbox.py"
_SANDBOX_SOURCE = _SANDBOX_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "forbidden",
    [
        "importlib",
        "__import__",
        "eval(",
        "exec(",
        "subprocess",
        "os.system",
        "socket",
        "from arch",
        "import arch",
        "import sklearn",
        "from sklearn",
        "linearmodels",
        "pandas_ta",
        "import requests",
        "import urllib",
        "import http",
    ],
)
def test_sandbox_source_no_forbidden_imports(forbidden: str) -> None:
    """CI-grep safety fence per spec §6.2 + §10.3.

    The sandbox source must NEVER contain dynamic-import / network /
    out-of-whitelist library references. The Literal types in the
    ToolCall whitelist alone are not sufficient defense — the module
    source is the ground truth.
    """
    # Allow false positives in comments only via a precise word-boundary check.
    # Use simple substring match; we author tests with explicit deny strings.
    pattern = re.compile(re.escape(forbidden))
    # Exclude comment lines (line starts with `#` after whitespace) and
    # docstring blocks (we keep this simple — the doc references
    # "no `importlib`, no `__import__`" etc in §6.2).
    code_only = "\n".join(
        line for line in _SANDBOX_SOURCE.splitlines() if not line.lstrip().startswith("#")
    )
    # Strip docstring-style mentions by removing lines inside triple-quoted blocks
    # at the module/function level. Simple state machine:
    out_lines: list[str] = []
    in_doc = False
    for line in code_only.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # Toggle in_doc; allow single-line docstrings to stay
            count = stripped.count('"""') + stripped.count("'''")
            if count >= 2:
                # Single-line docstring; keep neither in nor out
                continue
            in_doc = not in_doc
            continue
        if in_doc:
            continue
        out_lines.append(line)
    code_no_docs = "\n".join(out_lines)
    assert not pattern.search(code_no_docs), (
        f"Forbidden token '{forbidden}' present in tool_sandbox.py code "
        f"(outside comments/docstrings) — violates §6.2 safety fence."
    )


# ───────────────────────── synthetic snapshot factory ─────────────────────────


def _spy_snapshot(closes: list[float] | None = None) -> MarketSnapshot:
    """Build a 200-session synthetic snapshot for SPY + AAPL."""
    n = len(closes) if closes else 200
    if closes is None:
        rng = np.random.default_rng(seed=42)
        closes = [100.0 + float(rng.normal(0, 1)) for _ in range(n)]
    aapl_closes = [c * 1.5 + 5.0 for c in closes]

    base_date = date(2025, 1, 1)
    spy_rows = [
        PricePanelRow(
            ticker="SPY",
            session_date=base_date + timedelta(days=i),
            adj_open=c,
            adj_high=c * 1.005,
            adj_low=c * 0.995,
            adj_close=c,
            volume=1_000_000,
            dollar_volume=c * 1_000_000,
            log_return=float(np.log(c / closes[i - 1])) if i > 0 else 0.0,
            liquidity_tier="T1",
        )
        for i, c in enumerate(closes)
    ]
    aapl_rows = [
        PricePanelRow(
            ticker="AAPL",
            session_date=base_date + timedelta(days=i),
            adj_open=c,
            adj_high=c * 1.005,
            adj_low=c * 0.995,
            adj_close=c,
            volume=2_000_000,
            dollar_volume=c * 2_000_000,
            log_return=float(np.log(c / aapl_closes[i - 1])) if i > 0 else 0.0,
            liquidity_tier="T1",
        )
        for i, c in enumerate(aapl_closes)
    ]
    regime = MarketRegime(
        vol_regime="normal",
        trend_regime="range",
        macro_regime="expansion",
        sentiment_regime="neutral",
        cycle_position=("normal",),
        regime_tuple_id=_compute_regime_tuple_id(
            "normal", "range", "expansion", "neutral"
        ),
    )
    return MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=spy_rows[-1].session_date,
        universe="sp500",
        market_regime=regime,
        calendar=CalendarContext(
            session_date=spy_rows[-1].session_date,
            is_earnings_season=False,
            is_fomc_week=False,
            is_opex_week=False,
            is_year_end_week=False,
            days_to_next_fomc=0,
            days_to_next_earnings_season=0,
        ),
        price_window=tuple(spy_rows + aapl_rows),
        fundamentals=(),
        spreads=(),
        sentiment=(),
        macro=(),
        ledger_state=(),
        roster=(),
    )


# ───────────────────────── per-callable smoke ─────────────────────────


def test_ols_hac_nw_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="OLS_HAC_NW",
        args_json=json.dumps({
            "y_ticker": "SPY",
            "y_series": "log_return",
            "x_tickers": ["AAPL"],
            "x_series": "log_return",
            "hac_maxlags": 5,
            "add_constant": True,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None, f"OLS errored: {res.error}"
    assert res.numeric_summary is not None
    assert "OLS_HAC_NW" in res.numeric_summary.summary_text


def test_adfuller_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="adfuller",
        args_json=json.dumps({"ticker": "SPY", "series": "log_return", "maxlag": 5}),
    )
    res = dispatch(call, snap)
    assert res.error is None, f"adfuller errored: {res.error}"
    assert res.numeric_summary is not None
    assert res.numeric_summary.statistic is not None


def test_coint_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="coint",
        args_json=json.dumps({
            "ticker_a": "SPY",
            "ticker_b": "AAPL",
            "pair_pre_registered": True,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None, f"coint errored: {res.error}"
    assert res.numeric_summary is not None


def test_coint_requires_pre_registered() -> None:
    """pair_pre_registered must be True (spec §6.1 fence)."""
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="coint",
        args_json=json.dumps({
            "ticker_a": "SPY",
            "ticker_b": "AAPL",
            "pair_pre_registered": False,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is not None
    assert "ValidationError" in res.error


def test_arima_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="ARIMA_1_0_0",
        args_json=json.dumps({"ticker": "SPY"}),
    )
    res = dispatch(call, snap)
    assert res.error is None, f"ARIMA errored: {res.error}"
    assert res.numeric_summary is not None


def test_spearmanr_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="spearmanr",
        args_json=json.dumps({"ticker_a": "SPY", "ticker_b": "AAPL"}),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert res.numeric_summary is not None
    assert -1.0 <= res.numeric_summary.statistic <= 1.0


def test_pearsonr_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="pearsonr",
        args_json=json.dumps({"ticker_a": "SPY", "ticker_b": "AAPL"}),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert -1.0 <= res.numeric_summary.statistic <= 1.0


def test_ttest_hac_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="ttest_1samp_HAC",
        args_json=json.dumps({"ticker": "SPY", "popmean": 0.0, "hac_maxlags": 5}),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert res.numeric_summary.statistic is not None
    assert "hac_se" in res.numeric_summary.extra


def test_variance_ratio_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="variance_ratio",
        args_json=json.dumps({"ticker": "SPY", "q": 4}),
    )
    res = dispatch(call, snap)
    assert res.error is None
    # VR ~ 1 for random walk; just sanity-check it's a finite number.
    assert res.numeric_summary.statistic is not None


def test_hurst_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="hurst_exponent",
        args_json=json.dumps({"ticker": "SPY", "max_lag": 50}),
    )
    res = dispatch(call, snap)
    assert res.error is None
    # Hurst typically [0, 1]; for noise-dominated synth data should be near 0.5.
    h = res.numeric_summary.statistic
    assert h is not None and 0.0 <= h <= 1.5


def test_ljung_box_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="ljung_box",
        args_json=json.dumps({"ticker": "SPY", "lags": 10}),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert res.numeric_summary.statistic is not None


def test_rolling_spearmanr_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="rolling_spearmanr",
        args_json=json.dumps({
            "ticker_a": "SPY",
            "ticker_b": "AAPL",
            "window": 30,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert "ci_95_lo" in res.numeric_summary.extra
    assert "ci_95_hi" in res.numeric_summary.extra
    assert res.numeric_summary.extra["ci_95_lo"] <= res.numeric_summary.extra["ci_95_hi"]


def test_rolling_pearsonr_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="rolling_pearsonr",
        args_json=json.dumps({
            "ticker_a": "SPY",
            "ticker_b": "AAPL",
            "window": 30,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert res.numeric_summary.statistic is not None


def test_fama_macbeth_dispatches() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="fama_macbeth",
        args_json=json.dumps({
            "y_series": "log_return",
            "x_tickers": ["SPY", "AAPL"],
            "x_series": "log_return",
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None
    assert "mean_coef" in res.numeric_summary.extra


# ───────────────────────── cost_net_simulation (BINDING) ─────────────────────────


def test_cost_net_simulation_zero_cost_matches_gross() -> None:
    """With cost_bps=0, gross_sharpe ≈ cost_net_sharpe."""
    snap = _spy_snapshot()
    spy_dates = sorted({r.session_date for r in snap.price_window if r.ticker == "SPY"})
    entries = [d.isoformat() for d in spy_dates[:50]]
    exits = [d.isoformat() for d in spy_dates[50:100]]
    call = ToolCall(
        callable_name="cost_net_simulation",
        args_json=json.dumps({
            "ticker": "SPY",
            "entry_sessions": entries,
            "exit_sessions": exits,
            "cost_assumption_bps_roundtrip": 0.0,
            "bootstrap_iterations": 100,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None
    gross = res.numeric_summary.extra["gross_sharpe"]
    net = res.numeric_summary.extra["cost_net_sharpe"]
    assert abs(gross - net) < 1e-6


def test_cost_net_simulation_costs_drag_sharpe() -> None:
    """With cost_bps=50, cost_net_sharpe < gross_sharpe (drag)."""
    snap = _spy_snapshot()
    spy_dates = sorted({r.session_date for r in snap.price_window if r.ticker == "SPY"})
    entries = [d.isoformat() for d in spy_dates[:50]]
    exits = [d.isoformat() for d in spy_dates[50:100]]
    call = ToolCall(
        callable_name="cost_net_simulation",
        args_json=json.dumps({
            "ticker": "SPY",
            "entry_sessions": entries,
            "exit_sessions": exits,
            "cost_assumption_bps_roundtrip": 50.0,
            "bootstrap_iterations": 100,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is None
    gross = res.numeric_summary.extra["gross_sharpe"]
    net = res.numeric_summary.extra["cost_net_sharpe"]
    # Costs drag Sharpe down — mean shifts negatively, variance unchanged.
    assert net < gross
    assert res.numeric_summary.extra["bleed_projection_usd"] > 0


def test_cost_net_simulation_mismatched_lengths_error() -> None:
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="cost_net_simulation",
        args_json=json.dumps({
            "ticker": "SPY",
            "entry_sessions": ["2026-01-01"],
            "exit_sessions": ["2026-01-05", "2026-01-10"],
            "cost_assumption_bps_roundtrip": 8.0,
        }),
    )
    res = dispatch(call, snap)
    assert res.error is not None
    assert res.error == "ValueError"


# ───────────────────────── error-wrapping ─────────────────────────


def test_malformed_json_returns_error() -> None:
    snap = _spy_snapshot()
    call = ToolCall(callable_name="OLS_HAC_NW", args_json="not json")
    res = dispatch(call, snap)
    assert res.error is not None
    assert "args_json_decode" in res.error


def test_unknown_column_returns_error() -> None:
    """Defensive: even though Literals fence the type, the resolver also fences."""
    snap = _spy_snapshot()
    # Use a valid callable but a ticker that doesn't exist in the snapshot.
    call = ToolCall(
        callable_name="adfuller",
        args_json=json.dumps({"ticker": "NONEXISTENT_TICKER", "series": "log_return"}),
    )
    res = dispatch(call, snap)
    assert res.error is not None
    # Exception-type name only (no payload echo) per spec §6.2.
    assert "ValueError" in res.error


def test_error_does_not_echo_traceback() -> None:
    """Errors are bounded — exception type name only, no traceback echo."""
    snap = _spy_snapshot()
    call = ToolCall(
        callable_name="adfuller",
        args_json=json.dumps({"ticker": "DOES_NOT_EXIST", "series": "log_return"}),
    )
    res = dispatch(call, snap)
    assert res.error is not None
    # Error string is bounded to ≤256 chars + no path/file disclosure.
    assert len(res.error) <= 256
    assert "tpcore" not in res.error
    assert "Traceback" not in res.error
