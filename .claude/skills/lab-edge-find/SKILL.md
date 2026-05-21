---
name: lab-edge-find
description: "Slash-only wrapper for the Task #25 Path B autonomous LLM edge-finder — python -m ops.llm_edge_finder --trigger operator_command [--target <engine>] [--reference-bundle <name>]. Runs ONE finder cycle: assembles MarketSnapshot, reads regime, runs the bounded LLM↔tool-sandbox loop (≤10 turns × ≤4 tool calls), emits up to 3 ProposedSpecs via SP-G emit_once. Cost-net Sharpe is the binding metric; persona v2.0 honors the operator's 'I know it when I see it' outcome contract."
disable-model-invocation: true
---

# Lab edge-find (Task #25 — autonomous LLM edge-finder)

Canonical CLI: `python -m ops.llm_edge_finder --trigger operator_command [--target <engine>] [--reference-bundle <name>]`.
Authoritative external: <https://code.claude.com/docs/en/skills>.

## What this skill does

Runs **one** Task #25 Path B autonomous finder cycle:

1. **Phase A — Snapshot:** assembles `MarketSnapshot` from the 14+ ingested substrates (`prices_daily`, `fundamentals_quarterly`, `macro_indicators`, `aaii_sentiment`, `fear_greed`, `spread_observations`, `short_interest`, `borrow_rates`, `earnings_events`, the per-regime ledger, the engine roster). Computes the 5-axis `MarketRegime` (vol / trend / macro / sentiment / cycle_position). Loads the 3 mandatory reference bundles (`dsr_ntrials_discipline.md`, `regime_aware_trading.md`, `market_structure_primer.md`) + any caller-named extras (Carver / Chan).
2. **Phase B — Analysis:** bounded LLM ↔ tool-sandbox loop (≤10 turns × ≤4 tool calls). The LLM emits `AnalysisRequest` envelopes; the agent dispatches `ToolCall` through `tpcore.lab.llm_finder.tool_sandbox` (14-callable whitelist: `OLS_HAC_NW`, `adfuller`, `coint`, `ARIMA_1_0_0`, `spearmanr`, `pearsonr`, `ttest_1samp_HAC`, `variance_ratio`, `hurst_exponent`, `ljung_box`, `rolling_spearmanr`, `rolling_pearsonr`, `fama_macbeth`, **`cost_net_simulation`**).
3. **Phase C — Emission:** up to `EDGE_FINDER_RUN_QUOTA=3` `ProposedSpec` emissions per run. Each carries `cost_assumption_bps_roundtrip`, `regime_tuple_id` matching snapshot, `analysis_evidence_refs` citing tool results. Per spec §4.5, primary_metric is locked to `cost_net_sharpe` (NOT raw Sharpe).

The finder is **autonomous** (Path B). When wired into Phase D-F (follow-up PRs), each emission flows through SP-G `emit_once_with_auto_promote` → CI gate → auto-merge → ECR-MODIFY → LAB → PAPER. Operator audits OUTCOMES via the §12 dashboard ("I know it when I see it"); the loop reads the operator-posted `LAB_FINDER_OUTCOME_VERDICT` event + the mechanical `$5k bleed-cap` for auto-retire.

The finder NEVER:
- Self-merges a PR (draft + human-merge-only at the SP-G layer until Phase D ships);
- Mutates the roster directly (every change is via ECR);
- Bypasses the SP-A cumulative ledger (per-regime + aggregate fences — constraints 14 + 17);
- Calls the Anthropic SDK with a `tools` payload (advisory contract);
- Imports anything outside the §6 whitelist (`statsmodels` + `scipy.stats` only; CI-grep fence);
- Proposes a spec whose `cost_net_sharpe` (bootstrap 95% CI lower bound) is below 0 (cost-honesty fence; persona §6 self-reject directive).

## Usage

```bash
# Default — operator command, finder picks target from snapshot.roster:
python -m ops.llm_edge_finder --trigger operator_command

# Target a specific engine:
python -m ops.llm_edge_finder --trigger operator_command --target reversion

# Add caller-named reference bundles (the 3 mandatory bundles are always included):
python -m ops.llm_edge_finder --trigger operator_command \
    --reference-bundle carver_systematic_trading

# Multiple caller bundles (comma-separated):
python -m ops.llm_edge_finder --trigger operator_command \
    --reference-bundle carver_systematic_trading,chan_algorithmic_trading
```

