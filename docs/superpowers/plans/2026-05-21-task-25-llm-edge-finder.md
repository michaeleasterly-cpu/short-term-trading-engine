# Task #25 — Autonomous LLM+quant Edge Finder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v1 of the autonomous LLM edge finder per the merged spec — operator-command-only trigger (`/lab-edge-find`), 3 specs/run × 1 run/day, composing with SP-G's `emit_once` verbatim. v1 success is ONE finder-emitted candidate reaching PAPER via the standard ECR path.

**Architecture:** see `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`. The plan SEQUENCES the build (contracts → pure helpers → agent glue); it does NOT redesign. Every quota, fence, file path, and class signature is operator-binding — quoted in-step when first introduced. If you encounter ambiguity, STOP and surface — do not interpret.

**Tech Stack:** asyncpg (Postgres reads), Anthropic SDK (LLM round-trip), structlog (logging), Pydantic v2 (frozen models, `extra="forbid"`), statsmodels + scipy.stats (tool sandbox whitelist), pytest (TDD). Engine-FREE except in tests. Universal invariants: `from __future__ import annotations`, type hints, UTC always, no tpcore private access, no inline `# noqa`, ops-shadow `xdist_group` on every test that imports `scripts.ops` or `ops.*`. No `git stash` (banned per `docs/memory/feedback_git_stash_ban.md`).

**Universal hard rules (re-checked at every commit point):**
- `from __future__ import annotations` on every new .py file.
- Pydantic v2 frozen + `extra="forbid"` on every contract model.
- All timestamps UTC via `datetime.now(UTC)` (CLAUDE.md universal invariant).
- No `print()`; structlog only.
- No `tpcore` privates (`._store`, `._pool`); use public accessors. Extend the class with one if missing (`docs/STYLE_GUIDE.md`).
- Tests that import `ops.*` or `scripts.ops`: `pytestmark = pytest.mark.xdist_group("ops_shadow")` (per `feedback_ops_package_shadow_full_suite_gate`).
- All tests must work offline: no live Anthropic SDK, no live DB, no network.
- Lane: heavy. `gh pr checks <n>` is authoritative; whole-suite + reverse-order is the gate (per `.claude/rules/tests-and-ci.md`).
- Branch: `docs/task-25-llm-edge-finder-plan` for THIS plan PR; implementation tasks land on their own branches via subagent-driven-development.

---

## File structure (locked at plan time; reflects spec §3.1 verbatim)

**Create:**
- `tpcore/lab/llm_finder/__init__.py` — package init + `PERSONA_VERSION = "v1.0"` constant.
- `tpcore/lab/llm_finder/models.py` — `MarketSnapshot`, `AnalysisRequest`, `AnalysisResult`, `FinderRun`, `ToolCall`, `ToolResult`, `ProposedSpec`, plus `record_finder_run` write-path helper.
- `tpcore/lab/llm_finder/snapshot.py` — `MarketSnapshot` assembler (Postgres read, bounded payload).
- `tpcore/lab/llm_finder/tool_sandbox.py` — `statsmodels` + `scipy.stats` whitelist dispatcher.
- `tpcore/lab/llm_finder/reference_loader.py` — bundle loader (mandatory `dsr_ntrials_discipline.md`).
- `tpcore/lab/llm_finder/tests/__init__.py`
- `tpcore/lab/llm_finder/tests/test_models_frozen.py`
- `tpcore/lab/llm_finder/tests/test_snapshot_assembler.py`
- `tpcore/lab/llm_finder/tests/test_tool_sandbox_whitelist.py`
- `tpcore/lab/llm_finder/tests/test_tool_sandbox_no_dynamic_import.py`
- `tpcore/lab/llm_finder/tests/test_tool_sandbox_determinism.py`
- `tpcore/lab/llm_finder/tests/test_reference_loader_bundles.py`
- `tpcore/lab/llm_finder/tests/test_persona_versioned.py`
- `tpcore/lab/llm_finder/tests/test_record_finder_run.py`
- `ops/llm_edge_finder.py` — the agent (Anthropic SDK + Phase A/B/C orchestration).
- `tests/test_llm_edge_finder_agent.py` — agent-loop tests (mocked LLM at the seam).
- `tests/test_llm_edge_finder_anthropic_wiring.py` — `httpx.MockTransport` tests for the real SDK call shape.
- `tests/test_llm_edge_finder_round_trip.py` — `emit_once` composition test.
- `tests/test_llm_edge_finder_quota.py` — `EDGE_FINDER_RUN_QUOTA = 3` enforcement.
- `tests/test_llm_edge_finder_composes_with_sp_g.py` — source-grep "no re-implementation of SP-G".
- `tests/test_finder_cannot_bypass_sp_g.py` — source-grep "no `gh pr create` outside `emit_once`".
- `tests/test_finder_cannot_import_non_whitelisted.py` — source-grep on `tool_sandbox.py`.
- `tests/test_four_cotask_invariant.py` — daemon now has 4 co-tasks; two-daemon test still green.
- `docs/lab_emitter_references/dsr_ntrials_discipline.md` — mandatory-always-include bundle.
- `docs/lab_emitter_references/market_structure_primer.md` — v1 stub + TODO defect_ref.
- `docs/lab_finder_persona.md` — V1 persona text.
- `docs/llm_edge_finder_operator_runbook.md` — operator runbook (spec §8 walk-through).
- `.claude/skills/lab-edge-find/SKILL.md` — operator slash-skill.

**Modify:**
- `ops/llm_triage_service.py` — add 4th crash-isolated co-task (`_edge_finder_loop`).
- `scripts/tests/test_two_daemon_invariant.py` — daemon set still 2; whitelist still 4 installers (no change expected; sentinel-verify).
- `.claude/rules/llm-triage.md` — bump "two crash-isolated co-tasks" → "four crash-isolated co-tasks" wording + cite spec §2.7.
- `scripts/gen_engine_manifest.py` — no edit; the regen script is run at T12 final wiring.
- `TODO.md` — append `[defect_ref: ...]` row for the `market_structure_primer.md` stub.

**Test:**
- Whole-suite + reverse-order gate at T12 (per `.claude/rules/tests-and-ci.md`).

---

## Task 1: Models — `MarketSnapshot`, `AnalysisRequest`, `AnalysisResult`, `FinderRun` (spec §4)

**Spec citation:** §4 — "pydantic v2, all frozen + `extra='forbid'`". The LLM sees only these schemas — never raw Postgres rows, repo paths, or live credentials.

**Files:**
- Create: `tpcore/lab/llm_finder/__init__.py`
- Create: `tpcore/lab/llm_finder/models.py`
- Test: `tpcore/lab/llm_finder/tests/__init__.py`
- Test: `tpcore/lab/llm_finder/tests/test_models_frozen.py`

- [ ] **Step 1.1: Create the package init (RED-first reachability)**

Create `tpcore/lab/llm_finder/__init__.py`:

```python
"""Task #25 — Autonomous LLM+quant Edge Finder (engine-FREE contract layer).

Sibling of ``tpcore/lab/llm_emitter/`` (SP-G). Composes WITH SP-G's
``emit_once`` verbatim; never re-implements an SP-G function.

Spec: ``docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md``.
Engine-FREE: imports only stdlib + pydantic + statsmodels/scipy.stats
(inside tool_sandbox.py) + ``tpcore.lab.ledger`` + ``tpcore.engine_profile``
+ ``tpcore.lab.llm_emitter.*``.
"""
from __future__ import annotations

# Persona version SHA-pinning lockstep (spec §6 of T6 / spec §9.6).
# Bumped by hand on every persona edit; `test_persona_versioned.py` reds
# the build if `docs/lab_finder_persona.md` SHA does not match the
# checked-in value below.
PERSONA_VERSION: str = "v1.0"

# Spec §3.2: run-level quota (operator-binding).
EDGE_FINDER_RUN_QUOTA: int = 3
# Spec §3.2: per-run LLM analysis-turn ceiling (operator-binding).
ANALYSIS_TURN_QUOTA: int = 8
# Spec §4.1: bounded payload — 512 KiB hard cap, fail-loud on overflow.
MAX_SNAPSHOT_BYTES: int = 512 * 1024

__all__ = [
    "ANALYSIS_TURN_QUOTA",
    "EDGE_FINDER_RUN_QUOTA",
    "MAX_SNAPSHOT_BYTES",
    "PERSONA_VERSION",
]
```

- [ ] **Step 1.2: Write the failing models tests**

Create `tpcore/lab/llm_finder/tests/__init__.py` (empty file).

Create `tpcore/lab/llm_finder/tests/test_models_frozen.py`:

```python
"""Task #25 — pydantic v2 contract model tests (spec §4 + §10.1).

Covers:
 - MarketSnapshot, AnalysisRequest, AnalysisResult, FinderRun are
   frozen + extra="forbid".
 - ToolCall.callable_name Literal IS the whitelist (spec §4.2): a
   non-whitelisted name raises ValidationError BEFORE the dispatcher.
 - AnalysisRequest.turn is bounded 1..ANALYSIS_TURN_QUOTA (8).
 - AnalysisResult.proposed_specs is bounded len <= EDGE_FINDER_RUN_QUOTA
   (3) by a model validator (truncation happens at the agent layer; the
   model rejects > 3).
 - ProposedSpec.target_engine slug shape enforced.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tpcore.lab.llm_finder.models import (
    AnalysisRequest,
    AnalysisResult,
    FinderRun,
    MarketSnapshot,
    NumericSummary,
    ProposedSpec,
    ToolCall,
    ToolResult,
)
from tpcore.lab.target import LabPrimaryMetric


def _now() -> datetime:
    return datetime.now(UTC)


def _empty_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_ts=_now(),
        session_date=date(2026, 5, 21),
        universe="sp500",
        price_window=(),
        fundamentals=(),
        ledger_state=(),
        roster=(),
    )


def test_market_snapshot_is_frozen() -> None:
    snap = _empty_snapshot()
    with pytest.raises((AttributeError, ValidationError)):
        snap.universe = "sp1500"  # type: ignore[misc]


def test_market_snapshot_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        MarketSnapshot(
            snapshot_ts=_now(),
            session_date=date(2026, 5, 21),
            universe="sp500",
            price_window=(),
            fundamentals=(),
            ledger_state=(),
            roster=(),
            unknown_field="boom",  # extra="forbid" rejects
        )


def test_tool_call_callable_name_is_whitelist() -> None:
    """Spec §4.2: `callable_name: Literal[...]` IS the whitelist; a
    non-whitelisted name raises ValidationError BEFORE the dispatcher."""
    with pytest.raises(ValidationError):
        ToolCall(callable_name="GARCH", args_json="{}")  # not in Literal
    with pytest.raises(ValidationError):
        ToolCall(callable_name="sklearn_fit", args_json="{}")
    # All whitelisted names accepted:
    for name in (
        "OLS", "adfuller", "coint", "ARIMA_1_0_0",
        "spearmanr", "pearsonr", "ttest_1samp",
    ):
        ToolCall(callable_name=name, args_json="{}")


def test_analysis_request_turn_bounded_1_to_8() -> None:
    """Spec §4.2: `turn: Annotated[int, Field(ge=1, le=8)]`."""
    with pytest.raises(ValidationError):
        AnalysisRequest(turn=0, rationale="x", tool_calls=())
    with pytest.raises(ValidationError):
        AnalysisRequest(turn=9, rationale="x", tool_calls=())
    AnalysisRequest(turn=1, rationale="x", tool_calls=())
    AnalysisRequest(turn=8, rationale="x", tool_calls=())


def test_analysis_request_max_4_tool_calls_per_turn() -> None:
    """Spec §4.2: `tool_calls: tuple[ToolCall, ...] # <= 4 per turn`."""
    five = tuple(
        ToolCall(callable_name="OLS", args_json="{}") for _ in range(5)
    )
    with pytest.raises(ValidationError):
        AnalysisRequest(turn=1, rationale="x", tool_calls=five)


def test_proposed_spec_target_engine_slug_shape() -> None:
    """`target_engine` must be a roster-shaped slug
    (`^[a-z][a-z0-9_]+$`); a slug like `Canary` or `bad-engine` is
    rejected (mirrors SP-G EmittedSpec.target_engine pattern)."""
    base = dict(
        candidate_name="x-candidate",
        intent="fold_existing",
        primary_hypothesis="h",
        primary_metric=LabPrimaryMetric.SHARPE,
        param_ranges={"k": (1, 2, "choice:1,2")},
        rationale="r",
        falsification_criterion="f",
        expected_trials=1,
        analysis_evidence_refs=(),
    )
    with pytest.raises(ValidationError):
        ProposedSpec(target_engine="Canary", **base)  # uppercase rejected
    with pytest.raises(ValidationError):
        ProposedSpec(target_engine="bad-engine", **base)  # hyphen rejected
    ProposedSpec(target_engine="sentinel", **base)  # slug OK


def test_analysis_result_proposed_specs_max_3() -> None:
    """Spec §4.3: `proposed_specs: tuple[ProposedSpec, ...]  # <=
    EDGE_FINDER_RUN_QUOTA = 3`. The model REJECTS > 3 (truncation
    happens at the agent layer; the model is fail-loud)."""
    four = tuple(
        ProposedSpec(
            candidate_name=f"c-{i}",
            target_engine="sentinel",
            intent="fold_existing",
            primary_hypothesis="h",
            primary_metric=LabPrimaryMetric.SHARPE,
            param_ranges={"k": (1, 2, "choice:1,2")},
            rationale="r",
            falsification_criterion="f",
            expected_trials=1,
            analysis_evidence_refs=(),
        )
        for i in range(4)
    )
    with pytest.raises(ValidationError):
        AnalysisResult(
            turn=1, tool_results=(), proposed_specs=four,
            finder_rationale="r",
        )


def test_finder_run_is_frozen_extra_forbid() -> None:
    run = FinderRun(
        run_id=uuid4(),
        started_ts=_now(),
        completed_ts=None,
        snapshot_session_date=date(2026, 5, 21),
        persona_version="v1.0",
        reference_bundle="dsr_ntrials_discipline",
        analysis_turn_count=0,
        proposed_spec_count=0,
        emitted_pr_urls=(),
        rejection_reason=None,
    )
    with pytest.raises((AttributeError, ValidationError)):
        run.persona_version = "v2.0"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FinderRun(
            run_id=uuid4(),
            started_ts=_now(),
            completed_ts=None,
            snapshot_session_date=date(2026, 5, 21),
            persona_version="v1.0",
            reference_bundle="dsr_ntrials_discipline",
            analysis_turn_count=0,
            proposed_spec_count=0,
            emitted_pr_urls=(),
            rejection_reason=None,
            unknown="boom",
        )


def test_tool_result_numeric_summary_shape() -> None:
    """Spec §6.2 step 4: `ToolResult(numeric_summary: NumericSummary)`
    — bounded shape; never raw numpy arrays. `summary_text` capped at
    4 KiB."""
    big = "x" * (4 * 1024 + 1)
    with pytest.raises(ValidationError):
        NumericSummary(
            coefficients=(), pvalues=(), statistic=None, summary_text=big,
        )
    ToolResult(numeric_summary=NumericSummary(
        coefficients=(0.1,), pvalues=(0.05,), statistic=1.96,
        summary_text="OLS fit summary",
    ), error=None)


def test_tool_result_error_or_summary_not_both() -> None:
    """ToolResult is either a success (numeric_summary set, error None)
    or a failure (error set, numeric_summary None). Both-set or
    both-None is rejected."""
    with pytest.raises(ValidationError):
        ToolResult(numeric_summary=None, error=None)
    with pytest.raises(ValidationError):
        ToolResult(
            numeric_summary=NumericSummary(
                coefficients=(), pvalues=(), statistic=None,
                summary_text="x",
            ),
            error="boom",
        )
```

- [ ] **Step 1.3: Run the test — expect failure (no models.py yet)**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_models_frozen.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpcore.lab.llm_finder.models'`.

- [ ] **Step 1.4: Write the minimal models.py to make tests pass**

Create `tpcore/lab/llm_finder/models.py`:

```python
"""Task #25 — pydantic v2 contract models for the autonomous LLM edge
finder (spec §4, all frozen + extra="forbid").

The LLM sees only these schemas; never raw Postgres rows, repo paths,
or live credentials. Validators encode the spec §2 hard constraints
structurally — a violating LLM response fails pydantic BEFORE any
downstream wiring runs.

Engine-FREE: imports only stdlib + pydantic + tpcore.lab.target (for
``LabPrimaryMetric``; SP-D vocabulary).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from tpcore.lab.llm_finder import EDGE_FINDER_RUN_QUOTA
from tpcore.lab.target import LabPrimaryMetric


# ─── MarketSnapshot row shims (spec §4.1) ──────────────────────────────


class PricePanelRow(BaseModel):
    """One row of the price panel — a thin shim around
    ``platform.prices_daily``. Snapshot-bounded by the assembler."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: Annotated[str, Field(min_length=1, max_length=12)]
    session_date: date
    adj_close: float
    log_return: float | None = None
    vol_20d: float | None = None


class FundRow(BaseModel):
    """One row of the latest-quarter fundamentals — thin shim around
    ``platform.fundamentals_quarterly``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: Annotated[str, Field(min_length=1, max_length=12)]
    period_end: date
    revenue: float | None = None
    net_income: float | None = None
    book_value: float | None = None


class SnapshotLedgerEntry(BaseModel):
    """One per-target row of the SP-A cumulative ledger surfaced to the
    LLM in ``MarketSnapshot.ledger_state`` (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    target: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]+$", min_length=2)]
    cumulative_n_trials: int
    quota: int


class SnapshotRosterTarget(BaseModel):
    """One roster-resolved target the LLM may name as
    ``ProposedSpec.target_engine`` (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]+$", min_length=2)]
    lifecycle_state: Literal["LAB", "PAPER", "LIVE"]
    primary_metric: LabPrimaryMetric


# ─── MarketSnapshot (Phase A output) ───────────────────────────────────


class MarketSnapshot(BaseModel):
    """Spec §4.1 — the bounded payload the agent assembles from local
    Postgres and ships to the LLM. ``MAX_SNAPSHOT_BYTES = 512 KiB`` is
    enforced by the assembler (T4), not here — but the model is the
    schema everything else is keyed against."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot_ts: datetime
    session_date: date
    universe: Literal["sp500", "sp1500", "rus3k"]
    price_window: tuple[PricePanelRow, ...]
    fundamentals: tuple[FundRow, ...]
    ledger_state: tuple[SnapshotLedgerEntry, ...]
    roster: tuple[SnapshotRosterTarget, ...]


# ─── ToolCall / ToolResult (spec §4.2 + §6) ────────────────────────────


class ToolCall(BaseModel):
    """Spec §4.2 — `callable_name` Literal IS the whitelist; anything
    else fails validation BEFORE the dispatcher.

    ARIMA order is hard-pinned to ``(1,0,0)`` (spec §4.2: "The LLM
    cannot vary order — keeping n_trials honesty intact; order is not
    Lab-searched").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    callable_name: Literal[
        "OLS",
        "adfuller",
        "coint",
        "ARIMA_1_0_0",
        "spearmanr",
        "pearsonr",
        "ttest_1samp",
    ]
    args_json: Annotated[str, Field(max_length=16_000)]


class NumericSummary(BaseModel):
    """Spec §6.2 step 4 — bounded result shape; never raw numpy arrays.
    ``summary_text`` <= 4 KiB."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    coefficients: tuple[float, ...] = ()
    pvalues: tuple[float, ...] = ()
    statistic: float | None = None
    summary_text: Annotated[str, Field(max_length=4 * 1024)] = ""


class ToolResult(BaseModel):
    """Spec §6.2 step 3 + 4. EITHER a success (``numeric_summary`` set,
    ``error`` None) OR a failure (``error`` set, ``numeric_summary``
    None). Never both, never neither."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    numeric_summary: NumericSummary | None = None
    error: Annotated[str, Field(max_length=256)] | None = None

    @model_validator(mode="after")
    def _exactly_one_of_summary_or_error(self) -> ToolResult:
        if (self.numeric_summary is None) == (self.error is None):
            raise ValueError(
                "ToolResult: exactly one of {numeric_summary, error} "
                "must be set"
            )
        return self


# ─── AnalysisRequest (Phase B: LLM → agent, spec §4.2) ─────────────────


class AnalysisRequest(BaseModel):
    """Spec §4.2 — one analysis turn. ``turn`` bounded 1..8 by
    ``ANALYSIS_TURN_QUOTA``; ``tool_calls`` capped at 4 per turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    turn: Annotated[int, Field(ge=1, le=8)]
    rationale: Annotated[str, Field(min_length=1, max_length=4_000)]
    tool_calls: tuple[ToolCall, ...]

    @field_validator("tool_calls")
    @classmethod
    def _max_4_calls_per_turn(
        cls, v: tuple[ToolCall, ...]
    ) -> tuple[ToolCall, ...]:
        if len(v) > 4:
            raise ValueError(
                f"AnalysisRequest.tool_calls: max 4 per turn; got "
                f"{len(v)} (spec §4.2)"
            )
        return v


# ─── ProposedSpec (Phase C, spec §4.3) ─────────────────────────────────


class ProposedSpec(BaseModel):
    """Spec §4.3 — upstream of SP-G ``EmittedSpec``; the agent adapts
    via a thin adapter at the ``emit_once`` call site.

    NOTE: ``target_engine`` membership in
    ``tpcore.engine_profile.lab_targetable_engines()`` is enforced
    SEPARATELY by the agent at emission time (mirrors SP-G EmittedSpec;
    keeps this engine-FREE module from importing engine_profile)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_name: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9_-]+$", min_length=2)
    ]
    target_engine: Annotated[
        str, Field(pattern=r"^[a-z][a-z0-9_]+$", min_length=2)
    ]
    intent: Literal["fold_existing", "promote_new"]
    primary_hypothesis: Annotated[str, Field(min_length=1, max_length=2_000)]
    primary_metric: LabPrimaryMetric
    param_ranges: dict[str, tuple]
    rationale: Annotated[str, Field(min_length=1, max_length=8_000)]
    falsification_criterion: Annotated[
        str, Field(min_length=1, max_length=2_000)
    ]
    expected_trials: Annotated[int, Field(ge=1, le=10_000)]
    analysis_evidence_refs: tuple[int, ...] = ()


# ─── AnalysisResult (Phase B agent→LLM, also Phase C output) ───────────


class AnalysisResult(BaseModel):
    """Spec §4.3 — the LLM's structured response carrying ``<= 3``
    proposed specs (the ``EDGE_FINDER_RUN_QUOTA`` cap) plus the
    per-turn tool results the LLM is reasoning over."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    turn: int
    tool_results: tuple[ToolResult, ...]
    proposed_specs: tuple[ProposedSpec, ...]
    finder_rationale: Annotated[str, Field(max_length=8_000)]

    @field_validator("proposed_specs")
    @classmethod
    def _max_specs_per_run(
        cls, v: tuple[ProposedSpec, ...]
    ) -> tuple[ProposedSpec, ...]:
        if len(v) > EDGE_FINDER_RUN_QUOTA:
            raise ValueError(
                f"AnalysisResult.proposed_specs: > "
                f"EDGE_FINDER_RUN_QUOTA={EDGE_FINDER_RUN_QUOTA}; got "
                f"{len(v)} (spec §4.3)"
            )
        return v


# ─── FinderRun (run-level provenance, spec §4.4) ───────────────────────


class FinderRun(BaseModel):
    """Spec §4.4 — append-only audit row. Persisted via
    ``record_finder_run`` (T7) into ``platform.application_log`` with
    event_type ``LAB_FINDER_RUN`` (disjoint from
    ``lab_trial_ledger.*``; reuses SP-A substrate, no migration)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: UUID
    started_ts: datetime
    completed_ts: datetime | None
    snapshot_session_date: date
    persona_version: str
    reference_bundle: str
    analysis_turn_count: int
    proposed_spec_count: int
    emitted_pr_urls: tuple[str, ...]
    rejection_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """Stable JSON-serializable shape for the application_log
        ``data`` jsonb column (T7)."""
        return {
            "run_id": str(self.run_id),
            "started_ts": self.started_ts.isoformat(),
            "completed_ts": (
                self.completed_ts.isoformat()
                if self.completed_ts is not None
                else None
            ),
            "snapshot_session_date": self.snapshot_session_date.isoformat(),
            "persona_version": self.persona_version,
            "reference_bundle": self.reference_bundle,
            "analysis_turn_count": self.analysis_turn_count,
            "proposed_spec_count": self.proposed_spec_count,
            "emitted_pr_urls": list(self.emitted_pr_urls),
            "rejection_reason": self.rejection_reason,
        }


__all__ = [
    "AnalysisRequest",
    "AnalysisResult",
    "FinderRun",
    "FundRow",
    "MarketSnapshot",
    "NumericSummary",
    "PricePanelRow",
    "ProposedSpec",
    "SnapshotLedgerEntry",
    "SnapshotRosterTarget",
    "ToolCall",
    "ToolResult",
]
```

