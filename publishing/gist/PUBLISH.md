# Gist publishing checklist

Public-gist staging for the trading-engine documentation handoff. The
`staged/` subdirectory contains a curated, PII-scrubbed snapshot of the
repository docs that go in the gist. **This is staging only — the gist
is not published from CI; the operator runs the `gh` command below.**

The expert brief decisions baked into this staging:

- Apache 2.0 license (governs the companion `stelib` PyPI carve, not
  this gist directly — the gist itself is "code & docs as displayed on
  the gist page", which inherits the source repo's license once that's
  set).
- Single public gist, multi-file (gh CLI supports it).
- Operator-driven publish; **no autonomous `gh gist create`**.

## What's in `staged/`

- `README.md`, `CLAUDE.md`, `TODO.md` — top-level repo intros.
- `archive/sigma/EULOGY.md` — historical sigma-engine retro.
- `docs/` — the major spec/runbook/persona/research-spike set:
  - `DATABASE_AND_DATAFLOW.md`, `DEV_PIPELINE_STANDARD.md`,
    `EDGE_VALIDATION_PLAN.md`,
    `ENGINE_ESCALATION_HARDENING_LADDER.md`,
    `ESCALATION_HARDENING_LADDER.md`, `MASTER_PLAN.md`,
    `OPERATIONS.md`, `STYLE_GUIDE.md`, `glossary.md`,
    `MEMORY_MAINTENANCE.md`, plus the two LLM-triage persona/runbook
    files.
  - `docs/superpowers/specs/*.md` — all design specs (50 files).
  - `docs/decisions/`, `docs/runbooks/`, `docs/personas/`,
    `docs/research-spikes/` — supporting prose.

## What's explicitly excluded (per the expert brief)

- `.env*`, `.claude/` — secrets and agent state.
- `backtests/`, `docs/lab/`, `docs/lab_emitter_references/` — research
  workpaper output.
- `docs/session-log.md`, `docs/audits/` — operator-only narrative.
- Engine code (`reversion/`, `vector/`, `momentum/`, `sentinel/`,
  `canary/`, `catalyst/`, `carver/`) — code goes in the `stelib` PyPI
  carve, not the doc gist.
- `platform/migrations/` — schema is operator-internal until the next
  step.
- ECR/DFCR control files (`ecr_*.txt`, `dfcr_*.txt`).

## PII redaction applied at staging time

- `docs/superpowers/specs/2026-05-10-data-validation-suite-design.md`
  line ~355: `michaeleasterly-cpu/short-term-trading-engine` →
  `<github-handle>/<repo>` (the only operator-identity hit the brief
  surfaced).

## Operator pre-flight checklist

1. Confirm the four CI gates on this PR are green.
2. From the repo root, re-run the scrub:
   ```bash
   bash publishing/gist/scrub.sh
   ```
   It should print `PASS: no scrub hits — staging is publish-eligible.`
   If any line hits, redact or remove the offending file from
   `publishing/gist/staged/` and re-run.
3. Eyeball the staged file count and the diff vs. the source files —
   any "huh I didn't expect that" goes back into staging review, not
   the gist.
4. Decide whether the gist should be `--public` or `--secret`. The
   brief assumes public; if the operator wants a private link share
   instead, swap the flag below.

## Publish command

The gh CLI handles multi-file gists by listing each file explicitly.
The staging tree is flat enough that this can be a one-shot:

```bash
# Public gist with a description
gh gist create \
  --public \
  --desc "short-term-trading-engine — research & design handoff" \
  $(find publishing/gist/staged -type f | sort)
```

After `gh gist create` prints the gist URL, capture it in your
session log and (optionally) link it from the repo README.

## After publish

- The gist URL is permanent; the gist history is public. Treat it as
  "anything in here is now on the internet forever."
- If a future scrub pattern uncovers a leak in a published gist,
  delete the gist (`gh gist delete <id>`) rather than editing — the
  gist edit history is also public.
- For an updated handoff: re-stage from a fresh source tree, re-run
  `scrub.sh`, and `gh gist edit <id> --add path/to/new/file.md` (or
  `gh gist create` a new one — gists don't merge cleanly across
  large doc deltas).
