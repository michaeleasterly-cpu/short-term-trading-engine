---
name: refint-arc-state-2026-05-31
description: Evidence-based fundamentals/lifecycle arc P0→P2c shipped 2026-05-30/31; structural state worth checking before resuming related work
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

The five-commit refint arc closing "no validator suppression, no
country-based classification, evidence over heuristics" — shipped
2026-05-30 (P0/P1/P2a/P2b) + 2026-05-31 (P2c + the .get fix).

**Canonical work log:** `TODO.md` H2 section "⚑ Evidence-based
fundamentals/lifecycle arc (2026-05-30 → 2026-05-31)" — full per-PR
details + remaining-work list. Read TODO.md FIRST when resuming.

**Structural state worth knowing before related work:**

- **Migrations head: `20260530_0300`**. ticker_classifications grew
  by 13 cols (8 P0 metadata + 5 P2a lifecycle). New table
  `platform.ticker_lifecycle_events` (append-only audit log).
- **Live DB coverage** (as of 2026-05-31):
    * `sec_document_type_primary` populated: 362 / 13,840 (2.6%)
    * `issuer_lifecycle_state` populated: 544 (208 deregistered +
      336 delist_effective)
    * `ticker_lifecycle_events`: 1,129 rows
- **Bulk SEC cache lives at `data/sec_submissions/`** (gitignored,
  922 CIKs cached, ~1.3 GB). Path layout mirrors SEC URL:
  `CIK<10digit_zero_padded>.json`. SEC 404s cached as the sentinel
  `{"__sec_404__": true}` so re-runs short-circuit. **Future work
  reading SEC submissions for any of these 922 CIKs should use
  `SECCompanyFactsAdapter.get_submissions_cached(cik)` to hit the
  cache instead of re-pulling.** Cache pattern lives in
  `tpcore/sec/companyfacts_adapter.py`. Honors `TP_DATA_DIR` env.

**Three open follow-ups (detail in TODO.md):**
1. **P1b — CIK long tail.** 1,419 SEC-ticker-map-unresolved names
   need an FMP `/profile` fallback in a new backfill_sec_metadata
   sub-stage.
2. **P2c+ — 8-K Item 3.01 extractor** for `delist_pending` state.
   `tpcore/sec/corp_events_extractor.py` already does 8-K 1.01/2.01/
   1.02/1.03 parsing; extending to 3.01 is a small lift but needs
   spec on what delist_pending means for the capital gate (today
   the P2c gate ALLOWs delist_pending — Form 25 not effective yet).
3. **Metadata coverage gate.** The 90% NULL doctype rate fires the
   P1 structural sentinel, blocking DATA_OPERATIONS_COMPLETE. To
   clear: `python scripts/ops.py --stage backfill_sec_metadata
   --param dry_run=false` against the full universe (~14 min cold,
   ~30 sec cached on re-run thanks to the bulk-cache).

**Reusable pattern established:** operator-on-demand backfill stages
in `scripts/ops.py` with `dry_run=True` default + scope filters
(`tickers`, `failing_only`, `delisted_only`, etc.) + idempotent
writes + a per-source provenance precedence dict + the bulk-cache
indirection. See `_stage_backfill_sec_metadata` and
`_stage_backfill_sec_lifecycle`. Future P1b should follow this shape.

**Deterministic agents UNCHANGED** through the arc — `tpcore/selfheal/`,
`tpcore/auditheal/`, `ops/data_repair_service.py`, RiskStateStore all
intact. The arc was additive at the boundaries (validator routing +
gate read).

See also: [[utc-everything-operator-currently-sf]] (the operator's
current location for wall-clock interpretation), and the operator's
standing rules around bulk-before-API-crawl + apply-own-documented-
constraints — both honored in P2a's cache layer.