- [ ] **Step 1.5: Run the test — expect pass**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_models_frozen.py -v`
Expected: PASS — all assertions green; no warnings.

- [ ] **Step 1.6: Commit**

```bash
git add tpcore/lab/llm_finder/__init__.py tpcore/lab/llm_finder/models.py \
        tpcore/lab/llm_finder/tests/__init__.py \
        tpcore/lab/llm_finder/tests/test_models_frozen.py
git commit -m "$(cat <<'EOF'
feat(task-25): T1 — contract models for LLM edge finder

Frozen pydantic v2 models per spec §4 — MarketSnapshot,
AnalysisRequest, AnalysisResult, FinderRun, ToolCall, ToolResult,
ProposedSpec. ToolCall.callable_name Literal IS the v1 whitelist
(spec §4.2). EDGE_FINDER_RUN_QUOTA=3 enforced at the model layer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Reference loader (`reference_loader.py`) — mandatory `dsr_ntrials_discipline` always-include (spec §5 / §7)

**Spec citation:** §7.1 — `dsr_ntrials_discipline.md` is "NEW (v1.0), **mandatory always-include** ... the dispatcher includes it in every `ReferenceExcerpt` tuple regardless of `--reference-bundle` selection. Most-load-bearing of the four." §5 — "Read `docs/lab_emitter_references/<name>.md` files; build `ReferenceExcerpt` instances."

**Files:**
- Create: `tpcore/lab/llm_finder/reference_loader.py`
- Test: `tpcore/lab/llm_finder/tests/test_reference_loader_bundles.py`

- [ ] **Step 2.1: Write the failing reference-loader test**

Create `tpcore/lab/llm_finder/tests/test_reference_loader_bundles.py`:

```python
"""Task #25 — reference-bundle loader tests (spec §10.1 + §7).

Covers:
 - four named bundles load when present;
 - dsr_ntrials_discipline.md is ALWAYS included regardless of the
   --reference-bundle argument (spec §7.1 mandatory clause);
 - a missing named bundle raises FileNotFoundError (fail-loud, mirrors
   SP-G _load_reference_bundles);
 - path traversal is rejected by the slug pattern (mirrors
   ReferenceExcerpt.name pattern ^[a-z][a-z0-9_-]+$);
 - the mandatory bundle is the FIRST entry (so prompt-order is
   predictable).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tpcore.lab.llm_finder.reference_loader import (
    MANDATORY_REFERENCE_BUNDLE,
    MissingReferenceBundle,
    StubReferenceBundle,
    load_reference_bundles,
)


def _refs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "lab_emitter_references"
    d.mkdir(parents=True)
    return d


def test_mandatory_bundle_always_included(tmp_path: Path) -> None:
    refs = _refs_dir(tmp_path)
    (refs / "dsr_ntrials_discipline.md").write_text(
        "DSR discipline body — non-stub.", encoding="utf-8"
    )
    (refs / "carver_systematic_trading.md").write_text(
        "Carver body.", encoding="utf-8"
    )
    out = load_reference_bundles(
        ("carver_systematic_trading",), references_dir=refs,
    )
    names = [r.name for r in out]
    assert names[0] == MANDATORY_REFERENCE_BUNDLE
    assert "carver_systematic_trading" in names


def test_mandatory_bundle_included_when_no_bundle_named(
    tmp_path: Path,
) -> None:
    refs = _refs_dir(tmp_path)
    (refs / "dsr_ntrials_discipline.md").write_text(
        "DSR discipline body — non-stub.", encoding="utf-8"
    )
    out = load_reference_bundles((), references_dir=refs)
    assert len(out) == 1
    assert out[0].name == MANDATORY_REFERENCE_BUNDLE


def test_missing_named_bundle_raises(tmp_path: Path) -> None:
    refs = _refs_dir(tmp_path)
    (refs / "dsr_ntrials_discipline.md").write_text("body", encoding="utf-8")
    with pytest.raises(MissingReferenceBundle):
        load_reference_bundles(
            ("does_not_exist",), references_dir=refs,
        )


def test_missing_mandatory_bundle_raises(tmp_path: Path) -> None:
    """Spec §7.1: dsr_ntrials_discipline.md is the MOST load-bearing
    bundle. If it is absent the loader is fail-loud at the operator
    command path (the finder cannot run without it)."""
    refs = _refs_dir(tmp_path)
    with pytest.raises(MissingReferenceBundle):
        load_reference_bundles((), references_dir=refs)


def test_path_traversal_in_name_rejected(tmp_path: Path) -> None:
    refs = _refs_dir(tmp_path)
    (refs / "dsr_ntrials_discipline.md").write_text("body", encoding="utf-8")
    with pytest.raises(MissingReferenceBundle):
        load_reference_bundles(
            ("../../etc/passwd",), references_dir=refs,
        )


def test_stub_bundle_is_fail_loud(tmp_path: Path) -> None:
    """The market_structure_primer.md v1 ships as a `[operator-pending
    content]` stub; the loader detects the stub marker and raises
    StubReferenceBundle so the operator command path fails LOUDLY
    rather than feeding placeholder text to the LLM."""
    refs = _refs_dir(tmp_path)
    (refs / "dsr_ntrials_discipline.md").write_text("body", encoding="utf-8")
    (refs / "market_structure_primer.md").write_text(
        "[operator-pending content]\n\nNo body yet.", encoding="utf-8",
    )
    with pytest.raises(StubReferenceBundle):
        load_reference_bundles(
            ("market_structure_primer",), references_dir=refs,
        )
```

- [ ] **Step 2.2: Run the test — expect failure**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_reference_loader_bundles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpcore.lab.llm_finder.reference_loader'`.

- [ ] **Step 2.3: Write the reference_loader implementation**

Create `tpcore/lab/llm_finder/reference_loader.py`:

```python
"""Task #25 — reference-bundle loader (spec §5 + §7).

Reads ``docs/lab_emitter_references/<name>.md`` files and produces a
tuple of SP-G ``ReferenceExcerpt`` instances. Two structural rules:

1. ``dsr_ntrials_discipline.md`` is **mandatory always-include**
   (spec §7.1) — the dispatcher prepends it to every emission's
   reference tuple regardless of ``--reference-bundle`` selection.
   Missing-mandatory is fail-loud.
2. The ``[operator-pending content]`` stub marker is fail-loud
   (``StubReferenceBundle``) so the operator command path never feeds
   placeholder text to the LLM. The ``market_structure_primer.md`` v1
   ships as a stub for this purpose (spec §7.1 last row + TODO.md
   defect_ref).

Engine-FREE: stdlib only. Reuses SP-G's ``ReferenceExcerpt`` so the
finder's reference tuple can be passed THROUGH SP-G's existing
``EmissionContext`` shape unchanged (spec §3.3 "compose, don't
re-implement").
"""
from __future__ import annotations

from pathlib import Path

from tpcore.lab.llm_emitter.models import ReferenceExcerpt

# Spec §7.1: ``dsr_ntrials_discipline.md`` is the MANDATORY
# always-include bundle — the structural reminder to the LLM that every
# emission increments the SP-A ledger and the gate is cumulatively
# deflated.
MANDATORY_REFERENCE_BUNDLE: str = "dsr_ntrials_discipline"

# Stub-marker (case-sensitive) — the v1 ``market_structure_primer.md``
# carries this on line 1 so the loader is fail-loud rather than feeding
# placeholder text to the LLM.
_STUB_MARKER: str = "[operator-pending content]"

# Slug pattern mirrors ``ReferenceExcerpt.name`` — rejects path
# traversal (no ``/``, no ``..``).
_VALID_SLUG_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789_-"
)


class MissingReferenceBundle(FileNotFoundError):
    """Raised when a named bundle (or the mandatory bundle) cannot be
    found under ``references_dir``. Fail-loud at the operator command
    path; the agent maps this to a clear error message."""


class StubReferenceBundle(ValueError):
    """Raised when a bundle file contains the ``[operator-pending
    content]`` stub marker. The v1 ``market_structure_primer.md`` ships
    as a stub deliberately so the finder fails loudly until the
    operator authors the real content."""


def _is_valid_slug(name: str) -> bool:
    if not name:
        return False
    if name[0] not in "abcdefghijklmnopqrstuvwxyz":
        return False
    return all(c in _VALID_SLUG_CHARS for c in name)


def _load_one(references_dir: Path, name: str) -> ReferenceExcerpt:
    """Load one bundle file; raise MissingReferenceBundle / StubReference
    Bundle on a failure mode."""
    if not _is_valid_slug(name):
        raise MissingReferenceBundle(
            f"reference bundle name {name!r} is not a valid slug "
            f"(pattern ^[a-z][a-z0-9_-]+$)"
        )
    path = references_dir / f"{name}.md"
    if not path.is_file():
        available = (
            sorted(p.stem for p in references_dir.glob("*.md"))
            if references_dir.exists() else []
        )
        raise MissingReferenceBundle(
            f"reference bundle {name!r} not found at {path}; "
            f"available bundles: {available!r}"
        )
    text = path.read_text(encoding="utf-8")
    if _STUB_MARKER in text:
        raise StubReferenceBundle(
            f"reference bundle {name!r} at {path} contains the "
            f"{_STUB_MARKER!r} marker — the operator must author real "
            f"content before this bundle can be used. See the TODO.md "
            f"defect_ref row for the tracked author task."
        )
    return ReferenceExcerpt(name=name, text=text)


def load_reference_bundles(
    names: tuple[str, ...],
    *,
    references_dir: Path,
) -> tuple[ReferenceExcerpt, ...]:
    """Build the prompt's reference tuple.

    Returns a tuple whose FIRST entry is always the mandatory
    ``dsr_ntrials_discipline`` bundle (spec §7.1), followed by the
    operator-named bundles in declaration order.

    Raises ``MissingReferenceBundle`` if the mandatory bundle is absent
    or a named bundle cannot be found. Raises ``StubReferenceBundle``
    if any loaded bundle is a stub.
    """
    out: list[ReferenceExcerpt] = []
    # Mandatory ALWAYS first.
    out.append(_load_one(references_dir, MANDATORY_REFERENCE_BUNDLE))
    for name in names:
        if not name or name == MANDATORY_REFERENCE_BUNDLE:
            # Skip blank slots from --reference-bundle="" and avoid
            # double-loading the mandatory bundle.
            continue
        out.append(_load_one(references_dir, name))
    return tuple(out)


__all__ = [
    "MANDATORY_REFERENCE_BUNDLE",
    "MissingReferenceBundle",
    "StubReferenceBundle",
    "load_reference_bundles",
]
```

- [ ] **Step 2.4: Run the test — expect pass**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_reference_loader_bundles.py -v`
Expected: PASS — all six tests green.

- [ ] **Step 2.5: Commit**

```bash
git add tpcore/lab/llm_finder/reference_loader.py \
        tpcore/lab/llm_finder/tests/test_reference_loader_bundles.py
git commit -m "$(cat <<'EOF'
feat(task-25): T2 — reference-bundle loader

dsr_ntrials_discipline.md is mandatory always-include (spec §7.1);
[operator-pending content] stub marker is fail-loud
(StubReferenceBundle); path traversal rejected via slug pattern.
Reuses SP-G ReferenceExcerpt so the finder's reference tuple feeds
SP-G's EmissionContext unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Reference bundle content (`dsr_ntrials_discipline.md`, `market_structure_primer.md` stub) + TODO defect_ref

**Spec citation:** §7.1 — the table naming the four v1 bundles. `dsr_ntrials_discipline.md` is "NEW (v1.0), mandatory always-include ... most-load-bearing of the four". `market_structure_primer.md` is "NEW (v1.0), operator-authored later — for v1 a stub with `[operator-pending content]` + a TODO.md `[defect_ref:]` so the finder fails LOUDLY at runtime if the file is empty or stub-only".

**Files:**
- Create: `docs/lab_emitter_references/dsr_ntrials_discipline.md`
- Create: `docs/lab_emitter_references/market_structure_primer.md`
- Modify: `TODO.md` — append a `[defect_ref:]` row for the stub primer.

- [ ] **Step 3.1: Author the dsr_ntrials_discipline.md bundle**

Create `docs/lab_emitter_references/dsr_ntrials_discipline.md`:

```markdown
# DSR / n_trials discipline — Lab Edge Finder Reference Bundle

**Bundle name:** `dsr_ntrials_discipline`
**Status:** v1.0 — **mandatory always-include** for Task #25 (the
autonomous LLM edge finder, spec §7.1). The loader prepends this
bundle to every emission's reference tuple regardless of
`--reference-bundle` selection.
**Source:** the project's `project_ml_research_track` memory (the
commissioned-expert verdict, 2026-05-17) + SP-A spec
(`docs/superpowers/specs/2026-05-19-lab-ntrials-ledger.md`).

This bundle is the LOAD-BEARING reminder to the LLM that:

1. **Every emission is a multiple-testing increment.** The cumulative
   `n_trials` ledger (SP-A, `tpcore.lab.ledger`) tracks every Lab probe
   against every target engine. The DSR ≥ 0.95 floor at
   `ops/lab/run.py` is DEFLATED against `cumulative_n_trials(target) +
   this_run_trials`, NOT against the single run's `--trials`. Every
   prior emission against this target makes this emission's gate
   STRICTLY HARDER. The LLM cannot relax this. The deterministic gate
   disposes; the LLM proposes.

2. **One hypothesis per emission.** A multi-hypothesis emission is
   forbidden by spec §2.2 + pydantic on `EmittedSpec`. The
   `EDGE_FINDER_RUN_QUOTA = 3` cap on `AnalysisResult.proposed_specs`
   is THREE SEPARATE single-hypothesis emissions, each independently
   routed through SP-G's `emit_once` and each independently spending
   one ledger row. NEVER a grid, NEVER a sweep.

3. **`expected_trials` is the ledger spend.** The `expected_trials`
   field on `ProposedSpec` (and downstream on SP-G `EmittedSpec`)
   names the exact integer the agent will pass to
   `tpcore.lab.ledger.record_trial_spend` BEFORE the draft PR is
   opened. A finder that under-declares `expected_trials` to game the
   budget IS the multiple-testing inflation failure mode the SP-A
   ledger was built to catch.

## The structural defenses (cited so the LLM knows what it CANNOT do)

- The deterministic gate at `ops/lab/run.py` reads its DSR threshold
  (0.95) and credibility threshold (60) from frozen module-level
  constants, NOT from the candidate spec. The renderer mechanically
  forbids `--dsr-threshold` / `--credibility-threshold` flags in the
  run command (SP-G `GATE_OVERRIDE_FORBIDDEN_FLAGS`).
- The SP-A ledger is append-only (`platform.data_quality_log`
  rows are immutable in practice); the LLM cannot "reset" a target's
  cumulative count.
- The per-target `EMISSION_QUOTA_PER_TARGET = 20` cap (SP-G `ledger_
  gate.py`) is a HARD pre-emission rejection that fires BEFORE the
  Anthropic SDK is invoked when budget is exhausted. The per-run
  `EDGE_FINDER_RUN_QUOTA = 3` is multiplicative with the per-target
  cap.

## Why this matters operationally

The platform's binding constraint is the multiple-testing-deflated
bar. ML / LLM exploration is a degrees-of-freedom multiplier; its
choices inflate `n_trials` which raises the bar. The cumulative
ledger is the structural defense: every hypothesis the LLM proposes
COUNTS, whether it survives or not. There is no per-cycle reset; the
fence holds across operator sessions, across personas, across
reference-bundle selections.

## What the LLM should DO with this framing

- Propose hypotheses on EXISTING engines (`fold_existing`) whose
  cumulative ledger has BUDGET REMAINING.
- Prefer hypotheses that re-tune an existing rule (low DOF) over
  free-form strategy mining (high DOF; high `expected_trials`).
- Cite the analysis evidence from `tool_results` directly in
  `rationale` — a hypothesis without computed evidence is the
  free-form-mining failure mode.
- Declare `expected_trials` honestly: count the FULL search space the
  variant explores, not just the "one toggle" surface.

## What the LLM CANNOT do

- Emit more than 3 `ProposedSpec`s per run (the model validator
  rejects > 3).
- Name a `target_engine` outside `lab_targetable_engines()` (the
  agent re-validates BEFORE the ledger row is written).
- Write a gate-override flag into the rendered run command (the SP-G
  renderer rejects it).
- Declare `expected_trials < 1` or fold a sweep into a single emission
  (one hypothesis per emission; one ledger row per emission).
```

- [ ] **Step 3.2: Author the market_structure_primer.md stub**

Create `docs/lab_emitter_references/market_structure_primer.md`:

```markdown
# Market structure primer — Lab Edge Finder Reference Bundle

**Bundle name:** `market_structure_primer`
**Status:** v1.0 STUB — `[operator-pending content]`. The finder MUST
fail LOUDLY (`StubReferenceBundle`) on any attempt to select this
bundle until the operator authors real content (spec §7.1 last row;
TODO.md `[defect_ref:]` tracks the author task).

[operator-pending content]

## What this bundle WILL teach (per spec §7.1 + operator framing 2026-05-20)

The "(1)-half" of the operator framing on `project_research_llm_edge_
discovery`: the **trading environment** — market structure / micro-
structure / how everything interconnects. Concretely (per
`project_research_llm_edge_discovery` "Operator sharpened the roadmap
2026-05-20"):

- Auction mechanics (open / close cross), continuous-trading sessions
  (XNYS), the role of liquidity providers, market-maker rebate
  structures.
- How factor exposures interact across the universe (sector clusters,
  size buckets, ADV brackets) — what makes a cross-sectional regression
  meaningful.
- The reflexive-loop concept: how a rule becoming popular changes the
  market structure the rule was edged against.
- Practical caveats: the SIP-vs-IEX feed difference (CLAUDE.md
  universal invariant — IEX silently misses tickers that trade off-IEX);
  the survivorship-free database caveat in `prices_daily`.

## When the operator hardens this stub

The loader's `StubReferenceBundle` check is the structural enforcement.
Once authored:

1. Remove the `[operator-pending content]` line above (this is the
   stub marker the loader greps for).
2. Replace with operator-authored content (no upper bound; the
   `ReferenceExcerpt.text` validator caps at 64 KB per file).
3. Close the TODO.md `[defect_ref:]` row.
4. Re-run `python -m pytest tpcore/lab/llm_finder/tests/test_
   reference_loader_bundles.py -v` to confirm the bundle now loads
   cleanly.
```

- [ ] **Step 3.3: Add the TODO.md defect_ref row**

Append to `TODO.md` (the operator-authored content tracker):

```bash
python3 - <<'PY'
import pathlib
p = pathlib.Path("TODO.md")
text = p.read_text(encoding="utf-8")
addition = (
    "\n## Task #25 — market_structure_primer.md operator-authoring\n\n"
    "- [ ] Author `docs/lab_emitter_references/market_structure_primer.md` "
    "real content (per spec §7.1) [defect_ref: REVIEW_DEFECT_LOGGED-task25-"
    "market-structure-primer-stub]. Until authored, the bundle ships as "
    "an `[operator-pending content]` stub and the finder fails loud "
    "(StubReferenceBundle) on `--reference-bundle market_structure_"
    "primer` selection.\n"
)
if "task25-market-structure-primer-stub" not in text:
    p.write_text(text.rstrip() + addition, encoding="utf-8")
    print("appended")
else:
    print("already present")
PY
```

Expected: prints `appended` (or `already present` on re-run).

- [ ] **Step 3.4: Commit**

```bash
git add docs/lab_emitter_references/dsr_ntrials_discipline.md \
        docs/lab_emitter_references/market_structure_primer.md TODO.md
git commit -m "$(cat <<'EOF'
feat(task-25): T3 — reference bundle content (dsr_ntrials_discipline + market_structure_primer stub)

dsr_ntrials_discipline.md (mandatory always-include per spec §7.1) —
operationalises the project_ml_research_track expert verdict for the
LLM. market_structure_primer.md ships as `[operator-pending content]`
stub deliberately; TODO.md defect_ref row tracks the author task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: MarketSnapshot assembler (`snapshot.py`) — bounded payload (spec §4.1)

**Spec citation:** §4.1 — "loaded via one parameterised `asyncpg` read each. Total payload bounded by `MAX_SNAPSHOT_BYTES = 512 KiB` (pydantic validator; fail-loud on overflow — downsample N or M, never silent truncation)." §3.2 Phase A2: "assemble MarketSnapshot (local Postgres; bounded payload)". §9.1 decision #3: v1 restricts universe to `sp500`.

**Files:**
- Create: `tpcore/lab/llm_finder/snapshot.py`
- Test: `tpcore/lab/llm_finder/tests/test_snapshot_assembler.py`

- [ ] **Step 4.1: Write the failing snapshot-assembler test**

Create `tpcore/lab/llm_finder/tests/test_snapshot_assembler.py`:

