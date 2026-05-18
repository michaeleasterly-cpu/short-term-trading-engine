"""Thin compatibility shim — the walk-forward Lab engine now lives in
ops.lab.run (SDLC SP2 T5, H-S2-1). This module preserves the historical
`python scripts/search_parameters.py` CLI + every public + underscore
symbol the characterization oracle pins; all logic delegates to
ops.lab.run.

`ops.lab.run` hosts the engine-importing orchestration because `ops/` is
exempt from the `tpcore.scripts.check_imports` tpcore∌engine AST scan
(the same exemption this script historically relied on); `tpcore/lab/`
could NOT host it (CI runs `check_imports … tpcore`).
"""
from __future__ import annotations

import sys
from pathlib import Path

# When this shim is reached with `scripts/` ahead of the repo root on
# sys.path (either `python scripts/search_parameters.py` — scripts/ is
# sys.path[0] — or the oracle, which does
# `sys.path.insert(0, REPO_ROOT/"scripts")`), a bare `import ops` resolves
# to the sibling `scripts/ops.py` module, not the `ops/` package, so
# `ops.lab` does not exist. Ensure REPO_ROOT precedes every `scripts/`
# entry so the `ops/` package shadows `scripts/ops.py` — the same
# package-shadowing intent as the `scripts/ops.py` bootstrap.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
_insert_at = sys.path.index(_SCRIPTS_DIR) if _SCRIPTS_DIR in sys.path else 0
sys.path.insert(_insert_at, _REPO_ROOT)

from ops.lab.run import (  # noqa: E402,F401
    PARAM_RANGES,
    SliceMetrics,
    TrialResult,
    WalkWindow,
    _context_loader_for,
    _context_runner_for,
    _evaluate_candidate_with_context,
    _norm_inv,
    _parse_args,
    _runner_for,
    _score_for_ranking,
    amain,
    build_walk_windows,
    compute_dsr_for_verdict,
    compute_slice_metrics_from_trades,
    main,
    period_returns_from_trades,
    rank_candidates,
    sample_parameters,
    write_results_csv,
)

if __name__ == "__main__":  # pragma: no cover
    main()
