# Task #18 — macro_data tall-table consolidation

**Status:** SPEC v1. Supersedes the per-table `macro_indicators` / `aaii_sentiment` / `fear_greed` triad on the schema dimension only; runtime consumers stay live via shadow-read VIEWs during the cutover window.

**Author / role:** in-thread session 87291947 with `general-purpose` expert verdict on tall-schema design + an exhaustive macro-consumer codebase audit (`project_macro_consumer_audit_2026_05_23`).

**Provenance:** Task #18 was the carve-out non-goal in v2.1 / v2.2 plans. Spec lands now per operator directive 2026-05-23 ("convert the macro tables into a tall table and consolidate"). The target table name `macro_data` was set in v2 spec: *"Macro consolidation (`macro_indicators` / `aaii_sentiment` / `fear_greed` → `macro_data`) — Task #18, stays SEPARATE."*

---

## 1. Why consolidate

Current state: 3 macro tables on 3 different schemas.

| Table | Shape | Rows (2026-05-23) |
|---|---|---|
| `platform.macro_indicators` | TALL `(indicator, date, value)` | 51,323 (after NFCI add) |
| `platform.aaii_sentiment` | WIDE `(date, bullish_pct, bearish_pct, neutral_pct)` | 2,024 |
| `platform.fear_greed` | WIDE + heterogeneous (`date, score, label, direction, components×4, score_5d_ago`) | 6,638 |

Problems:
- Three query patterns for the same domain (macro time-series). Engines + Lab probes + dashboard panels duplicate join logic.
- No common point-in-time (bitemporal) story. `recorded_at` exists per table but the schemas drift on how revisions are handled.
- Adding new series requires either bloating macro_indicators (which is already tall) OR creating a new wide table (high friction).
- Operator wants to publish (`publishing/stelib/`) — consumers of a one-table API are easier to support than a three-table API.

Consolidation makes:
- one **table**, one **query pattern**, one **API**
- bitemporal point-in-time + vendor-revision auditing baked in
- one place to add the next 4 expert-recommended candidates (MOVE, put/call, TED/SOFR, EPU) without bloat
- stelib's `fear_greed.py` (4-component derivation) becomes a one-table read

## 2. Target schema (DDL)

```sql
CREATE TABLE platform.macro_data (
    source         text        NOT NULL,           -- 'fred','aaii','cnn_fear_greed','cboe' etc.
    series_id      text        NOT NULL,           -- 'vix','nfci','aaii.bullish_pct','fg.score' etc.
    observed_date  date        NOT NULL,           -- valid-time (when the fact was true)
    value_num      numeric                NULL,    -- numeric channel
    value_text     text                   NULL,    -- categorical channel ('Greed', 'rising')
    realtime_start timestamptz NOT NULL DEFAULT now(),  -- transaction-time start
    realtime_end   timestamptz NOT NULL DEFAULT 'infinity',
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT value_xor CHECK (
        (value_num IS NOT NULL)::int + (value_text IS NOT NULL)::int = 1
    ),
    CONSTRAINT pit_pk PRIMARY KEY (source, series_id, observed_date, realtime_start)
);

-- Hot read: "latest known value of series X on date D"
CREATE INDEX ix_macro_data_latest
    ON platform.macro_data (series_id, observed_date DESC)
    WHERE realtime_end = 'infinity';

-- Point-in-time queries: "as of date T, what did we know?"
CREATE INDEX ix_macro_data_pit
    ON platform.macro_data USING GIST
       (series_id, tstzrange(realtime_start, realtime_end));

-- Source-bucket filter (e.g. show-all-FRED dashboard panel)
CREATE INDEX ix_macro_data_source
    ON platform.macro_data (source, observed_date DESC)
    WHERE realtime_end = 'infinity';
```

Schema rationale (per expert verdict, citing FRED/ALFRED bitemporal model + Kimball degenerate-dimension + TimescaleDB narrow model):

- **`source` + `series_id` as wide key** (not EAV — the attribute IS the series). Two columns, not concat: enables `WHERE source = 'fred'` filters + tolerates two providers publishing the same series_id without name-mangling.
- **`value_num` + `value_text` with XOR CHECK** — escapes EAV's typed-storage pain (Cybertec pattern). Numeric stays numeric for arithmetic pushdown; categorical (`'Greed'`, `'rising'`) gets its own column without JSON parse hop.
- **`realtime_start` / `realtime_end`** — bitemporal pair mirroring FRED's ALFRED `realtime_period` model. Latest row has `realtime_end = 'infinity'`; revisions close the old row + insert a new one with `realtime_start = now()`.
- **Partial UNIQUE not added** — the PK `(source, series_id, observed_date, realtime_start)` already enforces no-dup on the bitemporal triple. Multiple revisions for the same `(source, series_id, observed_date)` differ on `realtime_start`.

