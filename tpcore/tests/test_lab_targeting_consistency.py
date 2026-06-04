"""SP-B clockwork (mirrors SP4 test_leg6_fails_on_roster_drift INTENT,
NOT a byte-shadow — argued spec §1): the Lab target set IS the roster
SoT predicate; CLI choices are generated; a synthetic roster change
propagates to the Lab with ZERO Lab-file edits (non-vacuous red-proof);
canary/lab-sentinel exclusions are pinned; and SP-A's cumulative
deflation still applies identically to a newly-roster-resolved target
(the dependency invariant — SP-B did NOT re-open SP-A's hole).

> Plan correction (controller-adjudicated, T1/T3/T4/T5 precedent):
> three plan-defects in the Task-6 code block were corrected here and
> the plan's Task-6 section was patched to stay truthful:
>   1. The plan's module-level `sys.modules` `ops`/`ops.*` purge is the
>      T5 oracle-drift footgun (a collection-time GLOBAL module eviction
>      that perturbs other ops-shadow tests' isolation — it was already
>      removed from the T5 sibling test in commit b59bf74). It is
>      DELETED here; it is empirically unnecessary (the resolver/parser
>      paths import cleanly with no purge — proven during impl).
>   2. `test_synthetic_roster_drift_propagates_to_lab`'s plan assertion
>      `match="has not.*declared.*LAB_TARGET"` is a hollow/wrong
>      red-proof: a synthetic package-less PAPER engine reaches the
>      resolver's `ModuleNotFoundError` branch (no `phantompaper`
>      package), NOT the undeclared-LAB_TARGET branch, so that regex
>      NEVER matches even with correct code. The faithful, non-vacuous
>      assertion that captures the plan's stated intent ("recognised as
>      a roster Lab target, NOT KeyError/'unknown engine'") against the
>      real resolver: the ValueError is the POST-roster-gate SP-F-path
>      message and is explicitly NOT the `"not Lab-targetable"`
>      roster-GATE rejection — proving the synthetic engine propagated
>      THROUGH the roster gate (it is treated as targetable) rather
>      than being rejected as unknown.
>   3. The new test accesses `ops.lab.run` / `ops.lab.__main__` /
>      `tpcore.engine_profile`-module-private symbols (that IS its
>      purpose — exercising the built parsers + resolver + ledger
>      seam). Task-6 in the plan had no pyproject step (the SLF-baseline
>      plan-defect class). A scoped `[tool.ruff.lint.per-file-ignores]`
>      entry for exactly this file was added, mirroring the existing
>      char/dispatch/CLI-choices precedents — never an inline
>      `# noqa: SLF001` (CLAUDE.md / STYLE_GUIDE).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.xdist_group("ops_shadow")


# ── (1) the accessor IS the roster predicate, not a hand-list ────────────

def test_target_set_equals_roster_predicate():
    from tpcore.engine_profile import (
        _PROFILE,
        LifecycleState,
        lab_targetable_engines,
    )

    expected = {
        n for n, p in _PROFILE.items()
        if p.lifecycle_state in {LifecycleState.LAB, LifecycleState.PAPER,
                                 LifecycleState.LIVE}
        and n not in {"allocator", "lab", "canary"}
    }
    assert set(lab_targetable_engines()) == expected


def test_lab_targetable_set_frozen_anchor():
    """Lab-targetable set is frozen; changes are high-risk and must be explicit.

    Symmetric to the SP4 sibling's
    test_dispatch_order_invariant_is_the_frozen_literal: every other
    assertion in this clockwork RECOMPUTES the predicate (structural
    mirror), so a roster add/remove silently flows through with no
    visible test edit. This ONE pinned literal makes a legitimate engine
    add/remove a high-risk change that MUST be an explicit, reviewed edit
    to this set — tightening the false-red/false-green boundary vs. pure
    recomputation.
    """
    from tpcore.engine_profile import lab_targetable_engines

    # roster-driven Lab-target changes are high-risk; pin it. Catalyst
    # joined 2026-05-20 via the autonomous Lab criteria path.
    assert set(lab_targetable_engines()) == {
        "reversion", "vector", "momentum", "sentinel", "carver", "catalyst"}


# ── (2) CLI choices are GENERATED from the accessor (both sites) ─────────

def test_cli_choices_are_generated_both_sites():
    import ops.lab.__main__ as m
    import ops.lab.run as run
    from tpcore.engine_profile import lab_targetable_engines

    acc = lab_targetable_engines()
    run._parse_args(["--engine", "reversion"])  # accepted
    m._parse_args(["--candidate", "c", "--target-engine", "reversion",
                   "--intent", "promote_new"])  # accepted
    with pytest.raises(SystemExit):
        run._parse_args(["--engine", "sigma"])  # RETIRED dropped automatically
    assert "sentinel" in acc  # eligible-but-undeclared still a CLI choice


# ── (3) RED-PROOF: a synthetic roster mutation propagates with ZERO Lab
#       edits; a RETIRED engine drops automatically (NON-VACUOUS) ─────────

def test_synthetic_roster_drift_propagates_to_lab(monkeypatch):
    """Mirrors test_leg6_fails_on_roster_drift: inject a fake PAPER
    engine into _PROFILE; lab_targetable_engines() + CLI choices +
    _lab_target_for ALL track it with NO Lab-file edit.

    Non-vacuous (corrected — see module docstring §2): the synthetic
    engine is recognised as a roster Lab target that PROPAGATED THROUGH
    the roster gate — the resolver raises a clear POST-gate SP-F-path
    ValueError, NOT the `"not Lab-targetable"` roster-GATE rejection and
    NOT a raw KeyError / 'unknown engine'. (A package-less synthetic
    engine reaches the resolver's import branch, not the
    LAB_TARGET-declaration branch — both are the same clear-ValueError
    class; the discriminating, non-vacuous property is that it got PAST
    the roster gate, exactly what the SP-B roster propagation must do.)
    """
    import ops.lab.run as run
    import tpcore.engine_profile as ep

    fake = ep.EngineProfile(
        engine="phantompaper", cadence=ep.Cadence.DAILY,
        dispatch_order=7, lifecycle_state=ep.LifecycleState.PAPER)
    patched = dict(ep._PROFILE)
    patched["phantompaper"] = fake
    monkeypatch.setattr(ep, "_PROFILE", patched)

    assert "phantompaper" in ep.lab_targetable_engines()

    # CLI choices see it (generated from the accessor, not a literal).
    run._parse_args(["--engine", "phantompaper"])  # argparse accepts it

    # The resolver recognises it as a roster Lab target (it propagated
    # THROUGH the roster gate) — a clear POST-gate SP-F-path ValueError,
    # explicitly NOT the `"not Lab-targetable"` roster-GATE rejection
    # (that would mean the synthetic engine never propagated) and NOT a
    # raw KeyError / 'unknown engine'.
    with pytest.raises(ValueError) as ei:
        run._lab_target_for("phantompaper")
    msg = str(ei.value)
    assert "not Lab-targetable" not in msg, (
        "the synthetic PAPER engine must propagate THROUGH the roster "
        "gate, not be rejected as non-targetable — the SP-B propagation "
        f"red-proof would be vacuous otherwise; got: {msg!r}")
    assert "phantompaper" in msg  # resolved via the roster, by name

    # Conversely: flipping a real engine to RETIRED drops it automatically.
    retired = dict(ep._PROFILE)
    retired["momentum"] = ep.EngineProfile(
        engine="momentum", cadence=ep.Cadence.MONTHLY_FIRST_TRADING_DAY,
        dispatch_order=3, lifecycle_state=ep.LifecycleState.RETIRED)
    monkeypatch.setattr(ep, "_PROFILE", retired)
    assert "momentum" not in ep.lab_targetable_engines()


# ── (4) policy-clause regression pins ────────────────────────────────────

def test_canary_not_lab_targetable():
    from tpcore.engine_profile import lab_targetable_engines
    assert "canary" not in lab_targetable_engines()


def test_lab_sentinel_not_lab_targetable():
    from tpcore.engine_profile import lab_targetable_engines
    assert "lab" not in lab_targetable_engines()


# ── (5) SP-A NON-REGRESSION: cumulative deflation still applies to a
#       newly-roster-resolved target; hard-reject happens BEFORE any
#       ledger spend (the dependency invariant, spec §4.5 / §4.10) ───────

@dataclass
class _Trade:
    entry_date: date
    pnl_pct: float


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetchrow(self, sql, *params):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO platform.data_quality_log"):
            # Plan 2 bind order: (kind, source, timestamp, latency_ms,
            # missing_bars, stale, confidence, notes); uuid PK ⇒ plain append.
            source, ts = params[1], params[2]
            self._rows.append({"source": source, "timestamp": ts,
                               "notes": params[7]})
            return {"?column?": 1}
        raise AssertionError(s)

    async def fetchval(self, sql, *params):
        s = " ".join(sql.split())
        if "SUM((notes::jsonb->>'trials')::int)" in s:
            src, before = params[0], params[1]
            import json as _j
            return sum(_j.loads(r["notes"])["trials"] for r in self._rows
                       if r["source"] == src and r["timestamp"] < before)
        raise AssertionError(s)


class _Acquire:
    def __init__(self, conn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return False


class _SharedPool:
    def __init__(self):
        self.rows = []

    def acquire(self):
        return _Acquire(_FakeConn(self.rows))

    async def close(self):
        ...


def _ns(output, *, engine, trials, seed):
    return argparse.Namespace(
        engine=engine, trials=trials, per_window_trials=4,
        train_start=date(2018, 1, 1), holdout_end=date(2021, 12, 31),
        final_holdout_start=date(2022, 1, 1),
        final_holdout_end=date(2022, 12, 31),
        walk_forward_step=365, train_years=3, holdout_years=1,
        seed=seed, output=output, database_url="postgres://fake/db",
        dsr_threshold=0.95, credibility_threshold=60,
        universe_tier_max=None)


def _install_offline(monkeypatch, lab_run, returns):
    from tpcore.backtest.credibility import CredibilityScore

    rubric = CredibilityScore(
        lookahead_clean=True, survivorship_inclusive=True,
        pit_fundamentals=True, regime_coverage=True,
        out_of_sample_validated=True, monte_carlo_drawdown=True, score=80)

    class _RR:
        credibility_score = 80
        credibility_rubric = rubric
        trade_log = [_Trade(date(2022, 1, 3) + timedelta(days=i), r)
                     for i, r in enumerate(returns)]

    monkeypatch.setattr("ops.lab.run._context_runner_for",
                        lambda e: (lambda c, *, overrides=None: _RR()))

    async def _aloader(**k):
        return _RR()

    monkeypatch.setattr("ops.lab.run._context_loader_for",
                        lambda e: _aloader)

    async def _runner(**k):
        return _RR()

    monkeypatch.setattr("ops.lab.run._runner_for", lambda e: _runner)

    async def _fw(pool, *, engine_name, score):
        return True

    monkeypatch.setattr(
        "tpcore.backtest.statistical_validation.write_credibility_score",
        _fw, raising=True)


async def test_sp_a_cumulative_applies_to_roster_resolved_target(
        monkeypatch, tmp_path):
    """A newly-roster-resolved target's SP-A cumulative deflation grows
    monotonically across runs — SP-B did NOT re-open SP-A's hole. Uses a
    real declared target (reversion) routed through the SP-B resolver
    (the resolver is between the CLI and the ledger; reversion is the
    'newly-roster-resolved' path post-SP-B). SP-B touches ZERO ledger
    code (spec §4.5)."""
    import numpy as np

    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    returns = [float(x) for x in np.random.default_rng(0).normal(
        0.015, 0.01, 40)]
    seen = []
    real = lab_run.compute_dsr_for_verdict

    def _spy(r, *, n_trials, trial_sharpe_variance=None):
        seen.append(n_trials)
        return real(r, n_trials=n_trials,
                    trial_sharpe_variance=trial_sharpe_variance)

    monkeypatch.setattr(lab_run, "compute_dsr_for_verdict", _spy)
    _install_offline(monkeypatch, lab_run, returns)
    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        c1 = await lab_run._run_lab_core(
            _ns(tmp_path / "a.csv", engine="reversion", trials=40, seed=1),
            candidate="rev_a")
    async with LabContext(db_url="postgres://fake/db"):
        c2 = await lab_run._run_lab_core(
            _ns(tmp_path / "b.csv", engine="reversion", trials=50, seed=2),
            candidate="rev_b")

    assert not isinstance(c1, int) and not isinstance(c2, int)
    assert seen == [40, 90]                 # cumulative: 0+40, then 40+50
    assert c2.effective_n_trials == 90 > c1.effective_n_trials == 40


async def test_undeclared_target_hard_rejects_before_any_ledger_spend(
        monkeypatch, tmp_path):
    """Edge §4.2/§4.10: a programmatic run with a non-targetable engine
    raises the clear ValueError inside sample_parameters → _lab_target_for
    BEFORE record_trial_spend (run.py:752-759). NO lab_trial_ledger.canary
    row is ever written."""
    import ops.lab.run as lab_run
    from tpcore.lab.context import LabContext

    shared = _SharedPool()

    async def _fb(url, *, read_only, **k):
        return shared

    monkeypatch.setattr("tpcore.db.build_asyncpg_pool", _fb, raising=True)

    async with LabContext(db_url="postgres://fake/db"):
        with pytest.raises(ValueError, match="not Lab-targetable"):
            await lab_run._run_lab_core(
                _ns(tmp_path / "x.csv", engine="canary", trials=10, seed=0),
                candidate="bad")

    # No spend row for the rejected target — the ledger stays clean.
    assert not any(
        r["source"] == "lab_trial_ledger.canary" for r in shared.rows), (
        "a hard-rejected target must NOT spend SP-A ledger budget")