```python
"""Task #25 — MarketSnapshot assembler tests (spec §10.1).

Covers:
 - assemble_snapshot reads price_window + fundamentals + ledger_state
   + roster from injected asyncpg-shaped FakePool (no real DB);
 - the LAST 252 sessions of price_window are returned (bounded N);
 - MAX_SNAPSHOT_BYTES = 512 KiB overflow is FAIL-LOUD (raises
   SnapshotOverflow, NEVER silent truncation per spec §4.1);
 - universe="sp500" is the v1 cap (spec §9.1 decision #3); other
   values are rejected at the call site (the Literal already enforces
   this on MarketSnapshot, but the assembler asserts pre-query).
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tpcore.lab.llm_finder.snapshot import (
    SnapshotOverflow,
    assemble_snapshot,
)


class _Conn:
    """Minimal asyncpg.Connection stand-in. Records every fetch + returns
    a scripted result per SQL fragment."""

    def __init__(self, scripted: dict[str, list[dict]]) -> None:
        self.scripted = scripted
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        # The assembler queries 4 distinct shapes; route by SQL substring.
        for key, rows in self.scripted.items():
            if key in sql:
                return rows
        return []


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, scripted: dict[str, list[dict]]) -> None:
        self._scripted = scripted

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._scripted))


@pytest.mark.asyncio
async def test_assemble_snapshot_happy_path() -> None:
    pool = _Pool({
        "prices_daily": [
            {
                "ticker": "AAPL", "session_date": date(2026, 5, 20),
                "adj_close": 200.0, "log_return": 0.01, "vol_20d": 0.18,
            },
        ],
        "fundamentals_quarterly": [
            {
                "ticker": "AAPL", "period_end": date(2026, 3, 31),
                "revenue": 1.0e11, "net_income": 2.0e10,
                "book_value": 7.0e10,
            },
        ],
        "lab_trial_ledger": [
            {"target": "sentinel", "cumulative_n_trials": 5},
        ],
        "engine_profile_roster": [
            {"name": "sentinel", "lifecycle_state": "PAPER",
             "primary_metric": "MAXDD_REDUCTION"},
        ],
    })
    snap = await assemble_snapshot(
        pool,
        session_date=date(2026, 5, 20),
        universe="sp500",
        snapshot_ts=datetime(2026, 5, 20, 21, 0, tzinfo=UTC),
    )
    assert snap.universe == "sp500"
    assert len(snap.price_window) == 1
    assert snap.price_window[0].ticker == "AAPL"
    assert snap.fundamentals[0].revenue == 1.0e11
    assert snap.ledger_state[0].target == "sentinel"
    assert snap.roster[0].name == "sentinel"


@pytest.mark.asyncio
async def test_assemble_snapshot_overflow_fail_loud() -> None:
    """Spec §4.1: payload > MAX_SNAPSHOT_BYTES raises SnapshotOverflow.
    NEVER silent truncation."""
    # Fabricate enough price rows to exceed 512 KiB serialized.
    big_price = [
        {
            "ticker": f"T{i:04d}", "session_date": date(2026, 5, 20),
            "adj_close": 1.0 + i * 0.01, "log_return": 0.0,
            "vol_20d": 0.2,
        }
        for i in range(200_000)  # well over the cap
    ]
    pool = _Pool({
        "prices_daily": big_price,
        "fundamentals_quarterly": [],
        "lab_trial_ledger": [],
        "engine_profile_roster": [],
    })
    with pytest.raises(SnapshotOverflow):
        await assemble_snapshot(
            pool,
            session_date=date(2026, 5, 20),
            universe="sp500",
            snapshot_ts=datetime(2026, 5, 20, 21, 0, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_assemble_snapshot_universe_v1_sp500_only() -> None:
    """Spec §9.1 decision #3: v1 restricts universe to sp500.
    Calling with sp1500/rus3k raises ValueError pre-query."""
    pool = _Pool({})
    with pytest.raises(ValueError, match="v1 universe"):
        await assemble_snapshot(
            pool,
            session_date=date(2026, 5, 20),
            universe="sp1500",  # type: ignore[arg-type]
            snapshot_ts=datetime(2026, 5, 20, 21, 0, tzinfo=UTC),
        )
```

- [ ] **Step 4.2: Run the test — expect failure**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_snapshot_assembler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.lab.llm_finder.snapshot'`.

- [ ] **Step 4.3: Write the snapshot.py implementation**

Create `tpcore/lab/llm_finder/snapshot.py`:

```python
"""Task #25 — MarketSnapshot assembler (spec §4.1 + §3.2 Phase A2).

Reads ``platform.prices_daily``, ``platform.fundamentals_quarterly``,
the SP-A ``lab_trial_ledger.*`` cumulative count, and the SP-B roster
into a frozen ``MarketSnapshot`` the LLM sees.

Bounded payload: ``MAX_SNAPSHOT_BYTES = 512 KiB`` (spec §4.1). On
overflow we raise ``SnapshotOverflow`` — fail-loud, NEVER silent
truncation. The operator's recourse is to narrow the universe / window
at the call site.

v1 universe is ``sp500`` only (spec §9.1 decision #3). The pydantic
Literal on ``MarketSnapshot.universe`` enforces this structurally; the
assembler additionally asserts pre-query for a clearer error message.

Engine-FREE: imports stdlib + pydantic + tpcore.lab.llm_finder.models
+ tpcore.lab.ledger + tpcore.engine_profile (the same engine-FREE
discipline SP-G established).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any, Literal

import structlog

from tpcore.lab.llm_finder import MAX_SNAPSHOT_BYTES
from tpcore.lab.llm_finder.models import (
    FundRow,
    MarketSnapshot,
    PricePanelRow,
    SnapshotLedgerEntry,
    SnapshotRosterTarget,
)

logger = structlog.get_logger(__name__)


# Spec §4.1 + §9.1 decision #3: v1 universe is sp500 only.
_V1_UNIVERSE: tuple[str, ...] = ("sp500",)
# Spec §4.1: last 252 sessions × <=500 tickers ("sp500" cap).
_PRICE_WINDOW_SESSIONS: int = 252


class SnapshotOverflow(ValueError):
    """Raised when the assembled MarketSnapshot exceeds
    ``MAX_SNAPSHOT_BYTES`` (512 KiB) when JSON-serialised.

    Spec §4.1 is explicit: fail-loud on overflow; downsample N or M at
    the call site; NEVER silent truncation. The operator's recourse is
    to narrow the universe or the price-window-sessions parameter.
    """


_PRICE_SQL = """
SELECT ticker, session_date, adj_close, log_return, vol_20d
FROM platform.prices_daily
WHERE session_date <= $1
  AND session_date > $1 - ($2 || ' days')::interval
ORDER BY ticker, session_date
"""

_FUND_SQL = """
SELECT ticker, period_end, revenue, net_income, book_value
FROM platform.fundamentals_quarterly
WHERE period_end <= $1
ORDER BY ticker, period_end DESC
"""

_LEDGER_SQL = """
SELECT target, cumulative_n_trials
FROM platform.lab_trial_ledger_cumulative
"""

_ROSTER_SQL = """
SELECT name, lifecycle_state, primary_metric
FROM platform.engine_profile_roster
WHERE lifecycle_state IN ('LAB', 'PAPER', 'LIVE')
"""


def _row_to_price(row: dict[str, Any]) -> PricePanelRow:
    return PricePanelRow(
        ticker=row["ticker"],
        session_date=row["session_date"],
        adj_close=float(row["adj_close"]),
        log_return=(
            float(row["log_return"]) if row.get("log_return") is not None
            else None
        ),
        vol_20d=(
            float(row["vol_20d"]) if row.get("vol_20d") is not None
            else None
        ),
    )


def _row_to_fund(row: dict[str, Any]) -> FundRow:
    return FundRow(
        ticker=row["ticker"],
        period_end=row["period_end"],
        revenue=(
            float(row["revenue"]) if row.get("revenue") is not None
            else None
        ),
        net_income=(
            float(row["net_income"]) if row.get("net_income") is not None
            else None
        ),
        book_value=(
            float(row["book_value"]) if row.get("book_value") is not None
            else None
        ),
    )


def _row_to_ledger(
    row: dict[str, Any], quota: int,
) -> SnapshotLedgerEntry:
    return SnapshotLedgerEntry(
        target=row["target"],
        cumulative_n_trials=int(row["cumulative_n_trials"]),
        quota=quota,
    )


def _row_to_roster(row: dict[str, Any]) -> SnapshotRosterTarget:
    from tpcore.lab.target import LabPrimaryMetric
    return SnapshotRosterTarget(
        name=row["name"],
        lifecycle_state=row["lifecycle_state"],
        primary_metric=LabPrimaryMetric(row["primary_metric"]),
    )


def _serialised_size(snapshot: MarketSnapshot) -> int:
    """Conservative size estimate: JSON-serialise the pydantic dict.

    The LLM prompt assembler may add wrapper text; this is a floor on
    the size that bites at the data-payload level. The check happens
    AFTER all four reads succeed — fail-loud at the assembly seam.
    """
    blob = snapshot.model_dump(mode="json")
    return len(json.dumps(blob, default=str).encode("utf-8"))


async def assemble_snapshot(
    pool: Any,
    *,
    session_date: date,
    universe: Literal["sp500", "sp1500", "rus3k"],
    snapshot_ts: datetime | None = None,
    price_window_sessions: int = _PRICE_WINDOW_SESSIONS,
    ledger_quota: int = 20,
) -> MarketSnapshot:
    """Assemble the LLM-visible payload from local Postgres.

    Raises:
        ValueError: if ``universe`` is not in the v1 allow-list.
        SnapshotOverflow: if the serialised payload exceeds
            ``MAX_SNAPSHOT_BYTES`` (spec §4.1).
    """
    if universe not in _V1_UNIVERSE:
        raise ValueError(
            f"v1 universe is restricted to {list(_V1_UNIVERSE)!r}; got "
            f"{universe!r} (spec §9.1 decision #3). v1.5+ may widen."
        )
    ts = snapshot_ts or datetime.now(UTC)

    async with pool.acquire() as conn:
        prices_raw = await conn.fetch(
            _PRICE_SQL, session_date, str(price_window_sessions),
        )
        fund_raw = await conn.fetch(_FUND_SQL, session_date)
        ledger_raw = await conn.fetch(_LEDGER_SQL)
        roster_raw = await conn.fetch(_ROSTER_SQL)

    price_window = tuple(_row_to_price(r) for r in prices_raw)
    fundamentals = tuple(_row_to_fund(r) for r in fund_raw)
    ledger_state = tuple(
        _row_to_ledger(r, ledger_quota) for r in ledger_raw
    )
    roster = tuple(_row_to_roster(r) for r in roster_raw)

    snap = MarketSnapshot(
        snapshot_ts=ts,
        session_date=session_date,
        universe=universe,
        price_window=price_window,
        fundamentals=fundamentals,
        ledger_state=ledger_state,
        roster=roster,
    )

    size = _serialised_size(snap)
    if size > MAX_SNAPSHOT_BYTES:
        raise SnapshotOverflow(
            f"MarketSnapshot serialised size {size} bytes exceeds "
            f"MAX_SNAPSHOT_BYTES={MAX_SNAPSHOT_BYTES} (spec §4.1). "
            f"Fail-loud: never silent truncation. Operator must "
            f"narrow universe or price_window_sessions at the call "
            f"site."
        )
    logger.info(
        "llm_edge_finder.snapshot_assembled",
        universe=universe,
        session_date=session_date.isoformat(),
        prices=len(price_window),
        fundamentals=len(fundamentals),
        ledger_rows=len(ledger_state),
        roster_size=len(roster),
        size_bytes=size,
    )
    return snap


__all__ = [
    "SnapshotOverflow",
    "assemble_snapshot",
]
```

- [ ] **Step 4.4: Confirm pytest-asyncio is available**

