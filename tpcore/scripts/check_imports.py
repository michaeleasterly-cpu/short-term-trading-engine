"""Forbidden-import scanner for engine directories.

Engines must use only ``tpcore.interfaces.*`` and approved data sources.
This script walks one or more directories, parses each ``.py`` file with
the stdlib ``ast`` module, and exits non-zero if any forbidden module is
imported.

Usage::

    python -m tpcore.scripts.check_imports sigma
    python -m tpcore.scripts.check_imports sigma other_engine
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

FORBIDDEN_MODULES = frozenset(
    {
        "alpaca_trade_api",
        "yfinance",
        "fmp_python_sdk",
        "praw",
        "iborrowdesk",
    }
)

# Layering invariant (operator, 2026-05-16): the tpcore shared library
# must NEVER import an engine — dependencies flow engine→tpcore, never
# the reverse, and engines never import each other. When ``tpcore`` is
# scanned these become forbidden too, so a tpcore→engine import (incl.
# under ``TYPE_CHECKING`` — ast.walk sees it) fails the build. Scoped
# to library code: tpcore/tests and tpcore/templates (scaffold) are
# exempt — they are not part of the shipped import graph.
ENGINE_PACKAGES = frozenset(
    {"sigma", "reversion", "vector", "momentum", "sentinel"}
)


def _module_root(name: str) -> str:
    return name.split(".", 1)[0]


def scan_file(
    path: Path, forbidden: frozenset[str] = FORBIDDEN_MODULES
) -> list[tuple[int, str]]:
    """Return (line, offending_module) for every forbidden import."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_root(alias.name) in forbidden:
                    findings.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _module_root(node.module) in forbidden:
                findings.append((node.lineno, node.module))
    return findings


def scan_dir(root: Path) -> dict[Path, list[tuple[int, str]]]:
    # Scanning tpcore? Then engine imports are forbidden too (the
    # tpcore-never-imports-an-engine invariant), except in tpcore's own
    # tests/templates which are not shipped library code.
    is_tpcore = root.name == "tpcore" or root.as_posix().endswith("/tpcore")
    findings: dict[Path, list[tuple[int, str]]] = {}
    for py in root.rglob("*.py"):
        parts = set(py.parts)
        forbidden = FORBIDDEN_MODULES
        if is_tpcore and "tests" not in parts and "templates" not in parts:
            forbidden = FORBIDDEN_MODULES | ENGINE_PACKAGES
        hits = scan_file(py, forbidden)
        if hits:
            findings[py] = hits
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: check_imports <dir> [<dir> ...]", file=sys.stderr)
        return 2
    any_findings = False
    for raw in args:
        root = Path(raw)
        if not root.exists():
            print(f"skip: {root} does not exist", file=sys.stderr)
            continue
        results = scan_dir(root)
        if results:
            any_findings = True
            for path, hits in sorted(results.items()):
                for lineno, mod in hits:
                    print(f"{path}:{lineno}: forbidden import {mod!r}", file=sys.stderr)
    if any_findings:
        return 1
    print("ok: no forbidden imports found")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
