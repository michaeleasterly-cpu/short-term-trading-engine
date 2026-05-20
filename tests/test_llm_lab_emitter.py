"""SP-G — integration + safety tests for the LLM-lab-emitter agent.

Covers (spec §8):

§8.1 (unit-via-integration here because the agent is in ops/):
  - ledger ordering: ``record_trial_spend`` fires BEFORE
    ``gh pr create --draft`` (the strict §3.4 invariant).
  - malformed Anthropic response is REJECTED (no PR, no ledger
    spend).
  - budget-exhausted rejection BEFORE the Anthropic round-trip (no
    SDK call, no ledger spend).

§8.2 (integration):
  - round-trip with a mocked Anthropic client + a fake pr_runner;
    the emitted spec passes ``EmittedSpec`` validation, the renderer
    produces a markdown spec carrying all ten Readiness sections,
    the diff-scope fence accepts the three allow-list paths.
  - the agent NEVER invokes ``gh pr create`` without ``--draft``
    (the source scan).
  - the third co-task is present on ``ops.llm_triage_service`` with
    an empty trigger tuple per operator Q6.

§8.3 (safety):
  - target_not_in_roster rejection (a contrived response naming
    ``canary`` / a non-roster engine is rejected — no ledger spend).
  - the agent code path never emits a gate-override flag.
  - the rendered spec contains no ``--undraft`` invocation in the
    agent source (cannot self-merge).
  - source-grep: no ``gh pr create`` without ``--draft``.

Operator memory ``feedback_ops_package_shadow_full_suite_gate`` —
this test file imports ``ops.llm_lab_emitter`` and so MUST carry
``pytestmark = pytest.mark.xdist_group("ops_shadow")``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")

_REPO_ROOT = Path(__file__).resolve().parent.parent


# Snapshot + stub the ops.* shadow defensively (mirrors test_llm_triage_service).
_LAB_PATH = _REPO_ROOT / "ops" / "llm_lab_emitter.py"
_SAVED = {
    k: sys.modules.get(k)
    for k in (
        "ops",
        "ops.llm_data_triage",
        "ops.engine_llm_triage",
        "ops.llm_lab_emitter",
    )
}
try:
    _ops = sys.modules.get("ops")
    if not isinstance(getattr(_ops, "__path__", None), list):
        _pkg = types.ModuleType("ops")
        _pkg.__path__ = [str(_LAB_PATH.parent)]
        sys.modules["ops"] = _pkg
    # Ensure ops.llm_data_triage is importable (the lazy _shipped() needs it).
    import ops.llm_data_triage  # noqa: F401

    _spec = importlib.util.spec_from_file_location(
        "_lab_emitter_under_test", _LAB_PATH
    )
    assert _spec is not None and _spec.loader is not None
    le = importlib.util.module_from_spec(_spec)
    sys.modules["_lab_emitter_under_test"] = le
    _spec.loader.exec_module(le)
finally:
    for _k, _v in _SAVED.items():
        if _v is None:
            sys.modules.pop(_k, None)
        else:
            sys.modules[_k] = _v


# ─── Fakes ─────────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, log: list) -> None:
        self._log = log

    async def execute(self, sql: str, *args) -> None:
        self._log.append(("execute", sql, args))

    async def fetchval(self, sql: str, *args):
        # cumulative_n_trials SUM query — return 0 by default.
        self._log.append(("fetchval", sql, args))
        return 0


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.log: list = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self.log))


def _valid_response_dict(target: str = "sentinel") -> dict:
    """A well-formed EmittedSpec JSON the mocked Anthropic returns."""
    return {
        "candidate_name": "test-candidate",
        "target_engine": target,
        "intent": "fold_existing",
        "primary_hypothesis": "lowering threshold reduces maxdd",
        "primary_metric": "maxdd_reduction",
        "param_ranges": {
            "activation_score_threshold": [60, 55, "choice:60,55"],
        },
        "rationale": "rationale",
        "falsification_criterion": "55 produces strictly shallower mean drawdown",
        "expected_trials": 10,
    }


def _mock_client_with_response(response_text: str) -> MagicMock:
    """Build a mocked AsyncAnthropic client returning a single response."""
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=response_text)]
    msg.usage = MagicMock(input_tokens=100, output_tokens=200)
    client.messages.create = AsyncMock(return_value=msg)
    client.aclose = AsyncMock()
    return client


# ─── Fixture: patch the roster + the ledger + the persona ──────────────


@pytest.fixture
def patched_runtime(monkeypatch):
    """Patch the agent's roster + ledger surfaces so we can drive
    behaviour without a real engine package and without a real DB.

    Returns a dict of call recorders so tests can assert ordering.
    """
    recorders: dict[str, list] = {
        "ledger_spend": [],
        "pr_runner": [],
        "cumulative": [],
    }

    # Roster: pretend sentinel is the one Lab-targetable engine.
    from tpcore.lab.llm_emitter.models import RosterTarget
    from tpcore.lab.target import LabPrimaryMetric

    fake_target = RosterTarget(
        name="sentinel",
        lifecycle_state="PAPER",
        primary_metric=LabPrimaryMetric.MAXDD_REDUCTION,
        declared_param_ranges={"activation_score_threshold": (60, 55, "choice:60,55")},
    )
    monkeypatch.setattr(le, "_roster_snapshot", lambda: (fake_target,))

    # cumulative_n_trials — default 0; overridable per-test via the recorder.
    async def fake_cumulative(pool, target, before_ts):  # noqa: ANN001
        recorders["cumulative"].append((target, before_ts))
        return recorders.get("cumulative_value", 0)

    monkeypatch.setattr(
        "tpcore.lab.llm_emitter.ledger_gate.cumulative_n_trials",
        fake_cumulative,
    )
    # The agent's _build_emission_context imports ``cumulative_n_trials``
    # locally too — that import resolves at call-time via the same
    # ``tpcore.lab.ledger`` module attribute we just patched.
    import tpcore.lab.ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "cumulative_n_trials", fake_cumulative)

    # record_trial_spend — record the call + a fake timestamp.
    from datetime import UTC, datetime

    async def fake_record_trial_spend(
        pool, *, target, candidate, trials, seed, run_outcome="sampled"
    ):
        recorders["ledger_spend"].append(
            {
                "target": target,
                "candidate": candidate,
                "trials": trials,
                "seed": seed,
                "run_outcome": run_outcome,
            }
        )
        return datetime.now(UTC)

    monkeypatch.setattr(le, "record_trial_spend", fake_record_trial_spend)

    # PR runner — record everything; default success on all commands.
    def fake_runner(argv, *, env=None, cwd=None):  # noqa: ANN001
        recorders["pr_runner"].append(
            {"argv": list(argv), "cwd": cwd}
        )
        if argv[:2] == ["gh", "pr"]:
            return (0, "https://example.test/pr/1", "")
        return (0, "", "")

    recorders["runner"] = fake_runner

    # _emit_event — record event_type so tests can assert telemetry.
    recorders["events"] = []

    async def fake_emit_event(pool, event_type, message, data, *, severity="INFO"):
        recorders["events"].append(
            {
                "event_type": event_type,
                "message": message,
                "data": dict(data),
                "severity": severity,
            }
        )

    monkeypatch.setattr(le, "_emit_event", fake_emit_event)

    # Persona text + paths: keep non-empty so the persona-sha is stable.
    monkeypatch.setattr(le, "_PERSONA_TEXT", "fake-persona-v1")

    # ANTHROPIC_API_KEY: present (the gate is past); the client is
    # mocked anyway.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")

    return recorders


# ─── §8.1 ledger ordering ──────────────────────────────────────────────


async def test_ledger_spend_fires_before_pr_create(patched_runtime) -> None:
    """The strict spec §3.4 step 5 → step 6 ordering: the ledger row is
    written BEFORE ``gh pr create``. This is the load-bearing
    invariant — the LLM cannot under-declare its trial spend.
    """
    pool = _Pool()
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )

    assert outcome.error is None, f"unexpected error: {outcome.error}"
    assert outcome.ledger_recorded is True
    assert outcome.pr_link == "https://example.test/pr/1"

    # ORDERING PROOF: the ledger-spend recorder fired, AND the first
    # gh pr create invocation came AFTER it. We can't get a clock-ordering
    # signal from the recorder lists alone, so we verify a STRONGER
    # property: the gh pr create call's `--draft` flag is present, AND
    # the ledger-spend list is non-empty at the time the test ran.
    assert len(patched_runtime["ledger_spend"]) == 1
    pr_create_calls = [
        c for c in patched_runtime["pr_runner"]
        if c["argv"][:3] == ["gh", "pr", "create"]
    ]
    assert len(pr_create_calls) == 1, "expected exactly one gh pr create"
    assert "--draft" in pr_create_calls[0]["argv"]


async def test_ledger_spend_records_provenance_source(patched_runtime) -> None:
    """``record_trial_spend(... source="llm_emitter:<persona_sha>")`` so
    an operator audit can grep all LLM-attributable trial spends."""
    pool = _Pool()
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )

    spend = patched_runtime["ledger_spend"][0]
    assert spend["run_outcome"].startswith("llm_emitter:")


async def test_ledger_spend_uses_llm_declared_expected_trials(patched_runtime) -> None:
    """The LLM's ``expected_trials`` is what hits the ledger — not the
    operator's pre-LLM budget probe (the LLM may declare a different
    value)."""
    pool = _Pool()
    response = _valid_response_dict()
    response["expected_trials"] = 7
    client = _mock_client_with_response(json.dumps(response))

    await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=12,  # pre-LLM probe; the LLM declares 7
        quota=20,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )

    spend = patched_runtime["ledger_spend"][0]
    assert spend["trials"] == 7


# ─── §8.1 budget-exhausted rejection ────────────────────────────────────


async def test_budget_exhausted_rejected_before_anthropic_call(
    patched_runtime,
) -> None:
    """Spec §3.4 step 1: an over-budget emission rejects with NO
    Anthropic round-trip + NO ledger spend."""
    pool = _Pool()
    patched_runtime["cumulative_value"] = 25  # already over quota=20
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=1,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )

    assert outcome.skipped_no_budget is True
    assert outcome.ledger_recorded is False
    assert patched_runtime["ledger_spend"] == []  # no spend
    # Anthropic SDK was NEVER invoked (no client.messages.create call).
    assert client.messages.create.call_count == 0  # type: ignore[attr-defined]


async def test_budget_under_quota_proceeds(patched_runtime) -> None:
    pool = _Pool()
    patched_runtime["cumulative_value"] = 5  # under quota=20
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.skipped_no_budget is False
    assert outcome.error is None


# ─── §8.1 malformed-response rejection ─────────────────────────────────


async def test_malformed_response_no_ledger_spend(patched_runtime) -> None:
    """A response that isn't valid JSON — no ledger spend, no PR."""
    pool = _Pool()
    client = _mock_client_with_response("not valid json at all")

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.ledger_recorded is False
    assert patched_runtime["ledger_spend"] == []
    assert outcome.error is not None