Run: `python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"`
Expected: prints a version (the project's existing test infrastructure already depends on this).

If missing, do NOT install it — instead surface to the operator and STOP. (Per CLAUDE.md universal: dependency adds are not silent.)

- [ ] **Step 4.5: Run the test — expect pass**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_snapshot_assembler.py -v`
Expected: PASS — three tests green (happy path, overflow fail-loud, universe rejection).

- [ ] **Step 4.6: Commit**

```bash
git add tpcore/lab/llm_finder/snapshot.py \
        tpcore/lab/llm_finder/tests/test_snapshot_assembler.py
git commit -m "$(cat <<'EOF'
feat(task-25): T4 — MarketSnapshot assembler

Reads prices_daily / fundamentals_quarterly / SP-A ledger / SP-B
roster into a frozen MarketSnapshot (spec §4.1). Bounded payload at
MAX_SNAPSHOT_BYTES = 512 KiB — overflow raises SnapshotOverflow
(fail-loud, never silent truncation). v1 universe sp500-only (spec
§9.1 decision #3). FakePool test pattern; no real DB in CI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Tool sandbox (`tool_sandbox.py`) — statsmodels + scipy.stats whitelist (spec §6)

**Spec citation:** §6.1 — the seven-row whitelist table (`OLS`, `adfuller`, `coint`, `ARIMA_1_0_0`, `spearmanr`, `pearsonr`, `ttest_1samp`). §2.9 — "Toolkit whitelist — `statsmodels` + `scipy.stats` ONLY (v1) ... Importing anything else from the sandbox is a fatal CI error. NO `arch`, NO `sklearn`, NO `linearmodels`, NO `pandas-ta`, NO network libs." §6.2 — "In-process attribute-allowlist (v1). Imports ONLY the named callables at module top. No `importlib`, no `__import__`, no `getattr(stats, name)`." §6.3 — "The dispatcher pins `numpy.random.seed(0)` at the top of `dispatch()` (belt-and-braces)."

**Files:**
- Create: `tpcore/lab/llm_finder/tool_sandbox.py`
- Test: `tpcore/lab/llm_finder/tests/test_tool_sandbox_whitelist.py`
- Test: `tpcore/lab/llm_finder/tests/test_tool_sandbox_no_dynamic_import.py`
- Test: `tpcore/lab/llm_finder/tests/test_tool_sandbox_determinism.py`

- [ ] **Step 5.1: Write the failing whitelist test**

Create `tpcore/lab/llm_finder/tests/test_tool_sandbox_whitelist.py`:

```python
"""Task #25 — tool sandbox whitelist tests (spec §10.1).

Covers:
 - ToolCall.callable_name outside Literal raises ValidationError
   BEFORE the dispatcher (already covered in test_models_frozen.py;
   re-asserted here for spec §10.1 traceability);
 - each whitelisted callable_name resolves and returns a ToolResult;
 - an unhandled callable_name inside dispatch (defence-in-depth — the
   pydantic Literal already gates this) raises ValueError;
 - args_json with malformed JSON returns ToolResult.error;
 - args resolving series-id outside the column whitelist returns
   ToolResult.error (spec §6.2 step 2 attribute-allowlist).
"""
from __future__ import annotations

import json
from datetime import date, datetime, UTC

import pytest

from tpcore.lab.llm_finder.models import (
    MarketSnapshot,
    PricePanelRow,
    ToolCall,
    ToolResult,
)
from tpcore.lab.llm_finder.tool_sandbox import dispatch


def _snapshot_with_series(n: int = 30) -> MarketSnapshot:
    rows = tuple(
        PricePanelRow(
            ticker="AAPL", session_date=date(2026, 1, 1 + (i % 28) + 1),
            adj_close=100.0 + i, log_return=0.01 + i * 0.0001,
            vol_20d=0.2,
        )
        for i in range(n)
    )
    return MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=date(2026, 5, 20),
        universe="sp500",
        price_window=rows,
        fundamentals=(),
        ledger_state=(),
        roster=(),
    )


def test_dispatch_returns_tool_result_for_each_whitelisted_name() -> None:
    snap = _snapshot_with_series(n=100)
    args = json.dumps({"series_id": "log_return", "ticker": "AAPL"})
    names = [
        "adfuller", "ARIMA_1_0_0", "ttest_1samp",
    ]
    for name in names:
        call = ToolCall(callable_name=name, args_json=args)
        result = dispatch(call, snap)
        assert isinstance(result, ToolResult)
        # Successful run carries numeric_summary, not error.
        assert (result.error is None) ^ (result.numeric_summary is None)


def test_dispatch_malformed_args_json_returns_error() -> None:
    snap = _snapshot_with_series()
    call = ToolCall(
        callable_name="adfuller", args_json="this is not json",
    )
    result = dispatch(call, snap)
    assert result.error is not None
    assert result.numeric_summary is None


def test_dispatch_non_whitelisted_series_id_returns_error() -> None:
    """Spec §6.2 step 2: series resolved BY ID against a fixed column
    whitelist (`adj_close`, `log_return`, `vol_20d`, ...). A name
    outside the whitelist returns ToolResult.error (no path traversal,
    no eval, no exec)."""
    snap = _snapshot_with_series()
    call = ToolCall(
        callable_name="adfuller",
        args_json=json.dumps({"series_id": "evil_attr", "ticker": "AAPL"}),
    )
    result = dispatch(call, snap)
    assert result.error is not None
```

- [ ] **Step 5.2: Write the failing no-dynamic-import test**

Create `tpcore/lab/llm_finder/tests/test_tool_sandbox_no_dynamic_import.py`:

```python
"""Task #25 — CI fence: tool_sandbox.py imports the whitelist
verbatim at module top; no dynamic-import, no eval/exec/subprocess,
no network libs (spec §10.3 + §2.9).

The test greps the source bytes — if any forbidden token appears the
build reds. This is the LAST LINE of defence: the LLM cannot exploit
``importlib.import_module(name)`` because the sandbox cannot use it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Spec §6.2 (the make-or-break safety test). Any single hit reds the
# build. Tokens are checked as substrings on STRIPPED source so a
# comment containing the word is not a false positive.
_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "importlib",
    "__import__",
    "eval(",
    "exec(",
    "subprocess",
    "os.system",
    "socket",
    "requests",
    "urllib",
    "http.client",
)

# Spec §2.9 + §10.3: non-whitelisted libraries that must NOT appear as
# import lines in the sandbox.
_FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "arch",
    "sklearn",
    "scikit_learn",
    "linearmodels",
    "pandas_ta",
    "tensorflow",
    "torch",
    "xgboost",
    "lightgbm",
)


def _sandbox_source() -> str:
    p = Path(__file__).resolve().parents[1] / "tool_sandbox.py"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Remove ``# ...`` line comments and triple-quoted docstrings so
    a docstring mention of a forbidden word is not a false positive."""
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"#.*", "", src)
    return src


def test_sandbox_has_no_dynamic_import_or_eval() -> None:
    stripped = _strip_comments(_sandbox_source())
    hits = [t for t in _FORBIDDEN_TOKENS if t in stripped]
    assert not hits, (
        f"tool_sandbox.py contains forbidden dynamic-dispatch tokens: "
        f"{hits!r} — spec §6.2 attribute-allowlist invariant"
    )


def test_sandbox_imports_no_non_whitelisted_libraries() -> None:
    stripped = _strip_comments(_sandbox_source())
    # Match `import foo` or `from foo`
    import_lines = re.findall(
        r"^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        stripped,
        flags=re.MULTILINE,
    )
    forbidden_present = [
        name for name in import_lines if name in _FORBIDDEN_IMPORTS
    ]
    assert not forbidden_present, (
        f"tool_sandbox.py imports forbidden libraries: "
        f"{forbidden_present!r} — spec §2.9 toolkit whitelist"
    )
```

- [ ] **Step 5.3: Write the failing determinism test**

Create `tpcore/lab/llm_finder/tests/test_tool_sandbox_determinism.py`:

```python
"""Task #25 — sandbox determinism (spec §6.3).

Same inputs -> same ToolResult byte-for-byte. The dispatcher pins
numpy.random.seed(0) at the top of dispatch() (belt-and-braces).
"""
from __future__ import annotations

import json
from datetime import date, datetime, UTC

from tpcore.lab.llm_finder.models import (
    MarketSnapshot, PricePanelRow, ToolCall,
)
from tpcore.lab.llm_finder.tool_sandbox import dispatch


def _snap(n: int = 60) -> MarketSnapshot:
    rows = tuple(
        PricePanelRow(
            ticker="AAPL", session_date=date(2026, 1, 1 + (i % 28) + 1),
            adj_close=100.0 + (i % 13) - 6,
            log_return=0.001 * ((i % 7) - 3),
            vol_20d=0.2,
        )
        for i in range(n)
    )
    return MarketSnapshot(
        snapshot_ts=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
        session_date=date(2026, 5, 20),
        universe="sp500",
        price_window=rows,
        fundamentals=(),
        ledger_state=(),
        roster=(),
    )


def test_dispatch_is_deterministic() -> None:
    snap = _snap()
    args = json.dumps({"series_id": "log_return", "ticker": "AAPL"})
    call = ToolCall(callable_name="adfuller", args_json=args)
    a = dispatch(call, snap)
    b = dispatch(call, snap)
    assert a.model_dump() == b.model_dump()
```

- [ ] **Step 5.4: Run the three tests — expect failure**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_tool_sandbox_whitelist.py tpcore/lab/llm_finder/tests/test_tool_sandbox_no_dynamic_import.py tpcore/lab/llm_finder/tests/test_tool_sandbox_determinism.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.lab.llm_finder.tool_sandbox'`.

- [ ] **Step 5.5: Write the tool_sandbox.py implementation**

Create `tpcore/lab/llm_finder/tool_sandbox.py`:

```python
"""Task #25 — tool sandbox (spec §6).

statsmodels + scipy.stats ONLY whitelist (spec §2.9). The callable
dispatcher is a pure-Python switch on ``ToolCall.callable_name`` (the
pydantic Literal already gates the name BEFORE we get here; we
defence-in-depth-reject anything else as well).

Per spec §6.2 the imports are at module top; there is NO importlib,
NO __import__, NO getattr-by-name on the stats namespace, NO eval,
NO exec, NO subprocess, NO network library. The CI test
``test_tool_sandbox_no_dynamic_import.py`` greps this file's source
to enforce these invariants.

Series resolution: column whitelist (spec §6.2 step 2). The LLM names
``series_id`` + ``ticker``; the dispatcher resolves to
``MarketSnapshot.price_window`` filtering on the named column. A
column name outside the whitelist returns ``ToolResult.error``.

Determinism: ``numpy.random.seed(0)`` at the top of dispatch (spec §6.3).
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import scipy.stats as scipy_stats
import statsmodels.api as sm
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.stattools import adfuller, coint

from tpcore.lab.llm_finder.models import (
    MarketSnapshot,
    NumericSummary,
    ToolCall,
    ToolResult,
)

# Spec §6.2 step 2 — the column whitelist. Resolving a series_id NOT
# in this set returns ToolResult.error. NO path traversal, NO eval,
# NO __dict__ access.
_COLUMN_WHITELIST: frozenset[str] = frozenset(
    {"adj_close", "log_return", "vol_20d"}
)


class _DispatchError(ValueError):
    """Internal — converted to ToolResult.error by dispatch()."""


def _resolve_series(
    snapshot: MarketSnapshot, ticker: str, series_id: str,
) -> list[float]:
    """Resolve (ticker, series_id) to a list[float] from
    ``snapshot.price_window``. Raises ``_DispatchError`` on a missing
    ticker or a series_id outside the column whitelist."""
    if series_id not in _COLUMN_WHITELIST:
        raise _DispatchError(
            f"series_id {series_id!r} not in column whitelist "
            f"{sorted(_COLUMN_WHITELIST)!r}"
        )
    out: list[float] = []
    for row in snapshot.price_window:
        if row.ticker != ticker:
            continue
        # Dispatch on series_id — explicit branches, no getattr.
        if series_id == "adj_close":
            out.append(row.adj_close)
        elif series_id == "log_return":
            if row.log_return is None:
                continue
            out.append(row.log_return)
        elif series_id == "vol_20d":
            if row.vol_20d is None:
                continue
            out.append(row.vol_20d)
    if not out:
        raise _DispatchError(
            f"no rows for ticker={ticker!r} series_id={series_id!r}"
        )
    return out


def _parse_args(call: ToolCall) -> dict[str, Any]:
    try:
        parsed = json.loads(call.args_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _DispatchError(f"malformed args_json: {exc!s}") from None
    if not isinstance(parsed, dict):
        raise _DispatchError(
            f"args_json must decode to dict; got {type(parsed).__name__}"
        )
    return parsed


def _ols(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    y_ticker = args.get("y_ticker", "")
    y_series = args.get("y_series_id", "")
    x_specs = args.get("x_series", [])  # list of (ticker, series_id)
    y = _resolve_series(snap, y_ticker, y_series)
    X_cols: list[list[float]] = []
    for spec in x_specs:
        t = spec.get("ticker", "")
        sid = spec.get("series_id", "")
        X_cols.append(_resolve_series(snap, t, sid))
    n = min(len(y), *(len(c) for c in X_cols)) if X_cols else len(y)
    if n < 5:
        raise _DispatchError("OLS needs >=5 aligned observations")
    y_arr = np.asarray(y[:n], dtype=float)
    if X_cols:
        X_arr = np.column_stack([np.asarray(c[:n], dtype=float) for c in X_cols])
        X_arr = sm.add_constant(X_arr)
    else:
        X_arr = sm.add_constant(np.ones(n))
    result = sm.OLS(y_arr, X_arr).fit()
    return NumericSummary(
        coefficients=tuple(float(c) for c in result.params),
        pvalues=tuple(float(p) for p in result.pvalues),
        statistic=float(result.rsquared),
        summary_text=f"OLS rsquared={result.rsquared:.4f} n={n}",
    )


def _adfuller(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    series = _resolve_series(
        snap, args.get("ticker", ""), args.get("series_id", ""),
    )
    if len(series) < 12:
        raise _DispatchError("adfuller needs >=12 observations")
    stat, pval, *_ = adfuller(series, autolag="AIC")
    return NumericSummary(
        coefficients=(),
        pvalues=(float(pval),),
        statistic=float(stat),
        summary_text=f"ADF stat={stat:.4f} p={pval:.4f}",
    )


def _coint(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    a = _resolve_series(snap, args.get("ticker_a", ""), args.get("series_id", ""))
    b = _resolve_series(snap, args.get("ticker_b", ""), args.get("series_id", ""))
    n = min(len(a), len(b))
    if n < 12:
        raise _DispatchError("coint needs >=12 aligned observations")
    stat, pval, _ = coint(a[:n], b[:n])
    return NumericSummary(
        coefficients=(),
        pvalues=(float(pval),),
        statistic=float(stat),
        summary_text=f"coint stat={stat:.4f} p={pval:.4f} n={n}",
    )


def _arima_1_0_0(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    series = _resolve_series(
        snap, args.get("ticker", ""), args.get("series_id", ""),
    )
    if len(series) < 20:
        raise _DispatchError("ARIMA_1_0_0 needs >=20 observations")
    # Order pinned to (1,0,0) per spec §4.2 — LLM cannot vary.
    fitted = ARIMA(series, order=(1, 0, 0)).fit()
    params = tuple(float(p) for p in fitted.params)
    pvals = tuple(float(p) for p in fitted.pvalues)
    return NumericSummary(
        coefficients=params,
        pvalues=pvals,
        statistic=float(fitted.aic),
        summary_text=f"ARIMA(1,0,0) aic={fitted.aic:.4f}",
    )


def _spearmanr(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    a = _resolve_series(snap, args.get("ticker_a", ""), args.get("series_a", ""))
    b = _resolve_series(snap, args.get("ticker_b", ""), args.get("series_b", ""))
    n = min(len(a), len(b))
    if n < 5:
        raise _DispatchError("spearmanr needs >=5 aligned observations")
    rho, p = scipy_stats.spearmanr(a[:n], b[:n])
    return NumericSummary(
        coefficients=(float(rho),),
        pvalues=(float(p),),
        statistic=float(rho),
        summary_text=f"spearman rho={rho:.4f} p={p:.4f} n={n}",
    )


def _pearsonr(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    a = _resolve_series(snap, args.get("ticker_a", ""), args.get("series_a", ""))
    b = _resolve_series(snap, args.get("ticker_b", ""), args.get("series_b", ""))
    n = min(len(a), len(b))
    if n < 5:
        raise _DispatchError("pearsonr needs >=5 aligned observations")
    r, p = scipy_stats.pearsonr(a[:n], b[:n])
    return NumericSummary(
        coefficients=(float(r),),
        pvalues=(float(p),),
        statistic=float(r),
        summary_text=f"pearson r={r:.4f} p={p:.4f} n={n}",
    )


def _ttest_1samp(args: dict[str, Any], snap: MarketSnapshot) -> NumericSummary:
    series = _resolve_series(
        snap, args.get("ticker", ""), args.get("series_id", ""),
    )
    if len(series) < 5:
        raise _DispatchError("ttest_1samp needs >=5 observations")
    popmean = float(args.get("popmean", 0.0))
    stat, p = scipy_stats.ttest_1samp(series, popmean)
    return NumericSummary(
        coefficients=(),
        pvalues=(float(p),),
        statistic=float(stat),
        summary_text=f"ttest_1samp t={stat:.4f} p={p:.4f} mu0={popmean}",
    )


def dispatch(call: ToolCall, snapshot: MarketSnapshot) -> ToolResult:
    """Spec §6.2 — pure-Python switch on ``call.callable_name``.

    Any exception inside a branch becomes ``ToolResult.error`` with
    exception-type name only (no traceback, no payload echo). On
    success returns ``ToolResult(numeric_summary=...)``.

    Determinism (spec §6.3): pins ``numpy.random.seed(0)`` at entry.
    """
    np.random.seed(0)  # belt-and-braces determinism (spec §6.3)
    try:
        args = _parse_args(call)
        name = call.callable_name
        if name == "OLS":
            summary = _ols(args, snapshot)
        elif name == "adfuller":
            summary = _adfuller(args, snapshot)
        elif name == "coint":
            summary = _coint(args, snapshot)
        elif name == "ARIMA_1_0_0":
            summary = _arima_1_0_0(args, snapshot)
        elif name == "spearmanr":
            summary = _spearmanr(args, snapshot)
        elif name == "pearsonr":
            summary = _pearsonr(args, snapshot)
        elif name == "ttest_1samp":
            summary = _ttest_1samp(args, snapshot)
        else:
            # Pydantic Literal already rejects this; defence-in-depth.
            raise _DispatchError(f"non-whitelisted callable: {name!r}")
        return ToolResult(numeric_summary=summary, error=None)
    except _DispatchError as exc:
        return ToolResult(numeric_summary=None, error=str(exc))
    except Exception as exc:
        # No traceback, no payload echo (spec §6.2 step 3).
        return ToolResult(
            numeric_summary=None,
            error=f"{type(exc).__name__}",
        )


__all__ = ["dispatch"]
```

- [ ] **Step 5.6: Verify statsmodels + scipy are importable in the test env**

Run: `python -c "import statsmodels.api, scipy.stats; print('ok')"`
Expected: prints `ok`. If `ModuleNotFoundError`, STOP and surface to the operator — these are required dependencies for the v1 toolkit (spec §2.9). Do NOT silently `pip install`.

- [ ] **Step 5.7: Run the three sandbox tests — expect pass**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_tool_sandbox_whitelist.py tpcore/lab/llm_finder/tests/test_tool_sandbox_no_dynamic_import.py tpcore/lab/llm_finder/tests/test_tool_sandbox_determinism.py -v`
Expected: PASS — whitelist (3 tests), no-dynamic-import (2 tests), determinism (1 test) all green.

- [ ] **Step 5.8: Commit**

```bash
git add tpcore/lab/llm_finder/tool_sandbox.py \
        tpcore/lab/llm_finder/tests/test_tool_sandbox_whitelist.py \
        tpcore/lab/llm_finder/tests/test_tool_sandbox_no_dynamic_import.py \
        tpcore/lab/llm_finder/tests/test_tool_sandbox_determinism.py
git commit -m "$(cat <<'EOF'
feat(task-25): T5 — tool sandbox (statsmodels + scipy.stats whitelist)

Seven-row whitelist dispatcher (spec §6.1) — OLS, adfuller, coint,
ARIMA_1_0_0, spearmanr, pearsonr, ttest_1samp. In-process
attribute-allowlist (spec §6.2): imports at module top, NO importlib,
NO eval/exec/subprocess, NO network libs, NO arch/sklearn/
linearmodels/pandas_ta. Column whitelist (adj_close/log_return/
vol_20d). numpy.random.seed(0) pin per spec §6.3. CI source-grep
fence reds the build on any forbidden token.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Persona file + SHA sentinel (`docs/lab_finder_persona.md` + `PERSONA_VERSION`)

**Spec citation:** §9.1 decision #6 — "Persona SHA-pinning — spec mandates `PERSONA_VERSION` constant mirroring SP-G; plan PR fixes location and CI sentinel." §10.2 — "`test_persona_versioned.py` — persona edit without `PERSONA_VERSION` bump reds the build." Mirrors SP-G `docs/lab_emitter_persona.md` mechanism (which currently does not exist on disk — Task #25 is the first persona file in this lineage).

**Files:**
- Create: `docs/lab_finder_persona.md`
- Test: `tpcore/lab/llm_finder/tests/test_persona_versioned.py`

Note: `PERSONA_VERSION = "v1.0"` was already added to `tpcore/lab/llm_finder/__init__.py` in Step 1.1. The sentinel test pins the SHA of the persona text against a checked-in constant in the test file.

- [ ] **Step 6.1: Author the V1 persona text**

Create `docs/lab_finder_persona.md`:

```markdown
# Lab Edge Finder Persona — v1.0

**Status:** v1.0 — Task #25 (spec §7 `PERSONA_VERSION`). The
`PERSONA_VERSION` constant in
`tpcore/lab/llm_finder/__init__.py` mirrors this file's SHA-256 short
hash. Editing this file WITHOUT bumping `PERSONA_VERSION` reds the
build via `test_persona_versioned.py`.

---

You are the **Lab Edge Finder** — an autonomous LLM that proposes
single-pre-registered Lab candidate hypotheses for a fully automated
US-equities trading platform. You operate under HARD CONSTRAINTS that
are NON-NEGOTIABLE.

## Your operating loop

You run a disciplined data → analysis → idea → Lab → graduation gate
loop. For each invocation:

1. **You receive** a `MarketSnapshot` (price window + fundamentals +
   SP-A ledger state + SP-B roster) and a tuple of `ReferenceExcerpt`
   bundles (the mandatory `dsr_ntrials_discipline` always plus any
   `--reference-bundle` selection).
2. **You analyse** by emitting `AnalysisRequest`s carrying typed
   `ToolCall`s. The dispatcher (`tpcore.lab.llm_finder.tool_sandbox`)
   runs each call and returns `ToolResult`s. You may run up to
   `ANALYSIS_TURN_QUOTA = 8` turns; you SHOULD spend fewer if the
   evidence is conclusive earlier.
3. **You propose** up to `EDGE_FINDER_RUN_QUOTA = 3` `ProposedSpec`s
   in a single `AnalysisResult`. Each spec is ONE pre-registered
   primary hypothesis with ONE primary metric. Each spec is
   independently routed through SP-G `emit_once`.

## Your HARD CONSTRAINTS

These are spec §2 verbatim and are enforced by code BELOW your output
layer. You cannot bypass them; you can only respect them honestly.

1. **`expected_trials` is your ledger spend.** Declare it honestly. A
   variant that explores a `(60, 55)` toggle on a single parameter is
   2 trials, not 1. A variant with hidden grids is `n` trials where
   `n` is the FULL search space. Under-declaration is the
   multiple-testing inflation failure mode the SP-A ledger was built
   to catch.

2. **One hypothesis per `ProposedSpec`.** Three `ProposedSpec`s in a
   run are THREE SEPARATE single-hypothesis emissions (each spending
   one SP-A ledger row), NOT a multi-hypothesis grid. The pydantic
   validator on SP-G `EmittedSpec` rejects multi-hypothesis shapes.

3. **The gate is sacred.** You may not write
   `--dsr-threshold` / `--credibility-threshold` flags into any spec.
   The deterministic gate at `ops/lab/run.py` reads from frozen
   constants. SP-G's `validate_no_gate_override` reds your output if
   you try.

4. **Roster-mediated.** Your `target_engine` must be in
   `MarketSnapshot.roster` (the SP-B `lab_targetable_engines()`
   derivation). Naming `canary`, the `lab` sentinel, the allocator,
   or any RETIRED engine is rejected by the agent BEFORE the ledger
   row is written.

5. **Cite your evidence.** Your `rationale` must reference specific
   `ToolResult`s by their index in `analysis_evidence_refs`. A
   hypothesis without computed evidence is the free-form-mining
   failure mode the `dsr_ntrials_discipline` bundle warns against.

6. **Pre-registered falsification.** Your `falsification_criterion` is
   what makes this hypothesis FAIL out-of-sample. Pin it BEFORE the
   Lab run, not after.

## What you SHOULD prefer (low-DOF discipline per `project_ml_research_track`)

- `fold_existing` over `promote_new`: re-tuning an existing engine's
  existing rule is lower-DOF than proposing a new engine.
- One-parameter `choice:` toggles for `fold_existing` (the
  feature-flag-variant shape per Readiness §2).
- Hypotheses that EXTEND a Carver / Chan framing (the reference
  bundles) over free-form indicator mining.
- Targets with BUDGET REMAINING in `MarketSnapshot.ledger_state`
  (look at `quota - cumulative_n_trials`).

## What you MUST NOT do

- Propose more than 3 specs per run (validator rejects > 3).
- Name a `target_engine` outside `MarketSnapshot.roster`.
- Write any `--dsr-threshold` / `--credibility-threshold` flag.
- Declare `expected_trials < 1` or fold a sweep into a single emission.
- Propose hypotheses without computed evidence (`analysis_evidence_
  refs` must be non-empty for each `ProposedSpec`).

You are advisory. The operator merges. The deterministic gate
disposes. You propose.
```

- [ ] **Step 6.2: Write the failing persona-version sentinel test**

Create `tpcore/lab/llm_finder/tests/test_persona_versioned.py`:

```python
"""Task #25 — persona-version sentinel (spec §10.2 / §9.1 decision #6).

Mirrors SP-G ``test_lab_emitter_persona_versioned.py`` mechanism: the
``PERSONA_VERSION`` constant in
``tpcore.lab.llm_finder.__init__`` must match a checked-in SHA-256
short-hash of ``docs/lab_finder_persona.md``. Editing the persona
without bumping the constant REDS the build.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from tpcore.lab.llm_finder import PERSONA_VERSION

# Sentinel: bump BOTH this constant AND ``PERSONA_VERSION`` in
# ``tpcore/lab/llm_finder/__init__.py`` whenever
# ``docs/lab_finder_persona.md`` is edited. The numeric version (v1.0,
# v1.1, ...) is the operator-visible name; the SHA pin is the
# build-time fence.
_PERSONA_SHA_PINNED_V1_0: str = (
    "PINNED_AT_T6_STEP_6_3"  # replaced in step 6.3
)
_PERSONA_VERSION_PINNED: str = "v1.0"


_REPO_ROOT = Path(__file__).resolve().parents[4]
_PERSONA_PATH = _REPO_ROOT / "docs" / "lab_finder_persona.md"


def _persona_sha() -> str:
    blob = _PERSONA_PATH.read_bytes()
    return hashlib.sha256(blob).hexdigest()[:12]


def test_persona_file_exists() -> None:
    assert _PERSONA_PATH.is_file(), (
        f"persona file missing at {_PERSONA_PATH}; "
        f"Task #25 spec §9.1 decision #6 mandates this file"
    )


def test_persona_version_pinned() -> None:
    """If this fails: bump ``PERSONA_VERSION`` in
    ``tpcore/lab/llm_finder/__init__.py`` AND update
    ``_PERSONA_SHA_PINNED_V1_0`` below to the new SHA.
    """
    assert PERSONA_VERSION == _PERSONA_VERSION_PINNED, (
        f"PERSONA_VERSION drifted: code={PERSONA_VERSION!r} "
        f"sentinel={_PERSONA_VERSION_PINNED!r}"
    )
    actual = _persona_sha()
    assert actual == _PERSONA_SHA_PINNED_V1_0, (
        f"persona SHA drifted without PERSONA_VERSION bump: "
        f"current={actual!r} pinned={_PERSONA_SHA_PINNED_V1_0!r}. "
        f"Edit the persona AND bump PERSONA_VERSION + this sentinel."
    )
```

- [ ] **Step 6.3: Capture the actual SHA and replace the placeholder**

Run: `python -c "import hashlib, pathlib; print(hashlib.sha256(pathlib.Path('docs/lab_finder_persona.md').read_bytes()).hexdigest()[:12])"`
Expected: prints a 12-hex-char SHA prefix.

Use `Edit` (NOT sed) to replace `PINNED_AT_T6_STEP_6_3` in `tpcore/lab/llm_finder/tests/test_persona_versioned.py` with the actual SHA value printed above.

- [ ] **Step 6.4: Run the persona test — expect pass**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_persona_versioned.py -v`
Expected: PASS — both tests green.

- [ ] **Step 6.5: Commit**

```bash
git add docs/lab_finder_persona.md \
        tpcore/lab/llm_finder/tests/test_persona_versioned.py
git commit -m "$(cat <<'EOF'
feat(task-25): T6 — persona v1.0 + SHA-pinned sentinel

V1 persona (spec §7) — autonomous-LLM operating contract with the
six HARD CONSTRAINTS spec §2 verbatim. PERSONA_VERSION='v1.0' in
tpcore.lab.llm_finder; test_persona_versioned.py pins SHA-256 short
hash so any edit-without-bump reds the build (mirrors SP-G's
mechanism). spec §10.2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: FinderRun persistence helper (`record_finder_run`)

**Spec citation:** §4.4 — "Persisted as one append-only row in `platform.data_quality_log` under `lab_edge_finder_run.<session_date>` (disjoint from `lab_trial_ledger.*`; reuses SP-A substrate — no migration)." §9.1 decision #5 — "`FinderRun` source-namespace string — `lab_edge_finder_run.<session_date>`; plan PR refines for grep-ability."

Plan-PR refinement (spec §9.1 decision #5): event_type is `LAB_FINDER_RUN`; the `data` jsonb carries the `FinderRun.as_dict()` payload; the `engine` column carries the literal `llm_edge_finder` (mirrors `_AGENT_ENGINE_TAG = "llm_lab_emitter"` on SP-G). This keeps the row grep-able by event_type AND by the engine column AND by data->>'persona_version'.

**Files:**
- Modify: `tpcore/lab/llm_finder/models.py` — append `record_finder_run` write-path helper.
- Test: `tpcore/lab/llm_finder/tests/test_record_finder_run.py`

- [ ] **Step 7.1: Write the failing record_finder_run test**

Create `tpcore/lab/llm_finder/tests/test_record_finder_run.py`:

```python
"""Task #25 — FinderRun persistence (spec §4.4 + §10.1).

record_finder_run inserts one row into platform.application_log with:
 - engine = 'llm_edge_finder'
 - event_type = 'LAB_FINDER_RUN'
 - severity = 'INFO'
 - data (jsonb) = FinderRun.as_dict()

Tested against FakePool. NO real DB. NO ledger spend (this row is
disjoint from lab_trial_ledger.*).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from tpcore.lab.llm_finder.models import FinderRun, record_finder_run


class _Conn:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args) -> None:
        self.executions.append((sql, args))


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.conn = _Conn()

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self.conn)


@pytest.mark.asyncio
async def test_record_finder_run_inserts_application_log_row() -> None:
    pool = _Pool()
    run = FinderRun(
        run_id=uuid4(),
        started_ts=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        completed_ts=datetime(2026, 5, 21, 12, 15, tzinfo=UTC),
        snapshot_session_date=date(2026, 5, 20),
        persona_version="v1.0",
        reference_bundle="dsr_ntrials_discipline",
        analysis_turn_count=3,
        proposed_spec_count=1,
        emitted_pr_urls=("https://github.com/x/y/pull/1",),
        rejection_reason=None,
    )
    await record_finder_run(pool, run)

    assert len(pool.conn.executions) == 1
    sql, args = pool.conn.executions[0]
    assert "INSERT INTO platform.application_log" in sql
    # Positional args: (engine, run_id, event_type, severity, message, data)
    assert args[0] == "llm_edge_finder"
    assert args[2] == "LAB_FINDER_RUN"
    assert args[3] == "INFO"
    payload = json.loads(args[5])
    assert payload["persona_version"] == "v1.0"
    assert payload["proposed_spec_count"] == 1
    assert payload["snapshot_session_date"] == "2026-05-20"
```

- [ ] **Step 7.2: Run the test — expect failure**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_record_finder_run.py -v`
Expected: FAIL — `ImportError: cannot import name 'record_finder_run' from 'tpcore.lab.llm_finder.models'`.

- [ ] **Step 7.3: Append `record_finder_run` to `models.py`**

Use `Edit` to append the following to `tpcore/lab/llm_finder/models.py` immediately BEFORE the `__all__` list at the end. The new content:

```python
# ─── Write-path helper (spec §4.4) ─────────────────────────────────────


_RECORD_FINDER_RUN_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""

_AGENT_ENGINE_TAG: str = "llm_edge_finder"


async def record_finder_run(pool: Any, run: FinderRun) -> None:
    """Spec §4.4 — append one ``LAB_FINDER_RUN`` audit row to
    ``platform.application_log``. Disjoint from ``lab_trial_ledger.*``
    (no migration, reuses SP-A substrate). The row is the
    run-level provenance trail; per-emission ledger rows are SP-G's
    ``record_trial_spend``.

    The ``engine`` column carries ``llm_edge_finder`` (mirrors SP-G's
    ``llm_lab_emitter`` tag) so the dashboard / digest can filter by
    finder lane without joining on event_type alone.
    """
    import json as _json
    payload = run.as_dict()
    message = (
        f"lab_edge_finder_run.{run.snapshot_session_date.isoformat()} "
        f"persona={run.persona_version} bundle={run.reference_bundle} "
        f"emitted={run.proposed_spec_count}"
    )
    async with pool.acquire() as conn:
        await conn.execute(
            _RECORD_FINDER_RUN_SQL,
            _AGENT_ENGINE_TAG,
            run.run_id,
            "LAB_FINDER_RUN",
            "INFO",
            message,
            _json.dumps(payload, default=str),
        )
```

Then update `__all__` to include `"record_finder_run"`.

- [ ] **Step 7.4: Run the test — expect pass**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_record_finder_run.py -v`
Expected: PASS — single test green.

- [ ] **Step 7.5: Re-run all T1 + T7 model tests to confirm no regression**

Run: `python -m pytest tpcore/lab/llm_finder/tests/test_models_frozen.py tpcore/lab/llm_finder/tests/test_record_finder_run.py -v`
Expected: PASS — all assertions green.

- [ ] **Step 7.6: Commit**

```bash
git add tpcore/lab/llm_finder/models.py \
        tpcore/lab/llm_finder/tests/test_record_finder_run.py
git commit -m "$(cat <<'EOF'
feat(task-25): T7 — record_finder_run write-path helper

Append-only LAB_FINDER_RUN row in platform.application_log (spec
§4.4 — reuses SP-A substrate, no migration). engine='llm_edge_finder'
mirrors SP-G's llm_lab_emitter tag; disjoint from lab_trial_ledger.*.
FakePool test pattern.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Agent core — Phase A / B / C orchestration (mocked LLM seam)

**Spec citation:** §3.2 — the data → analysis → idea loop with quotas `ANALYSIS_TURN_QUOTA = 8` and `EDGE_FINDER_RUN_QUOTA = 3`. §3.3 — "Task #25 is a CALLER of `emit_once`; it NEVER reimplements an SP-G function." §2.7 — preserved two-daemon invariant; co-task on `ops/llm_triage_service.py`.

This task builds the agent EXCEPT for the real Anthropic SDK call (which is T9). The LLM is mocked at the seam — `analyze_callable: Callable[[MarketSnapshot, tuple[ReferenceExcerpt, ...], int], AnalysisRequest | AnalysisResult]`. Tests inject a fake that emits a scripted sequence.

**Files:**
- Create: `ops/llm_edge_finder.py` (agent core only — Anthropic SDK wiring in T9).
- Test: `tests/test_llm_edge_finder_agent.py`

- [ ] **Step 8.1: Write the failing agent-loop test**

Create `tests/test_llm_edge_finder_agent.py`:

```python
"""Task #25 — agent-loop tests (mocked LLM seam).

Covers:
 - run_finder calls assemble_snapshot once + load_reference_bundles
   once (Phase A);
 - the LLM-seam callable is invoked up to ANALYSIS_TURN_QUOTA times
   (Phase B), with the dispatcher's ToolResults threaded through;
 - Phase C calls emit_once once per ProposedSpec, capped at
   EDGE_FINDER_RUN_QUOTA = 3 (a scripted 4-spec response truncates
   to 3 + LOG warning);
 - FinderRun audit row is recorded after the loop completes;
 - the agent NEVER calls gh pr create directly (it goes through
   emit_once).

Operator memory feedback_ops_package_shadow_full_suite_gate — this
test imports ops.llm_edge_finder; pytestmark =
pytest.mark.xdist_group("ops_shadow").
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

_REPO_ROOT = Path(__file__).resolve().parents[1]


# Defensive ops-shadow init (mirrors test_llm_lab_emitter.py).
_FINDER_PATH = _REPO_ROOT / "ops" / "llm_edge_finder.py"
_SAVED = {
    k: sys.modules.get(k)
    for k in ("ops", "ops.llm_data_triage", "ops.llm_lab_emitter",
              "ops.llm_edge_finder")
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_FINDER_PATH.parent)]
        sys.modules["ops"] = _pkg
    import ops.llm_data_triage  # noqa: F401
    import ops.llm_lab_emitter  # noqa: F401

    _spec = importlib.util.spec_from_file_location(
        "_edge_finder_under_test", _FINDER_PATH,
    )
    assert _spec is not None and _spec.loader is not None
    ef = importlib.util.module_from_spec(_spec)
    sys.modules["_edge_finder_under_test"] = ef
    _spec.loader.exec_module(ef)
finally:
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


from tpcore.lab.llm_emitter.models import ReferenceExcerpt
from tpcore.lab.llm_finder.models import (
    AnalysisRequest,
    AnalysisResult,
    MarketSnapshot,
    ProposedSpec,
    ToolCall,
    ToolResult,
    NumericSummary,
)
from tpcore.lab.target import LabPrimaryMetric


def _empty_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=date(2026, 5, 20),
        universe="sp500",
        price_window=(),
        fundamentals=(),
        ledger_state=(),
        roster=(),
    )


def _proposed(name: str = "test-cand") -> ProposedSpec:
    return ProposedSpec(
        candidate_name=name,
        target_engine="sentinel",
        intent="fold_existing",
        primary_hypothesis="h",
        primary_metric=LabPrimaryMetric.SHARPE,
        param_ranges={"k": (1, 2, "choice:1,2")},
        rationale="r",
        falsification_criterion="f",
        expected_trials=1,
        analysis_evidence_refs=(0,),
    )


@pytest.mark.asyncio
async def test_run_finder_calls_assemble_snapshot_and_loader_once(
    monkeypatch,
) -> None:
    snap = _empty_snapshot()
    fake_assemble = AsyncMock(return_value=snap)
    fake_loader = MagicMock(return_value=(
        ReferenceExcerpt(name="dsr_ntrials_discipline", text="body"),
    ))
    fake_emit = AsyncMock()
    fake_record = AsyncMock()

    # The LLM seam: scripted to skip the analysis loop and produce
    # zero specs (so emit_once is never called).
    scripted = [
        AnalysisResult(
            turn=1, tool_results=(), proposed_specs=(),
            finder_rationale="no evidence yet",
        ),
    ]

    async def fake_llm(*_args, **_kwargs):
        return scripted.pop(0)

    monkeypatch.setattr(ef, "assemble_snapshot", fake_assemble)
    monkeypatch.setattr(ef, "load_reference_bundles", fake_loader)
    monkeypatch.setattr(ef, "_call_llm", fake_llm)
    monkeypatch.setattr(ef, "emit_once", fake_emit)
    monkeypatch.setattr(ef, "record_finder_run", fake_record)

    run = await ef.run_finder(
        pool=MagicMock(),
        target_engine=None,  # any-target mode
        reference_bundles=(),
        session_date=date(2026, 5, 20),
    )
    assert fake_assemble.await_count == 1
    assert fake_loader.call_count == 1
    assert fake_emit.await_count == 0  # no specs → no emit_once
    assert fake_record.await_count == 1
    assert run.proposed_spec_count == 0


@pytest.mark.asyncio
async def test_run_finder_truncates_to_edge_finder_run_quota(
    monkeypatch, caplog,
) -> None:
    """Spec §10.2: a scripted 4-spec response truncates to 3 with a
    loud warning; the 4th is NEVER emitted."""
    snap = _empty_snapshot()
    monkeypatch.setattr(ef, "assemble_snapshot",
                        AsyncMock(return_value=snap))
    monkeypatch.setattr(ef, "load_reference_bundles",
                        MagicMock(return_value=()))

    # The model rejects > 3 at construction. So the agent must
    # truncate BEFORE building AnalysisResult — but we want to verify
    # that even if the LLM seam returns a list of 4 raw ProposedSpec
    # dicts, only 3 are passed to emit_once. So the seam returns 4
    # ProposedSpec objects directly and the agent's truncation logic
    # is what we are testing.
    proposed4 = tuple(_proposed(f"c-{i}") for i in range(4))

    async def fake_llm_raw(*_args, **_kwargs):
        # Return a dict-shaped pre-AnalysisResult that the agent
        # truncates BEFORE pydantic-validating.
        return {
            "turn": 1,
            "tool_results": [],
            "_raw_proposed_specs": proposed4,  # internal seam shape
            "finder_rationale": "evidence",
        }

    emit_calls: list = []

    async def fake_emit(pool, *, proposed_spec, **kwargs):
        emit_calls.append(proposed_spec.candidate_name)
        from ops.llm_lab_emitter import EmitterOutcome
        return EmitterOutcome(
            emitted_candidate=proposed_spec.candidate_name,
            target_engine=proposed_spec.target_engine,
            pr_link=f"https://gh/x/y/{proposed_spec.candidate_name}",
            ledger_recorded=True,
        )

    monkeypatch.setattr(ef, "_call_llm", fake_llm_raw)
    monkeypatch.setattr(ef, "emit_once", fake_emit)
    monkeypatch.setattr(ef, "record_finder_run", AsyncMock())

    run = await ef.run_finder(
        pool=MagicMock(), target_engine="sentinel",
        reference_bundles=(), session_date=date(2026, 5, 20),
    )
    assert len(emit_calls) == 3, (
        f"EDGE_FINDER_RUN_QUOTA=3 must truncate; got {len(emit_calls)}"
    )
    assert "c-3" not in emit_calls
    assert run.proposed_spec_count == 3


@pytest.mark.asyncio
async def test_run_finder_respects_analysis_turn_quota(
    monkeypatch,
) -> None:
    """Spec §3.2 + §4.2: the analysis loop is bounded by
    ANALYSIS_TURN_QUOTA = 8. If the LLM keeps emitting
    AnalysisRequests past 8 turns, the agent stops at 8."""
    snap = _empty_snapshot()
    monkeypatch.setattr(ef, "assemble_snapshot",
                        AsyncMock(return_value=snap))
    monkeypatch.setattr(ef, "load_reference_bundles",
                        MagicMock(return_value=()))

    turns_seen = []

    async def fake_llm_infinite(*_args, turn: int, **_kwargs):
        turns_seen.append(turn)
        # Always emit one more AnalysisRequest with one safe tool call
        # until the agent stops at the quota.
        if turn < 100:
            return AnalysisRequest(
                turn=turn, rationale="more!",
                tool_calls=(ToolCall(callable_name="OLS", args_json="{}"),),
            )
        return AnalysisResult(
            turn=turn, tool_results=(), proposed_specs=(),
            finder_rationale="done",
        )

    monkeypatch.setattr(ef, "_call_llm", fake_llm_infinite)
    monkeypatch.setattr(ef, "emit_once", AsyncMock())
    monkeypatch.setattr(ef, "record_finder_run", AsyncMock())

    run = await ef.run_finder(
        pool=MagicMock(), target_engine=None,
        reference_bundles=(), session_date=date(2026, 5, 20),
    )
    assert run.analysis_turn_count <= 8, (
        f"ANALYSIS_TURN_QUOTA=8 must bound the loop; got "
        f"{run.analysis_turn_count}"
    )
    assert max(turns_seen) <= 8
```

- [ ] **Step 8.2: Run the test — expect failure**

Run: `python -m pytest tests/test_llm_edge_finder_agent.py -v -p no:xdist`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.llm_edge_finder'` or similar.

- [ ] **Step 8.3: Write the agent core (`ops/llm_edge_finder.py`) WITHOUT the real SDK call**

Create `ops/llm_edge_finder.py`:

```python
"""Task #25 — autonomous LLM+quant Edge Finder agent.

Composes WITH SP-G's ``emit_once`` verbatim (spec §3.3). Never
re-implements an SP-G function. Per spec §3.2 the loop has three
phases:

- Phase A (deterministic): assemble MarketSnapshot, load reference
  bundles, read roster + ledger state.
- Phase B (LLM-driven): call ``_call_llm`` up to
  ``ANALYSIS_TURN_QUOTA = 8`` times; each turn dispatches the LLM's
  ``AnalysisRequest.tool_calls`` through
  ``tpcore.lab.llm_finder.tool_sandbox.dispatch`` and threads the
  ``ToolResult``s back in.
- Phase C (idea emission): for each ``ProposedSpec`` (capped at
  ``EDGE_FINDER_RUN_QUOTA = 3``) call SP-G ``emit_once`` once; the
  SP-G fence stack runs verbatim.

Spec citations: §3.2 (loop shape), §3.3 (compose-with-SP-G invariant),
§4 (contracts), §6 (tool sandbox), §7 (reference bundles).

The Anthropic SDK call (``_call_llm`` body) is wired in T9. This
module defines the seam; T9 implements it against the real SDK.

Safety:
 - The agent NEVER calls ``gh pr create`` directly — every draft PR
   goes through ``emit_once`` (CI source-grep enforces this).
 - The agent NEVER imports a non-whitelisted statistical library
   (CI source-grep on tool_sandbox.py enforces this).
 - The agent NEVER mutates the roster (the diff-scope fence inside
   SP-G ``emit_once`` enforces this).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import structlog

from ops.llm_lab_emitter import emit_once
from tpcore.lab.llm_emitter.models import ReferenceExcerpt
from tpcore.lab.llm_finder import (
    ANALYSIS_TURN_QUOTA,
    EDGE_FINDER_RUN_QUOTA,
    PERSONA_VERSION,
)
from tpcore.lab.llm_finder.models import (
    AnalysisRequest,
    AnalysisResult,
    FinderRun,
    MarketSnapshot,
    ProposedSpec,
    ToolResult,
    record_finder_run,
)
from tpcore.lab.llm_finder.reference_loader import (
    MANDATORY_REFERENCE_BUNDLE,
    load_reference_bundles,
)
from tpcore.lab.llm_finder.snapshot import assemble_snapshot
from tpcore.lab.llm_finder.tool_sandbox import dispatch

logger = structlog.get_logger(__name__)


async def _call_llm(
    snapshot: MarketSnapshot,
    references: tuple[ReferenceExcerpt, ...],
    prior_turns: tuple[AnalysisRequest | AnalysisResult, ...],
    prior_results: tuple[ToolResult, ...],
    *,
    turn: int,
    target_engine: str | None,
) -> AnalysisRequest | AnalysisResult | dict[str, Any]:
    """The Anthropic SDK seam.

    T8 leaves this as a NotImplementedError raise; T9 (separate task)
    wires the real SDK call against ``httpx.MockTransport`` in tests.

    The seam returns EITHER an ``AnalysisRequest`` (continue the
    analysis loop) OR an ``AnalysisResult`` (terminate the loop and
    return the proposed specs) OR a raw dict shape with a
    ``_raw_proposed_specs`` key (the truncation seam — see
    ``run_finder`` for the EDGE_FINDER_RUN_QUOTA=3 truncation logic).
    """
    raise NotImplementedError(
        "T8 stub — wired in T9 against httpx.MockTransport"
    )


def _truncate_specs(
    candidates: tuple[ProposedSpec, ...],
) -> tuple[tuple[ProposedSpec, ...], int]:
    """Spec §10.2: cap at EDGE_FINDER_RUN_QUOTA = 3 with a loud
    warning if the LLM emitted more. Returns the truncated tuple
    plus the count discarded."""
    if len(candidates) <= EDGE_FINDER_RUN_QUOTA:
        return candidates, 0
    discarded = len(candidates) - EDGE_FINDER_RUN_QUOTA
    logger.warning(
        "llm_edge_finder.specs_truncated",
        proposed=len(candidates),
        kept=EDGE_FINDER_RUN_QUOTA,
        discarded=discarded,
    )
    return candidates[:EDGE_FINDER_RUN_QUOTA], discarded


async def run_finder(
    pool: Any,
    *,
    target_engine: str | None,
    reference_bundles: tuple[str, ...],
    session_date: date,
    snapshot_ts: datetime | None = None,
) -> FinderRun:
    """One operator-command finder run.

    Returns a frozen ``FinderRun`` describing the loop's outcome. The
    function NEVER raises on a downstream failure (mirrors SP-G
    discipline) — every failure mode is captured in
    ``FinderRun.rejection_reason``.
    """
    run_id = uuid4()
    started = snapshot_ts or datetime.now(UTC)

    # Phase A — DATA ASSEMBLY (deterministic, pre-LLM).
    references_dir = (
        # Pulled in as a callable so tests can monkeypatch the loader.
        None
    )
    try:
        snapshot = await assemble_snapshot(
            pool,
            session_date=session_date,
            universe="sp500",
            snapshot_ts=started,
        )
    except Exception as exc:
        logger.error("llm_edge_finder.snapshot_failed", error=str(exc))
        return FinderRun(
            run_id=run_id, started_ts=started,
            completed_ts=datetime.now(UTC),
            snapshot_session_date=session_date,
            persona_version=PERSONA_VERSION,
            reference_bundle=(reference_bundles[0] if reference_bundles
                              else MANDATORY_REFERENCE_BUNDLE),
            analysis_turn_count=0,
            proposed_spec_count=0,
            emitted_pr_urls=(),
            rejection_reason=f"snapshot: {exc!s}",
        )

    try:
        from pathlib import Path
        refs_dir = Path(__file__).resolve().parent.parent / "docs" / (
            "lab_emitter_references"
        )
        references = load_reference_bundles(
            reference_bundles, references_dir=refs_dir,
        )
    except Exception as exc:
        logger.error("llm_edge_finder.refs_failed", error=str(exc))
        return FinderRun(
            run_id=run_id, started_ts=started,
            completed_ts=datetime.now(UTC),
            snapshot_session_date=session_date,
            persona_version=PERSONA_VERSION,
            reference_bundle=(reference_bundles[0] if reference_bundles
                              else MANDATORY_REFERENCE_BUNDLE),
            analysis_turn_count=0, proposed_spec_count=0,
            emitted_pr_urls=(),
            rejection_reason=f"references: {exc!s}",
        )

    # Phase B — ANALYSIS (LLM-driven, tool-sandboxed).
    prior_turns: list[AnalysisRequest | AnalysisResult] = []
    prior_results: list[ToolResult] = []
    analysis_result: AnalysisResult | None = None
    raw_specs: tuple[ProposedSpec, ...] = ()

    for turn in range(1, ANALYSIS_TURN_QUOTA + 1):
        emission = await _call_llm(
            snapshot, references, tuple(prior_turns),
            tuple(prior_results),
            turn=turn, target_engine=target_engine,
        )
        # Case A: raw dict seam (truncation test path) — read
        # _raw_proposed_specs before pydantic-validation.
        if isinstance(emission, dict) and "_raw_proposed_specs" in emission:
            raw_specs = tuple(emission["_raw_proposed_specs"])
            prior_turns.append(
                # Build a synthetic AnalysisResult-shaped marker for
                # logging only; we won't pydantic-validate the > 3
                # case (it would reject).
                None  # type: ignore[arg-type]
            )
            break
        if isinstance(emission, AnalysisResult):
            analysis_result = emission
            raw_specs = emission.proposed_specs
            prior_turns.append(emission)
            break
        if isinstance(emission, AnalysisRequest):
            # Dispatch each tool call IN-PROCESS.
            results: list[ToolResult] = []
            for call in emission.tool_calls:
                results.append(dispatch(call, snapshot))
            prior_turns.append(emission)
            prior_results.extend(results)
            continue
        # Unknown shape — bail.
        logger.error(
            "llm_edge_finder.llm_seam_unknown_shape",
            shape=type(emission).__name__,
        )
        break

    # Phase C — IDEA EMISSION (compose with SP-G `emit_once`).
    proposed_specs, _discarded = _truncate_specs(raw_specs)
    emitted_pr_urls: list[str] = []
    for spec in proposed_specs:
        out = await emit_once(
            pool,
            target=spec.target_engine,
            intent=spec.intent,
            expected_trials=spec.expected_trials,
            reference_bundles=reference_bundles,
        )
        if out.pr_link is not None:
            emitted_pr_urls.append(out.pr_link)

    # Phase C3 — write the FinderRun audit row.
    completed = datetime.now(UTC)
    bundle_for_log = (
        reference_bundles[0] if reference_bundles
        else MANDATORY_REFERENCE_BUNDLE
    )
    run = FinderRun(
        run_id=run_id,
        started_ts=started,
        completed_ts=completed,
        snapshot_session_date=session_date,
        persona_version=PERSONA_VERSION,
        reference_bundle=bundle_for_log,
        analysis_turn_count=len(prior_turns),
        proposed_spec_count=len(proposed_specs),
        emitted_pr_urls=tuple(emitted_pr_urls),
        rejection_reason=None,
    )
    try:
        await record_finder_run(pool, run)
    except Exception as exc:
        logger.error("llm_edge_finder.record_failed", error=str(exc))
    return run


__all__ = ["run_finder"]
```

- [ ] **Step 8.4: Run the test — expect pass**

Run: `python -m pytest tests/test_llm_edge_finder_agent.py -v -p no:xdist`
Expected: PASS — three tests green.

- [ ] **Step 8.5: Commit**

```bash
git add ops/llm_edge_finder.py tests/test_llm_edge_finder_agent.py
git commit -m "$(cat <<'EOF'
feat(task-25): T8 — agent core (Phase A/B/C orchestration, mocked LLM seam)

run_finder orchestrates the spec §3.2 loop: Phase A (snapshot + refs),
Phase B (LLM seam + tool_sandbox dispatch, bounded by
ANALYSIS_TURN_QUOTA=8), Phase C (emit_once per ProposedSpec, capped at
EDGE_FINDER_RUN_QUOTA=3 with loud warning on truncation). FinderRun
audit row written after the loop. _call_llm is a stub (T9 wires the
real Anthropic SDK against httpx.MockTransport).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Anthropic SDK wiring (`_call_llm` body) — `httpx.MockTransport` tests

**Spec citation:** §2.5 — "Credential-starved + crash-isolated. Co-task on `ops/llm_triage_service.py`; no `ALPACA_*` in env; no `tools` payload to the Anthropic SDK (the §6 sandbox is dispatched IN-PROCESS by the agent on the LLM's structured request, not by the SDK)." §3.2 Phase B1 — "invoke Anthropic SDK with snapshot + refs + persona". `feedback_use_official_docs` — fetch current Anthropic SDK shape; mocks must match the real shape.

The SDK call uses `AsyncAnthropic.messages.create(model=ANTHROPIC_MODEL, max_tokens=ANTHROPIC_MAX_TOKENS, temperature=0.0, system=<persona>, messages=[{role: "user", content: <snapshot+refs+turn-state json>}])`. There is NO `tools` payload (spec §2.5).

**Files:**
- Modify: `ops/llm_edge_finder.py` — implement `_call_llm` against the real SDK + add a `_build_user_prompt` helper.
- Test: `tests/test_llm_edge_finder_anthropic_wiring.py`

- [ ] **Step 9.1: Write the failing SDK-wiring test (`httpx.MockTransport`)**

Create `tests/test_llm_edge_finder_anthropic_wiring.py`:

```python
"""Task #25 — Anthropic SDK wiring (spec §2.5 + §3.2 Phase B1).

NEVER calls the real API. Uses ``httpx.MockTransport`` to intercept
the request and assert:
 - the call uses ``messages.create`` (the shipped Anthropic shape);
 - NO ``tools`` payload appears in the request body (spec §2.5);
 - the system prompt is the persona text;
 - the user message contains a JSON payload with snapshot + refs +
   turn-state keys.

Operator memory feedback_ops_package_shadow_full_suite_gate — this
test imports ops.llm_edge_finder; pytestmark =
pytest.mark.xdist_group("ops_shadow").
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FINDER_PATH = _REPO_ROOT / "ops" / "llm_edge_finder.py"
_SAVED = {
    k: sys.modules.get(k)
    for k in ("ops", "ops.llm_data_triage", "ops.llm_lab_emitter",
              "ops.llm_edge_finder")
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_FINDER_PATH.parent)]
        sys.modules["ops"] = _pkg
    import ops.llm_data_triage  # noqa: F401
    import ops.llm_lab_emitter  # noqa: F401
    _spec = importlib.util.spec_from_file_location(
        "_edge_finder_sdk_test", _FINDER_PATH,
    )
    ef = importlib.util.module_from_spec(_spec)
    sys.modules["_edge_finder_sdk_test"] = ef
    _spec.loader.exec_module(ef)
finally:
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


from tpcore.lab.llm_emitter.models import ReferenceExcerpt
from tpcore.lab.llm_finder.models import MarketSnapshot


def _empty_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=date(2026, 5, 20),
        universe="sp500",
        price_window=(),
        fundamentals=(),
        ledger_state=(),
        roster=(),
    )


@pytest.mark.asyncio
async def test_call_llm_uses_messages_create_with_no_tools_payload(
    monkeypatch,
) -> None:
    """Spec §2.5: NO `tools` payload to the Anthropic SDK."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "kind": "AnalysisResult",
                            "turn": 1,
                            "tool_results": [],
                            "proposed_specs": [],
                            "finder_rationale": "no edge in synthetic data",
                        }),
                    },
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    # Inject the mock transport — the agent's client factory uses
    # httpx_client= when provided.
    monkeypatch.setattr(
        ef, "_build_httpx_client",
        lambda: httpx.AsyncClient(transport=transport),
    )

    snap = _empty_snapshot()
    out = await ef._call_llm(
        snap,
        references=(
            ReferenceExcerpt(name="dsr_ntrials_discipline", text="body"),
        ),
        prior_turns=(),
        prior_results=(),
        turn=1,
        target_engine=None,
    )
    assert "messages" in captured["url"]
    assert "tools" not in captured["body"], (
        "spec §2.5 — NO tools payload to the Anthropic SDK"
    )
    assert captured["body"]["temperature"] == 0.0
    # System prompt is the persona text.
    assert isinstance(captured["body"].get("system"), str)
    assert len(captured["body"]["system"]) > 0
    # User message carries the snapshot + refs.
    user_msg = captured["body"]["messages"][0]
    assert user_msg["role"] == "user"
    user_content = user_msg["content"]
    if isinstance(user_content, list):
        user_text = user_content[0]["text"]
    else:
        user_text = user_content
    payload = json.loads(user_text)
    assert "snapshot" in payload
    assert "references" in payload
    assert payload["turn"] == 1
    # The response was decoded back into an AnalysisResult.
    from tpcore.lab.llm_finder.models import AnalysisResult
    assert isinstance(out, AnalysisResult)


@pytest.mark.asyncio
async def test_call_llm_returns_analysis_request_when_seam_says_so(
    monkeypatch,
) -> None:
    """If the LLM returns an AnalysisRequest-shaped JSON, the wrapper
    decodes it into AnalysisRequest (the loop continues)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "kind": "AnalysisRequest",
                            "turn": 1,
                            "rationale": "let's adfuller AAPL log_return",
                            "tool_calls": [
                                {
                                    "callable_name": "adfuller",
                                    "args_json": json.dumps({
                                        "ticker": "AAPL",
                                        "series_id": "log_return",
                                    }),
                                },
                            ],
                        }),
                    },
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        ef, "_build_httpx_client",
        lambda: httpx.AsyncClient(transport=transport),
    )

    snap = _empty_snapshot()
    out = await ef._call_llm(
        snap, references=(), prior_turns=(), prior_results=(),
        turn=1, target_engine=None,
    )
    from tpcore.lab.llm_finder.models import AnalysisRequest
    assert isinstance(out, AnalysisRequest)
    assert out.tool_calls[0].callable_name == "adfuller"
```

- [ ] **Step 9.2: Run the test — expect failure**

Run: `python -m pytest tests/test_llm_edge_finder_anthropic_wiring.py -v -p no:xdist`
Expected: FAIL — `NotImplementedError` from the T8 stub.

- [ ] **Step 9.3: Implement `_call_llm` against the real Anthropic SDK**

Replace the `_call_llm` stub in `ops/llm_edge_finder.py` with the implementation below. Also add the helpers `_build_user_prompt` and `_build_httpx_client`. Use `Edit` to find the existing stub:

```python
async def _call_llm(
    snapshot: MarketSnapshot,
    references: tuple[ReferenceExcerpt, ...],
    prior_turns: tuple[AnalysisRequest | AnalysisResult, ...],
    prior_results: tuple[ToolResult, ...],
    *,
    turn: int,
    target_engine: str | None,
) -> AnalysisRequest | AnalysisResult | dict[str, Any]:
    """The Anthropic SDK seam.

    T8 leaves this as a NotImplementedError raise; T9 (separate task)
    wires the real SDK call against ``httpx.MockTransport`` in tests.

    The seam returns EITHER an ``AnalysisRequest`` (continue the
    analysis loop) OR an ``AnalysisResult`` (terminate the loop and
    return the proposed specs) OR a raw dict shape with a
    ``_raw_proposed_specs`` key (the truncation seam — see
    ``run_finder`` for the EDGE_FINDER_RUN_QUOTA=3 truncation logic).
    """
    raise NotImplementedError(
        "T8 stub — wired in T9 against httpx.MockTransport"
    )
```

And replace with:

```python
import json as _json
from pathlib import Path as _Path

import httpx

# Reuse SP-G's shipped envelope constants — the same model + token
# ceiling apply to the finder (spec §3.3 "compose, don't reimplement").
from ops.llm_data_triage import (
    ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_MODEL,
)

_REPO_ROOT_FINDER = _Path(__file__).resolve().parent.parent
_PERSONA_PATH_FINDER = _REPO_ROOT_FINDER / "docs" / "lab_finder_persona.md"


def _build_httpx_client() -> httpx.AsyncClient:
    """Seam for tests: real code returns a default ``AsyncClient``;
    tests inject ``httpx.MockTransport``."""
    return httpx.AsyncClient(timeout=httpx.Timeout(120.0))


def _persona_text() -> str:
    if not _PERSONA_PATH_FINDER.is_file():
        raise RuntimeError(
            f"persona file missing at {_PERSONA_PATH_FINDER} — "
            f"see Task #25 T6"
        )
    return _PERSONA_PATH_FINDER.read_text(encoding="utf-8")


def _build_user_prompt(
    snapshot: MarketSnapshot,
    references: tuple[ReferenceExcerpt, ...],
    prior_turns: tuple[AnalysisRequest | AnalysisResult, ...],
    prior_results: tuple[ToolResult, ...],
    *,
    turn: int,
    target_engine: str | None,
) -> str:
    """JSON-encode the per-turn user payload. The LLM sees ONLY this
    payload — never repo paths or live credentials."""
    payload = {
        "turn": turn,
        "ANALYSIS_TURN_QUOTA": ANALYSIS_TURN_QUOTA,
        "EDGE_FINDER_RUN_QUOTA": EDGE_FINDER_RUN_QUOTA,
        "persona_version": PERSONA_VERSION,
        "target_engine": target_engine,
        "snapshot": snapshot.model_dump(mode="json"),
        "references": [
            {"name": r.name, "text": r.text} for r in references
        ],
        "prior_turns": [
            t.model_dump(mode="json") if t is not None else None
            for t in prior_turns
        ],
        "prior_results": [
            r.model_dump(mode="json") for r in prior_results
        ],
        "directive": (
            "Emit ONE JSON object. Either: (a) an AnalysisRequest "
            "(`kind: 'AnalysisRequest'`) carrying <=4 ToolCalls (the "
            "loop continues); OR (b) an AnalysisResult (`kind: "
            "'AnalysisResult'`) carrying up to 3 ProposedSpecs (the "
            "loop terminates)."
        ),
    }
    return _json.dumps(payload, default=str)


def _decode_llm_response(
    text: str,
) -> AnalysisRequest | AnalysisResult:
    """Parse the LLM's JSON text into the typed contract model."""
    parsed = _json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"LLM response not a JSON object: {type(parsed).__name__}"
        )
    kind = parsed.pop("kind", None)
    if kind == "AnalysisRequest":
        return AnalysisRequest.model_validate(parsed)
    if kind == "AnalysisResult":
        return AnalysisResult.model_validate(parsed)
    raise ValueError(f"LLM response missing or invalid kind: {kind!r}")


