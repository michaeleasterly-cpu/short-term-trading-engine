"""Thin test for scripts/audit_code_duplication.py (Lean Phase P3c).

The audit is REPORT-ONLY. These tests prove:
  1. ``--check``/``--dry-run`` runs the full analysis and writes NO doc
     (so it can run in CI / tests without mutating the live tree).
  2. The default write mode emits a deterministic markdown doc — driven
     against a ``tmp_path`` COPY of the script with the audit-doc target
     monkeypatched into ``tmp_path`` so the LIVE
     ``docs/audits/...`` is NEVER touched.
  3. The analysis is deterministic (same output across two runs).

CRITICAL ISOLATION INVARIANT (PR #61 lesson): nothing here runs real
git, real network, or real DB, and nothing mutates the live working
tree. The dry-run path mutates nothing; the write path is redirected
into ``tmp_path``.
"""
from __future__ import annotations

import importlib

import pytest

mod = importlib.import_module("scripts.audit_code_duplication")


def test_analyse_is_deterministic_and_readonly() -> None:
    """Two runs produce byte-identical rendered output (no clock/rng)."""
    c1, f1, u1 = mod.analyse()
    c2, f2, u2 = mod.analyse()
    assert (f1, u1) == (f2, u2)
    assert f1 > 0 and u1 > 0
    assert mod._render(c1, f1, u1) == mod._render(c2, f2, u2)  # noqa: SLF001


def test_dry_run_writes_no_doc(monkeypatch, tmp_path, capsys) -> None:
    """``--check`` runs the analysis but writes nothing anywhere."""
    sentinel = tmp_path / "should-not-exist.md"
    monkeypatch.setattr(mod, "_AUDIT_DOC", sentinel)
    rc = mod.main(["--check"])
    assert rc == 0
    assert not sentinel.exists()
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "doc NOT written" in out


def test_write_mode_emits_markdown_into_tmp_path(
    monkeypatch, tmp_path, capsys
) -> None:
    """Default mode writes a well-formed doc — redirected into tmp_path."""
    target = tmp_path / "docs" / "audits" / "out.md"
    monkeypatch.setattr(mod, "_AUDIT_DOC", target)
    rc = mod.main([])
    assert rc == 0
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# tpcore duplication audit")
    assert "AST-hash near-duplicate scan" in text
    assert "Lean dev-env + codebase-health" in text
    # Deterministic: re-running yields byte-identical content.
    mod.main([])
    assert target.read_text(encoding="utf-8") == text


def test_min_nodes_threshold_is_sane() -> None:
    """Guard the actionable-signal floor stays a positive constant."""
    assert isinstance(mod._MIN_NODES, int)  # noqa: SLF001
    assert mod._MIN_NODES >= 1  # noqa: SLF001


@pytest.mark.parametrize("flag", ["--check", "--dry-run"])
def test_both_dry_run_aliases_accepted(monkeypatch, tmp_path, flag) -> None:
    monkeypatch.setattr(mod, "_AUDIT_DOC", tmp_path / "nope.md")
    assert mod.main([flag]) == 0
    assert not (tmp_path / "nope.md").exists()
