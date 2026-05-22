"""Re-probe ``sentinel_macro_stress_gate_v1`` against the post-2026-05-22
multi-signal-count engine surface enrichment.

The autonomous finder emitted this candidate (run_id
``91100f12-0674-41dc-9b2b-09c00cc5e507``):

    "Sentinel should remain in STANDBY (no defensive rotation) but arm a
    multi-signal macro-stress trigger: when >=3 of {yield_curve<0,
    hy_spread>400bp, vix>22, sahm_rule>0.3} fire concurrently, rotate to
    the defensive ETF basket."

The pre-existing sentinel LAB_TARGET only carried the binary
``bear_score_mode`` choice (``current``/``graduated``) plus the
``activation_score_threshold`` sibling toggle — both of which encode
score-driven activation paths, NOT a multi-signal count-trigger. The
post-2026-05-22 surface enrichment landed by this PR:

* Adds a third arm to ``bear_score_mode``: ``macro_stress_count``.
* Adds ``macro_stress_signal_count`` (``choice:2,3,4``) — how many of
  the four stress signals must fire concurrently to arm the basket.
* Adds four per-signal float thresholds
  (``vix_stress_threshold``, ``hy_spread_stress_threshold_bps``,
  ``sahm_stress_threshold``, ``yield_curve_inversion_threshold``)
  sampled INDEPENDENTLY but read only when the count branch fires.

What this script does (mirrors the catalyst/reversion probe precedent):

* Temporarily narrows ``sentinel.backtest.LAB_TARGET.param_ranges`` to
  pin ``bear_score_mode='macro_stress_count'`` (the new arm) and the
  ``activation_score_threshold`` to its legacy value (60). The five
  count-branch knobs stay in their declared ranges so the Lab sweep
  explores the hypothesis space the LLM described.
* Invokes the same ``ops.lab.amain`` path the other engine probes use,
  so SP-A ledger spend + walk-forward + final-holdout + dossier write
  are byte-equivalent to the canonical Lab harness.
* RESTORES ``LAB_TARGET.param_ranges`` in a ``finally:`` block before
  exit so no probe-time pinning leaks into the in-tree LAB_TARGET (the
  signal_mode + catalyst precedent).

Usage (operator runs this AT THEIR DISCRETION after the PR lands; this
script is NEVER auto-invoked from CI or by the implementer — n_trials
ledger discipline):

    .venv/bin/python scripts/probe_sentinel_macro_stress_gate.py

Environment: requires ``$DATABASE_URL`` (or the ``.env`` file with
``DATABASE_URL_IPV4``). The wrapper shell at
``scripts/run_probe_sentinel_macro_stress_gate.sh`` sets these.
"""
from __future__ import annotations

import asyncio
import os
import sys

# ────────────────────────────────────────────────────────────────────────
# Ops-package-shadow guard (engine_readiness / tests-and-ci rule).
#
# Running `python scripts/foo.py` automatically prepends `scripts/` to
# sys.path, which makes `scripts/ops.py` shadow the `ops/` package
# (`ModuleNotFoundError: 'ops' is not a package` on `from ops.lab ...`).
# Remove the `scripts/` shadow + ensure the repo root is path[0] BEFORE
# any `import ops.*` happens (catalyst + reversion probe precedent).
# ────────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR in sys.path:
    sys.path.remove(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


CANDIDATE_NAME = "sentinel_macro_stress_gate_v1"


def _build_argv() -> list[str]:
    """Argv mirroring the canonical sentinel probe shape.

    Same trials / seed / window dates the operator uses for the other
    sentinel probes so the comparison is apples-to-apples — only the
    pinned LAB_TARGET dimension differs.
    """
    return [
        "--candidate", CANDIDATE_NAME,
        "--target-engine", "sentinel",
        "--intent", "fold_existing",
        "--trials", "20",
        "--per-window-trials", "10",
        "--seed", "20260522",
        "--train-start", "2018-01-01",
        "--holdout-end", "2023-12-31",
        "--final-holdout-start", "2024-01-01",
        "--final-holdout-end", "2025-12-31",
        "--train-years", "3",
        "--holdout-years", "1",
        "--notes",
        "Engine surface enrichment re-probe (macro-stress-count gate): "
        "bear_score_mode pinned to 'macro_stress_count' via temporary "
        "LAB_TARGET.param_ranges narrowing; macro_stress_signal_count + "
        "the four per-signal thresholds (vix / hy_spread / sahm / "
        "yield_curve) Lab-sampled in their declared ranges. Origin: "
        "autonomous finder run_id "
        "91100f12-0674-41dc-9b2b-09c00cc5e507.",
    ]


async def _amain() -> int:
    """In-process Lab probe with probe-time LAB_TARGET narrowing."""
    import sentinel.backtest as sentinel_bt
    from ops.lab.__main__ import _amain as ops_lab_amain

    target = sentinel_bt.LAB_TARGET
    original_ranges = dict(target.param_ranges)

    # The narrowed search space:
    # - bear_score_mode is PINNED to 'macro_stress_count' (a single-
    #   value choice satisfies LabTarget's validator: >=1 non-empty
    #   member required).
    # - activation_score_threshold is PINNED to 60 (the live-path
    #   legacy value) so we don't confound the macro-stress hypothesis
    #   with the sibling sentinel_maxdd activation-earlier variant.
    # - macro_stress_signal_count stays in its declared choice:2,3,4
    #   so the Lab samples each candidate-count.
    # - The four per-signal float thresholds stay in their declared
    #   ranges so the Lab explores the hypothesis space.
    narrowed_ranges: dict[str, tuple] = dict(original_ranges)
    narrowed_ranges["bear_score_mode"] = (0, 0, "choice:macro_stress_count")
    narrowed_ranges["activation_score_threshold"] = (60, 60, "choice:60")
    # macro_stress_signal_count + the four float thresholds stay
    # unchanged (the Lab samples them inside the count branch).
    print(
        "\n[probe_sentinel_macro_stress_gate] narrowing "
        "sentinel.backtest.LAB_TARGET.param_ranges:"
    )
    print(f"  before: {original_ranges}")
    print(f"   after: {narrowed_ranges}")

    object.__setattr__(target, "param_ranges", narrowed_ranges)
    try:
        rc = await ops_lab_amain(_build_argv())
    finally:
        # Restore the in-source LAB_TARGET so no probe-time narrowing
        # leaks into the in-tree state.
        object.__setattr__(target, "param_ranges", original_ranges)
        print(
            "[probe_sentinel_macro_stress_gate] restored "
            "sentinel.backtest.LAB_TARGET.param_ranges:"
        )
        print(f"  {dict(target.param_ranges)}")
    return rc


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print(
            "DATABASE_URL not set. Use the wrapper "
            "scripts/run_probe_sentinel_macro_stress_gate.sh which sources "
            ".env and exports DATABASE_URL_IPV4 -> DATABASE_URL.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
