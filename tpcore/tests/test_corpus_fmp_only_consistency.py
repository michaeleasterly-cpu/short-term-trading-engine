"""Sentinel test — single-source-FMP corpus consistency across splits.

The 2026-05-22 corpus-fitness audit (PR #281) found that dual-sourcing
(Alpaca-IEX pre-2026-05-22 + FMP-CTA post) caused a 3.04% close
disagreement on AAPL's 2020-08-31 split day. After the
``historical_prices_daily_fmp_rebuild`` one-shot lands and the corpus
is single-source (FMP only), the disagreement against fresh FMP probes
on those same split days should collapse to <0.5% (vendor-internal
rounding noise) — not the >2% feed-mix discontinuity of the pre-rebuild
state.

This test pins the post-rebuild contract:

* For each known split date, the live ``platform.prices_daily.close``
  vs. a fresh FMP probe must agree to within 0.5%.
* The 0.5% threshold is the operator-tightened post-rebuild gate — the
  pre-rebuild gate had to allow 2% to absorb feed-mix slack. Tightening
  is exactly the point of the rebuild.

DB-and-FMP-skip-gated for CI — like the existing
``test_ingest_fmp_bars_cross_validation`` integration block, this only
runs when DATABASE_URL + FMP_API_KEY are both present.

The four anchor split dates are the same ones the audit doc §B
probed, so the test directly verifies the §B finding has been healed
post-rebuild.
"""
from __future__ import annotations

import os
from datetime import date

import httpx
import pytest

# (ticker, split_date_iso, split_ratio_note). Same four splits the
# 2026-05-22 audit doc §B used.
_KNOWN_SPLIT_PROBES: tuple[tuple[str, str, str], ...] = (
    ("AAPL",  "2020-08-31", "4:1"),
    ("TSLA",  "2020-08-31", "5:1"),
    ("GOOGL", "2022-07-15", "20:1"),
    ("NVDA",  "2024-06-07", "10:1"),
)

# Tight gate — post-rebuild the corpus is single-source so vendor-
# internal rounding alone should keep us well under 0.5%. The pre-
# rebuild gate had to accept up to 3% on AAPL; tightening from 2% to
# 0.5% post-rebuild is the proof the dual-source defect is healed.
_MAX_DIFF_PCT = 0.005


def _have_db_and_fmp() -> bool:
    db_ok = bool(
        os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_URL_IPV4"),
    )
    return db_ok and bool(os.environ.get("FMP_API_KEY"))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not _have_db_and_fmp(),
    reason=(
        "sentinel test requires DATABASE_URL[_IPV4] + FMP_API_KEY; "
        "CI skip is expected"
    ),
)
@pytest.mark.parametrize(
    ("ticker", "split_date_iso", "ratio"),
    _KNOWN_SPLIT_PROBES,
)
async def test_post_rebuild_split_day_close_agrees_with_fmp(
    ticker: str, split_date_iso: str, ratio: str,
) -> None:
    """Post-rebuild, live close on each split day matches a fresh FMP
    probe to within 0.5%.

    Pre-rebuild this RED'd on AAPL (3.04% disagreement) and GOOGL
    (0.83%) per audit §B. Post-rebuild it must GREEN on all four —
    that's the load-bearing assertion this sentinel encodes.

    If this test starts redding after a previously-green run, the
    canonical recovery path is to re-run
    ``--stage historical_prices_daily_fmp_rebuild`` — something has
    overwritten the FMP-sourced rows with a different feed (e.g. a
    daily-bars run pointed at an Alpaca fallback). The rebuild fixes
    it.
    """
    import asyncpg

    from tpcore.data.ingest_fmp_bars import _to_fmp_symbol

    split_date = date.fromisoformat(split_date_iso)
    db_url = (
        os.environ.get("DATABASE_URL")
        or os.environ["DATABASE_URL_IPV4"]
    )
    fmp_api_key = os.environ["FMP_API_KEY"]

    # Read the live row.
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        row = await conn.fetchrow(
            """
            SELECT close, source
            FROM platform.prices_daily
            WHERE ticker = $1 AND date = $2
            """,
            ticker, split_date,
        )
    finally:
        await conn.close()
    assert row is not None, (
        f"{ticker} @ {split_date}: row missing from "
        f"platform.prices_daily — rebuild appears incomplete. "
        f"Operator: re-run ``.venv/bin/python scripts/ops.py "
        f"--stage historical_prices_daily_fmp_rebuild``"
    )
    db_close = float(row["close"])
    db_source = row["source"]
    assert db_source == "fmp", (
        f"{ticker} @ {split_date}: source = {db_source!r}, expected "
        f"'fmp' — the rebuild did not overwrite this row. Operator: "
        f"re-run the rebuild stage (idempotent)."
    )

    # Fresh FMP probe — narrow [split_date, split_date] window.
    fmp_sym = _to_fmp_symbol(ticker)
    url = (
        "https://financialmodelingprep.com/stable/"
        "historical-price-eod/full"
    )
    params = {
        "symbol": fmp_sym,
        "from": split_date_iso,
        "to": split_date_iso,
        "apikey": fmp_api_key,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, params=params)
    assert resp.status_code == 200, (
        f"FMP /historical-price-eod/full {ticker} @ {split_date} "
        f"returned HTTP {resp.status_code}: {resp.text[:200]}"
    )
    body = resp.json()
    rows: list[dict] = (
        body if isinstance(body, list)
        else (body.get("historical") if isinstance(body, dict) else [])
        or []
    )
    fmp_row = next(
        (r for r in rows if r.get("date") == split_date_iso), None,
    )
    assert fmp_row is not None, (
        f"{ticker} @ {split_date}: FMP returned no row for this "
        f"session — the split probe needs a different anchor date."
    )
    fmp_close = float(fmp_row["close"])

    rel_diff = abs(db_close - fmp_close) / max(fmp_close, 1e-9)
    assert rel_diff < _MAX_DIFF_PCT, (
        f"{ticker} @ {split_date} ({ratio}): db_close={db_close:.4f} "
        f"vs fmp_close={fmp_close:.4f} → {rel_diff:.2%} diff exceeds "
        f"the {_MAX_DIFF_PCT:.1%} post-rebuild gate. This is the same "
        f"feed-mix-discontinuity signature §B of the 2026-05-22 audit "
        f"caught. Operator: re-run ``.venv/bin/python scripts/ops.py "
        f"--stage historical_prices_daily_fmp_rebuild`` to re-source "
        f"the row from FMP."
    )
