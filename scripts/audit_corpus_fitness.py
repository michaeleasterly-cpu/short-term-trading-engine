"""Corpus-fitness audit for edge-finding (2026-05-22).

One-shot operator script — NOT part of the autonomous pipeline. Produces
the raw numbers that back ``docs/audits/2026-05-22-corpus-fitness-for-edge-finding.md``.
Four sections, executed in order, results dumped to stdout + JSON.

A. Broad-ticker cross-validation — 100 random T1+T2 tickers, 2026-05-15
   session, FMP vs the existing ``platform.prices_daily`` corpus. Reports
   per-ticker OHLC diff %, volume ratio, and any tickers FMP cannot resolve.

B. Split-day adjustment test — pinned splits (AAPL 2020-08-31 4:1,
   TSLA 2020-08-31 5:1, GOOGL 2022-07-15 20:1, NVDA 2024-06-07 10:1).
   Compares FMP adjusted-close on the split date vs corpus adjusted-close.

C. Survivorship audit — delisted-ticker completeness in ``platform.prices_daily``
   + delta vs ``platform.liquidity_tiers``, plus a 20-ticker spot check
   against known historical delistings.

D. 9 validation failures — re-runs ``data_validation`` and compares
   per-check state against the 2026-05-21 baseline.

Usage:
    bash scripts/run_audit_corpus_fitness.sh

Total FMP call budget: ≤100 (background ``daily_bars --force_refresh`` is
running on the same key — staying under 100 keeps us well below the
300 req/min shared ceiling).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from tpcore.data.ingest_fmp_bars import fetch_daily_bars_multi

# ─── A. Broad cross-validation ─────────────────────────────────────────

_BROAD_SESSION = date(2026, 5, 15)
_BROAD_SAMPLE_N = 100
_BROAD_OHLC_TOLERANCE_PCT = 0.005
_BROAD_VOLUME_TOLERANCE_PCT = 0.05


@dataclass
class BroadResult:
    sample_n: int = 0
    comparable_n: int = 0
    missing_from_fmp: list[str] = field(default_factory=list)
    missing_from_corpus: list[str] = field(default_factory=list)
    ohlc_diff_p50: float = 0.0
    ohlc_diff_p95: float = 0.0
    ohlc_diff_max: float = 0.0
    ohlc_diff_max_ticker: str = ""
    ohlc_breaches: list[str] = field(default_factory=list)
    volume_ratio_p50: float = 0.0
    volume_ratio_p95: float = 0.0
    volume_ratio_max: float = 0.0
    volume_breaches_n: int = 0


async def _audit_broad(pool: asyncpg.Pool) -> BroadResult:
    """Pull 100 random T1+T2 tickers, compare FMP vs corpus on 2026-05-15."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker
            FROM platform.liquidity_tiers
            WHERE tier <= 2
            ORDER BY ticker
            """,
        )
    pool_universe = [r["ticker"] for r in rows]
    rnd = random.Random(20260522)
    sample = sorted(rnd.sample(pool_universe, min(_BROAD_SAMPLE_N, len(pool_universe))))

    async with pool.acquire() as conn:
        corpus_rows = await conn.fetch(
            """
            SELECT ticker, open, high, low, close, volume
            FROM platform.prices_daily
            WHERE ticker = ANY($1::text[]) AND date = $2
            """,
            sample, _BROAD_SESSION,
        )
    corpus = {r["ticker"]: r for r in corpus_rows}

    async with httpx.AsyncClient(timeout=60.0) as client:
        fmp_out = await fetch_daily_bars_multi(
            client, sample, _BROAD_SESSION, _BROAD_SESSION,
        )

    result = BroadResult(sample_n=len(sample))
    ohlc_diffs: list[tuple[str, float]] = []
    volume_ratios: list[float] = []

    for ticker in sample:
        crow = corpus.get(ticker)
        fbars = fmp_out.get(ticker, [])
        if crow is None:
            result.missing_from_corpus.append(ticker)
            continue
        if not fbars:
            result.missing_from_fmp.append(ticker)
            continue
        b = fbars[-1]
        result.comparable_n += 1
        worst_diff = 0.0
        for fld, fv, dv in [
            ("open", b["o"], float(crow["open"])),
            ("high", b["h"], float(crow["high"])),
            ("low", b["l"], float(crow["low"])),
            ("close", b["c"], float(crow["close"])),
        ]:
            d = abs(fv - dv) / max(abs(dv), 1e-9)
            if d > worst_diff:
                worst_diff = d
            if d > _BROAD_OHLC_TOLERANCE_PCT:
                result.ohlc_breaches.append(
                    f"{ticker}.{fld} FMP={fv} DB={dv} diff={d:.4%}"
                )
        ohlc_diffs.append((ticker, worst_diff))
        v_db = float(crow["volume"])
        v_fmp = float(b["v"])
        if v_db > 0:
            ratio = v_fmp / v_db
            volume_ratios.append(ratio)
            if abs(v_fmp - v_db) / v_db > _BROAD_VOLUME_TOLERANCE_PCT:
                result.volume_breaches_n += 1

    if ohlc_diffs:
        sorted_diffs = sorted(d for _, d in ohlc_diffs)
        result.ohlc_diff_p50 = sorted_diffs[len(sorted_diffs) // 2]
        result.ohlc_diff_p95 = sorted_diffs[int(len(sorted_diffs) * 0.95)]
        worst = max(ohlc_diffs, key=lambda x: x[1])
        result.ohlc_diff_max = worst[1]
        result.ohlc_diff_max_ticker = worst[0]
    if volume_ratios:
        sorted_r = sorted(volume_ratios)
        result.volume_ratio_p50 = sorted_r[len(sorted_r) // 2]
        result.volume_ratio_p95 = sorted_r[int(len(sorted_r) * 0.95)]
        result.volume_ratio_max = max(volume_ratios)

    return result


# ─── B. Split-day adjustment test ──────────────────────────────────────

_SPLIT_CASES: list[tuple[str, date, float]] = [
    ("AAPL", date(2020, 8, 31), 4.0),
    ("TSLA", date(2020, 8, 31), 5.0),
    ("GOOGL", date(2022, 7, 15), 20.0),
    ("NVDA", date(2024, 6, 7), 10.0),
]


@dataclass
class SplitResult:
    ticker: str
    split_date: str
    ratio: float
    fmp_close: float | None
    fmp_adj_close: float | None
    db_close: float | None
    db_adj_close: float | None
    diff_close_pct: float | None
    diff_adj_close_pct: float | None
    verdict: str


async def _fmp_raw_eod(
    client: httpx.AsyncClient, symbol: str, on: date,
) -> dict[str, Any] | None:
    """Pull a single FMP EOD row (incl. ``adjClose`` if returned)."""
    key = os.environ["FMP_API_KEY"]
    url = "https://financialmodelingprep.com/stable/historical-price-eod/full"
    params = {"symbol": symbol, "from": on.isoformat(), "to": on.isoformat(), "apikey": key}
    resp = await client.get(url, params=params)
    if resp.status_code != 200:
        return None
    body = resp.json()
    rows = body if isinstance(body, list) else body.get("historical") or []
    for row in rows:
        if row.get("date") == on.isoformat():
            return row
    return None


async def _audit_splits(pool: asyncpg.Pool) -> list[SplitResult]:
    results: list[SplitResult] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for ticker, split_day, ratio in _SPLIT_CASES:
            fmp_row = await _fmp_raw_eod(client, ticker, split_day)
            async with pool.acquire() as conn:
                drow = await conn.fetchrow(
                    """
                    SELECT close, adjusted_close
                    FROM platform.prices_daily
                    WHERE ticker = $1 AND date = $2
                    """,
                    ticker, split_day,
                )
            fmp_close = float(fmp_row["close"]) if fmp_row and fmp_row.get("close") is not None else None
            fmp_adj = float(fmp_row.get("adjClose")) if fmp_row and fmp_row.get("adjClose") is not None else None
            db_close = float(drow["close"]) if drow and drow["close"] is not None else None
            db_adj = float(drow["adjusted_close"]) if drow and drow["adjusted_close"] is not None else None
            diff_close = None
            diff_adj = None
            if fmp_close is not None and db_close is not None and db_close != 0:
                diff_close = abs(fmp_close - db_close) / abs(db_close)
            if fmp_adj is not None and db_adj is not None and db_adj != 0:
                diff_adj = abs(fmp_adj - db_adj) / abs(db_adj)
            verdict = _split_verdict(fmp_close, db_close, fmp_adj, db_adj, ratio)
            results.append(SplitResult(
                ticker=ticker, split_date=split_day.isoformat(), ratio=ratio,
                fmp_close=fmp_close, fmp_adj_close=fmp_adj,
                db_close=db_close, db_adj_close=db_adj,
                diff_close_pct=diff_close, diff_adj_close_pct=diff_adj,
                verdict=verdict,
            ))
    return results


def _split_verdict(
    fmp_close: float | None, db_close: float | None,
    fmp_adj: float | None, db_adj: float | None, ratio: float,
) -> str:
    if fmp_close is None and db_close is None:
        return "BOTH_MISSING"
    if fmp_close is None:
        return "FMP_MISSING"
    if db_close is None:
        return "DB_MISSING"
    # Compare on adjusted close if both providers expose it; otherwise on close.
    if fmp_adj is not None and db_adj is not None:
        d = abs(fmp_adj - db_adj) / max(abs(db_adj), 1e-9)
        return f"AGREE_ADJ ({d:.2%})" if d <= 0.005 else f"DISAGREE_ADJ ({d:.2%})"
    d = abs(fmp_close - db_close) / max(abs(db_close), 1e-9)
    return f"AGREE_CLOSE ({d:.2%})" if d <= 0.005 else f"DISAGREE_CLOSE ({d:.2%})"


# ─── C. Survivorship audit ─────────────────────────────────────────────

_DELISTING_SPOT_CHECK: list[tuple[str, date, str]] = [
    ("WORK", date(2021, 7, 21), "Slack → CRM merger"),
    ("ATVI", date(2023, 10, 13), "Activision → MSFT"),
    ("AABA", date(2017, 6, 13), "Altaba (post-Yahoo→OATH)"),
    ("OSTK", date(2023, 8, 21), "Overstock → BYON ticker change"),
    ("LNKD", date(2016, 12, 8), "LinkedIn → MSFT"),
    ("WLTW", date(2022, 7, 1), "WTW merger"),
    ("CTXS", date(2022, 9, 30), "Citrix → private"),
    ("MGI", date(2023, 6, 1), "MoneyGram → private"),
    ("TIF", date(2021, 1, 7), "Tiffany → LVMH"),
    ("XLNX", date(2022, 2, 14), "Xilinx → AMD"),
    ("FB", date(2022, 6, 9), "Facebook → META rebrand"),
    ("TWTR", date(2022, 10, 27), "Twitter → private (Musk)"),
    ("RDS-A", date(2022, 1, 28), "Royal Dutch Shell unification"),
    ("CTL", date(2020, 9, 18), "CenturyLink → Lumen"),
    ("CELG", date(2019, 11, 20), "Celgene → BMY"),
    ("XEC", date(2021, 10, 1), "Cimarex → Coterra"),
    ("BHGE", date(2019, 12, 31), "Baker Hughes → BKR rebrand"),
    ("DPS", date(2018, 7, 9), "Dr Pepper Snapple merger"),
    ("RTN", date(2020, 4, 3), "Raytheon → RTX"),
    ("UTX", date(2020, 4, 3), "United Technologies → RTX"),
]


@dataclass
class SurvivorshipResult:
    delisted_count: int = 0
    delisted_with_late_capture_count: int = 0
    pre_2020_tickers_count: int = 0
    pre_2020_tickers_in_classifications: int = 0
    pre_2020_tickers_missing_from_classifications: int = 0
    delisted_ticker_examples: list[dict[str, Any]] = field(default_factory=list)
    spot_check: list[dict[str, Any]] = field(default_factory=list)


async def _audit_survivorship(pool: asyncpg.Pool) -> SurvivorshipResult:
    r = SurvivorshipResult()
    async with pool.acquire() as conn:
        # (a) total delisted count
        cnt = await conn.fetchval(
            "SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily WHERE delisted = true"
        )
        r.delisted_count = int(cnt or 0)

        # (b) delisted-true tickers whose latest bar is <30 days ago (late capture)
        late = await conn.fetch(
            """
            SELECT ticker, MAX(date) AS last_bar, MIN(delisting_date) AS dl_date
            FROM platform.prices_daily
            WHERE delisted = true
            GROUP BY ticker
            HAVING MAX(date) > (CURRENT_DATE - INTERVAL '30 days')
            LIMIT 50
            """
        )
        r.delisted_with_late_capture_count = len(late)
        for row in late[:10]:
            r.delisted_ticker_examples.append({
                "ticker": row["ticker"],
                "last_bar": row["last_bar"].isoformat(),
                "delisting_date": row["dl_date"].isoformat() if row["dl_date"] else None,
            })

        # (c) tickers with pre-2020-01-01 history NOT in ticker_classifications
        pre2020 = await conn.fetch(
            """
            WITH historical AS (
                SELECT DISTINCT ticker
                FROM platform.prices_daily
                WHERE date < DATE '2020-01-01'
            )
            SELECT h.ticker,
                   (SELECT MAX(date) FROM platform.prices_daily p WHERE p.ticker = h.ticker) AS last_bar,
                   EXISTS(SELECT 1 FROM platform.ticker_classifications c WHERE c.ticker = h.ticker) AS in_class,
                   EXISTS(SELECT 1 FROM platform.prices_daily p WHERE p.ticker = h.ticker AND p.delisted = true) AS marked_delisted
            FROM historical h
            """
        )
        r.pre_2020_tickers_count = len(pre2020)
        in_class = sum(1 for row in pre2020 if row["in_class"])
        r.pre_2020_tickers_in_classifications = in_class
        r.pre_2020_tickers_missing_from_classifications = r.pre_2020_tickers_count - in_class

        # (d) spot-check the 20 known delistings
        for ticker, dl_date, note in _DELISTING_SPOT_CHECK:
            row = await conn.fetchrow(
                """
                SELECT MAX(date) AS last_bar,
                       COUNT(*) AS rowcount,
                       bool_or(delisted) AS marked_delisted,
                       MIN(delisting_date) AS dl_recorded
                FROM platform.prices_daily
                WHERE ticker = $1
                """,
                ticker,
            )
            last_bar = row["last_bar"]
            rc = int(row["rowcount"] or 0)
            verdict = _spot_verdict(rc, last_bar, dl_date, row["marked_delisted"])
            r.spot_check.append({
                "ticker": ticker, "expected_delist": dl_date.isoformat(),
                "rowcount": rc,
                "last_bar": last_bar.isoformat() if last_bar else None,
                "marked_delisted": bool(row["marked_delisted"]),
                "delisting_date_recorded": row["dl_recorded"].isoformat() if row["dl_recorded"] else None,
                "note": note, "verdict": verdict,
            })
    return r


def _spot_verdict(rc: int, last_bar: date | None, expected: date, marked: Any) -> str:
    if rc == 0:
        return "NO_RECORD"
    if last_bar is None:
        return "NO_BARS"
    days = (expected - last_bar).days
    if days > 7:
        return f"EARLY_TRUNCATION (last_bar {days}d before delist)"
    if days < -30:
        return f"LATE_BARS (last_bar {-days}d after delist)"
    return f"OK ({'marked' if marked else 'unmarked'})"


# ─── D. 9 validation failures re-run ───────────────────────────────────

_BASELINE_FAILURES_2026_05_21 = [
    "fundamentals_quarterly_completeness",
    "corporate_actions_completeness",
    "earnings_events_monotone",
    "sec_insider_monotone",
    "liquidity_tiers_completeness",
    "ticker_classifications_coverage",
    "macro_indicators_completeness",
    "fear_greed_freshness",
    "aaii_sentiment_freshness",
]


@dataclass
class ValidationResult:
    name: str
    state: str
    detail: str


async def _audit_validation(pool: asyncpg.Pool) -> list[ValidationResult]:
    """Run the validation suite and tag each baseline-failure check.

    The suite write per-check rows. We read them back; for any baseline
    check, we tag STILL_RED / HEALED / NOT_RUN.
    """
    from tpcore.quality.validation.suite import run_suite

    suite = await run_suite(pool)
    out: list[ValidationResult] = []
    by_name = {c.name: c for c in suite.checks}
    for name in _BASELINE_FAILURES_2026_05_21:
        check = by_name.get(name)
        if check is None:
            out.append(ValidationResult(name=name, state="NOT_RUN", detail="check absent from suite"))
            continue
        if check.passed:
            out.append(ValidationResult(
                name=name, state="HEALED",
                detail=f"passed {check.total - check.failed}/{check.total}",
            ))
        else:
            # Summarize up to first 3 failure reasons.
            reasons: list[str] = []
            for f in check.failures[:3]:
                t = f.ticker or ""
                r = f.reason or ""
                e = f" expected={f.expected}" if f.expected else ""
                o = f" observed={f.observed}" if f.observed else ""
                reasons.append(f"{t}:{r}{e}{o}")
            tail = (
                f" + {len(check.failures) - 3} more" if len(check.failures) > 3 else ""
            )
            summary = " | ".join(reasons) + tail if reasons else "failed (no detail)"
            out.append(ValidationResult(
                name=name, state="STILL_RED",
                detail=f"{check.failed}/{check.total} failed: {summary[:300]}",
            ))
    return out


# ─── Driver ─────────────────────────────────────────────────────────────


async def main() -> int:
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    if not os.environ.get("FMP_API_KEY"):
        print("FAILED — FMP_API_KEY not set", file=sys.stderr)
        return 1

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4, statement_cache_size=0)
    if pool is None:
        raise RuntimeError("could not build asyncpg pool")
    summary: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat()}
    try:
        print(f"=== A. Broad cross-validation ({_BROAD_SAMPLE_N} random T1+T2, {_BROAD_SESSION}) ===")
        broad = await _audit_broad(pool)
        summary["A_broad"] = asdict(broad)
        print(json.dumps(asdict(broad), indent=2, default=str))

        print("\n=== B. Split-day adjustment ===")
        splits = await _audit_splits(pool)
        summary["B_splits"] = [asdict(s) for s in splits]
        for s in splits:
            print(json.dumps(asdict(s), indent=2, default=str))

        print("\n=== C. Survivorship ===")
        surv = await _audit_survivorship(pool)
        summary["C_survivorship"] = asdict(surv)
        print(json.dumps(asdict(surv), indent=2, default=str))

        print("\n=== D. Validation re-run ===")
        val = await _audit_validation(pool)
        summary["D_validation"] = [asdict(v) for v in val]
        for v in val:
            print(f"  {v.state:10s} {v.name:42s}  {v.detail}")
    finally:
        await pool.close()

    out_path = Path("docs/audits/data/2026-05-22-corpus-fitness.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
