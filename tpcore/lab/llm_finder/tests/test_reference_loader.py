"""Reference-loader tests — Task #25 §5 + §10.1.

Covers:
- 3 mandatory-always-include bundles loaded regardless of ``names``
- Named bundle loading + ordering determinism
- Fail-loud on missing / empty / stub bundles
- ``available_bundles`` enumeration
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tpcore.lab.llm_finder import MANDATORY_REFERENCE_BUNDLES
from tpcore.lab.llm_finder.reference_loader import (
    ReferenceEmptyError,
    ReferenceExcerpt,
    ReferenceNotFoundError,
    ReferenceStubError,
    available_bundles,
    load_reference_bundles,
)

# ───────────────────────── tmp_path bundle factory ─────────────────────────


def _seed(root: Path, name: str, content: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _seed_mandatory(root: Path) -> None:
    """Create the 3 mandatory bundles with non-empty content."""
    for name in MANDATORY_REFERENCE_BUNDLES:
        _seed(root, name, f"# {name}\n\nReal content here.\n")


# ───────────────────────── happy paths ─────────────────────────


def test_mandatory_bundles_always_loaded(tmp_path: Path) -> None:
    """Calling with names=() still returns the 3 mandatory bundles."""
    _seed_mandatory(tmp_path)
    excerpts = load_reference_bundles(names=(), root=tmp_path)
    loaded_names = {e.name for e in excerpts}
    assert loaded_names == set(MANDATORY_REFERENCE_BUNDLES)
    assert all(e.is_mandatory for e in excerpts)


def test_mandatory_bundles_alphabetical_order(tmp_path: Path) -> None:
    """Mandatory bundles come back in sorted order (deterministic)."""
    _seed_mandatory(tmp_path)
    excerpts = load_reference_bundles(names=(), root=tmp_path)
    names_in_order = [e.name for e in excerpts]
    assert names_in_order == sorted(MANDATORY_REFERENCE_BUNDLES)


def test_named_bundle_unioned(tmp_path: Path) -> None:
    """Named bundle appears AFTER the mandatory bundles."""
    _seed_mandatory(tmp_path)
    _seed(tmp_path, "carver_systematic_trading", "# Carver\n")
    excerpts = load_reference_bundles(
        names=("carver_systematic_trading",), root=tmp_path
    )
    names_in_order = [e.name for e in excerpts]
    assert names_in_order[-1] == "carver_systematic_trading"
    assert not excerpts[-1].is_mandatory


def test_named_bundle_dedup_against_mandatory(tmp_path: Path) -> None:
    """Requesting a mandatory bundle by name does NOT double-include it."""
    _seed_mandatory(tmp_path)
    excerpts = load_reference_bundles(
        names=("dsr_ntrials_discipline",), root=tmp_path
    )
    assert len(excerpts) == len(MANDATORY_REFERENCE_BUNDLES)
    dsr_excerpts = [e for e in excerpts if e.name == "dsr_ntrials_discipline"]
    assert len(dsr_excerpts) == 1
    assert dsr_excerpts[0].is_mandatory


def test_excerpt_fields_populated(tmp_path: Path) -> None:
    _seed_mandatory(tmp_path)
    excerpts = load_reference_bundles(names=(), root=tmp_path)
    for e in excerpts:
        assert isinstance(e, ReferenceExcerpt)
        assert e.byte_count > 0
        assert e.content
        assert e.path.is_file()


# ───────────────────────── fail-loud paths ─────────────────────────


def test_missing_mandatory_bundle_raises(tmp_path: Path) -> None:
    """A mandatory bundle missing from disk = fail-loud."""
    # Don't seed mandatory bundles.
    with pytest.raises(ReferenceNotFoundError, match="dsr_ntrials_discipline"):
        load_reference_bundles(names=(), root=tmp_path)


def test_missing_named_bundle_raises(tmp_path: Path) -> None:
    """A requested non-mandatory bundle missing from disk = fail-loud."""
    _seed_mandatory(tmp_path)
    with pytest.raises(ReferenceNotFoundError, match="nonexistent"):
        load_reference_bundles(names=("nonexistent",), root=tmp_path)


def test_empty_bundle_raises(tmp_path: Path) -> None:
    """Zero-byte file = fail-loud."""
    _seed_mandatory(tmp_path)
    _seed(tmp_path, "empty_one", "")
    with pytest.raises(ReferenceEmptyError, match="empty_one"):
        load_reference_bundles(names=("empty_one",), root=tmp_path)


def test_stub_bundle_raises(tmp_path: Path) -> None:
    """Stub-sentinel marker = fail-loud (spec §7.4-§7.5)."""
    _seed_mandatory(tmp_path)
    _seed(tmp_path, "stub_one", "# Stub\n\n[operator-pending content]\n")
    with pytest.raises(ReferenceStubError, match="stub_one"):
        load_reference_bundles(names=("stub_one",), root=tmp_path)


def test_stub_marker_in_mandatory_raises(tmp_path: Path) -> None:
    """Even mandatory bundle with stub-marker fails — defense against shipping placeholders."""
    _seed_mandatory(tmp_path)
    # Overwrite one mandatory bundle with stub content.
    _seed(
        tmp_path,
        "dsr_ntrials_discipline",
        "# DSR\n\n[operator-pending content]\n",
    )
    with pytest.raises(ReferenceStubError, match="dsr_ntrials_discipline"):
        load_reference_bundles(names=(), root=tmp_path)


# ───────────────────────── available_bundles ─────────────────────────


def test_available_bundles_empty(tmp_path: Path) -> None:
    """Empty root → empty tuple."""
    assert available_bundles(root=tmp_path) == ()


def test_available_bundles_nonexistent_root(tmp_path: Path) -> None:
    """Nonexistent root → empty tuple (no exception)."""
    assert available_bundles(root=tmp_path / "doesnotexist") == ()


def test_available_bundles_sorted(tmp_path: Path) -> None:
    """Enumerates ``.md`` files in alphabetical order."""
    _seed(tmp_path, "zeta", "z")
    _seed(tmp_path, "alpha", "a")
    _seed(tmp_path, "mu", "m")
    assert available_bundles(root=tmp_path) == ("alpha", "mu", "zeta")


# ───────────────────────── real on-disk smoke ─────────────────────────


def test_real_on_disk_mandatory_bundles_load() -> None:
    """The real ``docs/lab_emitter_references/`` carries the 3 mandatory bundles
    (shipped via PR #230 + #232). This is the load-bearing smoke test."""
    excerpts = load_reference_bundles(names=())
    loaded_names = {e.name for e in excerpts}
    assert loaded_names == set(MANDATORY_REFERENCE_BUNDLES)
    # Sanity: real content is non-trivial.
    for e in excerpts:
        assert e.byte_count > 1_000, f"{e.name} suspiciously small ({e.byte_count} bytes)"
