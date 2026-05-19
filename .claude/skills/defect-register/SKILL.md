---
name: defect-register
description: "Slash-only wrapper for the consolidated defect register — python -m ops.defect_register {list,log,resolve}. Derived read-model composing both Escalation & Hardening Ladders + the REVIEW_DEFECT_LOGGED primitive; NOT a new SoT."
disable-model-invocation: true
---

# Consolidated defect register

Canonical CLI: `python -m ops.defect_register {list,log,resolve}`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

The consolidated defect register is a **derived read-model** (NOT a new SoT/table/daemon — the parallel-SoT anti-pattern is rejected) that composes BOTH Escalation & Hardening Ladders' read APIs verbatim:

- `engine_ladder.list_undispositioned()` (engine lane)
- `weekly_digest.build_weekly_digest().undispositioned_entries` (data lane)

…plus ONE minimal `application_log` event class (`REVIEW_DEFECT_LOGGED` / `REVIEW_DEFECT_RESOLVED`, retention-exempt) for the today-homeless review-found-defect class — joined by `defect_ref` (never summed).

## Usage

```bash
# List undispositioned + open REVIEW_DEFECT_LOGGED rows past grace
python -m ops.defect_register list

# Log a review-found defect (joined by defect_ref)
python -m ops.defect_register log --defect-ref <ref> --description "<one line>"

# Resolve a logged defect
python -m ops.defect_register resolve --defect-ref <ref>
```

## Forcing rule (CI-enforced)

A review-found defect tagged in `TODO.md` with `[defect_ref: X]` MUST have a matching open `REVIEW_DEFECT_LOGGED` (CI forcing-test — it cannot live only in TODO.md and be forgotten).

## Render

The Health-tab panel `render_defect_register` is **render-only** (recomputes nothing; no write button — spec §5 OUT).

## Adjacent SoT

- `ops/defect_register.py`
- `docs/superpowers/specs/2026-05-19-consolidated-defect-register-design.md`
- `docs/ESCALATION_HARDENING_LADDER.md` (data lane)
- `docs/ENGINE_ESCALATION_HARDENING_LADDER.md` (engine lane)
- `.claude/skills/weekly-digest/SKILL.md`