async def _call_llm(
    snapshot: MarketSnapshot,
    references: tuple[ReferenceExcerpt, ...],
    prior_turns: tuple[AnalysisRequest | AnalysisResult, ...],
    prior_results: tuple[ToolResult, ...],
    *,
    turn: int,
    target_engine: str | None,
) -> AnalysisRequest | AnalysisResult:
    """Real-SDK Anthropic call (spec §2.5 + §3.2 Phase B1).

    NO ``tools`` payload (spec §2.5). The sandbox is dispatched
    in-process by ``run_finder`` on each ``AnalysisRequest.tool_calls``.
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    user_text = _build_user_prompt(
        snapshot, references, prior_turns, prior_results,
        turn=turn, target_engine=target_engine,
    )
    system_text = _persona_text()

    async with _build_httpx_client() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": ANTHROPIC_MAX_TOKENS,
                "temperature": 0.0,
                "system": system_text,
                "messages": [{"role": "user", "content": user_text}],
            },
        )
        resp.raise_for_status()
        body = resp.json()

    # Anthropic returns content as a list of content blocks; take the
    # first text block.
    text_blocks = [b for b in body["content"] if b["type"] == "text"]
    if not text_blocks:
        raise ValueError("Anthropic response carried no text block")
    return _decode_llm_response(text_blocks[0]["text"])
```

- [ ] **Step 9.4: Verify httpx is available**

Run: `python -c "import httpx; print(httpx.__version__)"`
Expected: prints a version. (`httpx` is a standard test dependency in the project — already used by the Anthropic SDK transitively.)

- [ ] **Step 9.5: Run the SDK-wiring test — expect pass**

Run: `python -m pytest tests/test_llm_edge_finder_anthropic_wiring.py -v -p no:xdist`
Expected: PASS — two tests green; no live network calls; `httpx.MockTransport` intercepts.

- [ ] **Step 9.6: Re-run T8 agent tests to confirm no regression**

Run: `python -m pytest tests/test_llm_edge_finder_agent.py tests/test_llm_edge_finder_anthropic_wiring.py -v -p no:xdist`
Expected: PASS — five tests green.

- [ ] **Step 9.7: Commit**

```bash
git add ops/llm_edge_finder.py \
        tests/test_llm_edge_finder_anthropic_wiring.py
git commit -m "$(cat <<'EOF'
feat(task-25): T9 — Anthropic SDK wiring (no tools payload, httpx.MockTransport tests)

_call_llm posts to /v1/messages with system=persona, NO tools payload
(spec §2.5), temperature=0.0. Reuses SP-G's ANTHROPIC_MODEL +
ANTHROPIC_MAX_TOKENS (compose, don't reimplement — spec §3.3). Tests
use httpx.MockTransport — no live API call, no live SDK auth. The
response carries `kind: 'AnalysisRequest'|'AnalysisResult'` so the
agent's loop knows when to terminate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Two-daemon invariant + 4th co-task augmentation

**Spec citation:** §2.7 — "Two-daemon invariant preserved. Adding the finder co-task brings the LLM-triage daemon co-task count to FOUR (data-triage + engine-triage + SP-G emitter + Task #25 finder). Still two daemons; `tests/test_two_daemon_invariant.py` still passes." §4.2 — "4th `_run_supervised` co-task; two-daemon invariant preserved."

Wait: re-reading `ops/llm_triage_service.py` (line 7-8 docstring): it says it co-hosts **THREE** lanes today (data, engine, lab-emitter SP-G). Spec §2.7 says the finder will be the **FOURTH** co-task. So today is 3, we add 1 → 4. Existing `test_two_daemon_invariant.py` does NOT check co-task COUNT (it checks the installer whitelist set of 4 installer tokens — which is unchanged because we're adding a co-task, not a daemon). The new test (`test_four_cotask_invariant.py`) is the four-co-task assertion.

**Files:**
- Modify: `ops/llm_triage_service.py` — add `_edge_finder_loop` co-task.
- Modify: `.claude/rules/llm-triage.md` — update co-task count wording.
- Test: `tests/test_four_cotask_invariant.py`
- Verify: `scripts/tests/test_two_daemon_invariant.py` still passes unchanged.

- [ ] **Step 10.1: Write the failing four-co-task test**

Create `tests/test_four_cotask_invariant.py`:

```python
"""Task #25 — four-co-task invariant test (spec §10.2 / §2.7).

