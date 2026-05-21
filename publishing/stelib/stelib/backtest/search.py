"""Shared dataclass + helpers for the parameter-search pipeline.

Each engine's ``backtest.py`` exposes ``run_for_search(...)`` that produces a
:class:`BacktestRunResult`. ``scripts/search_parameters.py`` imports these
directly (no subprocess) and uses the result to rank parameter combinations.

The JSON-output mode of each engine's CLI also serialises a
``BacktestRunResult`` so the same data is available to ad-hoc tooling.
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stelib.backtest.credibility import BacktestCredibilityRubric, CredibilityScore
from stelib.backtest.overfitting import OverfittingDiagnostic, OverfittingReport


@dataclass
class SearchTrade:
    """Standardised trade row consumed by the orchestrator / replay step."""

    ticker: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    pnl_pct: float
    direction: str  # "LONG" or "SHORT"
    exit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "entry_date": self.entry_date.isoformat(),
            "entry_price": self.entry_price,
            "exit_date": self.exit_date.isoformat(),
            "exit_price": self.exit_price,
            "pnl_pct": self.pnl_pct,
            "direction": self.direction,
            "exit_reason": self.exit_reason,
        }


@dataclass
class BacktestRunResult:
    """Single-parameter-set backtest outcome, in JSON-serialisable shape.

    The ``credibility_rubric`` field carries the full Pydantic
    :class:`~tpcore.backtest.credibility.CredibilityScore` (10 boolean
    rubric flags + integer score) for callers that need to persist or
    render the breakdown. ``credibility_score`` is the same number as an
    int for terse JSON output."""

    engine: str
    parameters: dict[str, Any]
    credibility_score: int
    passed_gate: bool
    sharpe: float
    profit_factor: float
    max_drawdown: float
    trades: int
    dsr: float
    min_btl_gap: int  # negative → already enough trades; positive → shortfall
    trades_per_param: float
    sensitivity_score: float | None
    ruin_probability: float
    trade_log: list[SearchTrade] = field(default_factory=list)
    credibility_rubric: CredibilityScore | None = None  # full Pydantic object

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "parameters": self.parameters,
            "credibility_score": self.credibility_score,
            "passed_gate": self.passed_gate,
            "sharpe": float(self.sharpe) if math.isfinite(self.sharpe) else None,
            "profit_factor": (
                float(self.profit_factor) if math.isfinite(self.profit_factor) else None
            ),
            "max_drawdown": float(self.max_drawdown),
            "trades": self.trades,
            "dsr": float(self.dsr) if math.isfinite(self.dsr) else None,
            "min_btl_gap": self.min_btl_gap,
            "trades_per_param": float(self.trades_per_param),
            "sensitivity_score": (
                float(self.sensitivity_score) if self.sensitivity_score is not None else None
            ),
            "ruin_probability": float(self.ruin_probability),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict(), indent=2)


def write_trade_log_csv(path: Path, trades: list[SearchTrade]) -> int:
    """Standardised search-trade CSV. Returns the row count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "ticker",
                "entry_date",
                "entry_price",
                "exit_date",
                "exit_price",
                "pnl_pct",
                "direction",
                "exit_reason",
            ]
        )
        for t in trades:
            w.writerow(
                [
                    t.ticker,
                    t.entry_date.isoformat(),
                    f"{t.entry_price:.6f}",
                    t.exit_date.isoformat(),
                    f"{t.exit_price:.6f}",
                    f"{t.pnl_pct:.6f}",
                    t.direction,
                    t.exit_reason,
                ]
            )
    return len(trades)


def read_trade_log_csv(path: Path) -> list[SearchTrade]:
    out: list[SearchTrade] = []
    with path.open("r", newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(
                SearchTrade(
                    ticker=row["ticker"],
                    entry_date=date.fromisoformat(row["entry_date"]),
                    entry_price=float(row["entry_price"]),
                    exit_date=date.fromisoformat(row["exit_date"]),
                    exit_price=float(row["exit_price"]),
                    pnl_pct=float(row["pnl_pct"]),
                    direction=row["direction"],
                    exit_reason=row["exit_reason"],
                )
            )
    return out


def _mean_sensitivity(report: OverfittingReport) -> float | None:
    """Mean of per-numeric-parameter sensitivity scores. None if no scores."""
    if not report.sensitivity:
        return None
    scores = [
        float(d.get("score"))
        for d in report.sensitivity.values()
        if isinstance(d, dict) and d.get("score") is not None
    ]
    if not scores:
        return None
    return float(np.mean(scores))


def compute_search_metrics(
    *,
    engine: str,
    parameters: dict[str, Any],
    trades_for_diag: list[dict[str, Any]],
    sharpe: float,
    profit_factor: float,
    max_drawdown: float,
    n_trials: int,
    price_data: pd.DataFrame,
    rubric_inputs: dict[str, bool],
    search_trades: list[SearchTrade],
    out_of_sample_validated: bool = False,
) -> BacktestRunResult:
    """Run the overfitting diagnostic + credibility rubric and bundle them.

    Args:
        trades_for_diag: list-of-dicts in the schema ``OverfittingDiagnostic``
            expects (``pnl_pct``, ``entry_date``, ``exit_date``, ``direction``,
            ``ticker``, ``entry_price``).
        rubric_inputs: ``lookahead_clean``, ``survivorship_inclusive``,
            ``pit_fundamentals``, ``regime_coverage``, ``monte_carlo_drawdown``
            (the integrity flags that don't come out of the diagnostic).
        out_of_sample_validated: if True, the rubric gets that flag set.
            The orchestrator only ever sets this on the final-holdout pass.
    """
    diag = OverfittingDiagnostic(
        trades=trades_for_diag,
        parameters=parameters,
        sr_observed=float(sharpe),
        n_trials=n_trials,
        price_data=price_data,
        engine=engine,
    )
    report = diag.run()
    rubric: CredibilityScore = BacktestCredibilityRubric().evaluate_with_overfitting(
        report,
        lookahead_clean=rubric_inputs.get("lookahead_clean", True),
        survivorship_inclusive=rubric_inputs.get("survivorship_inclusive", True),
        pit_fundamentals=rubric_inputs.get("pit_fundamentals", True),
        regime_coverage=rubric_inputs.get("regime_coverage", True),
        out_of_sample_validated=out_of_sample_validated,
        monte_carlo_drawdown=rubric_inputs.get("monte_carlo_drawdown", True),
    )
    n_trades = len(trades_for_diag)
    min_btl_gap = int(report.min_btl_days) - n_trades
    return BacktestRunResult(
        engine=engine,
        parameters=parameters,
        credibility_score=int(rubric.score),
        passed_gate=bool(rubric.passes_gate),
        sharpe=float(sharpe),
        profit_factor=float(profit_factor),
        max_drawdown=float(max_drawdown),
        trades=n_trades,
        dsr=float(report.dsr_value),
        min_btl_gap=min_btl_gap,
        trades_per_param=float(report.trades_per_param_ratio),
        sensitivity_score=_mean_sensitivity(report),
        ruin_probability=float(report.mc_ruin_probability),
        trade_log=search_trades,
        credibility_rubric=rubric,
    )
