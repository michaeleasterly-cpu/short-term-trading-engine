"""SP-G — Lab spec-emitter agent (thin advisory LLM spec-emitter).

The Anthropic-SDK-calling agent that produces one Lab candidate spec
per ``/lab-spec-emit`` invocation. Advisory + human-gated only:
draft PR + human-merge-only; the deterministic gate (`DSR >= 0.95 AND
credibility >= 60`) is sacred; the roster (`tpcore.engine_profile.
_PROFILE`) is never mutated; the diff-scope fence reds the build on
any over-broad PR.

Spec: ``docs/superpowers/specs/2026-05-20-lab-sp-g-llm-spec-emitter-
design.md`` (operator-confirmed §10 Q1-Q6 decisions baked in:
quota=20, `--reference-bundle <name>` staging, SP-E spec landing
path, `/lab-spec-emit` skill name, ``LAB_LEDGER_CAPACITY_AVAILABLE``
event class deferred).

Strict emission sequence (spec §3.4) — IMMUTABLE STEP ORDER:

1. ``ledger_gate.check_budget(target)`` — reject if over-budget.
2. Build ``EmissionContext`` (roster + ledger + references + persona).
3. Invoke the Anthropic SDK (no ``tools``, no network beyond the SDK
   call).
4. Validate the response against ``EmittedSpec`` (pydantic v2 frozen +
   ``extra="forbid"``).
5. ``record_trial_spend(...)`` — the SP-A ledger row is written
   **before** the draft PR is opened.
6. Render the markdown spec + JSON sidecar; ``enforce_diff_scope``;
   ``validate_no_gate_override``; ``gh pr create --draft``.

If step 6 fails after step 5 succeeds, the ledger row stands — by
design (spec §3.4); see ``docs/runbooks/lab-spec-emit-orphaned-spend.md``.

Reuse posture (2026-05-22 update — LLM triage REMOVED entirely per
operator directive "we aren't going to use the llm triage... take it
out"): the credential-starved sandbox + draft-PR machinery used to
re-use the SHIPPED ``ops.llm_data_triage`` public shared-SDK surface
(``ANTHROPIC_MODEL``, ``ANTHROPIC_MAX_TOKENS``, ``default_pr_runner``,
``AuthSkip``). Since the LLM triage modules have been DELETED, those
helpers are now defined directly in THIS module. SP-G is the sole
remaining user of the helpers in ``ops/`` (task #25 finder modules
have their own copy in ``ops/llm_edge_finder_sdk.py``).

Safety: the deterministic fences (the diff-scope allow-list, the
gate-override grep, the ledger pre-emission budget) are the
boundary, NOT this module. The persona text governs OUTPUT QUALITY
only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from anthropic import (
    APIError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from tpcore.lab.ledger import record_trial_spend
from tpcore.lab.llm_emitter.diff_fence import (
    DiffScopeViolation,
    enforce_diff_scope,
)
from tpcore.lab.llm_emitter.emitter import (
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
from tpcore.outage import with_retry

logger = structlog.get_logger(__name__)


# ─── Shared-SDK surface (inlined 2026-05-22) ───────────────────────────
# These helpers used to live on ``ops.llm_data_triage``'s public surface
# and were re-exported via the lazy ``_shipped()`` accessor. After the
# 2026-05-22 operator directive ("we aren't going to use the llm
# triage... take it out") removed the entire LLM-triage stack, the
# helpers are inlined directly here. SP-G owns these now.

# Default Anthropic SDK params (formerly imported as ANTHROPIC_MODEL /
# ANTHROPIC_MAX_TOKENS).
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_MAX_TOKENS = 2048


class AuthSkip(Exception):
    """Signals that the Anthropic API key is invalid/exhausted
    (AuthenticationError). Treated identically to a missing key:
    safe no-op, zero retries, zero emits."""


# Credential-starved sandbox env allowlist (formerly ``scrubbed_env``).
# Kept here for parity with the inlined surface even though SP-G itself
# does not invoke a local gate today — future emitter additions may.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG")
_ENV_ALLOWLIST_PREFIXES = ("PYTHON",)


def scrubbed_env() -> dict[str, str]:
    """A fresh dict of ONLY the allowlisted vars from os.environ.

    Built additively from an allowlist — a forbidden var (a *KEY*, a
    *TOKEN*, any *DATABASE_URL*, ANTHROPIC*/ALPACA*/SUPABASE*) is never
    even read into the result, so it CANNOT leak. This is the
    credential-starve guarantee for any local sandbox gate."""
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in _ENV_ALLOWLIST or any(
            k.upper().startswith(p) for p in _ENV_ALLOWLIST_PREFIXES
        ):
            env[k] = v
    return env


def _default_pr_runner(
    argv: list[str], *, env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Default ``pr_runner`` — runs one git/gh command. Returns
    (returncode, stdout, stderr). Tests inject a fake; this is never
    exercised in CI. ``env`` is passed verbatim to the child (the
    scrubbed allowlist dict for the gate; None ⇒ inherit for plain
    git/gh metadata calls that need no secrets)."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ─── Persona + constants ───────────────────────────────────────────────


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PERSONA_PATH = _REPO_ROOT / "docs" / "lab_emitter_persona.md"
_REFERENCES_DIR = _REPO_ROOT / "docs" / "lab_emitter_references"
_READINESS_CHECKLIST_PATH = (
    _REPO_ROOT / "docs" / "superpowers" / "checklists"
    / "lab_candidate_readiness.md"
)
_AGENT_ENGINE_TAG = "llm_lab_emitter"

# Persona may be absent in the dark-landing v1 cycle (the operator may
# author it in a follow-up edit). The agent FAILS LOUD if the persona
# is missing AND a non-replay invocation requires it.
_PERSONA_TEXT: str = (
    _PERSONA_PATH.read_text(encoding="utf-8") if _PERSONA_PATH.exists() else ""
)


def _persona_sha() -> str:
    """SHA-256 of the persona text (first 12 hex chars) — used as the
    ledger ``source`` provenance prefix and as the
    ``EmissionContext.persona_version`` value."""
    return hashlib.sha256(_PERSONA_TEXT.encode("utf-8")).hexdigest()[:12]


def _readiness_version() -> str:
    """SHA-256 of the Readiness checklist text (first 12 hex) — surfaced
    to the LLM in ``EmissionContext.readiness_checklist_version`` so the
    persona can fail-loud on a checklist drift."""
    if not _READINESS_CHECKLIST_PATH.exists():
        return "unknown"
    blob = _READINESS_CHECKLIST_PATH.read_bytes()
    return hashlib.sha256(blob).hexdigest()[:12]


# Byte-mirror the data-lane / engine-lane INSERT SQL convention.
_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit_event(
    pool: Any,
    event_type: str,
    message: str,
    data: dict[str, Any],
    *,
    severity: str = "INFO",
) -> None:
    """One advisory application_log event (the operator audit trail)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL,
            _AGENT_ENGINE_TAG,
            uuid.uuid4(),
            event_type,
            severity,
            message,
            json.dumps(data, default=str),
        )


