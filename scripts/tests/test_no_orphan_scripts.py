"""Phase P3b — orphan one-off script detector (structural anti-accretion gate).

CLAUDE.md bans one-off ``scripts/foo.py`` scripts: backfills / special
pulls / re-validations MUST run through the canonical ``ops.py`` stage,
and any script that exists must be wired into a wrapper, a stage
registry, a daemon/launchd plist, ``ci.yml``, ``pyproject.toml``, or
exercised by a test/module. A script with **zero** such references is an
orphan — exactly the one-off-rat's-nest class the prose rule forbids.
Prose rules drift; this is the mechanical enforcement.

A reference is **genuine code-level wiring**, NOT a prose mention. The
detector counts a script as referenced ONLY when:

* its **path token** ``scripts/<name>.py`` appears as an exact token
  (word/path boundaries) in any reference file — wrappers, ``ci.yml``,
  ``pyproject.toml``, plists, ``ops.py`` stage registries and shell
  invocations virtually always use the path; AND for ``*.py`` reference
  files the token must NOT be inside a comment or docstring (those are
  prose, which the rule explicitly does NOT count); OR
* a genuine Python **import** of the module — ``import scripts.<name>``
  / ``from scripts.<name> import …`` / a ``runpy``/``importlib`` of the
  ``scripts/<name>.py`` path.

A bare-stem substring match is **rejected** for two reasons proven in
the wild: (1) a docstring/comment mention ("Companion to
``scripts/extract_tradier.py``") is prose, not wiring — the docstring of
this very module says a script named only in a comment IS an orphan; and
(2) a shorter stem is a substring of a longer script name
(``extract_tradier`` ⊂ ``extract_tradier_full``), so a stem match
silently cross-counts. The exact path-token match kills both: a
``scripts/extract_tradier.py`` token never matches
``scripts/extract_tradier_full.py``.

For each ``scripts/*.py`` (excluding ``scripts/tests/`` and
``__init__.py``) this test asserts such a genuine reference exists in at
least one repo *reference file*
(``*.py``/``*.sh``/``*.yml``/``*.yaml``/``*.toml``/``*.plist``),
EXCLUDING ``data/``, ``.git``, ``.venv``, ``__pycache__``,
``*_archive``/``archive``, and ``.claude`` (the last because
``.claude/worktrees/`` holds ephemeral git-worktree mirrors of
``scripts/`` itself — counting them would make every script trivially
self-referential and render this gate structurally inert), the orphan
detector test file **itself** (it lists every script name in prose, so
counting it would self-pass every script), and a script's own
**self-mentions** (a script referencing only itself is still an orphan).

``_ALLOWLIST`` names deliberate standalone tools that are legitimately
invoked by hand / out-of-band and need no wrapper or registry. Adding a
script here is a *recorded decision*, not a silent bypass.

Deterministic: static file reads + a bounded ``rglob`` + bounded regex
only. It NEVER runs git/DB/network and never touches the live repo's
git.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
_DETECTOR_FILE = Path(__file__).resolve()

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
        # ---------------------------------------------------------------
        # Suspected-dead one-off scripts. Each is self-described as a
        # "one-shot"/hand-run tool with ZERO genuine code-level wiring
        # (no *.sh wrapper, no ops.py stage, no plist, no ci.yml, no
        # pyproject, no import, no test). The previous bare-substring
        # matcher false-PASSED every one of these by counting a prose
        # mention or a substring-of-a-longer-stem collision; the
        # path-token/import matcher correctly exposes them. Deletion is
        # Phase-5-class (code-mutating, out of this plan's scope), so
        # each is tracked HERE, NOT silently buried — the allowlist
        # entry + TODO + PR report are the honest record, mirroring how
        # ``ingest_tradier_csv`` is handled.
        # ---------------------------------------------------------------
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19 (one-off script, CLAUDE.md bans these). The P3b
        # sweep found 0 references. Deletion is Phase-5-class
        # (code-mutating, out of this plan's scope), so it is tracked
        # here, NOT silently buried: the allowlist entry + this comment
        # + the PR report are the honest record.
        "ingest_tradier_csv",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. One-shot backtest-universe daily-bar backfill;
        # canonical path is ``ops.py --stage daily_bars``. 0 genuine
        # references.
        "backfill_backtest_universe",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. Trade-log diff CLI; only a prose mention in
        # ``tpcore/backtest/equivalence.py``. 0 genuine references.
        "compare_baselines",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. Point-in-time P/B & D/E populator; only a prose
        # mention in a migration docstring. 0 genuine references.
        "compute_fundamental_ratios",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. One-shot corporate-actions all-active driver,
        # superseded by ``ops.py``; only prose mentions in ops.py /
        # run_daily_bars_all_active docstrings. 0 genuine references.
        "run_corporate_actions_all_active",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. One-shot daily-bars all-active driver, superseded
        # by ``ops.py``; only a prose mention in ops.py docstring. 0
        # genuine references.
        "run_daily_bars_all_active",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. End-to-end AAR-pipeline manual smoke against the
        # live DB; only a prose mention in audit_data_pipeline.py
        # docstring (not invoked by it). 0 genuine references.
        "test_aar_pipeline",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. End-to-end kill-switch manual verification against
        # the production pool; the ``test_kill_switch`` substring only
        # collides with ``test_kill_switch_blocks_all_trades`` in
        # tpcore/tests/test_risk_governor.py. 0 genuine references.
        "test_kill_switch",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. One-shot Tradier CSV extractor (50-name backtest
        # universe). Previously false-passed only because its name is a
        # substring of ``extract_tradier_full`` AND that file's
        # docstring says "Companion to scripts/extract_tradier.py"
        # (prose). 0 genuine references.
        "extract_tradier",
        # TODO(P5): migrate to ops.py stage or remove — flagged
        # 2026-05-19. Wide one-shot Tradier CSV extractor (full US
        # equity/ETF universe). Only prose mentions in
        # ingest_tradier_csv / extract_tradier docstrings. 0 genuine
        # references.
        "extract_tradier_full",
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


def _strip_py_prose(text: str) -> str:
    """Return *text* with Python **prose** (``#`` comments and
    docstrings) blanked, while KEEPING string literals used as code
    (call args, registry values, ``subprocess`` argv). A path-token
    match in the surviving text is then genuine wiring, never prose.

    Why keep argument strings: genuine wiring of a script is almost
    always a *string* — ``subprocess.run(["python",
    "scripts/x.py"])``, an ``ops.py`` stage registry value, a path in
    ``pyproject.toml``/``ci.yml``. Only **docstrings** (a string
    expression that is the entire logical line — module/class/func
    leading string or a bare string statement) and ``#`` comments are
    prose and must not count. Conservative on ambiguity toward
    treating a *bare-string-statement* line as prose, since real code
    rarely has a path token only inside a statement-position string
    literal that is never assigned/passed.
    """
    # Blank triple-quoted blocks (docstrings + multi-line strings) —
    # these are statement-position prose in practice for this repo's
    # scripts; a path token genuinely wiring a script is never inside a
    # triple-quoted block.
    text = re.sub(r'"""[\s\S]*?"""', " ", text)
    text = re.sub(r"'''[\s\S]*?'''", " ", text)
    out_lines: list[str] = []
    for line in text.splitlines():
        # Drop ``#`` comments (full-line and trailing). A ``#`` inside a
        # surviving string literal is rare for a path-token line and
        # erring toward stripping only ever HIDES a reference (fails
        # safe for an anti-orphan gate — never false-passes an orphan).
        no_comment = re.sub(r"#.*$", "", line)
        stripped = no_comment.lstrip()
        # A line whose entire content is a single-quoted string literal
        # is a bare-string statement / single-line docstring: prose.
        if re.fullmatch(
            r"""(?:r|b|rb|br|f)?(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*""",
            stripped,
        ):
            continue
        out_lines.append(no_comment)
    return "\n".join(out_lines)


def _path_token_re(name: str) -> re.Pattern[str]:
    """Exact ``scripts/<name>.py`` token, bounded so a shorter stem can
    never match a longer script's path (kills the
    ``extract_tradier`` ⊂ ``extract_tradier_full`` collision)."""
    token = re.escape(f"scripts/{name}.py")
    # No leading word/path char (so ``xscripts/...`` etc. is rejected)
    # and no trailing word char (so ``scripts/foo.py`` does not match
    # ``scripts/foo.pyc`` or ``scripts/foobar.py``-style adjacency).
    return re.compile(r"(?<![\w./-])" + token + r"(?![\w])")


def _import_re(name: str) -> re.Pattern[str]:
    """Genuine Python import of ``scripts.<name>`` (incl. submodule)."""
    n = re.escape(name)
    return re.compile(
        r"\b(?:"
        r"import\s+scripts\." + n + r"(?:\b|\.)"
        r"|from\s+scripts\." + n + r"(?:\.\w+)*\s+import\b"
        r")"
    )


def is_referenced(
    name: str,
    script_path: Path,
    ref_texts: list[tuple[Path, str, str, str]],
) -> Path | None:
    """Return the reference file genuinely wiring ``scripts/<name>.py``,
    or ``None``. Genuine = exact path token in non-prose text, OR a real
    ``scripts.<name>`` import. Self-mentions and the detector file are
    not references.
    """
    path_re = _path_token_re(name)
    import_re = _import_re(name)
    for ref_path, raw, code_only, suffix in ref_texts:
        if ref_path == script_path or ref_path == _DETECTOR_FILE:
            continue
        if suffix == ".py":
            # Path token must survive prose-stripping; OR a real import.
            if path_re.search(code_only) or import_re.search(code_only):
                return ref_path
        else:
            # Non-Python config/shell: an exact path token IS the wiring
            # (a ``.sh``/``.yml``/``.toml``/``.plist`` does not have
            # Python docstrings; comment-only matches are vanishingly
            # rare and the path-token boundary already rejects prose
            # substrings — the documented standalone tools are
            # allowlisted, not relied on via a comment).
            if path_re.search(raw):
                return ref_path
    return None


def _build_ref_texts() -> list[tuple[Path, str, str, str]]:
    """``(path, raw, code_only, suffix)`` for every reference file."""
    out: list[tuple[Path, str, str, str]] = []
    for path in _reference_files():
        raw = path.read_text(encoding="utf-8", errors="replace")
        suffix = path.suffix
        code_only = _strip_py_prose(raw) if suffix == ".py" else raw
        out.append((path, raw, code_only, suffix))
    return out


def test_no_orphan_scripts() -> None:
    """Every scripts/*.py is genuinely wired, or explicitly allowlisted.

    An unreferenced, non-allowlisted script is a re-accreted one-off —
    CLAUDE.md forbids these. "Referenced" means a genuine code-level
    reference (exact ``scripts/<name>.py`` path token in non-prose text,
    or a real ``scripts.<name>`` import) — a docstring/comment mention
    or a substring-of-a-longer-stem is NOT a reference. Failure lists
    each offender so the fix is obvious: wire it into a
    wrapper/stage/CI, or (with a recorded justification) add it to
    ``_ALLOWLIST``.
    """
    ref_texts = _build_ref_texts()

    orphans: list[str] = []
    for script in _candidate_scripts():
        name = script.stem
        if name in _ALLOWLIST:
            continue
        if is_referenced(name, script, ref_texts) is None:
            orphans.append(name)

    assert not orphans, (
        "Orphan scripts/*.py with ZERO genuine references (no ops.py "
        "stage, no *.sh wrapper, no daemon/launchd plist, no ci.yml, "
        "no pyproject.toml, no test/module import — a prose/docstring "
        "mention does NOT count). CLAUDE.md bans one-off scripts — "
        "wire each into the pipeline OR add to _ALLOWLIST with a "
        f"recorded justification: {sorted(orphans)}"
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


def test_prose_and_substring_mentions_are_not_references() -> None:
    """Regression guard for THIS bug: the matcher must reject (1) a
    path-token that appears ONLY inside a Python docstring/comment
    (prose), and (2) a bare-stem substring of a longer script's path
    token. Both previously false-PASSED real orphans.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        script = tmp / "myscript.py"
        script.write_text("print('hi')\n", encoding="utf-8")

        # (1) Prose-only: the path token appears solely in a docstring
        # and a ``#`` comment of an otherwise-unrelated module.
        prose_ref = tmp / "prose_ref.py"
        prose_ref.write_text(
            '"""Companion to scripts/myscript.py — see scripts/myscript.py."""\n'
            "# also scripts/myscript.py is mentioned here\n"
            "x = 1\n",
            encoding="utf-8",
        )
        prose_raw = prose_ref.read_text(encoding="utf-8")
        prose_texts = [
            (
                prose_ref,
                prose_raw,
                _strip_py_prose(prose_raw),
                ".py",
            )
        ]
        assert is_referenced("myscript", script, prose_texts) is None, (
            "prose-only docstring/comment mention must NOT count as a "
            "reference"
        )

        # The SAME token as real code (e.g. a subprocess invocation
        # string) MUST be accepted — proves we reject prose, not the
        # token itself.
        code_ref = tmp / "code_ref.py"
        code_ref.write_text(
            "import subprocess\n"
            'subprocess.run(["python", "scripts/myscript.py"])\n',
            encoding="utf-8",
        )
        code_raw = code_ref.read_text(encoding="utf-8")
        code_texts = [
            (code_ref, code_raw, _strip_py_prose(code_raw), ".py")
        ]
        assert is_referenced("myscript", script, code_texts) == code_ref, (
            "a genuine code-level path token MUST be accepted"
        )

        # (2) Substring collision: a longer script's path token must not
        # count as a reference to the shorter-stem script.
        short = tmp / "extract_tradier.py"
        short.write_text("print('short')\n", encoding="utf-8")
        long_ref = tmp / "wrapper.sh"
        long_ref.write_text(
            "python scripts/extract_tradier_full.py --resume\n",
            encoding="utf-8",
        )
        long_raw = long_ref.read_text(encoding="utf-8")
        coll_texts = [(long_ref, long_raw, long_raw, ".sh")]
        assert is_referenced("extract_tradier", short, coll_texts) is None, (
            "scripts/extract_tradier_full.py must NOT count as a "
            "reference to extract_tradier (substring collision)"
        )

        # And a script that mentions only itself is still an orphan.
        selfish = tmp / "selfish.py"
        selfish.write_text(
            "# scripts/selfish.py self-mention\n"
            'subprocess.run(["python", "scripts/selfish.py"])\n',
            encoding="utf-8",
        )
        self_raw = selfish.read_text(encoding="utf-8")
        self_texts = [
            (selfish, self_raw, _strip_py_prose(self_raw), ".py")
        ]
        assert is_referenced("selfish", selfish, self_texts) is None, (
            "a script referencing only itself is still an orphan"
        )
