"""Report-only tpcore-duplication audit (Lean codebase-health, Phase P3c).

This is a REGISTERED ANALYSIS TOOL (allowlisted in the P3b orphan-script
gate): it is invoked on-demand, not daemon-wired.

It is the input artifact generator for the deferred *Actual de-dup /
tpcore reconsolidation* phase of the "Lean dev-env + codebase-health"
spec/plan (``docs/superpowers/specs/2026-05-19-lean-dev-env-codebase-
health-design.md`` §3 P3(c) / the matching plan's Phase P3 → P3c). It
emits a point-in-time findings snapshot to
``docs/audits/2026-05-19-tpcore-duplication-audit.md``.

Analysis path
-------------
The spec/plan name ``pylint --enable=duplicate-code`` *or* an AST-hash
near-dup scan, whichever is available — pylint is report-only here and
must NOT be added as a dependency or a CI gate. ``pylint`` is NOT a
project dependency and is not installed in this environment, so this
tool takes the deterministic **AST-hash near-duplicate scan** path: it
parses every module in the engine packages + ``tpcore``, normalises
each function/method body by stripping positions and identifier names
(so renamed copies still collide), structurally hashes it, and reports
hash-collision clusters that span ≥2 source locations. Output is fully
sorted/deterministic — no clock, no network, no DB, no git.

HARD INVARIANT: this tool is analysis-only. It READS source and writes
exactly one markdown doc. It MUST NOT modify any source file. The
``--check`` / ``--dry-run`` mode runs the full analysis WITHOUT writing
the doc (used by the thin test).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Engine packages + the shared library. ``tests``/``data`` are excluded
# (mirrors the spec's ``--ignore-paths='.*/(tests|data)/.*'``).
_PACKAGES: tuple[str, ...] = (
    "reversion",
    "vector",
    "momentum",
    "sentinel",
    "canary",
    "tpcore",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AUDIT_DOC = (
    _REPO_ROOT / "docs" / "audits" / "2026-05-19-tpcore-duplication-audit.md"
)

# A body must have at least this many AST statement-nodes to be worth
# flagging — tiny stubs (``return None`` / ``pass`` / one-line wrappers)
# collide trivially and are not actionable de-dup signal.
_MIN_NODES = 18


def _iter_source_files(root: Path) -> list[Path]:
    """All ``*.py`` under a package, excluding tests/data dirs.

    Deterministic (sorted). Read-only.
    """
    out: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        parts = set(path.relative_to(_REPO_ROOT).parts)
        if "tests" in parts or "data" in parts:
            continue
        out.append(path)
    return out


class _Normaliser(ast.NodeTransformer):
    """Strip identifier names + literals so renamed copies still hash equal.

    Names → ``_N``, attribute accesses → ``_A``, constants → ``_C``. The
    structural shape (control flow, call arity, statement nesting) is
    what survives — i.e. genuine copy-paste-and-rename duplication.
    """

    def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Name(id="_N", ctx=node.ctx), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
        self.generic_visit(node)
        return ast.copy_location(
            ast.Attribute(value=node.value, attr="_A", ctx=node.ctx), node
        )

    def visit_arg(self, node: ast.arg) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.arg(arg="_a", annotation=None), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
        return ast.copy_location(ast.Constant(value="_C"), node)


@dataclass(frozen=True)
class _Unit:
    """One hashable function/method body."""

    rel_path: str
    qualname: str
    lineno: int
    n_nodes: int
    digest: str


def _walk(
    node: ast.AST, scope: list[str], rel: str, units: list[_Unit]
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = ".".join([*scope, child.name])
            body_mod = ast.Module(body=list(child.body), type_ignores=[])
            _Normaliser().visit(body_mod)
            ast.fix_missing_locations(body_mod)
            n_nodes = sum(1 for _ in ast.walk(body_mod))
            if n_nodes >= _MIN_NODES:
                dumped = ast.dump(body_mod, annotate_fields=False)
                digest = hashlib.sha256(
                    dumped.encode("utf-8")
                ).hexdigest()[:16]
                units.append(
                    _Unit(
                        rel_path=rel,
                        qualname=qual,
                        lineno=child.lineno,
                        n_nodes=n_nodes,
                        digest=digest,
                    )
                )
            _walk(child, [*scope, child.name], rel, units)
        elif isinstance(child, ast.ClassDef):
            _walk(child, [*scope, child.name], rel, units)
        else:
            _walk(child, scope, rel, units)


def _collect_units(files: list[Path]) -> list[_Unit]:
    units: list[_Unit] = []
    for path in files:
        rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            # Read-only tool: a single unparseable file is skipped, never
            # mutated; the audit notes coverage is best-effort.
            continue
        _walk(tree, [], rel, units)
    return units


@dataclass(frozen=True)
class _Cluster:
    digest: str
    n_nodes: int
    members: tuple[_Unit, ...]

    @property
    def cross_package(self) -> bool:
        pkgs = {m.rel_path.split("/", 1)[0] for m in self.members}
        return len(pkgs) > 1


def analyse() -> tuple[list[_Cluster], int, int]:
    """Run the read-only AST-hash scan.

    Returns (clusters, n_files_scanned, n_units). Pure: no I/O beyond
    reading source. Fully deterministic.
    """
    files: list[Path] = []
    for pkg in _PACKAGES:
        root = _REPO_ROOT / pkg
        if root.is_dir():
            files.extend(_iter_source_files(root))
    files.sort()
    units = _collect_units(files)

    by_digest: dict[str, list[_Unit]] = defaultdict(list)
    for u in units:
        by_digest[u.digest].append(u)

    clusters: list[_Cluster] = []
    for digest, members in by_digest.items():
        # A cluster = same structural hash across ≥2 distinct source
        # locations (file+qualname).
        locs = {(m.rel_path, m.qualname) for m in members}
        if len(locs) < 2:
            continue
        ordered = tuple(
            sorted(members, key=lambda m: (m.rel_path, m.lineno, m.qualname))
        )
        clusters.append(
            _Cluster(
                digest=digest,
                n_nodes=ordered[0].n_nodes,
                members=ordered,
            )
        )

    # Deterministic ordering: cross-package first (highest Phase-5
    # signal), then by size desc, then by member count desc, then by
    # first member's path for a total stable order.
    clusters.sort(
        key=lambda c: (
            not c.cross_package,
            -c.n_nodes,
            -len(c.members),
            c.members[0].rel_path,
            c.members[0].qualname,
        )
    )
    return clusters, len(files), len(units)


def _render(clusters: list[_Cluster], n_files: int, n_units: int) -> str:
    cross = [c for c in clusters if c.cross_package]
    intra = [c for c in clusters if not c.cross_package]
    lines: list[str] = []
    lines.append("# tpcore duplication audit — point-in-time snapshot")
    lines.append("")
    lines.append(
        "**Generated by** `scripts/audit_code_duplication.py` "
        "(report-only; reads source, writes only this doc — modifies no "
        "source file)."
    )
    lines.append("")
    lines.append(
        "This is a **point-in-time snapshot** and the input artifact for "
        "the deferred *Actual de-dup / tpcore reconsolidation* phase of "
        "the **Lean dev-env + codebase-health** spec/plan "
        "(`docs/superpowers/specs/2026-05-19-lean-dev-env-codebase-"
        "health-design.md` §3 P3(c); the matching plan's Phase P3 → "
        "P3c). Re-run the script to refresh; it is not a CI gate."
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "`pylint --enable=duplicate-code` is **not a project dependency** "
        "and is not installed (the spec/plan permits either pylint or an "
        "AST-hash scan; pylint is report-only here and must NOT become a "
        "dep or gate). This snapshot therefore uses a deterministic "
        "**AST-hash near-duplicate scan**: every function/method body in "
        f"`{', '.join(_PACKAGES)}` (excluding `tests/`/`data/`) is "
        "normalised (identifier names, attribute names, constants "
        "stripped) and structurally SHA-256-hashed; bodies with "
        f"≥ {_MIN_NODES} AST nodes that share a hash across ≥ 2 distinct "
        "source locations are reported. Renamed copy-paste duplication "
        "still collides; output is fully sorted/deterministic."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Source files scanned: **{n_files}**")
    lines.append(f"- Candidate bodies (≥ {_MIN_NODES} nodes): **{n_units}**")
    lines.append(f"- Duplication clusters: **{len(clusters)}**")
    lines.append(
        f"- Cross-package clusters (highest de-dup signal): "
        f"**{len(cross)}**"
    )
    lines.append(f"- Intra-package clusters: **{len(intra)}**")
    lines.append("")

    def _emit(title: str, group: list[_Cluster]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not group:
            lines.append("_None._")
            lines.append("")
            return
        for i, c in enumerate(group, 1):
            pkgs = sorted({m.rel_path.split("/", 1)[0] for m in c.members})
            lines.append(
                f"### {i}. hash `{c.digest}` — {c.n_nodes} nodes, "
                f"{len(c.members)} sites, packages: {', '.join(pkgs)}"
            )
            lines.append("")
            for m in c.members:
                lines.append(
                    f"- `{m.rel_path}` :: `{m.qualname}` (line {m.lineno})"
                )
            lines.append("")

    _emit("Cross-package duplication (Phase-5 priority)", cross)
    _emit("Intra-package duplication", intra)
    lines.append("---")
    lines.append("")
    lines.append(
        "_Findings only — no refactor performed (cross-session-safe). "
        "The de-dup refactor is the deferred Phase 5 of the Lean "
        "codebase-health plan._"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report-only tpcore-duplication audit (Lean P3c). Reads "
            "source, writes one markdown doc; modifies no source."
        )
    )
    parser.add_argument(
        "--check",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Run the full analysis WITHOUT writing the audit doc.",
    )
    args = parser.parse_args(argv)

    clusters, n_files, n_units = analyse()
    rendered = _render(clusters, n_files, n_units)

    if args.dry_run:
        print(
            f"[dry-run] analysed {n_files} files, {n_units} bodies, "
            f"{len(clusters)} clusters — doc NOT written "
            f"({len(rendered)} chars would be emitted)."
        )
        return 0

    _AUDIT_DOC.parent.mkdir(parents=True, exist_ok=True)
    _AUDIT_DOC.write_text(rendered, encoding="utf-8")
    try:
        shown = _AUDIT_DOC.relative_to(_REPO_ROOT)
    except ValueError:
        shown = _AUDIT_DOC
    print(
        f"Wrote {shown} — "
        f"{len(clusters)} clusters across {n_files} files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
