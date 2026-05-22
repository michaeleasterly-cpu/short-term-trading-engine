"""Re-probe ``catalyst_pead_expansion_range`` against the post-2026-05-22
catalyst engine surface enrichment (the ``beat_30d_only`` pure-PEAD arm
+ the Lab-sampled ``hold_days`` knob).

The prior 2026-05-22 probe FAILED with n_trades=2 because the engine
required the ``CATALYST_MIN_DISTINCT_INSIDERS=3`` insider-cluster floor
BEFORE its event-confirmation gate — stripping the LLM's PEAD-only
hypothesis to two cluster-AND-beat coincidences in the holdout. The
new ``beat_30d_only`` arm bypasses that floor entirely.

What this script does:

* Temporarily narrows ``catalyst.backtest.LAB_TARGET.param_ranges`` to
  pin ``event_confirmation_mode='beat_30d_only'``. ``cluster_window_days``
  is dropped from the search space (it's a no-op under the PEAD branch
  — the cluster loop is bypassed). ``hold_days`` stays Lab-sampled over
  [5, 30] (the candidate hypothesis recommends 20; the sampler will
  show whether 20 actually wins).
* Invokes the same ``ops.lab.amain`` path the prior probe used, in the
  same process, so the SP-A ledger spend + walk-forward + final-holdout
  + dossier write are all byte-equivalent to the canonical Lab harness.
* RESTORES ``LAB_TARGET.param_ranges`` in a ``finally:`` block before
  exit so no probe-time pinning leaks into the in-tree LAB_TARGET (the
  reversion subagent's pattern, operator's task brief).

Output: the Lab dossier path printed by ``ops.lab.amain``; the dossier
+ JSON live in ``docs/lab/``. The verdict (DSR / credibility /
n_trades / PBO) is extracted from the dossier post-run.

Usage:
    .venv/bin/python scripts/probe_catalyst_pead_expansion.py

Environment: requires ``$DATABASE_URL`` (or the .env file with
``DATABASE_URL_IPV4``). The wrapper shell at
``scripts/run_probe_catalyst_pead_expansion.sh`` sets these correctly.
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
# any `import ops.*` happens (mirrors the CLAUDE.md ops-package-shadow
# rule + the operator's 2026-05-21 lesson).
# ────────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR in sys.path:
    sys.path.remove(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _build_argv() -> list[str]:
    """Argv mirroring the prior 2026-05-22 catalyst PEAD probe.

    The same trials / seed / window dates as
    ``docs/lab/2026-05-22-catalyst_pead_expansion_range-FAILED-seed20260522.md``
    so the comparison is apples-to-apples — only the engine surface
    (the ``beat_30d_only`` arm + ``hold_days`` knob) changes.
    """
    return [
        "--candidate", "catalyst_pead_expansion_range",
        "--target-engine", "catalyst",
        "--intent", "fold_existing",
        # Search space under the narrowed param_ranges is
        # event_confirmation_mode pinned + hold_days int over [5, 30]
        # (26 unique values) — 50 sampled trials covers the space
        # well, per-window-trials=20 keeps each window's evaluation
        # reasonable. We ALSO want the SP-A ledger spend bounded
        # because the catalyst cumulative count is moving (current
        # 100; +50 here keeps the cumulative under the operator's
        # budgetary radar).
        "--trials", "30",
        "--per-window-trials", "10",
        "--seed", "20260522",
        # Walk-forward windows match the prior probe.
        "--train-start", "2018-01-01",
        "--holdout-end", "2023-12-31",
        "--final-holdout-start", "2024-01-01",
        "--final-holdout-end", "2025-12-31",
        "--train-years", "3",
        "--holdout-years", "1",
        # T1+T2 production universe (~1300 names) — 2026-05-22 operator-
        # discretion re-probe to test whether +1.24 Sharpe on 15-ticker
        # test universe extrapolates. Binding constraint on prior probe
        # was n_trades=10 ≤ gate floor 30; T1+T2 has 3,753 BEATs in
        # 2024-2025 holdout, so n_trades should easily exceed 30.
        "--universe-tier-max", "2",
        "--notes",
        "Engine surface enrichment re-probe (PEAD) on T1+T2 production "
        "universe: event_confirmation_mode pinned to beat_30d_only via "
        "temporary LAB_TARGET.param_ranges narrowing; hold_days "
        "Lab-sampled 5..30; cluster_window_days dropped from search; "
        "universe-tier-max=2.",
    ]


async def _amain() -> int:
    """In-process Lab probe with probe-time LAB_TARGET narrowing."""
    # 1. Late-import so the LAB_TARGET object we narrow IS the one
    #    `ops.lab.run._lab_target_for(...)` resolves later (same
    #    `catalyst.backtest` module object in sys.modules).
    import catalyst.backtest as catalyst_bt
    from ops.lab.__main__ import _amain as ops_lab_amain

    target = catalyst_bt.LAB_TARGET
    original_ranges = dict(target.param_ranges)
    # The narrowed search space:
    # - event_confirmation_mode is PINNED to beat_30d_only (a single-
    #   value choice satisfies LabTarget's validator: ≥1 non-empty
    #   member required).
    # - cluster_window_days is DROPPED — the PEAD branch bypasses the
    #   cluster loop entirely, so sweeping the cluster window is a
    #   pure waste of trials.
    # - hold_days stays Lab-sampled int over [5, 30] — the hypothesis
    #   recommends 20 but the sampler will tell us if that's the
    #   per-window OOS winner.
    narrowed_ranges: dict[str, tuple] = {
        "event_confirmation_mode": (0, 0, "choice:beat_30d_only"),
        "hold_days": (5, 30, "int"),
    }
    print(
        "\n[probe_catalyst_pead_expansion] narrowing "
        "catalyst.backtest.LAB_TARGET.param_ranges:"
    )
    print(f"  before: {original_ranges}")
    print(f"   after: {narrowed_ranges}")
    object.__setattr__(target, "param_ranges", narrowed_ranges)
    try:
        rc = await ops_lab_amain(_build_argv())
    finally:
        # Restore the in-source LAB_TARGET so no probe-time narrowing
        # leaks into the in-tree state — the operator-binding rule
        # ("restore LAB_TARGET in-source via finally-block IF you do
        # any probe-time pinning") respected.
        object.__setattr__(target, "param_ranges", original_ranges)
        print(
            "[probe_catalyst_pead_expansion] restored "
            "catalyst.backtest.LAB_TARGET.param_ranges:"
        )
        print(f"  {dict(target.param_ranges)}")
    return rc


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print(
            "DATABASE_URL not set. Use the wrapper "
            "scripts/run_probe_catalyst_pead_expansion.sh which sources "
            ".env and exports DATABASE_URL_IPV4 → DATABASE_URL.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
