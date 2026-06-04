---
name: hy-spread-sacred
description: "Operator 2026-05-23: 'i had to rebuild the hy spread so that is sacred'. The platform.macro_data series_id='hy_spread' (~7,674 rows 1996-12-31 → present; BAMLH0A0HYM2 FRED ID) is operator-rebuilt; never re-fetch / re-derive / overwrite historical rows. Incremental adds via the normal FRED puller cadence are fine; historical re-pulls or force_refresh modes that touch this series are FORBIDDEN. (Earlier ~9,097 count in this memory was wrong — that's credit_spread/BAA10Y. Corrected 2026-05-24 audit.)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing constraint (operator 2026-05-23):** *"i had to rebuild the hy spread so that is sacred"* + *"FRED only publishes a few years of it and i had to find it elsewhere and piece it together"*.

`platform.macro_indicators` rows where `indicator='hy_spread'` (7,674 rows from 1996-12-31 to 2026-05-21, per live count 2026-05-23) are an OPERATOR-CURATED, CROSS-SOURCE-STITCHED dataset. FRED itself only publishes a recent window (a few years). The pre-FRED-window history (~1996-2020+) was manually located by the operator FROM OTHER SOURCES and stitched into this single series.

**This dataset is irreplaceable from FRED alone.** If we lose the historical rows, the operator has to redo the manual multi-source archeology to reconstruct them.

Treat this series like the sacred byte-identical tests in sentinel — DO NOT modify the historical rows. Treat it with EVEN MORE care than the byte-identical tests because byte-identical tests can be regenerated from code; this dataset cannot be regenerated without the operator's manual labor.

## What this forbids

- Running `ops.py --stage macro_indicators --param force_refresh=true` (or equivalent) for hy_spread.
- Running `ops.py --stage historical_macro_indicators` (the bulk historical re-pull stage) without a `--param skip_indicators=hy_spread` override.
- Any Task #18 (macro_data consolidation) migration that re-derives hy_spread from FRED instead of copying the existing rows verbatim.
- Any TRUNCATE / DELETE of hy_spread rows.
- Any test (especially live-DB integration test) that overwrites or repulls hy_spread.

## What is allowed

- Incremental adds — the normal weekly/daily FRED puller cadence that APPENDS new dates is fine. Pin-at-first-resolve discipline: never overwrite an EXISTING row (the same discipline TKR-14's parent_resolver uses).
- Read-only queries — engines + Lab probes + backtests can SELECT freely.
- Data MOVE during Task #18 — the macro_data migration must COPY hy_spread rows verbatim from macro_indicators → macro_data, not re-derive from FRED. The bitemporal `realtime_start` / `realtime_end` columns should preserve the historical recorded_at values, not stamp now().

## How to enforce

When designing any stage / migration / test that touches `macro_indicators`:

1. Grep the SQL/code for `hy_spread` references.
2. If WRITE access is implied (UPDATE / DELETE / INSERT ... ON CONFLICT DO UPDATE), add a WHERE guard `AND indicator != 'hy_spread'` OR a feature flag the operator must explicitly set to bypass.
3. For Task #18 macro_data migration: the backfill copy SQL must be:
   ```sql
   INSERT INTO platform.macro_data (source, series_id, observed_date, value_num, recorded_at, realtime_start, realtime_end)
   SELECT 'fred', indicator, date, value, recorded_at, recorded_at, 'infinity'
   FROM platform.macro_indicators
   WHERE indicator IS NOT NULL
   ON CONFLICT DO NOTHING;
   ```
   No re-derivation, no recomputation — straight copy.

## Why "sacred"

The operator paid the cost of stitching this together once from multi-source archeology that FRED's API alone cannot replay. Asking them to pay it again because a stage / migration silently overwrote 7,674 rows is exactly the class of failure that erodes trust in the platform.

This is stronger than the sentinel byte-identical tests (those can be regenerated from code). The hy_spread historical rows have NO algorithmic source — they came from operator-side manual stitching. Reconstruction requires:
1. Re-locating the non-FRED historical source(s) (likely BAML / Bloomberg / academic archives)
2. Verifying the values match the methodology FRED uses now
3. Re-importing + reconciling against the FRED-window rows
4. Hours-to-days of operator labor

The CI gates + the partial-write defenses in the FRED puller should never silently overwrite this dataset.

## Related

- [[verify-expert-verdict-in-codebase-first]] — sibling: verify ALL drop / overwrite recommendations against operator-paid baselines
- [[no-shortcuts-100-pct]] — sibling: don't trade operator-paid data for "convenience" of a re-pull
- [[macro-consumer-audit-2026-05-23]] — companion: lists hy_spread among the load-bearing macro consumers (sentinel + stelib fear_greed formula)
- v2.2 plan / Task #18 (macro_data consolidation) — must respect this constraint in the migration
