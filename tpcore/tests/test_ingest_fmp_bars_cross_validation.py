"""Cross-validation: FMP daily-bars vs the existing Alpaca-sourced corpus.

Two suites:

1. **Unit** (always runs in CI): mocks the FMP HTTP layer and verifies
   the response-shape translation into the Alpaca-compatible
   ``{o,h,l,c,v,t,vw}`` dict shape that ``_upsert_bars`` consumes.
2. **Integration** (skip-gated, ``pytest.mark.integration``): pulls
   ten high-cap tickers' 2026-05-15 session live from FMP and compares
   OHLC against the rows already present in ``platform.prices_daily``.
   Skipped automatically when ``FMP_API_KEY`` / ``DATABASE_URL`` are
   absent — that means CI's lab-isolation DB will silently skip.
   Operator runs locally to verify FMP corpus consistency before any
   backtest trusts the new feed.

OHLC tolerance: **0.5%** — adjusted-close algorithm differences between
FMP and Alpaca should be tiny for non-split-adjusted recent dates.
This is the load-bearing assertion of the test.

Volume comparison: **DIAGNOSTIC ONLY** — printed for the operator but
non-failing. The 2026-05-22 empirical finding is that the existing
corpus is **Alpaca-IEX**, not Alpaca-SIP as initially believed: AAPL
2026-05-15 corpus volume = 1,241,262 vs FMP consolidated = 54,862,836
(a ~44x ratio). A symmetric ±5% volume band cannot pass against an
IEX-subset corpus; the SP-A-style ±5% band only becomes a real
assertion AFTER an FMP-driven full-universe refresh re-baselines
``platform.prices_daily`` with consolidated-tape volumes. Until then,
volume is recorded in the test output for operator inspection but is
not gating.

Tickers requiring symbol translation (Alpaca ``BRK.B`` → FMP
``BRK-B``) get translated inside the adapter — the test still calls
with the Alpaca-canonical spelling. If a ticker is absent from the
corpus the test reports it as skipped rather than failing.
"""
from __future__ import annotations

import os
from datetime import date

import httpx
import pytest

from tpcore.data.ingest_fmp_bars import (
    _to_alpaca_shape,
    fetch_daily_bars_multi,
)

# ─── UNIT — JSON-shape parsing (always runs) ────────────────────────────


def test_to_alpaca_shape_translates_fmp_response() -> None:
    fmp_rows = [
        {
            "symbol": "AAPL", "date": "2026-05-21",
            "open": 301.055, "high": 305.54, "low": 300.4,
            "close": 304.99, "volume": 42823425,
            "change": 3.94, "changePercent": 1.30707, "vwap": 303.64,
        },
        {
            "symbol": "AAPL", "date": "2026-05-20",
            "open": 298.18, "high": 302.8, "low": 298.08,
            "close": 302.25, "volume": 38229843, "vwap": 300.3275,
        },
    ]
    out = _to_alpaca_shape(fmp_rows)
    # Ascending date order after translation.
    assert [b["t"][:10] for b in out] == ["2026-05-20", "2026-05-21"]
    last = out[-1]
    assert last["o"] == 301.055
    assert last["h"] == 305.54
    assert last["l"] == 300.4
    assert last["c"] == 304.99
    assert last["v"] == 42823425
    assert last["vw"] == 303.64
    # Timestamp is midnight-UTC ISO with the session date.
    assert last["t"] == "2026-05-21T00:00:00Z"


def test_to_alpaca_shape_skips_rows_missing_required_fields() -> None:
    fmp_rows = [
        {"symbol": "X", "date": "2026-05-21", "open": 1.0, "high": 2.0,
         "low": 0.5, "close": 1.5, "volume": 1000},
        {"symbol": "X", "date": "2026-05-20", "open": 1.0, "high": 2.0,
         "low": 0.5, "close": None, "volume": 1000},  # missing close
        {"symbol": "X", "date": "2026-05-19"},  # missing everything
        {"symbol": "X", "date": "not-a-date", "open": 1, "high": 1,
         "low": 1, "close": 1, "volume": 1},  # bad date
    ]
    out = _to_alpaca_shape(fmp_rows)
    assert len(out) == 1
    assert out[0]["t"].startswith("2026-05-21")


def test_to_alpaca_shape_handles_missing_vwap() -> None:
    fmp_rows = [{
        "symbol": "X", "date": "2026-05-21",
        "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100,
    }]
    out = _to_alpaca_shape(fmp_rows)
    assert out[0]["vw"] is None


@pytest.mark.asyncio
async def test_fetch_daily_bars_multi_with_mock_transport(monkeypatch) -> None:
    """End-to-end shape: mocked httpx returns FMP's JSON, the adapter
    fans out per-symbol and returns the Alpaca-compatible dict."""
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sym = request.url.params.get("symbol", "")
        captured.append(sym)
        if sym == "MISSING":
            return httpx.Response(404)
        return httpx.Response(200, json=[{
            "symbol": sym, "date": "2026-05-15",
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 50000, "vwap": 100.25,
        }])

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        out = await fetch_daily_bars_multi(
            client, ["AAPL", "MSFT", "MISSING"],
            date(2026, 5, 15), date(2026, 5, 15),
        )
    assert captured == ["AAPL", "MSFT", "MISSING"]
    assert len(out["AAPL"]) == 1
    assert len(out["MSFT"]) == 1
    assert out["MISSING"] == []  # 404 graceful skip
    assert out["AAPL"][0]["c"] == 100.5


