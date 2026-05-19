"""Unit tests for the disk-based shrinkage_detector check.

The check used to query ``platform.application_log`` for a structlog
event that has no structlog→DB bridge in this repo — so it was vacuous
(could never fire). These tests pin the re-keyed, disk-archive-based
behaviour: a real ``.csv.gz`` shrink must produce a FAIL finding;
stable archives produce OK; an *uncheckable* source (no archive / <2
snapshots / empty data dir) must NOT report a silent green OK — it is
WARN ("not green, needs attention"), because reporting green when
nothing was actually compared is a false all-clear on a live-money
data-integrity guardrail (Prep 2).

The archive root is steered via the ``TP_DATA_DIR`` env seam (Prep 1)
so the tests never touch the real repo ``data/`` dir.
"""
from __future__ import annotations

import gzip
import importlib
from pathlib import Path

import pytest

import tpcore.ingestion.csv_archive as csv_archive

audit = importlib.import_module("scripts.audit_data_pipeline")


def _write_archive(root: Path, source: str, stamp: str, n_rows: int) -> None:
    d = root / f"{source}_archive"
    d.mkdir(parents=True, exist_ok=True)
    gz = d / f"{source}_{stamp}.csv.gz"
    with gzip.open(gz, "wt", encoding="utf-8", newline="") as fh:
        fh.write("a,b\n")
        for i in range(n_rows):
            fh.write(f"{i},{i}\n")


@pytest.fixture()
def archive_root(tmp_path, monkeypatch):
    # Use the Prep-1 TP_DATA_DIR env seam to relocate the archive root
    # onto tmp_path — the real repo data/ dir is never touched.
    monkeypatch.setenv("TP_DATA_DIR", str(tmp_path))
    assert csv_archive.repo_data_dir() == tmp_path
    return tmp_path


def _findings_by_check(findings, name):
    return [f for f in findings if f.check_name == name]


def _only(findings):
    f = _findings_by_check(findings, "shrinkage_detector")
    assert len(f) == 1
    return f[0]


def test_shrinkage_fail_when_archive_shrinks(archive_root):
    # Prior archive 1000 rows, latest 600 rows → 40% shrink (> 20%).
    src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, src, "20260514T000000Z", 1000)
    _write_archive(archive_root, src, "20260515T000000Z", 600)

    reports, uncheckable = audit._detect_archive_shrinkage()  # noqa: SLF001
    over = [r for r in reports if r.over_threshold]
    assert len(over) == 1
    r = over[0]
    assert r.source == src
    assert r.previous_rows == 1000
    assert r.current_rows == 600
    assert round(r.shrinkage_pct, 2) == 0.40

    findings: list = []
    audit._append_shrinkage_finding(findings, reports, uncheckable)  # noqa: SLF001
    f = _only(findings)
    assert f.phase == "known_knowns"
    assert f.source == "csv_archive"
    assert f.severity == "FAIL"
    ev_sources = {e["source"] for e in f.evidence["over_threshold"]}
    assert src in ev_sources
    e = next(e for e in f.evidence["over_threshold"] if e["source"] == src)
    assert e["previous_rows"] == 1000
    assert e["current_rows"] == 600
    assert "shrinkage_pct" in e
    assert f.recommended_action


def test_shrinkage_ok_when_all_sources_compared_and_stable(archive_root):
    # EVERY archive source has ≥2 stable snapshots → genuinely compared,
    # none over → honest green OK.
    for src in audit.ARCHIVE_SOURCES:
        _write_archive(archive_root, src, "20260514T000000Z", 1000)
        _write_archive(archive_root, src, "20260515T000000Z", 1000)

    reports, uncheckable = audit._detect_archive_shrinkage()  # noqa: SLF001
    assert uncheckable == []
    assert all(not r.over_threshold for r in reports)
    assert len(reports) == len(audit.ARCHIVE_SOURCES)

    findings: list = []
    audit._append_shrinkage_finding(findings, reports, uncheckable)  # noqa: SLF001
    f = _only(findings)
    assert f.severity == "OK"