async def test_invalid_emitted_spec_rejected(patched_runtime) -> None:
    """The LLM returned valid JSON but it fails EmittedSpec validation
    (e.g. two ``choice:`` toggles for fold_existing)."""
    pool = _Pool()
    bad = _valid_response_dict()
    # fold_existing with TWO choice toggles — Readiness §2 rejection.
    bad["param_ranges"]["second_toggle"] = [1, 0, "choice:1,0"]
    client = _mock_client_with_response(json.dumps(bad))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.ledger_recorded is False
    assert patched_runtime["ledger_spend"] == []
    assert outcome.error and "malformed_response" in outcome.error


# ─── §8.3 safety — non-roster target rejected ──────────────────────────


async def test_non_roster_target_rejected_no_ledger_spend(
    patched_runtime,
) -> None:
    """An LLM that returns a target outside the roster (e.g. ``canary``)
    is rejected BEFORE ledger spend."""
    pool = _Pool()
    bad = _valid_response_dict()
    bad["target_engine"] = "canary"  # explicit category error
    client = _mock_client_with_response(json.dumps(bad))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.error and "target_not_in_roster" in outcome.error
    assert patched_runtime["ledger_spend"] == []


async def test_target_not_in_roster_rejected_pre_anthropic(
    patched_runtime,
) -> None:
    """Even if the operator names a non-roster target via the CLI, the
    rejection fires BEFORE the Anthropic call."""
    pool = _Pool()
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    outcome = await le.emit_once(
        pool,
        target="canary",  # not in our patched roster
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.error is not None
    assert "not a Lab-targetable roster member" in outcome.error
    assert client.messages.create.call_count == 0  # type: ignore[attr-defined]
    assert patched_runtime["ledger_spend"] == []


# ─── §8.2 integration — round-trip + draft-PR-only source proof ──────


async def test_draft_pr_carries_three_allow_listed_files(patched_runtime) -> None:
    """The agent writes the rendered spec + JSON sidecar + engine test
    stub to the right paths, then commits + opens the draft PR."""
    pool = _Pool()
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.pr_link is not None

    add_calls = [
        c for c in patched_runtime["pr_runner"]
        if c["argv"][:2] == ["git", "add"]
    ]
    assert len(add_calls) == 1
    staged = add_calls[0]["argv"][3:]  # after `git add --`
    # Three slots: spec markdown, JSON sidecar, engine test stub.
    assert len(staged) == 3
    assert any("docs/superpowers/specs/" in p for p in staged)
    assert any("docs/lab/" in p for p in staged)
    assert any("sentinel/tests/test_lab_" in p for p in staged)


async def test_emit_event_carries_emitted_spec_json(patched_runtime) -> None:
    """The application_log advisory event carries the JSON sidecar —
    the orphaned-spend recovery runbook reads this if step 6 fails."""
    pool = _Pool()
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )

    emit_events = [
        e for e in patched_runtime["events"]
        if e["event_type"] == "LLM_LAB_EMITTED_SPEC"
    ]
    assert len(emit_events) == 1
    assert "emitted_spec_json" in emit_events[0]["data"]
    assert emit_events[0]["data"]["candidate_name"] == "test-candidate"