The `--trigger` flag exists for parity with the daemon's event-driven paths
(`ledger_capacity_event`, `regime_change_event`); operator-invoked runs
default to `operator_command`. The trigger value lands in
`FinderRun.trigger` for §12 audit-dashboard distinction.

## Pre-conditions

- `ANTHROPIC_API_KEY` is set in the operator's environment. If unset,
  the finder degrades to smoke-mode (Phase A only; provenance row still
  lands; no emissions).
- `DATABASE_URL` (or `DATABASE_URL_IPV4`) reaches the canonical
  Postgres instance.
- The 3 mandatory reference bundles exist on disk under
  `docs/lab_emitter_references/`:
  `dsr_ntrials_discipline.md`, `regime_aware_trading.md`,
  `market_structure_primer.md`. (Loader fails loud on missing or stub
  content — `[operator-pending content]` marker triggers
  `ReferenceStubError`.)
- The persona file `docs/lab_finder_persona.md` matches
  `PERSONA_SHA256` in `tpcore/lab/llm_finder/__init__.py`. Drift reds
  the `test_persona_versioned` sentinel.

## What this skill does NOT do

- It does NOT run a Lab probe (`python -m ops.lab` is `/lab-target-run`).
- It does NOT modify the roster directly (`/ecr` is the ECR touchpoint).
- It does NOT modify provider bindings (`/dfcr` is the DFCR touchpoint).
- It does NOT change the gate (DSR ≥ 0.95 ∧ credibility ≥ 60 stays sacred — spec §2.3).
- It does NOT bypass the per-regime + aggregate ledger fences (constraints 14 + 17).
- It does NOT promote a `ProposedSpec` past the SP-G gate without the
  full fence stack (Phase D auto-promote is a follow-up PR; v1 leaves
  emissions at the draft-PR-only boundary).

## Outputs

- ONE `FinderRun` row in `platform.application_log`
  (`event_type='LAB_FINDER_RUN'`, payload = full `FinderRun.model_dump_json()`).
- ZERO-or-N `LAB_FINDER_ACTION` rows per autonomous step (per spec §2.16
  provenance contract). v1 emits at the post-emission boundary only;
  Phase D adds draft/undraft/merge/ecr_modify/ecr_retire actions.
- Up to `EDGE_FINDER_RUN_QUOTA=3` rendered candidate-spec markdowns
  under `docs/lab/` (post-Phase-D follow-up PR).
- One JSON sidecar per emission (post-Phase-D follow-up).
- N draft PRs (post-Phase-D follow-up; v1 cycle stops at the
  in-memory `ProposedSpec` boundary — the agent doesn't open PRs until
  Phase D ships).

## Operator runbook

Full runbook: `docs/llm_edge_finder_operator_runbook.md`.

Failure modes:
- **`AuthSkip`** (no `ANTHROPIC_API_KEY`) → smoke-mode run; provenance lands; no emissions.
- **`ReferenceNotFoundError`** / **`ReferenceEmptyError`** / **`ReferenceStubError`** → mandatory bundle missing/empty/placeholder; fix the bundle file, retry.
- **`SnapshotOverflowError`** → snapshot serialized > 512 KiB; reduce universe or window. Fail-loud (NEVER silent truncation).
- **JSON-decode failure** from LLM response → agent surfaces a synthetic `AnalysisRequest` with `rationale='llm_json_decode_failed'` and continues the loop. Operator audits via §12 if persistent.
- **`AgentError`** (LLM envelope `kind` not in `{AnalysisRequest, AnalysisResult}`) → finder stops; `FinderRun.rejection_reason` carries detail; provenance row lands.

## Related skills

- `/lab-spec-emit` — SP-G manual single-emission emitter (the layer below
  Task #25 — the finder calls `emit_once` once Phase D ships).
- `/lab-target-run` — runs a Lab probe + dossier (NOT the finder).
- `/ecr` — Engine Change Request (the touchpoint a finder-emitted spec
  eventually reaches once Phase D auto-issues an ECR-MODIFY).
- `/audit-data-pipeline` — full data-lane audit (the snapshot reads
  the same substrate the audit validates).
