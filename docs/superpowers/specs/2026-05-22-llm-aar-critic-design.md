# LLM-AAR Critic — Design Spec v1.0

**Status:** DESIGN — third autonomous LLM agent (data-triage, edge-finder, AAR-critic). Operator binding 2026-05-22: close the engine-improvement loop. Spec is ship-ready; build follows immediately in this PR cycle.

**Composes with:** Task #25 LLM edge-finder (PR #294 Sessions-API wiring; spec `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md`). The AAR-critic is upstream of the finder in the autonomous loop:

```
engines trade → AARs → LLM-AAR critic finds patterns → memstore findings → finder reads findings → finder emits regime-conditional candidates → SP-A gate → Lab → PAPER
```

**Lane:** heavy (engine-SDLC-adjacent — new advisory LLM consumer of `platform.aar_events`; new `tpcore/lab/llm_aar/` sub-package; new `ops/llm_aar_critic.py` + SDK + co-task; new dedicated memstore).

**Predecessor PRs:** Task #25 finder (PRs #213 spec, #232 build, #294 Sessions-API wiring); autonomous Lab criteria (#158).

**Discipline:** spec PR (docs-only) → build PR (mirroring PR #294's split for operator visibility).

---

## §1 Outcome target + bright lines

### §1.1 The missing piece

The autonomous loop today reads forward (engine → SP-A → Lab → PAPER) but doesn't read backward (PAPER outcomes → engine improvement hypotheses). The finder reads regime + ledger; it does NOT read AAR events. The credibility scorer reads AARs but only collapses them into a single number. **Pattern recognition over per-engine AAR history is the missing seam.**

The LLM-AAR critic fills that seam. It reads `platform.aar_events` + per-engine P&L histograms + `classify_exit_reason` distributions, identifies behavioural patterns (e.g. "catalyst loses 70% of its losers to time_stop in the 0–3 session band — exit logic might be too patient"), and writes structured `AARFinding` records into a dedicated memstore. The finder cross-reads that memstore and uses the findings as hypothesis-substrate when proposing new candidates.

### §1.2 Bright lines (non-negotiable; advisory-only contract)

The critic is **advisory-only**:

1. **Never mutates engines.** No code edits, no plug changes, no `LAB_TARGET` rewrites.
2. **Never opens PRs.** Only the finder opens PRs (Task #25 §3 — the finder is the sole emission surface).
3. **Never bypasses gates.** The SP-A statistical gate + autonomous Lab criteria (PR #158) + per-engine credibility scorer are sacred and unchanged.
4. **Never bypasses the engine-roster ECR machine path.** `_PROFILE` is hook-blocked; the critic doesn't even try.
5. **Findings are advisory text only.** The finder is the only thing that can emit a `ProposedSpec`. The critic writes `AARFinding` rows; the finder optionally cites them in its `rationale`.
6. **Pattern recognition, not engine redesign.** The critic surfaces *what AARs say*; it doesn't prescribe new engine logic.
7. **No `tools` payload to the Anthropic SDK** (mirrors finder §2.5 fence). The critic emits a structured JSON envelope; the application parses + persists.

### §1.3 Why split from the finder

A separate agent (vs adding AAR aggregation to the finder) is the right cut because:
- **Cadence mismatch.** Finder is event-driven on regime / ledger capacity (intermittent). AAR critic is nightly (cumulative).
- **Token budget mismatch.** The finder's `MarketSnapshot` is already 512 KiB; piling AAR per-engine series on top breaks the budget.
- **Memstore semantics.** The finder's memstore holds prior emissions + curation policy; the critic's memstore holds findings + per-engine pattern history.
- **Independence.** The critic shipping does not gate the finder; both can iterate independently.

---

## §2 Input substrate

### §2.1 Tables read (read-only)

- **`platform.aar_events`** — per-trade postmortems. Read via `tpcore.aar.AARReader.fetch_all_grouped()` (existing reader; no new table). Carries `engine`, `trade_id`, `ticker`, `pnl_net`, `exit_ts`, `entry_ts`, `exit_reason`. The critic also pulls the full `aar_data` jsonb to access `confidence_at_entry`, `regime_tags`, `rule_compliance`, `slippage_bps`, `filter_diagnostics`.
- **`platform.engine_credibility`** (read-only, descriptive) — current credibility score per engine; the critic reads it as context but does not propose changes.
- **`tpcore.engine_profile.lab_targetable_engines()`** — roster filter (RETIRED engines excluded).

### §2.2 Per-engine aggregates the critic constructs

For each engine in the roster, the application assembles a structured payload before the LLM sees anything (deterministic — same single-source-of-truth principle as the finder's `MarketSnapshot`):

```
EnginePerformanceWindow:
  engine: str
  as_of_session: date
  trade_count_total: int
  trade_count_window: int                        # last 90 NYSE sessions
  pnl_net_total_usd: Decimal
  pnl_net_window_usd: Decimal
  win_rate: float                                # over window
  win_rate_total: float
  exit_reason_distribution: dict[ExitReason, int]
  exit_reason_pnl_by_reason: dict[ExitReason, Decimal]
  hold_duration_buckets: dict[Literal["0-1d","1-3d","3-7d","7-21d","21d+"], int]
  pnl_per_hold_bucket: dict[str, Decimal]
  slippage_bps_p50: float | None
  slippage_bps_p95: float | None
  rule_compliance_rate: float
  recent_aars: tuple[AARRowSummary, ...]         # 20 most recent (anonymized — ticker only)
```

The window cap is **90 NYSE sessions** to keep payload bounded (~5KiB per engine; ~6 engines * 5KiB = 30 KiB total). The critic does NOT see raw `trade_id` strings or order/position state — only the aggregate shapes above.

### §2.3 Cap

`MAX_AAR_PAYLOAD_BYTES = 256 KiB` — fail-loud on overflow (same fail-loud principle as `MAX_SNAPSHOT_BYTES` for finder). Empirical sizing is ~30 KiB across 6 engines; the cap is 8x ceiling.

---

## §3 Output contract

### §3.1 `AARFinding` (frozen pydantic v2)

```python
class AARFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str                                 # ∈ roster
    finding_id: str                             # SHA-12 of (engine, theme, observation_window)
    theme: Literal[
        "exit_timing", "entry_quality", "sizing_drift",
        "regime_conditional_perf", "exit_reason_skew",
        "rule_compliance_drift", "hold_duration_skew",
        "slippage_drift", "win_rate_decay",
    ]
    pattern_observed: Annotated[str, Field(max_length=2048)]
    suggested_emission_axis: Annotated[str, Field(max_length=1024)]
    evidence_aar_count: Annotated[int, Field(ge=3)]   # ≥3 AARs supporting the pattern
    evidence_window_sessions: Annotated[int, Field(ge=1, le=90)]
    confidence: Literal["low", "medium", "high"]
    observation_session: date
    persona_version: str
```

**Field semantics:**
- `theme` — closed vocabulary (Literal); the LLM picks one. Themes are pattern-class buckets the finder cares about.
- `pattern_observed` — the LLM's prose description of what it sees in the data.
- `suggested_emission_axis` — what the finder might test (NOT a full hypothesis — that's the finder's job). E.g. "test 1-day hold variant for catalyst", "test post-earnings-day-1 vs day-0 entry for catalyst", "regime-condition vector composite on vol_regime=stress".
- `evidence_aar_count` — minimum 3 AARs supporting the pattern (anti-overfit cap; pattern claims must rest on >2 trades).
- `confidence` — `low` for `evidence_aar_count ∈ [3,7]`; `medium` for `[8,20]`; `high` for `>20`. Mechanical mapping; LLM picks but we cross-validate at write time.

### §3.2 `AARCriticRun` (provenance — written to `application_log`)

```python
class AARCriticRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    started_ts: datetime
    completed_ts: datetime
    trigger: Literal["nightly_cron", "operator_command"]
    as_of_session: date
    engines_examined: tuple[str, ...]
    findings_emitted: tuple[str, ...]            # finding_ids
    persona_version: str
    rejection_reason: str | None
```

Provenance row per run; analogous to `LAB_FINDER_RUN`.

### §3.3 What the LLM does NOT emit

- No `ProposedSpec`. (That's the finder's job.)
- No engine code changes.
- No `LAB_TARGET` arms.
- No credibility-score changes.
- No retirement recommendations.

The critic's only output is a tuple of `AARFinding`s + the provenance row.

---

## §4 Memory store

### §4.1 Provisioning

**Name:** `aar-llm-critic-context`
**Description:** *"Post-trade pattern recognition for the autonomous engine-improvement loop. Reads platform.aar_events; writes AARFinding records into /findings/<engine>/<finding_id>.md. Cross-read by the finder memstore."*
**Mount path:** `/mnt/memory/aar-llm-critic/`

### §4.2 Namespace layout

```
/agent-context/
    curation-policy.md         # caps + write discipline (operator-staged)
/findings/<engine>/<finding_id>.md
                               # one file per AARFinding emitted
/recent-runs/<run_id>.md       # last N=20 run summaries (LRU eviction)
/lessons/                      # operator-staged hand-curated notes
                               # (e.g. "catalyst's time_stop band is operator-tuned at 5 sessions")
```

**Caps:**
- `/findings/<engine>/` — max **30 files per engine** (LRU evict on next run; the most recent observation wins).
- `/recent-runs/` — max 20 files (LRU evict).
- `/lessons/` — operator-managed; agent reads, NEVER writes.

### §4.3 Cross-memstore read (finder → critic)

**Decision: COPY findings into the finder memstore.** The simpler option of the two operator-suggested choices.

When a finding is written to `aar-llm-critic-context/findings/<engine>/<finding_id>.md`, the application ALSO writes a copy to `memstore_01MzLun3AfRf2viPmDqJvsWi/aar-findings/<engine>/<finding_id>.md` (the existing finder memstore). The finder's persona §11 directs it to read `/aar-findings/<target_engine>/` at startup alongside `/prior-emissions/`.

Why copy vs. cross-attach: Sessions API does support attaching multiple memstores (per `BetaManagedAgentsMemoryStoreResourceParam`), but multi-memstore attach increases prompt-tokens (every attached store renders into the system prompt). Per-engine findings are small (~1KiB each, max 30 per engine = 30KiB per engine the finder targets). Curated copy is cheaper than full-cross-attach.

Curation: when finder runs target `engine=X`, it reads only `/aar-findings/X/` (not all engines). The persona §11 will be amended in a separate finder PR to add this directive (out of scope for the critic PR itself — the finder works fine without; the cross-read is the polish, not the prerequisite).

### §4.4 Curation policy

`/agent-context/curation-policy.md` (operator-staged at memstore creation) documents:

- Per-namespace caps (above).
- Write discipline: every finding MUST be evidence-grounded (`evidence_aar_count ≥ 3`).
- No deletion by the LLM — only operator-staged hand-curation.
- LRU eviction is application-managed (the LLM does NOT manage its own caps; same discipline as finder).

---

## §5 Cadence

### §5.1 Default: nightly via launchd

**Trigger:** post-engine-close (after all engines have logged their AARs for the session). Concretely: launchd schedule at **23:00 local Manila = 15:00 UTC** (well after NYSE close at 21:00 UTC even on the latest possible early-close days).

Co-task lives in `ops/llm_aar_critic.py`; daemon hosting is the same pattern as `ops/llm_edge_finder.py` (event-driven dispatch by the triage co-task surface).

### §5.2 Alternative: operator-command

`python -m ops.llm_aar_critic --since <date>` runs on demand. Useful for backfill / pilot / ad-hoc operator inspection. The same code path as the cron trigger; just different `trigger` value in provenance.

### §5.3 Event-triggered (deferred to v1.5)

Operator's directive listed event-triggered AAR-threshold-breach as an option. v1 stays nightly; v1.5 may add a `AAR_THRESHOLD_BREACH` event class fired by `ops/data_repair_service.py` post-close hook when an engine's last-N-AAR pattern crosses a heuristic floor (e.g. 5+ consecutive losers, or P&L draw > 3σ). Out of scope for v1.

### §5.4 Rate ceiling

`MAX_AAR_CRITIC_RUNS_PER_DAY = 2` — defense against runaway invocation. Counted on the `LAB_AAR_CRITIC_RUN` rows in `application_log`.

---

## §6 Fence

### §6.1 The fence stack (mirrors finder §5 in shape, not load)

| # | Fence | Mechanism |
|---|---|---|
| 1 | Advisory-only | No `tools` payload to Anthropic SDK; structured JSON envelope only |
| 2 | No engine mutation | No `_PROFILE` write, no engine file write — diff-scope test asserts `tpcore/lab/llm_aar/`, `ops/llm_aar_*`, `docs/llm_aar_persona.md`, and `tests/` only |
| 3 | No ECR emission | Critic does NOT call `python -m ops.engine_sdlc --ecr` |
| 4 | Findings ground-truthed | `AARFinding.evidence_aar_count ≥ 3` (pydantic-enforced) |
| 5 | Read-only AAR access | `AARReader` is the only DB read path; no writes back to `aar_events` |
| 6 | Bounded payload | `MAX_AAR_PAYLOAD_BYTES = 256 KiB`; fail-loud on overflow |
| 7 | Persona SHA-pinned | `PERSONA_SHA256` in `tpcore/lab/llm_aar/__init__.py`; CI sentinel reds drift |
| 8 | Run-rate ceiling | `MAX_AAR_CRITIC_RUNS_PER_DAY = 2` enforced on `application_log` reads |
| 9 | Credential-starved | No `ALPACA_*` in env; no `tools` payload; same as finder |
| 10 | Persona internalises bright lines | Persona §1 explicitly forbids prescriptive engine-redesign output |
| 11 | Closed-vocabulary theme | `theme` field is a `Literal[...]` — LLM cannot invent new theme classes |
| 12 | Mechanical confidence mapping | `confidence` cross-validated against `evidence_aar_count` bands at write time |

The critic is structurally smaller-blast-radius than the finder: it cannot open PRs, cannot mutate engines, cannot trigger ECRs, cannot impact the gate. The worst-case failure mode is a wasted nightly cron + a stale finding nobody reads. There is no autonomous-merge or auto-retire surface — those are finder-side.

---

## §7 Integration with the finder

### §7.1 v1 integration: curated copy

As specified in §4.3 — application copies each `AARFinding` to `<finder_memstore>/aar-findings/<engine>/<finding_id>.md` at write time.

### §7.2 Finder persona amendment (separate PR, NOT in this build)

The finder's persona §11 currently directs reads of `/agent-context/`, `/cross-agent/dev-to-finder/`, `/prior-emissions/`, `/outcomes/`, `/lessons/` at startup. A future finder-side PR adds:

> *"Also read `/aar-findings/<target_engine>/*.md` if your target engine is fixed (operator-command + ledger-capacity triggers only — regime-change triggers do not yet target a specific engine before the LLM picks one). These are LLM-AAR critic findings from cumulative post-trade pattern recognition. Each finding carries a `suggested_emission_axis` — treat as one of many hypothesis seeds, never as the load-bearing evidence (your tool-result citations still bind)."*

The finder works without this amendment (the cross-read is additive). The critic ships first; the finder amendment is the polish step.

### §7.3 What the finder does with findings

The finder's persona will direct it to:
- Read findings at startup (turn 1, before snapshot analysis).
- Cite findings in `ProposedSpec.rationale` when relevant.
- NEVER take a finding as load-bearing — tool-call evidence still required per the SP-A gate fence.

Findings are *seeds*, not specs. The finder still does the analysis work; the critic just surfaces what AARs say.

---

## §8 Persona

### §8.1 File

`docs/llm_aar_persona.md` (v1.0). SHA-pinned via `PERSONA_SHA256` constant in `tpcore/lab/llm_aar/__init__.py`. CI sentinel: `tests/test_aar_critic_persona_versioned.py`.

### §8.2 Six mandatory sections (mirrors finder persona shape)

1. **Identity + outcome.** *"You are the post-trade pattern recognition agent. Your job is to read AAR aggregates + identify behavioural patterns the finder can test as hypothesis seeds. NOT to redesign engines. NOT to propose specs. NOT to mutate roster. Advisory text only."*
2. **AAR substrate framing.** Explains the `EnginePerformanceWindow` shape + what each field means. Especially the `exit_reason_distribution` semantics (e.g. `time_stop` ≠ failure, it's a hold-budget breach; `thesis_broken` is engine self-cancellation; `stop_loss` is risk-side enforcement).
3. **Theme vocabulary.** Walks through the 9 themes + when each fits. E.g. `exit_timing` is when `exit_reason_pnl_by_reason[TIME_STOP] < 0` and `hold_duration_buckets["3-7d"]` dominates — the engine is closing winners early or losers too late.
4. **Evidence discipline.** `evidence_aar_count ≥ 3`, prefer ≥ 8 for `medium`, ≥ 20 for `high`. Never claim a pattern from < 3 supporting AARs.
5. **Suggested emission axis discipline.** Specific + actionable + testable. E.g. NOT *"catalyst should hold longer"* (vague); YES *"catalyst's TIME_STOP exits in the 5-7d band have positive median P&L; finder could test a 10-session variant"* (specific).
6. **What you don't do.** Never prescribe engine code changes. Never propose `LAB_TARGET` arms. Never claim a finding is causal — they're pattern observations.

### §8.3 Bump rule

Persona edits bump `PERSONA_VERSION` + `PERSONA_SHA256` together. CI sentinel reds drift.

---

## §9 Tests

### §9.1 Unit tests (no DB; mocks)

`tests/test_llm_aar_critic.py`:
- `test_aar_finding_frozen` — pydantic `extra="forbid"` + `frozen=True`.
- `test_aar_finding_confidence_band_validation` — `confidence='high'` with `evidence_aar_count=5` REJECTED at construction.
- `test_aar_finding_finding_id_deterministic` — same (engine, theme, observation_window) → same finding_id.
- `test_engine_performance_window_assembler` — fixture AARs → expected aggregate shape.
- `test_critic_payload_bounded` — synthesised 6-engine payload < `MAX_AAR_PAYLOAD_BYTES`.
- `test_run_aar_critic_smoke_mode` — no `llm_callable` → empty findings + provenance row written.

`tests/test_llm_aar_critic_sdk.py` (mirrors `test_llm_edge_finder_sdk.py`):
- `test_make_sdk_aar_callable_returns_async_callable`
- `test_sdk_aar_callable_returns_json_envelope`
- `test_sdk_aar_authskip_on_auth_error`
- `test_sdk_aar_json_decode_failure_returns_synthetic_request` — malformed LLM output → synthetic response, no crash
- `test_sdk_aar_no_tools_param` — advisory-only contract
- `test_sdk_aar_temperature_zero`
- `test_sdk_aar_persona_alignment_warning` — sha mismatch warns, doesn't raise (mirrors finder)

`tpcore/lab/llm_aar/tests/`:
- `test_models_frozen.py` — all 6 models frozen + extra-forbid.
- `test_persona_versioned.py` — `PERSONA_SHA256` matches `docs/llm_aar_persona.md` sha.
- `test_run_writer.py` — provenance writer schema.
- `test_payload_assembler.py` — `assemble_aar_payload(pool, as_of_session)` against fixture AARs.

### §9.2 Integration test

`ops/tests/test_llm_aar_critic_to_memstore.py`:
- Fake `AsyncAnthropic` + fake `AARReader` + fake memstore-create.
- End-to-end: `run_aar_critic` reads fake AARs → invokes fake LLM → writes 2 findings to fake memstore → verifies `LAB_AAR_CRITIC_RUN` provenance row + 2 `LAB_AAR_CRITIC_FINDING` rows in `application_log`.

### §9.3 Whole-suite + order-flip

Heavy-lane discipline: whole-suite parallel-accelerator + single-process + reverse-order all green before push.

---

## §10 Cost model

### §10.1 Per-run token estimate

- **Input:** persona ~3 KiB + curation-policy ~1 KiB + `/agent-context/`+`/lessons/` memstore reads ~5 KiB + per-engine AAR payload ~30 KiB = **~40 KiB ≈ 10k input tokens per run**.
- **Output:** ~6 engines × ~1 finding × ~500 tokens = **~3k output tokens per run** (most engines produce 0–2 findings; some produce more on volatile days).
- **Cache:** persona + curation-policy + lessons cached (~9 KiB) via Sessions API memstore mount → ~75% of input tokens hit cache after turn 1 of run 1.

### §10.2 Daily cost

At **2 runs/day** × `claude-sonnet-4-6` pricing (Sonnet input $3/Mtok, output $15/Mtok):
- Input: 2 × 10k = 20k tokens/day @ $3/Mtok = $0.06/day
- Output: 2 × 3k = 6k tokens/day @ $15/Mtok = $0.09/day
- **Total: ~$0.15/day ≈ $5/month** at nominal load.

Pilot run on 30 days of catalyst AARs (~24 trades) should cost ~$0.50 cumulative (~10x a nominal nightly run because pilot reads back-history). Operator visibility: under $1 for the pilot, ~$5/month at steady state.

### §10.3 Compared to the finder

Finder: ~$0.51 per pilot run (PR #294), ~$0.10–0.50 per steady-state run. Critic is structurally cheaper because (a) payload is bounded by AAR aggregate shape not snapshot complexity, (b) single-turn (no multi-turn tool dispatch loop — the critic doesn't run statsmodels), (c) output is small (findings, not full specs).

---

## §11 Package layout

```
tpcore/lab/llm_aar/
    __init__.py                  # constants + PERSONA_SHA256 + model re-exports
    models.py                    # AARFinding, AARCriticRun, EnginePerformanceWindow,
                                 # AARRowSummary, AARCriticRequest, AARCriticResponse
    payload_assembler.py         # assemble_aar_payload(pool, as_of_session) -> tuple[EnginePerformanceWindow, ...]
    persona.py                   # persona_text() reader
    run_writer.py                # record_aar_critic_run + record_aar_finding (application_log writes)
    memstore_writer.py           # archive_finding_to_memstore + copy_to_finder_memstore
    tests/
        __init__.py
        test_models_frozen.py
        test_persona_versioned.py
        test_payload_assembler.py
        test_run_writer.py

ops/llm_aar_critic.py             # run_aar_critic(pool, ...) — main loop, mirrors run_finder shape
ops/llm_aar_critic_sdk.py         # Anthropic Sessions API wiring (mirrors llm_edge_finder_sdk.py)
ops/llm_aar_anthropic_ids.py      # AAR_CRITIC_AGENT_ID, AAR_CRITIC_ENVIRONMENT_ID, AAR_CRITIC_MEMSTORE_ID
ops/tests/
    test_llm_aar_critic.py
    test_llm_aar_critic_sdk.py
    test_llm_aar_critic_to_memstore.py

scripts/anthropic_aar_critic_provision.py     # mirrors anthropic_agent_provision.py
scripts/tests/test_anthropic_aar_critic_provision.py

docs/llm_aar_persona.md           # v1.0
```

---

## §12 Build order

Sequenced for incremental landing:

1. **T1 — Spec PR (this doc).** Lands first, docs-only PR for operator visibility.
2. **T2 — Models + payload assembler.** `tpcore/lab/llm_aar/{models.py, payload_assembler.py}` + unit tests.
3. **T3 — Persona.** `docs/llm_aar_persona.md` + `persona.py` reader + sha-pin test.
4. **T4 — Provenance writer.** `run_writer.py` + tests.
5. **T5 — Memstore + provisioning.** Provision the memstore via curl (operator-supplied API key); seed `/agent-context/curation-policy.md`; provision the agent + environment via `scripts/anthropic_aar_critic_provision.py`; capture IDs in `ops/llm_aar_anthropic_ids.py`.
6. **T6 — SDK.** `ops/llm_aar_critic_sdk.py` (mirrors PR #294's Sessions API pattern).
7. **T7 — Main loop.** `ops/llm_aar_critic.py` — orchestrates payload → SDK → findings → memstore writes.
8. **T8 — CLI.** `python -m ops.llm_aar_critic --since <date>`.
9. **T9 — Live pilot.** Run on catalyst's last-30-days AARs. Verify: (a) reads from `platform.aar_events`, (b) writes findings to memstore, (c) operator-readable finding surfaced.
10. **T10 — Build PR.** Bundle T2–T9 in a single feature PR (per `feedback_push_when_tangible_batch_prs.md`).

Co-task daemon registration (mirroring finder's slot in `ops/llm_triage_service.py`) is deferred to follow-up — v1 ships with operator-command + cron invocation; daemon hosting is the polish.

---

## §13 What's deferred (NOT in v1)

- **Event-triggered runs** on AAR-threshold breach (v1.5; §5.3).
- **Co-task daemon registration** in `ops/llm_triage_service.py` (follow-up; v1 = cron + CLI).
- **Finder persona §11 amendment** for `/aar-findings/<engine>/` reads (follow-up finder PR).
- **Cross-memstore attach** (vs curated copy) — decision in §4.3 is to ship copy; attach is v2 if findings volume changes the math.
- **Outcome marker reads** — the critic could read `LAB_FINDER_OUTCOME_VERDICT` events to identify which finder-emitted engines the operator marked `success`/`failure`. Defer to v2; v1 is engine-level AAR aggregates only.

---

## §14 Source URLs (authoritative — operator standing rule)

- **Anthropic Managed Agents API (POST /v1/memory_stores):** `https://platform.claude.com/docs/en/api/cli/beta/memory_stores`
- **Memory Store attach (`BetaManagedAgentsMemoryStoreResourceParam`):** `https://platform.claude.com/docs/en/api/beta`
- **Memory create (POST /v1/memory_stores/{id}/memories):** `https://platform.claude.com/docs/en/api/java/beta/memory_stores/memories/create`
- **Beta header (`managed-agents-2026-04-01`):** `https://platform.claude.com/docs/en/api/beta`
- **Sessions API (used via PR #294's wiring; same pattern):** `https://platform.claude.com/docs/en/api/cli/beta/sessions`
- **Anthropic SDK retry semantics + 529 handling:** internal `feedback_anthropic_529_self_heal.md` + status.claude.com incident posts.

All API contract decisions in this spec read CURRENT (2026-05-22) Anthropic docs via context7 MCP; training-data knowledge alone was not used per operator standing rule (`feedback_use_official_docs.md`).
