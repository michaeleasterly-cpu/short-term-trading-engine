# P1b — CIK long-tail FMP `/profile` fallback backfill (implementation plan)

> **Phase: PLAN ONLY.** This document is the only artifact created or modified by this PR. No implementation code, no migration, no live FMP calls, no live DB writes, no trading/risk/runtime behavior change. The follow-up heavy-lane implementation PR is the next step in the `docs/DEV_PIPELINE_STANDARD.md` §1 sequence.

## Verdict

Translate the merged spec (`docs/superpowers/specs/2026-06-01-p1b-cik-long-tail-backfill.md`, PR #423) into a concrete, file-by-file implementation plan: extract `tpcore/fmp/profile_adapter.py`, extend `scripts/ops.py::_stage_backfill_sec_metadata` with a `do_fmp_fallback` sub-leg, add 12 hermetic tests, no migration, no validator semantics change, no `tpcore/risk/**` touch. Operator-on-demand only.

## Spec inputs

Source-of-truth spec: `docs/superpowers/specs/2026-06-01-p1b-cik-long-tail-backfill.md` (merged via PR #423, commit `0748593`).

Pre-resolved by the operator for this plan (each item from spec §"Open operator decisions"):

| Spec open decision | Operator's resolution |
|---|---|
| Inline-call vs extract `tpcore/fmp/profile_adapter.py` | **Extract** |
| `dry_run` + FMP API calls | **Call FMP for first 100 unresolved by default; skip beyond unless explicitly bounded via `fmp_max_unresolved`** |
| Country writeback in sub-leg | **No — CIK-only** |
| Ambiguous FMP response (`len(profiles) > 1`) | **Emit `IDENTITY_DIVERGENCE_INVESTIGATE`** |
| ADR-CIK vs depositary-CIK semantics | **Defer — no ADR-specific override in P1b** |
| Trigger | **Operator-on-demand only** (no scheduled wire-up) |

## Resolved operator decisions (canonical for the implementation PR)

The implementation PR MUST honor each decision verbatim. Re-opening a decision requires a separate spec amendment, not a plan revision.

1. **Adapter is extracted.** New file `tpcore/fmp/profile_adapter.py` with one async public function `fetch_profile(client, ticker, *, api_key) -> FMPProfileResult`. The existing call sites in `scripts/ops.py` (`_stage_fmp_profile_backfill` line 4194; `_backfill_tkr14_via_fmp_profile` line 6955) are NOT refactored in this PR — they continue inline-calling. Migration of those sites to the new adapter is a future cleanup; **out of scope for P1b** to keep diff size + review surface small.
2. **Dry-run + FMP API calls.** When `dry_run=true`:
   - If `fmp_max_unresolved` is unset or > 100, the sub-leg processes only the first 100 unresolved tickers through FMP (real HTTP calls), DB-writes zero rows, prints would-resolve sample.
   - If `fmp_max_unresolved` is explicitly set ≤ 100, processes that many with real FMP calls + zero DB writes.
   - Operator can force `dry_run=true` against the full set by passing both `fmp_max_unresolved=0` (the special "no cap" value documented in `_stage_fmp_profile_backfill` line 4231) AND `dry_run=true` — but that's an explicit ask, not the default.
3. **Country/sector/legal-name writeback is OFF in P1b.** `cik` column only. The existing `fmp_profile_backfill` stage continues to own country/sector/legal-name on its own cadence.
4. **Ambiguous-response divergence.** `len(profiles) > 1` after the symbol-equality filter is `symbol_mismatch` → no write + `IDENTITY_DIVERGENCE_INVESTIGATE` to `platform.application_log`.
5. **ADRs deferred.** P1b does not distinguish issuer-CIK from depositary-CIK. Any FMP-returned CIK that survives the symbol-equality + numeric-CIK gates is persisted as `cik_source = 'fmp'`. Follow-up spec needed for ADR carve-outs.
6. **Operator-on-demand only.** No `run_data_operations.sh` integration, no launchd, no `dispatcher.py` registration. Operator invokes via `python scripts/ops.py --stage backfill_sec_metadata --param ...` exclusively.

## Implementation boundaries

**Touched (the implementation PR):**
- `tpcore/fmp/profile_adapter.py` — new file (~80–120 lines).
- `tpcore/fmp/__init__.py` — add module-level export of `FMPProfileAdapter`/`fetch_profile`/`FMPProfileResult` if package wiring requires.
- `scripts/ops.py` — extend `_stage_backfill_sec_metadata` only (no other stage edited; no new `_STAGE_SPECS` entry).
- `tests/test_p1b_cik_long_tail_fallback.py` — new file, 12 hermetic tests.

**Not touched (must remain unchanged in the implementation PR — hard rule):**
- `platform/migrations/**` — no migration; `cik_source='fmp'` is already a permitted value (verified at `platform/migrations/versions/20260530_0200_issuer_metadata_foundation.py:76-77`).
- `tpcore/risk/**` — no capital-gate / RiskGovernor change.
- `tpcore/quality/validation/**` — no validator semantics change. The validator at `fundamentals_quarterly_completeness.py` reads the column the implementation will populate; that's the entire point. No code change in the validator file.
- `tpcore/providers.py`, `tpcore/engine_profile.py` — no DFCR/ECR triggered.
- `tpcore/selfheal/**`, `tpcore/auditheal/**` — no HealSpec or audit change.
- `ops/engine_service.py`, `ops/engine_sdlc/**`, `ops/data_feed_sdlc/**`, `ops/cutover_agent.py` — no change.
- `reversion/`, `vector/`, `momentum/`, `sentinel/`, `canary/`, `catalyst/` — no engine code change.
- `.claude/**`, `.github/workflows/**`, `PROJECT_PROFILE.yaml`, `scripts/run_dev_system_audit.sh` — no change.
- `tpcore/sec/ticker_cik_map.py` — no change (already exposes everything needed via `resolve_missing_ciks`).

## File change plan

### New: `tpcore/fmp/profile_adapter.py`

Public surface (deliberately minimal):

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx
import structlog


FMP_BASE_URL = "https://financialmodelingprep.com/stable"   # mirrors fundamentals_adapter pattern
_DEFAULT_TIMEOUT_S = 20.0
_PROFILE_PATH = "/profile"


ResolutionState = Literal[
    "resolved",
    "no_match",
    "symbol_mismatch",
    "no_cik_in_profile",
    "ambiguous_response",   # distinct from symbol_mismatch; sub-leg maps both to symbol_mismatch downstream
    "fmp_error",
]


@dataclass(frozen=True, slots=True)
class FMPProfileResult:
    """Outcome of one FMP /stable/profile call for one ticker."""

    ticker: str                       # the requested ticker (input echo)
    state: ResolutionState
    cik: str | None = None            # left-padded to 10 digits when state == "resolved"
    country: str | None = None        # diagnostic only; sub-leg does NOT persist
    profiles_count: int = 0           # how many array entries FMP returned
    http_status: int | None = None    # for fmp_error / debugging
    error_summary: str | None = None  # truncated 120 chars; for telemetry
```

Two helper-level functions:

- `async def fetch_profile(client: httpx.AsyncClient, ticker: str, *, api_key: str, retry_429_max: int = 3) -> FMPProfileResult`
  - Single GET to `https://financialmodelingprep.com/stable/profile?symbol=<ticker>&apikey=<api_key>`.
  - Retry-on-429 with exponential-ish sleep matching the existing pattern in `_backfill_tkr14_via_fmp_profile` (sleep 2 s, then 4 s, then 8 s capped at 3 attempts).
  - Decoder behavior:
    - HTTP non-200 (after retries) → `state="fmp_error"`, `http_status`, `error_summary`.
    - JSON not a list → `state="fmp_error"`.
    - Empty list → `state="no_match"`.
    - List length 1 with matching `symbol`:
      - Has non-empty numeric `cik` → `state="resolved"`, `cik` zero-padded to 10.
      - Missing/empty `cik` → `state="no_cik_in_profile"`.
    - List length 1 with non-matching `symbol` → `state="symbol_mismatch"`.
    - List length ≥ 2 → `state="ambiguous_response"`.
  - No DB writes. No state. Pure HTTP + parse.
- `def _zero_pad_cik(raw: str | int) -> str | None` — 10-digit pad; returns `None` for non-numeric inputs.

Both helpers are testable hermetically with `httpx.MockTransport` (no live network). The adapter never raises on per-ticker errors — errors are encoded in the `FMPProfileResult.state` field.

### Modified: `tpcore/fmp/__init__.py`

Add re-exports if the existing `__init__` already re-exports siblings (NEEDS_REPO_VERIFICATION at implementation time — check if `fundamentals_adapter` is re-exported; mirror that pattern). Otherwise leave untouched and import via `from tpcore.fmp.profile_adapter import …` directly.

### Modified: `scripts/ops.py::_stage_backfill_sec_metadata`

Add three new `cfg` knobs to the existing param-parsing block:

| Param | Type | Default | Purpose |
|---|---|---|---|
| `do_fmp_fallback` | bool | `false` | Master enable for the new sub-leg. Default off so existing operator-on-demand invocations behave identically. |
| `fmp_rate_limit_sleep_s` | float | `0.2` | Per-call sleep (matches the more-conservative `_backfill_tkr14_via_fmp_profile` posture). |
| `fmp_max_unresolved` | int | `100` | Cap on unresolved tickers processed in one run. `0` means no cap (matching the `_stage_fmp_profile_backfill` convention at line 4231). |

Wiring (after the existing SEC CIK leg ends, before the metadata leg starts):

1. If `not do_fmp_fallback` → skip; existing behavior unchanged. Continue to metadata leg.
2. Build the unresolved set from `result.unresolved` (already in scope from the SEC CIK leg at line 2843).
3. Cap: if `fmp_max_unresolved > 0`, slice `unresolved[:fmp_max_unresolved]`.
4. Open `httpx.AsyncClient(timeout=20.0)` (matches `_backfill_tkr14_via_fmp_profile` line 7060).
5. Loop with `await asyncio.sleep(fmp_rate_limit_sleep_s)` between calls; concurrency 1 (operator-on-demand long-tail; predictable rate beats throughput).
6. For each ticker, call `fetch_profile(client, ticker, api_key=FMP_API_KEY)`.
7. Buffer write candidates in `fmp_cik_writes: list[tuple[ticker, cik]]` (one row per `state=="resolved"`).
8. Stream-flush every `BATCH = 500` rows (matches `_backfill_tkr14_via_fmp_profile` line 7016) using the existing transactional pattern. Guarded `UPDATE`:

   ```sql
   UPDATE platform.ticker_classifications
   SET cik = $2, cik_source = 'fmp'
   WHERE ticker = $1
     AND cik IS NULL
     AND lifetime_end IS NULL
   ```

   Rows that fail the guard (concurrent population, lifetime ended between scope read and write) increment `skipped_existing_cik` / `skipped_lifetime_ended` from the SQL row count.

9. On `state="symbol_mismatch"` or `state="ambiguous_response"`: emit one `IDENTITY_DIVERGENCE_INVESTIGATE` event to `platform.application_log` per the existing convention (see "Logging and data_quality/application_log behavior" §).
10. Telemetry: emit `ops.stage.backfill_sec_metadata.fmp_fallback.{start,progress,symbol_mismatch,writes_committed,end}` with structured fields.
11. After the FMP sub-leg ends, control returns to the existing metadata leg. The metadata leg already supports newly-resolved-CIKs-this-run via `cik_resolutions_this_run` (line 2879); extend that dict to include FMP-resolved CIKs so the metadata leg picks them up in the same run.
12. Extend the stage's return payload with a `cik_fmp_fallback` sub-dict carrying all 7 terminal-state counters (per the spec):

    ```python
    "cik_fmp_fallback": {
        "candidates": int,                # = len(unresolved) capped
        "resolved": int,
        "no_match": int,
        "symbol_mismatch": int,
        "no_cik_in_profile": int,
        "fmp_error": int,
        "skipped_existing_cik": int,
        "skipped_lifetime_ended": int,
        "written": int,                   # rows the UPDATE actually committed
        "divergence_events_written": int, # IDENTITY_DIVERGENCE_INVESTIGATE count
    }
    ```

Diff size estimate: ~120 lines added inside `_stage_backfill_sec_metadata`, plus 1 import line. No structural refactor of the stage's existing CIK/metadata legs.

### New: `tests/test_p1b_cik_long_tail_fallback.py`

12 hermetic tests (see §"Test plan" for the per-test contract). All use the `_mock_pool` helper pattern from `tests/test_backfill_sec_metadata_stage.py:31-50` extended with an `executemany`-recording capture. FMP HTTP is mocked via `httpx.MockTransport`. No real `FMP_API_KEY` required; tests inject a sentinel string.

## Detailed step sequence (for the implementation PR)

Numbered for reviewer cross-checking and operator audit:

1. **Add `tpcore/fmp/profile_adapter.py`** with the public surface defined above. Include module docstring stating "P1b CIK long-tail fallback adapter; do NOT use for country/sector/legal-name enrichment — see `_stage_fmp_profile_backfill`."
2. **Add re-export to `tpcore/fmp/__init__.py`** only if the existing module already re-exports `fundamentals_adapter` symbols (NEEDS_REPO_VERIFICATION at implementation time).
3. **Edit `scripts/ops.py::_stage_backfill_sec_metadata`**:
   - Add the 3 new `cfg` knob parses near the existing param block (around line 2700).
   - Insert the FMP-fallback sub-leg between the existing SEC CIK leg's `log.info("ops.stage.backfill_sec_metadata.cik_leg", …)` (line 2851) and the metadata-leg `if do_metadata:` block (line 2858).
   - Extend `cik_resolutions_this_run` dict construction to include FMP-resolved rows so the metadata leg picks them up.
   - Add the `cik_fmp_fallback` sub-dict to the stage's return payload (around the existing dict assembly).
   - Extend the docstring's "Scope params" table with the 3 new knobs and the "Output payload" example with the new sub-dict.
4. **Add `tests/test_p1b_cik_long_tail_fallback.py`** with the 12 tests per §"Test plan".
5. **Local-gate run** (the implementation PR's pre-push checklist):
   - `python scripts/check_manifests.py` → OK
   - `python -m ruff check tpcore/fmp/profile_adapter.py scripts/ops.py tests/test_p1b_cik_long_tail_fallback.py` → clean
   - `python -m pytest -p no:xdist -q tests/test_p1b_cik_long_tail_fallback.py` → 12 passed
   - `python -m pytest -p no:xdist -q tests/test_backfill_sec_metadata_stage.py` → existing tests still green (no regression in the parent stage's TEST-007/TEST-009)
   - `python -m pytest -p no:xdist -q` → whole-suite serial green (authoritative gate per `.claude/rules/tests-and-ci.md`)
   - `python -m pytest -p no:xdist -q tests/<reversed-order-module-list>` → order-flip still green per the same rule
   - `gitleaks detect --config .gitleaks.toml --no-banner --redact --source .` → no leaks

## FMP profile adapter design

Module structure (single file ~80–120 lines):

- Module docstring: purpose ("P1b CIK fallback ONLY; never write country/sector here — that's `_stage_fmp_profile_backfill`'s lane"), authority order reminder ("SEC > FMP; never overwrite non-NULL"), no-DB-writes contract, hermetic-testable invariant.
- `FMP_BASE_URL` constant mirrors `tpcore/fmp/fundamentals_adapter.py:29`.
- `ResolutionState` Literal type alias for type-safety in callers.
- `FMPProfileResult` frozen-slots dataclass (above).
- `fetch_profile(client, ticker, *, api_key, retry_429_max=3)` async function.
- `_zero_pad_cik(raw)` private helper.

Imports: `httpx`, `structlog`, `dataclass`/`Literal` from stdlib. No `tpcore.outage.DataProviderOutage` import — the adapter's contract is "encode errors in `FMPProfileResult.state`, never raise per-ticker"; that matches the spec's "sub-leg never raises a stage-fatal exception" requirement.

## scripts/ops.py stage wiring

**Stage signature unchanged.** Same `pool, cfg` arguments; same return-payload `dict[str, Any]` shape (with the new sub-dict added).

**`cfg` parsing additions** (`_to_bool` already defined at line 2619):

```python
do_fmp_fallback = _to_bool(cfg.get("do_fmp_fallback", False))
fmp_rate_limit_sleep_s = float(cfg.get("fmp_rate_limit_sleep_s", 0.2))
fmp_max_unresolved = int(cfg.get("fmp_max_unresolved", 100))
```

**Sub-leg insertion point.** After the existing `log.info("ops.stage.backfill_sec_metadata.cik_leg", ...)` (currently line 2851) and before `if do_metadata:` (line 2858). The metadata leg reuses the existing `cik_resolutions_this_run` dict (line 2879) — extend that to include FMP-resolved rows before the metadata loop starts:

```python
# After the SEC CIK leg + FMP fallback sub-leg complete, BEFORE the
# metadata leg. The metadata leg's _cik_for helper at line 2882 reads
# this dict for tickers newly resolved in THIS run.
cik_resolutions_this_run = {
    t: cik for t, cik, _src in cik_writes
}
```

becomes:

```python
cik_resolutions_this_run = {
    t: cik for t, cik, _src in cik_writes
}
# P1b — FMP-fallback-resolved CIKs also feed the metadata leg in the
# same run so the operator gets evidence-column population for free.
for ticker, cik in fmp_cik_writes:
    cik_resolutions_this_run.setdefault(ticker, cik)
```

(The `setdefault` is defensive — the SEC leg always wins for the same ticker, though by definition `fmp_cik_writes` only contains tickers that were SEC-unresolved, so a collision shouldn't occur. Belt-and-braces.)

## Persistence and provenance

| Column | Set to | Guard |
|---|---|---|
| `platform.ticker_classifications.cik` | FMP-derived 10-digit string | `WHERE cik IS NULL AND lifetime_end IS NULL` |
| `platform.ticker_classifications.cik_source` | `'fmp'` | same guard; **schema CHECK already permits `'fmp'`** (verified at `20260530_0200_issuer_metadata_foundation.py:76`) |
| `platform.application_log` row | `event_type='IDENTITY_DIVERGENCE_INVESTIGATE'` | one row per `symbol_mismatch` / `ambiguous_response` |

**No write to**: `country`, `gics_sector`, `current_legal_name`, `sec_document_type_primary` (those are owned by other stages / the existing SEC metadata leg), `metadata_source`, `metadata_updated_at`, any other column.

**Idempotency.** Re-running the stage on the same scope is a no-op for already-resolved rows because:
- The SEC CIK leg's `existing_ciks` dict (line 2839) carries the non-NULL flag; `resolve_missing_ciks` short-circuits already-set tickers into `skipped_already_set`.
- The FMP UPDATE's `cik IS NULL` clause guards against double-write.

## Terminal state handling

Per spec §"Resolution states" — the implementation must produce one terminal state per processed ticker. Mapping from `FMPProfileResult.state` → operator-facing terminal state in `cik_fmp_fallback`:

| Adapter `state` | Operator terminal state | Persistence side effect | `IDENTITY_DIVERGENCE_INVESTIGATE`? |
|---|---|---|---|
| `resolved` | `resolved` (or `skipped_existing_cik`/`skipped_lifetime_ended` if UPDATE returns 0 rows) | `UPDATE` once | No |
| `no_match` | `no_match` | None | No |
| `symbol_mismatch` | `symbol_mismatch` | None | **Yes** |
| `ambiguous_response` | `symbol_mismatch` (collapsed) | None | **Yes** |
| `no_cik_in_profile` | `no_cik_in_profile` | None | No |
| `fmp_error` | `fmp_error` | None | No |

The collapse of `ambiguous_response` → `symbol_mismatch` is intentional: from the operator's perspective both are "FMP couldn't give us a confident single CIK for this ticker", and the divergence event captures the nuance.

## Logging and `data_quality_log`/`application_log` behavior

**Structured-log events** (`structlog`):
- `ops.stage.backfill_sec_metadata.fmp_fallback.start` — `unresolved_count`, `cap_applied`, `fmp_rate_limit_sleep_s`, `dry_run`.
- `ops.stage.backfill_sec_metadata.fmp_fallback.progress` — every 100 tickers, `processed`, `resolved`, `errors`.
- `ops.stage.backfill_sec_metadata.fmp_fallback.symbol_mismatch` — per occurrence, `ticker`, `fmp_symbol_returned` (NOT logged in full to avoid leaking PII; only the requested + returned symbol strings).
- `ops.stage.backfill_sec_metadata.fmp_fallback.writes_committed` — totals.
- `ops.stage.backfill_sec_metadata.fmp_fallback.end` — full `cik_fmp_fallback` sub-dict echoed.

**`platform.application_log` writes** (only on `symbol_mismatch` / `ambiguous_response`):

```sql
INSERT INTO platform.application_log (
    event_type,
    payload,
    recorded_at
) VALUES (
    'IDENTITY_DIVERGENCE_INVESTIGATE',
    $1::jsonb,
    NOW()
)
```

Payload shape (mirrors the `parent_resolver.py` precedent — keeps PII to the symbols + provenance):

```json
{
  "source": "p1b_fmp_fallback",
  "requested_ticker": "FOREIGN1",
  "fmp_response_state": "symbol_mismatch",
  "fmp_symbol_returned": "OTHER",
  "fmp_profiles_count": 1,
  "row_existing_cik": null,
  "advised": "operator review before relying on FMP profile for this ticker"
}
```

**No `platform.data_quality_log` writes from this stage.** The coverage signal lives in the existing `fundamentals_quarterly_completeness` validator output; P1b is an input-side backfill, not a validator.

**Dry-run logging**: same structured-log events fire (operator must see what would have been done), but the `INSERT INTO platform.application_log` line is gated on `if not dry_run:` so divergence events also aren't written in dry-run.

## Dry-run and bounded-live behavior

Per the operator's resolved decisions:

| Knobs | Behavior |
|---|---|
| `dry_run=true` (default), `fmp_max_unresolved` unset (default 100) | FMP HTTP calls made for first 100 unresolved tickers. Zero DB writes. Sample preview of would-resolve set printed. |
| `dry_run=true`, `fmp_max_unresolved=25` | FMP HTTP calls for first 25. Zero DB writes. |
| `dry_run=true`, `fmp_max_unresolved=0` | FMP HTTP calls for ALL unresolved (explicit operator ask; "no cap"). Zero DB writes. |
| `dry_run=false`, `fmp_max_unresolved=100` (recommended first live) | First 100 unresolved processed live; DB writes guarded by `cik IS NULL`. |
| `dry_run=false`, `fmp_max_unresolved=0` | Full ~1,419 ticker run (~5 min at 0.2 s/call). |
| `do_fmp_fallback=false` (default) | Sub-leg skipped entirely. Stage behaves identically to today. |

**No automatic operator-confirmation prompts.** The stage runs to completion under whatever knobs the operator set; trust the operator-on-demand convention.

## Test plan

12 hermetic tests at `tests/test_p1b_cik_long_tail_fallback.py`. All `pytest.mark.asyncio`. All inject the adapter via the existing `_mock_pool` pattern (extend `tests/test_backfill_sec_metadata_stage.py:31-50` helper into the new test file, OR import it from a shared `tests/conftest.py` extension — implementation PR decides).

| # | Test | Purpose | Required asserts |
|---|---|---|---|
| 1 | `test_fmp_profile_adapter_extracts_cik` | FMP `/stable/profile` fixture with `cik` field normalizes correctly via `fetch_profile`. | `state == "resolved"`, `cik == "0000123456"` (10-padded), `country` populated, `profiles_count == 1`. |
| 2 | `test_fallback_resolves_unresolved_ticker_with_fmp_cik` | Unresolved SEC ticker gets FMP CIK candidate; UPDATE fires only when `cik IS NULL`. | `cik_fmp_fallback.resolved == 1`, `cik_fmp_fallback.written == 1`, executemany capture shows the expected UPDATE with `cik_source = 'fmp'`. |
| 3 | `test_fallback_never_overwrites_existing_cik` | Row with existing `cik = '0000999999'` is **skipped** even if FMP would return `0000111111`. | `cik_fmp_fallback.skipped_existing_cik >= 1`, no UPDATE statement modifies the row. |
| 4 | `test_symbol_mismatch_fails_closed_and_logs_divergence` | FMP returns `symbol="OTHER"` for requested `"FOREIGN1"` → no write + `IDENTITY_DIVERGENCE_INVESTIGATE` event. | `cik_fmp_fallback.symbol_mismatch == 1`, `cik_fmp_fallback.divergence_events_written == 1`, executemany capture shows an `INSERT INTO platform.application_log` with `event_type='IDENTITY_DIVERGENCE_INVESTIGATE'`. |
| 5 | `test_no_cik_in_profile_terminal_state` | FMP profile present but `cik` empty/missing → `no_cik_in_profile`, no write. | `cik_fmp_fallback.no_cik_in_profile == 1`, no UPDATE issued. |
| 6 | `test_no_profile_terminal_state` | Empty list response from FMP → `no_match`. | `cik_fmp_fallback.no_match == 1`, no UPDATE issued. |
| 7 | `test_multiple_profiles_ambiguous_terminal_state` | FMP returns `len(profiles) > 1` (with at least one matching symbol) → `symbol_mismatch` (collapsed) + divergence event. | `cik_fmp_fallback.symbol_mismatch >= 1`, `cik_fmp_fallback.divergence_events_written >= 1`. |
| 8 | `test_fmp_error_continues_batch` | One FMP HTTP 500 mid-batch is logged as `fmp_error`; remaining tickers continue. | `cik_fmp_fallback.fmp_error >= 1`, `cik_fmp_fallback.resolved >= 1` for the surviving ticker. |
| 9 | `test_skips_lifetime_ended` | Row whose `lifetime_end IS NOT NULL` between scope read and UPDATE → `skipped_lifetime_ended`. | UPDATE returns 0 rows; counter incremented; no error. |
| 10 | `test_dry_run_persists_nothing` | `dry_run=true` may call FMP but never UPDATEs. | executemany capture shows zero UPDATE statements; `cik_fmp_fallback.written == 0`; `IDENTITY_DIVERGENCE_INVESTIGATE` events also NOT inserted. |
| 11 | `test_summary_counts_include_all_terminal_states` | Stage payload includes all 7 terminal-state counters plus `candidates` + `written` + `divergence_events_written`. | `set(cik_fmp_fallback.keys()) >= {"candidates","resolved","no_match","symbol_mismatch","no_cik_in_profile","fmp_error","skipped_existing_cik","skipped_lifetime_ended","written","divergence_events_written"}`. |
| 12 | `test_no_migration_required_sentinel` | Schema CHECK constraint already permits `cik_source='fmp'`. Read the migration file's `_VALID_CIK_SOURCES` tuple via static parse and assert membership. | `"fmp" in _VALID_CIK_SOURCES` (read from `platform/migrations/versions/20260530_0200_issuer_metadata_foundation.py`). |

Test fixtures:
- `httpx.MockTransport` returning per-ticker JSON arrays for each scenario (`resolved`, `no_match`, `symbol_mismatch`, `ambiguous`, `no_cik_in_profile`, `fmp_error_500`).
- Mock asyncpg pool with `executemany` capture + per-call UPDATE-row-count override (TEST 9 needs `UPDATE 0`).
- `SEC_EDGAR_USER_AGENT` env-var set at module level (matching the existing test file pattern at `tests/test_backfill_sec_metadata_stage.py:18`).
- `FMP_API_KEY` env-var set to a sentinel like `"TEST_KEY_NOT_REAL"` at module level.

## Verification commands

Implementation PR's pre-push verification (in order; each must be clean before push):

```bash
# 1. Architecture gate
python scripts/check_manifests.py

# 2. New tests pass
python -m pytest -p no:xdist -q tests/test_p1b_cik_long_tail_fallback.py

# 3. Adjacent existing tests still pass (regression check on the parent stage)
python -m pytest -p no:xdist -q tests/test_backfill_sec_metadata_stage.py tests/test_sec_ticker_cik_map.py

# 4. Whole-suite serial — the authoritative gate per .claude/rules/tests-and-ci.md
python -m pytest -p no:xdist -q

# 5. Reversed-module-order gate (per the same rule — flips the per-module init order)
# Implementation PR will compute the actual reversed list; placeholder:
python -m pytest -p no:xdist -q --tb=short  # operator constructs reversed list at push time

# 6. Lint
python -m ruff check tpcore/fmp/profile_adapter.py scripts/ops.py tests/test_p1b_cik_long_tail_fallback.py

# 7. Secret-scan
gitleaks detect --config .gitleaks.toml --no-banner --redact --source .

# 8. Architecture sentinels covering the round-trip surface
python -m pytest -p no:xdist -q tests/test_project_profile_present.py tests/test_manifest_check_present.py
```

**Authoritative gate:** items 4 + 5 (whole-suite + order-flip) per `.claude/rules/tests-and-ci.md`. A green parallel-test run with a red whole-suite or order-flip is a FAIL.

## Live smoke sequence

**Operator-on-demand only** (NOT part of the implementation PR's automated verification — the operator runs these after the PR merges):

```bash
# 0. Confirm DATABASE_URL is the IPv4 pooler (per .env.example line 32-37)
export DATABASE_URL=$DATABASE_URL_IPV4

# 1. Tiny dry-run smoke — 25 tickers, no DB writes, real FMP calls.
#    Operator sanity-checks 3-5 sample tickers' would-resolve CIKs
#    against FMP's web UI.
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=true \
    --param do_fmp_fallback=true \
    --param fmp_max_unresolved=25 \
    --param no_cik_country_null=true

# 2. Wider dry-run — 100 tickers, still no DB writes.
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=true \
    --param do_fmp_fallback=true \
    --param fmp_max_unresolved=100 \
    --param no_cik_country_null=true

# 3. Bounded live — first 100 unresolved, DB writes guarded by cik IS NULL.
#    Operator inspects platform.application_log afterward for any
#    IDENTITY_DIVERGENCE_INVESTIGATE events.
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=false \
    --param do_fmp_fallback=true \
    --param fmp_max_unresolved=100 \
    --param no_cik_country_null=true

# 4. Pre-existing default-behavior smoke (confirms the do_fmp_fallback
#    default is OFF and the existing dry-run preview path is unchanged).
python scripts/ops.py --stage backfill_sec_metadata \
    --param dry_run=true
```

After step 3 (bounded live), operator decides whether to proceed with the full ~1,419-ticker run (`fmp_max_unresolved=0`, `dry_run=false`) based on the divergence event count + manual spot-checks.

Post-full-run coverage check:

```bash
# Verifies the coverage gate ratio dropped (or didn't — informs whether
# the separate "metadata coverage backfill" follow-up item is also needed).
python -m scripts.ops --stage validate_data --param suite=fundamentals_quarterly_completeness
```

(The exact stage name to invoke for the validator is NEEDS_REPO_VERIFICATION at implementation time; the canonical validator runner is whatever `run_data_operations.sh` invokes for the `fundamentals_quarterly_completeness` check.)

## Rollback plan

If a defect is discovered post-merge:

1. **Toggle off via the flag.** Operator stops invoking with `--param do_fmp_fallback=true`. Existing default behavior (`do_fmp_fallback=false`) is unchanged; no rollback PR strictly required to disable.
2. **Code revert.** `git revert <implementation_commit>` on `main`. Two files revert: `tpcore/fmp/profile_adapter.py` (deletion) and `scripts/ops.py` (back to pre-P1b sub-leg). No schema change to roll back (because none was made). No data-state rollback required — any already-written `cik_source='fmp'` rows remain valid per the schema CHECK; they don't break any other code path.
3. **Per-row remediation (if a specific FMP-derived CIK is wrong).** Operator's `manual` provenance path is already in the schema (`_VALID_CIK_SOURCES` at `20260530_0200_issuer_metadata_foundation.py:77`). Manual operator UPDATE sets `cik = <corrected>, cik_source = 'manual'` per the existing convention. No special tooling needed.

Per-stage error rollback within a single run is handled by the existing `BATCH = 500` streaming-flush pattern: a mid-run interruption leaves committed rows committed; the next invocation picks up where it left off (the `cik IS NULL` guard ensures no double-write).

## Risk assessment

| Risk | Severity | Mitigation |
|---|---|---|
| FMP returns a CIK that belongs to a different issuer (e.g. ADR vs underlying) | Medium | Symbol-equality guard + `IDENTITY_DIVERGENCE_INVESTIGATE` event on mismatch. Operator's `manual` provenance override is the per-row fix. Spec defers ADR-specific carve-out (resolved decision #5). |
| FMP rate-limit exhaustion during full run | Low | 0.2 s/call sleep × 1,419 = 5 minutes. Below the 750/min Starter ceiling. Retry-on-429 in the adapter. `fmp_max_unresolved` lets operator throttle further. |
| FMP API outage during long-tail run | Low | Per-ticker errors continue the loop (`fmp_error` state); no stage-fatal exception. Operator re-runs after the outage; `cik IS NULL` guard makes the re-run idempotent. |
| Coverage gate threshold (25%) still not crossed after P1b | Medium | This is exactly what the separate "metadata coverage backfill" follow-up item addresses (full-active-universe SEC metadata backfill). P1b's success criterion is structural completeness, not threshold-clearing. |
| New write code path bypasses some existing trigger / FK | Low | The UPDATE pattern is identical in shape to `_stage_fmp_profile_backfill` line 4321-4327 and `_backfill_tkr14_via_fmp_profile` line 7011-7026 (both already in production); no new FK / trigger surface introduced. |
| `tpcore.outage.DataProviderOutage` not used (deviates from existing adapter convention) | Low | Intentional design choice: per-ticker errors are encoded in `FMPProfileResult.state` not raised, so the stage can continue. Other FMP adapters that raise `DataProviderOutage` (`fundamentals_adapter.py`, `ingest_fmp_bars.py`) are scheduled / engine-consumed and need fail-fast; P1b is operator-on-demand and per-ticker resilience is more valuable than fail-fast. |
| Whole-suite gate flakes on the parent stage's existing tests | Low | TEST 2/3/10 explicitly assert the stage's parent-leg behavior is unchanged. Implementation PR's verification step 3 runs `tests/test_backfill_sec_metadata_stage.py` standalone. |

## Acceptance criteria (for the implementation PR that follows)

- [ ] New file `tpcore/fmp/profile_adapter.py` exists with the documented public surface.
- [ ] `scripts/ops.py::_stage_backfill_sec_metadata` adds the three `cfg` knobs, the FMP fallback sub-leg, the extended `cik_resolutions_this_run` dict, and the `cik_fmp_fallback` return-payload sub-dict.
- [ ] `tests/test_p1b_cik_long_tail_fallback.py` exists with 12 tests, all hermetic, all green.
- [ ] No file under `platform/migrations/**` is modified.
- [ ] No file under `tpcore/risk/**` is modified.
- [ ] No file under `tpcore/quality/validation/**` is modified.
- [ ] No file under engine packages (`reversion/`, `vector/`, `momentum/`, `sentinel/`, `canary/`, `catalyst/`) is modified.
- [ ] No `.claude/**` or `.github/workflows/**` modified.
- [ ] `git diff --name-only` lists exactly: `tpcore/fmp/profile_adapter.py`, `scripts/ops.py`, `tests/test_p1b_cik_long_tail_fallback.py`, and optionally `tpcore/fmp/__init__.py` (only if package re-export is required).
- [ ] `python -m pytest -p no:xdist -q` (whole-suite serial) green.
- [ ] `python scripts/check_manifests.py` OK.
- [ ] `gitleaks detect` no leaks.
- [ ] PR body references this plan doc and spec PR #423.

## Implementation prompt

The next PR (heavy-lane execution PR) should be opened with the following task spec, ready for a fresh implementer:

> Implement P1b CIK long-tail FMP `/profile` fallback backfill per the merged spec at `docs/superpowers/specs/2026-06-01-p1b-cik-long-tail-backfill.md` (PR #423) and the merged plan at `docs/superpowers/plans/2026-06-01-p1b-cik-long-tail-backfill-plan.md` (PR \<this-PR\>).
>
> Scope is exactly the four files listed in §"Implementation boundaries → Touched". Honor every resolved decision in §"Resolved operator decisions". Do not open a re-litigation of any decision — escalate to a separate spec amendment instead.
>
> Heavy-lane (touches `scripts/ops.py` per `.claude/path_registry.yaml`). Follow `docs/DEV_PIPELINE_STANDARD.md` §1 step 7 (manual fresh-context review on the diff before merge). Run the whole-suite + order-flip authoritative gate per `.claude/rules/tests-and-ci.md` before push.
>
> Do not:
> - touch `tpcore/risk/**`, `tpcore/quality/validation/**`, `tpcore/providers.py`, `tpcore/engine_profile.py`, or any engine package
> - add a migration
> - run live FMP calls or live DB writes during implementation
> - introduce auto-merge, `--admin`, Docker, `railway up`, deployment, or Anthropic API write surface
> - widen the `cik_source` CHECK constraint
> - refactor `_stage_fmp_profile_backfill` or `_backfill_tkr14_via_fmp_profile` (P1b extracts the adapter for new callers only; existing inline-callers stay inline)
>
> Open the implementation PR titled `feat(sec): P1b — CIK long-tail FMP /profile fallback (impl)`. Body must reference PR #423 (spec) and \<this-PR\> (plan), declare heavy-lane, and check the §"Acceptance criteria" boxes.

## Post-merge live-smoke result — 2026-06-02

> Implementation PR #425 + ruff hygiene PR #426 merged 2026-06-01. The operator ran the three-step live-smoke sequence per §"Live smoke sequence" on 2026-06-02. **Result: implementation works correctly; the original hypothesis that FMP `/stable/profile` would resolve part of the 1,419 long-tail bucket is empirically NOT supported.**

### Steps run

| Step | Command knobs | Wall clock | Result |
|---|---|---|---|
| 1 | `dry_run=true do_fmp_fallback=true fmp_max_unresolved=25` | 9.6 s | `cik_fmp_fallback = {candidates: 25, resolved: 0, no_match: 25, fmp_error: 0, written: 0, divergence_events_written: 0}` |
| 2 | `dry_run=true do_fmp_fallback=true fmp_max_unresolved=100` | 32 s | `{candidates: 100, resolved: 0, no_match: 100, ...same shape}` |
| 3 (live) | `dry_run=false do_fmp_fallback=true fmp_max_unresolved=100` | 35.6 s | `{candidates: 100, resolved: 0, no_match: 100, written: 0, divergence_events_written: 0}`; `coverage_before == coverage_after` |

Coverage snapshot (steady-state across all three runs): `total=13,840 has_cik=10,239` → 1,419 unresolved (exactly matches the TODO entry).

### Safety-check outcomes (per spec §"Resolution states")

- ✅ No overwrite of existing CIK (0 writes total across 3 runs).
- ✅ No lifetime-ended row updated.
- ✅ No country writeback.
- ✅ `platform.application_log` `IDENTITY_DIVERGENCE_INVESTIGATE` rows introduced: 0 (consistent with 0 symbol-mismatch + 0 ambiguous-response across 225 FMP calls).
- ✅ FMP HTTP layer healthy (0 errors, 0 rate-limit hits, 0 timeouts).
- ✅ Adapter terminal-state distribution observed live matches the hermetic-test classification (`no_match` for all 225 calls; the other 5 states stay exercised by the unit tests).

### Empirical finding

The 1,419 SEC-ticker-map-unresolved tickers (`cik IS NULL AND country IS NULL` scope) are **also unresolvable by FMP `/stable/profile`** in the sampled 100-ticker prefix. FMP returns an empty list (`no_match`) for every requested ticker. The bucket appears to be composed of issuers neither SEC's public file nor FMP indexes — delisted / non-equity (warrants, units, preferred) / pink-sheet OTCs with no canonical CIK. P1b's adapter correctly classifies them as the spec's `no_match` honest dead end.

### Recommendation against running the uncapped full pass

The spec's §"Live verification strategy" envisioned a Step-4 full run after Steps 1–3 passed. Based on the 100-ticker sample, the full ~1,419-ticker run would:

- consume ~1,419 × 0.32 s ≈ 7.5 min of FMP quota
- produce ~0 writes with high statistical confidence
- not move the `metadata_coverage_low` coverage-gate threshold (currently 90% NULL; needs the full-active-universe `backfill_sec_metadata` run for that — different bucket)

**Do not run the full pass until a separate triage (TODO `P1c — unresolved-security-source triage`) produces evidence of an alternative source with non-zero hit rate.**

### Follow-up disposition (live in TODO.md)

- **P1b — DONE** (implementation), but the optimistic assumption embedded in the original framing is documented as empirically not supported.
- **P1c (NEW)** — unresolved-security-source triage. Probe FMP with different params, OpenFIGI `/v3/mapping`, SEC EDGAR full-text search, manual spot-check. Do NOT implement another stage extension until a non-zero-hit source is identified.
- **Metadata coverage gate — STILL OPEN.** P1b's path does not advance it. The full-active-universe `backfill_sec_metadata` run remains the highest-leverage next move for `DATA_OPERATIONS_COMPLETE` progress.

---

> Status: PLAN COMPLETE. Spec PR #423, plan PR #424, impl PR #425, ruff hygiene PR #426 — all merged. 2026-06-02 live smoke captured this section. Cross-references: spec PR #423 (`0748593`); existing P0-003 stage at `scripts/ops.py:2604`; existing FMP `/profile` call sites at `scripts/ops.py:4194` + `scripts/ops.py:6955`; SEC CIK map at `tpcore/sec/ticker_cik_map.py`; divergence-event protocol at `tpcore/identity/parent_resolver.py`; coverage gate at `tpcore/quality/validation/checks/fundamentals_quarterly_completeness.py`; schema CHECK at `platform/migrations/versions/20260530_0200_issuer_metadata_foundation.py:76-77`.
