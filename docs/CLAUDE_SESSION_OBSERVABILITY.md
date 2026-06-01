# Claude Session / Cost Observability

C0.5 (2026-06-01) — manual, local, metadata-only Claude session
and cost report. Built to track Claude-assisted development usage
*without* storing transcript content, prompts, tool payloads,
secrets, or any operator-private data.

Companion artifacts:

- `scripts/claude_session_report.py` — stdlib-only manual report
  generator.
- `scripts/run_claude_session_report.sh` — operator wrapper with
  safe defaults.
- `tests/test_claude_session_report.py` — sentinels enforcing the
  whitelist + redaction + fail-closed schema invariants.
- `.gitignore` whitelist — generated reports never enter git.

> **Master rule**: this tooling is *report-only*. It never calls
> the Anthropic API, never writes to memstores, never writes to
> the database, never deploys, never runs as a daemon, and never
> stores raw transcript content. Per `docs/MEMSTORE_HANDOFF.md`
> (C0.1) tier 4 — repo docs/tests/hooks override every memory
> tier — generated reports are tier 4 artifacts placed in a
> gitignored operator-local directory.

## §1 — What this tool is

The Claude Code session jsonl files at
`~/.claude/projects/-Users-michael-short-term-trading-engine/*.jsonl`
carry both load-bearing metadata (token counts, model names,
timestamps, tool-call names) AND sensitive transcript content
(prompts, responses, tool arguments, tool results, attachment
payloads, file-history snapshots, queue-operation content). The
report script extracts only the metadata; everything else is
forbidden.

The tool answers operator questions like:

- How many Claude sessions did this project consume last week?
- What's the input/output token mix?
- What models were used, and at roughly what cost?
- Which tool-call names dominate (Bash vs Edit vs Read)?
- Is the local jsonl schema drifting in a way that warrants a
  script bump?

It does NOT answer questions like:

- What was the operator's latest prompt? (forbidden)
- What did Claude reply to query X? (forbidden)
- What's the content of attached file Y? (forbidden)

## §2 — Safe metadata whitelist

The report contains exactly these top-level fields (any other key
in the script output is a defect):

| Field | Source |
|---|---|
| `report_generated_at` | `datetime.now(UTC).isoformat()` |
| `repo` | `os.getcwd()` basename |
| `git_branch` | `git rev-parse --abbrev-ref HEAD` |
| `git_commit` | `git rev-parse HEAD` |
| `session_file_count` | count of `*.jsonl` in input dir |
| `session_date_range` | min / max of event `timestamp` (UTC ISO) |
| `estimated_total_sessions` | distinct `sessionId` values |
| `tool_call_counts_by_tool_name` | `{<tool_name>: <count>}` from `tool_use` blocks (NAMES ONLY) |
| `model_names` | distinct `message.model` values |
| `token_counts` | sums of `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` |
| `estimated_cost_usd` | per-model cost using the hardcoded snapshot in §5 |
| `cost_rate_snapshot` | the rate table (model → price/MTok) the run used |
| `redaction_count` | count of secret-shaped values redacted from whitelisted strings |
| `warnings` | drift warnings (unknown event types, unknown safe-metadata keys, missing cost rates) |
| `input_source_paths` | input jsonl paths (paths only) |
| `output_report_path` | written report path |
| `version_label` | `c0.5` |
| `script_sha256` | SHA-256 of the script file (tamper-detect for operator-recorded reports) |

## §3 — Forbidden data model

Never recorded in any report under any flag:

- `message.content` text (assistant or user) — prompts, responses.
- `message.content[*].input` / `.text` / `.tool_result` payloads.
- `attachment.attachment` payloads.
- `file-history-snapshot.snapshot` payloads.
- `queue-operation.content` payloads.
- Tool *arguments* and tool *results* (only tool *names* are counted).
- API keys, OAuth tokens, SSH/RSA keys, Postgres URLs with embedded credentials, broker credentials, private financial balances, raw logs, raw backtest dumps, unredacted environment variable values.
- `aiTitle` is **excluded by default** because it can leak a
  prompt summary. Opt in with `--include-ai-titles` if your
  workflow needs it; the operator decision is recorded in the
  report's warnings list.

The script defensively scans every whitelisted string value
against a secret-shape regex panel (`sk-`, `gh[ps]_`, `xox[bp]-`,
`postgres(ql)?://u:p@`, `Bearer ...`, `password = ...`). Any hit
is replaced with `<REDACTED:secret_pattern>` and increments
`redaction_count`. Configurable cap via `--max-redactions`
(default 10); exceeding the cap fails the run closed so an
operator never publishes a redacted-but-still-suspicious report.

## §4 — Output behavior

Default output directory: `.operator/reports/claude/` (gitignored
per the C0.5 `.gitignore` whitelist). Override with
`--output-dir <path>`. The script also gitignores
`claude-session-report*.{json,md,html}` patterns at the repo root
so an `--output-dir .` invocation still doesn't leak the report.