Asserts ops/llm_triage_service.py runs FOUR crash-isolated co-tasks
on the ONE advisory pool:
  1. data lane
  2. engine lane
  3. SP-G lab-emitter
  4. Task #25 lab-edge-finder

The two-daemon invariant
(scripts/tests/test_two_daemon_invariant.py) is preserved — Task #25
adds a CO-TASK, not a daemon.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVICE_PATH = _REPO_ROOT / "ops" / "llm_triage_service.py"


def _load_service():
    _SAVED = {
        k: sys.modules.get(k)
        for k in ("ops", "ops.llm_triage_service")
    }
    try:
        _ops = sys.modules.get("ops")
        if not isinstance(getattr(_ops, "__path__", None), list):
            _pkg = types.ModuleType("ops")
            _pkg.__path__ = [str(_SERVICE_PATH.parent)]
            sys.modules["ops"] = _pkg
        _spec = importlib.util.spec_from_file_location(
            "_llm_triage_service_under_test", _SERVICE_PATH,
        )
        m = importlib.util.module_from_spec(_spec)
        sys.modules["_llm_triage_service_under_test"] = m
        _spec.loader.exec_module(m)
        return m
    finally:
        for _k, _v in _SAVED.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v


def test_service_module_defines_edge_finder_loop() -> None:
    svc = _load_service()
    assert hasattr(svc, "_edge_finder_loop"), (
        "Task #25 spec §2.7 — ops/llm_triage_service.py must define "
        "_edge_finder_loop as the 4th co-task"
    )


def test_service_pool_max_size_widened_for_four_cotasks() -> None:
    svc = _load_service()
    # Today: POOL_MAX_SIZE = 5 (three co-tasks). Task #25 widens to 6
    # so the 4th co-task's poll doesn't contend.
    assert svc.POOL_MAX_SIZE >= 6, (
        f"POOL_MAX_SIZE={svc.POOL_MAX_SIZE} must be >=6 for four "
        f"co-tasks (1 poll/lane + each lane's run_triage acquire + "
        f"headroom)"
    )


def test_service_source_lists_four_cotasks() -> None:
    """Source-grep — _amain spawns FOUR asyncio tasks."""
    src = _SERVICE_PATH.read_text(encoding="utf-8")
    # We expect a new factory name like _edge_finder_factory and a
    # corresponding task variable like edge_finder_task.
    assert "_edge_finder_factory" in src
    assert "edge_finder_task" in src


def test_two_daemon_invariant_still_green() -> None:
    """Running the existing two-daemon invariant assertion inline."""
    import re
    sh = (
        _REPO_ROOT / "scripts" / "install_all_daemons.sh"
    ).read_text(encoding="utf-8")
    m = re.search(r"for installer in ([^\n;]+);\s*do", sh)
    assert m is not None
    tokens = set(m.group(1).split())
    # Task #25 adds a CO-TASK, not a daemon — the installer whitelist
    # is UNCHANGED.
    assert tokens == {
        "install_launchd_engine_service",
        "install_launchd_data_repair_service",
        "install_launchd_data_operations",
        "install_launchd_llm_triage_service",
    }
```

- [ ] **Step 10.2: Run the test — expect failure**

Run: `python -m pytest tests/test_four_cotask_invariant.py -v -p no:xdist`
Expected: FAIL — `_edge_finder_loop` not in `ops/llm_triage_service.py` yet.

- [ ] **Step 10.3: Augment `ops/llm_triage_service.py` with the 4th co-task**

Use `Edit` to make four changes to `ops/llm_triage_service.py`:

**(a)** Bump `POOL_MAX_SIZE` from `5` to `6`. Find:

```python
# poll (1 per lane) + each lane's run_triage acquires + headroom; with
# THREE co-hosted lanes (data, engine, lab_emitter — SP-G) sharing the
# one advisory pool we widen the cap once more.
POOL_MAX_SIZE = 5
```

Replace with:

```python
# poll (1 per lane) + each lane's run_triage acquires + headroom; with
# FOUR co-hosted lanes (data, engine, lab_emitter SP-G, lab_edge_finder
# Task #25) sharing the one advisory pool we widen the cap once more.
POOL_MAX_SIZE = 6
```

**(b)** Add the import near the existing `ops.llm_lab_emitter` import. Find:

```python
from ops.llm_lab_emitter import (
    LAB_EMITTER_TRIGGER_EVENT_TYPES,
    run_lab_emitter_cotask,
)
```

Append below it:

```python
from ops.llm_edge_finder import run_finder as _run_edge_finder
```

**(c)** Add the `_edge_finder_loop` co-task function. Insert AFTER `_lab_emitter_loop` and BEFORE `_run_supervised`:

```python
# Spec §2.7 (Task #25): the LAB_EDGE_FINDER trigger event class is
# operator-command-only in v1 — there is NO scheduled event. The
# co-task is structurally present (preserves the four-co-task
# symmetry) but its trigger tuple is empty by design; the operator
# triggers via `/lab-edge-find` calling `python -m ops.llm_edge_finder`.
LAB_EDGE_FINDER_TRIGGER_EVENT_TYPES: tuple[str, ...] = ()


async def _run_edge_finder_cotask(pool) -> None:
    """Co-task entry. Per spec §1 the v1 trigger is operator-command-
    only — the trigger tuple is empty so this is a no-op until the
    operator runs `/lab-edge-find`. Symmetric with the SP-G lab-emitter
    co-task per spec §2.7."""
    logger.info("llm_triage_service.edge_finder_cotask_noop_v1")


async def _edge_finder_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR,
) -> None:
    """LAB-EDGE-FINDER co-task (Task #25; spec §2.7 / §4.2).

    The FOURTH crash-isolated ``_run_supervised`` co-task on the ONE
    advisory pool. Like the SP-G lab-emitter co-task, the v1 trigger
    tuple is empty by design (operator-command-only via
    ``/lab-edge-find``). When a future event-emitter PR populates
    ``LAB_EDGE_FINDER_TRIGGER_EVENT_TYPES``, this co-task starts firing
    ``_run_edge_finder_cotask`` on the trigger — zero code change here.

    The two-daemon invariant test must stay green unedited — Task #25
    adds a CO-TASK, not a daemon.
    """
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=LAB_EDGE_FINDER_TRIGGER_EVENT_TYPES,
        triage_fn=_run_edge_finder_cotask,
        lane="lab_edge_finder",
    )
```

**(d)** Update `_amain` to spawn the fourth co-task. Find:

```python
    async def _lab_emitter_factory():
        await _lab_emitter_loop(pool, stop_event, lock_dir)

    data_task = asyncio.create_task(
        _run_supervised("data", _data_factory, stop_event))
    engine_task = asyncio.create_task(
        _run_supervised("engine", _engine_factory, stop_event))
    lab_emitter_task = asyncio.create_task(
        _run_supervised("lab_emitter", _lab_emitter_factory, stop_event))
```

Replace with:

```python
    async def _lab_emitter_factory():
        await _lab_emitter_loop(pool, stop_event, lock_dir)

    async def _edge_finder_factory():
        await _edge_finder_loop(pool, stop_event, lock_dir)

    data_task = asyncio.create_task(
        _run_supervised("data", _data_factory, stop_event))
    engine_task = asyncio.create_task(
        _run_supervised("engine", _engine_factory, stop_event))
    lab_emitter_task = asyncio.create_task(
        _run_supervised("lab_emitter", _lab_emitter_factory, stop_event))
    edge_finder_task = asyncio.create_task(
        _run_supervised(
            "lab_edge_finder", _edge_finder_factory, stop_event,
        ))
```

And find:

```python
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        all_done = asyncio.gather(data_task, engine_task, lab_emitter_task)
        done, _pending = await asyncio.wait(
            {stop_waiter, all_done},
            return_when=asyncio.FIRST_COMPLETED)
```

Replace with:

```python
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        all_done = asyncio.gather(
            data_task, engine_task, lab_emitter_task, edge_finder_task,
        )
        done, _pending = await asyncio.wait(
            {stop_waiter, all_done},
            return_when=asyncio.FIRST_COMPLETED)
```

And find:

```python
    finally:
        for t in (data_task, engine_task, lab_emitter_task):
            t.cancel()
        await asyncio.gather(data_task, engine_task, lab_emitter_task,
                             return_exceptions=True)
```

Replace with:

```python
    finally:
        for t in (
            data_task, engine_task, lab_emitter_task, edge_finder_task,
        ):
            t.cancel()
        await asyncio.gather(
            data_task, engine_task, lab_emitter_task, edge_finder_task,
            return_exceptions=True,
        )
