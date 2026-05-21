"""Provider-agnostic backtest harness, credibility rubric, cost model."""

from .cost_model import SimpleCostModel
from .credibility import BacktestCredibilityRubric, CredibilityScore
from .equivalence import (
    EquivalenceReport,
    TradeMismatch,
    assert_trade_lists_equal,
    compare_trade_lists,
)
from .filter_diagnostics import FilterDiagnostics
from .harness import BacktestConfig, BacktestHarness, BacktestResult, Strategy
from .overfitting import OverfittingDiagnostic, OverfittingReport, cscv_pbo

__all__ = [
    "BacktestConfig",
    "BacktestCredibilityRubric",
    "BacktestHarness",
    "BacktestResult",
    "CredibilityScore",
    "EquivalenceReport",
    "FilterDiagnostics",
    "OverfittingDiagnostic",
    "OverfittingReport",
    "SimpleCostModel",
    "Strategy",
    "TradeMismatch",
    "assert_trade_lists_equal",
    "compare_trade_lists",
    "cscv_pbo",
]
