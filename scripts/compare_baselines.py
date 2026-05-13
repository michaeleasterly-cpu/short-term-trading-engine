"""Diff two trade-log CSVs and report whether they're equivalent.

Used as a regression-safety gate when refactoring engines or migrating
between strategy constructions (e.g., monthly-rebalance → rolling).

Workflow:

    1. Before the change: run the backtest with ``--trade-log baseline.csv``.
    2. Make the change.
    3. Re-run: ``--trade-log candidate.csv``.
    4. ``python scripts/compare_baselines.py --baseline baseline.csv \
            --candidate candidate.csv``
    5. Exit 0 if equivalent within tolerance; exit 1 with a structured
       diff if not.

Tolerances are absolute, not relative. Defaults are tight (1e-6 on
pnl_pct, 1e-4 on prices) — relax via ``--tol-pnl-pct`` and
``--tol-price`` if you're comparing across a known-non-deterministic
change.

CSV shape is the standard :func:`tpcore.backtest.search.write_trade_log_csv`
output: columns ``ticker``, ``entry_date``, ``entry_price``,
``exit_date``, ``exit_price``, ``pnl_pct``, ``direction``,
``exit_reason``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tpcore.backtest import compare_trade_lists
from tpcore.backtest.equivalence import DEFAULT_TOL_PNL_PCT, DEFAULT_TOL_PRICE
from tpcore.backtest.search import read_trade_log_csv


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--baseline", type=Path, required=True, help="Known-good trade-log CSV.")
    p.add_argument("--candidate", type=Path, required=True, help="Trade-log CSV to compare against the baseline.")
    p.add_argument(
        "--tol-pnl-pct", type=float, default=DEFAULT_TOL_PNL_PCT,
        help="Absolute tolerance on pnl_pct (default 1e-6).",
    )
    p.add_argument(
        "--tol-price", type=float, default=DEFAULT_TOL_PRICE,
        help="Absolute tolerance on entry_price / exit_price (default 1e-4).",
    )
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args()
    if not args.baseline.exists():
        print(f"baseline file not found: {args.baseline}", file=sys.stderr)
        raise SystemExit(2)
    if not args.candidate.exists():
        print(f"candidate file not found: {args.candidate}", file=sys.stderr)
        raise SystemExit(2)

    baseline = read_trade_log_csv(args.baseline)
    candidate = read_trade_log_csv(args.candidate)
    report = compare_trade_lists(
        baseline, candidate,
        tol_pnl_pct=args.tol_pnl_pct, tol_price=args.tol_price,
    )
    print(report.summary())
    raise SystemExit(0 if report.equivalent else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
