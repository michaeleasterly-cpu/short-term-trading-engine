---
name: tests-and-ci
paths:
  - "tests/**"
  - "**/tests/**"
  - "pyproject.toml"
  - "pytest.ini"
  - ".github/workflows/**"
description: "Path-scoped rule: full single-process pytest + order-flip is authoritative; ops-package-shadow warning; gh pr checks conclusion gate; tests never touch the real git/gh."
---

# Tests & CI discipline

Canonical SoT: `pyproject.toml` (`[tool.pytest.ini_options]`, ruff config, per-file SLF ignores), `.github/workflows/ci.yml`, `tests/test_dev_pipeline_standard_present.py` (the anti-rot sentinel).
Authoritative external: <https://code.claude.com/docs/en/extend>, <https://code.claude.com/docs/en/context-window>.

Discipline:

- **Authoritative gate** = `python -m pytest -p no:xdist` (whole suite, one process) + the **reversed module order**. The parallel `-n auto --dist loadgroup` is the fast accelerator only. A green parallel run + a red `-p no:xdist` / order-flip is a FAIL.
- **`gh pr checks <n>`**, NEVER `gh run watch` (its exit code is a documented misreport).
- **Gate on `statusCheckRollup` conclusion==SUCCESS**, NOT `mergeStateStatus`==CLEAN. Docs-path PRs flip CLEAN before pytest finishes — operator's 2026-05-19 memo.
- **Ops-package-shadow rule**: `ops/*.py from ops import …` ↔ `scripts/ops.py` collision. Subset / single-file pytest runs are unrepresentative of CI. Run the whole suite + order-flip. New tests that touch `sys.modules['ops']`, `spec_from_file_location(ops)`, or `importlib`-of-ops MUST carry `pytest.mark.xdist_group("ops_shadow")`; `tests/test_xdist_group_manifest.py` reds CI without it.
- **Hermetic tests** (the SP-D CI lesson): defer `import ops.lab.run` / DB / network imports IN-BODY; stub pools/credibility (the `_SharedPool` / `_FakeConn` precedent); never a collection-time `sys.modules` purge.
- **No `dashboard.py` import in a CI test** — `streamlit` not in CI venv.
- **CI venv has no streamlit** → never `import dashboard.py` in a CI test (pure `dashboard_components/` only).
- **Tests/code MUST NEVER run real `git`/`gh` against the working repo.** Isolate in a `tmp_path` throwaway repo OR inject a fake runner + a host-repo guard that fails loud (PR #61 leaked-branch lesson).
- **`vulture --fail-on-new-dead-code`** is part of the CI gate — run it locally before push.
- **Ruff** `E,F,I,B,UP,DTZ,SLF`; `**/tests/**` ignores only `E741,E702`; **NO inline `# noqa`**; scoped pyproject per-file SLF ignores are the canonical form for legit engine-lane-module-private test access — never widened.
- **Lean cadence**: ONE review per PR (see `docs/DEV_PIPELINE_STANDARD.md` §0 default + fast lanes); `split-review` only on heavy lane.
- **Tool-walk excludes** in `pyproject.toml` (ruff `extend-exclude`, pytest `norecursedirs`), the tracked `.ignore` file, and `respect-gitignore=false` are deliberate — do NOT re-enable `respect-gitignore`; the tracked `.ignore` is the SoT.