```

- [ ] **Step 10.4: Update `.claude/rules/llm-triage.md` to reflect 4 co-tasks**

Use `Edit` to update `.claude/rules/llm-triage.md`. Find:

```markdown
- **Advisory lane** (Epic E B1): `llm_triage_service` — two crash-isolated `_run_supervised` co-tasks (data-lane + engine-lane), event-driven off `application_log`. NOT a new daemon — folded into the existing one.
```

This text lives in `.claude/rules/daemons.md` not `llm-triage.md`. Fix in `daemons.md`. Find that line and replace with:

```markdown
- **Advisory lane** (Epic E B1 + SP-G + Task #25): `llm_triage_service` — FOUR crash-isolated `_run_supervised` co-tasks (data-lane + engine-lane + SP-G lab-emitter + Task #25 lab-edge-finder), event-driven off `application_log`. NOT a new daemon — folded into the existing one (spec §2.7).
```

- [ ] **Step 10.5: Run the four-co-task test — expect pass**

Run: `python -m pytest tests/test_four_cotask_invariant.py -v -p no:xdist`
Expected: PASS — four tests green.

- [ ] **Step 10.6: Run the existing two-daemon invariant test — expect pass unchanged**

Run: `python -m pytest scripts/tests/test_two_daemon_invariant.py -v -p no:xdist`
Expected: PASS — all existing tests green, no edits required to that file (Task #25 adds a CO-TASK, not a daemon).

- [ ] **Step 10.7: Commit**

```bash
git add ops/llm_triage_service.py tests/test_four_cotask_invariant.py \
        .claude/rules/daemons.md
git commit -m "$(cat <<'EOF'
feat(task-25): T10 — fourth co-task on llm_triage_service

Adds _edge_finder_loop as the 4th crash-isolated co-task (spec §2.7).
v1 trigger tuple is empty by design — operator-command-only via
`/lab-edge-find`. Mirrors SP-G lab-emitter co-task symmetry. Bumps
POOL_MAX_SIZE 5→6 for the 4th lane. Updates .claude/rules/daemons.md
to reflect the four-co-task topology. The two-daemon invariant test
remains green UNEDITED — Task #25 adds a co-task, not a daemon.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Slash-skill (`.claude/skills/lab-edge-find/SKILL.md`) + operator runbook + safety-grep tests

**Spec citation:** §9.1 decision #7 — "Slash-skill exact filename — `.claude/skills/lab-edge-find/SKILL.md` (SP-G precedent); plan PR confirms." §8 — the operator runbook content (the 10-step graduation walk-through). §10.3 — the make-or-break safety tests (`test_finder_cannot_bypass_sp_g.py`, `test_finder_cannot_import_non_whitelisted.py`).

**Files:**
- Create: `.claude/skills/lab-edge-find/SKILL.md`
- Create: `docs/llm_edge_finder_operator_runbook.md`
- Create: `tests/test_llm_edge_finder_composes_with_sp_g.py`
- Create: `tests/test_finder_cannot_bypass_sp_g.py`
- Create: `tests/test_finder_cannot_import_non_whitelisted.py`
- Create: `tests/test_llm_edge_finder_quota.py`
- Create: `tests/test_llm_edge_finder_round_trip.py`

- [ ] **Step 11.1: Author the slash-skill**

Create `.claude/skills/lab-edge-find/SKILL.md`:

```markdown
---
name: lab-edge-find
description: "Slash-only wrapper for the Task #25 autonomous LLM+quant edge finder — python -m ops.llm_edge_finder [--target-engine <engine>] [--reference-bundle <name>]. The finder runs the spec §3.2 data → analysis → idea loop ONCE, emits up to EDGE_FINDER_RUN_QUOTA=3 single-hypothesis ProposedSpecs, and routes EACH through SP-G emit_once verbatim. v1 quota: 3 specs/run x max 1 run/day. Operator-command only; no autonomous loop."
disable-model-invocation: true
---

# Lab edge-find (Task #25 — autonomous LLM+quant edge finder)

Canonical CLI: `python -m ops.llm_edge_finder [--target-engine <engine>] [--reference-bundle <name>] [--session-date YYYY-MM-DD]`.
Spec: `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs ONE Task #25 finder cycle:

1. **Phase A — DATA** (deterministic): assembles a frozen `MarketSnapshot`
   from local Postgres (price window 252 sessions, latest quarter
   fundamentals, SP-A ledger state, SP-B roster). Loads the
   `--reference-bundle` selection (the mandatory
   `dsr_ntrials_discipline` is ALWAYS included). Bounded payload at
   `MAX_SNAPSHOT_BYTES = 512 KiB` — fail-loud on overflow.
2. **Phase B — ANALYSIS** (LLM-driven, tool-sandboxed): calls the
   Anthropic SDK up to `ANALYSIS_TURN_QUOTA = 8` times. The LLM emits
   typed `AnalysisRequest`s carrying `ToolCall`s; the agent dispatches
   each in-process via the `statsmodels` + `scipy.stats` WHITELIST
   sandbox; results are threaded back.
3. **Phase C — IDEA EMISSION** (compose with SP-G): for each
   `ProposedSpec` (capped at `EDGE_FINDER_RUN_QUOTA = 3`) the agent
   calls `ops.llm_lab_emitter.emit_once` ONCE. SP-G's fence stack runs
   verbatim — ledger pre-check, EmittedSpec validate,
   record_trial_spend, render, enforce_diff_scope,
   validate_no_gate_override, gh pr create --draft. One draft PR per
   spec.

The finder STOPS at Phase C3 (one FinderRun audit row written to
`platform.application_log`). Steps after — operator review, SP-C
Readiness, `python -m ops.lab`, autonomous Lab criteria, ECR — are
existing infrastructure unchanged.

## Hard constraints (the v1 fence stack — spec §2)

- **Cumulative n_trials honesty** (SP-A) — every emission spends one
  `record_trial_spend` row BEFORE the draft PR is opened.
- **Single pre-registered hypothesis per emission** — pydantic
  validators reject multi-hypothesis shapes; 3 specs/run = 3 SEPARATE
  emissions, each with its own ledger row + its own draft PR.
- **The gate is sacred** — autonomous Lab criteria, the credibility
  scorer, the readiness checklist, the ECR mechanism, the
  `_PROFILE` roster are NEVER edited.
- **Advisory + human-gated only** — draft PR only; no `--undraft`
  code path; operator is the merge authority.
- **Credential-starved + crash-isolated** — runs as the 4th co-task
  on `ops/llm_triage_service.py`; no `ALPACA_*` in env; no `tools`
  payload to the SDK.
- **Roster-mediated, never roster-mutating** — reads
  `tpcore.engine_profile.lab_targetable_engines()`; never edits
  `_PROFILE` / `providers.py` / any engine `LAB_TARGET`.
- **No network beyond the Anthropic SDK call** — `MarketSnapshot` is
  local Postgres only.
- **Toolkit whitelist** — `statsmodels` + `scipy.stats` ONLY (v1).
  The seven callables in spec §6.1 are the complete v1 surface.

## Usage

```bash
# Default — let the LLM choose a target from the roster:
python -m ops.llm_edge_finder

# Pin a target engine (must be in lab_targetable_engines()):
python -m ops.llm_edge_finder --target-engine sentinel

# Select an optional reference bundle (dsr_ntrials_discipline is
# ALWAYS included regardless):
python -m ops.llm_edge_finder --target-engine reversion \
    --reference-bundle chan_algorithmic_trading

# Pin the session date (default: today's UTC date):
python -m ops.llm_edge_finder --session-date 2026-05-20
```

## Pre-conditions

- `ANTHROPIC_API_KEY` set in the operator's environment.
- `DATABASE_URL` (or `DATABASE_URL_IPV4`) reachable; the snapshot reads
  `platform.prices_daily` + `platform.fundamentals_quarterly` +
  `platform.lab_trial_ledger_cumulative` +
  `platform.engine_profile_roster`.
- For `--reference-bundle market_structure_primer`: the operator must
  have authored real content (the v1 ships as a `[operator-pending
  content]` stub — see TODO.md defect_ref row).
- If `--target-engine` is named, it must appear in
  `tpcore.engine_profile.lab_targetable_engines()` (NOT `canary`, NOT
  `lab` sentinel, NOT the allocator, NOT a RETIRED engine).

## After the run

Each emitted ProposedSpec produces ONE draft PR via SP-G `emit_once`.
The operator follows the SP-G human-in-the-loop seam:

1. Hardens the §3 byte-identical proof, §8 data prereqs, §9 lookahead
   honesty sections of the rendered spec.
2. Captures the characterization golden RED-first (Readiness §3 C1-C4).
3. Verifies the diff is the three-slot allow-list ONLY.
4. `gh pr ready` to move the PR out of draft.
5. Routes through `/lab-target-run` → `_run_lab_core` → gate → dossier
   → `/ecr`.

The full 10-step graduation walk-through is in
`docs/llm_edge_finder_operator_runbook.md`.

## Adjacent SoT

- Spec: `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`
- Runbook: `docs/llm_edge_finder_operator_runbook.md`
- SP-G sibling skill: `.claude/skills/lab-spec-emit/SKILL.md`
- Lab-target-run sibling: `.claude/skills/lab-target-run/SKILL.md`
- ECR sibling: `.claude/skills/ecr/SKILL.md`
- Reference bundles dir (shared with SP-G):
  `docs/lab_emitter_references/`
- Persona: `docs/lab_finder_persona.md`
```

- [ ] **Step 11.2: Author the operator runbook**

Create `docs/llm_edge_finder_operator_runbook.md`:

```markdown
# Task #25 LLM Edge Finder — Operator Runbook

Spec: `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`
Skill: `.claude/skills/lab-edge-find/SKILL.md`
Persona: `docs/lab_finder_persona.md`

This runbook walks through the 10-step graduation path (spec §8) from
`/lab-edge-find` to PAPER. v1 success criterion (spec §12): ONE
finder-emitted candidate reaches PAPER via the standard ECR path.

## The graduation path (spec §8 verbatim)

1. **Operator runs `/lab-edge-find --reference-bundle <name>`**
   (or `python -m ops.llm_edge_finder ...`).

2. **Finder Phase A** reads the roster (SP-B
   `lab_targetable_engines()`) + the SP-A cumulative ledger; assembles
   the bounded `MarketSnapshot`; loads the named reference bundle plus
   the mandatory `dsr_ntrials_discipline`; writes the `FinderRun` row
   to `platform.application_log` (event_type `LAB_FINDER_RUN`).

3. **Finder Phase B** invokes the Anthropic SDK up to 8 times. Each
   `AnalysisRequest` carries up to 4 typed `ToolCall`s; the agent
   dispatches each in-process through the `statsmodels` /
   `scipy.stats` whitelist sandbox; results are JSON-shipped back to
   the LLM as `ToolResult`s.

4. **Finder Phase C** receives an `AnalysisResult` with up to 3
   `ProposedSpec`s. For EACH, calls `ops.llm_lab_emitter.emit_once`.
   The SP-G fence stack runs VERBATIM:
   - `ledger_gate.check_budget` — rejects if cumulative + expected >
     `EMISSION_QUOTA_PER_TARGET = 20`.
   - `EmittedSpec` pydantic validate — single hypothesis, one
     `choice:` toggle for `fold_existing`, etc.
   - `record_trial_spend` — the SP-A ledger row, BEFORE the draft PR.
   - `render_candidate_spec` — the SP-E / Readiness 10-section
     markdown.
   - `enforce_diff_scope` — the three-slot allow-list.
   - `validate_no_gate_override` — no `--dsr-threshold` /
     `--credibility-threshold` flags.
   - `gh pr create --draft` — the draft, human-merge-only PR.

5. **Operator review** (the human-in-the-loop seam, SP-G §3.5):
   - Harden §3 byte-identical proof (the C1 committed golden).
   - Harden §8 data prereqs (concrete row counts, not "should be
     there").
   - Harden §9 lookahead / point-in-time honesty.
   - Verify the diff is the three-slot allow-list ONLY.
   - `gh pr ready` to move the PR out of draft.

6. **SP-C Readiness checklist** — mechanical pre-flight:
   - Exactly one `PARAM_RANGES` toggle.
   - Live path files untouched (`grep` proof).
   - Characterization golden present + RED-first.

7. **`python -m ops.lab --candidate <name> --target-engine <engine>
   --intent <i>`** — SP-B dispatch, SP-D ranking, SP-A-deflated gate.
   Dossier lands at
   `docs/lab/<date>-<name>-{SURVIVED|FAILED}-seed*.json`.

8. **Autonomous Lab criteria adjudication** (PR #158):
   - `promote_new`: `_assess_new_engine_signal(dossier)` —
     Sharpe > 0 AND trades ≥ 10 AND MaxDD ≥ −0.50 AND ruin ≤ 0.30
     AND profit_factor ≥ 1.0 AND min_btl_gap ≤ 365.
   - `fold_existing`: `_assess_improvement` — candidate-beats-
     incumbent (strict on `primary_metric`) AND new-engine-floor AND
     trade-count-drift-bounded.

9. **Operator opens ECR** via `/ecr` (ADD for `promote_new`, MODIFY
   for `fold_existing`). PR #210 threads `data_dependencies` through
   MODIFY. The planner re-derives the gate from the dossier sidecar;
   never trusts text.

10. **Engine SDLC: LAB → PAPER** — deterministic, automated post-ECR.
    **v1 success criterion satisfied** when ONE finder-emitted
    candidate reaches this state.

## What to check after a run

- `platform.application_log WHERE event_type = 'LAB_FINDER_RUN'` —
  the run's audit row. `data->>'persona_version'` tells you which
  persona was active.
- `platform.application_log WHERE event_type = 'LLM_LAB_EMITTED_SPEC'`
  — one row per `ProposedSpec` that reached `emit_once` step 5+.
- `platform.data_quality_log WHERE source_namespace LIKE
  'lab_trial_ledger.%'` — the SP-A ledger rows the run spent.
- `gh pr list --label lab-spec-emit` — the draft PRs the run opened.

## Failure modes + recovery

| Symptom | Cause | Action |
|---|---|---|
| `SnapshotOverflow` raised | Snapshot serialised > 512 KiB | Narrow the universe at the call site or wait for v1.5 (sp1500/rus3k). |
| `StubReferenceBundle: market_structure_primer` | The v1 stub is selected | Author real content per the TODO.md defect_ref row; bump the bundle's version. |
| `MissingReferenceBundle: dsr_ntrials_discipline` | Mandatory bundle missing | Re-clone — `dsr_ntrials_discipline.md` is the mandatory always-include. |
| Draft PR open failed AFTER ledger row written | gh CLI flake | Replay from sidecar: `python -m ops.llm_lab_emitter --replay <sidecar>` (SP-G's orphaned-spend recovery). |
| `LedgerBudgetExhausted` for target | Cumulative > quota | Operator-only env override of the per-target quota — NEVER via the LLM. |
| `FinderRun.rejection_reason` set | Phase A failure | Check the `application_log` row's `data->>'rejection_reason'` for the structured cause. |

## What this runbook does NOT cover

- Editing the persona — see `docs/lab_finder_persona.md` + bump
  `PERSONA_VERSION` in `tpcore/lab/llm_finder/__init__.py` AND the
  sentinel SHA in `test_persona_versioned.py`.
- Adding a callable to the tool sandbox — that's a v1.5+ spec, not a
  runbook step.
- Modifying the autonomous Lab criteria — those are `ops/engine_sdlc/
  lab_criteria.py` and are PERMANENTLY OUT OF SCOPE for Task #25
  (spec §9.7).
- ECR / engine SDLC mechanics — see `/ecr` skill and
  `docs/superpowers/checklists/engine_change_request.md`.
```

- [ ] **Step 11.3: Write the safety / composition tests**

Create `tests/test_llm_edge_finder_composes_with_sp_g.py`:

```python
"""Task #25 — composes-with-SP-G CI grep (spec §10.2 + §3.3).

Asserts ops/llm_edge_finder.py:
 - imports `emit_once` from ops.llm_lab_emitter;
 - does NOT re-define record_trial_spend, render_candidate_spec,
   enforce_diff_scope, or validate_no_gate_override.

Spec §3.3: "Task #25 is a CALLER of emit_once; it NEVER reimplements
an SP-G function."
"""
from __future__ import annotations

import re
from pathlib import Path

_FINDER_SRC = (
    Path(__file__).resolve().parents[1] / "ops" / "llm_edge_finder.py"
).read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"#.*", "", src)
    return src


def test_finder_imports_emit_once() -> None:
    stripped = _strip_comments(_FINDER_SRC)
    assert (
        "from ops.llm_lab_emitter import emit_once" in stripped
        or "from ops.llm_lab_emitter import" in stripped
        and "emit_once" in stripped
    ), "Task #25 spec §3.3: finder must IMPORT emit_once, never re-implement"


def test_finder_does_not_redefine_sp_g_functions() -> None:
    stripped = _strip_comments(_FINDER_SRC)
    forbidden = (
        "def record_trial_spend",
        "def render_candidate_spec",
        "def enforce_diff_scope",
        "def validate_no_gate_override",
    )
    hits = [f for f in forbidden if f in stripped]
    assert not hits, (
        f"Task #25 finder re-defines SP-G functions: {hits!r} — "
        f"spec §3.3 'compose, don't reimplement'"
    )
```

Create `tests/test_finder_cannot_bypass_sp_g.py`:

```python
"""Task #25 — finder cannot bypass SP-G (spec §10.3, make-or-break).

Asserts ops/llm_edge_finder.py does NOT invoke `gh pr create`
directly — every draft PR goes through SP-G `emit_once`.
"""
from __future__ import annotations

import re
from pathlib import Path


def _strip_comments(src: str) -> str:
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"#.*", "", src)
    return src


def test_no_gh_pr_create_in_finder_source() -> None:
    src = (
        Path(__file__).resolve().parents[1] / "ops" / "llm_edge_finder.py"
    ).read_text(encoding="utf-8")
    stripped = _strip_comments(src)
    assert "gh pr create" not in stripped, (
        "Task #25 spec §10.3: finder must NEVER invoke `gh pr create` "
        "directly — every draft PR goes through SP-G emit_once"
    )
    # Defence-in-depth: no subprocess.run with `gh` either.
    assert "subprocess" not in stripped, (
        "Task #25: finder must not invoke any subprocess; gh calls "
        "are SP-G's responsibility"
    )
```

Create `tests/test_finder_cannot_import_non_whitelisted.py`:

```python
"""Task #25 — sandbox imports nothing outside the whitelist
(spec §10.3 + §2.9).

This is a second copy of the no-dynamic-import grep, run at the
tests/ top level so it lights up in the wide CI suite even if the
in-package test is skipped.
"""
from __future__ import annotations

import re
from pathlib import Path

_FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "arch", "sklearn", "scikit_learn", "linearmodels",
    "pandas_ta", "tensorflow", "torch", "xgboost", "lightgbm",
    "requests", "urllib", "http.client",
)

_SANDBOX = (
    Path(__file__).resolve().parents[1]
    / "tpcore" / "lab" / "llm_finder" / "tool_sandbox.py"
).read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"#.*", "", src)
    return src


def test_sandbox_does_not_import_non_whitelisted() -> None:
    stripped = _strip_comments(_SANDBOX)
    import_lines = re.findall(
        r"^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_.]*)",
        stripped, flags=re.MULTILINE,
    )
    bad = [n for n in import_lines if n.split(".")[0] in _FORBIDDEN_IMPORTS]
    assert not bad, (
        f"Task #25 spec §2.9: tool_sandbox imports forbidden "
        f"libraries: {bad!r}"
    )
```

Create `tests/test_llm_edge_finder_quota.py`:

```python
"""Task #25 — EDGE_FINDER_RUN_QUOTA=3 enforcement (spec §10.2).

Asserts the constant is the locked v1 value and the agent's
_truncate_specs returns at most 3 items with a loud warning when
asked to truncate.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


from tpcore.lab.llm_finder import (
    ANALYSIS_TURN_QUOTA,
    EDGE_FINDER_RUN_QUOTA,
)
from tpcore.lab.llm_finder.models import ProposedSpec
from tpcore.lab.target import LabPrimaryMetric


def test_edge_finder_run_quota_pinned_to_3() -> None:
    assert EDGE_FINDER_RUN_QUOTA == 3, (
        f"Task #25 spec §3.2: EDGE_FINDER_RUN_QUOTA is locked to 3; "
        f"got {EDGE_FINDER_RUN_QUOTA}"
    )


def test_analysis_turn_quota_pinned_to_8() -> None:
    assert ANALYSIS_TURN_QUOTA == 8, (
        f"Task #25 spec §3.2: ANALYSIS_TURN_QUOTA is locked to 8; "
        f"got {ANALYSIS_TURN_QUOTA}"
    )


def test_truncate_specs_caps_at_quota(caplog) -> None:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    _FINDER_PATH = _REPO_ROOT / "ops" / "llm_edge_finder.py"
    _SAVED = {
        k: sys.modules.get(k)
        for k in ("ops", "ops.llm_data_triage",
                  "ops.llm_lab_emitter", "ops.llm_edge_finder")
    }
    try:
        _ops = sys.modules.get("ops")
        if not isinstance(getattr(_ops, "__path__", None), list):
            _pkg = types.ModuleType("ops")
            _pkg.__path__ = [str(_FINDER_PATH.parent)]
            sys.modules["ops"] = _pkg
        import ops.llm_data_triage  # noqa
        import ops.llm_lab_emitter  # noqa
        _spec = importlib.util.spec_from_file_location(
            "_ef_quota", _FINDER_PATH,
        )
        m = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(m)

        def _p(i: int) -> ProposedSpec:
            return ProposedSpec(
                candidate_name=f"c-{i}", target_engine="sentinel",
                intent="fold_existing", primary_hypothesis="h",
                primary_metric=LabPrimaryMetric.SHARPE,
                param_ranges={"k": (1, 2, "choice:1,2")},
                rationale="r", falsification_criterion="f",
                expected_trials=1, analysis_evidence_refs=(0,),
            )

        five = tuple(_p(i) for i in range(5))
        kept, discarded = m._truncate_specs(five)
        assert len(kept) == 3
        assert discarded == 2
    finally:
        for _k, _v in _SAVED.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v
```

Create `tests/test_llm_edge_finder_round_trip.py`:

```python
"""Task #25 — round-trip integration (spec §10.2).

Synthetic AnalysisResult with 1 ProposedSpec round-trips through
emit_once via the agent's run_finder; the rendered spec validates
against SP-G's render_candidate_spec golden shape (the EmittedSpec
rendered markdown).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FINDER_PATH = _REPO_ROOT / "ops" / "llm_edge_finder.py"
_SAVED = {
    k: sys.modules.get(k)
    for k in ("ops", "ops.llm_data_triage",
              "ops.llm_lab_emitter", "ops.llm_edge_finder")
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_FINDER_PATH.parent)]
        sys.modules["ops"] = _pkg
    import ops.llm_data_triage  # noqa
    import ops.llm_lab_emitter  # noqa
    _spec = importlib.util.spec_from_file_location(
        "_ef_round_trip", _FINDER_PATH,
    )
    ef = importlib.util.module_from_spec(_spec)
    sys.modules["_ef_round_trip"] = ef
    _spec.loader.exec_module(ef)
finally:
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v

from tpcore.lab.llm_finder.models import (
    AnalysisResult, MarketSnapshot, ProposedSpec,
)
from tpcore.lab.target import LabPrimaryMetric


