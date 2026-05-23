"""Sentinel: the TKR-14 regex in the P2 migration must equal the runtime regex.

The Postgres CHECK constraint on `ticker_classifications.id` enforces the TKR-14
shape at the database. The Python `tpcore.identity.tkr14.TKR14_REGEX` enforces it
at the application layer. If they drift, rows accepted by Python can fail at the DB
(silent INSERT failures), OR rows accepted by the DB can be rejected by Python
(silent ingest drops). Both fail modes are operationally hostile.

This sentinel reads the SQL-string embedded in the P2 migration and asserts it is
byte-identical to the Python regex constant.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tpcore.identity.tkr14 import TKR14_REGEX

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "versions"
    / "20260524_0000_tkr14_columns_on_ticker_classifications.py"
)


def _extract_module_constant(path: Path, name: str) -> object:
    """Parse a Python file via AST and return the value of the named module-level constant.

    Uses ast.literal_eval so we don't import the alembic migration (which would try to
    import `op` and fail at test-collection time).
    """
    src = path.read_text()
    module = ast.parse(src)
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == name
                and node.value is not None
            ):
                return ast.literal_eval(node.value)
    raise AssertionError(f"Module-level constant {name!r} not found in {path}")


def test_p2_migration_tkr14_regex_matches_tpcore_regex():
    """The `_TKR14_REGEX` constant in the migration MUST equal `tpcore.identity.tkr14.TKR14_REGEX`."""
    migration_regex = _extract_module_constant(_MIGRATION_PATH, "_TKR14_REGEX")
    assert isinstance(migration_regex, str), (
        f"_TKR14_REGEX must be a str literal; got {type(migration_regex).__name__}"
    )
    assert migration_regex == TKR14_REGEX, (
        f"TKR-14 regex DRIFT detected between migration and tpcore.identity.tkr14:\n"
        f"  migration: {migration_regex!r}\n"
        f"  tpcore:    {TKR14_REGEX!r}\n"
        f"If you changed one intentionally, change both. The DB CHECK and the Python "
        f"validator MUST agree exactly or ingest silently misbehaves."
    )
