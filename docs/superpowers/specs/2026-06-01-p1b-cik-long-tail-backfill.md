# P1b — CIK long-tail FMP `/profile` fallback backfill (heavy-lane spec)

> **Phase: SPEC ONLY.** This document is the only artifact created or modified by this PR. No code change, no migration, no live FMP calls, no live DB writes, no trading/risk/runtime behavior change. Implementation is deferred to a separate plan PR (see §"Implementation plan outline").

## Verdict

Add a second resolution leg to the existing `backfill_sec_metadata` operator-on-demand stage so the **`CIKResolveResult.unresolved`** list returned by `SECTickerCIKMap` is then offered to the existing **FMP `/stable/profile`** adapter as a lower-authority fallback. **Persist newly-resolved CIKs with `cik_source = 'fmp'`** (the schema's CHECK constraint at `platform/migrations/versions/20260530_0200_issuer_metadata_foundation.py` already permits this value — **no migration is required**). The SEC ticker map remains highest authority; existing non-NULL CIKs are never overwritten; mismatched-symbol / ambiguous-issuer / non-equity-product rows fail closed.

This unblocks the structural coverage gate at `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py` (`metadata_coverage_low`, currently at 90% NULL doctype — the dispositive structural block on `DATA_OPERATIONS_COMPLETE`) by feeding more rows into the existing metadata-extraction leg.

## Problem statement

The first-pass CIK backfill at `scripts/ops.py::_stage_backfill_sec_metadata` resolves tickers via `tpcore/sec/ticker_cik_map.py::SECTickerCIKMap` (one fetch of SEC's public `https://www.sec.gov/files/company_tickers.json` — ~1.5 MB JSON). That map only carries the issuer's base equity ticker for SEC-registered domestic common stock. Per the `tpcore/sec/ticker_cik_map.py` module docstring:

> [unresolved are] tickers the SEC map does NOT cover (likely non-equity instruments, recently-delisted, or pink-sheet/OTC tickers SEC doesn't index in this public file).

TODO.md `## ⚑ Evidence-based fundamentals/lifecycle arc` quantifies the gap: **~1,419** SEC-ticker-map-unresolved tickers sit indefinitely as `excluded_metadata_required` in the cadence-routed validator because the metadata-extraction leg (SEC `/submissions/CIK<cik>.json`) requires a CIK first.

The first-pass stage at `scripts/ops.py:2850` logs `cik_stats["unresolved"]` for visibility, but does not retry them through any fallback. They never move out of `cik IS NULL`. The coverage gate at `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py:54-61` blocks `DATA_OPERATIONS_COMPLETE` whenever the metadata-required ratio exceeds the 25% threshold — currently ~90% — so this long-tail directly stalls the "100% data or don't trade" mandate.

## Current evidence from repository

### Files inspected (read-only)

- `tpcore/sec/ticker_cik_map.py` (200 lines) — async client; `SECTickerCIKMap.resolve_missing_ciks(tickers, existing_ciks)` → `CIKResolveResult(resolved, unresolved, skipped_already_set)`. Docstring explicitly states: "NEVER overwrites a non-NULL CIK. SEC ticker reuse means a ticker string can map to different CIKs over time".
- `scripts/ops.py::_stage_backfill_sec_metadata` (line 2604) — operator-on-demand stage. Scope params: `dry_run` (default **True**), `tickers`, `failing_only`, `no_cik_country_null`, `max_tickers`, `force_refresh_metadata`. Two legs (`do_cik`, `do_metadata`); CIK leg today only consults `SECTickerCIKMap`.
- `scripts/ops.py::_stage_fmp_profile_backfill` (line 4194) — separate existing operator-on-demand stage. Already calls `https://financialmodelingprep.com/stable/profile`. Currently populates `gics_sector`, `country`, `current_legal_name`. **Does NOT populate `cik`** today.
- `scripts/ops.py::_backfill_tkr14_via_fmp_profile` (line 6955, SLICE 2 of `tkr14_backfill`) — **does** already fill CIK from `profile.get("cik")` for rows in the v2.2 Phase P5 backfill scope. This is the proof point that `https://financialmodelingprep.com/stable/profile` returns the `cik` field directly.
- `tpcore/identity/parent_resolver.py` (head) — documents the canonical authority order (per spec v2.2 §1.10): "INSIDER / MATERIAL_EVENTS (data source = SEC EDGAR; CIK in hand): 1. SEC company_tickers.json reverse-lookup … 2. FMP `/profile` fallback for foreign-issuer CIKs SEC doesn't carry. 3. FMP `/profile` enrichment for country/asset_class/exchange. 4. OpenFIGI `/v3/mapping` …". And: "NEVER overwrite an existing non-null `figi`/`cusip`/`isin`/`cik`. Divergence … writes an `IDENTITY_DIVERGENCE_INVESTIGATE` event to `application_log` for operator review — NEVER silent update."
- `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py` — `METADATA_COVERAGE_FAIL_THRESHOLD = 0.25`; the `<metadata_coverage>` synthetic-ticker FailureDetail fires when `excluded_metadata_required / metadata_denom > 0.25`. The class docstring (lines 53–61) calls this out as the dispositive structural block on `DATA_OPERATIONS_COMPLETE`.
- `platform/migrations/versions/20260530_0200_issuer_metadata_foundation.py:44-52` — `cik_source` CHECK constraint already accepts `'sec_ticker_map' | 'fmp' | 'manual' | NULL`. **The schema already supports FMP-provenance CIK rows; this spec requires NO migration.**

### Key code locations the implementation will touch

- `scripts/ops.py::_stage_backfill_sec_metadata` (lines 2604–3000-ish) — add a `do_fmp_fallback` flag + second CIK-resolution sub-leg that consumes the `CIKResolveResult.unresolved` list.
- `tpcore/fmp/` — **NEEDS_REPO_VERIFICATION:** no dedicated `/profile` adapter module exists today (`grep` returned `fundamentals_adapter.py` only). The existing FMP `/profile` callers (`_stage_fmp_profile_backfill`, `_backfill_tkr14_via_fmp_profile`) both inline-call `httpx` against `https://financialmodelingprep.com/stable/profile`. The implementation plan will decide whether to extract a `tpcore/fmp/profile_adapter.py` thin wrapper (preferred per the data-adapter rule's "no forked scripts" guidance) or inline-call like the existing two stages.

## Current data flow

1. Operator runs `python -m scripts.ops backfill_sec_metadata --param dry_run=false --param do_cik=true --param do_metadata=true --param no_cik_country_null=true`.
2. Stage selects ~1,630 rows (`cik IS NULL AND country IS NULL AND lifetime_end IS NULL`).
3. CIK leg: `SECTickerCIKMap.resolve_missing_ciks` returns `resolved={…}`, `unresolved=[~1,419 tickers]`, `skipped_already_set=[]`.
4. Metadata leg: for each ticker with a CIK (existing or newly resolved by this run), fetch `data.sec.gov/submissions/CIK<cik>.json` and extract `sec_document_type_primary`, `fiscal_year_end_month`, `first_public_filing_date`, `last_filing_date`.
5. Two `UPDATE`s — one for CIK columns (with `cik_source = 'sec_ticker_map'`), one for SEC evidence columns (with `metadata_source = 'sec_submissions'`).
6. The ~1,419 unresolved tickers stay `cik IS NULL` → never enter the metadata leg → forever `excluded_metadata_required` → 90% NULL doctype → coverage gate blocks `DATA_OPERATIONS_COMPLETE`.

## Target behavior

After P1b ships, the operator can run the same stage with a new `--param do_fmp_fallback=true` flag. The stage proceeds through legs 1–5 as above and then performs:

7. **FMP fallback sub-leg**: for each ticker in `CIKResolveResult.unresolved`, call FMP `/stable/profile?symbol=<ticker>&apikey=<FMP_API_KEY>`. Per the existing call sites, the response is a JSON array; a non-empty result with a `cik` field is a candidate match.
8. **Validation gates** (fail-closed; described in §"Failure modes"): symbol must match (case-insensitive); CIK must be a 10-or-fewer-digit numeric string; FMP-claimed `country` must be non-empty (per `parent_resolver.py` lane semantics for the foreign-issuer carve-out).
9. **Persist**: `UPDATE platform.ticker_classifications SET cik = <fmp_cik_padded>, cik_source = 'fmp' WHERE ticker = <ticker> AND cik IS NULL` (the `cik IS NULL` guard is the existing-non-NULL-never-overwritten enforcement). No other columns written by this sub-leg — country / legal_name / sector enrichment is a separate concern handled by the existing `fmp_profile_backfill` stage.
10. **Re-enter metadata leg** for the now-resolved tickers (the existing leg already supports this via `cik_resolutions_this_run`, lines 2879–2890). The full `data.sec.gov/submissions/CIK<cik>.json` extraction runs against the FMP-derived CIKs same as for SEC-map-derived ones.

End-state expected: a meaningful fraction (NEEDS_REPO_VERIFICATION — depends on how many of the 1,419 are foreign issuers SEC index files don't carry vs. genuine non-equity / pink-sheet) move to `cik IS NOT NULL AND cik_source='fmp'` → metadata leg populates their `sec_document_type_primary` → `excluded_metadata_required` shrinks → coverage gate's 25% threshold becomes reachable.

## Non-goals

- **No change to capital gate / RiskGovernor / trading logic.** `tpcore/risk/**` is not touched in P1b.
- **No change to validator semantics.** The cadence-routed validator already reads `sec_document_type_primary`; P1b just makes that column populated for more rows. The validator code is byte-frozen via SHA pin in `_stage_backfill_sec_metadata` (per the P0 docstring) and stays that way.
- **No change to the SEC ticker map's authority.** SEC remains highest. FMP is only consulted when SEC explicitly returned `unresolved` for a ticker.
- **No country / sector / legal-name backfill in this stage.** Those are the responsibility of the existing `fmp_profile_backfill` stage. P1b is CIK-only.
- **No OpenFIGI fallback** in this PR. The `parent_resolver.py` v2.2 lane semantics call for OpenFIGI as a further step; P1b stays scoped to the SEC-first → FMP-fallback two-tier per the TODO entry.
- **No automatic scheduling.** Operator-on-demand only; no `run_data_operations.sh` integration, no launchd timer. Operator decides when to run.
- **No retry of `skipped_already_set` rows.** Existing non-NULL CIKs are sacred (operator-provenance preservation per `ticker_cik_map.py:18-20`).

## Proposed data sources

Single new data source: **FMP `/stable/profile?symbol=<ticker>`** (already used by the two existing FMP /profile callers in `scripts/ops.py`, no new dependency).

Existing rate limits (per the existing callers' comments):
- `_stage_fmp_profile_backfill`: 750/min Starter ceiling, 0.1 s/call sleep, configurable concurrency.
- `_backfill_tkr14_via_fmp_profile`: 300/min in the comment / 0.2 s sleep (≈5 req/s) — more conservative.
- P1b should adopt the **more conservative** posture (0.2 s sleep, concurrency 1) because the long-tail tickers are individually higher-risk (foreign issuers, pink sheet) and a slower steady scan is acceptable for an operator-on-demand workflow.

API key: `FMP_API_KEY` env var (existing pattern; missing → `RuntimeError`).

## FMP `/profile` fallback semantics

The FMP `/stable/profile` response (per call sites at lines 4294, 7053, 7060, 7062) is an array of profile dicts. The relevant fields are:

| Field | Used for | P1b treatment |
|---|---|---|
| `symbol` | Validate the response matches the requested ticker | **Hard guard** — case-insensitive equality; fail closed on mismatch (see §"Failure modes") |
| `cik` | The candidate CIK string | Coerce to `str`; left-pad to 10 digits to match the SEC ticker-map convention (per `ticker_cik_map.py:60-61`); reject if non-numeric / empty |
| `country` | Authority signal (NEEDS_OPERATOR_DECISION whether to also write it — see §"Open operator decisions") | Read but **not written** by P1b's sub-leg |
| `companyName`, `cusip`, `isin`, `sector` | Not P1b's concern | Not read by P1b |

Output of one FMP call per unresolved ticker:

```
{
  "ticker": str,             # the requested ticker (the input, not FMP's echo)
  "resolution": "resolved" | "no_match" | "symbol_mismatch" | "no_cik_in_profile" | "fmp_error",
  "fmp_cik": str | None,     # only set when resolution == "resolved"
  "fmp_country": str | None  # diagnostic; not persisted by this sub-leg
}
```

## Resolution states

Each ticker entering the FMP fallback sub-leg ends in exactly one terminal state:

| Code | Meaning | Persistence side effect |
|---|---|---|
| `resolved` | FMP returned a profile whose `symbol` matches and `cik` is a valid numeric string; row had `cik IS NULL` | `UPDATE … SET cik = $cik, cik_source = 'fmp' WHERE ticker = $t AND cik IS NULL` |
| `no_match` | FMP returned `[]` or HTTP 404 | None; logged. Counter incremented. |
| `symbol_mismatch` | FMP returned a profile but `symbol != requested_ticker` | None; logged + `IDENTITY_DIVERGENCE_INVESTIGATE` event written to `application_log` (per `parent_resolver.py` divergence protocol) for operator review. Counter incremented. |
| `no_cik_in_profile` | FMP returned a profile but the `cik` field is missing or empty | None; logged. Counter incremented. |
| `fmp_error` | HTTP non-200 (after the existing retry-on-429 pattern) or JSON-decode failure | None; logged at WARN. Counter incremented. Stage does NOT exit on individual ticker errors. |
| `skipped_existing_cik` | Defensive guard — row's CIK was concurrently populated by another process between scope read and write | None. Counter incremented. |
| `skipped_lifetime_ended` | Row's `lifetime_end IS NOT NULL` between scope read and write | None. Counter incremented. |

## Operator-on-demand stage shape

**Preferred approach (consistent with the existing P0-003 pattern):** extend `_stage_backfill_sec_metadata` in place with a new `do_fmp_fallback` parameter. **Rationale**: the existing stage already wires together CIK resolution + metadata extraction in a single transaction-scoped pass; adding a fallback CIK sub-leg keeps the "single stage, two legs, atomic visibility into newly-resolved rows" contract from the P0 docstring. Splitting into a separate stage would duplicate the scope-selection SQL and require an extra operator invocation to push newly-resolved tickers through the metadata leg.

New / changed `cfg` params:

| Param | Type | Default | Behavior |
|---|---|---|---|
| `do_fmp_fallback` | bool | **false** | When true, run the FMP fallback sub-leg against `CIKResolveResult.unresolved`. Default false because the existing operator-on-demand muscle memory invokes the stage without this flag; existing behavior unchanged for non-opting callers. |
| `fmp_rate_limit_sleep_s` | float | 0.2 | Per-call sleep. Tunable for dry-run smoke vs production. |
| `fmp_max_unresolved` | int (optional) | none | Cap on number of unresolved tickers processed in one run. Enables incremental rollout (e.g. start with 100, validate, expand). |

`dry_run=true` remains the default. In dry-run mode, the sub-leg performs the FMP calls and prints the would-resolve count but emits no DB writes. (Alternatively NEEDS_OPERATOR_DECISION: also skip the FMP API calls themselves in dry-run to avoid spending quota on dry validations — see §"Open operator decisions".)

## Persistence model

**No new table; no new column; no migration.** The fields written are already present:

- `platform.ticker_classifications.cik` (existing column).
- `platform.ticker_classifications.cik_source` (added by `20260530_0200_issuer_metadata_foundation.py`; CHECK constraint already permits `'fmp'`).

UPDATE SQL pattern (the `cik IS NULL` guard is the existing-non-NULL-never-overwritten enforcement):

```sql
UPDATE platform.ticker_classifications
SET cik = $2, cik_source = 'fmp'
WHERE ticker = $1
  AND cik IS NULL
  AND lifetime_end IS NULL
```

A row count of `0` from this UPDATE in the steady state means the row's CIK was concurrently populated by another process; the sub-leg logs and increments `skipped_existing_cik`. The stage does NOT retry.

**Telemetry:** existing `structlog` event names match the P0 stage's convention:
- `ops.stage.backfill_sec_metadata.fmp_fallback.start` — at the head of the sub-leg, with `unresolved_count`, `fmp_rate_limit_sleep_s`, `dry_run`.
- `ops.stage.backfill_sec_metadata.fmp_fallback.progress` — every N tickers.
- `ops.stage.backfill_sec_metadata.fmp_fallback.symbol_mismatch` — once per mismatched response.
- `ops.stage.backfill_sec_metadata.fmp_fallback.writes_committed` — at sub-leg end.
- `application_log` event `IDENTITY_DIVERGENCE_INVESTIGATE` on symbol mismatch (per `parent_resolver.py` lane protocol).

## Idempotency and dry-run behavior

- **Idempotent**: re-running the stage on the same scope is a no-op for already-resolved rows. The scope-selection SQL itself filters to `cik IS NULL`; rows that gained a non-NULL CIK in a prior run are not re-fetched.
- **Resumable**: a mid-run interruption (operator Ctrl-C; SEC fair-use sleep timeout) leaves committed rows committed; the next run picks up the remaining unresolved set. This requires periodic flushing (existing `BATCH = 500` pattern in `_backfill_tkr14_via_fmp_profile`).
- **`dry_run=true` (default)**: prints the to-be-resolved count and a small sample (existing P0 pattern of `n_filled + sample_preview`). NEEDS_OPERATOR_DECISION (§"Open operator decisions"): whether dry-run also skips the FMP API calls themselves.
- **`force_refresh_metadata=true`**: does NOT cause re-resolution of already-CIK'd rows (that would violate the "never overwrite" rule). It only forces the metadata leg to re-fetch `data.sec.gov/submissions/CIK<cik>.json` for existing CIKs.

## Coverage-gate interaction

Today: `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py` emits `<metadata_coverage>` synthetic-ticker FailureDetail when `excluded_metadata_required / metadata_denom > 0.25`. Current state: ~90%, blocking `DATA_OPERATIONS_COMPLETE`.

After P1b's FMP fallback ships and the operator runs it:

1. The ~1,419 unresolved tickers split into the resolution states above (`resolved`, `no_match`, `symbol_mismatch`, `no_cik_in_profile`, `fmp_error`).
2. Only the `resolved` rows gain a CIK; the metadata leg then populates their `sec_document_type_primary`.
3. The `<metadata_coverage>` ratio drops by the count of newly-populated rows divided by the active universe.
4. **NEEDS_OPERATOR_VERIFICATION**: whether the FMP `/profile` coverage is sufficient to push the ratio under 25%. The repo evidence cannot answer this without running the live backfill; the spec sizes the maximum upside as 1,419 / 13,840 ≈ 10.3% of the active universe (which would only move the needle if a large fraction of the current 90% NULL count overlaps with the FMP-resolvable subset).
5. If P1b alone is insufficient, the next escalation per the TODO entry is operator-on-demand `backfill_sec_metadata` against the full active universe (the documented ~14-minute cold / ~30-second cached run) — that's a separate "metadata coverage backfill" follow-up item in the lifecycle arc's deferred subsection, not part of P1b's scope.

P1b's success criterion is **structural completeness, not threshold-clearing**: every previously-unresolved ticker reaches a terminal state and is either resolved with provenance or recorded as an honest dead end. The coverage-gate threshold is a separate question of universe composition.

## Failure modes

1. **`FMP_API_KEY` missing** → `RuntimeError` at sub-leg start (existing pattern; the `_stage_fmp_profile_backfill` precedent does this at the stage head before any DB work).
2. **FMP rate-limit hit** (HTTP 429) → existing retry-on-429 pattern (`_backfill_tkr14_via_fmp_profile` lines 7062–7064 sleep then retry). After 3 attempts, mark as `fmp_error` and continue.
3. **Symbol mismatch** (FMP returns a profile for a different ticker — possible for renamed / merged issuers) → fail closed for that ticker (resolution state `symbol_mismatch`), write `IDENTITY_DIVERGENCE_INVESTIGATE` to `application_log` per `parent_resolver.py` protocol. **Do not** persist the CIK.
4. **Ambiguous issuer identity** (FMP returns multiple profile rows) → take the first row whose `symbol` matches exactly (case-insensitive). If none, treat as `symbol_mismatch`. NEEDS_OPERATOR_DECISION: whether `len(profiles) > 1` should itself trigger an `IDENTITY_DIVERGENCE_INVESTIGATE` event.
5. **Non-equity / ETF / fund / ADR / pink-sheet** with no SEC CIK → FMP may return a profile but the `cik` field is missing or empty → resolution state `no_cik_in_profile`. Not a defect; documented dead end.
6. **Delisted ticker** → FMP may or may not have a profile depending on its retention. Either way, P1b doesn't care — the row's `lifetime_end IS NULL` guard ensures we only persist for currently-active universe members.
7. **Concurrent process populates CIK between scope read and write** → the `cik IS NULL` clause in the UPDATE returns `0` rows → resolution state `skipped_existing_cik` → no error, just logged.
8. **Network timeout / DNS failure** → caught as `fmp_error` per the existing retry-on-429 pattern; the sub-leg continues with the next ticker.
9. **JSON-decode failure on FMP response** → `fmp_error`; logged with response body truncated to 120 chars (existing pattern).
10. **Universe shrunk between scope SQL and sub-leg execution** → existing logic re-reads `lifetime_end` on UPDATE; rows that became delisted are skipped.

The sub-leg never raises a stage-fatal exception. Per-ticker errors are counted and the stage returns a normal payload at the end.

## Test strategy

All tests must be hermetic. **No live FMP calls.** **No live DB writes.** Live verification is a separate operator-on-demand step (see §"Live verification strategy").

Required tests under `tests/`:

1. **`test_backfill_sec_metadata_fmp_fallback.py`** — extends the existing `tests/test_backfill_sec_metadata_stage.py` patterns:
   - **TEST-FMP-001 unresolved tickers feed into FMP fallback when do_fmp_fallback=true**. Inject a fake `SECTickerCIKMap` returning `unresolved=["FOREIGN1", "FOREIGN2"]`; inject a fake HTTP client returning a matching profile for `FOREIGN1` and a no-match for `FOREIGN2`. Assert `FOREIGN1` gets `cik = '<padded>'` and `cik_source = 'fmp'`; `FOREIGN2` gets no row written.
   - **TEST-FMP-002 do_fmp_fallback=false (default) preserves existing behavior**. Same inputs; assert no FMP calls, no DB writes for the unresolved set, identical stage payload to pre-P1b.
   - **TEST-FMP-003 existing non-NULL CIK is never overwritten by the fallback sub-leg**. Inject a row with `cik = '0000999999'` and inject a fake FMP response returning `cik = '0000111111'` for the same ticker. Assert the row's CIK is unchanged; `skipped_existing_cik` count is incremented.
   - **TEST-FMP-004 symbol mismatch fails closed**. Fake FMP responds with `symbol = 'OTHER'` for requested `FOREIGN1`. Assert no row written; resolution state `symbol_mismatch`; an `IDENTITY_DIVERGENCE_INVESTIGATE` event is written to a fake `application_log`.
   - **TEST-FMP-005 dry_run=true makes no DB writes**. Same inputs as TEST-FMP-001 but `dry_run=true`. Assert no UPDATEs landed; payload reports the would-resolve count.
   - **TEST-FMP-006 FMP HTTP 429 → retry-then-fmp_error**. Fake FMP returns 429 three times. Assert resolution state `fmp_error`; sub-leg continues to next ticker.
   - **TEST-FMP-007 `no_cik_in_profile` is honest dead-end**. Fake FMP returns a profile with `cik` missing/empty. Assert no row written; counter incremented; sub-leg continues.
   - **TEST-FMP-008 `force_refresh_metadata=true` does not re-resolve already-CIK'd rows**. Assert no `IDENTITY_DIVERGENCE_INVESTIGATE` events for rows whose CIK is already populated.
   - **TEST-FMP-009 cik_source values written are subset of the schema constraint**. Assert the written `cik_source` is always in `{'sec_ticker_map', 'fmp'}` (`'manual'` is not used by this stage; the schema permits it for the future).
   - **TEST-FMP-010 telemetry payload has FMP-fallback counts**. Assert the stage return value includes `cik_fmp_fallback: {resolved, no_match, symbol_mismatch, no_cik_in_profile, fmp_error, skipped_existing_cik, written}`.
   - **TEST-FMP-011 stage stays under FMP rate limit headroom**. Assert (under fake httpx) that the per-call sleep parameter is read from cfg and applied.

2. **`tests/test_sec_ticker_cik_map.py`** — no change. The existing 3-test contract (`sec_ticker_to_cik_exact_match`, `unresolved_is_reported`, `existing_cik_not_overwritten_unsafely`) still covers the SEC leg.

3. **Schema sentinel** — no new sentinel required. The migration sentinel `platform/migrations/tests/test_tkr14_regex_sync.py` already covers the CHECK constraint side. The `cik_source = 'fmp'` provenance is already a permitted value.

Total expected: ~11 new hermetic tests, no existing-test changes.

## Live verification strategy

After the implementation PR lands and the unit tests are green, **the operator performs the live smoke separately** (this spec authorises only documentation). Suggested operator sequence:

1. **Dry-run smoke** (≈30 seconds with the proposed default conservative sleep + a `fmp_max_unresolved=100` cap):
   ```bash
   python -m scripts.ops backfill_sec_metadata \
     --param dry_run=true \
     --param do_cik=true \
     --param do_metadata=false \
     --param do_fmp_fallback=true \
     --param fmp_max_unresolved=100 \
     --param no_cik_country_null=true
   ```
   Operator inspects the sample preview + counts. Cross-checks 3–5 sample tickers manually against FMP's web UI to confirm the CIKs look right.

2. **Bounded live run** (same flags, `dry_run=false`, `fmp_max_unresolved=100`):
   Inspect the `application_log` for any `IDENTITY_DIVERGENCE_INVESTIGATE` events. Spot-check the `platform.ticker_classifications` rows that gained `cik_source='fmp'` against FMP's web UI. If any divergence rows surfaced, triage them before scaling.

3. **Full live run** (`dry_run=false`, no `fmp_max_unresolved` cap):
   Wall-clock estimate at 0.2 s/call × 1,419 tickers ≈ **5 minutes**. Re-evaluate the coverage gate post-run.

4. **Coverage-gate recheck**: the existing `audit_data_pipeline` or `run_data_operations.sh` cycle will re-emit `fundamentals_quarterly_completeness` results; operator inspects whether the `metadata_coverage` ratio dropped meaningfully.

5. **If coverage still blocked**: separate operator-on-demand "metadata coverage backfill" follow-up (the lifecycle arc's third deferred bullet) — full active universe SEC metadata backfill (~14 min cold).

## Heavy-lane classification

This PR is **SPEC ONLY** (default lane). The implementation PR following this spec will be **heavy lane** because:

- Touches `scripts/ops.py` (path is `scripts/ops.py` per `.claude/path_registry.yaml` `groups.heavy_lane`).
- Adjacent to the data-acceptance gate at `tpcore/quality/validation/**` (path is `tpcore/quality/validation/**` per the registry).
- Affects `DATA_OPERATIONS_COMPLETE` predicate indirectly via the coverage gate.

The implementation PR will follow `docs/DEV_PIPELINE_STANDARD.md` §1 (brainstorm → expert-harden → spec → plan → implement → split-review → operator gate). This spec doc is step 3 of that pipeline. Step 4 (plan doc) is the next item.

## Implementation plan outline

Outline only — full plan in a follow-up PR per the staged-adoption pattern this repo uses.

1. **Extract** (or inline-call, per the open decision in §"Open operator decisions") an FMP `/stable/profile` client function. If extraction: new file `tpcore/fmp/profile_adapter.py` with one async function `fetch_profile(ticker) -> ProfileResponse`. **NEEDS_OPERATOR_DECISION** (§"Open operator decisions").
2. **Extend** `scripts/ops.py::_stage_backfill_sec_metadata`:
   - Add `do_fmp_fallback`, `fmp_rate_limit_sleep_s`, `fmp_max_unresolved` to `cfg` knobs + docstring.
   - After the existing SEC CIK leg, if `do_fmp_fallback=true`, iterate `result.unresolved` and call the FMP client.
   - Stream-flush UPDATEs every `BATCH = 500` rows.
   - Emit telemetry under `ops.stage.backfill_sec_metadata.fmp_fallback.*`.
   - Write `IDENTITY_DIVERGENCE_INVESTIGATE` to `application_log` on symbol mismatch (compose with the existing `application_log` event helper — see `tpcore/identity/parent_resolver.py` for the event-emission convention).
3. **Add 11 hermetic tests** per §"Test strategy".
4. **No migration.** No new tables. No new columns.
5. **Update `docs/DATABASE_AND_DATAFLOW.md` §5 Implementation Queue** with a one-line P1b entry (if §5 is the canonical operational queue per `TODO.md`'s preamble).
6. **No changes to `tpcore/risk/**`, `tpcore/quality/validation/**`, engine packages, broker/order/risk code.** Trading behavior unchanged.

## Open operator decisions

These are flagged so the implementation PR can close them.

1. **Inline-call vs. extract `tpcore/fmp/profile_adapter.py`.** The data-adapter rule in `.claude/rules/data-adapter.md` mandates "canonical stage entry, not forked scripts" — but does not directly opine on extracting a shared FMP adapter when two existing stages already inline-call `httpx`. **Recommendation in this spec**: extract, because three call sites (the existing two + this new one) cross the threshold where shared client wiring (timeout, retry-on-429, rate-limit) becomes worth a module. Operator may override.

2. **Dry-run + FMP API calls.** `dry_run=true` could either (a) still make the FMP calls and report the would-resolve set without DB writes (more accurate but spends quota), or (b) skip the FMP calls entirely and just count `len(CIKResolveResult.unresolved)` as the upper bound (zero quota cost but loose). **Recommendation in this spec**: (a) for the first 100 tickers (per `fmp_max_unresolved=100` cap); (b) for any larger dry-run. Operator may pick uniformly one way.

3. **Country writeback in the FMP fallback sub-leg.** FMP's `/profile` response carries `country`; the existing `_stage_fmp_profile_backfill` writes it via a different stage. **Recommendation in this spec**: do NOT write country in P1b. Keep stage scope CIK-only; let the existing `fmp_profile_backfill` stage own country / sector / legal-name on its own cadence. Operator may flip this to "also write country when CIK is resolved" if they want one operator invocation to do both.

4. **Ambiguous-FMP-response divergence threshold.** Whether `len(profiles) > 1` (multiple FMP rows for a single symbol) should fire `IDENTITY_DIVERGENCE_INVESTIGATE` or silently take the first matching row. **Recommendation in this spec**: fire `IDENTITY_DIVERGENCE_INVESTIGATE`. The operator-review-on-divergence default is consistent with `parent_resolver.py` and minimizes silent wrong-issuer attribution.

5. **Foreign-issuer ADR semantics.** Whether ADR tickers (which FMP may return a CIK for, but whose "CIK" is the depositary bank's filer ID, not the underlying issuer's) should be marked `resolved` or `symbol_mismatch`. **NEEDS_OPERATOR_DECISION**. The repo doesn't currently distinguish issuer-CIK vs. depositary-CIK; this is its own design question that may warrant a separate spec.

6. **Hook into `run_data_operations.sh` or remain operator-on-demand only.** The TODO entry says "operator-on-demand"; the spec assumes operator-on-demand. **Recommendation in this spec**: keep operator-on-demand. Long-tail backfills should not auto-run on the scheduled cycle.

---

> Status: SPEC ONLY. Implementation plan + implementation PR are follow-ups. Cross-references: `scripts/ops.py::_stage_backfill_sec_metadata` (P0-003), `tpcore/sec/ticker_cik_map.py` (P0-001), `tpcore/identity/parent_resolver.py` (v2.2 §1.10 lane dispatch), `platform/migrations/versions/20260530_0200_issuer_metadata_foundation.py` (schema), `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py` (coverage gate), TODO.md `## ⚑ Evidence-based fundamentals/lifecycle arc` deferred subsection.