## 3. Source-row mapping

| Source row | → `(source, series_id, observed_date, value_num, value_text)` |
|---|---|
| `macro_indicators('vix','2026-05-22',18.4,recorded_at=R)` | `('fred','vix','2026-05-22',18.4,NULL)` `realtime_start=R, realtime_end='infinity'` |
| `macro_indicators('nfci','2026-05-15',-0.45,...)` | `('fred','nfci','2026-05-15',-0.45,NULL)` |
| `macro_indicators('hy_spread','2010-06-15',5.21,...)` | `('fred','hy_spread','2010-06-15',5.21,NULL)` **— SACRED; verbatim copy, no re-derive** |
| `macro_indicators('sos_state_diffusion','2024-12-01',0.18,...)` | `('fred','sos_state_diffusion','2024-12-01',0.18,NULL)` — derived; preserved as-is |
| `macro_indicators('phci_ca','2024-12-01',132.5,...)` × 50 states | `('fred','phci_ca','2024-12-01',132.5,NULL)` × 50 (each series_id) |
| `aaii_sentiment('2026-05-22', 38.1, 31.2, 30.7, R)` | three rows: `('aaii','bullish_pct',d,38.1,NULL)`, `('aaii','bearish_pct',d,31.2,NULL)`, `('aaii','neutral_pct',d,30.7,NULL)` |
| `fear_greed('2026-05-22', 62, 'Greed', 'rising', 58, 65, 70, 55, 60, R)` | eight rows: `('cnn_fear_greed','score',d,62,NULL)`, `…'label',d,NULL,'Greed'`, `…'direction',d,NULL,'rising'`, `…'score_5d_ago',d,58,NULL`, plus the four components (`volatility_component`, `credit_component`, `momentum_component`, `safe_haven_component`) each as numeric |

**hy_spread invariant** (per `project_hy_spread_sacred`): the migration MUST `INSERT … SELECT` from `macro_indicators` directly, copying `value` and `recorded_at` verbatim. It MUST NOT re-pull from FRED (FRED only publishes a rolling window; the historical 1996-2010 rows are operator-curated from non-FRED sources and irreplaceable).

## 4. Consumer compatibility — backward shim via VIEWs

The migration ADDs `macro_data` first, then creates three updatable views that pivot tall→wide for legacy consumers:

```sql
-- Shim 1: macro_indicators (already tall — view is a 1:1 select from macro_data)
CREATE VIEW platform.macro_indicators_v AS
SELECT series_id AS indicator, observed_date AS date, value_num AS value, recorded_at
FROM platform.macro_data
WHERE source = 'fred' AND realtime_end = 'infinity';

-- Shim 2: aaii_sentiment (tall → wide via aggregation)
CREATE VIEW platform.aaii_sentiment_v AS
SELECT
    observed_date AS date,
    MAX(value_num) FILTER (WHERE series_id = 'bullish_pct') AS bullish_pct,
    MAX(value_num) FILTER (WHERE series_id = 'bearish_pct') AS bearish_pct,
    MAX(value_num) FILTER (WHERE series_id = 'neutral_pct') AS neutral_pct,
    MAX(recorded_at) AS recorded_at
FROM platform.macro_data
WHERE source = 'aaii' AND realtime_end = 'infinity'
GROUP BY observed_date;

-- Shim 3: fear_greed (tall → wide via aggregation; mirror existing 9 columns)
CREATE VIEW platform.fear_greed_v AS
SELECT
    observed_date AS date,
    MAX(value_num) FILTER (WHERE series_id = 'score') AS score,
    MAX(value_text) FILTER (WHERE series_id = 'label') AS label,
    MAX(value_text) FILTER (WHERE series_id = 'direction') AS direction,
    MAX(value_num) FILTER (WHERE series_id = 'score_5d_ago') AS score_5d_ago,
    MAX(value_num) FILTER (WHERE series_id = 'volatility_component') AS volatility_component,
    MAX(value_num) FILTER (WHERE series_id = 'credit_component') AS credit_component,
    MAX(value_num) FILTER (WHERE series_id = 'momentum_component') AS momentum_component,
    MAX(value_num) FILTER (WHERE series_id = 'safe_haven_component') AS safe_haven_component,
    MAX(recorded_at) AS recorded_at
FROM platform.macro_data
WHERE source = 'cnn_fear_greed' AND realtime_end = 'infinity'
GROUP BY observed_date;
```