# ─── Outcome ──────────────────────────────────────────────────────────


@dataclass
class EmitterOutcome:
    """Structured result of one emission pass."""

    emitted_candidate: str | None = None
    target_engine: str | None = None
    pr_link: str | None = None
    skipped_no_key: bool = False
    skipped_no_budget: bool = False
    error: str | None = None
    ledger_recorded: bool = False
    notes: list[str] = field(default_factory=list)


# ─── Default Anthropic client ──────────────────────────────────────────


def _default_client() -> AsyncAnthropic:
    """The official ASYNC SDK client (mirrors data/engine triage)."""
    return AsyncAnthropic()


# ─── Reference bundle loading ──────────────────────────────────────────


def _load_reference_bundles(names: tuple[str, ...]) -> tuple[ReferenceExcerpt, ...]:
    """Q3 (operator-confirmed): per-emission ``--reference-bundle <name>``
    argument naming a curated subset under
    ``docs/lab_emitter_references/<name>.md``. Each named bundle becomes
    one ``ReferenceExcerpt``. A missing bundle file raises (fail-loud at
    the operator command path; the agent does NOT silently fall back to
    a different bundle)."""
    excerpts: list[ReferenceExcerpt] = []
    for name in names:
        if not name:
            continue
        # Defence: a path traversal in the bundle name is rejected
        # because ``ReferenceExcerpt.name`` requires the slug shape
        # ``^[a-z][a-z0-9_-]+$``. The file lookup uses the validated
        # name; ``/`` cannot appear in a valid name.
        path = _REFERENCES_DIR / f"{name}.md"
        if not path.is_file():
            available = sorted(
                p.stem for p in _REFERENCES_DIR.glob("*.md")
            ) if _REFERENCES_DIR.exists() else []
            raise FileNotFoundError(
                f"reference bundle {name!r} not found at {path}; "
                f"available bundles: {available!r}"
            )
        text = path.read_text(encoding="utf-8")
        excerpts.append(ReferenceExcerpt(name=name, text=text))
    return tuple(excerpts)


# ─── Roster snapshot (read-only) ───────────────────────────────────────


