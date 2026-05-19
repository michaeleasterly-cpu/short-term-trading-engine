"""Anti-rot tripwire: docs/superpowers/checklists/lab_candidate_readiness.md
must exist, keep its load-bearing clauses, AND stay cross-linked from the
Lab→ECR handoff (ops/lab/dossier.py::_next_step).

Direct sibling of tests/test_dev_pipeline_standard_present.py (the
DEV_PIPELINE_STANDARD anti-rot sentinel) — same shape: a PRESENCE +
intactness check of a canonical gating doc, NOT a behavioural test of
the process (the process is operator + reviewer discipline,
un-testable here).

The cross-link half is the SP-C "doc-only; the only test is a
cross-link/consistency assertion if one is added" deliverable: it
mirrors how engine_readiness.md is named from dossier._next_step for the
promote_new path, so a Lab candidate cannot bypass the readiness gate
the same way an engine ADD cannot bypass engine_readiness. RED if the
checklist is gutted OR the cross-link is removed; GREEN with both.

Self-exemption (conservative, by construction): the scan reads ONLY the
two target files and never globs the test tree, so this file's own
literals cannot self-trip the tripwire — mirrors the documented
self-exemption reasoning in tests/test_dev_pipeline_standard_present.py.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "superpowers" / "checklists" / "lab_candidate_readiness.md"
_DOSSIER = _REPO / "ops" / "lab" / "dossier.py"

# Load-bearing literal anchors — if any vanishes, the checklist has been
# silently gutted; red CI. Keep this list == the doc's must-contain
# clauses (the SP-C "Delivers" set: feature-flag-variant pattern, single
# pre-registered primary hypothesis, byte-identical live path, n_trials
# ledger acknowledgement, roster-targeting prerequisite, sacred gate,
# the Vector pilot pointer, the non-optional framing).
_ANCHORS = (
    "Single pre-registered primary hypothesis",
    "feature-flag-variant pattern",
    "Byte-identical live path",
    "n_trials ledger acknowledgement",
    "cumulative (not per-run) DSR",
    "tpcore.lab.ledger",
    "lab_targetable_engines()",
    "The gate is sacred",
    "2026-05-19-vector-composite-lab-candidate.md",
    "These ten sections are non-optional",
    "engine_readiness.md",
)

# The cross-link contract: the Lab→ECR handoff (dossier._next_step)
# names this checklist, symmetric to its existing engine_readiness
# reference. Removing it reds CI.
_CROSSLINK = "lab_candidate_readiness"


def test_lab_candidate_readiness_present_and_intact() -> None:
    assert _DOC.is_file(), f"missing canonical checklist: {_DOC}"
    src = _DOC.read_text()
    missing = [a for a in _ANCHORS if a not in src]
    assert not missing, (
        "lab_candidate_readiness.md lost load-bearing clauses "
        f"(silent rot): {missing}")


def test_lab_candidate_readiness_crosslinked_from_lab_to_ecr_handoff() -> None:
    assert _DOSSIER.is_file(), f"missing Lab dossier module: {_DOSSIER}"
    src = _DOSSIER.read_text()
    assert _CROSSLINK in src, (
        "ops/lab/dossier.py no longer cross-references "
        "lab_candidate_readiness — a Lab candidate could bypass the "
        "readiness gate the way an engine ADD cannot bypass "
        "engine_readiness. Restore the _next_step cross-link.")
