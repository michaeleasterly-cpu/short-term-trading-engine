"""Trade-list equivalence checking for baseline regression tests.

After any refactor that touches an engine's setup-detection / sizing /
execution code, the operator wants confidence the trade outputs are
*identical* to a known-good baseline. This module provides:

* :class:`EquivalenceReport` — structured diff of two trade lists
* :func:`compare_trade_lists` — pure function returning the report
* :func:`assert_trade_lists_equal` — raise-on-mismatch wrapper for tests

The comparison key is ``(ticker, entry_date, exit_date, direction)`` —
unique enough for any engine (multiple closed trades on the same ticker
have distinct entry/exit dates). Numeric fields (entry_price, exit_price,
pnl_pct) are compared within a tolerance.

Usage in a regression test::

    baseline = read_trade_log_csv(Path("backtests/baseline.csv"))
    candidate = run_engine_and_get_trades(...)
    assert_trade_lists_equal(baseline, candidate)  # raises on mismatch

Usage in a diff CLI (``scripts/compare_baselines.py``)::

    report = compare_trade_lists(baseline, candidate)
    if not report.equivalent:
        print(report.summary())
        sys.exit(1)
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from tpcore.backtest.search import SearchTrade

# Default tolerance for numeric comparisons. 1e-6 catches floating-point
# jitter from legitimate compute paths while still flagging real numeric
# changes (basis-point-scale and larger).
DEFAULT_TOL_PNL_PCT: float = 1e-6
DEFAULT_TOL_PRICE: float = 1e-4  # cent-level — Alpaca quotes don't go finer


TradeKey = tuple[str, date, date, str]


class TradeMismatch(BaseModel):
    """One per-trade mismatch line in the report."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    entry_date: date
    exit_date: date
    direction: str
    field: str = Field(description="Field that mismatched: pnl_pct / entry_price / exit_price.")
    baseline_value: float
    candidate_value: float
    delta: float = Field(description="candidate - baseline.")


class EquivalenceReport(BaseModel):
    """Structured comparison of two trade lists.

    ``equivalent=True`` iff every trade in baseline appears (by key) in
    candidate and vice versa, AND every paired trade's numeric fields
    match within tolerance."""

    model_config = ConfigDict(extra="forbid")

    equivalent: bool
    n_baseline: int = Field(ge=0)
    n_candidate: int = Field(ge=0)
    missing_in_candidate: list[TradeKey] = Field(default_factory=list)
    extra_in_candidate: list[TradeKey] = Field(default_factory=list)
    mismatches: list[TradeMismatch] = Field(default_factory=list)
    tol_pnl_pct: float = DEFAULT_TOL_PNL_PCT
    tol_price: float = DEFAULT_TOL_PRICE

    def summary(self) -> str:
        """Human-readable summary suitable for terminal output."""
        if self.equivalent:
            return (
                f"✓ trade lists EQUIVALENT — {self.n_baseline} trades, every match "
                f"within tol_pnl_pct={self.tol_pnl_pct} tol_price={self.tol_price}"
            )
        out = [
            f"✗ trade lists DIFFER — baseline={self.n_baseline} candidate={self.n_candidate}",
        ]
        if self.missing_in_candidate:
            out.append(
                f"  missing in candidate ({len(self.missing_in_candidate)}): "
                + ", ".join(
                    f"{t[0]}@{t[1].isoformat()}→{t[2].isoformat()}"
                    for t in self.missing_in_candidate[:10]
                )
                + ("…" if len(self.missing_in_candidate) > 10 else "")
            )
        if self.extra_in_candidate:
            out.append(
                f"  extra in candidate ({len(self.extra_in_candidate)}): "
                + ", ".join(
                    f"{t[0]}@{t[1].isoformat()}→{t[2].isoformat()}"
                    for t in self.extra_in_candidate[:10]
                )
                + ("…" if len(self.extra_in_candidate) > 10 else "")
            )
        if self.mismatches:
            out.append(f"  numeric mismatches ({len(self.mismatches)}):")
            for m in self.mismatches[:10]:
                out.append(
                    f"    {m.ticker} {m.entry_date.isoformat()}→{m.exit_date.isoformat()}: "
                    f"{m.field}  baseline={m.baseline_value:.6f}  "
                    f"candidate={m.candidate_value:.6f}  Δ={m.delta:+.6f}"
                )
            if len(self.mismatches) > 10:
                out.append(f"    … ({len(self.mismatches) - 10} more)")
        return "\n".join(out)


def _key(t: SearchTrade) -> TradeKey:
    """Unique identifier for one trade."""
    return (t.ticker, t.entry_date, t.exit_date, t.direction)


def compare_trade_lists(
    baseline: list[SearchTrade],
    candidate: list[SearchTrade],
    *,
    tol_pnl_pct: float = DEFAULT_TOL_PNL_PCT,
    tol_price: float = DEFAULT_TOL_PRICE,
) -> EquivalenceReport:
    """Pure-function trade-list comparison. Never raises.

    Returns an :class:`EquivalenceReport` with ``equivalent`` set per the
    docstring on the class. Tolerances are absolute, not relative:
    ``abs(candidate - baseline) <= tol``.
    """
    b_by_key = {_key(t): t for t in baseline}
    c_by_key = {_key(t): t for t in candidate}

    missing = sorted(set(b_by_key) - set(c_by_key))
    extra = sorted(set(c_by_key) - set(b_by_key))

    mismatches: list[TradeMismatch] = []
    for k in sorted(set(b_by_key) & set(c_by_key)):
        b = b_by_key[k]
        c = c_by_key[k]
        ticker, entry_date, exit_date, direction = k
        for field, tol in (
            ("pnl_pct", tol_pnl_pct),
            ("entry_price", tol_price),
            ("exit_price", tol_price),
        ):
            bv = float(getattr(b, field))
            cv = float(getattr(c, field))
            if abs(cv - bv) > tol:
                mismatches.append(
                    TradeMismatch(
                        ticker=ticker, entry_date=entry_date, exit_date=exit_date,
                        direction=direction, field=field,
                        baseline_value=bv, candidate_value=cv, delta=cv - bv,
                    )
                )

    return EquivalenceReport(
        equivalent=(not missing and not extra and not mismatches),
        n_baseline=len(baseline),
        n_candidate=len(candidate),
        missing_in_candidate=missing,
        extra_in_candidate=extra,
        mismatches=mismatches,
        tol_pnl_pct=tol_pnl_pct,
        tol_price=tol_price,
    )


def assert_trade_lists_equal(
    baseline: list[SearchTrade],
    candidate: list[SearchTrade],
    *,
    tol_pnl_pct: float = DEFAULT_TOL_PNL_PCT,
    tol_price: float = DEFAULT_TOL_PRICE,
) -> None:
    """Raise :class:`AssertionError` with the full diff if the two trade
    lists aren't equivalent.

    Designed for use inside regression tests so a failure surfaces the
    exact mismatch (which tickers, which fields, what magnitude) rather
    than a bare 'lists differ' message."""
    report = compare_trade_lists(
        baseline, candidate,
        tol_pnl_pct=tol_pnl_pct, tol_price=tol_price,
    )
    if not report.equivalent:
        raise AssertionError(report.summary())
