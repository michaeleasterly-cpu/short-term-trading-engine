"""SP-G — Lab front-half thin advisory LLM spec-emitter (engine-FREE
contract layer).

This package is the engine-free contract layer for the SP-G LLM spec-
emitter: pydantic-v2 frozen models that describe what the LLM is allowed
to read (``EmissionContext``) and emit (``EmittedSpec``), the pure-Python
emission helpers (``emitter``), the pre-emission ledger-budget gate
(``ledger_gate``), and the build-time diff-scope allow-list fence
(``diff_fence``).

Engine-FREE: imports only stdlib + pydantic (+ ``tpcore.lab.target`` for
``LabPrimaryMetric``; ``tpcore.lab.ledger`` for the budget read at the
edge, never an engine import). The Anthropic-SDK-calling agent lives in
``ops/llm_lab_emitter.py``; this package never invokes the LLM and never
opens a PR. The same engine-free layering established by
``tpcore/lab/ledger.py`` (SP-A) + ``tpcore/lab/target.py`` (SP-B).

Hard constraints encoded here (all from
``docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-design.md``
§2):

- §2.1 the SP-A ledger row is written BEFORE the draft PR — the agent
  in ``ops/llm_lab_emitter.py`` calls ``record_trial_spend`` strictly
  before ``gh pr create``; this layer defines the contract and the
  pre-emission ``check_budget`` gate.
- §2.2 single pre-registered primary hypothesis per emission — enforced
  by ``EmittedSpec``'s pydantic validators.
- §2.3 the gate is sacred — enforced by the diff-scope allow-list
  (``diff_fence``) which fails the build if any forbidden path appears
  in the emitted PR's diff.
- §2.4 advisory + draft-PR-only — enforced by the agent (and pinned by
  the safety tests).
- §2.6 roster-mediated, never roster-mutating — the diff fence forbids
  ``tpcore/engine_profile.py`` / ``tpcore/providers.py`` edits; this
  is defence-in-depth on top of the ``.claude/hooks/`` ECR/DFCR gate.
"""
from __future__ import annotations

from tpcore.lab.llm_emitter.diff_fence import (
    FORBIDDEN_PATH_PREFIXES,
    DiffScopeViolation,
    enforce_diff_scope,
)
from tpcore.lab.llm_emitter.emitter import (
    GATE_OVERRIDE_FORBIDDEN_FLAGS,
    GateOverrideRejected,
    render_candidate_spec,
    validate_no_gate_override,
)
from tpcore.lab.llm_emitter.ledger_gate import (
    EMISSION_QUOTA_PER_TARGET,
    LedgerBudgetExhausted,
    check_budget,
)
from tpcore.lab.llm_emitter.models import (
    EmissionContext,
    EmittedSpec,
    LedgerEntry,
    ReferenceExcerpt,
    RosterTarget,
)

__all__ = [
    "EMISSION_QUOTA_PER_TARGET",
    "FORBIDDEN_PATH_PREFIXES",
    "GATE_OVERRIDE_FORBIDDEN_FLAGS",
    "DiffScopeViolation",
    "EmissionContext",
    "EmittedSpec",
    "GateOverrideRejected",
    "LedgerBudgetExhausted",
    "LedgerEntry",
    "ReferenceExcerpt",
    "RosterTarget",
    "check_budget",
    "enforce_diff_scope",
    "render_candidate_spec",
    "validate_no_gate_override",
]