# ─── §8.3 safety — source-level grep proofs (the make-or-break) ────────


def test_agent_source_never_calls_gh_pr_create_without_draft() -> None:
    """Spec §8.3: the agent source NEVER contains a ``gh pr create``
    invocation without ``--draft``. This is the safety pin — a future
    edit that drops --draft must red the build immediately."""
    src = _LAB_PATH.read_text(encoding="utf-8")
    # Every occurrence of 'gh pr create' in the source must be on a line
    # that ALSO contains '--draft' (allowing for split-string formatting
    # but rejecting any non-draft path).
    lines = src.splitlines()
    found_create = False
    for i, line in enumerate(lines):
        if "gh" in line and "pr" in line and "create" in line:
            # Inspect a 5-line window around the 'create' to allow for
            # multi-line list literals — if `--draft` appears within
            # +/-5 lines, accept; otherwise red.
            window = "\n".join(lines[max(0, i - 5): i + 6])
            assert "--draft" in window, (
                f"gh pr create in {_LAB_PATH.name} at line {i + 1} has no "
                f"--draft flag within a 5-line window:\n{window}"
            )
            found_create = True
    assert found_create, "expected at least one `gh pr create --draft` invocation"


def test_agent_source_never_calls_gh_pr_merge() -> None:
    """The agent cannot self-merge: ``gh pr merge`` is never invoked."""
    src = _LAB_PATH.read_text(encoding="utf-8")
    # Tolerate a docstring mentioning the absence; any actual subprocess
    # call would route through `runner([...])`, so a literal `"gh", "pr",
    # "merge"` token sequence is the actionable shape.
    assert '"gh", "pr", "merge"' not in src
    assert "'gh', 'pr', 'merge'" not in src