def _roster_snapshot() -> tuple[RosterTarget, ...]:
    """Build the SP-B-derived roster targets the LLM may name. Imports
    the engine package's ``backtest.LAB_TARGET`` lazily — only the
    engines that declare one are surfaced. An engine that lacks a
    ``LAB_TARGET`` is silently omitted (the operator's roster-side
    addition is the canonical fix, NOT the agent's responsibility)."""
    # Lazy import to keep module-load engine-independent (the
    # check_imports invariant: tpcore never imports an engine, and the
    # ops/ agent only imports an engine at CALL time).
    from tpcore.engine_profile import _PROFILE, lab_targetable_engines

    targets: list[RosterTarget] = []
    for name in lab_targetable_engines():
        profile = _PROFILE[name]
        lifecycle = profile.lifecycle_state.value
        # Try to import the engine's LAB_TARGET; skip on failure (a
        # PAPER engine that has not yet declared its LAB_TARGET is the
        # SP-B / SP-E precondition — it's a known visible gap).
        try:
            mod = __import__(f"{name}.backtest", fromlist=["LAB_TARGET"])
            lab_target = getattr(mod, "LAB_TARGET", None)
        except (ImportError, AttributeError):
            lab_target = None
        if lab_target is None:
            continue
        targets.append(
            RosterTarget(
                name=name,
                lifecycle_state=lifecycle,  # type: ignore[arg-type]
                primary_metric=lab_target.primary_metric,
                declared_param_ranges=dict(lab_target.param_ranges),
            )
        )
    return tuple(targets)


def _verify_target_in_roster(target: str) -> RosterTarget:
    """Pre-LLM-call membership check. ``canary``, ``lab`` sentinel,
    allocator, RETIRED engines are NOT in ``lab_targetable_engines()``;
    a category-error target rejects here (no ledger spend, no Anthropic
    round-trip)."""
    roster = _roster_snapshot()
    for entry in roster:
        if entry.name == target:
            return entry
    available = [e.name for e in roster]
    raise ValueError(
        f"target engine {target!r} is not a Lab-targetable roster member; "
        f"available: {available!r}. Engine roster ADD is an operator ECR "
        f"(/ecr skill); the SP-G emitter never modifies the roster."
    )


# ─── EmissionContext build ─────────────────────────────────────────────


async def _build_emission_context(
    pool: Any,
    *,
    target: str,
    expected_trials: int,
    reference_bundles: tuple[str, ...],
    quota: int,
) -> tuple[EmissionContext, int]:
    """Assemble the input contract (spec §3.2). Returns the context AND
    the cumulative ledger count (returned so the caller can emit it as
    audit data on the application_log event)."""
    # Roster snapshot (also validates the target).
    roster = _roster_snapshot()
    _verify_target_in_roster(target)

    # Cumulative ledger state — one LedgerEntry per roster member.
    ledger_entries: list[LedgerEntry] = []
    from tpcore.lab.ledger import cumulative_n_trials as _cum

    now = datetime.now(UTC)
    target_cumulative = 0
    for entry in roster:
        cum = await _cum(pool, entry.name, now)
        ledger_entries.append(
            LedgerEntry(target=entry.name, cumulative_n_trials=cum, quota=quota)
        )
        if entry.name == target:
            target_cumulative = cum

    references = _load_reference_bundles(reference_bundles)

    ctx = EmissionContext(
        roster_targets=roster,
        ledger_state=tuple(ledger_entries),
        readiness_checklist_version=_readiness_version(),
        reference_excerpts=references,
        persona_version=_persona_sha(),
        emission_quota_remaining=max(0, quota - target_cumulative - expected_trials),
    )
    return ctx, target_cumulative


# ─── Anthropic call (mirrors the shipped envelope) ─────────────────────


def _build_prompt(ctx: EmissionContext, target: str, intent: str) -> str:
    """Build the user-message text for the SDK call. The LLM sees the
    full EmissionContext (as JSON) PLUS a directive naming the target
    engine + intent."""
    payload = {
        "task": "emit_lab_candidate_spec",
        "directive": (
            f"Propose ONE single-hypothesis Lab candidate against target "
            f"engine {target!r} with intent {intent!r}. Return ONLY a JSON "
            f"object matching the EmittedSpec schema (candidate_name, "
            f"target_engine, intent, primary_hypothesis, primary_metric, "
            f"param_ranges, rationale, falsification_criterion, "
            f"expected_trials)."
        ),
        "context": {
            "readiness_checklist_version": ctx.readiness_checklist_version,
            "persona_version": ctx.persona_version,
            "emission_quota_remaining": ctx.emission_quota_remaining,
            "roster_targets": [
                {
                    "name": r.name,
                    "lifecycle_state": r.lifecycle_state,
                    "primary_metric": r.primary_metric.value,
                    "declared_param_ranges": {
                        k: list(v) for k, v in r.declared_param_ranges.items()
                    },
                }
                for r in ctx.roster_targets
            ],
            "ledger_state": [
                {
                    "target": le.target,
                    "cumulative_n_trials": le.cumulative_n_trials,
                    "quota": le.quota,
                }
                for le in ctx.ledger_state
            ],
            "reference_excerpts": [
                {"name": e.name, "text": e.text}
                for e in ctx.reference_excerpts
            ],
        },
    }
    return json.dumps(payload, indent=2, default=str)


