"""Diagnose Reversion's per-trade losses on the 8-year backtest.

Reads ``backtests/reversion_trades.csv`` (produced by
``reversion/backtest.py``) and slices the trade ledger six
ways:

    A. By |Z-score| bucket at entry
    B. By holding period (1..5 days)
    C. By exit reason (target / stop / time-out / max-hold)
    D. By direction (LONG vs SHORT)
    E. Top 10 worst trades
    F. By earnings-quality grade

Intended as a one-off diagnostic to surface the strategy's primary failure
mode before tuning thresholds. Pure I/O — no DB calls. The CSV already has
everything needed; if a field is empty (older CSVs), the corresponding cut
is skipped with a note.

Run::

    python reversion/diagnose_backtest.py
    python reversion/diagnose_backtest.py --csv backtests/reversion_trades.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from statistics import mean


_DEFAULT_CSV = Path("backtests/reversion_trades.csv")
_DEFAULT_REPORT = Path("backtests/reversion_diagnosis.txt")


# Hardcoded sector map for the 50-name backtest universe. Used in the
# top-10-worst section to spot sector concentration. Tickers not in the map
# come back as "Unknown" rather than failing the report.
_SECTORS: dict[str, str] = {
    # Tech
    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "META": "Tech",
    "NVDA": "Tech", "PLTR": "Tech", "CRM": "Tech", "ORCL": "Tech",
    # Consumer (mixed discretionary + staples)
    "AMZN": "Consumer", "TSLA": "Consumer", "WMT": "Consumer", "COST": "Consumer",
    "HD": "Consumer", "LOW": "Consumer", "MCD": "Consumer", "SBUX": "Consumer",
    "TGT": "Consumer", "NKE": "Consumer", "DIS": "Consumer", "NFLX": "Consumer",
    "PG": "Consumer", "KO": "Consumer", "PEP": "Consumer",
    "ABNB": "Consumer", "UBER": "Consumer", "RBLX": "Consumer",
    # Financials
    "JPM": "Financials", "V": "Financials",
    # Healthcare
    "JNJ": "Healthcare", "PFE": "Healthcare", "MRK": "Healthcare", "ABBV": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy",
    # Industrials / Aerospace
    "BA": "Industrials", "CAT": "Industrials", "GE": "Industrials", "GM": "Industrials",
    "F": "Industrials", "LMT": "Defense", "RTX": "Defense", "NOC": "Defense", "GD": "Defense",
    # Utilities
    "SO": "Utilities", "DUK": "Utilities", "NEE": "Utilities",
    # Hyper-growth / IPOs
    "SNAP": "Tech", "RIVN": "EV", "LCID": "EV", "FSLR": "Energy",
}


@dataclass
class Trade:
    ticker: str
    direction: str  # "long" | "short"
    entry_date: str
    entry_price: float
    exit_reason: str
    holding_days: int
    return_pct: float
    quality_grade: str
    z_score_at_entry: float | None
    rsi_at_entry: float | None
    adx_at_entry: float | None

    @property
    def sector(self) -> str:
        return _SECTORS.get(self.ticker, "Unknown")

    @property
    def is_winner(self) -> bool:
        return self.return_pct > 0


def _f(s: str) -> float | None:
    if s == "" or s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _i(s: str) -> int:
    return int(s) if s else 0


def load_trades(csv_path: Path) -> list[Trade]:
    trades: list[Trade] = []
    with csv_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            trades.append(
                Trade(
                    ticker=row["ticker"],
                    direction=row["direction"],
                    entry_date=row["entry_date"],
                    entry_price=float(row["entry_price"]),
                    exit_reason=row["exit_reason"],
                    holding_days=_i(row["holding_days"]),
                    return_pct=float(row["return_pct"]),
                    quality_grade=row["quality_grade"],
                    z_score_at_entry=_f(row.get("z_score_at_entry", "")),
                    rsi_at_entry=_f(row.get("rsi_at_entry", "")),
                    adx_at_entry=_f(row.get("adx_at_entry", "")),
                )
            )
    return trades


# ────────────────────────────────────────────────────────────────────────────
# Bucket math
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class BucketStats:
    label: str
    n: int
    win_rate: float
    avg_return: float
    profit_factor: float

    @classmethod
    def of(cls, label: str, trades: list[Trade]) -> "BucketStats":
        if not trades:
            return cls(label=label, n=0, win_rate=0.0, avg_return=0.0, profit_factor=0.0)
        wins = [t.return_pct for t in trades if t.return_pct > 0]
        losses = [t.return_pct for t in trades if t.return_pct < 0]
        gross_wins = sum(wins)
        gross_losses = -sum(losses)  # positive
        if gross_losses > 0:
            pf = gross_wins / gross_losses
        else:
            pf = float("inf") if gross_wins > 0 else 0.0
        return cls(
            label=label,
            n=len(trades),
            win_rate=len(wins) / len(trades),
            avg_return=mean(t.return_pct for t in trades),
            profit_factor=pf,
        )


def _bucket_z(t: Trade) -> str | None:
    if t.z_score_at_entry is None:
        return None
    z = abs(t.z_score_at_entry)
    if z < 2.0:
        return "|Z| < 2.0 (below threshold — should not occur)"
    if z < 2.5:
        return "|Z| 2.0–2.5"
    if z < 3.0:
        return "|Z| 2.5–3.0"
    if z < 4.0:
        return "|Z| 3.0–4.0"
    return "|Z| >= 4.0"


def _bucket_holding(t: Trade) -> str:
    if t.holding_days <= 1:
        return "1 day"
    if t.holding_days == 2:
        return "2 days"
    if t.holding_days == 3:
        return "3 days"
    if t.holding_days == 4:
        return "4 days"
    if t.holding_days == 5:
        return "5 days (time stop)"
    return f"{t.holding_days} days (over time stop)"


# ────────────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────────────


def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def _fmt_pf(x: float) -> str:
    if x == float("inf"):
        return "inf"
    return f"{x:.2f}"


def _render_bucket_table(out: StringIO, header: str, buckets: list[BucketStats]) -> None:
    out.write(f"{header}\n")
    out.write(
        f"  {'bucket':30s}  {'n':>4s}  {'win_rate':>9s}  {'avg_ret':>9s}  {'profit_factor':>13s}\n"
    )
    out.write("  " + "-" * 30 + "  " + "-" * 4 + "  " + "-" * 9 + "  " + "-" * 9 + "  " + "-" * 13 + "\n")
    for b in buckets:
        out.write(
            f"  {b.label:30s}  {b.n:4d}  {_fmt_pct(b.win_rate):>9s}  "
            f"{_fmt_pct(b.avg_return):>9s}  {_fmt_pf(b.profit_factor):>13s}\n"
        )
    out.write("\n")


def _build_report(trades: list[Trade]) -> str:
    out = StringIO()
    out.write("Reversion backtest diagnosis\n")
    out.write(f"  source: {len(trades)} baseline trades\n")
    out.write(
        f"  span: {min(t.entry_date for t in trades)} → {max(t.entry_date for t in trades)}\n"
    )
    out.write("\n")

    # --- A. Z-score buckets -------------------------------------------------
    z_present = [t for t in trades if t.z_score_at_entry is not None]
    if z_present:
        order = [
            "|Z| < 2.0 (below threshold — should not occur)",
            "|Z| 2.0–2.5",
            "|Z| 2.5–3.0",
            "|Z| 3.0–4.0",
            "|Z| >= 4.0",
        ]
        groups: dict[str, list[Trade]] = {k: [] for k in order}
        for t in z_present:
            b = _bucket_z(t)
            if b is not None:
                groups[b].append(t)
        buckets = [BucketStats.of(label, groups[label]) for label in order if groups[label]]
        _render_bucket_table(out, "A. By |Z-score| at entry", buckets)
    else:
        out.write("A. By |Z-score| at entry — SKIPPED (no z_score_at_entry data)\n\n")

    # --- B. Holding period --------------------------------------------------
    order_hold = [
        "1 day", "2 days", "3 days", "4 days", "5 days (time stop)",
    ]
    groups: dict[str, list[Trade]] = {k: [] for k in order_hold}
    extras: dict[str, list[Trade]] = {}
    for t in trades:
        b = _bucket_holding(t)
        if b in groups:
            groups[b].append(t)
        else:
            extras.setdefault(b, []).append(t)
    buckets = [BucketStats.of(label, groups[label]) for label in order_hold if groups[label]]
    for k in sorted(extras):
        buckets.append(BucketStats.of(k, extras[k]))
    _render_bucket_table(out, "B. By holding period", buckets)

    # --- C. Exit reason -----------------------------------------------------
    order_exit = ["target", "stop", "time_out", "max_hold"]
    groups = {k: [] for k in order_exit}
    for t in trades:
        groups.setdefault(t.exit_reason, []).append(t)
    buckets = [BucketStats.of(label, groups[label]) for label in order_exit if groups.get(label)]
    _render_bucket_table(out, "C. By exit reason", buckets)

    # --- D. Direction -------------------------------------------------------
    longs = [t for t in trades if t.direction == "long"]
    shorts = [t for t in trades if t.direction == "short"]
    buckets = []
    if longs:
        buckets.append(BucketStats.of("LONG", longs))
    if shorts:
        buckets.append(BucketStats.of("SHORT", shorts))
    _render_bucket_table(out, "D. By direction", buckets)

    # --- E. Top 10 worst ----------------------------------------------------
    out.write("E. Top 10 worst trades\n")
    out.write(
        f"  {'#':>3s} {'ticker':6s} {'dir':5s} {'entry_date':10s} "
        f"{'sector':10s} {'Z':>6s} {'RSI':>6s} {'ADX':>6s} "
        f"{'exit':10s} {'days':>4s} {'EQ':>7s} {'return':>9s}\n"
    )
    out.write("  " + "-" * 100 + "\n")
    worst = sorted(trades, key=lambda t: t.return_pct)[:10]
    for i, t in enumerate(worst, 1):
        z = f"{t.z_score_at_entry:+.2f}" if t.z_score_at_entry is not None else "—"
        rsi = f"{t.rsi_at_entry:.1f}" if t.rsi_at_entry is not None else "—"
        adx = f"{t.adx_at_entry:.1f}" if t.adx_at_entry is not None else "—"
        out.write(
            f"  {i:3d} {t.ticker:6s} {t.direction:5s} {t.entry_date:10s} "
            f"{t.sector:10s} {z:>6s} {rsi:>6s} {adx:>6s} "
            f"{t.exit_reason:10s} {t.holding_days:4d} {t.quality_grade:>7s} "
            f"{_fmt_pct(t.return_pct):>9s}\n"
        )
    out.write("\n")
    # Sector concentration in the worst-10
    sector_counts = Counter(t.sector for t in worst)
    out.write("  worst-10 sector counts: ")
    out.write(", ".join(f"{s}={n}" for s, n in sector_counts.most_common()))
    out.write("\n\n")

    # --- F. Earnings quality grade -----------------------------------------
    order_eq = ["high", "medium", "low", "no_data"]
    groups = {k: [] for k in order_eq}
    for t in trades:
        groups.setdefault(t.quality_grade, []).append(t)
    buckets = [BucketStats.of(label, groups[label]) for label in order_eq if groups.get(label)]
    _render_bucket_table(out, "F. By earnings-quality grade", buckets)

    # --- Conclusion ---------------------------------------------------------
    out.write("Conclusion\n")
    out.write("  " + _diagnose(trades) + "\n")
    return out.getvalue()


def _diagnose(trades: list[Trade]) -> str:
    """One-paragraph synthesis — primary failure mode and highest-leverage fix.

    Built mechanically from the cuts so the conclusion always matches the
    data. The logic looks for the *strongest* separating filter — the
    single dimension where one bucket is decisively profitable and the
    others are decisively not — and reports that as the highest-leverage
    fix. Falls back to the dominant losing exit type if no clean separator
    exists.
    """
    n = len(trades)
    if n == 0:
        return "No trades to diagnose."

    # Build per-cut summaries we can reason about.
    eq_groups: dict[str, list[Trade]] = {}
    for t in trades:
        eq_groups.setdefault(t.quality_grade, []).append(t)
    eq_stats = {k: BucketStats.of(k, ts) for k, ts in eq_groups.items() if ts}

    z_buckets_order = ["|Z| 2.0–2.5", "|Z| 2.5–3.0", "|Z| 3.0–4.0", "|Z| >= 4.0"]
    z_groups: dict[str, list[Trade]] = {k: [] for k in z_buckets_order}
    for t in trades:
        b = _bucket_z(t)
        if b in z_groups:
            z_groups[b].append(t)
    z_stats = {k: BucketStats.of(k, z_groups[k]) for k in z_buckets_order if z_groups[k]}

    by_exit: dict[str, list[Trade]] = {}
    for t in trades:
        by_exit.setdefault(t.exit_reason, []).append(t)

    # Identify the single most-profitable EQ grade and Z bucket.
    profitable_eq = [k for k, s in eq_stats.items() if s.avg_return > 0 and s.profit_factor > 1.0]
    profitable_z = [k for k, s in z_stats.items() if s.avg_return > 0 and s.profit_factor > 1.0]
    losing_eq = [k for k, s in eq_stats.items() if s.avg_return < 0]
    losing_z = [k for k, s in z_stats.items() if s.avg_return < 0]

    # Headline failure mode = the largest losing exit reason by trade count.
    primary_loss_exit, primary_loss_trades = max(
        ((k, ts) for k, ts in by_exit.items() if mean(t.return_pct for t in ts) < 0),
        key=lambda kv: len(kv[1]),
        default=("none", []),
    )
    parts: list[str] = []
    if primary_loss_exit != "none":
        primary_loss_avg = mean(t.return_pct for t in primary_loss_trades)
        parts.append(
            f"The primary failure mode is {primary_loss_exit} exits "
            f"(avg return {_fmt_pct(primary_loss_avg)} across {len(primary_loss_trades)} of {n} trades)."
        )

    # Decide which lever has the highest leverage based on the cleanness of
    # the separation. Prefer the cut where the profitable subset is
    # decisively positive AND the rest is decisively negative.
    fix_clauses: list[str] = []
    if profitable_eq and losing_eq:
        best_eq = max(profitable_eq, key=lambda k: eq_stats[k].avg_return)
        s = eq_stats[best_eq]
        n_other = sum(eq_stats[k].n for k in losing_eq)
        avg_other = (
            sum(eq_stats[k].n * eq_stats[k].avg_return for k in losing_eq) / n_other
            if n_other
            else 0.0
        )
        fix_clauses.append(
            f"earnings-quality cut is the cleanest separator — {best_eq.upper()} is profitable "
            f"({s.n} trades, avg {_fmt_pct(s.avg_return)}, PF {s.profit_factor:.2f}) while "
            f"the other grades combined ({n_other} trades) average {_fmt_pct(avg_other)}. "
            f"The gate currently rejects only LOW; tightening it to require {best_eq.upper()} "
            "would remove most of the loss tail at the cost of trade count."
        )
    if profitable_z and losing_z:
        best_z = max(profitable_z, key=lambda k: z_stats[k].avg_return)
        s = z_stats[best_z]
        n_lower = sum(z_stats[k].n for k in losing_z)
        avg_lower = (
            sum(z_stats[k].n * z_stats[k].avg_return for k in losing_z) / n_lower
            if n_lower
            else 0.0
        )
        fix_clauses.append(
            f"|Z|-threshold tightening is the second lever — {best_z} is profitable "
            f"({s.n} trades, avg {_fmt_pct(s.avg_return)}, PF {s.profit_factor:.2f}) while "
            f"|Z| < the threshold of that bucket ({n_lower} trades) averages {_fmt_pct(avg_lower)}."
        )

    if fix_clauses:
        parts.append("The highest-leverage fix: " + " Additionally, ".join(fix_clauses))
    else:
        parts.append(
            "No single cut cleanly separates winners from losers — the strategy needs a structural review "
            "rather than a one-knob threshold change."
        )
    return " ".join(parts)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--csv", type=Path, default=_DEFAULT_CSV)
    p.add_argument("--report", type=Path, default=_DEFAULT_REPORT)
    args = p.parse_args()

    if not args.csv.exists():
        raise SystemExit(
            f"{args.csv} not found — run `python reversion/backtest.py` "
            "with --start 2018-01-01 --end 2025-12-31 first."
        )

    trades = load_trades(args.csv)
    report = _build_report(trades)
    print(report)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report)
    print(f"\nreport saved → {args.report}")


if __name__ == "__main__":
    main()


__all__ = ["BucketStats", "Trade", "load_trades", "main"]
