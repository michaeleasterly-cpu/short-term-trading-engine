"""Tests for scripts/agent_pr_label_guard.py — the fail-CLOSED guard.

Threat model (MED security fix): the two LLM-triage deterministic fence
jobs in .github/workflows/ci.yml are label-gated (``if:`` runs ONLY when
the ``llm-data-triage`` / ``engine-llm-triage`` label is present). The
label is added by the agent's OWN automation. Failure mode = fail-OPEN:
an agent draft PR that omits/loses the label triggers NEITHER fence and
merges *unfenced*.

This guard makes label-omission on an *agent* PR a hard CI failure while
leaving HUMAN maintenance PRs to the same files completely untouched
(concrete proof this matters: human PRs #97/#98 modified
``ops/llm_data_triage.py`` correctly UNlabeled — they must still pass).

Agent-PR signal (evasion-proof, OR-ed — ANY positive agent indicator
without the label fails closed):
  (1) branch (head ref) starts with ``llm-triage/``
      — both agents hardcode ``branch = f"llm-triage/{ref_short}"``
      (ops/llm_data_triage.py:204, ops/engine_llm_triage.py:217); the
      agent cannot create-the-PR and suppress its own head ref.
  (2) the diff adds a provenance artifact under
      ``docs/llm_triage_proposals/`` or
      ``docs/engine_llm_triage_proposals/`` (ops/llm_data_triage.py:221,
      ops/engine_llm_triage.py:232) — always committed by the agent,
      never by a human ops/* maintenance edit.

CRITICAL ISOLATION INVARIANT (PR #61 lesson): every test that touches
git MUST fabricate a THROWAWAY ``git init`` repo in ``tmp_path`` and
operate exclusively there. NOTHING here ever runs git against the real
working repo.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agent_pr_label_guard.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "_agent_pr_label_guard_under_test", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["_agent_pr_label_guard_under_test"] = _mod
_SPEC.loader.exec_module(_mod)

_HOST_REPO = Path(__file__).resolve().parents[2]


def _run_guard(
    *, head_ref: str, changed_paths: list[str], labels: list[str]
) -> int:
    """Invoke the guard's pure entrypoint and return its exit code."""
    return _mod.evaluate(
        head_ref=head_ref,
        changed_paths=changed_paths,
        labels=labels,
    )


# ---------------------------------------------------------------------------
# Pure-predicate tests (no git) — the core fail-closed logic.
# ---------------------------------------------------------------------------

def test_agent_branch_no_label_fails():
    """Agent branch prefix + NO label ⇒ fail-closed (non-zero)."""
    rc = _run_guard(
        head_ref="llm-triage/cross_table_audit-x",
        changed_paths=["docs/llm_triage_proposals/abc.binding.txt"],
        labels=[],
    )
    assert rc != 0


def test_agent_provenance_artifact_no_label_fails():
    """Provenance artifact present + NO label ⇒ fail-closed, even if the
    head ref were somehow renamed (defense-in-depth OR)."""
    rc = _run_guard(
        head_ref="some-renamed-branch",
        changed_paths=[
            "docs/engine_llm_triage_proposals/h1.dossier.md",
            "ops/engine_ladder.py",
        ],
        labels=["unrelated"],
    )
    assert rc != 0


def test_human_pr_to_same_files_passes():
    """Human maintenance PR (#97/#98 shape): edits ops/llm_data_triage.py,
    normal branch, NO agent provenance artifact, NO label ⇒ PASS (no-op).
    This is the false-positive guard the threat model demands."""
    rc = _run_guard(
        head_ref="fix/transient-retry-broker",
        changed_paths=[
            "ops/llm_data_triage.py",
            "tpcore/llm_data_triage/fence.py",
            "tests/test_llm_data_triage_agent.py",
        ],
        labels=[],
    )
    assert rc == 0


def test_agent_branch_with_data_label_passes():
    """Agent PR WITH the correct data label ⇒ pass (the fence runs)."""
    rc = _run_guard(
        head_ref="llm-triage/cross_table_audit-x",
        changed_paths=["docs/llm_triage_proposals/abc.binding.txt"],
        labels=["llm-data-triage"],
    )
    assert rc == 0


def test_agent_branch_with_engine_label_passes():
    """Agent PR WITH the engine label ⇒ pass (the engine fence runs)."""
    rc = _run_guard(
        head_ref="llm-triage/engine-hold-1",
        changed_paths=["docs/engine_llm_triage_proposals/h1.dossier.md"],
        labels=["engine-llm-triage"],
    )
    assert rc == 0


def test_completely_unrelated_pr_passes():
    """A normal feature PR — no agent signal, no label ⇒ pass."""
    rc = _run_guard(
        head_ref="feat/new-indicator",
        changed_paths=["tpcore/indicators/adx.py"],
        labels=[],
    )
    assert rc == 0


def test_provenance_artifact_with_label_passes():
    """Agent provenance artifact + correct label ⇒ pass (fence handles)."""
    rc = _run_guard(
        head_ref="renamed",
        changed_paths=["docs/llm_triage_proposals/x.dossier.md"],
        labels=["llm-data-triage"],
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# CLI integration test against a THROWAWAY git repo (PR #61 isolation).
# ---------------------------------------------------------------------------

def test_cli_against_throwaway_repo(tmp_path, monkeypatch):
    """Drives main() end-to-end via env vars in an isolated tmp repo —
    asserts the agent-no-label case exits non-zero and never touches the
    host repo."""
    repo = tmp_path / "throwaway"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True
        )

    git("init", "-q")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (repo / "README.md").write_text("x", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")

    # Agent-shaped PR context with NO label ⇒ guard must exit non-zero.
    monkeypatch.setenv("GUARD_HEAD_REF", "llm-triage/some-ref")
    monkeypatch.setenv(
        "GUARD_CHANGED_PATHS",
        "docs/llm_triage_proposals/some-ref.binding.txt",
    )
    monkeypatch.setenv("GUARD_LABELS", "")

    rc = _mod.main_from_env()
    assert rc != 0

    # Host-guard: no new worktree / branch leaked into the real repo.
    host = subprocess.run(
        ["git", "branch", "--list", "llm-triage/*"],
        cwd=_HOST_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "llm-triage/some-ref" not in host.stdout