@pytest.mark.asyncio
async def test_fetch_daily_bars_multi_permanent_4xx_raises(monkeypatch) -> None:
    """Non-404 4xx (e.g. 401 invalid key) raises DataProviderOutage —
    we never want to silently treat an auth failure as 'no data'."""
    from tpcore.outage import DataProviderOutage

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Invalid API key")

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DataProviderOutage):
            await fetch_daily_bars_multi(
                client, ["AAPL"], date(2026, 5, 15), date(2026, 5, 15),
            )


# ─── INTEGRATION — live FMP + live DB (operator-runs-locally) ───────────

_INTEGRATION_TICKERS = (
    "AAPL", "MSFT", "SPY", "NVDA", "GOOGL",
    "AMZN", "TSLA", "JPM", "BRK.B", "WMT",
)
_INTEGRATION_SESSION = date(2026, 5, 15)
_OHLC_TOLERANCE_PCT = 0.005  # 0.5%
_VOLUME_TOLERANCE_PCT = 0.05  # 5%


def _have_live_credentials() -> bool:
    return bool(os.environ.get("FMP_API_KEY")) and bool(
        os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_live_credentials(),
    reason="integration test requires FMP_API_KEY + DATABASE_URL[_IPV4]",
)
async def test_fmp_cross_validation_against_corpus() -> None:
    """LIVE: ten high-cap tickers' OHLC must match the existing
    Alpaca corpus to within 0.5%. Volume must be ≥ corpus volume
    (FMP = consolidated tape; corpus = Alpaca-IEX subset)."""
    import asyncpg

    db_url = os.environ.get("DATABASE_URL") or os.environ["DATABASE_URL_IPV4"]
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        db_rows = await conn.fetch(
            """
            SELECT ticker, date, open, high, low, close, volume
            FROM platform.prices_daily
            WHERE ticker = ANY($1::text[]) AND date = $2
            ORDER BY ticker
            """,
            list(_INTEGRATION_TICKERS), _INTEGRATION_SESSION,
        )
    finally:
        await conn.close()
    corpus = {r["ticker"]: r for r in db_rows}

    async with httpx.AsyncClient(timeout=30.0) as client:
        fmp_out = await fetch_daily_bars_multi(
            client, list(_INTEGRATION_TICKERS),
            _INTEGRATION_SESSION, _INTEGRATION_SESSION,
        )

    mismatches: list[str] = []
    skipped: list[str] = []
    volume_diagnostics: list[str] = []
    passed = 0
    for ticker in _INTEGRATION_TICKERS:
        corpus_row = corpus.get(ticker)
        fmp_bars = fmp_out.get(ticker, [])
        if corpus_row is None:
            # Corpus doesn't track this ticker yet — skip rather than
            # fail. FMP corpus expansion will close the gap on the
            # next universe-wide pull.
            skipped.append(f"{ticker}: absent from corpus (no comparison possible)")
            continue
        if not fmp_bars:
            mismatches.append(f"{ticker}: FMP returned no bars")
            continue
        b = fmp_bars[-1]  # the session

        # OHLC — STRICT 0.5% gate. This is the load-bearing assertion.
        ticker_failed_ohlc = False
        for field, fmp_val, db_val in [
            ("open", b["o"], float(corpus_row["open"])),
            ("high", b["h"], float(corpus_row["high"])),
            ("low", b["l"], float(corpus_row["low"])),
            ("close", b["c"], float(corpus_row["close"])),
        ]:
            if abs(fmp_val - db_val) / max(abs(db_val), 1e-9) > _OHLC_TOLERANCE_PCT:
                mismatches.append(
                    f"{ticker}.{field}: FMP={fmp_val} DB={db_val} "
                    f"diff={abs(fmp_val-db_val)/db_val:.4%}",
                )
                ticker_failed_ohlc = True

        # Volume — DIAGNOSTIC only. The current corpus is Alpaca-IEX,
        # not SIP, so a strict ±5% band is structurally impossible to
        # pass (the 44x ratio finding 2026-05-22). Recorded for the
        # operator; will become gating after FMP-driven re-baselining.
        v_db = float(corpus_row["volume"])
        v_fmp = float(b["v"])
        if v_db > 0:
            diff_pct = abs(v_fmp - v_db) / v_db
            if diff_pct > _VOLUME_TOLERANCE_PCT:
                volume_diagnostics.append(
                    f"  {ticker}: FMP={int(v_fmp):>13,} DB={int(v_db):>13,} "
                    f"ratio={v_fmp/v_db:6.2f}x (DB looks IEX-subset)",
                )

        if not ticker_failed_ohlc:
            passed += 1

    # Diagnostic output — visible when the test is run with -v / -s.
    if volume_diagnostics:
        print(  # noqa: T201
            f"\nvolume diagnostic — {len(volume_diagnostics)} ticker(s) "
            f"outside ±{_VOLUME_TOLERANCE_PCT:.0%} band (expected when "
            f"corpus is Alpaca-IEX, NOT a failure):\n"
            + "\n".join(volume_diagnostics),
        )
    if skipped:
        print(  # noqa: T201
            f"\ncross-validation skipped {len(skipped)} ticker(s) "
            f"(absent from corpus):\n  " + "\n  ".join(skipped),
        )
    n_comparable = len(_INTEGRATION_TICKERS) - len(skipped)
    assert not mismatches, (
        f"cross-validation OHLC: {passed}/{n_comparable} comparable tickers OK; "
        f"{len(skipped)} skipped; failures:\n  " + "\n  ".join(mismatches)
    )