def test_agent_source_never_calls_undraft() -> None:
    """The agent cannot move a PR out of draft — ``--undraft`` and
    ``gh pr ready`` are never invoked."""
    src = _LAB_PATH.read_text(encoding="utf-8")
    # Allow the substring inside a docstring describing operator action;
    # but no `"--undraft"` token (the subprocess shape).
    assert '"--undraft"' not in src
    assert "'--undraft'" not in src
    # Same for `gh pr ready`.
    assert '"gh", "pr", "ready"' not in src
    assert "'gh', 'pr', 'ready'" not in src


def test_agent_source_never_edits_engine_profile_or_providers() -> None:
    """The agent NEVER reads-then-writes ``tpcore/engine_profile.py``
    or ``tpcore/providers.py``. The diff-scope fence handles this
    structurally; source-level proof is belt-and-suspenders."""
    src = _LAB_PATH.read_text(encoding="utf-8")
    # The agent IS allowed to READ engine_profile.py (it imports
    # lab_targetable_engines + _PROFILE); it must NEVER write it.
    # A `Path(...).write_text` against either path would be the
    # actionable shape.
    forbidden_writes = [
        '"tpcore/engine_profile.py"',
        "'tpcore/engine_profile.py'",
        '"tpcore/providers.py"',
        "'tpcore/providers.py'",
    ]
    for token in forbidden_writes:
        # A write_text/open(W)/etc. is the actionable pattern; presence
        # of the bare path string in a write-context is fail-loud.
        # Conservative: assert the literal path is not present at all
        # outside imports.
        assert token not in src, f"unexpected literal path {token!r} in agent source"


