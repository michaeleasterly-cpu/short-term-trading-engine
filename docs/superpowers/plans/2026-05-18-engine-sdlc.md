# Engine SDLC (SP4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the SP1 H-B6 carry-forward by shipping a single non-Python shadow-manifest generator + `--check` CI-divergence gate, fold the clockwork redundancy, close the reverse `roster ⊆ ENGINE_TABLES` leg, migrate the SP3 executor to the one renderer atomicity-preserving, and deliver code-accurate doc-closure for the now-shipped Engine SDLC.

**Architecture:** A pure `str → str` renderer in `scripts/gen_engine_manifest.py` owns sentinel-delimited regions inside four non-Python shadow files. Three callers: CI `--check` (read-only diff), the `--write` CLI (idempotent regen), and the SP3 executor (journals OLD bytes → writes renderer's NEW text → byte-identical rollback unchanged). The generator imports only `tpcore.engine_profile` + stdlib (tpcore∌engine preserved). Doc-closure is verified against shipped code by a lightweight `test_sdlc_docs_match_code.py` gate. SP4 is engine-lane only; the 8 data-lane files + the data-SDLC spec/checklist are READ-ONLY symmetry reference, untouched; the SP3 (a)/(b) carry-forwards are RECORDED in docs, never implemented.

**Tech Stack:** Python 3.11, pytest, pydantic v2 (already in `tpcore.engine_profile`), `tomllib` (stdlib), `ast` (stdlib), `argparse` (stdlib), `subprocess` (stdlib), `difflib` (stdlib). Venv: `/Users/michael/short-term-trading-engine/.venv/bin/python`. Worktree: `/Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab`, branch `worktree-engine-sp4`.

---

## Standing constraints (every task)

- **Worktree / branch:** all work in `/Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab`. Before EVERY commit, verify `git branch --show-current` prints exactly `worktree-engine-sp4` (abort the commit otherwise). Use `git switch` never `git checkout`. Tests must NEVER run real `git`/`gh` against the working repo — the SP4 scope-gate test (Tn) uses read-only `git diff --name-only` only, exactly mirroring the proven SP3 `scripts/tests/test_sp3_scope_confined.py` pattern.
- **Per-task CI gate (the standing CI-exact set — run from the worktree root):**
  - Full suite: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider`
  - `/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`
  - `/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore`
  - plus that task's named pinning tests (listed per task).
- **Lane discipline:** SP4 touches ONLY the files in the Tn allowlist. The 8 data-lane-owned files (`tpcore/calendar.py`, `tpcore/risk/`, `tpcore/risk/governor.py`, `ops/engine_supervisor.py`, `ops/engine_service.py`, `ops/engine_ladder.py`, `tpcore/supervisor_state.py`, `tpcore/trade_monitor.py`) and the data-SDLC spec/checklist/registry (`tpcore/providers.py`, `tpcore/feeds/`, `tpcore/selfheal/`, `docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md`, `docs/superpowers/checklists/data_feed_change_request.md`) are NEVER edited.
- **No SP3 carry-forward fix:** SP4 does NOT extend `_ENGINE_DEFAULT_CONSTS` and does NOT change `_validate_modify`'s `type(want)(v)` line. They are RECORDED in docs and pinned-unchanged by the H-S4-10(e) gate.

---

## File Structure

### New files (created by SP4)

| File | One responsibility |
|---|---|
| `scripts/gen_engine_manifest.py` | The single shadow-manifest tool: the pure `render_region`/`render_all` (`str → str`, no I/O, no journal), the matched-pair fence parser (`ManifestFenceError`), the pure in-process `divergences() -> str \| None`, and the `--write`(default)/`--check` argparse CLI. Imports `tpcore.engine_profile` + stdlib ONLY (no engine, no `ops`). |
| `scripts/tests/test_gen_engine_manifest_render.py` | T1 pinning tests: renderer purity, fence-parser fail-loud, no-engine-import. Carries the H-S4-9 collision-eviction stanza. |
| `scripts/tests/test_engine_manifest_in_sync.py` | T3 CI-divergence gate: invokes `gen_engine_manifest.py --check` as a subprocess, asserts rc 0 on the committed tree; in-fence-edit RED / out-of-fence-edit GREEN; the collision-preemption-stanza meta-assert. Carries the H-S4-9 stanza. |
| `scripts/tests/test_sdlc_docs_match_code.py` | T8 code-accuracy gate (H-S4-10 clauses a–e): entrypoints resolve, documented lifecycle states == `LifecycleState`, documented roster line == `roster_for_dispatch()`, CLAUDE.md FAIL-the-gate honesty substring, OPERATIONS.md re-role, SP3 carry-forwards provably unchanged. Carries the H-S4-9 stanza. |
| `scripts/tests/test_sp4_scope_confined.py` | Tn scope-confinement gate: read-only `git diff --name-only` against the SP4 base (skip-not-fail on no base ref), SP4's own allowlist + the data-lane/data-SDLC FORBIDDEN list. |

### Modified files (by SP4)

| File | What changes |
|---|---|
| `scripts/run_smoke_test.sh` | T2: wrap the step-3 `for engine in …; do` loop AND the line 7–8 docstring engine listing in `#`-comment sentinel fences. |
| `scripts/run_all_engines.sh` | T2: wrap the line 10 `# Engines dispatched: …` line in a `#`-comment sentinel fence. |
| `ops/platform_pipeline.py` | T2: wrap the docstring engine listing (lines 13–14, `reversion → vector → momentum → sentinel → canary`) in a `# >>> … >>>` sentinel-fenced span inside the module docstring. |
| `pyproject.toml` | T2: re-express the `packages.find.include` array multi-line; fence the engine `testpaths` rows + the engine `include` globs (`"tpcore*"` row + trailing comment stay outside the fence). |
| `tpcore/tests/test_engine_sdlc_planner.py` | T2 (DDF-1): re-express `_make_synthetic_engine_tree`'s `throwaway`-shadow injection via the T1 renderer instead of stale `str.replace()` literals. |
| `tpcore/tests/test_engine_lifecycle_consistency.py` | T4: fold leg 6 body → one-line delegation to the generator's in-process `divergences()`; add the reverse `set(roster_for_dispatch()) <= (set(ENGINE_TABLES) - {"allocator"})` leg to leg 5; delete the SP1 deferred comment (lines 112–113). |
| `ops/engine_sdlc/planner.py` | T5: `_shadow_edit_remove` + `_maybe_rewrite_frozen_literal` recompute new text via the T1 renderer instead of inline `re.sub`/`str.replace`; the journal+write+rollback ordering is byte-for-byte unchanged. |
| `CLAUDE.md` | T6: add the Engine SDLC Architecture/Conventions entry + Session Rules ECR/Lab commands + shortlist cross-ref + accuracy guard. |
| `docs/OPERATIONS.md` | T7: new "Engine SDLC" section + the Lab runbook + the `search_parameters.py` re-role (NOT delete). |
| `docs/superpowers/checklists/engine_readiness.md` | T8: header note tying it to the SDLC ADD-path build gate + bidirectional cross-links + per-item enforced/operator-verified accuracy. |
| `docs/glossary.md` | T8: add the 8 engine-domain terms; fix the stale "9 sections" engine-readiness line to "10 sections". |
| `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md` | T0 only (already filled — this commit). |
| `docs/superpowers/plans/2026-05-18-engine-sdlc.md` | T0 (this plan). |

---

## Canonical snippets reused verbatim across tasks

### The H-S4-9 collision-eviction stanza

Every SP4 test under `scripts/tests/` that imports `ops.*` or the generator copies this verbatim near the top (after `from pathlib import Path`), copied from the proven `scripts/tests/test_lab_cli_entrypoint.py:24-31`:

```python
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.*`` resolves the package —
# the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]
```

### The sentinel-fence vocabulary (the four shadow files, pinned per H-S4-6)

Region ids and their fence markers:

| Region id | File | Open marker | Close marker |
|---|---|---|---|
| `smoke-loop` | `scripts/run_smoke_test.sh` | `# >>> engine-manifest:smoke-loop (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>` | `# <<< engine-manifest:smoke-loop <<<` |
| `smoke-doc` | `scripts/run_smoke_test.sh` | `# >>> engine-manifest:smoke-doc (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>` | `# <<< engine-manifest:smoke-doc <<<` |
| `all-engines-doc` | `scripts/run_all_engines.sh` | `# >>> engine-manifest:all-engines-doc (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>` | `# <<< engine-manifest:all-engines-doc <<<` |
| `pipeline-doc` | `ops/platform_pipeline.py` | `# >>> engine-manifest:pipeline-doc (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>` | `# <<< engine-manifest:pipeline-doc <<<` |
| `pyproject-testpaths` | `pyproject.toml` | `# >>> engine-manifest:pyproject-testpaths (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>` | `# <<< engine-manifest:pyproject-testpaths <<<` |
| `pyproject-include` | `pyproject.toml` | `# >>> engine-manifest:pyproject-include (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>` | `# <<< engine-manifest:pyproject-include <<<` |

The renderer emits the SoT-derived body **between** (exclusive of) the marker lines. The join policy: space-joined for the bash `for engine in` loop, ` → ` for the dispatch-order prose, `, ` for the smoke listing, one `    "<e>/tests",` / `    "<e>*",` per row for pyproject. Trailing-newline policy: each region body ends with exactly one `\n` before the close marker line.

---

## Task T0 — Spec + expert-harden + this plan

**Files:**
- Modify: `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md` (already filled — §14 H-S4 register + T-decomposition + DDF-1, committed at `27396ae`)
- Create: `docs/superpowers/plans/2026-05-18-engine-sdlc.md` (this document)

- [ ] **Step 1: Confirm the spec is the source of truth**

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab log --oneline -1 -- docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`
Expected: shows the hardened spec commit (`27396ae` or its descendant on this branch). No spec edits in SP4 beyond T0.

- [ ] **Step 2: Save this plan and run the writing-plans Self-Review**

The Self-Review is recorded at the end of this document (spec-coverage / placeholder-scan / type-consistency). Fix any finding inline before committing.

- [ ] **Step 3: Verify branch, then commit**

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current`
Expected: `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add docs/superpowers/plans/2026-05-18-engine-sdlc.md
git commit -m "docs(engine-sdlc): SP4 implementation plan (T0–T8+Tn, no-placeholder TDD)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T1 — The pure renderer + generator skeleton

**Satisfies:** H-S4-1 (pure `str → str`, no I/O, no journal), H-S4-2 (matched-pair-or-raise fence parser), H-S4-4 (imports no engine).

**Files:**
- Create: `scripts/gen_engine_manifest.py`
- Create: `scripts/tests/test_gen_engine_manifest_render.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_gen_engine_manifest_render.py`:

```python
"""SP4 T1 — the pure renderer + fence parser (H-S4-1/2/4).

The renderer is str → str: no filesystem I/O, no journal, engine-free.
The fence parser is matched-pair-or-raise. The generator imports only
tpcore.engine_profile + stdlib.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.*`` resolves the package —
# the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

import scripts.gen_engine_manifest as gm  # noqa: E402

ROSTER = ("reversion", "vector", "momentum", "sentinel", "canary")
ARCHIVED = ("sigma",)

OPEN = ("# >>> engine-manifest:smoke-loop (generated by "
        "scripts/gen_engine_manifest.py — edit the SoT, not this) >>>")
CLOSE = "# <<< engine-manifest:smoke-loop <<<"


def test_renderer_is_pure_no_filesystem_io():
    """render_* perform no filesystem I/O — the source of every render
    helper contains no open(/.write_text/.write_bytes/os.write, and a
    call returns a pure transformed string (the original string is not
    mutated, nothing is written)."""
    src = gm.__file__
    body = Path(src).read_text()
    # Split: the ONLY intentional writer is the _cli_write function.
    forbidden = ("open(", ".write_text(", ".write_bytes(", "os.write(")
    # Locate the render functions' source via inspect — assert none of
    # them reference a write primitive.
    import inspect
    for fn in (gm.render_region, gm.render_all, gm.divergences):
        fsrc = inspect.getsource(fn)
        for tok in forbidden:
            assert tok not in fsrc, (
                f"{fn.__name__} contains forbidden fs primitive {tok!r} "
                f"— the renderer must be pure str→str (H-S4-1)")
    text = f"before\n{OPEN}\nstale\n{CLOSE}\nafter\n"
    out = gm.render_region(text, "smoke-loop", ROSTER, ARCHIVED)
    assert isinstance(out, str)
    assert text == f"before\n{OPEN}\nstale\n{CLOSE}\nafter\n", (
        "render_region mutated its input string — must be pure")
    assert "before\n" in out and "after\n" in out
    del body  # the module-source read is for the structural assert only


def test_unmatched_sentinel_raises():
    text = f"x\n{OPEN}\nbody\n"  # open present, close absent
    with pytest.raises(gm.ManifestFenceError):
        gm.render_region(text, "smoke-loop", ROSTER, ARCHIVED)


def test_duplicate_sentinel_raises():
    text = f"{OPEN}\na\n{CLOSE}\n{OPEN}\nb\n{CLOSE}\n"
    with pytest.raises(gm.ManifestFenceError):
        gm.render_region(text, "smoke-loop", ROSTER, ARCHIVED)


def test_missing_close_sentinel_raises():
    text = f"{OPEN}\nbody-no-close\n"
    with pytest.raises(gm.ManifestFenceError):
        gm.render_region(text, "smoke-loop", ROSTER, ARCHIVED)


def test_text_outside_fence_is_never_touched():
    pre = "LIVE-PRE-1\nLIVE-PRE-2\n"
    post = "LIVE-POST-1\nLIVE-POST-2\n"
    text = f"{pre}{OPEN}\nWILL-BE-REPLACED\n{CLOSE}\n{post}"
    out = gm.render_region(text, "smoke-loop", ROSTER, ARCHIVED)
    assert out.startswith(pre), "bytes before the fence were altered"
    assert out.endswith(post), "bytes after the fence were altered"
    assert "WILL-BE-REPLACED" not in out, "fence body not regenerated"


def test_generator_imports_no_engine():
    """H-S4-4: importing the generator in a fresh interpreter pulls in
    NO engine package and NO ops.* — pure SoT reads only."""
    code = (
        "import sys; import scripts.gen_engine_manifest;"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "{'reversion','vector','momentum','sentinel','canary','ops'}];"
        "print(';'.join(bad))"
    )
    res = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "", (
        f"generator eager-imported forbidden modules: {res.stdout!r}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_gen_engine_manifest_render.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.gen_engine_manifest'`

- [ ] **Step 3: Write the minimal implementation**

Create `scripts/gen_engine_manifest.py`:

```python
"""The engine non-Python shadow-manifest generator (SP4 §10).

Closes the SP1 H-B6 carry-forward: every non-Python shadow of the
roster SoT (the run_smoke_test.sh step-3 loop + its docstring, the
run_all_engines.sh dispatch-order line, the platform_pipeline.py
docstring, the pyproject testpaths/include engine rows) is regenerated
FROM tpcore.engine_profile and a silent drift is ungameable.

Three callers, ONE writer-per-context (H-S4-1):
  * CI ``--check``  — regenerate in memory, diff vs disk, READ-ONLY.
  * ``--write``     — idempotent in-place regen (operator/regen tool).
  * the SP3 executor (ops/engine_sdlc/planner.py, T5) — journals OLD
    bytes, then writes the renderer's returned NEW text; rollback
    restores OLD. The renderer NEVER writes / NEVER journals.

Imports tpcore.engine_profile + stdlib ONLY — no engine, no ops
(tpcore∌engine layering, H-S4-4).
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

from tpcore.engine_profile import archived_engines, roster_for_dispatch

REPO_ROOT = Path(__file__).resolve().parents[1]

_ENGINE_TOKEN = re.compile(r"^[a-z0-9_]+$")

_SENTINEL_NOTE = ("generated by scripts/gen_engine_manifest.py — "
                  "edit the SoT, not this")


class ManifestFenceError(RuntimeError):
    """A fenced region's sentinel pair is missing / unmatched /
    duplicated / crossed — fail LOUD, never silently no-op (H-S4-2)."""

    def __init__(self, file: str, region: str, reason: str) -> None:
        super().__init__(f"{file}: region {region!r}: {reason}")
        self.file = file
        self.region = region
        self.reason = reason


def _open_marker(region: str) -> str:
    return f"# >>> engine-manifest:{region} ({_SENTINEL_NOTE}) >>>"


def _close_marker(region: str) -> str:
    return f"# <<< engine-manifest:{region} <<<"


def _assert_roster_safe(roster: tuple[str, ...]) -> None:
    for tok in roster:
        if not _ENGINE_TOKEN.match(tok):
            raise ManifestFenceError(
                "<roster>", "<all>",
                f"roster token {tok!r} is not ^[a-z0-9_]+$ — refusing "
                f"to emit unsafe bash/TOML/docstring")


def _region_body(region: str, roster: tuple[str, ...],
                 archived: tuple[str, ...]) -> str:
    """The SoT-derived body emitted BETWEEN the sentinel markers
    (exclusive). Each body ends with exactly one trailing newline so
    the close marker sits on its own line."""
    _assert_roster_safe(roster)
    space = " ".join(roster)
    arrow = " → ".join(roster)
    comma = ", ".join(roster)
    if region == "smoke-loop":
        return f"for engine in {space}; do\n"
    if region == "smoke-doc":
        return (f"#   3. Per-engine scheduler dry-run (no orders "
                f"submitted): {comma}\n#      (Sigma archived "
                f"2026-05-16).\n")
    if region == "all-engines-doc":
        return f"# Engines dispatched: {arrow}.\n"
    if region == "pipeline-doc":
        return (f"    2. ``scripts/run_all_engines.sh`` — runs "
                f"{arrow} schedulers back-to-back (Sigma\n"
                f"       archived 2026-05-16). Each engine handles\n"
                f"       its own market-closed / no-rebalance / "
                f"no-candidates gating.\n")
    if region == "pyproject-testpaths":
        return "".join(f'    "{e}/tests",\n' for e in roster)
    if region == "pyproject-include":
        return "".join(f'    "{e}*",\n' for e in roster)
    raise ManifestFenceError("<unknown>", region, "unknown region id")


def render_region(file_text: str, region: str,
                  roster: tuple[str, ...],
                  archived: tuple[str, ...]) -> str:
    """Pure str → str. Replace ONLY the bytes strictly between the one
    matched ``>>> engine-manifest:<region> >>>`` /
    ``<<< engine-manifest:<region> <<<`` pair. Zero / >1 / crossed /
    missing-close ⇒ ManifestFenceError (LOUD). No filesystem I/O, no
    journal (H-S4-1/2)."""
    om = _open_marker(region)
    cm = _close_marker(region)
    n_open = file_text.count(om)
    n_close = file_text.count(cm)
    if n_open == 0 and n_close == 0:
        raise ManifestFenceError(
            "<text>", region, "no sentinel pair present")
    if n_open != 1 or n_close != 1:
        raise ManifestFenceError(
            "<text>", region,
            f"expected exactly one open and one close marker, "
            f"found open={n_open} close={n_close}")
    oi = file_text.index(om)
    ci = file_text.index(cm)
    if ci < oi:
        raise ManifestFenceError(
            "<text>", region, "close marker precedes open marker")
    # the body is everything after the open marker line through the
    # newline immediately before the close marker line.
    after_open = file_text.index("\n", oi) + 1
    before_close = file_text.rindex("\n", 0, ci) + 1
    if before_close < after_open:
        raise ManifestFenceError(
            "<text>", region, "crossed/empty fence span")
    body = _region_body(region, roster, archived)
    return file_text[:after_open] + body + file_text[before_close:]


# Which region ids live in which shadow file (relative to REPO_ROOT).
_FILE_REGIONS: dict[str, tuple[str, ...]] = {
    "scripts/run_smoke_test.sh": ("smoke-doc", "smoke-loop"),
    "scripts/run_all_engines.sh": ("all-engines-doc",),
    "ops/platform_pipeline.py": ("pipeline-doc",),
    "pyproject.toml": ("pyproject-testpaths", "pyproject-include"),
}


def render_all(file_text: str, file_rel: str,
               roster: tuple[str, ...],
               archived: tuple[str, ...]) -> str:
    """Pure str → str. Regenerate EVERY region declared for
    ``file_rel`` in ``file_text`` (idempotent fixed point — H-S4-3).
    No filesystem I/O."""
    out = file_text
    for region in _FILE_REGIONS.get(file_rel, ()):
        out = render_region(out, region, roster, archived)
    return out


def divergences(repo_root: Path | None = None) -> str | None:
    """Pure, in-process, READ-ONLY: regenerate every fenced region of
    every shadow file in memory and diff vs the bytes on disk. Returns
    a unified diff naming the drifted file/region, or None if every
    region is byte-identical. The T4 clockwork delegation + the T3
    --check share this (one mechanism)."""
    root = repo_root or REPO_ROOT
    roster = roster_for_dispatch()
    archived = archived_engines()
    chunks: list[str] = []
    for rel in _FILE_REGIONS:
        p = root / rel
        on_disk = p.read_text()
        regenerated = render_all(on_disk, rel, roster, archived)
        if regenerated != on_disk:
            chunks.append("".join(difflib.unified_diff(
                on_disk.splitlines(keepends=True),
                regenerated.splitlines(keepends=True),
                fromfile=f"{rel} (on disk)",
                tofile=f"{rel} (regenerated from SoT)")))
    return "".join(chunks) if chunks else None


def _cli_write(repo_root: Path) -> int:
    """The ONE intentional file writer (the operator/idempotent-regen
    tool — NOT the transaction path; the SP3 executor has its own
    journal+write). Rewrites every fenced region in place."""
    roster = roster_for_dispatch()
    archived = archived_engines()
    for rel in _FILE_REGIONS:
        p = repo_root / rel
        cur = p.read_text()
        new = render_all(cur, rel, roster, archived)
        if new != cur:
            p.write_text(new)
    return 0


def _cli_check(repo_root: Path) -> int:
    diff = divergences(repo_root)
    if diff is None:
        return 0
    sys.stderr.write(
        "engine shadow-manifest DRIFT — a roster/SoT change did not "
        "regenerate the shadows. Run `python scripts/gen_engine_"
        "manifest.py` and commit.\n")
    sys.stderr.write(diff)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gen_engine_manifest",
        description="Regenerate the non-Python engine shadows from the "
                    "tpcore.engine_profile SoT (SP4 §10).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="READ-ONLY: exit non-zero + unified diff on "
                        "any drift (the CI-divergence gate).")
    g.add_argument("--write", action="store_true",
                   help="(default) rewrite every fenced region in "
                        "place; idempotent.")
    ns = p.parse_args(argv)
    if ns.check:
        return _cli_check(REPO_ROOT)
    return _cli_write(REPO_ROOT)


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
```

- [ ] **Step 4: Run the T1 pinning tests to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_gen_engine_manifest_render.py -q -p no:cacheprovider`
Expected: PASS — 6 tests (`test_renderer_is_pure_no_filesystem_io`, `test_unmatched_sentinel_raises`, `test_duplicate_sentinel_raises`, `test_missing_close_sentinel_raises`, `test_text_outside_fence_is_never_touched`, `test_generator_imports_no_engine`).

- [ ] **Step 5: Run the standing CI gate**

Run:
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
```
Expected: full suite PASS (T1 adds no fences yet so the existing clockwork legs 6 / `test_retired_engine_absent_from_structural_shadows` are unaffected — they parse the un-fenced shipped shadows exactly as today), ruff clean, check_imports clean.

- [ ] **Step 6: Verify branch, then commit**

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add scripts/gen_engine_manifest.py scripts/tests/test_gen_engine_manifest_render.py
git commit -m "feat(engine-sdlc): SP4 T1 — pure shadow-manifest renderer + fence parser

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T2 — Sentinel-fence the four shadows + migrate the SP3 synthetic-tree builder (DDF-1)

**Satisfies:** H-S4-3 (idempotent round-trip byte-identical), H-S4-6 (valid bash/TOML/docstring post-fence), DDF-1 (the cross-task SP3-atomicity break caught INSIDE T2).

**Files:**
- Modify: `scripts/run_smoke_test.sh`
- Modify: `scripts/run_all_engines.sh`
- Modify: `ops/platform_pipeline.py`
- Modify: `pyproject.toml`
- Modify: `tpcore/tests/test_engine_sdlc_planner.py`
- Modify (extend): `scripts/tests/test_gen_engine_manifest_render.py`

- [ ] **Step 1: Write the failing tests (append to `scripts/tests/test_gen_engine_manifest_render.py`)**

Append:

```python
import tomllib  # noqa: E402
import ast  # noqa: E402

_SHADOWS = (
    "scripts/run_smoke_test.sh",
    "scripts/run_all_engines.sh",
    "ops/platform_pipeline.py",
    "pyproject.toml",
)


@pytest.mark.parametrize("rel", _SHADOWS)
def test_generator_is_idempotent(rel):
    """H-S4-3: render_all is a fixed point — render_all(render_all(x))
    == render_all(x) for every shadow file as committed."""
    text = (REPO_ROOT / rel).read_text()
    once = gm.render_all(text, rel, ROSTER, ARCHIVED)
    twice = gm.render_all(once, rel, ROSTER, ARCHIVED)
    assert twice == once, f"{rel}: render_all is not idempotent"


@pytest.mark.parametrize("rel", _SHADOWS)
def test_check_clean_after_write(rel):
    """H-S4-3: the committed shadow is already byte-identical to its
    own regeneration (a clean checkout never needs a regen ritual)."""
    text = (REPO_ROOT / rel).read_text()
    assert gm.render_all(text, rel, ROSTER, ARCHIVED) == text, (
        f"{rel}: committed bytes != regeneration — run the generator")


def test_smoke_sh_still_parses():
    res = subprocess.run(  # noqa: S603
        ["bash", "-n", "scripts/run_smoke_test.sh"],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert res.returncode == 0, (
        f"run_smoke_test.sh failed bash -n post-fence:\n{res.stderr}")


def test_run_all_engines_sh_still_parses():
    res = subprocess.run(  # noqa: S603
        ["bash", "-n", "scripts/run_all_engines.sh"],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert res.returncode == 0, (
        f"run_all_engines.sh failed bash -n post-fence:\n{res.stderr}")


def test_pyproject_still_valid_toml():
    pp = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    testpaths = set(pp["tool"]["pytest"]["ini_options"]["testpaths"])
    includes = set(pp["tool"]["setuptools"]["packages"]["find"]["include"])
    for e in ROSTER:
        assert f"{e}/tests" in testpaths, f"{e}/tests missing"
        assert f"{e}*" in includes, f"{e}* missing"
    # the hand-owned non-engine rows survive outside the fence:
    assert "tpcore/tests" in testpaths
    assert "tpcore*" in includes


def test_platform_pipeline_docstring_still_valid():
    src = (REPO_ROOT / "ops" / "platform_pipeline.py").read_text()
    mod = ast.parse(src)
    doc = ast.get_docstring(mod)
    assert doc is not None, "platform_pipeline lost its module docstring"
    assert " → ".join(ROSTER) in doc, (
        "the regenerated dispatch-order line is absent from the docstring")
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_gen_engine_manifest_render.py::test_check_clean_after_write -q -p no:cacheprovider`
Expected: FAIL — the committed shadows carry no sentinel fences yet, so `render_all` raises `ManifestFenceError` ("no sentinel pair present").

- [ ] **Step 3: Fence `scripts/run_smoke_test.sh`**

Replace lines 4–9 (the docstring lines `#` through the step-6 listing) — specifically replace the block:

```
#   1. Full pytest suite — every engine, tpcore, dashboard, forensics.
#   2. ruff — lint clean.
#   3. Per-engine scheduler dry-run (no orders submitted): reversion,
#      vector, momentum, sentinel, canary (Sigma archived 2026-05-16).
#   4. Forensics CLI — scan AAR table for triggers (no-op when empty).
```

with:

```
#   1. Full pytest suite — every engine, tpcore, dashboard, forensics.
#   2. ruff — lint clean.
# >>> engine-manifest:smoke-doc (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>
#   3. Per-engine scheduler dry-run (no orders submitted): reversion, vector, momentum, sentinel, canary
#      (Sigma archived 2026-05-16).
# <<< engine-manifest:smoke-doc <<<
#   4. Forensics CLI — scan AAR table for triggers (no-op when empty).
```

And replace the step-3 loop line:

```
for engine in reversion vector momentum sentinel canary; do
```

with:

```
# >>> engine-manifest:smoke-loop (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>
for engine in reversion vector momentum sentinel canary; do
# <<< engine-manifest:smoke-loop <<<
```

- [ ] **Step 4: Fence `scripts/run_all_engines.sh`**

Replace line 10:

```
# Engines dispatched: reversion → vector → momentum → sentinel → canary.
```

with:

```
# >>> engine-manifest:all-engines-doc (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>
# Engines dispatched: reversion → vector → momentum → sentinel → canary.
# <<< engine-manifest:all-engines-doc <<<
```

- [ ] **Step 5: Fence the `ops/platform_pipeline.py` docstring**

Replace docstring lines 13–16:

```
    2. ``scripts/run_all_engines.sh`` — runs reversion → vector →
       momentum → sentinel → canary schedulers back-to-back (Sigma
       archived 2026-05-16). Each engine handles
       its own market-closed / no-rebalance / no-candidates gating.
```

with:

```
# >>> engine-manifest:pipeline-doc (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>
    2. ``scripts/run_all_engines.sh`` — runs reversion → vector → momentum → sentinel → canary schedulers back-to-back (Sigma
       archived 2026-05-16). Each engine handles
       its own market-closed / no-rebalance / no-candidates gating.
# <<< engine-manifest:pipeline-doc <<<
```

(The `#`-comment sentinel lines sit inside the triple-quoted module docstring; they are plain text there and `ast.get_docstring` still returns a non-None docstring containing `reversion → vector → momentum → sentinel → canary` — verified by `test_platform_pipeline_docstring_still_valid`.)

- [ ] **Step 6: Re-express + fence `pyproject.toml`**

Replace the single-line include (line 59):

```
include = ["tpcore*", "reversion*", "vector*", "momentum*", "sentinel*", "canary*"]  # sigma archived 2026-05-16
```

with the multi-line array (the hand-owned `"tpcore*"` and the trailing comment stay OUTSIDE the fence; multi-line TOML arrays are legal and `tomllib`-parseable, H-S4-6):

```
include = [
    "tpcore*",  # sigma archived 2026-05-16
# >>> engine-manifest:pyproject-include (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>
    "reversion*",
    "vector*",
    "momentum*",
    "sentinel*",
    "canary*",
# <<< engine-manifest:pyproject-include <<<
]
```

Replace the `testpaths` engine rows (lines 88–92, the five `"<e>/tests",` rows) — replace:

```
    "reversion/tests",
    "vector/tests",
    "momentum/tests",
    "sentinel/tests",
    "canary/tests",
    "scripts/tests",
```

with (the non-engine `"scripts/tests"` row stays outside the fence):

```
# >>> engine-manifest:pyproject-testpaths (generated by scripts/gen_engine_manifest.py — edit the SoT, not this) >>>
    "reversion/tests",
    "vector/tests",
    "momentum/tests",
    "sentinel/tests",
    "canary/tests",
# <<< engine-manifest:pyproject-testpaths <<<
    "scripts/tests",
```

- [ ] **Step 7: DDF-1 — migrate `_make_synthetic_engine_tree` to the renderer**

In `tpcore/tests/test_engine_sdlc_planner.py`, the three `str.replace()` calls on the now-fenced shadow forms (the smoke loop at lines 240–243 and the pyproject include/testpaths at lines 245–248) become silent no-ops once T2 fences them. Replace the body of `_make_synthetic_engine_tree` after the `ep.write_text(t)` line (the `# add a PAPER _PROFILE entry` block stays — `engine_profile.py` is NOT fenced) so the shadow tokens are produced by the T1 renderer against a `throwaway`-augmented roster.

Replace this block (current lines 239–249):

```python
    smoke = staged / "scripts" / "run_smoke_test.sh"
    smoke.write_text(smoke.read_text().replace(
        "for engine in reversion vector momentum sentinel canary; do",
        "for engine in reversion vector momentum sentinel canary "
        "throwaway; do"))
    pp = staged / "pyproject.toml"
    pj = pp.read_text().replace(
        '"canary*"]  # sigma archived 2026-05-16',
        '"canary*", "throwaway*"]  # sigma archived 2026-05-16').replace(
        '    "canary/tests",', '    "canary/tests",\n    "throwaway/tests",')
    pp.write_text(pj)
```

with (DDF-1 fix — the synthetic shadows are a renderer call against the `throwaway`-augmented roster snapshot, so the fenced form stays correct regardless of fence wording):

```python
    # DDF-1 (SP4 T2): the shadows are sentinel-fenced; the old
    # str.replace on the un-fenced literal is now a silent no-op. Build
    # the synthetic `throwaway`-bearing shadows by the ONE renderer
    # against a throwaway-augmented roster, so the staged tree is green
    # pre-REMOVE no matter the fence wording.
    import sys as _sys
    _repo = Path(__file__).resolve().parents[2]
    _sys.path.insert(0, str(_repo))
    from scripts.gen_engine_manifest import render_all
    _aug_roster = ("reversion", "vector", "momentum", "sentinel",
                   "canary", "throwaway")
    _aug_archived = ("sigma",)
    for _rel in ("scripts/run_smoke_test.sh", "pyproject.toml"):
        _p = staged / _rel
        _p.write_text(render_all(_p.read_text(), _rel,
                                 _aug_roster, _aug_archived))
```

- [ ] **Step 8: Run the DDF-1 cross-task gate (the make-or-break check inside T2)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_sdlc_planner.py -q -p no:cacheprovider`
Expected: PASS — the entire SP3 atomicity suite (incl. `test_apply_red_consistency_rolls_back_to_byte_identical`, `test_apply_mid_move_loop_failure_byte_identical`, `test_remove_throwaway_engine_end_to_end`, `test_remove_rostered_engine_updates_frozen_literal`, the ADD rollback tests) is GREEN. (DDF-1: the cross-task break a per-T2-shadow reviewer could never see is caught HERE, inside T2, by re-running the SP3 suite.)

- [ ] **Step 9: Run the T2 pinning tests + the still-passing leg-6**

Run:
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_gen_engine_manifest_render.py -q -p no:cacheprovider
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest "tpcore/tests/test_engine_lifecycle_consistency.py::test_structurally_parseable_shadows_match_sot" "tpcore/tests/test_engine_lifecycle_consistency.py::test_retired_engine_absent_from_structural_shadows" -q -p no:cacheprovider
```
Expected: T2 pinning tests PASS (`test_generator_is_idempotent`, `test_check_clean_after_write`, `test_smoke_sh_still_parses`, `test_run_all_engines_sh_still_parses`, `test_pyproject_still_valid_toml`, `test_platform_pipeline_docstring_still_valid`). Leg 6 + `test_retired_engine_absent_from_structural_shadows` still PASS — their regexes (`for engine in ([^\n;]+);\s*do` and the tomllib `testpaths`/`include` reads) match the fenced-but-equivalent content unchanged (leg 6 is folded in T4, not T2; T2 keeps it green).

- [ ] **Step 10: Run the standing CI gate, verify branch, commit**

Run the standing CI gate (full suite + ruff + check_imports). Expected: all green.

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add scripts/run_smoke_test.sh scripts/run_all_engines.sh ops/platform_pipeline.py pyproject.toml tpcore/tests/test_engine_sdlc_planner.py scripts/tests/test_gen_engine_manifest_render.py
git commit -m "feat(engine-sdlc): SP4 T2 — sentinel-fence the four shadows + DDF-1 synthetic-tree migration

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T3 — The `--check` CI-divergence gate + the in-sync test

**Satisfies:** H-S4-5 (in-fence edit RED / out-of-fence edit GREEN), H-S4-9 (collision-preemption stanza present + meta-asserted).

**Files:**
- Create: `scripts/tests/test_engine_manifest_in_sync.py`
- (No `scripts/gen_engine_manifest.py` change — `divergences()` + `--check` already shipped in T1; T3 only adds the CI test that exercises them as a subprocess.)

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_engine_manifest_in_sync.py`:

```python
"""SP4 T3 — the --check CI-divergence gate (H-S4-5/9).

Invokes scripts/gen_engine_manifest.py --check as a SUBPROCESS (the
faithful CI shape; a fresh interpreter side-steps the scripts/ops.py↔ops
sys.modules collision entirely). An in-fence hand-edit fails --check;
an out-of-fence hand-edit passes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.*`` resolves the package —
# the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

GEN = "scripts/gen_engine_manifest.py"


def _check(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, GEN, "--check"],
        cwd=str(cwd), capture_output=True, text=True)


def test_check_clean_on_committed_tree():
    res = _check(REPO_ROOT)
    assert res.returncode == 0, (
        f"--check RED on the committed tree (the shadows must already "
        f"be in sync):\n{res.stdout}\n{res.stderr}")


def _staged_copy(tmp_path: Path) -> Path:
    staged = tmp_path / "tree"
    shutil.copytree(
        REPO_ROOT, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    return staged


def test_hand_edit_in_fence_fails_check(tmp_path):
    staged = _staged_copy(tmp_path)
    smoke = staged / "scripts" / "run_smoke_test.sh"
    txt = smoke.read_text()
    smoke.write_text(txt.replace(
        "for engine in reversion vector momentum sentinel canary; do",
        "for engine in reversion vector momentum; do"))  # in-fence drift
    res = _check(staged)
    assert res.returncode != 0, "an in-fence hand-edit must fail --check"
    assert "run_smoke_test.sh" in (res.stdout + res.stderr), (
        "the unified diff must name the drifted file")


def test_hand_edit_out_of_fence_passes_check(tmp_path):
    staged = _staged_copy(tmp_path)
    smoke = staged / "scripts" / "run_smoke_test.sh"
    txt = smoke.read_text()
    # mutate a line that is NOT inside any fenced region (the shebang
    # comment block at the very top).
    smoke.write_text(txt.replace(
        "# Platform-wide smoke test — covers every engine + shared services.",
        "# Platform-wide smoke test — covers every engine + services (edited)."))
    res = _check(staged)
    assert res.returncode == 0, (
        f"an out-of-fence edit must NOT fail --check:\n{res.stdout}\n"
        f"{res.stderr}")


def test_collision_preemption_stanza_present():
    """H-S4-9: every SP4 scripts/tests file that imports ops/the
    generator carries the proven sys.modules eviction loop verbatim."""
    needle = ('for _m in [m for m in list(sys.modules) if m == "ops" '
              'or m.startswith("ops.")]:')
    for fn in ("test_engine_manifest_in_sync.py",
               "test_gen_engine_manifest_render.py",
               "test_sdlc_docs_match_code.py",
               "test_sp4_scope_confined.py"):
        p = REPO_ROOT / "scripts" / "tests" / fn
        if not p.is_file():
            continue  # lands in its own task; asserted there too
        assert needle in p.read_text(), (
            f"{fn}: missing the H-S4-9 collision-eviction stanza")
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_manifest_in_sync.py -q -p no:cacheprovider`
Expected: FAIL on first collection/run only if the generator's `--check` had a bug; since T1/T2 shipped `--check` + fenced shadows, the EXPECTED first failure is NONE for `test_check_clean_on_committed_tree` — instead this test file is net-new, so run it to confirm it PASSES against the already-shipped `--check`. If `test_hand_edit_in_fence_fails_check` fails, the generator's region-replace is wrong (fix in `scripts/gen_engine_manifest.py`). (TDD note: the behavior under test — `--check` — was implemented in T1; T3's red-first is the absence of the CI test itself. Confirm the test exists and the gate is real by Step 3.)

- [ ] **Step 3: Run the T3 pinning tests to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_manifest_in_sync.py -q -p no:cacheprovider`
Expected: PASS — `test_check_clean_on_committed_tree`, `test_hand_edit_in_fence_fails_check`, `test_hand_edit_out_of_fence_passes_check`, `test_collision_preemption_stanza_present`.

- [ ] **Step 4: Standing CI gate, verify branch, commit**

Run the standing CI gate. Expected: all green.

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add scripts/tests/test_engine_manifest_in_sync.py
git commit -m "test(engine-sdlc): SP4 T3 — --check CI-divergence gate + in-sync test

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T4 — Fold clockwork leg 6 + close the reverse `roster ⊆ ENGINE_TABLES` leg

**Satisfies:** H-S4-7 (delegation in-process pure, no oracle gap, structure legs intact, no `ops` import), H-S4-8 (the exact grounded reverse predicate, deferred comment removed).

**Files:**
- Modify: `tpcore/tests/test_engine_lifecycle_consistency.py`

- [ ] **Step 1: Write the failing tests (append to `tpcore/tests/test_engine_lifecycle_consistency.py`)**

Append:

```python
def test_leg6_green_on_clean_tree():
    """The folded leg 6 (manifest-delegation) passes on the committed
    repo (the shadows are in sync — same diagnostic surface as the old
    parsed assertion, one mechanism)."""
    import sys
    sys.path.insert(0, str(REPO))
    from scripts.gen_engine_manifest import divergences
    assert divergences(REPO) is None


def test_leg6_fails_on_roster_drift(tmp_path):
    """H-S4-7: a drifted shadow in a staged tree makes the delegated
    in-process divergences() return a diff naming the file/region."""
    import shutil
    import sys
    staged = tmp_path / "tree"
    shutil.copytree(
        REPO, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    smoke = staged / "scripts" / "run_smoke_test.sh"
    smoke.write_text(smoke.read_text().replace(
        "for engine in reversion vector momentum sentinel canary; do",
        "for engine in reversion vector; do"))
    sys.path.insert(0, str(REPO))
    from scripts.gen_engine_manifest import divergences
    diff = divergences(staged)
    assert diff is not None, "drift not detected by the folded leg 6"
    assert "run_smoke_test.sh" in diff


def test_clockwork_imports_no_ops():
    """H-S4-7: the folded delegation imports the generator's PURE
    in-process divergences() (tpcore.engine_profile + stdlib only) —
    importing this clockwork module pulls in NO ops.* (no subprocess-
    in-subprocess + scripts/ops.py collision surface)."""
    import sys
    bad = [m for m in sys.modules
           if m == "ops" or m.startswith("ops.")]
    bad = [m for m in bad if not hasattr(sys.modules[m], "__path__")]
    assert bad == [], (
        f"the clockwork pulled a non-package ops into sys.modules: {bad}")


def test_live_engine_has_engine_tables_row():
    """H-S4-8: the closed reverse leg — every live PAPER/LIVE roster
    engine MUST have an ENGINE_TABLES data-dep row (a live engine with
    no row is a silent un-gated half-state). Grounded predicate (the
    shipped ENGINE_TABLES keys are exactly {reversion, vector,
    momentum, sentinel, allocator, canary}; allocator is excluded from
    the roster but legitimately keyed via its own _dispatch_allocator
    path, so it is subtracted on the reverse side)."""
    missing = set(roster_for_dispatch()) - (set(ENGINE_TABLES) - {"allocator"})
    assert not missing, (
        f"live roster engines with NO ENGINE_TABLES data-dep row "
        f"(silent un-gated engines): {missing}")


def test_reverse_engine_tables_leg_catches_a_missing_row(tmp_path):
    """H-S4-8: a synthetic roster with an engine absent from
    ENGINE_TABLES trips the reverse predicate (proves the leg is a
    real detector, not a tautology)."""
    synthetic_roster = set(roster_for_dispatch()) | {"phantomengine"}
    missing = synthetic_roster - (set(ENGINE_TABLES) - {"allocator"})
    assert missing == {"phantomengine"}, (
        "the reverse predicate must flag a roster engine that has no "
        "ENGINE_TABLES row")
```

- [ ] **Step 2: Run to verify the new reverse-leg + delegation tests fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest "tpcore/tests/test_engine_lifecycle_consistency.py::test_leg6_green_on_clean_tree" "tpcore/tests/test_engine_lifecycle_consistency.py::test_live_engine_has_engine_tables_row" -q -p no:cacheprovider`
Expected: these new tests reference `divergences` (already shipped T1) and `ENGINE_TABLES` (already imported at the top of this clockwork file) — they PASS immediately as written. The TDD red-first for T4 is the leg-6 FOLD itself (Step 3): before folding, leg 6's body is the old parsed assertion. Confirm the fold is real by Step 5 (the leg-6 body must be a single delegation line, asserted by inspection in Step 3).

- [ ] **Step 3: Fold leg 6's body to a one-line delegation**

In `tpcore/tests/test_engine_lifecycle_consistency.py`, replace the ENTIRE body of `test_structurally_parseable_shadows_match_sot` (current lines 116–132) with the in-process delegation (NOT a subprocess — the clockwork is itself run as a subprocess by the SP3 executor; a subprocess-in-subprocess + the `ops`/`scripts` collision is the SP2-T9/SP3 hazard, H-S4-7):

```python
def test_structurally_parseable_shadows_match_sot():
    """Folded SP4 §10.5: leg 6 is no longer an independent parsed-roster
    assertion (that would be a SECOND shadow mechanism that can disagree
    with the generator's byte-identity verdict). It delegates to the
    generator's PURE in-process regenerate-and-diff (one mechanism). The
    clockwork stays the structure/lifecycle oracle; the generator is the
    sole shadow-shape/bytes oracle; zero overlap."""
    import sys
    sys.path.insert(0, str(REPO))
    from scripts.gen_engine_manifest import divergences
    diff = divergences(REPO)
    assert diff is None, (
        f"engine shadow-manifest DRIFT — a roster/SoT change did not "
        f"regenerate the non-Python shadows. Run "
        f"`python scripts/gen_engine_manifest.py` and commit:\n{diff}")
```

- [ ] **Step 4: Close the reverse `ENGINE_TABLES` leg + delete the SP1 deferred comment**

In `test_engine_tables_keys_are_known_engines` (current lines 105–113), replace the trailing deferred-comment lines:

```python
    # SP4 will also assert the reverse (roster_for_dispatch() ⊆ ENGINE_TABLES) — a live engine
    # with no data-dep entry. Deferred per spec H-B6/§4 (ENGINE_TABLES is a documented seam in SP1).
```

with the closed reverse assertion (H-S4-8 — grounded against the shipped `ENGINE_TABLES` keys `{reversion, vector, momentum, sentinel, allocator, canary}`; `allocator` is on the existing forward `allowed` set, subtracted on the reverse side):

```python
    # SP4 §10.4 / H-S4-8: the closed reverse — every live PAPER/LIVE
    # roster engine MUST have an ENGINE_TABLES data-dep row. Grounded
    # against the shipped ENGINE_TABLES keys ({reversion, vector,
    # momentum, sentinel, allocator, canary}); allocator is excluded
    # from the roster (its own _dispatch_allocator path) so it is
    # subtracted here. A live engine with no row is a silent un-gated
    # half-state (the _required_sources fail-safe would mask it).
    missing = set(roster_for_dispatch()) - (set(ENGINE_TABLES) - {"allocator"})
    assert not missing, (
        f"live roster engines with NO ENGINE_TABLES data-dep row "
        f"(silent un-gated engines): {missing}")
```

- [ ] **Step 5: Run the full clockwork (legs 1–11 + the new T4 tests) GREEN**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_lifecycle_consistency.py -q -p no:cacheprovider`
Expected: PASS — every leg (1–5 incl. the closed reverse, the folded leg 6 delegation, 7–11) plus `test_leg6_green_on_clean_tree`, `test_leg6_fails_on_roster_drift`, `test_clockwork_imports_no_ops`, `test_live_engine_has_engine_tables_row`, `test_reverse_engine_tables_leg_catches_a_missing_row`. Confirm by inspection that `test_structurally_parseable_shadows_match_sot`'s body is now the single `divergences` delegation and the SP1 deferred-comment lines are gone.

- [ ] **Step 6: Standing CI gate, verify branch, commit**

Run the standing CI gate. Expected: all green (note: the SP3 dry-run/atomicity suite runs `test_engine_lifecycle_consistency.py` as a subprocess inside `_make_synthetic_engine_tree` trees; the T2 DDF-1 fix already made those trees carry fenced shadows, so the folded leg-6 delegation finds them in sync — `tpcore/tests/test_engine_sdlc_planner.py` stays green).

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add tpcore/tests/test_engine_lifecycle_consistency.py
git commit -m "refactor(engine-sdlc): SP4 T4 — fold leg 6 to one generator mechanism + close reverse ENGINE_TABLES leg

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T5 — Migrate the SP3 executor's inline shadow edits to the ONE renderer (atomicity-preserving)

**Satisfies:** H-S4-1 (renderer pure, executor journal+write+rollback ordering byte-for-byte unchanged — NO new write path).

**Files:**
- Modify: `ops/engine_sdlc/planner.py`
- Modify (extend): `tpcore/tests/test_engine_sdlc_planner.py`

**T5 atomicity-preservation invariant (do not deviate):** the renderer is pure `str → str`. The executor seam is unchanged in shape — `_apply_*` still calls `jn.record_file(p)` BEFORE any write, then writes the renderer's *returned* text, exactly as today. `apply()`'s post-stage clockwork subprocess + reverse-order `jn.restore()` to byte-identical is untouched. The renderer replaces ONLY the `re.sub`/`str.replace` *computation* inside `_shadow_edit_remove` / `_maybe_rewrite_frozen_literal` — never the journal+write+rollback ordering. There are exactly three callers and one writer-of-files-per-context: (a) CI `--check` (reads), (b) the `--write` CLI (writes, no journal), (c) the SP3 executor (journals OLD → writes renderer's NEW → rollback restores OLD). NO fourth write path.

- [ ] **Step 1: Write the failing tests (append to `tpcore/tests/test_engine_sdlc_planner.py`)**

Append:

```python
def test_planner_shadow_edit_uses_renderer_not_inline_regex():
    """H-S4-1: _shadow_edit_remove computes the new shadow text via the
    ONE renderer (scripts.gen_engine_manifest.render_all), NOT an inline
    re.sub/str.replace — one mechanism that knows a shadow's shape."""
    import inspect
    from ops.engine_sdlc import planner
    src = inspect.getsource(planner._shadow_edit_remove)
    assert "render_all" in src or "render_region" in src, (
        "_shadow_edit_remove must compute new text via the renderer")
    assert "re.search(r\"(for engine in )" not in src, (
        "the inline for-engine-in regex must be gone (one mechanism)")


def test_renderer_never_called_with_a_path():
    """H-S4-1: the renderer signature is str → str; it has no Path/open
    in its body (a guard so a future refactor can't make it a writer)."""
    import inspect
    from scripts.gen_engine_manifest import render_all, render_region
    for fn in (render_all, render_region):
        body = inspect.getsource(fn)
        assert "open(" not in body and ".write_text" not in body, (
            f"{fn.__name__} must never touch the filesystem")


def test_renderer_is_pure_no_filesystem_io_in_planner_path():
    """The SP3 executor still journals OLD bytes BEFORE writing — the
    record_file-before-write ordering in _shadow_edit_remove is intact
    (the renderer only supplied the new string)."""
    import inspect
    from ops.engine_sdlc import planner
    src = inspect.getsource(planner._shadow_edit_remove)
    # record_file MUST appear before write_text for each shadow file.
    rf = src.index("jn.record_file")
    wt = src.index(".write_text")
    assert rf < wt, (
        "record_file must precede write_text — the journal-before-"
        "mutate atomicity contract (H-S4-1)")
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_sdlc_planner.py::test_planner_shadow_edit_uses_renderer_not_inline_regex -q -p no:cacheprovider`
Expected: FAIL — `_shadow_edit_remove` currently uses the inline `re.search(r"(for engine in )...")`, not the renderer.

- [ ] **Step 3: Migrate `_shadow_edit_remove` to the renderer**

In `ops/engine_sdlc/planner.py`, replace the body of `_shadow_edit_remove` (current lines 326–342). The semantics: REMOVE drops `engine` from the roster, so the renderer is called with the post-removal roster (`roster_for_dispatch()` from the staged tree's SoT — but `_apply_remove` flips the SoT to RETIRED only at the very end via `ep.write_text(new_src)`; the shadow purge runs BEFORE that flip, so compute the post-removal roster explicitly by filtering the engine out of the staged roster). Replace:

```python
def _shadow_edit_remove(staged: Path, engine: str, jn: _Journal) -> None:
    """Purge the engine from the two structurally-parseable shadows
    (the ONLY non-SoT-derived sites — spec §4.2 fs_op 4)."""
    smoke = staged / "scripts" / "run_smoke_test.sh"
    jn.record_file(smoke)
    s = smoke.read_text()
    m = re.search(r"(for engine in )([^\n;]+)(;\s*do)", s)
    if m:
        toks = [t for t in m.group(2).split() if t != engine]
        smoke.write_text(s.replace(
            m.group(0), f"{m.group(1)}{' '.join(toks)}{m.group(3)}"))
    pp = staged / "pyproject.toml"
    jn.record_file(pp)
    txt = pp.read_text()
    txt = txt.replace(f'"{engine}*", ', "").replace(f', "{engine}*"', "")
    txt = re.sub(rf'\n\s*"{engine}/tests",', "", txt)
    pp.write_text(txt)
```

with (the ONE renderer computes the new text; the journal+write ordering is byte-for-byte unchanged — `record_file` BEFORE `write_text`, no Path ever passed to the renderer):

```python
def _shadow_edit_remove(staged: Path, engine: str, jn: _Journal) -> None:
    """Purge the engine from the non-Python shadows. SP4 T5: the new
    shadow text is computed by the ONE renderer
    (scripts.gen_engine_manifest.render_all) — there is exactly one
    mechanism that knows how a shadow is shaped. The journal+write
    ordering is UNCHANGED (record_file BEFORE write_text); the renderer
    is pure str→str and is NEVER given a path / NEVER writes (H-S4-1)."""
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from scripts.gen_engine_manifest import render_all
    from tpcore.engine_profile import archived_engines, roster_for_dispatch
    # REMOVE drops `engine` from the roster; the SoT flip to RETIRED
    # happens later in _apply_remove (ep.write_text), so derive the
    # post-removal roster by filtering it out of the current roster.
    post_roster = tuple(e for e in roster_for_dispatch() if e != engine)
    archived = archived_engines()
    for rel in ("scripts/run_smoke_test.sh", "pyproject.toml"):
        p = staged / rel
        jn.record_file(p)
        p.write_text(render_all(p.read_text(), rel,
                                post_roster, archived))
```

(Note: `_maybe_rewrite_frozen_literal` rewrites the frozen-literal tuple in `test_engine_lifecycle_consistency.py`, which is a *test pin*, NOT a generator-owned shadow per spec §10.1 — it is the clockwork's, not the manifest's. Per spec §10.1 and §10.5 the frozen-literal is explicitly NOT a generator concern. `_maybe_rewrite_frozen_literal` is therefore LEFT UNCHANGED in T5 — only `_shadow_edit_remove` migrates. This is consistent with H-S4-1's "the renderer replaces only the shadow-shape computation"; the frozen literal is not a shadow.)

- [ ] **Step 4: Run the FULL SP3 atomicity suite (the T5 make-or-break gate)**

Run:
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest \
  "tpcore/tests/test_engine_sdlc_planner.py::test_apply_red_consistency_rolls_back_to_byte_identical" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_apply_mid_move_loop_failure_byte_identical" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_apply_modify_edits_default_const_and_rolls_back_byte_identical" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_add_red_consistency_rolls_back_to_byte_identical" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_add_readiness_miss_rolls_back_to_byte_identical" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_apply_move_failure_restores_text_edits" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_remove_rostered_engine_updates_frozen_literal" \
  "tpcore/tests/test_engine_sdlc_planner.py::test_remove_throwaway_engine_end_to_end" \
  -q -p no:cacheprovider
```
Expected: ALL PASS — byte-identical rollback (renderer is pure str→str, executor still journals OLD bytes + writes NEW + rollback restores OLD; no new write path). If any rolls back non-byte-identical, the renderer was given a path or wrote a file — STOP and re-read the T5 atomicity invariant.

- [ ] **Step 5: Run the T5 pinning tests + the full planner + clockwork suites**

Run:
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_sdlc_planner.py tpcore/tests/test_engine_lifecycle_consistency.py -q -p no:cacheprovider
```
Expected: PASS — incl. `test_planner_shadow_edit_uses_renderer_not_inline_regex`, `test_renderer_never_called_with_a_path`, `test_renderer_is_pure_no_filesystem_io_in_planner_path`.

- [ ] **Step 6: Standing CI gate, verify branch, commit**

Run the standing CI gate. Expected: all green.

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add ops/engine_sdlc/planner.py tpcore/tests/test_engine_sdlc_planner.py
git commit -m "refactor(engine-sdlc): SP4 T5 — SP3 executor shadow edits via the ONE renderer (atomicity-preserving)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T6 — Doc-closure: `CLAUDE.md` (§11.1)

**Satisfies:** §11.1 doc-closure; H-S4-10 clauses b/c/d content lands here (validated by T8's gate).

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the Engine SDLC Architecture entry**

In `CLAUDE.md`, immediately AFTER the `- Future engines: s2/, catalyst/` line (line 25) and BEFORE the `**Engine credibility status …**` line (line 27), insert:

```
- **Engine SDLC (SP1→SP4, shipped 2026-05-18):** trading engines have a lifecycle — `LAB → PAPER → LIVE → RETIRED` (`tpcore.engine_profile.LifecycleState`). The roster SoT is `tpcore.engine_profile._PROFILE` (frozen pydantic-v2; the single mechanically-enforced source for what engines exist / order / cadence / lifecycle); every Python shadow is hard-derived and the non-Python shadows are regenerated by `scripts/gen_engine_manifest.py` (`--check` fails CI on drift). All five live engines (reversion, vector, momentum, sentinel, canary) are **PAPER** — none has graduated; `LIVE` is reserved (paper-only mandate). Durable sentinels prove the non-PAPER states are real and exercised: `sigma` (RETIRED) and `lab` (LAB). The N-way `tpcore/tests/test_engine_lifecycle_consistency.py` clockwork fails the build on any half-state. Spec: `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`.
```

- [ ] **Step 2: Update the credibility-status accuracy guard**

Replace the existing line 27:

```
**Engine credibility status as of 2026-05-13 (post data-cleanup):** All four engines produce positive OOS edge candidates (scores 0.78–1.26), all four still fail the DSR ≥ 0.95 / credibility ≥ 60 gate. Data foundation is clean; signal strength is the binding constraint.
```

with (accuracy guard — the literal substring "all five engines currently FAIL the DSR/credibility gate" is the H-S4-10(d) assertion target; do NOT imply any engine graduated):

```
**Engine credibility status (accuracy guard):** all five engines currently FAIL the DSR/credibility gate (DSR ≥ 0.95 ∧ credibility ≥ 60) — they produce positive OOS edge candidates (~0.78–1.26) but signal strength is the binding constraint, not data quality. No engine has graduated; `canary` is the one documented non-graduating heartbeat (never calls `write_credibility_score`, spec §4b).
```

- [ ] **Step 3: Add the Session Rules ECR/Lab commands**

In `CLAUDE.md` Session Rules, immediately AFTER the git-hygiene bullet (line 53, ends `…the reproducible source of the fetch.prune/gc.worktreePruneExpire config.`) and BEFORE `- Read docs/STYLE_GUIDE.md before writing any code.` (line 54), insert:

```
- **Engine roster/lifecycle changes go through the Engine Change Request ONLY — never hand-edit `_PROFILE`/shadows (the Sigma 22-site-drift rule), symmetric to the data-feed-change-request rule.** The operator approves exactly two operations: **ADD** an engine (new scaffold or Lab-graduated) and **REMOVE** one (retire/archive) — a binary `APPROVE? (y/n)` on a proven-consistent, dry-run-green diff. A **MODIFY** (re-tuned params that already cleared DSR≥0.95 ∧ cred≥60) and a **LAB→PAPER promote** (capital gate already green) are automated, deterministic, no approval. Canonical commands: `python -m ops.engine_sdlc --ecr <file>` (the ECR; `docs/superpowers/checklists/engine_change_request.md` is the structured touchpoint) and `python -m ops.engine_sdlc --promote <engine>`. The Lab (on-demand edge-hunt, recommendation-only, never daemon-wired): `python -m ops.lab --candidate <name> --target-engine {reversion|vector|momentum} --intent {promote_new|fold_existing}` → a `docs/lab/<dossier>.md` + byte-frozen `.json` sidecar. Known-limitation (recorded, NOT fixed): MODIFY is reversion-only today (`planner._ENGINE_DEFAULT_CONSTS` maps only `reversion`); a vector/momentum MODIFY is a documented fail-loud reject.
```

- [ ] **Step 4: Cross-ref in the engine-build compliance shortlist**

In the `- **Engine-build compliance shortlist**` block, AFTER the bullet `- Every new engine is added to the for engine in ...; do loop in scripts/run_all_engines.sh AND its docstring listing AND ops/platform_pipeline.py's docstring listing — these are what the engine-service daemon dispatches; an engine omitted from them never runs live.` (line 62), insert:

```
  - The non-Python shadows (smoke loop, run_all_engines.sh / platform_pipeline.py docstrings, pyproject testpaths/include) are sentinel-fenced regions regenerated by `scripts/gen_engine_manifest.py`. Do NOT hand-edit inside a fence; a roster change must regenerate (`python scripts/gen_engine_manifest.py`) or `gen_engine_manifest.py --check` reds CI — the engine-domain analog of the data generated-manifest discipline. The ADD path's build gate is `docs/superpowers/checklists/engine_readiness.md` (the 10 sections; the ECR machine-checks the `planner._check_readiness` subset, the rest is operator-verified).
```

- [ ] **Step 5: Standing CI gate, verify branch, commit**

Run the standing CI gate (T6 is a doc-only edit; the full suite is unaffected — T8's gate validates this content). Expected: all green.

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add CLAUDE.md
git commit -m "docs(engine-sdlc): SP4 T6 — CLAUDE.md Engine SDLC doc-closure

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T7 — Doc-closure: `docs/OPERATIONS.md` + the stale search-script reframe

**Satisfies:** §11.2 doc-closure; H-S4-11 (re-role NOT delete; `ls`-verify the named search scripts).

**Files:**
- Modify: `docs/OPERATIONS.md`

- [ ] **Step 1: `ls`-verify the named search scripts (H-S4-11)**

Run: `ls -1 scripts/run_sigma_search.sh scripts/run_vector_search.sh scripts/run_all_searches.sh scripts/search_parameters.py 2>&1`
Expected (the shipped reality — used to write accurate prose in Step 3): `scripts/run_sigma_search.sh` is **absent** (sigma archived 2026-05-16); `scripts/run_vector_search.sh`, `scripts/run_all_searches.sh`, `scripts/search_parameters.py` are **present**. Write the reframe to match exactly what `ls` reports — do not describe a script that does not exist.

- [ ] **Step 2: Add the new "Engine SDLC" section**

In `docs/OPERATIONS.md`, immediately BEFORE the `## 5.5 Parameter-Search Pipeline` heading (line 711), insert a new section:

```
## 5.4a Engine SDLC — the Engine Change Request + The Lab

Trading engines have a lifecycle: `LAB → PAPER → LIVE → RETIRED`
(`tpcore.engine_profile.LifecycleState`). The roster SoT is
`tpcore.engine_profile._PROFILE`. Canonical spec:
`docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`.

**The Engine Change Request (ECR) — the single operator touchpoint.**
Fill `docs/superpowers/checklists/engine_change_request.md` and run:

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m ops.engine_sdlc --ecr docs/superpowers/checklists/engine_change_request.md
```

The operator approves **exactly two** operations — **ADD** an engine
(new scaffold or Lab-graduated) and **REMOVE** one (retire/archive) —
a binary `APPROVE? (y/n)` on a proven-consistent, dry-run-green diff
(fail-closed: non-TTY / EOF / anything not `y`/`yes` ⇒ declined,
nothing changed, audit emitted). A **MODIFY** (re-tuned params that
already cleared DSR≥0.95 ∧ credibility≥60) and a **LAB→PAPER promote**
(`--promote <engine>`; capital gate already green) are automated,
deterministic, no approval. A request that cannot produce a consistent
diff is rejected with the exact reason — never handed to the operator
to force. Every terminal outcome emits one
`platform.application_log` `ENGINE_CHANGE_REQUEST` row. This tool is
on-demand, operator-driven, **NEVER wired into any daemon / dispatch /
engine_service** (parity with `python -m ops.lab`).

**The snap-out (REMOVE).** A REMOVE is atomic-or-abort: the SoT entry
flips to RETIRED (AST-validated single-entry rewrite), the
`ENGINE_TABLES` orphan is removed, the non-Python shadows are
regenerated, the package CONTENTS are physically moved to
`archive/<engine>/`, and an EULOGY is rendered from
`tpcore/templates/eulogy_template.md`. A failed transition leaves ZERO
trace (journaled byte-identical rollback).

**The consistency clockwork + the manifest gate.**
`tpcore/tests/test_engine_lifecycle_consistency.py` is the N-way
half-state-fails-CI oracle (a new/removed/archived engine fails the
build unless coherently wired or fully offboarded in the same change).
`scripts/gen_engine_manifest.py --check` is the CI-divergence gate that
regenerates every non-Python shadow from the SoT and fails on drift —
run `python scripts/gen_engine_manifest.py` after any roster change.

**Known-limitations (recorded, NOT fixed in SP4):** (a) MODIFY is
reversion-only today (`planner._ENGINE_DEFAULT_CONSTS` maps only
`reversion`; a vector/momentum MODIFY is a documented fail-loud
reject). (b) `_validate_modify`'s `type(want)(v)` coercion is a bool
footgun, harmless today (every Lab-swept param is numeric). Future-work
only; out of SP4 scope.

### The Lab runbook (`python -m ops.lab`)

The Lab is the operable form of `LifecycleState.LAB`: an isolated,
concurrent, shadow/candidate backtest harness for hunting parameter
edges WITHOUT touching the live platform. It is the canonical on-demand
edge-hunt entrypoint.

```bash
set -a; source .env; set +a
DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python -m ops.lab \
  --candidate myexp \
  --target-engine reversion \
  --intent fold_existing \
  [--param-overrides '{"z_threshold": 3.1}'] [--trials 50] [--seed 0]
```

- A separate OS process, operator-driven, **NEVER wired into any
  daemon / dispatch / engine_service**. No DSN ⇒ explicit non-zero rc
  + a logged error (never a silent 0).
- Isolation: `tpcore.lab.context.LabContext` forces the server pool
  read-only for the duration, provides the single allowlisted RW
  credibility pool, and installs a fail-closed reentrancy guard at
  every live-side-effect boundary.
- Output: a rendered `docs/lab/<day>-<candidate>-<verdict>-seed<seed>.md`
  PLUS a byte-frozen `.json` sidecar (the machine-readable evidence the
  ECR re-derives every gate number from). Credibility persists under
  the `lab.<candidate>` namespace.
- The dossier **recommends** a next step (`promote_new` → ADD a new
  engine; `fold_existing` → MODIFY the target; `none` → iterate) but
  the Lab **never applies it** — the ECR does, gated. Recommendation-
  only.
```

(Bash fenced blocks inside this Markdown are part of the inserted
prose; OPERATIONS.md is Markdown, not executed — the triple-backtick
fences are literal Markdown content.)

- [ ] **Step 3: Re-role (NOT delete) the `search_parameters.py`-as-prod framing**

In `docs/OPERATIONS.md`, replace line 713:

```
Production edge-discovery runs are driven by `scripts/search_parameters.py`. Random search + walk-forward + final held-back DSR verdict. Imports each engine's `load_*_window_context()` / `run_*_with_context()` programmatically — no subprocess. Per-window data load is shared across all candidates.
```

with (re-role: the canonical on-demand edge-hunt is now `python -m ops.lab`; `search_parameters.py` is re-described as the *underlying* harness — NOT deleted, H-S4-11):

```
The canonical on-demand edge-hunt entrypoint is now **`python -m ops.lab`** (§5.4a — isolated, recommendation-only, ECR-gated). `ops.lab.run` hosts the walk-forward Lab engine; `scripts/search_parameters.py` is a thin re-export shim delegating to it (re-roled, NOT deleted) — NOT the operator entrypoint. Random search + walk-forward + final held-back DSR verdict; imports each engine's `load_*_window_context()` / `run_*_with_context()` programmatically — no subprocess; per-window data load is shared across all candidates. The direct invocation below is the lower-level harness; prefer `python -m ops.lab` for an operator edge-hunt.
```

Replace line 755–757 (the convenience-wrappers list) — the H-S4-11 `ls`-verified reality is that `run_sigma_search.sh` does NOT exist. Replace:

```
- `scripts/run_sigma_search.sh` — Sigma 200-trial sweep.
- `scripts/run_vector_search.sh` — Vector sweep on T1+T2 (currently expected to produce zero trades until earnings_events backfill).
- `scripts/run_all_searches.sh` — sigma + reversion + vector back-to-back. **Note:** `set -e` is intentionally OFF; a FAILED verdict exits 1 but should not abort the multi-engine sweep.
```

with (accurate to `ls`; `run_sigma_search.sh` removed because sigma is archived and the script no longer exists):

```
- `scripts/run_vector_search.sh` — Vector sweep on T1+T2. (`scripts/run_sigma_search.sh` was removed when Sigma was archived 2026-05-16.)
- `scripts/run_all_searches.sh` — reversion + vector back-to-back. **Note:** `set -e` is intentionally OFF; a FAILED verdict exits 1 but should not abort the multi-engine sweep. These wrappers are the lower-level harness; the operator edge-hunt is `python -m ops.lab` (§5.4a).
```

- [ ] **Step 4: Standing CI gate, verify branch, commit**

Run the standing CI gate (doc-only; full suite unaffected — T8's gate validates the OPERATIONS.md clause). Expected: all green.

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add docs/OPERATIONS.md
git commit -m "docs(engine-sdlc): SP4 T7 — OPERATIONS.md Engine SDLC + Lab runbook + search-script re-role

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task T8 — Doc-closure: `engine_readiness.md` + `glossary.md` + the "docs match code" gate

**Satisfies:** §11.3, §11.4 doc-closure; H-S4-10 clauses a–e (the code-accuracy gate, lands in the same task as the last doc edit it validates).

**Files:**
- Modify: `docs/superpowers/checklists/engine_readiness.md`
- Modify: `docs/glossary.md`
- Create: `scripts/tests/test_sdlc_docs_match_code.py`

- [ ] **Step 1: Add the engine_readiness.md SDLC header note**

In `docs/superpowers/checklists/engine_readiness.md`, immediately AFTER line 5 (`Template: copy tpcore/templates/engine_template/ as the starting point — it satisfies most of these by construction.`) and BEFORE `## 1. Five Plugs present` (line 7), insert:

```
> **This checklist IS the Engine SDLC ADD-path build gate (spec §8 —
> `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`).** A
> `new_scaffold` ADD filed via the Engine Change Request
> (`docs/superpowers/checklists/engine_change_request.md` →
> `python -m ops.engine_sdlc --ecr <file>`) **machine-checks the
> programmatically-checkable subset** in
> `ops.engine_sdlc.planner._check_readiness`: the scaffold dir
> (`<engine>/`) exists, `<engine>/tests/` exists, `<engine>.scheduler`
> is importable, and exactly **5 `BaseEnginePlug` subclasses** are
> present in `<engine>/plugs/`. Every other item below is
> **operator-verified before filing the ECR** (the ECR does not and
> cannot machine-check human-judgement readiness). Items marked
> *(ECR-enforced)* are checked by `_check_readiness`; all others are
> *(operator-verified)*.
```

- [ ] **Step 2: Mark the ECR-enforced items accurately**

In `## 1. Five Plugs present`, append ` *(ECR-enforced: exactly 5 BaseEnginePlug subclasses in <engine>/plugs/)*` to the line:

```
- [ ] Every plug subclasses `tpcore.interfaces.engine_plug.BaseEnginePlug` and implements both `validate_dependencies` and `healthcheck`.
```

→ becomes:

```
- [ ] Every plug subclasses `tpcore.interfaces.engine_plug.BaseEnginePlug` and implements both `validate_dependencies` and `healthcheck`. *(ECR-enforced: exactly 5 BaseEnginePlug subclasses in <engine>/plugs/)*
```

In `## 6. Tests`, append ` *(ECR-enforced: <engine>/tests/ dir exists)*` to the first checklist line under that heading (the `<engine>/tests/` presence item — the line beginning `- [ ] <engine>/tests/`; if the exact wording differs, append the marker to whichever line asserts the `tests/` directory exists). In `## 7. Scheduler + daemon integration`, append ` *(ECR-enforced: <engine>.scheduler importable)*` to the line asserting `<engine>.scheduler` / `python -m <engine>.scheduler` is runnable.

- [ ] **Step 3: Add the bidirectional cross-link footer**

At the END of `docs/superpowers/checklists/engine_readiness.md` (after the final line, the `## Why this exists` section's content), append:

```

## SDLC cross-reference

This checklist is the ADD-path build gate of the Engine SDLC. See:

- `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md` — the
  canonical Engine SDLC spec (§8 names this checklist the ADD build
  gate).
- `docs/superpowers/checklists/engine_change_request.md` — the
  structured ECR touchpoint; `python -m ops.engine_sdlc --ecr <file>`
  machine-checks the `planner._check_readiness` subset of this
  checklist, the rest is operator-verified before filing.
```

- [ ] **Step 4: Add the 8 engine-domain glossary terms + fix the stale section count**

In `docs/glossary.md`, fix the stale line 52 — replace:

```
engine readiness checklist: Pre-merge gate at `docs/superpowers/checklists/engine_readiness.md`. 9 sections (5 plugs, shared tpcore reuse, risk gates, order layout, logging, tests, scheduler integration, backtest credibility, final checks) every new engine must satisfy. Mirrors the adapter readiness checklist.
```

with (the shipped file has 10 sections — §10 "Compliance verifications" was added 2026-05-15; and it is the SDLC ADD-path build gate):

```
engine readiness checklist: Pre-merge gate at `docs/superpowers/checklists/engine_readiness.md`. 10 sections (5 plugs, shared tpcore reuse, risk gates, order layout, logging, tests, scheduler integration, backtest credibility, final checks, compliance verifications) every new engine must satisfy. This checklist IS the Engine SDLC ADD-path build gate (spec §8); a `new_scaffold` ADD via the ECR machine-checks the `planner._check_readiness` subset. Mirrors the adapter readiness checklist.
```

Append to the END of `docs/glossary.md` (symmetric in form to the existing **Data Provider Lifecycle** / **ProviderBinding** / **Data Feed Change Request** entries, §11.4):

```

**Engine SDLC** — the lifecycle for trading engines; states `LAB → PAPER → LIVE → RETIRED`. Spec `docs/superpowers/specs/2026-05-18-engine-sdlc-design.md`. Operator approves ONLY ADD/REMOVE (binary y/n on a dry-run-green diff); MODIFY/promote are automated. Engine-domain analog of the Data Provider Lifecycle (symmetry of approach, not a clone — §9 ledger is binding). Added 2026-05-18.

**The Lab** — `python -m ops.lab`; an isolated, concurrent candidate backtest harness (the operable form of `LifecycleState.LAB`) → a two-exit graduation dossier (`docs/lab/<day>-<candidate>-<verdict>-seed<seed>.md` + a byte-frozen `.json` sidecar). Recommendation-only; never auto-applies; never daemon-wired. `lab.<candidate>` credibility namespace; isolation via `tpcore.lab.context.LabContext`. Added 2026-05-18.

**Engine Change Request (ECR)** — `docs/superpowers/checklists/engine_change_request.md` + `python -m ops.engine_sdlc --ecr <file>`; the single structured operator touchpoint for engine roster/lifecycle changes. Never hand-edit `_PROFILE`/shadows (the Sigma 22-site-drift rule). Added 2026-05-18.

**LifecycleState** — `tpcore.engine_profile.LifecycleState` StrEnum: `LAB`, `PAPER`, `LIVE`, `RETIRED`. `_DISPATCHABLE = {PAPER, LIVE}` is the single gate filtering every dispatch/allocator accessor. Added 2026-05-18.

**promote (engine)** — the automated, gated `LAB → PAPER` transition (`python -m ops.engine_sdlc --promote <engine>`); NOT an ECR action. Flips iff the capital-gate/`graduation_ready` authority is green; a promote without a resolved gate verdict is a hard reject. Added 2026-05-18.

**snap-out** — the engine REMOVE operation: state → RETIRED + physical `archive/<engine>/` move + EULOGY render + non-Python shadow regeneration, atomic-or-abort (journaled byte-identical rollback; a failed transition leaves ZERO trace). Added 2026-05-18.

**engine roster SoT** — `tpcore.engine_profile._PROFILE`; the frozen pydantic-v2, mechanically-enforced single source for what engines exist / in what order / cadence / lifecycle. Every Python shadow is hard-derived; the non-Python shadows are regenerated by the engine shadow-manifest. Added 2026-05-18.

**engine shadow-manifest** — `scripts/gen_engine_manifest.py`; the generator + `--check` CI-divergence gate that keeps the non-Python shadows (smoke loop, `run_all_engines.sh` / `platform_pipeline.py` docstrings, pyproject testpaths/include) byte-in-sync with the roster SoT. The engine-domain analog of the data generated-manifest discipline. Added 2026-05-18.
```

- [ ] **Step 5: Write the failing "docs match code" gate (H-S4-10)**

Create `scripts/tests/test_sdlc_docs_match_code.py`:

```python
"""SP4 T8 — the docs-match-code gate (H-S4-10).

Asserts the SP4 doc-closure prose against the SHIPPED modules so a
future doc edit claiming a command/state/behavior the code does not
have fails CI. Clauses a–e per the spec hardening register.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.*`` resolves the package —
# the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from tpcore.engine_profile import LifecycleState, roster_for_dispatch  # noqa: E402

CLAUDE = (REPO_ROOT / "CLAUDE.md").read_text()
OPS = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
GLOSS = (REPO_ROOT / "docs" / "glossary.md").read_text()


def test_clause_a_entrypoints_import_resolve():
    """(a) python -m ops.engine_sdlc / python -m ops.lab resolve."""
    assert importlib.util.find_spec("ops.engine_sdlc.__main__") is not None
    assert importlib.util.find_spec("ops.lab.__main__") is not None


def test_clause_b_documented_lifecycle_states_match_enum():
    """(b) the documented LAB→PAPER→LIVE→RETIRED == LifecycleState."""
    names = {s.name for s in LifecycleState}
    assert names == {"LAB", "PAPER", "LIVE", "RETIRED"}
    for doc in (CLAUDE, GLOSS):
        assert "LAB → PAPER → LIVE → RETIRED" in doc or \
               "LAB→PAPER→LIVE→RETIRED" in doc, (
            "the documented lifecycle ladder is absent/incorrect")


def test_clause_c_documented_roster_matches_sot():
    """(c) the roster line any doc states == roster_for_dispatch()."""
    sot = " → ".join(roster_for_dispatch())
    assert sot == "reversion → vector → momentum → sentinel → canary"
    # CLAUDE.md states the live engines; assert the SoT-derived names
    # all appear in the SDLC entry's engine list.
    for e in roster_for_dispatch():
        assert e in CLAUDE, f"{e} absent from CLAUDE.md SDLC entry"


def test_clause_d_claude_fail_the_gate_honesty_substring():
    """(d) CLAUDE.md states all five engines FAIL the gate (prevents a
    future edit implying a graduation)."""
    assert "all five engines currently FAIL the DSR/credibility gate" in CLAUDE


def test_clause_e_sp3_carry_forwards_provably_unchanged():
    """(e) the recorded SP3 known-limitations still match shipped code:
    _ENGINE_DEFAULT_CONSTS is reversion-only; _validate_modify still
    carries the type(want)(v) coercion line. A future accidental
    fix/regress fails this gate (the known-limitations are provably
    truthful, not aspirational)."""
    import inspect

    from ops.engine_sdlc import planner
    assert set(planner._ENGINE_DEFAULT_CONSTS) == {"reversion"}, (
        "SP3 carry-forward (a) changed: _ENGINE_DEFAULT_CONSTS is no "
        "longer reversion-only — the docs' known-limitation is now "
        "false (or this is an out-of-scope SP4 fix)")
    vm = inspect.getsource(planner._validate_modify)
    assert "type(want)(v)" in vm, (
        "SP3 carry-forward (b) changed: the type(want)(v) coercion "
        "line is gone — the docs' known-limitation is now false")


def test_operations_md_re_role_not_delete():
    """H-S4-11: OPERATIONS.md gained the python -m ops.lab canonical
    framing AND still references scripts/search_parameters.py (the
    re-role, NOT a delete)."""
    assert "python -m ops.lab" in OPS
    assert "scripts/search_parameters.py" in OPS
    assert "ops.engine_sdlc" in OPS, (
        "the Engine SDLC section / ECR command is absent from OPERATIONS.md")
```

- [ ] **Step 6: Run the gate to verify it passes (content from T6/T7/T8 now exists)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_sdlc_docs_match_code.py -q -p no:cacheprovider`
Expected: PASS — all 6 clauses (a entrypoints, b lifecycle states, c roster, d FAIL-the-gate honesty, e SP3 carry-forwards unchanged, the OPERATIONS.md re-role). If clause d fails, the T6 accuracy-guard substring is wrong; if clause e fails, an out-of-scope SP3 fix leaked in — STOP (SP4 must NOT fix the carry-forwards).

- [ ] **Step 7: Standing CI gate, verify branch, commit**

Run the standing CI gate. Expected: all green.

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add docs/superpowers/checklists/engine_readiness.md docs/glossary.md scripts/tests/test_sdlc_docs_match_code.py
git commit -m "docs(engine-sdlc): SP4 T8 — engine_readiness + glossary closure + docs-match-code gate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task Tn — SP4 scope-confinement gate + full-suite + finish-branch

**Satisfies:** H-S4-12 (the scope gate; SP3 carry-forwards recorded-not-fixed via the H-S4-10(e) clause from T8).

**Files:**
- Create: `scripts/tests/test_sp4_scope_confined.py`

- [ ] **Step 1: Write the scope-confinement gate (mirror the proven SP3 T9 pattern)**

Create `scripts/tests/test_sp4_scope_confined.py` (reuses the `scripts/tests/test_sp3_scope_confined.py` read-only `git diff --name-only` pattern verbatim in shape, incl. the `cef7368` CI-portability lesson: prefer `origin/main`, fall back to local `main`, skip-not-fail if neither resolves):

```python
"""Tn — SP4 change-set scope confinement (H-S4-12). The SP4 diff
against the SP4 base must be confined to the SP4 allowlist; NO
data-lane file (the 8 owned + the data-SDLC spec/checklist/registry).
Read-only `git diff --name-only` against a SNAPSHOT of names only (no
git mutation), never against a synthetic repo — the canonical scope
proof, mirroring scripts/tests/test_sp3_scope_confined.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.*`` resolves the package —
# the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]


def _resolve_sp4_base() -> str:
    """Prefer origin/main (the ref CI's PR checkout actually has); fall
    back to a local main; skip (not fail) if neither resolves — the
    scope gate must never false-RED on a checkout lacking the base ref
    (the SP3 cef7368 CI-portability lesson)."""
    for ref in ("origin/main", "main"):
        rev = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=REPO, capture_output=True, text=True)
        if rev.returncode != 0:
            continue
        mb = subprocess.run(  # noqa: S603
            ["git", "merge-base", "HEAD", ref],
            cwd=REPO, capture_output=True, text=True)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    pytest.skip("no SP4 base ref (origin/main / main) in this checkout")


# The 8 data-lane-owned files + the data-SDLC spec/checklist/registry
# SP4 must NEVER touch (spec §13.1, H-S4-12).
_FORBIDDEN_PREFIXES = (
    "tpcore/calendar.py",
    "tpcore/risk/",
    "tpcore/risk/governor.py",
    "ops/engine_supervisor.py",
    "ops/engine_service.py",
    "ops/engine_ladder.py",
    "tpcore/supervisor_state.py",
    "tpcore/trade_monitor.py",
    "tpcore/providers.py",
    "tpcore/feeds/",
    "tpcore/selfheal/",
    "docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md",
    "docs/superpowers/checklists/data_feed_change_request.md",
)

# The SP4 net-new surface + the enumerated in-place modifies (allowlist).
_ALLOWED_PREFIXES = (
    "scripts/gen_engine_manifest.py",
    "scripts/tests/test_gen_engine_manifest_render.py",
    "scripts/tests/test_engine_manifest_in_sync.py",
    "scripts/tests/test_sdlc_docs_match_code.py",
    "scripts/tests/test_sp4_scope_confined.py",
    "CLAUDE.md",
    "docs/OPERATIONS.md",
    "docs/superpowers/checklists/engine_readiness.md",
    "docs/glossary.md",
    "docs/superpowers/specs/2026-05-18-engine-sdlc-design.md",
    "docs/superpowers/plans/2026-05-18-engine-sdlc.md",
    "scripts/run_smoke_test.sh",
    "scripts/run_all_engines.sh",
    "ops/platform_pipeline.py",
    "pyproject.toml",
    "tpcore/tests/test_engine_lifecycle_consistency.py",
    "ops/engine_sdlc/planner.py",
    "tpcore/tests/test_engine_sdlc_planner.py",
)


def test_sp4_change_set_confined_to_net_new_surface():
    base = _resolve_sp4_base()
    names = subprocess.run(  # noqa: S603 — read-only name-only diff
        ["git", "diff", "--name-only", base, "HEAD"],
        cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for n in names:
        assert not n.startswith(_FORBIDDEN_PREFIXES), (
            f"SP4 touched a forbidden data-lane / data-SDLC file: {n}")
        assert n.startswith(_ALLOWED_PREFIXES), (
            f"SP4 touched a file outside the allowlist: {n} "
            f"(if intentional, the spec scope is wrong — escalate, do "
            f"not widen the allowlist silently)")
```

- [ ] **Step 2: Run the scope gate**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_sp4_scope_confined.py -q -p no:cacheprovider`
Expected: PASS (the full SP4 diff is confined to the allowlist; zero forbidden prefix) — or `SKIPPED` if no base ref resolves in this checkout (skip-not-fail; the gate runs for real in CI where `origin/main` exists).

- [ ] **Step 3: Run the FULL standing CI gate + the H-S4-10(e) carry-forwards-unchanged clause**

Run:
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest "scripts/tests/test_sdlc_docs_match_code.py::test_clause_e_sp3_carry_forwards_provably_unchanged" -q -p no:cacheprovider
```
Expected: full suite PASS, ruff clean, check_imports clean (the generator imports no engine — H-S4-4), the carry-forwards clause GREEN (SP3 (a)/(b) provably recorded-not-fixed).

- [ ] **Step 4: Verify branch, commit, finish the branch**

Run: `git -C /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab branch --show-current` → expect `worktree-engine-sp4`

```bash
cd /Users/michael/short-term-trading-engine/.claude/worktrees/engine-lab
git add scripts/tests/test_sp4_scope_confined.py
git commit -m "test(engine-sdlc): SP4 Tn — scope-confinement gate + full-suite green

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Finish the development branch**

Use the `superpowers:finishing-a-development-branch` skill: present the structured merge/PR options for the single CI-green-mergeable `worktree-engine-sp4` branch to `main` (this is the LAST sub-project of the operator-approved 4-chain SP1→SP2→SP3→SP4).

---

## Self-Review (writing-plans skill — run with fresh eyes against the spec)

### 1. Spec coverage — every spec § / H-S4-1..12 / DDF-1 / doc-closure / known-limitation maps to a task

| Spec item | Task |
|---|---|
| §1 Problem & Goal (canonical spec, doc closure, manifest+gate, known-limitations) | T0 (spec), T6/T7/T8 (docs), T1–T3 (manifest+gate), T6/T7/T8 (known-limitations recorded) |
| §2 Roster SoT (documented, changed none) | T6 (CLAUDE.md SoT entry), T8 (glossary `engine roster SoT`) |
| §3 Lifecycle states & transitions (documented) | T6 (CLAUDE.md), T7 (OPERATIONS.md), T8 (glossary `LifecycleState`/`promote`/`snap-out`) |
| §4 The Lab (documented) | T7 (OPERATIONS.md Lab runbook), T8 (glossary `The Lab`) |
| §5 Graduation gate (documented; accuracy guard) | T6 (CLAUDE.md accuracy guard), T7 (OPERATIONS.md), T8 clause d |
| §6 ECR (documented) | T6 (CLAUDE.md Session Rules), T7 (OPERATIONS.md), T8 (glossary `ECR`) |
| §7 Clockwork (documented + leg-6 fold + reverse leg) | T4 (fold + reverse leg), T6/T7 (documented) |
| §8 engine_readiness.md ADD build gate | T8 (header note + cross-links + ECR-enforced markers) |
| §9 Symmetry/divergence ledger (binding; in spec) | T0 (spec); referenced as symmetry-not-clone in T6/T8 prose |
| §10 Shadow-manifest generator + `--check` + §10.4 reverse + §10.5 no-redundancy | T1 (renderer), T2 (fences+DDF-1), T3 (`--check`), T4 (§10.4 reverse + §10.5 leg-6 fold), T5 (one-renderer dedup) |
| §11 Doc-closure deliverable (11.1–11.4) | T6 (11.1 CLAUDE.md), T7 (11.2 OPERATIONS.md), T8 (11.3 engine_readiness + 11.4 glossary) |
| §12 Reused-vs-new ledger | structurally honored across T1–T5 (compose) + T6–T8 (document) |
| §13.1 Non-goals | enforced by Tn scope gate + no-SP3-fix constraint |
| §13.2 Known-limitations (a)/(b)/(c) recorded | T6 (CLAUDE.md), T7 (OPERATIONS.md) record (a)/(b); pinned-unchanged by T8 clause e |
| §13.3 Future-work | recorded in T6/T7 known-limitations prose |
| §14.1 DDF-1 | T2 Step 7 (synthetic-tree → renderer) + T2 Step 8 (SP3 atomicity suite GREEN inside T2) |
| H-S4-1 | T1 (purity tests) + T5 (full SP3 atomicity suite + `test_renderer_never_called_with_a_path` + record_file-before-write) |
| H-S4-2 | T1 (`test_unmatched/duplicate/missing_close_sentinel_raises`, `test_text_outside_fence_is_never_touched`) |
| H-S4-3 | T2 (`test_generator_is_idempotent`, `test_check_clean_after_write`) |
| H-S4-4 | T1 (`test_generator_imports_no_engine`) + every-task check_imports gate |
| H-S4-5 | T3 (`test_hand_edit_in_fence_fails_check`, `test_hand_edit_out_of_fence_passes_check`) |
| H-S4-6 | T2 (`test_smoke_sh_still_parses`, `test_run_all_engines_sh_still_parses`, `test_pyproject_still_valid_toml`, `test_platform_pipeline_docstring_still_valid`) |
| H-S4-7 | T4 (`test_leg6_fails_on_roster_drift`, `test_leg6_green_on_clean_tree`, `test_clockwork_imports_no_ops`, full clockwork GREEN) |
| H-S4-8 | T4 (`test_live_engine_has_engine_tables_row` + the closed reverse predicate + deferred-comment delete + `test_reverse_engine_tables_leg_catches_a_missing_row`) |
| H-S4-9 | every SP4 `scripts/tests/` file carries the verbatim stanza + T3 `test_collision_preemption_stanza_present` |
| H-S4-10 | T8 (`test_sdlc_docs_match_code.py` clauses a–e) |
| H-S4-11 | T7 (`ls`-verify Step 1 + re-role not delete) + T8 `test_operations_md_re_role_not_delete` |
| H-S4-12 | Tn (`test_sp4_scope_confined.py` allowlist + FORBIDDEN data-lane list) |

**Result: no spec § / H-S4 / DDF-1 / doc-closure / known-limitation is without a home. No genuine spec gap found.** §9 is a spec-internal binding ledger (T0 territory — already in the committed spec); SP4 honors it as symmetry-not-clone in T6/T8 prose, not a code task, which the spec itself states ("the actual edits are SP4 implementation tasks; the §9 ledger governs how SP4 adapts").

### 2. Placeholder scan

No "TBD"/"implement later"/"similar to Task N"/"add the doc content"/"handle edge cases". Every code step has complete code; every doc step has the exact prose to insert verified against shipped reality (engine_readiness has 10 sections — confirmed; `run_sigma_search.sh` is absent — confirmed by `ls`; `ENGINE_TABLES` keys are exactly `{reversion, vector, momentum, sentinel, allocator, canary}` — confirmed; the H-S4-9 stanza is the verbatim `test_lab_cli_entrypoint.py:24-31` form — confirmed). The generator is COMPLETE code; the renderer signature `render_region(file_text: str, region: str, roster: tuple[str,...], archived: tuple[str,...]) -> str` / `render_all(file_text, file_rel, roster, archived) -> str` / `divergences(repo_root=None) -> str | None` is defined once in T1 and used identically in T2 (DDF-1 `render_all`), T4 (`divergences`), T5 (`render_all`).

### 3. Type / name consistency

- Renderer names: `render_region`, `render_all`, `divergences`, `ManifestFenceError`, `_FILE_REGIONS`, region ids (`smoke-loop`/`smoke-doc`/`all-engines-doc`/`pipeline-doc`/`pyproject-testpaths`/`pyproject-include`) — used identically across T1/T2/T3/T4/T5.
- `render_all` second arg is the **file-relative path** (`file_rel`, e.g. `"scripts/run_smoke_test.sh"`) consistently in T1 (definition), T2 Step 7 (DDF-1 `render_all(_p.read_text(), _rel, …)`), T5 (`render_all(p.read_text(), rel, …)`) — keyed against `_FILE_REGIONS`. Consistent.
- The H-S4-9 stanza string `for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:` is byte-identical in every SP4 test file and in T3's `test_collision_preemption_stanza_present` needle. Consistent.
- Reverse predicate `set(roster_for_dispatch()) - (set(ENGINE_TABLES) - {"allocator"})` is identical in T4 Step 1 (`test_live_engine_has_engine_tables_row`), T4 Step 4 (the folded leg), and the Tn-relevant H-S4-8 wording. Consistent.
- T5 keeps `_maybe_rewrite_frozen_literal` unchanged (the frozen literal is a clockwork test-pin, NOT a generator shadow per spec §10.1/§10.5) — only `_shadow_edit_remove` migrates. This is internally consistent with H-S4-1 ("the renderer replaces only the shadow-shape computation") and the spec's explicit "the frozen-literal tuple is rewritten by the SP3 executor in the staged diff (not a generator concern)".

**Inconsistency caught & fixed inline:** T3's red-first is unusual because `--check`/`divergences()` ship in T1 — the plan documents this explicitly in T3 Step 2 (the TDD red is the absence of the CI subprocess test itself; the behavior under test was implemented earlier) rather than fabricating a fake failing state. Likewise T4 Step 2 documents that the new reverse-leg/delegation tests pass-as-written and the real T4 red-first is the leg-6 *fold* (asserted by inspection). Both are noted in-task so an executor reading out of order is not misled. No code/name inconsistency remains.

---

*Lane: ENGINE. Data-SDLC files (`tpcore/providers.py`, `tpcore/feeds/`, `tpcore/selfheal/`, the 2026-05-17 data-provider spec/checklist) are READ-ONLY symmetry reference, untouched. The 8 data-lane-owned files are FORBIDDEN (Tn gate). The SP3 (a)/(b) carry-forwards are RECORDED in docs and pinned-unchanged (T8 clause e), never fixed. This is the LAST sub-project of the operator-approved 4-chain SP1→SP2→SP3→SP4.*
