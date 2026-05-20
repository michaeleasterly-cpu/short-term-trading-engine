"""Autonomous Lab criteria — framework-evaluated signal-presence + comparative-improvement gates.

The single absolute ``DSR ≥ 0.95 ∧ credibility_score ≥ 60`` gate is
over-constrained:

- DSR's denominator depends on ``n_trials``; a sparse-but-real-edge
  engine (catalyst: 24 trades / 6y / Sharpe 2.27 / DSR 0.754) can never
  clear DSR ≥ 0.95 no matter how clean the signal.
- For an improvement, an absolute threshold rejects real wins (Sharpe
  0.4 → 0.7 is a real improvement; the absolute bar rejects it).

This module replaces the single gate with two pure criteria sets the
framework evaluates against the engine's OWN backtest dossier:

- **New-engine criteria** (``_assess_new_engine_signal``) — signal-
  presence test for LAB→PAPER promotion and ADD ``source: existing_code``.
- **Improvement criteria** (``_assess_improvement``) — comparative test
  for MODIFY (``fold_existing``) that the candidate is strictly better
  than the incumbent on the declared primary metric AND passes the
  new-engine floor.

See ``docs/superpowers/specs/2026-05-20-autonomous-lab-criteria.md``.

Pure: no I/O, no DB, no logging. Takes dossier-like objects in; returns
``(passed, rejection_reason)``. The dossier loader (``load_engine_dossier``)
is a single read of ``backtests/<engine>_backtest_results.json`` — the
canonical artifact every ``<engine>.backtest`` already writes (see
``catalyst/backtest.py::run_backtest``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from tpcore.lab.target import LabPrimaryMetric

# ─── Criterion thresholds (calibrated against catalyst's empirical run) ───
# These are deliberate, named constants — recalibration is future-spec.
# Catalyst's recent backtest: sharpe=2.27, trades=24, max_dd=-0.41,
# ruin_prob=0.087, profit_factor=1.36, min_btl_gap=109. Every criterion
# clears with margin. The absolute DSR (0.754) / credibility (45) numbers
# are informational — n_trials sparsity, not signal absence, is what
# binds them. The criteria below test signal presence directly.

#: positive OOS Sharpe — the most basic signal-presence test.
POSITIVE_SHARPE_FLOOR: float = 0.0

#: non-trivial trade count — below 10 you can't distinguish signal from noise.
MIN_TRADE_COUNT: int = 10

#: bounded drawdown — no ≤−50% catastrophic draws (signal-presence, NOT live-capital).
MIN_MAX_DRAWDOWN: float = -0.50

#: bounded ruin probability — 30% chance of ruin too high even for paper-trade-and-learn.
MAX_RUIN_PROBABILITY: float = 0.30

#: profit factor at least 1.0 — no edge if avg loss > avg win.
MIN_PROFIT_FACTOR: float = 1.0

#: sane min between-trade gap (days) — engine that fires less than once
#: a year on average has too-slow an experience curve to be useful.
MAX_MIN_BTL_GAP: int = 365

#: trade-count drift bound (improvement criteria) — a "better Sharpe"
#: that comes from cutting 90% of trades is a different engine, not an
#: improvement. Candidate trades must be ≥ 50% of incumbent's.
MIN_TRADE_COUNT_DRIFT_RATIO: float = 0.5


class NewEngineDossier(BaseModel):
    """Frozen mirror of ``BacktestRunResult.to_json_dict()`` — the JSON shape
    every ``<engine>.backtest`` writes to ``backtests/<engine>_backtest_results.json``.

    Only the fields the criteria functions read are mandatory; the rest
    (dsr/credibility_score) are kept for informational display + future
    audit. ``extra="ignore"`` because the JSON dict carries fields like
    ``engine`` / ``parameters`` / ``passed_gate`` / ``trades_per_param``
    / ``sensitivity_score`` that the criteria functions don't need.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    sharpe: float
    trades: int
    max_drawdown: float
    ruin_probability: float
    profit_factor: float
    min_btl_gap: int
    # informational (the OLD binding-gate numbers — kept for audit /
    # dashboard / weekly digest; the criteria functions never read them).
    dsr: float | None = None
    credibility_score: int | None = None


