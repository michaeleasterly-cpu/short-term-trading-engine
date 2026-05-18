"""Engine-lane fence — a THIN wrapper that consumes the SHIPPED pure
data-lane fence (`tpcore.llm_data_triage.fence`) via injected
parameters (FORK-A RESOLVED, spec §3): one fence implementation is a
SAFETY asset (two could silently diverge — one hard-denied list to
audit), so the engine lane consumes the SAME code object, not a twin.

This module only supplies the engine-lane DATA:
- the engine hard-denied path set (the engine deterministic-mechanism
  files + the shared protected paths the data lane already denies); and
- a thin engine-flavoured provenance entrypoint.

It adds NO new fence logic. The byte-no-op for the live data lane is
proven by the UNCHANGED #187 fence suite still passing.
"""
from __future__ import annotations

from collections.abc import Mapping

from tpcore.llm_data_triage.fence import (
    DENIED_EXACT,
    DENIED_GLOBS,
    DENIED_PREFIXES,
    hard_denied_paths,
    provenance_violations,
)

# ── Engine hard-denied set (DATA, injected — never hardcoded into the
# shared fence function). = the engine deterministic-mechanism files
# (the agent may add an additive POLICY binding but NEVER edit the
# ladder/supervisor/autotune MECHANISM) UNION the shared protected
# paths the data lane already denies. Spec §3.
_ENGINE_MECHANISM_EXACT: tuple[str, ...] = (
    "ops/engine_supervisor.py",  # DA-1 mechanism
    "ops/aar_autotune.py",       # DA-2 mechanism
    "tpcore/supervisor_state.py",  # supervisor read/vocabulary
    "ops/engine_ladder.py",      # Ladder mechanism (policy bind is additive,
                                 # but the ladder CODE is hard-denied)
)
ENGINE_DENIED_EXACT: tuple[str, ...] = DENIED_EXACT + _ENGINE_MECHANISM_EXACT
ENGINE_DENIED_PREFIXES: tuple[str, ...] = DENIED_PREFIXES
ENGINE_DENIED_GLOBS: tuple[str, ...] = DENIED_GLOBS


def engine_hard_denied_paths(paths: list[str]) -> list[str]:
    """Engine-lane hard-denied check: the SHARED fence with the engine
    denied set injected as data. No new logic."""
    return hard_denied_paths(
        paths,
        denied_exact=ENGINE_DENIED_EXACT,
        denied_prefixes=ENGINE_DENIED_PREFIXES,
        denied_globs=ENGINE_DENIED_GLOBS,
    )


def engine_provenance_violations(
    baseline: Mapping[str, Mapping],
    head: Mapping[str, Mapping],
    *,
    baseline_stages: set[str],
) -> list[str]:
    """Engine-lane provenance: the SHARED evaluator, no fork.

    The engine lane has NO HealSpec/RemediationSpec set (confirmed by
    reading — spec §3/§11); its sole declarative SoT is
    `ops.engine_ladder.DISPOSITION_POLICIES`. The engine provenance
    check proves exactly one property: the proposed additive
    DISPOSITION_POLICIES entry binds the novel pattern to an
    ALREADY-EXISTING EngineEscalationDisposition value
    (`converted`/`structural`/`removed`) — it can NEVER introduce a new
    disposition member, a new escalation-class semantic, or edit an
    existing policy (all human-only, hard-denied). The shared
    `provenance_violations` already enforces exactly this generically
    (additive-only, no edit/remove, mechanism must already exist), with
    the disposition verb in the spec-dict `stage` slot and
    `baseline_stages` = the set of existing EngineEscalationDisposition
    values. One evaluator, zero clone.
    """
    return provenance_violations(baseline, head, baseline_stages)


__all__ = [
    "ENGINE_DENIED_EXACT",
    "ENGINE_DENIED_GLOBS",
    "ENGINE_DENIED_PREFIXES",
    "engine_hard_denied_paths",
    "engine_provenance_violations",
]