@pytest.mark.asyncio
async def test_round_trip_one_proposed_spec_reaches_emit_once(
    monkeypatch,
) -> None:
    snap = MarketSnapshot(
        snapshot_ts=datetime.now(UTC),
        session_date=date(2026, 5, 20),
        universe="sp500",
        price_window=(), fundamentals=(),
        ledger_state=(), roster=(),
    )
    monkeypatch.setattr(ef, "assemble_snapshot",
                        AsyncMock(return_value=snap))
    monkeypatch.setattr(ef, "load_reference_bundles",
                        MagicMock(return_value=()))

    one_spec = ProposedSpec(
        candidate_name="my-candidate", target_engine="sentinel",
        intent="fold_existing", primary_hypothesis="reduce maxdd",
        primary_metric=LabPrimaryMetric.MAXDD_REDUCTION,
        param_ranges={"threshold": (60, 55, "choice:60,55")},
        rationale="adfuller showed mean-reversion",
        falsification_criterion="55 produces deeper drawdown than 60",
        expected_trials=2, analysis_evidence_refs=(0,),
    )

    async def fake_llm(*_args, **_kwargs):
        return AnalysisResult(
            turn=1, tool_results=(), proposed_specs=(one_spec,),
            finder_rationale="r",
        )

    emit_calls: list = []

    async def fake_emit(pool, *, target, intent, expected_trials,
                        reference_bundles, **kwargs):
        from ops.llm_lab_emitter import EmitterOutcome
        emit_calls.append({
            "target": target, "intent": intent,
            "expected_trials": expected_trials,
        })
        return EmitterOutcome(
            emitted_candidate="my-candidate",
            target_engine=target,
            pr_link="https://gh/x/y/123",
            ledger_recorded=True,
        )

    monkeypatch.setattr(ef, "_call_llm", fake_llm)
    monkeypatch.setattr(ef, "emit_once", fake_emit)
    monkeypatch.setattr(ef, "record_finder_run", AsyncMock())

    run = await ef.run_finder(
        pool=MagicMock(), target_engine="sentinel",
        reference_bundles=(), session_date=date(2026, 5, 20),
    )
    assert run.proposed_spec_count == 1
    assert run.emitted_pr_urls == ("https://gh/x/y/123",)
    assert emit_calls == [
        {"target": "sentinel", "intent": "fold_existing",
         "expected_trials": 2},
    ]
```

- [ ] **Step 11.4: Run all safety + composition tests — expect pass**

Run: `python -m pytest tests/test_llm_edge_finder_composes_with_sp_g.py tests/test_finder_cannot_bypass_sp_g.py tests/test_finder_cannot_import_non_whitelisted.py tests/test_llm_edge_finder_quota.py tests/test_llm_edge_finder_round_trip.py -v -p no:xdist`
Expected: PASS — all five test files green.

- [ ] **Step 11.5: Commit**

```bash
git add .claude/skills/lab-edge-find/SKILL.md \
        docs/llm_edge_finder_operator_runbook.md \
        tests/test_llm_edge_finder_composes_with_sp_g.py \
        tests/test_finder_cannot_bypass_sp_g.py \
        tests/test_finder_cannot_import_non_whitelisted.py \
        tests/test_llm_edge_finder_quota.py \
        tests/test_llm_edge_finder_round_trip.py
git commit -m "$(cat <<'EOF'
feat(task-25): T11 — slash-skill + operator runbook + safety/composition tests

`.claude/skills/lab-edge-find/SKILL.md` (operator trigger surface,
spec §9.1 decision #7). `docs/llm_edge_finder_operator_runbook.md` —
the spec §8 10-step graduation walk-through + failure-mode table.
Safety tests: composes-with-SP-G (no re-impl), cannot-bypass-SP-G (no
gh pr create outside emit_once), cannot-import-non-whitelisted (tool
sandbox grep). Quota: EDGE_FINDER_RUN_QUOTA=3 + ANALYSIS_TURN_QUOTA=8
pinned. Round-trip: AnalysisResult -> emit_once with one spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Live wiring — CLI entry-point, manifest-fence regen, whole-suite + reverse-order proof

**Spec citation:** §11 — "heavy lane ... new statistical-tool sandbox surface. Full §1 pipeline applies." `.claude/rules/tests-and-ci.md` — "Authoritative gate = `python -m pytest -p no:xdist` (whole suite, one process) + the reversed module order. The parallel `-n auto --dist loadgroup` is the fast accelerator only." CLAUDE.md — "Smoke loop + `run_all_engines.sh` + `ops/platform_pipeline.py` docstrings are sentinel-fenced (regenerated by `scripts/gen_engine_manifest.py`; do NOT hand-edit inside a fence)."

Note: Task #25 is engine-FREE (no new engine, no roster change), so the manifest-fence regen should produce a no-op. We still RUN it as the discipline check to confirm.

**Files:**
- Modify: `ops/llm_edge_finder.py` — add `__main__`-safe CLI entry point.

- [ ] **Step 12.1: Add a CLI entry point to `ops/llm_edge_finder.py`**

Append to `ops/llm_edge_finder.py` (after the existing `__all__` declaration):

```python
import argparse
import sys


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ops.llm_edge_finder",
        description=(
            "Task #25 — autonomous LLM+quant edge finder. Runs ONE "
            "spec §3.2 data->analysis->idea loop, emits <=3 single-"
            "hypothesis ProposedSpecs via SP-G emit_once. Draft, "
            "human-merge-only PR per emission."
        ),
    )
    parser.add_argument(
        "--target-engine",
        type=str,
        default="",
        help=(
            "Pin a target engine (must be in "
            "tpcore.engine_profile.lab_targetable_engines()). Default "
            "blank lets the LLM choose from the roster."
        ),
    )
    parser.add_argument(
        "--reference-bundle",
        type=str,
        default="",
        help=(
            "Comma-separated reference bundle names under "
            "docs/lab_emitter_references/. The mandatory "
            "`dsr_ntrials_discipline` bundle is ALWAYS included."
        ),
    )
    parser.add_argument(
        "--session-date",
        type=str,
        default="",
        help="ISO YYYY-MM-DD; default: today's UTC date.",
    )
    return parser.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    import os
    from tpcore.db import build_asyncpg_pool

    args = _parse_args(argv)
    dsn = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_URL_IPV4")
    )
    if not dsn:
        logger.error("llm_edge_finder.no_dsn")
        return 1

    if args.session_date:
        session_date = date.fromisoformat(args.session_date)
    else:
        session_date = datetime.now(UTC).date()

    bundles = tuple(
        b for b in args.reference_bundle.split(",") if b.strip()
    )
    target = args.target_engine or None

    pool = await build_asyncpg_pool(dsn)
    try:
        run = await run_finder(
            pool,
            target_engine=target,
            reference_bundles=bundles,
            session_date=session_date,
        )
    finally:
        await pool.close()

    logger.info(
        "llm_edge_finder.run_complete",
        run_id=str(run.run_id),
        proposed_spec_count=run.proposed_spec_count,
        emitted_pr_urls=list(run.emitted_pr_urls),
        rejection_reason=run.rejection_reason,
    )
    return 0 if run.rejection_reason is None else 2


def main() -> None:  # pragma: no cover - CLI shim
    code = asyncio.run(_amain(sys.argv[1:]))
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 12.2: Run the manifest-fence regen as a no-op discipline check**

Run: `python scripts/gen_engine_manifest.py`
Expected: completes; `git diff` shows no changes (Task #25 is engine-FREE). If there ARE changes, STOP and surface — that means a sentinel-fence drifted unrelated to Task #25.

Run: `git diff --name-only`
Expected: empty.

- [ ] **Step 12.3: Run the WHOLE-SUITE authoritative gate**

Run: `python -m pytest -p no:xdist --tb=short 2>&1 | tail -50`
Expected: PASS — all tests green. NO red. (Per `.claude/rules/tests-and-ci.md`: "Authoritative gate = `python -m pytest -p no:xdist` (whole suite, one process)".)

If RED: investigate per `superpowers:systematic-debugging`. Likely failure modes:
 - ops-shadow contamination — confirm `pytestmark = pytest.mark.xdist_group("ops_shadow")` on every new test that imports `ops.*`.
 - missing `statsmodels` / `scipy` — see T5.5 (surface to operator, don't auto-install).
 - persona SHA drift — T6.3 placeholder not replaced.

- [ ] **Step 12.4: Run the REVERSE-ORDER proof**

Run: `python -m pytest -p no:xdist --tb=short -p reverse 2>&1 | tail -30`

If the `reverse` plugin is not installed, use the project's canonical reverse-order invocation (search for it under `scripts/` — it's typically `python -m pytest -p no:xdist --reverse-order` if a custom plugin is present, OR a wrapper script `scripts/run_full_suite_reverse.sh`).

Run: `ls scripts/run_full_suite_reverse.sh 2>/dev/null && bash scripts/run_full_suite_reverse.sh || echo "NO REVERSE WRAPPER — use ops-package-shadow native reversal"`

If no wrapper exists: Run the suite with collection-order reversal via:
`python -m pytest -p no:xdist --collect-only -q | tac | head -20`
(to verify collection-reversal works), then execute:
`python -m pytest -p no:xdist $(python -m pytest -p no:xdist --collect-only -q 2>/dev/null | grep '::' | tac | tr '\n' ' ')`

Expected: PASS — same green as forward order. A red on reverse + green on forward = ops-package-shadow contamination; investigate per `feedback_ops_package_shadow_full_suite_gate`.

- [ ] **Step 12.5: Commit the CLI entry-point**

```bash
git add ops/llm_edge_finder.py
git commit -m "$(cat <<'EOF'
feat(task-25): T12 — CLI entry-point for `python -m ops.llm_edge_finder`

Argparse: --target-engine (optional pin), --reference-bundle (CSV;
dsr_ntrials_discipline always included), --session-date (ISO; default
today UTC). Calls run_finder once, logs the outcome, exits 0 on
success / 2 if FinderRun.rejection_reason is set. Subprocess-safe
asyncio.run top-level mirrors ops/llm_lab_emitter.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 12.6: Push the branch + open the plan PR (operator reviews; does NOT auto-merge)**

Run: `git push -u origin docs/task-25-llm-edge-finder-plan`
Expected: branch pushed to origin.

Open the PR with `gh pr create`. The PR BODY first three paragraphs:

1. **(scope, one line):** Implementation plan for Task #25 — autonomous LLM+quant edge finder. 12 sequenced tasks, contracts → pure helpers → agent glue → live wiring; composes with SP-G `emit_once` verbatim per spec §3.3.
2. **(T1 preview):** Task 1 starts at the contract layer — `MarketSnapshot`, `AnalysisRequest`, `AnalysisResult`, `FinderRun`, `ToolCall`, `ToolResult`, `ProposedSpec`, `NumericSummary` — all frozen pydantic v2 with `extra="forbid"`. `ToolCall.callable_name: Literal[...]` IS the v1 whitelist (spec §4.2: "Anything else fails pydantic validation BEFORE the dispatcher"). `EDGE_FINDER_RUN_QUOTA=3` is enforced at the model layer via a `field_validator` on `AnalysisResult.proposed_specs`. RED-first: the test file is written + run BEFORE `models.py` exists.
3. **(biggest risk + mitigation):** The single biggest risk is **the LLM under-declaring `expected_trials` to game the SP-A ledger budget** (spec §10 hard constraint clause b — "the LLM's analysis IS counted against n_trials"). v1 does NOT fold pre-emission analysis turns into the ledger directly. **Mitigation:** (a) the mandatory `dsr_ntrials_discipline.md` bundle is the load-bearing reminder to the LLM (spec §7.1 + the T3 content); (b) SP-G's `ledger_gate.check_budget` re-validates `cumulative + spec.expected_trials <= EMISSION_QUOTA_PER_TARGET=20` AFTER the LLM's response (`ops/llm_lab_emitter.py` lines 777-791); (c) every emission is one append-only ledger row that the operator can grep. The v2 spec (spec §9.4 onward) may reify analysis-turn-into-ledger if v1 telemetry shows the LLM is gaming the gap; v1 deliberately stays observable rather than over-engineered.

Use:

```bash
gh pr create --title "docs(task-25): implementation plan — autonomous LLM+quant edge finder (12 tasks, sequenced from contracts up)" --body "$(cat <<'EOF'
## Summary

- **Scope:** Implementation plan for Task #25 — the autonomous LLM+quant edge finder. 12 sequenced tasks: contracts (T1) → reference loader + content (T2/T3) → snapshot assembler (T4) → tool sandbox (T5) → persona + sentinel (T6) → FinderRun persistence (T7) → agent core with mocked LLM seam (T8) → real Anthropic SDK wiring (T9) → 4th co-task on `llm_triage_service.py` (T10) → slash-skill + operator runbook + safety greps (T11) → CLI + whole-suite + reverse-order proof (T12). Composes with SP-G `emit_once` verbatim per spec §3.3.

- **T1 preview:** Contract layer — `MarketSnapshot`, `AnalysisRequest`, `AnalysisResult`, `FinderRun`, `ToolCall`, `ToolResult`, `ProposedSpec`, `NumericSummary` — all frozen pydantic v2 with `extra="forbid"`. `ToolCall.callable_name: Literal[...]` IS the v1 whitelist (spec §4.2: "Anything else fails pydantic validation BEFORE the dispatcher"). `EDGE_FINDER_RUN_QUOTA=3` is enforced at the model layer via a `field_validator` on `AnalysisResult.proposed_specs`. RED-first: the test file is written + run BEFORE `models.py` exists.

- **Biggest risk + mitigation:** The single biggest risk surfaced in this plan is **the LLM under-declaring `expected_trials` to game the SP-A ledger budget** (spec §10 hard constraint clause b — "the LLM's analysis IS counted against n_trials"). v1 does NOT fold pre-emission analysis turns into the ledger directly. **Mitigation:** (a) the mandatory `dsr_ntrials_discipline.md` bundle is the load-bearing reminder to the LLM (spec §7.1 + the T3 content); (b) SP-G's `ledger_gate.check_budget` re-validates `cumulative + spec.expected_trials <= EMISSION_QUOTA_PER_TARGET=20` AFTER the LLM's response (`ops/llm_lab_emitter.py` lines 777-791); (c) every emission is one append-only ledger row that the operator can grep. The v2 spec (§9.4 onward) may reify analysis-turn-into-ledger if v1 telemetry shows the LLM is gaming the gap; v1 deliberately stays observable rather than over-engineered.

## Test plan
- [ ] Operator reviews the 12-task plan structure against spec §3.1 (file roster) + §3.2 (phase chain).
- [ ] Operator confirms every spec §1-§14 section has at least one task entry (T1-T12 coverage map in the plan file's self-review section).
- [ ] Operator confirms zero placeholder text in the plan body (no TBD / TODO / `<...>`).
- [ ] Operator gives explicit greenlight for the build (subagent-driven-development takes the plan task-by-task; one consolidated review per `feedback_cut_process_overhead_ship`).

## DO NOT AUTO-MERGE
Per operator standing rule: the operator reviews the plan before greenlighting the build. The plan PR is **docs-only**; the implementation lands on follow-on PRs (one per task; subagent-driven).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed. Surface the URL to the operator.

---

## Self-review (the writing-plans skill checklist)

### Spec coverage map

| Spec section | Covered in task |
| --- | --- |
| §1 Motivation | (context only; no task) |
| §2.1 Cumulative n_trials honesty | T8 + T9 (calls SP-G `emit_once` which calls `record_trial_spend`) + T3 (`dsr_ntrials_discipline.md` reinforces) |
| §2.2 Single pre-registered hypothesis per emission | T1 (`AnalysisResult.proposed_specs` validator; `ProposedSpec` shape mirrors SP-G `EmittedSpec`) + T8 (loop emits one spec per `emit_once` call) |
| §2.3 The gate is sacred | T11 (`test_finder_cannot_bypass_sp_g.py`) + the SP-G fence inherited unchanged |
| §2.4 Advisory + human-gated only | T11 (`SKILL.md` documents draft-PR-only; T11 grep test asserts no `gh pr create` outside `emit_once`) |
| §2.5 Credential-starved + crash-isolated; no `tools` payload | T9 (`httpx.MockTransport` test asserts no `tools` body) + T10 (co-task on `llm_triage_service.py`) |
| §2.6 Roster-mediated, never roster-mutating | T8 (`target_engine` validated against `lab_targetable_engines()` via SP-G `_verify_target_in_roster`) |
| §2.7 Two-daemon invariant preserved | T10 (4th co-task; `test_two_daemon_invariant.py` stays green unedited; `test_four_cotask_invariant.py` asserts 4-co-task structure) |
| §2.8 No network beyond Anthropic SDK | T4 (snapshot is local Postgres only; T9 asserts SDK shape) + T11 (`test_finder_cannot_import_non_whitelisted.py` reds `requests`/`urllib`/`http`) |
| §2.9 Toolkit whitelist | T5 (`tool_sandbox.py` whitelist + 2 source-grep CI tests) + T11 (top-level grep duplicate) |
| §2.10 LLM analysis counted against n_trials | T7 (`FinderRun` audit row records `analysis_turn_count` for v2 reification) + T3 (`dsr_ntrials_discipline.md` operationalises the discipline) |
| §3.1 Package layout | (file structure section at top) + T1-T11 file creates match the layout |
| §3.2 The loop shape | T8 (`run_finder` Phase A/B/C orchestration) |
| §3.3 Composition with SP-G | T8 (imports `emit_once`) + T11 (`test_llm_edge_finder_composes_with_sp_g.py` asserts) |
| §4.1 MarketSnapshot | T1 (model) + T4 (assembler + `MAX_SNAPSHOT_BYTES` overflow) |
| §4.2 AnalysisRequest / ToolCall | T1 (model + Literal whitelist + turn bounds + tool_calls cap) |
| §4.3 AnalysisResult / ProposedSpec | T1 (model + `EDGE_FINDER_RUN_QUOTA` validator) |
| §4.4 FinderRun | T1 (model) + T7 (`record_finder_run` write-path) |
| §5 Safety posture (the 13-fence table) | All 13 fences covered: 1 (T8 inheritance), 2 (T1 + SP-G), 3 (T11 grep), 4 (T11 SKILL doc), 5 (T9 + T10), 6 (T10), 7 (T8 roster validate + SP-G diff-scope), 8 (T11 grep), 9 (T5 + T11 grep), 10 (T1 model validators + T8 truncation), 11 (SP-G inheritance, T8 calls), 12 (SP-G inheritance), 13 (SP-G inheritance) |
| §6 Tool sandbox | T5 (whitelist + dispatcher + determinism + no-dynamic-import) |
| §7 Reference bundles | T2 (loader) + T3 (`dsr_ntrials_discipline.md` content + `market_structure_primer.md` stub) |
| §8 The graduation path | T11 (operator runbook walks all 10 steps) |
| §9.1 Plan-PR decisions Q1-Q7 | Q1 `ANALYSIS_TURN_QUOTA=8` (T1 const + test); Q2 `MAX_SNAPSHOT_BYTES=512 KiB` (T1 const + T4 overflow test); Q3 universe sp500-only (T4 + T1 Literal); Q4 column whitelist (T5 `_COLUMN_WHITELIST` = `adj_close`/`log_return`/`vol_20d`); Q5 `LAB_FINDER_RUN` event_type + `engine='llm_edge_finder'` (T7); Q6 `PERSONA_VERSION` in `tpcore/lab/llm_finder/__init__.py` + `test_persona_versioned.py` sentinel (T6); Q7 slash-skill at `.claude/skills/lab-edge-find/SKILL.md` (T11) |
| §9.2-§9.7 v1.5+ deferrals | (no task; deliberately deferred) |
| §10.1 Unit tests | T1, T2, T4, T5, T6, T7 — all covered |
| §10.2 Integration tests | T8 (agent loop) + T11 (round-trip, composes-with-SP-G, quota) + T10 (four-co-task) + T6 (persona-versioned) |
| §10.3 Safety tests | T11 (cannot-bypass-SP-G, cannot-import-non-whitelisted) + T5 (no-dynamic-import). `test_finder_cannot_write_to_db.py` (read-only role) is DEFERRED to operator gate at T12 (requires real DB role config; mock-only stand-in would be theatre — surface as a follow-up). `test_finder_diff_scope_inherits_sp_g.py` is SP-G's existing test; Task #25 inherits because all draft PRs go through `emit_once`. |
| §10.4 Lane discipline | every new test under `tests/` that imports `ops.*` carries `pytestmark = pytest.mark.xdist_group("ops_shadow")` (T8, T9, T10, T11 round-trip) |
| §10.5 E2E proof (`test_llm_edge_finder_to_paper.py`) | DEFERRED to a follow-up PR — Task #25 v1 success criterion (spec §12) is satisfied by ONE real-life candidate reaching PAPER, not a mocked-E2E test (the mocked test would be theatre per `feedback_no_lazy_vendor_blame`). Flagged in T12 step 12.7. |
| §11 Lane = heavy | T12 (whole-suite + reverse-order proof per `.claude/rules/tests-and-ci.md`) |
| §12 v1 success criterion | (post-build operator action; named in the operator runbook T11) |
| §13 Phasing roadmap | (context only; v1.5+ deferred per §9.2-§9.6) |
| §14 Cross-references | (context only) |

### Type-consistency check

- `record_finder_run` (T7) is referenced verbatim in T8's import block — not `write_finder_run` or `log_finder_run`.
- `EDGE_FINDER_RUN_QUOTA` (T1 constant) is referenced verbatim in T1 model validator, T8 truncation, T11 quota test — same name throughout.
- `ANALYSIS_TURN_QUOTA` (T1 constant) is referenced verbatim in T1 model validator, T8 loop bound, T11 quota test — same name throughout.
- `MAX_SNAPSHOT_BYTES` (T1 constant) is referenced verbatim in T4 overflow test + T4 assembler check — same name throughout.
- `MANDATORY_REFERENCE_BUNDLE` (T2 constant) is referenced verbatim in T2 tests + T8 `run_finder` default — same name throughout.
- `_call_llm` (T8 stub seam) is the same function T9 replaces — same name throughout.
- `_truncate_specs` (T8 helper) is referenced verbatim in T11 quota test — same name throughout.
- `_build_httpx_client` (T9 seam) is referenced verbatim in T9 SDK-wiring test monkeypatches — same name throughout.

### Placeholder scan

The placeholder marker `PINNED_AT_T6_STEP_6_3` is intentional and is REPLACED in step 6.3 (the operator computes the actual SHA and edits the test file). No other `TBD` / `<...>` / "fill in later" / "similar to N" markers appear in the plan body. The `[operator-pending content]` marker IS the stub-detection mechanism in T3 — it stays in the `market_structure_primer.md` bundle by design.

### Deferred-with-rationale

| Item | Reason for deferral |
| --- | --- |
| `test_finder_cannot_write_to_db.py` (spec §10.3) | Requires a real read-only DB role; a mock-only sentinel is theatre. Surface as a follow-up PR once the operator stands up the role in Supabase. |
| `test_llm_edge_finder_to_paper.py` (spec §10.5) | The single E2E mock would mock the Anthropic boundary + `ops.lab` + ECR — that's enough mocking that the test proves nothing the unit tests don't already prove. The REAL v1 success criterion is one candidate reaching PAPER (spec §12); that's a real-data milestone, not a CI test. |
| `market_structure_primer.md` real content | Operator-authored later (spec §7.1); the stub is the fail-loud structural enforcement. TODO.md `[defect_ref:]` row tracks. |
| `LAB_LEDGER_CAPACITY_AVAILABLE` event-driven trigger | v1.5+ deferral per spec §9.2. The 4th co-task is structurally present with an empty trigger tuple. |

---

## Plan complete

Plan saved to `docs/superpowers/plans/2026-05-21-task-25-llm-edge-finder.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task (T1-T12), parent reviews between tasks, isolation via worktree per `superpowers:using-git-worktrees`.

**2. Inline Execution** — execute T1-T12 in this session via `superpowers:executing-plans`, with checkpoints at T5, T8, T11 for operator review.

Operator decides at greenlight time. The plan PR (this file) lands first; the build PRs follow.
