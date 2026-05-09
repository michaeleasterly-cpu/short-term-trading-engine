"""Backtest comparison: CHOP-enhanced regime gate vs ADX-only baseline.

Both variants run over the same trading window using the same
``sigma.plugs.setup_detection`` indicator helpers (so the science doesn't
drift from the live code), then we compare trade-level performance.

Variants
--------
**baseline**         pass iff ``ADX(14) < 20``
**chop-enhanced**    pass iff ``ADX(14) < 20`` AND ``CHOP(14) > 38.2``

(The shipped engine uses *SPY-level* CHOP for Market Context scoring; this
backtest tests the simpler *per-stock* CHOP gate the user asked for. They
aren't the same hypothesis — the per-stock gate is a strictly tighter
filter than ADX alone, and asking whether it improves PnL is the
narrowest, most testable form of the question.)

Trade simulation
----------------
* One position at a time; if a trade is still open, skip new entries.
* Entry: next day's open × (1 + slippage). Slippage 0.05% per side.
* Exit (Sigma scale-out, mirrors `sigma.plugs.execution_risk`):
    * If the day's low ≤ entry × (1 − 0.03), stopped out at that stop level.
    * Else if the day's high ≥ mid-band, fill 50% at mid-band ("tier 1").
      Continue holding the other 50% until the day's high ≥ upper band, then
      fill at that level ("tier 2"). Hard cap on hold time = 30 trading days.
* PnL is dollar-weighted by tier qty (50/50 split on entry notional).

Database expectations
---------------------
``platform.prices_daily(ticker text, date date, open numeric, high numeric,
low numeric, close numeric, volume bigint, ...)``. Read-only; the script
will *not* write to the DB. If the table is empty the script exits cleanly
with a zero-output summary so it can be re-run after ingestion.

Usage
-----
::

    python -m sigma.backtest_chop --start 2018-01-01 --end 2025-12-31

Reads ``DATABASE_URL`` from the environment.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from sigma.plugs.setup_detection import (
    ADX_PERIOD,
    BB_NUM_STD,
    BB_PERIOD,
    CHOP_PERIOD,
    CHOP_SIDEWAYS_WEAK,
    MAX_ADX,
    _band_proximity,
    _compute_adx,
    _compute_bbands,
    compute_chop,
)
from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("sigma.backtest_chop")

# ────────────────────────────────────────────────────────────────────────────
# Backtest knobs
# ────────────────────────────────────────────────────────────────────────────

# 50 large-cap names that traded continuously across 2018–2025. ETFs included
# for index-level coverage. No survivorship-free guarantees vs. delistings —
# that requires a populated `platform.prices_daily` per the docstring.
DEFAULT_UNIVERSE: tuple[str, ...] = (
    # Index ETFs
    "SPY", "QQQ", "DIA", "IWM",
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA",
    "ADBE", "CRM", "ORCL", "CSCO", "INTC", "AMD",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP",
    # Healthcare
    "JNJ", "PFE", "UNH", "ABBV", "MRK", "LLY", "BMY",
    # Energy
    "XOM", "CVX", "COP", "SLB",
    # Consumer / staples / discretionary
    "KO", "PEP", "MCD", "SBUX", "NKE", "DIS", "WMT", "HD", "COST", "TGT",
    # Industrials & payments
    "V", "MA", "PG", "MMM", "CAT", "BA", "LMT",
)

SLIPPAGE_PER_SIDE = 0.0005  # 5 bps round-trip leg
HARD_STOP_PCT = 0.03
TIER_SPLIT = 0.5  # 50/50 scale-out
MAX_HOLD_DAYS = 30
TRADING_DAYS_PER_YEAR = 252

# Where to write the run artefacts.
DEFAULT_OUTPUT_DIR = Path("backtests")
DEFAULT_RESULTS_FILE = "chop_backtest_results.json"
DEFAULT_REJECTED_FILE = "rejected_by_chop.csv"


# ────────────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    variant: str
    ticker: str
    entry_date: date
    entry_price: float
    tier1_exit_date: date | None = None
    tier1_exit_price: float | None = None
    tier2_exit_date: date | None = None
    tier2_exit_price: float | None = None
    stopped_out: bool = False
    holding_days: int = 0
    notional: float = 0.0
    pnl: float = 0.0
    return_pct: float = 0.0  # pnl / notional

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("entry_date", "tier1_exit_date", "tier2_exit_date"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


@dataclass
class VariantSummary:
    variant: str
    n_trades: int
    win_rate: float
    avg_return_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    profit_factor: float
    by_year: dict[int, dict] = field(default_factory=dict)


@dataclass
class RejectedRow:
    """A baseline-passing setup that the CHOP-enhanced gate refused.

    The simulated exit is computed identically to the baseline trade so the
    operator can scan ``rejected_by_chop.csv`` and judge whether CHOP threw
    out winners or losers on average.
    """

    ticker: str
    entry_date: date
    adx: float
    chop: float
    return_pct: float


# ────────────────────────────────────────────────────────────────────────────
# Data load + indicator precompute
# ────────────────────────────────────────────────────────────────────────────


async def load_bars(pool, tickers: tuple[str, ...], start: date, end: date) -> dict[str, pd.DataFrame]:
    """Pull every bar for ``tickers`` in [start, end] in one query."""
    sql = """
        SELECT ticker, date, open, high, low, close, volume
        FROM platform.prices_daily
        WHERE ticker = ANY($1) AND date BETWEEN $2 AND $3
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, list(tickers), start, end)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(
            {
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            }
        )
    out: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        if len(ticker_rows) < BB_PERIOD + ADX_PERIOD:
            continue
        df = pd.DataFrame(ticker_rows).set_index("date").sort_index()
        out[ticker] = df
    return out