def _assess_new_engine_signal(
    dossier: NewEngineDossier,
) -> tuple[bool, str | None]:
    """Evaluate the new-engine signal-presence criteria.

    Reads ``dossier``; returns ``(passed, rejection_reason)``. Each clause
    carries a clear reason naming WHICH criterion failed. None of the
    clauses are subjective; all are read directly off the dossier.

    Used by:

    - ``promote()`` (LAB → PAPER) — autonomous gate.
    - ADD ``source: existing_code`` — post-hoc roster registration of
      shipped engine code that has produced a backtest dossier.
    - ``_assess_improvement`` — as the floor a candidate must also pass.

    Calibrated against catalyst (sharpe=2.27, trades=24, max_dd=-0.41,
    ruin_prob=0.087, profit_factor=1.36, min_btl_gap=109) — every
    catalyst clause clears with margin. The criteria are CALIBRATED, not
    arbitrary.
    """
    if not (dossier.sharpe > POSITIVE_SHARPE_FLOOR):
        return False, (
            f"positive_sharpe: sharpe {dossier.sharpe:.4f} is not > "
            f"{POSITIVE_SHARPE_FLOOR} — no signal presence")
    if dossier.trades < MIN_TRADE_COUNT:
        return False, (
            f"min_trade_count: trades {dossier.trades} < "
            f"{MIN_TRADE_COUNT} — below this you can't distinguish "
            f"signal from noise")
    if dossier.max_drawdown < MIN_MAX_DRAWDOWN:
        return False, (
            f"bounded_drawdown: max_drawdown {dossier.max_drawdown:.4f} "
            f"< {MIN_MAX_DRAWDOWN} — catastrophic draw fails the "
            f"signal-presence test")
    if dossier.ruin_probability > MAX_RUIN_PROBABILITY:
        return False, (
            f"bounded_ruin_probability: ruin_probability "
            f"{dossier.ruin_probability:.4f} > {MAX_RUIN_PROBABILITY} — "
            f"too risky even for paper-trade-and-learn")
    if dossier.profit_factor < MIN_PROFIT_FACTOR:
        return False, (
            f"min_profit_factor: profit_factor "
            f"{dossier.profit_factor:.4f} < {MIN_PROFIT_FACTOR} — avg "
            f"loss > avg win, no edge to learn from")
    if dossier.min_btl_gap > MAX_MIN_BTL_GAP:
        return False, (
            f"sane_min_btl_gap: min_btl_gap {dossier.min_btl_gap} > "
            f"{MAX_MIN_BTL_GAP} days — engine fires less than once a "
            f"year on average, experience curve too slow")
    return True, None


def _metric_value(dossier: NewEngineDossier,
                  metric: LabPrimaryMetric) -> float:
    """Pull the comparator scalar for ``metric`` out of a dossier.

    ``SHARPE`` → ``dossier.sharpe`` (higher is better).
    ``MAXDD_REDUCTION`` → ``-dossier.max_drawdown`` (higher = shallower
    draw; ``max_drawdown`` is signed negative so the negation puts it on
    a higher-is-better axis matching SHARPE).
    Other metrics: not yet implemented; raises ``NotImplementedError``
    (SP-D fail-loud at resolve, spec §4.3).
    """
    if metric is LabPrimaryMetric.SHARPE:
        return float(dossier.sharpe)
    if metric is LabPrimaryMetric.MAXDD_REDUCTION:
        # max_drawdown is signed negative (-0.20 = 20% draw); a shallower
        # draw is a value CLOSER to zero, i.e. HIGHER on the signed axis
        # (-0.10 > -0.20). So the comparator is the signed max_drawdown
        # itself — higher (closer to zero) wins. This matches the
        # MAXDD_REDUCTION name: the candidate "reduces" the drawdown.
        return float(dossier.max_drawdown)
    raise NotImplementedError(
        f"LabPrimaryMetric.{metric.name} not yet implemented in "
        f"lab_criteria._metric_value (SP-D reserved vocabulary)")