def test_empty_archive_root_is_WARN_not_silent_OK(archive_root):
    # The bite: a fresh/empty data/ — NO source is checkable. Pre-fix
    # this produced severity OK ("I checked nothing" reported green).
    reports, uncheckable = audit._detect_archive_shrinkage()  # noqa: SLF001
    assert reports == []
    assert {u["source"] for u in uncheckable} == set(audit.ARCHIVE_SOURCES)

    findings: list = []
    audit._append_shrinkage_finding(findings, reports, uncheckable)  # noqa: SLF001
    f = _only(findings)
    # WARN is NOT green in this audit's OK|WARN|FAIL vocabulary.
    assert f.severity == "WARN"
    assert f.severity != "OK"

    # Prove the pre-fix behaviour would genuinely have been OK: with the
    # OLD signature (no uncheckable arg, empty reports) the else-branch
    # emitted OK. This assertion bites — it documents the fixed bug.
    pre_fix: list = []
    audit._append_shrinkage_finding(pre_fix, [])  # uncheckable defaults []  # noqa: SLF001
    assert _only(pre_fix).severity == "OK"

    ev = f.evidence["uncheckable"]
    assert {u["source"] for u in ev} == set(audit.ARCHIVE_SOURCES)
    assert all("reason" in u for u in ev)
    assert f.recommended_action


def test_mixed_some_compared_some_uncheckable_none_over_is_WARN(archive_root):
    # First source has 2 stable snapshots (compared, fine); the rest
    # have none (uncheckable). No source is over → WARN, not OK.
    compared_src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, compared_src, "20260514T000000Z", 500)
    _write_archive(archive_root, compared_src, "20260515T000000Z", 500)

    reports, uncheckable = audit._detect_archive_shrinkage()  # noqa: SLF001
    assert [r.source for r in reports] == [compared_src]
    assert all(not r.over_threshold for r in reports)
    uncheckable_srcs = {u["source"] for u in uncheckable}
    assert compared_src not in uncheckable_srcs
    assert uncheckable_srcs == set(audit.ARCHIVE_SOURCES[1:])

    findings: list = []
    audit._append_shrinkage_finding(findings, reports, uncheckable)  # noqa: SLF001
    f = _only(findings)
    assert f.severity == "WARN"
    assert f.severity != "OK"
    ev_unchk = {u["source"] for u in f.evidence["uncheckable"]}
    assert ev_unchk == set(audit.ARCHIVE_SOURCES[1:])
    ev_cmp = {c["source"] for c in f.evidence["compared"]}
    assert ev_cmp == {compared_src}


def test_FAIL_precedence_over_uncheckable(archive_root):
    # One source genuinely shrank > 20% AND another is uncheckable.
    # FAIL must win (precedence FAIL > WARN > OK).
    over_src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, over_src, "20260514T000000Z", 1000)
    _write_archive(archive_root, over_src, "20260515T000000Z", 100)  # 90% drop

    reports, uncheckable = audit._detect_archive_shrinkage()  # noqa: SLF001
    assert any(r.over_threshold for r in reports)
    assert len(uncheckable) >= 1  # the other ARCHIVE_SOURCES

    findings: list = []
    audit._append_shrinkage_finding(findings, reports, uncheckable)  # noqa: SLF001
    f = _only(findings)
    assert f.severity == "FAIL"
    assert "over_threshold" in f.evidence


def test_single_archive_is_uncheckable_not_OK(archive_root):
    # Only one archive for a source → detect_shrinkage returns None →
    # uncheckable ("no prior snapshot"), NOT a silent OK.
    src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, src, "20260515T000000Z", 600)

    reports, uncheckable = audit._detect_archive_shrinkage()  # noqa: SLF001
    assert reports == []
    assert src in {u["source"] for u in uncheckable}

    findings: list = []
    audit._append_shrinkage_finding(findings, reports, uncheckable)  # noqa: SLF001
    f = _only(findings)
    assert f.severity == "WARN"
    assert f.severity != "OK"