def precompute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Layer ADX, CHOP, BB, band_proximity columns onto a per-ticker frame."""
    df = df.copy()
    df["adx"] = _compute_adx(df)
    df["chop"] = compute_chop(df["high"], df["low"], df["close"])
    sma, upper, lower, _ = _compute_bbands(df, period=BB_PERIOD, num_std=BB_NUM_STD)
    df["bb_mid"] = sma
    df["bb_upper"] = upper
    df["bb_lower"] = lower
    df["band_proximity"] = (df["close"] - lower) / (upper - lower).replace(0, np.nan)
    return df


# ────────────────────────────────────────────────────────────────────────────
# Trade simulation
# ────────────────────────────────────────────────────────────────────────────


def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    mid_band: float,
    upper_band: float,
    *,
    variant: str,
    ticker: str,
    entry_date: date,
) -> TradeRecord:
    """Walk forward day-by-day until both legs exit or we time out.

    Logic per bar (Sigma scale-out, simplified):
        if low <= stop_price → full stop-out at stop (slippage applied).
        elif we have NOT filled tier1 and high >= mid_band → fill tier1.
        elif tier1 already filled and high >= upper_band → fill tier2.

    Tier1 fill consumes 50% of qty; tier2 fill consumes the remainder. Stop
    sells the entire remaining position at the stop level.
    """
    stop_price = entry_price * (1.0 - HARD_STOP_PCT)
    notional = 1.0  # unit-normalized so PnL is a dollar return per $1 notional
    tier1_qty = TIER_SPLIT
    tier2_qty = 1.0 - TIER_SPLIT
    record = TradeRecord(
        variant=variant,
        ticker=ticker,
        entry_date=entry_date,
        entry_price=entry_price,
        notional=notional,
    )

    bars_left = min(MAX_HOLD_DAYS, len(df) - entry_idx - 1)
    pnl = 0.0
    for i in range(1, bars_left + 1):
        bar = df.iloc[entry_idx + i]
        high = float(bar["high"])
        low = float(bar["low"])
        # Stop check first — most punitive scenario, takes precedence.
        if low <= stop_price:
            sell_px = stop_price * (1.0 - SLIPPAGE_PER_SIDE)
            remaining_qty = (tier1_qty if record.tier1_exit_date is None else 0.0) + tier2_qty
            pnl += remaining_qty * (sell_px - entry_price)
            record.stopped_out = True
            record.tier2_exit_date = bar.name
            record.tier2_exit_price = sell_px
            record.holding_days = i
            break
        # Tier 1 fill?
        if record.tier1_exit_date is None and high >= mid_band:
            sell_px = mid_band * (1.0 - SLIPPAGE_PER_SIDE)
            pnl += tier1_qty * (sell_px - entry_price)
            record.tier1_exit_date = bar.name
            record.tier1_exit_price = sell_px
        # Tier 2 fill — only after tier 1 has filled (same bar OK).
        if record.tier1_exit_date is not None and high >= upper_band:
            sell_px = upper_band * (1.0 - SLIPPAGE_PER_SIDE)
            pnl += tier2_qty * (sell_px - entry_price)
            record.tier2_exit_date = bar.name
            record.tier2_exit_price = sell_px
            record.holding_days = i
            break
    else:
        # MAX_HOLD_DAYS expired without a clean exit — close at last-bar close.
        bar = df.iloc[entry_idx + bars_left]
        sell_px = float(bar["close"]) * (1.0 - SLIPPAGE_PER_SIDE)
        remaining_qty = (tier1_qty if record.tier1_exit_date is None else 0.0) + tier2_qty
        pnl += remaining_qty * (sell_px - entry_price)
        record.tier2_exit_date = bar.name
        record.tier2_exit_price = sell_px
        record.holding_days = bars_left

    record.pnl = pnl
    record.return_pct = pnl / entry_price if entry_price else 0.0
    return record


# ────────────────────────────────────────────────────────────────────────────
# Per-variant backtest loop
# ────────────────────────────────────────────────────────────────────────────


def run_variant(
    *,
    variant: str,
    panels: dict[str, pd.DataFrame],
    start: date,
    end: date,
    require_chop: bool,
) -> tuple[list[TradeRecord], list[RejectedRow]]:
    """Walk every trading day; pick top-1 candidate; simulate forward.

    Returns:
        (trades, rejected_rows).  ``rejected_rows`` is non-empty only on the
        baseline variant — it captures setups baseline allows but
        chop-enhanced would have refused, with their would-be returns.
    """
    # Build a global, sorted list of trading dates that appears in any panel.
    all_dates = sorted({d for df in panels.values() for d in df.index})
    all_dates = [d for d in all_dates if start <= d <= end]

    trades: list[TradeRecord] = []
    rejected: list[RejectedRow] = []
    next_eligible_idx = 0  # index into `all_dates` — single-position lockout.

    for di, today in enumerate(all_dates):
        if di < next_eligible_idx:
            continue

        best: tuple[float, str, pd.DataFrame, int] | None = None
        # (score, ticker, panel, idx-in-panel)
        for ticker, df in panels.items():
            if today not in df.index:
                continue
            row_pos = df.index.get_loc(today)
            if row_pos < BB_PERIOD + ADX_PERIOD:
                continue
            row = df.iloc[row_pos]
            adx = float(row["adx"])
            chop = float(row["chop"])
            if math.isnan(adx) or math.isnan(chop):
                continue
            if adx >= MAX_ADX:
                continue
            if require_chop and chop <= CHOP_SIDEWAYS_WEAK:
                continue
            bb_lower = float(row["bb_lower"])
            bb_upper = float(row["bb_upper"])
            if math.isnan(bb_lower) or math.isnan(bb_upper):
                continue
            prox = float(row["band_proximity"])
            if math.isnan(prox) or prox > 0.5:  # demand entry near lower half
                continue
            # Score: simple "channel quality + entry precision" proxy.
            #   low ADX is good (up to 20 pts); low band_proximity is good (up to 35).
            score = (20.0 - adx) + 35.0 * max(0.0, 1.0 - 2.0 * prox)
            if best is None or score > best[0]:
                best = (score, ticker, df, row_pos)

        if best is None:
            continue

        _score, ticker, df, idx = best
        # Entry next bar's open.
        if idx + 1 >= len(df):
            continue
        next_open_bar = df.iloc[idx + 1]
        entry_price = float(next_open_bar["open"]) * (1.0 + SLIPPAGE_PER_SIDE)
        mid_band = float(df.iloc[idx]["bb_mid"])
        upper_band = float(df.iloc[idx]["bb_upper"])
        record = simulate_trade(
            df,
            entry_idx=idx + 1,
            entry_price=entry_price,
            mid_band=mid_band,
            upper_band=upper_band,
            variant=variant,
            ticker=ticker,
            entry_date=df.index[idx + 1],
        )
        trades.append(record)
        # Lock out the strategy until the trade exits, so equity curves are
        # comparable across variants without phantom-overlap artefacts.
        if record.tier2_exit_date is not None:
            try:
                exit_global_idx = all_dates.index(record.tier2_exit_date)
            except ValueError:
                exit_global_idx = di + record.holding_days
            next_eligible_idx = exit_global_idx + 1
        else:
            next_eligible_idx = di + 1

        # On the baseline pass, also track which setups the CHOP gate would have
        # rejected (CHOP ≤ 38.2). We're already inside the "baseline-passes" branch.
        if not require_chop and float(df.iloc[idx]["chop"]) <= CHOP_SIDEWAYS_WEAK:
            rejected.append(
                RejectedRow(
                    ticker=ticker,
                    entry_date=df.index[idx + 1],
                    adx=float(df.iloc[idx]["adx"]),
                    chop=float(df.iloc[idx]["chop"]),
                    return_pct=record.return_pct,
                )
            )

    return trades, rejected


# ────────────────────────────────────────────────────────────────────────────
# Metrics
# ────────────────────────────────────────────────────────────────────────────


def compute_summary(variant: str, trades: list[TradeRecord]) -> VariantSummary:
    if not trades:
        return VariantSummary(
            variant=variant,
            n_trades=0,
            win_rate=0.0,
            avg_return_pct=0.0,
            sharpe_annualized=0.0,
            max_drawdown_pct=0.0,
            profit_factor=0.0,
        )
    returns = np.array([t.return_pct for t in trades], dtype=float)
    n = len(returns)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    win_rate = float(len(wins) / n) if n else 0.0
    avg_return = float(returns.mean())

    # Annualized Sharpe via trades-per-year scaling (no risk-free).
    span_days = (trades[-1].entry_date - trades[0].entry_date).days or 1
    trades_per_year = n / (span_days / 365.25) if span_days else n
    if returns.std(ddof=1) > 0 and len(returns) > 1:
        sharpe = float(avg_return / returns.std(ddof=1) * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0

    equity = np.concatenate(([1.0], 1.0 + np.cumsum(returns)))
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = float(drawdown.min())  # negative value

    gross_wins = float(wins.sum()) if len(wins) else 0.0
    gross_losses = float(-losses.sum()) if len(losses) else 0.0
    profit_factor = float(gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    by_year: dict[int, dict] = {}
    for year in sorted({t.entry_date.year for t in trades}):
        yr_returns = np.array([t.return_pct for t in trades if t.entry_date.year == year])
        yr_wins = yr_returns[yr_returns > 0]
        by_year[year] = {
            "n_trades": int(len(yr_returns)),
            "win_rate": float(len(yr_wins) / len(yr_returns)) if len(yr_returns) else 0.0,
            "avg_return_pct": float(yr_returns.mean()) if len(yr_returns) else 0.0,
            "total_return_pct": float(yr_returns.sum()),
        }

    return VariantSummary(
        variant=variant,
        n_trades=n,
        win_rate=win_rate,
        avg_return_pct=avg_return,
        sharpe_annualized=sharpe,
        max_drawdown_pct=max_dd,
        profit_factor=profit_factor,
        by_year=by_year,
    )


# ────────────────────────────────────────────────────────────────────────────
# Output
# ────────────────────────────────────────────────────────────────────────────


def render_summary(b: VariantSummary, c: VariantSummary) -> str:
    def fmt_pct(x: float) -> str:
        return f"{x*100:+.2f}%"

    def fmt_sharpe(x: float) -> str:
        return f"{x:+.2f}"

    rows = [
        ("trades", str(b.n_trades), str(c.n_trades)),
        ("win rate", fmt_pct(b.win_rate), fmt_pct(c.win_rate)),
        ("avg return / trade", fmt_pct(b.avg_return_pct), fmt_pct(c.avg_return_pct)),
        ("Sharpe (annualized)", fmt_sharpe(b.sharpe_annualized), fmt_sharpe(c.sharpe_annualized)),
        ("max drawdown", fmt_pct(b.max_drawdown_pct), fmt_pct(c.max_drawdown_pct)),
        (
            "profit factor",
            "inf" if math.isinf(b.profit_factor) else f"{b.profit_factor:.2f}",
            "inf" if math.isinf(c.profit_factor) else f"{c.profit_factor:.2f}",
        ),
    ]
    width_label = max(len(r[0]) for r in rows)
    width_val = max(len(r[1]) for r in rows + [("col", "baseline", "")])
    width_val = max(width_val, len("baseline"), len("chop-enhanced"))
    out = []
    out.append(f"  {'metric'.ljust(width_label)}    {'baseline'.rjust(width_val)}    {'chop-enhanced'.rjust(width_val)}")
    out.append(f"  {'-'*width_label}    {'-'*width_val}    {'-'*width_val}")
    for label, lhs, rhs in rows:
        out.append(f"  {label.ljust(width_label)}    {lhs.rjust(width_val)}    {rhs.rjust(width_val)}")
    return "\n".join(out)


def conclusion_line(b: VariantSummary, c: VariantSummary) -> str:
    if b.sharpe_annualized == 0:
        return "baseline Sharpe is zero — no comparison possible"
    delta = (c.sharpe_annualized - b.sharpe_annualized) / abs(b.sharpe_annualized)
    direction = "improved" if delta > 0 else "did not improve"
    return f"CHOP filter {direction} Sharpe by {delta*100:+.1f}% (baseline {b.sharpe_annualized:.2f} → enhanced {c.sharpe_annualized:.2f})"


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set — pass --database-url or export it.", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Pull bars for the universe in a single query.
    pool = await build_asyncpg_pool(db_url)
    try:
        logger.info(
            "loading bars  universe=%d  range=%s..%s",
            len(args.universe),
            args.start.isoformat(),
            args.end.isoformat(),
        )
        raw = await load_bars(pool, args.universe, args.start, args.end)
    finally:
        await pool.close()

    if not raw:
        print("platform.prices_daily returned 0 rows for the requested universe and window.")
        print("Populate the table (see tpcore.data.ingest_alpaca_bars) and re-run.")
        return 0

    logger.info("computing indicators  tickers=%d", len(raw))
    panels = {ticker: precompute_indicators(df) for ticker, df in raw.items()}

    logger.info("running baseline (ADX-only)")
    baseline_trades, rejected = run_variant(
        variant="baseline",
        panels=panels,
        start=args.start,
        end=args.end,
        require_chop=False,
    )

    logger.info("running chop-enhanced (ADX + CHOP > 38.2)")
    chop_trades, _ = run_variant(
        variant="chop-enhanced",
        panels=panels,
        start=args.start,
        end=args.end,
        require_chop=True,
    )

    baseline_summary = compute_summary("baseline", baseline_trades)
    chop_summary = compute_summary("chop-enhanced", chop_trades)

    print()
    print(f"Sigma CHOP backtest  {args.start} → {args.end}  universe={len(raw)} names")
    print()
    print(render_summary(baseline_summary, chop_summary))
    print()
    print(conclusion_line(baseline_summary, chop_summary))
    print()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "universe": list(args.universe),
        "n_universe_loaded": len(raw),
        "baseline": asdict(baseline_summary),
        "chop_enhanced": asdict(chop_summary),
        "conclusion": conclusion_line(baseline_summary, chop_summary),
    }
    results_path = args.output_dir / args.results_file
    results_path.write_text(json.dumps(payload, indent=2))
    print(f"results → {results_path}")

    if rejected and not args.skip_rejected_csv:
        rejected_path = args.output_dir / args.rejected_file
        with rejected_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ticker", "entry_date", "adx", "chop", "would_be_return_pct"])
            for row in rejected:
                writer.writerow(
                    [
                        row.ticker,
                        row.entry_date.isoformat(),
                        f"{row.adx:.4f}",
                        f"{row.chop:.4f}",
                        f"{row.return_pct:.6f}",
                    ]
                )
        print(f"rejected-by-chop → {rejected_path}  rows={len(rejected)}")

    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument(
        "--universe",
        type=lambda s: tuple(t.strip().upper() for t in s.split(",") if t.strip()),
        default=DEFAULT_UNIVERSE,
        help="Comma-separated tickers (default: 50 large caps).",
    )
    p.add_argument("--database-url", default=None, help="Postgres URL; defaults to $DATABASE_URL.")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--results-file", default=DEFAULT_RESULTS_FILE)
    p.add_argument("--rejected-file", default=DEFAULT_REJECTED_FILE)
    p.add_argument("--skip-rejected-csv", action="store_true")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
