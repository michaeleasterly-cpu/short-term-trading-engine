# Engine ↔ Postgres Abstraction Layer (2026-05-25)

**Status:** SHIPPED — 21 PRs (#335..#362). All 6 production engines + AAR write/read path + indirect helpers now read through a typed, classification_id-keyed abstraction.

**Context:** v2.2 schema refactor (2026-05-24, the other session) swapped natural-key (`ticker`) primary keys for TKR-14 surrogate IDs (`ticker_classifications.id`) and added `ticker_history` for SCD-2 rename tracking. Engines previously embedded raw `ticker = ANY($1)` SQL everywhere. The operator's directive: "we need to wire the engines to use the updated schema… i really dont want the engines to query the database directly but be abstracted from it" (2026-05-24).

---

## Design — 3 layers + the AAR wedge

### 1. `IdentityDispatcher` — the only place ticker ↔ classification_id translates

- `tpcore/identity/dispatcher.py`
- Methods: `ticker_to_classification_id(ticker, as_of=None) → str | None`, `classification_id_to_ticker(cid, as_of=None) → str | None`, `invalidate(...)`, `reset_shared_caches()`
- TTL+LRU cache, **shared across all instances holding the same pool** (keyed on `id(pool)`) — engine callsites that construct `IdentityDispatcher(pool)` per function call still benefit from cross-call caching
- Backed by `platform.ticker_history` SCD-2; `valid_to IS NULL` is "currently active"; date param applies `valid_from <= as_of <= valid_to`
- Emits structlog DEBUG on `None` resolution for observability of unknown tickers
- Wire-IN boundaries: CSV replay, Alpaca fill confirmation, manual operator ticker
- Wire-OUT boundaries: Alpaca order submission, dashboard render, log emission, AAR ticker snapshot

### 2. `platform.v_universe` — the only schema artifact

- Single CREATE VIEW migration: `platform/migrations/versions/20260524_2000_engine_abstraction_universe_view.py`
- Joins `ticker_classifications` × `ticker_history` × `liquidity_tiers`
- Columns: `classification_id, ticker_at_date, current_ticker, asset_class, country, status, liquidity_tier, valid_from, valid_to`
- Reversible (DROP VIEW downgrade); no table touched; no trigger
- Single source of truth for "what's in the universe right now / at a date"

### 3. Read repositories in `tpcore/data/repositories/`

Every method takes `classification_id` (or `series_id` for macro), returns frozen Pydantic v2 row models. asyncpg + raw SQL (no ORM — preserves tight scoring-loop performance). Auto-chunked at 500 cids for large tables; Supabase-recovery middleware on prices + fundamentals + insider.

| Repo | Table | Pydantic model | Key methods |
|---|---|---|---|
| `UniverseRepo` | `platform.v_universe` | `UniverseRow` | `enumerate(as_of, max_liquidity_tier, asset_class, country, include_untracked_liquidity)` |
| `PricesRepo` | `platform.prices_daily` | `Bar` (OHLCV) | `get_window`, `get_window_batch`, `latest_at_or_before_batch` |
| `MacroRepo` | `platform.macro_data` | `MacroObservation` | `get_window`, `get_window_batch(source=…)`, `get_latest_as_of` |
| `FundamentalsRepo` | `platform.fundamentals_quarterly` | `QuarterlyFundamentals` | `get_window`, `get_window_batch`, `get_quarterly_pit`, `funded_subset`, `cids_with_value_factors` |
| `EarningsRepo` | `platform.earnings_events` | `EarningsEvent` | `get_window`, `get_window_batch`, `get_beats`, `cids_with_event_type` |
| `InsiderRepo` | `platform.insider_transactions` | `InsiderTransaction` | `get_window`, `get_window_batch` |

### 4. AAR cid wedge

PR-12 wired `AARWriter` to populate `aar_events.classification_id` via `IdentityDispatcher` at write time — durable surrogate FK alongside the human-readable ticker snapshot. PR-17 added `classification_id` to `AARRow` + a `fetch_by_classification_id(cid)` cross-engine query. Event-sourcing canonical: events carry both the durable FK and the at-the-time natural key.

---

## Edge-adapter pattern — zero blast radius migration

Engines were converted with the public function signature **unchanged** — ticker in, ticker out. Internally the function dispatches to cid, calls the repo, and maps results back to ticker. Callers (engine plugs, schedulers, backtest drivers) saw no contract change.

```python
async def _fetch_prices(pool, *, universe: tuple[str, ...], start, end) -> dict[str, pd.DataFrame]:
    """Edge adapter: ticker universe in, ticker-keyed DataFrame out."""
    dispatcher = IdentityDispatcher(pool)
    repo = PricesRepo(pool)
    cid_to_ticker = {await dispatcher.ticker_to_classification_id(t): t for t in universe}
    cid_to_ticker = {k: v for k, v in cid_to_ticker.items() if k is not None}
    bars_by_cid = await repo.get_window_batch(list(cid_to_ticker), start, end)
    return {cid_to_ticker[cid]: _to_dataframe(bars) for cid, bars in bars_by_cid.items()}
```

When engines fully internalize cid in their own state (separate future migration), the signature drops the ticker.

---

## Why classification_id internally — industry precedent

Operator pushed back on the warehouse-pattern default ("surrogate inside storage, natural at app boundary"). For OLTP system components (which engines are), the **surrogate IS the engine's identity primitive**:

- **Bloomberg FIGI** exists for exactly this reason — "Once a FIGI is assigned, it never changes throughout the trade lifecycle… An instrument's FIGI never changes as a result of any corporate action" (OpenFIGI Overview).
- **QuantConnect LEAN** `Common/Symbol.cs`: `// only SID is used for comparisons`. Algorithms hold the SID; `Value` exposes current ticker for display only.
- **Zipline** integer `sid` is the open-source canonical example.
- **Evans / Vernon / Fowler DDD**: an entity's identity is the surrogate; natural attributes are mutable fields. Khorikov: "Natural primary keys are not a good fit for representing an identity precisely because they tend to change over time."

Ticker is decoration. Surrogate is identity. Engines carry cid; edges translate.

---

## What stays raw SQL (the platform-overlay allowlist)

Engines and engine-adjacent helpers were converted **only where the table is engine-facing validation-gated feed data**. Service-internal state tables stay raw SQL in their owning service:

| Table | Owner | Why raw |
|---|---|---|
| `aar_events` | AARWriter / AARReader | Repo-like wrapper exists (PR-12 + PR-17), but the wrapper-as-state-store stays direct SQL — it owns the table |
| `application_log` | DBLogHandler / engine schedulers | Log substrate, not feed data |
| `data_quality_log` | quality validation framework | Credibility / validation state |
| `forensics_triggers` | forensics service | Service-internal |
| `parity_drift_log` | parity harness | Service-internal |
| `open_orders` | OrderManagement | Order state |
| `risk_state` | RiskGovernor | Risk state |
| `universe_candidates` | prescreener daemon | Pre-computed daily candidates |
| `allocations` | AllocatorService | Allocator state |

These are documented in `_PROFILE.data_dependencies` as a platform-overlay allowlist consulted by `test_engine_data_dependencies_drift` so the gate doesn't false-positive on them.

---

## The 21 PRs

### Foundation (PR-1..6)

| PR | # | What |
|---|---|---|
| 1 | [#335](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/335) | `IdentityDispatcher` + 13 tests |
| 2 | [#336](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/336) | `platform.v_universe` view + `UniverseRepo` + 10 tests |
| 3 | [#337](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/337) | `PricesRepo` + `MacroRepo` + 14 tests |
| 4 | [#338](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/338) | catalyst `_fetch_prices` — proof of pattern (1 callsite) |
| 5 | [#339](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/339) | 5 macro consumers refactor (sentinel + snapshot.py) onto MacroRepo |
| 6 | [#341](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/341) | `FundamentalsRepo` + `EarningsRepo` + `InsiderRepo` + 29 tests |

### Per-engine conversions (PR-7..11, 16)

| PR | # | Engine | Callsites |
|---|---|---|---|
| 7 | [#343](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/343) | catalyst (remaining 4: insider × 2, prices, earnings) — also fixed `sec_insider_transactions` rename bug |
| 8 | [#344](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/344) | sentinel — basket ETF prices + SPY + as-of-latest scheduler |
| 9 | [#346](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/346) | reversion — fundamentals (3 callsites) |
| 10 | [#347](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/347) | vector — fundamentals + earnings + universe primitive (8 callsites) |
| 11 | [#348](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/348) | momentum backtest (3 callsites; scheduler initially deferred) |
| 16 | [#357](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/357) | momentum live-path setup_detection plug (3 of 4; `universe_candidates` left allowlisted) |

### Engine-adjacent helpers (PR-13, 14)

| PR | # | What |
|---|---|---|
| 13 | [#353](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/353) | `tpcore/backtest/price_loader.py` (reversion + vector backtest indirect path) |
| 14 | [#355](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/355) | `tpcore/data/postgres_data_adapter.py` (reversion + vector scheduler indirect path) |

### AAR cid wedge (PR-12, 17)

| PR | # | What |
|---|---|---|
| 12 | [#352](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/352) | `AARWriter` populates `aar_events.classification_id` via dispatcher |
| 17 | [#358](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/358) | `AARReader` exposes `classification_id` + `fetch_by_classification_id(cid)` cross-engine query |

### Drift + cleanups (PR-15, 18, 19, 20, 21)

| PR | # | What |
|---|---|---|
| 15 | [#356](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/356) | 3 ECR data_dependencies syncs (catalyst, momentum, sentinel) — drift test goes green |
| 18 | [#359](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/359) | byte-equivalent test pinned literal (review-found block) |
| 19 | [#360](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/360) | dispatcher shared cache + silent-drop logging + ECR doc fix |
| 20 | [#361](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/361) | macro SQL bind reorder + AAR reader dead try/except |
| 21 | [#362](https://github.com/michaeleasterly-cpu/short-term-trading-engine/pull/362) | allocator AAR fake + conftest dispatcher cache reset |

---

## Defects fixed in flight

The conversion work surfaced bugs from the parallel session's v2.2 + Task #18 migrations:

1. **`sec_insider_transactions` rename** — table renamed to `insider_transactions` in v2.2 phase 1; catalyst still queried the old name. Fixed in PR-7 (InsiderRepo reads the new name).
2. **`macro_indicators` / `aaii_sentiment` / `fear_greed` dropped** — Task #18 P7 (commit a6935d2) dropped these tables; 5 consumers in sentinel + `tpcore/lab/llm_finder/snapshot.py` still queried them. Fixed in PR-5 (MacroRepo reads `macro_data`).
3. **`_PROFILE.data_dependencies` drift** — engines after PR-7/8/11 declared the wrong substrate tables. Fixed via 3 accuracy-only ECR MODIFYs in PR-15.

---

## Review outcomes (2026-05-25)

Both code-quality + spec-conformance reviewers: **GO-WITH-EDITS**.

**Block addressed:** byte-equivalent test pinned literal (PR-18).

**Concerns addressed:**
- Dispatcher per-callsite allocation → class-level shared cache (PR-19)
- Silent ticker drops → structlog DEBUG (PR-19)
- Stale momentum ECR text (PR-19)
- Macro SQL `$4` bind position cosmetic (PR-20)
- Dead `try/except` in AAR reader (PR-20)
- `_FakeConn` test pollution from id() recycling (PR-21)

**Deferred follow-ups (low value or pre-existing):**
- `PostgresDataAdapter.get_universe_symbols` raw SQL — statement timeout on v_universe alternative; raw SQL is perf-correct
- Untyped `pool: asyncpg.Pool` params — project-wide convention sweep
- FundamentalsCache Step B (drop ticker accessor) — cache adds FMP fallback + backfill; not redundant
- `tpcore/backtest/spread_estimator.py` raw SQL — complex CTE, no clean repo expression
- `test_run_backtest_persists_credibility_rubric` order-dependent flake — pytest-monkeypatch `sys.modules` contamination from earlier tests; **predates this stack**

---

## Test posture

- 2647 / 2649 pass on the cumulative single-process suite (1 skipped, 1 pre-existing flake)
- 472+ pass across the engine-abstraction-impacted suites
- All gates green locally: ruff / format / vulture / gitleaks
- Drift test green; byte-equivalent test green

---

## What changed in the database

**One** migration: `20260524_2000_engine_abstraction_universe_view.py` — pure `CREATE OR REPLACE VIEW platform.v_universe`. Reversible. No tables touched. The view computes on-demand (not materialized); minimal catalog footprint (< 1 KB SQL definition).

All other schema work (v2.2 PK swap, ticker_history, BEFORE INSERT triggers, Task #18 macro consolidation, AAR classification_id column, sec_insider_transactions rename) was the **other session's** lane.

---

## Cross-session coordination

Cross-agent memstore handoffs:
- `/handoffs/2026-05-24-engine-abstraction-session-startup.md` — startup briefing
- `/cross-agent/engine-to-data/2026-05-24-macro-tables-dropped-broke-5-consumers.md` — surfaced the macro_indicators consumer breakage
- 3 ECR files under `docs/superpowers/ecrs/2026-05-25/` — data_dependencies sync

---

## Files of record

```
# Foundation
tpcore/identity/dispatcher.py
tpcore/data/repositories/
  __init__.py
  universe.py + tests/test_universe.py
  prices.py + tests/test_prices.py
  macro.py + tests/test_macro.py
  fundamentals.py + tests/test_fundamentals.py
  earnings.py + tests/test_earnings.py
  insider.py + tests/test_insider.py
platform/migrations/versions/20260524_2000_engine_abstraction_universe_view.py

# AAR cid
tpcore/aar/writer.py  (PR-12)
tpcore/aar/reader.py  (PR-17)

# Engine conversions
catalyst/{backtest,scheduler}.py
sentinel/{backtest,scheduler}.py + sentinel/plugs/setup_detection.py
reversion/backtest.py
vector/{backtest,scheduler}.py
momentum/{backtest}.py + momentum/plugs/setup_detection.py

# Engine-adjacent
tpcore/backtest/price_loader.py
tpcore/data/postgres_data_adapter.py

# ECRs (audit trail)
docs/superpowers/ecrs/2026-05-25/
  catalyst_data_deps_sync.txt
  momentum_data_deps_sync.txt
  sentinel_data_deps_sync.txt
```

---

## Operator directives reflected in this work

- "engines should not query the database directly but be abstracted from it" → 3-layer abstraction
- "we have an internal id that connects everything now and not the ticker" → engines carry classification_id, ticker at edges
- "i would rather refactor the engine and use a shared repo or view, not adjust the view to fit the old model" → no shim views for `macro_indicators`; consumers refactored onto MacroRepo
- "no bandaids" → MacroRepo reads `macro_data` directly; no back-compat layer to ape the dropped names
- "you can only modify views… never the database tables or schema for this particular session" → single CREATE VIEW migration; all other schema work was the other session
- "momentum is paper trading the rest of them are not" → momentum scheduler initially gated; PR-16 converted when operator greenlit
- "canary engine is a test engine… does a little trade each day" → canary intentionally untouched (heartbeat)
- "do this" (drift + momentum live path) → PR-15 + PR-16 shipped