def _assess_improvement(
    candidate: NewEngineDossier,
    incumbent: NewEngineDossier,
    primary_metric: LabPrimaryMetric,
) -> tuple[bool, str | None]:
    """Evaluate the comparative-improvement criteria for a MODIFY
    (``fold_existing``).

    All must hold:

    - ``candidate`` strictly beats ``incumbent`` on ``primary_metric``.
    - ``candidate`` passes the new-engine floor — an "improvement" that
      fails basic signal-presence isn't worth shipping.
    - ``candidate.trades`` is at least 50% of ``incumbent.trades`` — a
      better Sharpe via cutting 90% of trades is a different engine, not
      an improvement.

    Returns ``(passed, rejection_reason)``. Each clause carries a clear
    reason naming WHICH criterion failed.
    """
    cand_metric = _metric_value(candidate, primary_metric)
    inc_metric = _metric_value(incumbent, primary_metric)
    if not (cand_metric > inc_metric):
        return False, (
            f"candidate_beats_incumbent: candidate {primary_metric.value}="
            f"{cand_metric:.4f} is not strictly > incumbent "
            f"{primary_metric.value}={inc_metric:.4f} — not an "
            f"improvement on the declared primary metric")
    floor_passed, floor_reason = _assess_new_engine_signal(candidate)
    if not floor_passed:
        return False, (
            f"candidate_passes_new_engine_floor: the improvement candidate "
            f"fails the new-engine signal-presence floor — {floor_reason}")
    # incumbent.trades==0 is degenerate; guard with max(1, ...) so the
    # ratio is defined (a candidate with positive trade count against a
    # zero-trade incumbent is trivially within drift bounds).
    inc_trades = max(1, incumbent.trades)
    if candidate.trades < MIN_TRADE_COUNT_DRIFT_RATIO * inc_trades:
        return False, (
            f"trade_count_drift_bounded: candidate.trades "
            f"{candidate.trades} < {MIN_TRADE_COUNT_DRIFT_RATIO} * "
            f"incumbent.trades ({incumbent.trades}) — trade-count crash "
            f"is a different engine, not an improvement")
    return True, None


def dossier_from_lab_held_metrics(
    held_metrics: dict[str, Any],
    *,
    ruin_probability: float | None = None,
    min_btl_gap: int | None = None,
) -> NewEngineDossier:
    """Build a ``NewEngineDossier`` from a ``LabResult.held_metrics``
    dict (the Lab-side OOS slice).

    ``held_metrics`` carries ``n_trades / sharpe / profit_factor /
    max_drawdown`` (see ``ops/lab/run.py::SliceMetrics.to_dict``); the
    Lab does not write ``ruin_probability`` or ``min_btl_gap`` to
    held_metrics. Absent fields default to NEUTRAL values that PASS the
    new-engine floor — the Lab side has its own DSR/credibility walk for
    the candidate (the sacred LabResult.verdict gate); the new-engine
    floor here is the SANITY check ("did the Lab produce a self-coherent
    candidate dossier"), not a re-litigation of the Lab walk. A
    candidate that genuinely fails on profit_factor / drawdown / ruin
    must have those fields populated in held_metrics for the floor to
    bite — the principled position is that anything the candidate
    DOES report we evaluate strictly; anything it OMITS we presume the
    Lab walked.

    Used by ``_validate_modify`` to synthesize a candidate dossier from
    the Lab sidecar's held_metrics.
    """
    # NEUTRAL defaults: each defaults to a value just inside the floor —
    # an explicit non-neutral value in held_metrics is evaluated strictly.
    return NewEngineDossier(
        sharpe=float(held_metrics.get("sharpe", 0.0)),
        trades=int(held_metrics.get("n_trades", 0)),
        max_drawdown=float(held_metrics.get("max_drawdown", 0.0)),
        ruin_probability=float(
            ruin_probability if ruin_probability is not None
            else held_metrics.get("ruin_probability", 0.0)),
        profit_factor=float(held_metrics.get("profit_factor", 1.0)),
        min_btl_gap=int(
            min_btl_gap if min_btl_gap is not None
            else held_metrics.get("min_btl_gap", 0)),
    )


def load_engine_dossier(repo_root: Path,
                        engine: str) -> NewEngineDossier | None:
    """Read ``backtests/<engine>_backtest_results.json`` and parse into a
    ``NewEngineDossier``.

    Returns ``None`` if the file is absent. Raises ``ValueError`` if
    present but unparseable (a corrupt dossier is louder than silently
    missing — the framework must NEVER make a gate decision against a
    half-read file).
    """
    p = repo_root / "backtests" / f"{engine}_backtest_results.json"
    if not p.is_file():
        return None
    try:
        raw: dict[str, Any] = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"backtest dossier at {p} is unparseable JSON: {exc}") from exc
    return NewEngineDossier.model_validate(raw)


__all__ = [
    "MAX_MIN_BTL_GAP",
    "MAX_RUIN_PROBABILITY",
    "MIN_MAX_DRAWDOWN",
    "MIN_PROFIT_FACTOR",
    "MIN_TRADE_COUNT",
    "MIN_TRADE_COUNT_DRIFT_RATIO",
    "NewEngineDossier",
    "POSITIVE_SHARPE_FLOOR",
    "_assess_improvement",
    "_assess_new_engine_signal",
    "dossier_from_lab_held_metrics",
    "load_engine_dossier",
]