Consumer cutover sequence (Fowler expand-contract):

1. **Expand:** Phase A — create `macro_data` + 3 shim VIEWs (different names from the live tables, e.g. `_v` suffix); producers double-write to old + new.
2. **Verify parity:** for one full cadence cycle (a week), assert row-by-row equivalence between live tables and views.
3. **Cutover:** Phase B — rename live tables (`macro_indicators` → `_legacy`, etc.); rename shim views to the original names (`macro_indicators_v` → `macro_indicators`); producers stop double-writing.
4. **Quiet period:** one PAPER-engine week with the shim VIEWs serving consumers.
5. **Contract:** Phase C — drop `_legacy` tables; the consumer migration is done.

Consumer-side code migration (cleanup, can lag the schema migration):

- Engines + Lab + dashboard switch from `SELECT … FROM platform.macro_indicators` → `SELECT … FROM platform.macro_data WHERE source='fred' AND series_id=…`
- Per `project_macro_consumer_audit_2026_05_23`, the affected files are: sentinel/{backtest,models,plugs/setup_detection}.py, reversion/{regime_filter,backtest,plugs/setup_detection}.py, vector/plugs/{setup_detection,execution_risk,lifecycle_analysis}.py, catalyst/tests/test_lab_macro_expansion_*.py, tpcore/lab/llm_finder/snapshot.py, tpcore/indicators/fear_greed.py, tpcore/backtest/filter_diagnostics.py, publishing/stelib/stelib/indicators/fear_greed.py.

## 5. Phase plan

| Phase | What | Wall-clock |
|---|---|---|
| P0 | This spec + plan PR | spec done; plan ~1 hr |
| P1 | Alembic migration `20260525_0000_create_macro_data.py` — CREATE TABLE + 3 indexes + 3 shim VIEWs (`_v` suffix, parallel to live tables) | 30 min |
| P2 | Backfill — `ops.py --stage macro_data_backfill` — chunked INSERT-SELECT from each of 3 source tables. `hy_spread` row-by-row verbatim copy. Estimated total rows ~120K (sub-second per chunk). | 1 hr build + ~5 min run |
| P3 | Producer double-write — modify `handle_macro_indicators`, `handle_aaii_sentiment`, `handle_fear_greed` to write BOTH old table AND `macro_data`. Hold for one full cadence cycle. | 2-3 hr |
| P4 | Parity verification — `tests/test_macro_data_parity.py` asserts row-by-row equivalence between live tables and `macro_data` for one week of double-writes | 2 hr |
| P5 | Cutover — rename: `macro_indicators` → `macro_indicators_legacy`, `aaii_sentiment` → `aaii_sentiment_legacy`, `fear_greed` → `fear_greed_legacy`; rename shim views to take the original names. Producers stop double-writing. | 30 min migration + verify |
| P6 | Consumer migration — engines + lab + tpcore/indicators + publishing/stelib switch to direct `macro_data` reads. Many small PRs; each file's switch is mechanical. | 1-2 weeks |
| P7 | Contract — drop `*_legacy` tables (Alembic migration). Quiet-period gate: one PAPER-engine week green after P6 completes. | 30 min |

Total: 2-3 weeks single-operator wall-clock, dominated by P6 consumer migrations.

## 6. Per-consumer migration impact (per the audit)

For each consumer the migration shape is:

```python
# Before:
SELECT date, value FROM platform.macro_indicators WHERE indicator = $1

# After:
SELECT observed_date AS date, value_num AS value FROM platform.macro_data
WHERE source = 'fred' AND series_id = $1 AND realtime_end = 'infinity'
```

Per consumer:
- **sentinel** (5 files) — straight substitution. Byte-identical tests should pass (value unchanged).
- **reversion regime_filter** — same.
- **vector plugs** (3 files) — VIX read path; same.
- **catalyst Lab macro-expansion candidate** — consumes regime_bundle (already abstracted); no direct macro read change needed.
- **stelib/indicators/fear_greed** — 4-component derivation reads `vix`, `hy_spread`, `yield_curve` + SPY. Switch the 3 FRED reads.
- **tpcore/indicators/fear_greed** — same as stelib.
- **tpcore/backtest/filter_diagnostics** — broadly-shared library; switch + sentinel byte-identical tests should pass.
- **tpcore/lab/llm_finder/snapshot** — context snapshot; switch.

## 7. New indicators land via this same surface

