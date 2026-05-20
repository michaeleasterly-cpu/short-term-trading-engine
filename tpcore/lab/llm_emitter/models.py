"""SP-G — Lab spec-emitter pydantic-v2 contract models.

These are the ONLY data shapes the LLM sees (``EmissionContext``) and the
ONLY data shape the LLM is allowed to emit (``EmittedSpec``). All models
are frozen + ``extra="forbid"`` so a malformed LLM response fails
validation rather than degrading the downstream pipeline silently.

Engine-FREE: imports only pydantic + stdlib + ``tpcore.lab.target`` (for
``LabPrimaryMetric``; SP-B / SP-D vocabulary).

The pydantic validators are the structural enforcement of the
non-negotiable Readiness §1 + §2 mandates (single hypothesis, exactly
one ``choice:`` toggle for ``fold_existing``).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tpcore.lab.target import LabPrimaryMetric


class RosterTarget(BaseModel):
    """One roster-resolved Lab target the LLM may name as
    ``EmittedSpec.target_engine``.

    Built by the agent in ``ops/llm_lab_emitter.py`` from
    ``tpcore.engine_profile.lab_targetable_engines()`` (the SP-B
    derivation; an engine in ``LifecycleState.{LAB,PAPER,LIVE}`` minus
    the allocator, the ``lab`` sentinel, and ``canary``). The LLM
    NEVER reads ``_PROFILE`` directly; it reads ``RosterTarget``
    instances via ``EmissionContext.roster_targets``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]+$", min_length=2)]
    lifecycle_state: Literal["LAB", "PAPER", "LIVE"]
    primary_metric: LabPrimaryMetric
    declared_param_ranges: dict[str, tuple]


class LedgerEntry(BaseModel):
    """One per-target row of the SP-A cumulative ledger state surfaced
    to the LLM in ``EmissionContext.ledger_state``.

    ``cumulative_n_trials`` is the SUM over every prior
    ``lab_trial_ledger.<target>`` spend row (the SP-A monotone-harder
    constraint). The LLM may read it to choose a target with budget
    remaining; SP-G's hard fence is that the agent (NOT the LLM) calls
    ``ledger_gate.check_budget`` before invoking Anthropic (§3.4 step 1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str
    cumulative_n_trials: int
    quota: int  # the EMISSION_QUOTA_PER_TARGET applied at this snapshot

    @field_validator("cumulative_n_trials", "quota")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("cumulative_n_trials / quota must be >= 0")
        return v


class ReferenceExcerpt(BaseModel):
    """An operator-staged reference bundle excerpt the LLM may read.

    Q3 (operator-confirmed): per-emission ``--reference-bundle <name>``
    skill argument names one curated subset under
    ``docs/lab_emitter_references/``. The agent reads the named bundle
    file and ships it as one or more ``ReferenceExcerpt`` instances in
    ``EmissionContext.reference_excerpts``. The LLM cannot fetch new
    references itself (no ``tools``, no network calls beyond the
    Anthropic SDK call). ``name`` is the bundle file stem (e.g.
    ``carver_systematic_trading``) for provenance tracing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]+$", min_length=2)]
    text: Annotated[str, Field(min_length=1, max_length=64_000)]


