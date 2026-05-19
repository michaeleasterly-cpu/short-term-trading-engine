"""Phase P3b — orphan one-off script detector (structural anti-accretion gate).

CLAUDE.md bans one-off ``scripts/foo.py`` scripts: backfills / special
pulls / re-validations MUST run through the canonical ``ops.py`` stage,
and any script that exists must be wired into a wrapper, a stage
registry, a daemon/launchd plist, ``ci.yml``, ``pyproject.toml``, or
exercised by a test/module. A script with **zero** such references is an
orphan — exactly the one-off-rat's-nest class the prose rule forbids.
Prose rules drift; this is the mechanical enforcement.

For each ``scripts/*.py`` (excluding ``scripts/tests/`` and
``__init__.py``) this test asserts the script is referenced (by stem or
``scripts/<name>.py`` path) in at least one repo *reference file*
(``*.py``/``*.sh``/``*.yml``/``*.yaml``/``*.toml``/``*.plist``),
EXCLUDING ``data/``, ``.git``, ``.venv``, ``__pycache__``,
``*_archive``/``archive``, and ``.claude`` (the last because
``.claude/worktrees/`` holds ephemeral git-worktree mirrors of
``scripts/`` itself — counting them would make every script trivially
self-referential and render this gate structurally inert).

``_ALLOWLIST`` names deliberate standalone tools that are legitimately
invoked by hand / out-of-band and need no wrapper or registry. Adding a
script here is a *recorded decision*, not a silent bypass.

Deterministic: static file reads + a bounded ``rglob`` only. It NEVER
runs git/DB/network and never touches the live repo's git.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Directories whose contents are NOT a legitimate "reference" to a
# script: vendored/generated/scratch trees and git internals. ``.claude``
# is excluded because ``.claude/worktrees/`` are ephemeral git-worktree
# copies of ``scripts/`` — including them would self-reference every
# script and make this gate inert.
_EXCLUDE_DIR_NAMES = frozenset(
    {
        "data",
        ".git",
        ".venv",
        "__pycache__",
        ".claude",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
)

_REF_GLOBS = ("*.py", "*.sh", "*.yml", "*.yaml", "*.toml", "*.plist")

# Deliberate standalone tools — invoked by hand / out-of-band, no
# wrapper or stage registry by design. Each entry is a recorded
# decision. Bare script name (no ``.py``).
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # NOTE: git_hygiene is intentionally NOT here — it is
        # scripts/git_hygiene.sh (a shell tool), not a scripts/*.py, so
        # it is outside this .py-only gate's scope entirely. Adding it
        # would be a stale (no scripts/git_hygiene.py) allowlist entry.
        # CI guard for the agent PR label fence — a standalone gate
        # invoked directly from ci.yml; deliberate, listed for
        # defense-in-depth if that ci ref is ever refactored.
        "agent_pr_label_guard",
        # Roster manifest generator — the engine-domain generated-shadow
        # tool, run on-demand (``--check`` in CI); deliberate standalone.
        "gen_engine_manifest",
        # P3c read-only duplication audit — an analysis command by
        # registered intent (it MUST NOT mutate source); deliberate
        # standalone tool, allowlisted per the P3b plan.
        "audit_code_duplication",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19 (one-off script, CLAUDE.md bans these). The P3b
        # sweep found 0 references. Deletion is Phase-5-class
        # (code-mutating, out of this plan's scope), so it is tracked
        # here, NOT silently buried: the allowlist entry + this comment
        # + the PR report are the honest record.
        "ingest_tradier_csv",
    }
)


def _is_excluded(path: Path) -> bool:
    """True if *path* lives in a non-reference tree (vendored/scratch)."""
    parts = path.relative_to(REPO_ROOT).parts
    if set(parts) & _EXCLUDE_DIR_NAMES:
        return True
    return any(part.endswith("_archive") or part == "archive" for part in parts)


def _reference_files() -> list[Path]:
    """All repo code/config files that could legitimately wire a script."""
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in _REF_GLOBS:
        for path in REPO_ROOT.rglob(pattern):
            if path in seen or _is_excluded(path) or not path.is_file():
                continue
            seen.add(path)
            out.append(path)
    return out


def _candidate_scripts() -> list[Path]:
    """Every ``scripts/*.py`` subject to the orphan rule."""
    return sorted(
        p
        for p in SCRIPTS_DIR.glob("*.py")
        if p.stem != "__init__"
    )


def test_no_orphan_scripts() -> None:
    """Every scripts/*.py is referenced somewhere, or explicitly allowlisted.

    An unreferenced, non-allowlisted script is a re-accreted one-off —
    CLAUDE.md forbids these. Failure lists each offender so the fix is
    obvious: wire it into a wrapper/stage/CI, or (with a recorded
    justification) add it to ``_ALLOWLIST``.
    """
    ref_texts: list[tuple[Path, str]] = [
        (p, p.read_text(encoding="utf-8", errors="replace"))
        for p in _reference_files()
    ]

    orphans: list[str] = []
    for script in _candidate_scripts():
        name = script.stem
        if name in _ALLOWLIST:
            continue
        path_token = f"scripts/{name}.py"
        referenced = any(
            ref_path != script and (name in text or path_token in text)
            for ref_path, text in ref_texts
        )
        if not referenced:
            orphans.append(name)

    assert not orphans, (
        "Orphan scripts/*.py with ZERO references (no ops.py stage, "
        "no *.sh wrapper, no daemon/launchd plist, no ci.yml, no "
        "pyproject.toml, no test/module). CLAUDE.md bans one-off "
        f"scripts — wire each into the pipeline OR add to _ALLOWLIST "
        f"with a recorded justification: {sorted(orphans)}"
    )


def test_allowlist_entries_are_real_scripts() -> None:
    """No stale allowlist entries — every name maps to an existing script.

    A dangling allowlist entry would silently mask a future same-named
    orphan; keep the allowlist honest.
    """
    stale = sorted(
        name
        for name in _ALLOWLIST
        if not (SCRIPTS_DIR / f"{name}.py").is_file()
    )
    assert not stale, (
        f"_ALLOWLIST names that no longer have a scripts/<name>.py: "
        f"{stale} — remove the stale entries."
    )