async def _call_anthropic(
    client: Any, ctx: EmissionContext, target: str, intent: str
) -> dict[str, Any]:
    """Invoke the SDK once + parse the JSON response. AuthenticationError
    is intercepted and re-raised as the local ``AuthSkip`` so it escapes
    the retry tuple entirely (zero retries) — identical semantics to a
    missing key."""
    @with_retry(
        max_attempts=3,
        backoff_base_sec=2.0,
        backoff_cap_sec=30.0,
        retry_on=(RateLimitError, APIError),
    )
    async def _call() -> Any:
        try:
            return await client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                temperature=0.0,
                system=_PERSONA_TEXT or "(persona missing — operator must author)",
                messages=[
                    {
                        "role": "user",
                        "content": _build_prompt(ctx, target, intent),
                    }
                ],
            )
        except AuthenticationError:
            raise AuthSkip() from None

    resp = await _call()
    txt = resp.content[0].text
    parsed = json.loads(txt)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"LLM response is not a JSON object: type={type(parsed).__name__}"
        )
    return parsed


# ─── Draft-PR open (sandbox + diff-scope fence) ────────────────────────


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _spec_filename(candidate: str) -> str:
    return f"{_today()}-{candidate}-lab-candidate.md"


def _sidecar_filename(candidate: str) -> str:
    return f"{_today()}-{candidate}-emitted-spec.json"


def _engine_test_stub_path(spec: EmittedSpec) -> str:
    """Relative POSIX path of the engine test stub (allow-list slot 3)."""
    underscored = spec.candidate_name.replace("-", "_")
    return (
        f"{spec.target_engine}/tests/"
        f"test_lab_{underscored}_byte_identical.py"
    )


def _engine_test_stub_body(spec: EmittedSpec) -> str:
    """RED-first stub (Readiness §3 C1–C4). The operator authors the
    real golden in the human-in-the-loop §3 OPERATOR-DRAFT step."""
    underscored = spec.candidate_name.replace("-", "_")
    return (
        f'"""Characterization test stub (RED-first) for Lab candidate '
        f'{spec.candidate_name!r}.\n\n'
        f"This file was emitted by the SP-G Lab spec-emitter. It is the\n"
        f"Readiness §3 byte-identical proof scaffold; the operator must\n"
        f"author the C1 committed golden + C2/C3/C4 assertions before\n"
        f"the candidate is Lab-ready.\n"
        f'"""\n'
        f"from __future__ import annotations\n\n"
        f"import pytest\n\n\n"
        f"@pytest.mark.skip(reason='SP-G stub; operator authors C1–C4 golden')\n"
        f"def test_lab_{underscored}_byte_identical_c1_committed_golden() -> None:\n"
        f"    \"\"\"C1: committed golden — pre-candidate behaviour.\"\"\"\n"
        f"    raise NotImplementedError('operator authors the C1 golden')\n\n\n"
        f"@pytest.mark.skip(reason='SP-G stub; operator authors C1–C4 golden')\n"
        f"def test_lab_{underscored}_byte_identical_c2_default_is_legacy() -> None:\n"
        f"    raise NotImplementedError('operator authors C2')\n\n\n"
        f"@pytest.mark.skip(reason='SP-G stub; operator authors C1–C4 golden')\n"
        f"def test_lab_{underscored}_byte_identical_c3_variant_distinct() -> None:\n"
        f"    raise NotImplementedError('operator authors C3')\n\n\n"
        f"@pytest.mark.skip(reason='SP-G stub; operator authors C1–C4 golden')\n"
        f"def test_lab_{underscored}_byte_identical_c4_no_cross_trial_leakage() -> None:\n"
        f"    raise NotImplementedError('operator authors C4')\n"
    )