Default format: `--format json`. `--format markdown` writes a
human-readable summary; the markdown body contains exactly the
same metadata fields as the JSON output, never any extracted
transcript content. The redacted-summary print to stdout is the
report path + the headline counts only — never raw transcript
lines.

Other flags:

- `--input-dir <path>` — default is
  `~/.claude/projects/-Users-michael-short-term-trading-engine/`.
- `--dry-run` — emit nothing to disk; print summary only.
- `--best-effort` — **intended for triage runs only.** Gracefully
  skips unknown event types with an aggregated warning instead of
  failing closed. Default is OFF; normal runs should fail closed
  on truly unknown schema so a real drift gets a human look. Use
  `--best-effort` when you're already investigating drift and want
  the partial report.
- `--max-files <N>` — refuse to read more than N input jsonl
  files in one invocation (defense in depth; default 200).
- `--max-redactions <N>` — fail-stop ceiling on redacted-string
  count (default 10).
- `--include-ai-titles` — opt in to the `aiTitle` event type per
  §3.

### Recognized event types and ignored payload surfaces

The script's `_KNOWN_EVENT_TYPES` allowlist covers every event
type observed in current Claude Code session jsonl files:
`assistant`, `user`, `system`, `attachment`, `ai-title`,
`last-prompt`, `permission-mode`, `queue-operation`,
`file-history-snapshot`, plus the routing-metadata types
`pr-link`, `worktree-state`, `custom-title`, `mode`,
`agent-name`. The routing-metadata types are recognized so the
default fail-closed mode succeeds on real session files, but
**their bodies are never walked** — the script reads only their
`sessionId` and `timestamp`. The `customTitle` / `agentName` /
`prUrl` payload fields stay forbidden (see §3) and are silently
ignored — they never enter the report.

A new event type Anthropic ships in a future Claude Code release
will fail-closed by default, surfacing one aggregated warning so
an operator can decide whether to add it to the allowlist after
confirming its payload shape.

Warnings are aggregated by category — a session jsonl with
thousands of routing-metadata events of one unknown type
produces ONE warning line with the event count, not thousands of
per-event lines.

## §5 — Cost rate snapshot

Costs are estimated using a **hardcoded snapshot** of Claude API
list-published rates (USD per 1 M tokens) as of the script's
`rate_snapshot_date`. Rates drift; the snapshot is intentional, so
a future operator can audit which rates produced a given report.
The snapshot table lives at the top of
`scripts/claude_session_report.py` (`_COST_RATE_SNAPSHOT`); bump
it deliberately when Anthropic publishes new rates and re-run
reports if you need historically-comparable numbers.

If the script encounters a model name the rate table doesn't
know, `estimated_cost_usd` for that model is `null` and a
warning is added. The aggregate `estimated_cost_usd` is the sum
of known-model costs only; the warnings list names every unknown
model.

## §6 — Invocation

Through the wrapper (operator-typed):

```bash
./scripts/run_claude_session_report.sh                 # JSON to .operator/reports/claude/
./scripts/run_claude_session_report.sh --dry-run       # summary only, no file written
./scripts/run_claude_session_report.sh --format markdown
./scripts/run_claude_session_report.sh --best-effort   # for triage on schema drift
```

The wrapper applies safe defaults and calls
`python scripts/claude_session_report.py "$@"`. Both forms are
manual-only — there is no daemon, no schedule, no auto-export.

## §7 — Forbidden runtime actions

Per the C0.4 cascade master rule:

- Never calls the Anthropic API.
- Never writes to Anthropic memstores
  (`memstore_01P5DiJJgau4NhMMekaZDQEN` or
  `memstore_01MzLun3AfRf2viPmDqJvsWi`).
- Never writes to the platform database.
- Never uses Docker.
- Never invokes `railway up` or any deploy command.
- Never opens network connections (no `requests`, `httpx`,
  `urllib3`, `aiohttp`, `anthropic` imports — verified by the
  sentinel test).
- Never auto-fixes or auto-merges.
- Never runs as a daemon — `scripts/run_claude_session_report.sh`
  is operator-typed only; no entry in
  `scripts/install_all_daemons.sh`.

## §8 — Future scope

Out of scope for C0.5, deferred to a later C0.x or D0:

- A Postgres `claude_sessions` table (option_c in the C0.5 plan).
  Storing redacted metadata in the DB would make it queryable but
  needs a schema migration + retention policy + access-control
  story; the manual report is a strict superset of operator
  needs today.
- An automated weekly Claude-session digest analogous to
  `ops/weekly_digest.py`. The current weekly digest is engine-
  state-only; folding Claude-session data into it would require
  the DB store above.
- HTML output. The gitignore pattern reserves
  `claude-session-report*.html` for forward compatibility but
  C0.5 does not emit HTML.
