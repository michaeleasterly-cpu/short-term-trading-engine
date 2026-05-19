"""Fail-CLOSED guard for LLM-triage agent PRs (MED security fix).

The two LLM-triage deterministic fence jobs in .github/workflows/ci.yml
are label-gated: their ``if:`` runs ONLY when the ``llm-data-triage`` /
``engine-llm-triage`` label is present, and that label is applied by the
agent's OWN automation. Failure mode = **fail-OPEN**: an agent-produced
draft PR that omits/loses the label triggers NEITHER fence and merges
*unfenced*.

This guard closes that hole. It runs on EVERY pull_request and:

  * Detects whether the PR is **agent-produced** via the evasion-proof
    signals the agent's PR-creation path *cannot* both emit-the-PR and
    suppress (and that a human maintenance PR does NOT carry):

      (1) the head ref starts with ``llm-triage/`` — both agents
          hardcode ``branch = f"llm-triage/{ref_short}"``
          (ops/llm_data_triage.py:204, ops/engine_llm_triage.py:217);
      (2) the diff ADDS a provenance artifact under
          ``docs/llm_triage_proposals/`` or
          ``docs/engine_llm_triage_proposals/`` — always committed by
          the agent (ops/llm_data_triage.py:221,
          ops/engine_llm_triage.py:232), never by a human ops/* edit.

    The two signals are OR-ed: ANY positive agent indicator is enough
    (bias to fail-closed on a positive agent signal).

  * If the PR IS agent-produced but carries NEITHER the
    ``llm-data-triage`` NOR the ``engine-llm-triage`` label ⇒ the job
    FAILS (non-zero) with a clear message. This makes label-omission on
    an agent PR a HARD CI failure.

  * A human maintenance PR (no agent branch prefix, no provenance
    artifact) ⇒ no-op pass. Concrete proof this matters: human PRs
    #97 (modified ops/llm_data_triage.py) and #98 are correctly
    UNlabeled and MUST still pass.

Pure-deterministic, no secrets, no network. Lane-agnostic. Additive —
it does NOT touch the existing fence jobs (the engine fence is co-owned
by another session).

Env contract (set by the CI workflow from the ``pull_request`` event):
    GUARD_HEAD_REF      — head ref / branch name of the PR
    GUARD_CHANGED_PATHS — newline-separated changed-file paths
    GUARD_LABELS        — comma- and/or newline-separated PR label names
"""
from __future__ import annotations

import os
import re
import sys

# The agent branch namespace — hardcoded identically in BOTH agents'
# `_open_draft_pr` (ops/llm_data_triage.py:204, ops/engine_llm_triage.py:217).
_AGENT_BRANCH_PREFIX = "llm-triage/"

# The provenance-artifact directories the agents ALWAYS write into
# (ops/llm_data_triage.py:221 / ops/engine_llm_triage.py:232). A human
# maintenance edit to ops/* never adds files here.
_PROVENANCE_DIRS = (
    "docs/llm_triage_proposals/",
    "docs/engine_llm_triage_proposals/",
)

# Either of these labels means the matching deterministic fence job runs.
_FENCE_LABELS = frozenset({"llm-data-triage", "engine-llm-triage"})


def _is_agent_branch(head_ref: str) -> bool:
    return head_ref.startswith(_AGENT_BRANCH_PREFIX)


def _has_provenance_artifact(changed_paths: list[str]) -> bool:
    return any(
        p.startswith(d) for p in changed_paths for d in _PROVENANCE_DIRS
    )


def evaluate(
    *, head_ref: str, changed_paths: list[str], labels: list[str]
) -> int:
    """Pure predicate. Returns 0 (pass) or 1 (fail-closed).

    Agent-produced (branch prefix OR provenance artifact) but missing
    BOTH fence labels ⇒ 1. Anything else ⇒ 0 (no-op for human PRs).
    """
    agent_branch = _is_agent_branch(head_ref)
    agent_artifact = _has_provenance_artifact(changed_paths)
    is_agent_pr = agent_branch or agent_artifact

    if not is_agent_pr:
        print(
            "agent_pr_label_guard: not an agent-produced triage PR "
            "(no `llm-triage/` branch, no provenance artifact) — pass."
        )
        return 0

    has_fence_label = bool(_FENCE_LABELS.intersection(labels))
    if has_fence_label:
        print(
            "agent_pr_label_guard: agent-produced triage PR carries a "
            "fence label — pass (the deterministic fence job runs)."
        )
        return 0

    signals = []
    if agent_branch:
        signals.append(f"head ref `{head_ref}` ~ `{_AGENT_BRANCH_PREFIX}*`")
    if agent_artifact:
        signals.append("commits an LLM-triage provenance artifact")
    print(
        "FAIL-CLOSED: agent-produced triage PR is missing its "
        "deterministic-fence label — fail-closed.\n"
        f"  agent signal(s): {'; '.join(signals)}\n"
        f"  labels present : {sorted(labels) or '(none)'}\n"
        "  An agent draft PR MUST carry `llm-data-triage` or "
        "`engine-llm-triage` so its deterministic fence runs. A PR that "
        "is agent-produced but unlabeled would otherwise merge UNFENCED "
        "(the fail-open hole this guard closes). If this is a legitimate "
        "human maintenance PR to the same files, it must NOT use the "
        "`llm-triage/` branch namespace and must NOT commit a provenance "
        "artifact under docs/{llm_triage_proposals,"
        "engine_llm_triage_proposals}/."
    )
    return 1


def _env_list(name: str) -> list[str]:
    """Split an env var on newlines AND commas (the workflow joins
    labels with ``,`` and emits changed paths newline-separated; tolerate
    either for both so the predicate is robust to the wiring)."""
    raw = os.environ.get(name, "")
    return [tok.strip() for tok in re.split(r"[,\n]", raw) if tok.strip()]


def main_from_env() -> int:
    """CI entrypoint: read the PR context from the env contract and
    return the guard's exit code (does NOT call sys.exit so tests can
    assert on the return value)."""
    head_ref = os.environ.get("GUARD_HEAD_REF", "").strip()
    changed_paths = _env_list("GUARD_CHANGED_PATHS")
    labels = _env_list("GUARD_LABELS")
    return evaluate(
        head_ref=head_ref,
        changed_paths=changed_paths,
        labels=labels,
    )


if __name__ == "__main__":
    sys.exit(main_from_env())
