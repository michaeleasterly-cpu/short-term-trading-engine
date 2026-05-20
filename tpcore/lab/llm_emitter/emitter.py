"""SP-G — pure-Python emission helpers (no external calls).

This module contains the deterministic transforms that the agent in
``ops/llm_lab_emitter.py`` composes:

- ``render_candidate_spec(spec)`` — mechanically populate the SP-E /
  Readiness ten-section markdown spec from a validated ``EmittedSpec``.
  Sections §3 (byte-identical proof), §8 (data prereqs), §9 (lookahead
  honesty) are emitted as ``[OPERATOR-DRAFT]`` placeholders — the
  explicit human-in-the-loop seam (spec §3.5).
- ``validate_no_gate_override(rendered)`` — a static grep over the
  rendered markdown asserting the run command contains NO
  ``--dsr-threshold`` / ``--credibility-threshold`` flag at all (spec
  §8.3). A flag with a value above-the-floor is still rejected — the
  LLM is not allowed to write the gate values into the run command;
  the deterministic gate uses its floor (``ops/lab/run.py``).

No imports of the Anthropic SDK, no subprocess, no I/O. Pure transforms
+ a string scanner. Engine-FREE.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from tpcore.lab.llm_emitter.models import EmittedSpec


# Spec §8.3 (test_emitter_cannot_bypass_gate): the rendered run command
# must contain NO gate-override flags below the floor. The cleanest fence
# is "the run command must contain NO gate-override flags at all" — the
# deterministic gate uses its own floor (``ops/lab/run.py``); the LLM
# has no business naming a threshold.
GATE_OVERRIDE_FORBIDDEN_FLAGS: tuple[str, ...] = (
    "--dsr-threshold",
    "--credibility-threshold",
)


class GateOverrideRejected(Exception):
    """Raised when the rendered spec contains a forbidden gate-override
    flag. The agent treats this as a fatal validation failure (no draft
    PR is opened; the ledger row is already written, see spec §3.4 step
    5 — operator runbook documents the orphaned-spend recovery)."""


def validate_no_gate_override(rendered_markdown: str) -> None:
    """Grep the rendered spec for a forbidden gate-override flag.

    Spec §8.3: the rendered ``python -m ops.lab ...`` run command MUST
    NOT contain any ``--dsr-threshold`` or ``--credibility-threshold``
    flag. Raises ``GateOverrideRejected`` on a hit.

    The check is a substring scan (not a regex over arg shapes) — the
    intent is to make the LLM CANNOT write such a flag into the run
    command, full stop. The deterministic gate at ``ops/lab/run.py``
    uses its floor unconditionally.
    """
    for flag in GATE_OVERRIDE_FORBIDDEN_FLAGS:
        if flag in rendered_markdown:
            raise GateOverrideRejected(
                f"rendered spec contains forbidden gate-override flag "
                f"{flag!r}; the LLM cannot write gate thresholds into the "
                f"run command (spec §8.3, the make-or-break safety test)"
            )


def _format_param_ranges_block(spec: EmittedSpec) -> str:
    lines = ["```python", "param_ranges = {"]
    for k, v in spec.param_ranges.items():
        # Render tuples as Python-readable literals (the LabTarget shape).
        lines.append(f"    {k!r}: {tuple(v)!r},")
    lines.append("}")
    lines.append("```")
    return "\n".join(lines)


def _format_choice_keys(spec: EmittedSpec) -> str:
    """List the ``choice:`` toggles (for the Readiness §2 + §10 grep
    proofs that name "exactly one PARAM_RANGES toggle")."""
    keys = [
        name
        for name, val in spec.param_ranges.items()
        if isinstance(val[2], str) and val[2].startswith("choice:")
    ]
    return ", ".join(repr(k) for k in keys) if keys else "(none)"


def render_candidate_spec(spec: EmittedSpec) -> str:
    """Produce the SP-E / Readiness-shaped markdown spec.

    All ten Readiness sections are emitted. Sections that are
    mechanically derivable from the ``EmittedSpec`` are filled in; the
    three operator-review sections (§3 byte-identical proof, §8 data
    prereqs, §9 lookahead honesty) carry an explicit ``[OPERATOR-DRAFT]``
    placeholder block describing what the operator must harden before
    moving the PR out of draft.
    """
    title = (
        f"# {spec.candidate_name.replace('-', ' ').title()} "
        f"Lab Candidate (LLM-emitted, draft)"
    )

    run_cmd = (
        f"python -m ops.lab \\\n"
        f"    --candidate {spec.candidate_name} \\\n"
        f"    --target-engine {spec.target_engine} \\\n"
        f"    --intent {spec.intent} \\\n"
        f"    --trials {spec.expected_trials}"
    )

    parts: list[str] = [
        title,
        "",
        "**Status:** DRAFT — emitted by the SP-G LLM spec-emitter. "
        "Not Lab-ready until the three operator-review sections "
        "(§3, §8, §9) are hardened by the operator and the draft "
        "PR is moved out of draft.",
        f"**Target engine:** `{spec.target_engine}`",
        f"**Intent:** `{spec.intent}`",
        f"**Primary metric:** `LabPrimaryMetric.{spec.primary_metric.name}`",
        "",
        "---",
        "",
        "## 1. Single pre-registered primary hypothesis",
        "",
        f"**Primary hypothesis (ONE, pre-registered, pinned):** "
        f"{spec.primary_hypothesis}",
        "",
        f"**Primary metric / verdict (ONE):** "
        f"`LabPrimaryMetric.{spec.primary_metric.name}` "
        f"(declared on `{spec.target_engine}.backtest.LAB_TARGET.primary_metric`).",
        "",
        f"**Falsification criterion (pre-registered, red-is-red):** "
        f"{spec.falsification_criterion}",
        "",
        "- **No post-hoc metric shopping.** The success/falsification "
        "criterion above is pinned BEFORE the run.",
        "- **This is NOT a sweep.** The only Lab-sampled values are the "
        f"declared `param_ranges` keys: {_format_choice_keys(spec)}.",
        "- Every other engine constant is a code constant, never Lab-sampled.",
        "",
        "## 2. Feature-flag-variant pattern",
        "",
        f"The variant is reached by the engine's existing ``_*_OVERRIDE`` "
        f"pattern in ``{spec.target_engine}/backtest.py``; "
        f"`default_params()` returns the legacy default so the dossier "
        f"`param_diff` carries the true `legacy -> variant` delta.",
        "",
        "Declared `param_ranges` (the ONLY Lab-sampled surface):",
        "",
        _format_param_ranges_block(spec),
        "",
        "## 3. Byte-identical live path (the make-or-break proof)",
        "",
        "**[OPERATOR-DRAFT]** The operator must harden this section "
        "before moving the draft PR out of draft. Required:",
        "",
        f"- A characterization test `{spec.target_engine}/tests/"
        f"test_lab_{spec.candidate_name.replace('-', '_')}_byte_identical.py` "
        "that pins a committed golden of `run_<engine>_with_context(ctx, "
        "overrides={})` field-for-field equal to the pre-candidate "
        "behaviour (C1).",
        "- The test asserts the flag default is the legacy path (C2).",
        "- The test asserts the variant is reachable and distinct (C3).",
        "- The test asserts no cross-trial leakage (C4).",
        "- The golden is captured BEFORE the variant code exists (TDD RED).",
        "",
        "## 4. n_trials ledger acknowledgement",
        "",
        f"This run records its `--trials={spec.expected_trials}` spend to "
        f"the cumulative ledger (`tpcore.lab.ledger.record_trial_spend` -> "
        f"`lab_trial_ledger.{spec.target_engine}` in "
        f"`platform.data_quality_log`), unconditionally at sample time. "
        f"The verdict's DSR is deflated against "
        f"`tpcore.lab.ledger.cumulative_n_trials('{spec.target_engine}') "
        f"+ this_run_trials` — **not** the single run's `--trials` in "
        "isolation. The author acknowledges cumulative (not per-run) DSR "
        "deflation: every prior Lab run against this target makes this "
        "run's gate strictly harder.",
        "",
        "**Pre-emission ledger budget**: this emission was preceded by a "
        "`tpcore.lab.llm_emitter.ledger_gate.check_budget` call that "
        "verified `cumulative + expected_trials <= "
        "EMISSION_QUOTA_PER_TARGET` (spec §4.1 rate-limit fence).",
        "",
        "## 5. Roster-targeting prerequisite (post-SP-B)",
        "",
        f"The target engine `{spec.target_engine}` is in "
        f"`tpcore.engine_profile.lab_targetable_engines()` (verified by "
        "the agent at emission time). The candidate adds **zero** changes "
        "to the Lab CLI, dispatch, `tpcore/lab/`, or any SoT/roster.",
        "",
        "## 6. The gate is sacred — preserved or strengthened",
        "",
        "The candidate routes through `python -m ops.lab` -> "
        "`_run_lab_core` -> `survived` -> dossier -> ECR like every "
        "other candidate. The verdict is the deterministic "
        "`DSR >= 0.95 AND credibility >= 60 AND n_trades >= 3` floor. "
        "No clause is relaxed. The run command below contains NO "
        "gate-threshold-override flag (the SP-G renderer mechanically "
        "forbids any flag in `GATE_OVERRIDE_FORBIDDEN_FLAGS`).",
        "",
        "## 7. Lab credibility namespacing",
        "",
        f"The candidate writes its experimental credibility under the "
        f"`backtest_credibility.lab.{spec.candidate_name}` namespace "
        "(the `_lab_credibility_engine_name` H-S2-3 mechanism). "
        "No new migration, no new table, no new SoT.",
        "",
        "## 8. Data prerequisites stated honestly",
        "",
        "**[OPERATOR-DRAFT]** The operator must list every data "
        "dependency the candidate consumes with its status + concrete "
        "evidence (a live row/ticker count, not 'it should be there'). "
        "Any genuine BLOCKER is stated precisely (the exact missing "
        "table/column, the `information_schema` query result) and "
        "resolved by a single pre-registered conservative fallback.",
        "",
        "## 9. Lookahead / point-in-time honesty",
        "",
        "**[OPERATOR-DRAFT]** The operator must verify every signal the "
        "variant scores uses strictly point-in-time / backward data "
        "windows. Degenerate cross-sectional inputs have a pinned, "
        "unit-tested neutral guard. Entry/exit mechanics, sizing, "
        "crash-guard, and the cost model are unchanged — the variant "
        "changes which names are selected/scored, not the trade "
        "machinery.",
        "",
        "## 10. Compliance verifications (the grep-able set)",
        "",
        "- **Exactly one (or declared) `param_ranges` toggle.** The "
        f"declared `choice:` keys are: {_format_choice_keys(spec)}.",
        "- **Live path files untouched.** `git diff --name-only` "
        f"contains no `{spec.target_engine}/plugs/`, "
        f"`{spec.target_engine}/scheduler.py`, "
        f"`{spec.target_engine}/order_manager.py`, "
        "`scripts/run_all_engines.sh`, `ops/platform_pipeline.py`, "
        "`tpcore/lab/`, or any SoT/roster file. (Enforced by SP-G "
        "`tpcore.lab.llm_emitter.diff_fence`.)",
        "- **Characterization golden present + RED-first.** "
        "[OPERATOR-DRAFT §3]",
        "- **Roster target verified.** "
        f"`'{spec.target_engine}' in lab_targetable_engines()` returns "
        "True (verified by the SP-G agent at emission time).",
        "- **No gate override below the floor.** The intended run "
        "command carries no gate-threshold-override flag whatsoever "
        "(mechanically enforced by `validate_no_gate_override`; the "
        "list of forbidden flags is exported from "
        "`tpcore.lab.llm_emitter.emitter` as "
        "`GATE_OVERRIDE_FORBIDDEN_FLAGS`).",
        "- **n_trials acknowledgement present.** See §4.",
        "- **Single-hypothesis attestation.** See §1.",
        "",
        "---",
        "",
        "## Rationale (LLM-emitted, advisory only)",
        "",
        spec.rationale,
        "",
        "## Run command",
        "",
        "```bash",
        run_cmd,
        "```",
        "",
        "---",
        "",
        "**Advisory only.** This spec was emitted by the SP-G thin "
        "advisory LLM spec-emitter. The draft PR is human-merge-only; "
        "the operator hardens §3, §8, §9 before moving the PR out of "
        "draft. The deterministic gate (`DSR >= 0.95 AND credibility "
        ">= 60 AND n_trades >= 3`) is sacred — never relaxed.",
        "",
    ]
    rendered = "\n".join(parts)
    validate_no_gate_override(rendered)
    return rendered


__all__ = [
    "GATE_OVERRIDE_FORBIDDEN_FLAGS",
    "GateOverrideRejected",
    "render_candidate_spec",
    "validate_no_gate_override",
]