After P5 cutover, adding any new series is trivial:

```python
# In tpcore/fred/adapter.py (or new adapter for non-FRED sources):
INDICATOR_SERIES = (
    ("nfci", "NFCI"),
    ("move_index", "BMOVE3M"),       # MOVE Index — expert recommendation
    ("put_call_ratio", "CBOE_PCR"),  # CBOE put/call — needs adapter
    ("ted_spread", "TEDRATE"),       # FRED TED (deprecated; alt: SOFR-OIS)
    ("epu_index", "USEPUINDXD"),     # Baker-Bloom-Davis EPU
    ...
)

# Insert lands as: ('fred','move_index',date,value,NULL)
```

No schema change needed. The validation check's `EXPECTED_INDICATORS` extends; that's the only friction.

## 8. Sacred constraints + safeguards

1. **`hy_spread` is sacred.** Per `project_hy_spread_sacred`: P2 migration must COPY hy_spread rows verbatim from `macro_indicators` to `macro_data`. NEVER re-derive from FRED (FRED only publishes a rolling ~3-year window; pre-FRED-window history was operator-stitched from non-FRED sources). Test: `tests/test_macro_data_hy_spread_preservation.py` — asserts every row in `macro_indicators WHERE indicator='hy_spread'` has a 1:1 match in `macro_data WHERE source='fred' AND series_id='hy_spread'` with identical `value` and `recorded_at`.
2. **sos_state_diffusion sacred-ish.** Sentinel bear-score Lab anchor; 2 byte-identical tests in sentinel. Same verbatim-copy rule.
3. **50 PHCI series stay.** Substrate for sos_state_diffusion derivation; verbatim copy.
4. **value_num precision preserved.** No silent rounding during INSERT-SELECT.
5. **realtime_start = source.recorded_at** for backfilled rows (NOT `now()`) — preserves the original transaction-time correctly.

## 9. Operator-facing changes

- **None during P0-P4.** Consumers still read the live tables.
- **P5 cutover is the operator-visible moment.** Existing queries continue to work (shim views took the original names); but the underlying engine is now `macro_data`.
- **Post-P6:** consumers query `macro_data` directly. The shim views can stay as a thin compatibility layer or be dropped.
- **DFCR:** no new provider added (FRED + AAII + CNN Fear/Greed are all existing); FeedProfile entries unchanged. ProviderBinding registry unchanged.

## 10. Disk impact

- `macro_data` will have ~120K rows (51K macro_indicators + ~6K aaii expanded × 3 = 18K aaii + ~6.6K fear_greed × 8 = ~52K fear_greed = ~120K total).
- Per row: ~80 bytes (3 short text cols + 1 numeric + 3 timestamps + check overhead). Total ~10 MB raw + ~5-10 MB for indexes = ~20 MB.
- During P3 double-write window: legacy tables stay + new macro_data exists = peak +20 MB.
- After P7 drops legacy: net -20-30 MB (reclaims macro_indicators + aaii_sentiment + fear_greed footprint, minus the macro_data overhead).

Trivial impact relative to the 8 GB Pro plan + the 4.3 GB prices_daily.

## 11. References

- v2.2 spec / plan — listed Task #18 as carve-out non-goal
- v2 spec — original `macro_data` target name
- Expert verdict (general-purpose subagent, 2026-05-23) — bitemporal schema design
- `project_macro_consumer_audit_2026_05_23` — exhaustive consumer map
- `project_hy_spread_sacred` — sacred-data preservation invariant
- `project_supabase_constraints_2026_05_23` — chunked-DML mandate for the backfill
- FRED/ALFRED bitemporal model — <https://alfred.stlouisfed.org/>
- Kimball SCD Type-2 — surrogate key + as-of-date dimension pattern
- TimescaleDB narrow model — recommended for sparse/heterogeneous time-series
- Cybertec EAV escape — typed-column XOR pattern over generic EAV
- Fowler expand-contract — migration pattern

## 12. Out of scope

- TimescaleDB hypertable migration (deferred; would need extension install + 10M+ row scale to matter)
- Column-store compression (deferred; same trigger)
- `series_catalog` companion table with `description`/`unit`/`frequency` (deferred; useful for discoverability but not load-bearing)
- Adding the other 4 expert-recommended series (MOVE, put/call, TED/SOFR, EPU) — each gets its own follow-up via the same `add-series` pattern
- Multi-provider parity for the same series_id (e.g., CBOE vs FRED-mirrored VIX) — folds in naturally via the `source` segment but not implemented in this spec