class EmissionContext(BaseModel):
    """The complete input contract for one LLM emission.

    Assembled by the agent before the Anthropic call; the LLM sees ONLY
    this schema. The agent NEVER embeds raw repo paths, live
    credentials, or the engine source tree in the prompt.

    All fields are frozen tuples / dicts; ``extra="forbid"`` so a typo
    in the agent's constructor fails loud at validation time rather than
    silently dropping context.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    roster_targets: tuple[RosterTarget, ...]
    ledger_state: tuple[LedgerEntry, ...]
    readiness_checklist_version: str
    reference_excerpts: tuple[ReferenceExcerpt, ...]
    persona_version: str
    emission_quota_remaining: int  # the per-target budget for the current target

    @field_validator("emission_quota_remaining")
    @classmethod
    def _quota_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("emission_quota_remaining must be >= 0")
        return v


class EmittedSpec(BaseModel):
    """The complete output contract for one LLM emission.

    The LLM's JSON response is parsed into this model; a malformed
    response is REJECTED (no draft PR, no ledger spend). The pydantic
    validators encode the Readiness §1 + §2 mandates:

    - exactly ONE primary hypothesis + ONE primary metric;
    - for ``fold_existing`` candidates ``param_ranges`` carries exactly
      ONE ``choice:`` toggle (the feature-flag-variant shape; the SP-E
      Sentinel pilot is the canonical instance);
    - ``candidate_name`` is a slug suitable for both a docs filename
      and a branch name (no spaces, no shell metacharacters).

    NOTE: ``target_engine`` membership in
    ``tpcore.engine_profile.lab_targetable_engines()`` is enforced
    SEPARATELY by the agent at emission time (an at-validation roster
    check would couple this engine-FREE module to ``engine_profile``;
    the agent already imports ``engine_profile`` to build the context).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]+$", min_length=2)]
    target_engine: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]+$", min_length=2)]
    intent: Literal["fold_existing", "promote_new"]
    primary_hypothesis: Annotated[str, Field(min_length=1, max_length=2_000)]
    primary_metric: LabPrimaryMetric
    param_ranges: dict[str, tuple]
    rationale: Annotated[str, Field(min_length=1, max_length=8_000)]
    falsification_criterion: Annotated[str, Field(min_length=1, max_length=2_000)]
    expected_trials: Annotated[int, Field(ge=1, le=10_000)]

    @model_validator(mode="after")
    def _validate_param_ranges_shape(self) -> EmittedSpec:
        """Param-ranges shape per spec §3.3 + Readiness §2.

        Mirrors ``LabTarget.model_post_init`` shape-checking (a malformed
        range is fail-loud at DECLARATION, not at sample time) AND adds
        the SP-G-specific Readiness §2 mandate: a ``fold_existing``
        candidate has exactly ONE ``choice:`` toggle (the feature-flag-
        variant shape — the SP-E Sentinel pilot is the canonical
        instance). ``promote_new`` may have multiple ranges (a new
        engine is allowed a parameter surface), but is still single-
        hypothesis by ``primary_hypothesis`` declaration.
        """
        if not self.param_ranges:
            raise ValueError(
                "EmittedSpec.param_ranges must declare at least one swept "
                "parameter (the Lab needs SOMETHING to sample)"
            )

        choice_keys: list[str] = []
        for name, spec in self.param_ranges.items():
            if not isinstance(spec, tuple) or len(spec) != 3:
                raise ValueError(
                    f"EmittedSpec.param_ranges[{name!r}] must be a 3-tuple "
                    f"(low, high, kind); got {spec!r}"
                )
            kind = spec[2]
            if not isinstance(kind, str):
                raise ValueError(
                    f"EmittedSpec.param_ranges[{name!r}] kind must be str; "
                    f"got {kind!r}"
                )
            if kind in ("float", "int"):
                continue
            if not kind.startswith("choice:"):
                raise ValueError(
                    f"EmittedSpec.param_ranges[{name!r}] kind {kind!r} not "
                    f"in 'float'|'int'|'choice:<csv>'"
                )
            members = [c for c in kind.split(":", 1)[1].split(",") if c.strip()]
            if not members:
                raise ValueError(
                    f"EmittedSpec.param_ranges[{name!r}] kind {kind!r}: a "
                    f"'choice:' kind needs >=1 non-empty member"
                )
            choice_keys.append(name)

        if self.intent == "fold_existing" and len(choice_keys) != 1:
            raise ValueError(
                f"fold_existing emission must declare exactly ONE 'choice:' "
                f"toggle (the feature-flag-variant shape — Readiness §2); "
                f"got {len(choice_keys)} choice keys: {choice_keys!r}"
            )
        return self

    def as_dict(self) -> dict[str, Any]:
        """Stable JSON-serializable shape for the sidecar file."""
        return {
            "candidate_name": self.candidate_name,
            "target_engine": self.target_engine,
            "intent": self.intent,
            "primary_hypothesis": self.primary_hypothesis,
            "primary_metric": self.primary_metric.value,
            "param_ranges": {k: list(v) for k, v in self.param_ranges.items()},
            "rationale": self.rationale,
            "falsification_criterion": self.falsification_criterion,
            "expected_trials": self.expected_trials,
        }


__all__ = [
    "EmissionContext",
    "EmittedSpec",
    "LedgerEntry",
    "ReferenceExcerpt",
    "RosterTarget",
]