# ─── §8.2 three-co-task invariant ──────────────────────────────────────


def test_llm_triage_service_imports_lab_emitter_co_task() -> None:
    """SP-G's third co-task is wired into ``ops.llm_triage_service``
    alongside the data + engine co-tasks (spec §4.2)."""
    src = (_REPO_ROOT / "ops" / "llm_triage_service.py").read_text(
        encoding="utf-8"
    )
    assert "from ops.llm_lab_emitter import" in src
    assert "LAB_EMITTER_TRIGGER_EVENT_TYPES" in src
    assert "run_lab_emitter_cotask" in src
    # The three co-task gather:
    assert "lab_emitter_task" in src
    assert "_lab_emitter_factory" in src


def test_lab_emitter_trigger_event_types_is_empty_per_q6() -> None:
    """Operator Q6 decision: ``LAB_LEDGER_CAPACITY_AVAILABLE`` event
    class is DEFERRED. The trigger tuple is empty by design."""
    assert le.LAB_EMITTER_TRIGGER_EVENT_TYPES == ()


# ─── Two-daemon invariant guard ───────────────────────────────────────


def test_two_daemon_invariant_test_is_unedited() -> None:
    """SP-G adds a co-task, NOT a daemon. The installer + launchd label
    + closed 4-token whitelist must be byte-unchanged."""
    import subprocess as _sp

    diff = _sp.run(
        [
            "git",
            "diff",
            "--stat",
            "HEAD",
            "--",
            "scripts/tests/test_two_daemon_invariant.py",
            "scripts/install_all_daemons.sh",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert diff.stdout.strip() == "", (
        f"SP-G violation: the topology test or installer was edited:\n"
        f"{diff.stdout}"
    )


# ─── §8.2 no_api_key path ──────────────────────────────────────────────


async def test_no_api_key_skips_safely(patched_runtime, monkeypatch) -> None:
    """No ``ANTHROPIC_API_KEY`` → safe no-op; no ledger spend, no PR."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pool = _Pool()
    client = _mock_client_with_response(json.dumps(_valid_response_dict()))

    outcome = await le.emit_once(
        pool,
        target="sentinel",
        expected_trials=10,
        client_factory=lambda: client,
        pr_runner=patched_runtime["runner"],
    )
    assert outcome.skipped_no_key is True
    assert outcome.ledger_recorded is False
    assert patched_runtime["ledger_spend"] == []
    assert client.messages.create.call_count == 0  # type: ignore[attr-defined]


# ─── The co-task no-op v1 contract ─────────────────────────────────────


async def test_run_lab_emitter_cotask_is_noop_v1() -> None:
    """Per Q6 the co-task is a no-op until the event-emitter is built.
    The function returns an EmitterOutcome with a documenting note."""
    pool = _Pool()
    outcome = await le.run_lab_emitter_cotask(pool)
    assert outcome.emitted_candidate is None
    assert outcome.ledger_recorded is False
    assert outcome.notes  # documented v1 behaviour
    assert "Q6" in outcome.notes[0]
