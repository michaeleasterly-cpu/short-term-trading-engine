"""Structural tests for the risk_close_ledger migration (#251 B1.1).

No DB / no alembic runtime: these parse the migration *files* (the same
discipline the repo uses elsewhere — pure structural assertions) to
prove (1) the migration graph is a SINGLE LINEAR head (no multi-head —
a multi-head would silently skip the ledger in a real upgrade and
re-open the dual-decrement fail-open) and (2) the new migration creates
``platform.risk_close_ledger`` with PK ``(engine, trade_id)`` and a
``recorded_at`` default, and drops it on downgrade.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_VERSIONS = Path(__file__).resolve().parents[2] / "platform" / "migrations" / "versions"
_LEDGER = _VERSIONS / "20260519_0000_risk_close_ledger.py"

_REV_RE = re.compile(r'^revision:\s*str\s*=\s*"([0-9_]+)"', re.M)
_DOWN_RE = re.compile(r'^down_revision:\s*(?:str\s*\|\s*None|str)?\s*=\s*"([0-9_]+)"', re.M)


def _scan_graph() -> tuple[set[str], dict[str, str]]:
    revs: set[str] = set()
    edges: dict[str, str] = {}  # revision -> down_revision
    for path in _VERSIONS.glob("*.py"):
        text = path.read_text()
        rev_m = _REV_RE.search(text)
        if rev_m is None:
            continue
        rev = rev_m.group(1)
        revs.add(rev)
        down_m = _DOWN_RE.search(text)
        if down_m is not None:
            edges[rev] = down_m.group(1)
    return revs, edges


def test_migration_graph_is_single_linear_head() -> None:
    """Exactly one head; every down_revision points at a real revision."""
    revs, edges = _scan_graph()
    pointed_to = set(edges.values())
    heads = revs - pointed_to
    assert heads == {"20260519_0000"}, (
        f"expected the ledger migration to be the sole head, got heads={sorted(heads)} "
        f"(a multi-head graph would skip risk_close_ledger on upgrade → re-opens "
        f"the dual-decrement fail-open)"
    )
    # No dangling down_revision (every parent must exist).
    for rev, down in edges.items():
        assert down in revs, f"{rev} chains to unknown down_revision {down!r}"


def test_ledger_migration_chains_to_prior_head() -> None:
    _, edges = _scan_graph()
    assert edges["20260519_0000"] == "20260517_0900"


def test_ledger_table_and_pk_shape() -> None:
    text = _LEDGER.read_text()
    up = text.split("def upgrade()")[1].split("def downgrade()")[0]
    assert "CREATE TABLE IF NOT EXISTS platform.risk_close_ledger" in up
    assert re.search(r"engine\s+text\s+NOT NULL", up)
    assert re.search(r"trade_id\s+text\s+NOT NULL", up)
    assert re.search(r"recorded_at\s+timestamptz\s+NOT NULL\s+DEFAULT\s+now\(\)", up)
    assert re.search(r"PRIMARY KEY\s*\(\s*engine\s*,\s*trade_id\s*\)", up)


def test_ledger_migration_downgrade_drops_table() -> None:
    text = _LEDGER.read_text()
    down = text.split("def downgrade()")[1]
    assert "DROP TABLE IF EXISTS platform.risk_close_ledger" in down


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
