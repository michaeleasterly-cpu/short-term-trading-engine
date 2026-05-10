"""CLI for the overfitting diagnostic.

::

    python -m tpcore.backtest --trades-file backtests/reversion_trades.json \\
        --parameters '{"z_threshold": 3.0, "quality_grade": "HIGH"}' \\
        --sr 0.28 --trials 45 [--engine reversion] [--price-data prices.csv]

Or with everything in a JSON config::

    python -m tpcore.backtest --config overfitting_config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .overfitting import OverfittingDiagnostic


def _coerce_date(value: Any) -> Any:
    """Best-effort: ``"2024-01-15"`` → date(2024, 1, 15). Pass through otherwise."""
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return value


def _normalize_trades(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for t in raw:
        trade = dict(t)
        # accept the common alias
        if "pnl_pct" not in trade and "return_pct" in trade:
            trade["pnl_pct"] = trade["return_pct"]
        for k in ("entry_date", "exit_date"):
            if k in trade:
                trade[k] = _coerce_date(trade[k])
        out.append(trade)
    return out


def _load_price_data(path: Path | None) -> pd.DataFrame | None:
    if path is None:
        return None
    if not path.exists():
        print(f"warning: price-data path {path} does not exist; skipping", file=sys.stderr)
        return None
    df = pd.read_csv(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tpcore.backtest",
        description="Run the nine-test overfitting diagnostic on a backtest's trades.",
    )
    p.add_argument("--config", type=Path, help="JSON file with all CLI parameters.")
    p.add_argument("--trades-file", type=Path, help="JSON file: list of trade dicts.")
    p.add_argument(
        "--parameters",
        type=str,
        help='JSON-encoded parameter dict, e.g. \'{"z_threshold": 3.0}\'.',
    )
    p.add_argument("--sr", dest="sr", type=float, help="Observed Sharpe ratio.")
    p.add_argument("--trials", type=int, help="Honest count of parameter combinations tried.")
    p.add_argument("--benchmark-sr", type=float, default=0.0, help="Benchmark Sharpe (default 0.0).")
    p.add_argument("--engine", type=str, default="unknown", help="Engine name for the report.")
    p.add_argument("--price-data", type=Path, help="CSV of daily bars (ticker,date,close,...).")
    p.add_argument(
        "--output",
        type=Path,
        help="Where to save the JSON report. Default: backtests/<engine>_overfitting_report.json.",
    )
    return p


def _resolve_args(argv: list[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.config is not None:
        with args.config.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in cfg.items():
            cli_key = k.replace("-", "_")
            if getattr(args, cli_key, None) in (None, parser.get_default(cli_key)):
                if cli_key in {"trades_file", "price_data", "output"} and isinstance(v, str):
                    v = Path(v)
                setattr(args, cli_key, v)
    # Required after config-merge
    missing = [k for k in ("trades_file", "parameters", "sr", "trials") if getattr(args, k, None) is None]
    if missing:
        parser.error(f"missing required arguments: {', '.join(missing)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _resolve_args(argv)

    with args.trades_file.open("r", encoding="utf-8") as f:
        raw_trades = json.load(f)
    trades = _normalize_trades(raw_trades)
    parameters = (
        args.parameters if isinstance(args.parameters, dict) else json.loads(args.parameters)
    )
    price_data = _load_price_data(args.price_data)

    diag = OverfittingDiagnostic(
        trades=trades,
        parameters=parameters,
        sr_observed=float(args.sr),
        n_trials=int(args.trials),
        price_data=price_data,
        benchmark_sr=float(args.benchmark_sr),
        engine=str(args.engine),
    )
    report = diag.run()

    out_path = args.output or Path("backtests") / f"{args.engine}_overfitting_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    # stdout: formatted JSON for piping/inspection
    print(report.model_dump_json(indent=2))
    print(f"\nSaved: {out_path}", file=sys.stderr)
    return 0 if report.overall_passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["build_parser", "main"]