async def _open_draft_pr(
    spec: EmittedSpec,
    rendered_markdown: str,
    runner: Callable[..., tuple[int, str, str]],
) -> str | None:
    """Open ONE draft PR carrying the rendered spec + JSON sidecar + the
    engine test stub. ALL gh invocations carry ``--draft``. NEVER calls
    ``gh pr merge`` and NEVER passes ``--undraft``.

    Returns the PR URL (or None on failure). Crash-isolated: a runner
    failure logs + returns None; never raises. The ledger row already
    stands at this point (spec §3.4 step 5 → 6 ordering)."""
    spec_basename = _spec_filename(spec.candidate_name)
    sidecar_basename = _sidecar_filename(spec.candidate_name)
    test_stub_rel = _engine_test_stub_path(spec)

    branch = f"lab-spec-emit/{spec.candidate_name}"
    tmpdir = tempfile.mkdtemp(prefix="llm_lab_emitter_wt_")
    worktree_added = False
    try:
        rc, _o, err = runner(
            ["git", "worktree", "add", tmpdir, "-b", branch],
            cwd=str(_REPO_ROOT),
        )
        if rc != 0:
            logger.error(
                "llm_lab_emitter.worktree_add_failed",
                candidate=spec.candidate_name,
                error=err,
            )
            return None
        worktree_added = True

        # Write the three allow-list slots.
        spec_path = (
            pathlib.Path(tmpdir) / "docs" / "superpowers" / "specs"
            / spec_basename
        )
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(rendered_markdown, encoding="utf-8")

        sidecar_path = pathlib.Path(tmpdir) / "docs" / "lab" / sidecar_basename
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(
            json.dumps(spec.as_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        test_stub_path = pathlib.Path(tmpdir) / test_stub_rel
        test_stub_path.parent.mkdir(parents=True, exist_ok=True)
        test_stub_path.write_text(
            _engine_test_stub_body(spec), encoding="utf-8"
        )

        # Defence-in-depth: enforce the diff-scope fence on the exact
        # paths we just wrote. A code-path drift here would otherwise
        # be the silent failure mode.
        relative_paths = (
            f"docs/superpowers/specs/{spec_basename}",
            f"docs/lab/{sidecar_basename}",
            test_stub_rel,
        )
        enforce_diff_scope(
            relative_paths,
            candidate=spec.candidate_name,
            target_engine=spec.target_engine,
        )

        # Stage + commit the three allow-list files explicitly.
        runner(["git", "add", "--", *relative_paths], cwd=tmpdir)
        commit_msg = (
            f"feat(lab-spec-emit): {spec.candidate_name} candidate "
            f"({spec.intent}, target={spec.target_engine})\n\n"
            f"Emitted by SP-G thin advisory LLM spec-emitter. Draft, "
            f"human-merge-only. Operator hardens §3/§8/§9 OPERATOR-DRAFT "
            f"sections before moving out of draft.\n"
        )
        crc, _co, cerr = runner(
            ["git", "commit", "-m", commit_msg], cwd=tmpdir
        )
        if crc != 0:
            logger.error(
                "llm_lab_emitter.commit_failed",
                candidate=spec.candidate_name,
                error=cerr,
            )
            return None

        # Open the DRAFT PR — `--draft` is non-negotiable.
        title = f"[lab-spec-emit] {spec.candidate_name} ({spec.intent})"
        body = (
            f"## SP-G LLM-emitted Lab candidate (DRAFT)\n\n"
            f"- candidate: `{spec.candidate_name}`\n"
            f"- target_engine: `{spec.target_engine}`\n"
            f"- intent: `{spec.intent}`\n"
            f"- primary_metric: `{spec.primary_metric.value}`\n"
            f"- expected_trials: `{spec.expected_trials}`\n\n"
            f"## Operator hardening required (the human-in-the-loop seams)\n\n"
            f"- §3 byte-identical live path (the C1 committed golden — "
            f"RED-first).\n"
            f"- §8 data prerequisites (status + concrete evidence, "
            f"not 'should be there').\n"
            f"- §9 lookahead / point-in-time honesty (the "
            f"strictly-backward window proof).\n\n"
            f"Once hardened: `gh pr ready` to move out of draft, then "
            f"route via `/lab-target-run`.\n\n"
            f"---\n\n"
            f"Spec: `docs/superpowers/specs/2026-05-20-lab-sp-g-llm-"
            f"spec-emitter-design.md`\n"
            f"Runbook (orphaned spend): "
            f"`docs/runbooks/lab-spec-emit-orphaned-spend.md`\n"
        )
        prc, pout, perr = runner(
            [
                "gh", "pr", "create", "--draft",
                "--label", "lab-spec-emit",
                "--title", title,
                "--body", body,
            ],
            cwd=tmpdir,
        )
        if prc != 0:
            logger.error(
                "llm_lab_emitter.pr_create_failed",
                candidate=spec.candidate_name,
                error=perr,
            )
            return None
        url = pout.strip() or branch
        logger.info(
            "llm_lab_emitter.draft_pr_opened",
            candidate=spec.candidate_name,
            pr=url,
        )
        return url

    except DiffScopeViolation as exc:
        logger.error(
            "llm_lab_emitter.diff_scope_violation",
            candidate=spec.candidate_name,
            violating_paths=exc.violating_paths,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — never raise; advisory preserved
        logger.error(
            "llm_lab_emitter.pr_isolated_failure",
            candidate=spec.candidate_name,
            error=str(exc),
        )
        return None
    finally:
        if worktree_added:
            try:
                runner(
                    ["git", "worktree", "remove", "--force", tmpdir],
                    cwd=str(_REPO_ROOT),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.error(
                    "llm_lab_emitter.worktree_remove_failed",
                    candidate=spec.candidate_name,
                    error=str(exc),
                )
            try:
                runner(
                    ["git", "branch", "-D", branch], cwd=str(_REPO_ROOT)
                )
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.error(
                    "llm_lab_emitter.branch_delete_failed",
                    candidate=spec.candidate_name,
                    error=str(exc),
                )
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── Main emission orchestrator ────────────────────────────────────────


async def emit_once(
    pool: Any,
    *,
    target: str,
    intent: str = "fold_existing",
    expected_trials: int | None = None,
    reference_bundles: tuple[str, ...] = (),
    quota: int = EMISSION_QUOTA_PER_TARGET,
    client_factory: Callable[[], Any] = _default_client,
    pr_runner: Callable[..., tuple[int, str, str]] = _default_pr_runner,
) -> EmitterOutcome:
    """Run ONE SP-G emission cycle. Never raises — all failures are
    captured in ``EmitterOutcome.error`` (mirrors data/engine triage
    discipline). Enforces the strict §3.4 step ordering.

    ``expected_trials`` defaults to a conservative 50 if not provided
    (the operator may override; the LLM's response may declare its own
    ``expected_trials`` which the agent re-validates against the
    budget AFTER the LLM round-trip is already paid for — that
    response value updates the actual ledger spend).
    """
    out = EmitterOutcome(target_engine=target)

    try:  # noqa: BLE001 — never abort the operator command
        # Pre-LLM: target must be in the roster.
        try:
            _verify_target_in_roster(target)
        except ValueError as exc:
            out.error = str(exc)
            return out

        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.info("llm_lab_emitter.no_api_key")
            out.skipped_no_key = True
            return out

        # Default pre-LLM probe = 10 trials per emission (fits within
        # the default EMISSION_QUOTA_PER_TARGET=20). The LLM's declared
        # ``expected_trials`` supersedes this for the actual ledger spend.
        budget_trials = expected_trials or 10

        # STEP 1: pre-emission ledger budget gate (spec §3.4 step 1).
        try:
            await check_budget(
                pool, target=target, expected_trials=budget_trials, quota=quota
            )
        except LedgerBudgetExhausted as exc:
            logger.info(
                "llm_lab_emitter.budget_exhausted",
                target=target,
                cumulative=exc.cumulative,
                expected=exc.expected,
                quota=exc.quota,
            )
            out.skipped_no_budget = True
            out.error = str(exc)
            return out

        # STEP 2: build EmissionContext.
        ctx, _cumulative = await _build_emission_context(
            pool,
            target=target,
            expected_trials=budget_trials,
            reference_bundles=reference_bundles,
            quota=quota,
        )

        # STEP 3: Anthropic SDK call (no tools, no network beyond SDK).
        client = client_factory()
        try:
            try:
                response_dict = await _call_anthropic(
                    client, ctx, target, intent
                )
            except AuthSkip:
                logger.warning("llm_lab_emitter.auth_error_skipped")
                out.skipped_no_key = True
                return out

            # STEP 4: validate EmittedSpec.
            try:
                spec = EmittedSpec.model_validate(response_dict)
            except Exception as exc:  # noqa: BLE001 — malformed response
                logger.warning(
                    "llm_lab_emitter.malformed_response", error=str(exc)
                )
                out.error = f"malformed_response: {exc}"
                return out

            # Re-validate the LLM-declared target is in the roster (a
            # rogue LLM proposing canary / a RETIRED engine is rejected
            # BEFORE the ledger row is written).
            try:
                _verify_target_in_roster(spec.target_engine)
            except ValueError as exc:
                logger.warning(
                    "llm_lab_emitter.target_not_in_roster", error=str(exc)
                )
                out.error = f"target_not_in_roster: {exc}"
                return out

            # Re-validate budget against the LLM's declared
            # ``expected_trials`` (the LLM may declare a different
            # value than our default budget probe; the actual ledger
            # spend uses the LLM's value).
            try:
                await check_budget(
                    pool,
                    target=spec.target_engine,
                    expected_trials=spec.expected_trials,
                    quota=quota,
                )
            except LedgerBudgetExhausted as exc:
                logger.info(
                    "llm_lab_emitter.budget_exhausted_post_validate",
                    error=str(exc),
                )
                out.skipped_no_budget = True
                out.error = str(exc)
                return out

            # STEP 5: record_trial_spend BEFORE the draft PR is opened
            # (spec §3.4 — the immutable step ordering). A failure here
            # is a hard error: no ledger row, no PR.
            try:
                await record_trial_spend(
                    pool,
                    target=spec.target_engine,
                    candidate=spec.candidate_name,
                    trials=spec.expected_trials,
                    seed=0,
                    run_outcome=f"llm_emitter:{_persona_sha()}",
                )
                out.ledger_recorded = True
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "llm_lab_emitter.ledger_write_failed",
                    candidate=spec.candidate_name,
                    error=str(exc),
                )
                out.error = f"ledger_write_failed: {exc}"
                return out

            # Emit the application_log advisory event carrying the full
            # EmittedSpec JSON sidecar — the orphaned-spend recovery
            # runbook reads this if step 6 fails.
            await _emit_event(
                pool,
                "LLM_LAB_EMITTED_SPEC",
                f"SP-G emitted Lab candidate {spec.candidate_name!r}",
                {
                    "schema": 1,
                    "candidate_name": spec.candidate_name,
                    "target_engine": spec.target_engine,
                    "intent": spec.intent,
                    "persona_version": _persona_sha(),
                    "readiness_checklist_version": ctx.readiness_checklist_version,
                    "emitted_spec_json": spec.as_dict(),
                    "expected_trials": spec.expected_trials,
                    "primary_metric": spec.primary_metric.value,
                },
            )
            out.emitted_candidate = spec.candidate_name

            # STEP 6: render + draft PR. A failure here leaves the
            # ledger row standing (the orphaned-spend runbook).
            try:
                rendered = render_candidate_spec(spec)
            except GateOverrideRejected as exc:
                logger.error(
                    "llm_lab_emitter.gate_override_rejected",
                    error=str(exc),
                )
                out.error = f"gate_override_rejected: {exc}"
                return out

            # Belt-and-suspenders: validate again after rendering (the
            # renderer already calls this, but the operator's audit
            # trail benefits from the explicit second invocation in
            # case a future renderer edit forgets).
            validate_no_gate_override(rendered)

            pr_link = await _open_draft_pr(spec, rendered, pr_runner)
            out.pr_link = pr_link
            if pr_link is None:
                out.notes.append(
                    "draft PR open failed; ledger row stands. "
                    "See docs/runbooks/lab-spec-emit-orphaned-spend.md."
                )

        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    except Exception as exc:  # noqa: BLE001 — never raises
        logger.error("llm_lab_emitter.error", error=str(exc))
        out.error = str(exc)
    return out


# ─── Co-task entry-point (the third co-task on llm_triage_service) ─────


# Spec §2.7 (operator Q6 decision): ``LAB_LEDGER_CAPACITY_AVAILABLE``
# event class is DEFERRED in this PR. The co-task is structurally
# present (preserves spec §4.2 "third crash-isolated co-task" wording),
# but its trigger event-type set is empty by design — the co-task polls
# and sees nothing to do; the operator-command path (the
# ``/lab-spec-emit`` skill calling ``python -m ops.llm_lab_emitter``) is
# the v1 trigger. Task #25 / a future event-emitter PR may populate
# this tuple.
LAB_EMITTER_TRIGGER_EVENT_TYPES: tuple[str, ...] = ()


async def run_lab_emitter_cotask(pool: Any) -> EmitterOutcome:
    """Co-task entry called by ``ops/llm_triage_service.py`` when a
    trigger event fires. Per operator Q6 the trigger event class is
    deferred; this is a no-op safe-by-design until the event-emitter
    is built.

    The signature mirrors ``ops.llm_data_triage.run_triage`` /
    ``ops.engine_llm_triage.run_triage`` so the daemon's
    ``_lane_loop`` can fire it with the same ``triage_fn(pool)`` shape.
    """
    logger.info("llm_lab_emitter.cotask_noop_v1")
    return EmitterOutcome(
        notes=["v1: operator-command-driven; event-class deferred per Q6"]
    )


# ─── CLI entry point ───────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ops.llm_lab_emitter",
        description=(
            "SP-G thin advisory LLM spec-emitter — one Lab candidate "
            "emission per invocation. Draft PR + human-merge-only."
        ),
    )
    parser.add_argument(
        "--target",
        type=str,
        required=False,
        help=(
            "Target engine (must be in "
            "tpcore.engine_profile.lab_targetable_engines())."
        ),
    )
    parser.add_argument(
        "--intent",
        type=str,
        choices=("fold_existing", "promote_new"),
        default="fold_existing",
    )
    parser.add_argument(
        "--expected-trials",
        type=int,
        default=10,
        help=(
            "Pre-LLM budget probe (default 10; the LLM-declared "
            "``expected_trials`` supersedes for the actual ledger spend). "
            "Must fit within `--quota` (default 20 per Q2)."
        ),
    )
    parser.add_argument(
        "--reference-bundle",
        type=str,
        default="",
        help=(
            "Comma-separated bundle names under docs/lab_emitter_references/. "
            "Operator Q3 decision."
        ),
    )
    parser.add_argument(
        "--quota",
        type=int,
        default=EMISSION_QUOTA_PER_TARGET,
        help=(
            f"Per-target emission quota (operator override; default "
            f"{EMISSION_QUOTA_PER_TARGET} per Q2)."
        ),
    )
    parser.add_argument(
        "--replay",
        type=str,
        default="",
        help=(
            "Path to a persisted EmittedSpec JSON sidecar; re-renders "
            "and re-opens the draft PR WITHOUT spending the ledger (the "
            "orphaned-spend recovery path — see runbook)."
        ),
    )
    return parser.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from tpcore.db import build_asyncpg_pool

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("llm_lab_emitter.no_dsn")
        return 1
    pool = await build_asyncpg_pool(dsn)
    try:
        if args.replay:
            outcome = await _replay_from_sidecar(pool, args.replay)
        else:
            if not args.target:
                logger.error("llm_lab_emitter.no_target")
                return 2
            bundles = tuple(
                b for b in args.reference_bundle.split(",") if b.strip()
            )
            outcome = await emit_once(
                pool,
                target=args.target,
                intent=args.intent,
                expected_trials=args.expected_trials,
                reference_bundles=bundles,
                quota=args.quota,
            )
    finally:
        await pool.close()

    print(
        f"llm_lab_emitter: candidate={outcome.emitted_candidate} "
        f"target={outcome.target_engine} pr_link={outcome.pr_link} "
        f"ledger_recorded={outcome.ledger_recorded} "
        f"skipped_no_key={outcome.skipped_no_key} "
        f"skipped_no_budget={outcome.skipped_no_budget} "
        f"error={outcome.error}"
    )
    return 0 if outcome.error is None else 3


async def _replay_from_sidecar(pool: Any, path: str) -> EmitterOutcome:
    """Orphaned-spend recovery — re-render + re-open the draft PR from a
    persisted EmittedSpec JSON sidecar, WITHOUT spending the ledger.

    The ledger row already stands (spec §3.4 step 5 → 6 ordering). The
    runbook ``docs/runbooks/lab-spec-emit-orphaned-spend.md`` is the
    operator-facing contract.
    """
    out = EmitterOutcome()
    try:  # noqa: BLE001
        sidecar = pathlib.Path(path)
        if not sidecar.is_file():
            out.error = f"sidecar not found: {path}"
            return out
        spec_dict = json.loads(sidecar.read_text(encoding="utf-8"))
        # Coerce list-encoded param_ranges back to tuples for pydantic.
        if "param_ranges" in spec_dict:
            spec_dict["param_ranges"] = {
                k: tuple(v) for k, v in spec_dict["param_ranges"].items()
            }
        spec = EmittedSpec.model_validate(spec_dict)
        out.emitted_candidate = spec.candidate_name
        out.target_engine = spec.target_engine

        rendered = render_candidate_spec(spec)
        validate_no_gate_override(rendered)

        pr_link = await _open_draft_pr(spec, rendered, _default_pr_runner)
        out.pr_link = pr_link
        out.notes.append("replay: ledger NOT respent; recovery from orphaned spend")
        if pool is not None:
            await _emit_event(
                pool,
                "LLM_LAB_EMITTER_REPLAY",
                f"SP-G replay for candidate {spec.candidate_name!r}",
                {
                    "schema": 1,
                    "candidate_name": spec.candidate_name,
                    "target_engine": spec.target_engine,
                    "pr_link": pr_link,
                    "source_sidecar": str(sidecar),
                },
            )
    except Exception as exc:  # noqa: BLE001
        out.error = str(exc)
    return out


# Public shared-SDK surface (for tests + the daemon co-task).
__all__ = [
    "LAB_EMITTER_TRIGGER_EVENT_TYPES",
    "EmitterOutcome",
    "emit_once",
    "run_lab_emitter_cotask",
]


def main() -> None:  # pragma: no cover - CLI shim
    """Subprocess-safe runner. The cwd-reset between bash calls means
    asyncio.run is the safest top-level."""
    code = asyncio.run(_amain(sys.argv[1:]))
    sys.exit(code)


# Subprocess-safe entry: only invoke main() under `python -m ops.llm_lab_emitter`.
if __name__ == "__main__":  # pragma: no cover
    main()
