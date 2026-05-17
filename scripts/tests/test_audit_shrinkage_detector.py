"""Unit tests for the disk-based shrinkage_detector check.

The check used to query ``platform.application_log`` for a structlog
event that has no structlog→DB bridge in this repo — so it was vacuous
(could never fire). These tests pin the re-keyed, disk-archive-based
behaviour: a real ``.csv.gz`` shrink must produce a FAIL finding;
stable archives produce OK; a source with <2 archives is not an alarm.
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
    monkeypatch.setattr(csv_archive, "repo_data_dir", lambda: tmp_path)
    return tmp_path


def _findings_by_check(findings, name):
    return [f for f in findings if f.check_name == name]


def test_shrinkage_fail_when_archive_shrinks(archive_root):
    # Prior archive 1000 rows, latest 600 rows → 40% shrink (> 20%).
    src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, src, "20260514T000000Z", 1000)
    _write_archive(archive_root, src, "20260515T000000Z", 600)

    reports = audit._detect_archive_shrinkage()
    over = [r for r in reports if r.over_threshold]
    assert len(over) == 1
    r = over[0]
    assert r.source == src
    assert r.previous_rows == 1000
    assert r.current_rows == 600
    assert round(r.shrinkage_pct, 2) == 0.40

    findings: list = []
    audit._append_shrinkage_finding(findings, reports)
    fnd = _findings_by_check(findings, "shrinkage_detector")
    assert len(fnd) == 1
    f = fnd[0]
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


def test_shrinkage_ok_when_stable(archive_root):
    src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, src, "20260514T000000Z", 1000)
    _write_archive(archive_root, src, "20260515T000000Z", 1000)

    reports = audit._detect_archive_shrinkage()
    assert all(not r.over_threshold for r in reports)

    findings: list = []
    audit._append_shrinkage_finding(findings, reports)
    f = _findings_by_check(findings, "shrinkage_detector")[0]
    assert f.severity == "OK"


def test_single_archive_is_not_an_alarm(archive_root):
    # Only one archive for the source → detect_shrinkage returns None →
    # nothing to compare → must NOT alarm.
    src = audit.ARCHIVE_SOURCES[0]
    _write_archive(archive_root, src, "20260515T000000Z", 600)

    reports = audit._detect_archive_shrinkage()
    assert reports == [] or all(not r.over_threshold for r in reports)

    findings: list = []
    audit._append_shrinkage_finding(findings, reports)
    f = _findings_by_check(findings, "shrinkage_detector")[0]
    assert f.severity == "OK"


def test_no_archives_at_all_is_not_an_alarm(archive_root):
    reports = audit._detect_archive_shrinkage()
    findings: list = []
    audit._append_shrinkage_finding(findings, reports)
    f = _findings_by_check(findings, "shrinkage_detector")[0]
    assert f.severity == "OK"
