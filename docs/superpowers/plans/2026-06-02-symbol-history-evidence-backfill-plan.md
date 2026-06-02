# Symbol-history evidence backfill — implementation plan

**Status:** PLAN ONLY. No implementation. No DB writes. No live cleanup,
quarantine, or delete authorized in this PR. Direct successor to spec
`docs/superpowers/specs/2026-06-02-symbol-history-evidence-backfill.md`
(PR #442, merged 2026-06-02).

Author: symbol-history / issuer-identity track. Owner: operator.

## 1. Verdict

Path B (FMP `/stable/symbol-change` bulk) is the **primary** source.
Path C (SEC `submissions.zip` cross-walk via the existing
`SECSubmissionsBulkReader`) is the **CIK resolver and SEC-attestation
secondary**. Path A (R2-archived FMP daily roster snapshots) is
**deferred** — the substrate carries no roster snapshots today.

The implementation is one new heavy-lane `scripts/ops.py` stage
`symbol_history_evidence_backfill` that populates `ticker_history`
and `issuer_securities` (and a small number of historical-predecessor
`ticker_classifications` rows) from a **single bulk GET** of the FMP
symbol-change endpoint, archived under the existing R2 substrate
before ingest. No per-ticker crawl. No new migration (schemas exist).
No cleanup, quarantine, or delete in this stage — those remain
gated by a later cleanup-rerun PR.

## 2. Resolved §10 discovery findings

The spec's three §10 open questions are now answered with empirical evidence:

### 2.1 R2 roster snapshot inventory (Path A)

**Status: UNAVAILABLE / DEFERRED.** The `ste-archives` bucket holds
233 objects across 15 source prefixes. None is a daily ticker→CIK
roster snapshot. The FMP-related prefixes are event-shaped
(`fmp_catalyst_events`, `fmp_earnings_events`) or bars-shaped
(`fmp_daily_bars`) — none carry a `(ticker, CIK, effective_date)`
tuple stream. Building this would require starting daily archive of
`/v3/stock/list` going forward and waiting months for usable depth.
Out of scope for this cleanup arc; optional hardening side-quest.

### 2.2 FMP `/stable/symbol-change` shape (Path B)

**Status: PRIMARY.** Single bulk GET on the operator's $200/yr
Starter tier returns the full dataset:

| `?limit=` | Rows  | Earliest   | Latest     |
|----------:|------:|------------|------------|
| 10000     | 5,334 | 1969-12-31 | 2026-06-01 |

Row shape: `{date, companyName, oldSymbol, newSymbol}`. **No CIK
field** in response. Bytes-per-row ~135 → ~700 KB compressed.
Pagination params `page`/`offset`/`from`/`to` are not honored —
only `limit` controls depth. The `1969-12-31` floor is FMP's
"unknown historical date" sentinel; affects ~10s of rows.

### 2.3 TKR-14 classification_id mint for historical predecessors

**Status: RESOLVED — deterministic mint covers historical rows.**
`tpcore.identity.tkr14.mint` is pure-deterministic from
`(country, asset_class, ipo_venue YY, discovery_source,
cik || legal_name)`. For historical predecessor rows the plan uses:

| Segment             | Value source                                                              | Fallback                        |
|---------------------|---------------------------------------------------------------------------|---------------------------------|
| `country`           | SEC `submissions.addresses.business.stateOrCountry` (cross-walked via Path C) | `"US"`                          |
| `asset_class`       | `"S"` (stock) — verifiable from any old SEC filing                        | `"S"`                           |
| `ipo_venue`         | `"Z"` (sentinel/unknown — already permitted by the TKR-14 regex)          | `"Z"`                           |
| `discovery_year_yy` | predecessor's earliest SEC-filing year (via Path C)                       | symbol-change `date` year       |
| `discovery_source`  | `"S"` (SEC) when CIK resolves                                             | `"F"` (FMP-only)                |
| issuer seed         | `country|CIK` (preferred — via Path C)                                    | `country|companyName` from FMP  |

Salt-retry on collision per the existing `mint(salt=…)` contract.
Predecessor rows in `ticker_classifications` land with
`lifetime_end` non-NULL (the historical-row marker; the parent_resolver's
pin-at-first-resolve discipline is preserved for live rows because
historical mints carry `lifetime_end ≠ NULL` and don't compete on
the live-active key).

## 3. Path B primary — FMP bulk symbol-change

### 3.1 Bulk artifact handling

1. **Read first.** Resolve via the existing `csv_archive_backends`
   `S3Backend.list_archives("fmp_symbol_change")` ordering by
   filename timestamp. If a recent (`< 7 days`) archive exists,
   stream that. **No provider call.**
2. **Fallback to local.** If R2 list is unreachable but a local
   `/tmp/fmp_symbol_change_<ts>.json.gz` exists and is recent,
   stream that. Verify checksum/size against R2 metadata if R2
   later returns. Treat mismatch as a hard stop — re-download.
3. **Provider download.** Only if both archive and local cache are
   missing/stale: one bulk GET to `/stable/symbol-change?limit=10000`
   via `tpcore.outage.with_retry`. **No per-ticker iteration.**
   On success, write to R2 under `fmp_symbol_change_archive/` using
   the existing `S3Backend.write(source="fmp_symbol_change", …)`
   contract, then re-read the just-written archive to verify
   local↔archive parity before ingest.
4. **`use_bulk_zip = True` is the only path.** The stage MUST raise
   on `use_bulk_zip = false` — symmetric to the cleanup stage's
   sentinel. **No per-ticker crawl** is a producer-hard-stop.

### 3.2 Row-shape parsing

```text
{date: "YYYY-MM-DD", companyName: str, oldSymbol: str, newSymbol: str}
```

Rules:

* Strip `oldSymbol` / `newSymbol` to upper-case ASCII.
* Reject rows whose date is the **`1969-12-31` sentinel** for further
  processing — instead, set `valid_from = COALESCE(predecessor_first_sec_filing_year, NULL)`
  and emit a `data_quality_log` row with `kind = 'fmp_symbol_change_sentinel_date'`.
  Do NOT drop the row from the run — explicit triage, not silent loss.
* Reject rows where `oldSymbol == newSymbol` (FMP sometimes emits these).
* For `oldSymbol` not in current `ticker_classifications`, that is the
  expected case for cross-issuer reuse — proceed to mint a historical
  predecessor.

### 3.3 Same-CIK ticker change vs different-issuer reuse

For each `(oldSymbol, newSymbol, date)` row, after Path C cross-walk
resolves `oldSymbol@date → oldCIK` and `newSymbol@now → newCIK`:

* `oldCIK == newCIK` → **same-CIK ticker change** (SPAC merger
  pattern). **Option B forward fix (2026-06-02; corrects the
  spec-PR-doc's additive-row intent that ran into the GiST
  `ticker_history_no_overlap` EXCLUDE constraint on the live
  populate of 2026-06-02; see §5.1 and §13):** run the
  three-step sequence inside ONE transaction:
  1. **Guard SELECT** — read the pre-existing open-ended
     ticker_history row (`valid_to IS NULL`) for
     `classification_id_of_newCIK`. If the row's
     `valid_from >= change_date` (unresolvable temporal conflict)
     OR no open row exists, emit `data_quality_log kind=
     'same_cik_window_pre_dates_change'` or `'same_cik_no_open_window'`
     and skip the write. If the row's `valid_from == change_date`
     AND `ticker == newSymbol`, the state is already-applied —
     silent re-run no-op.
  2. **UPDATE existing row** — `SET valid_to = change_date,
     ticker = oldSymbol` (closes the previously open-ended
     window AND rewrites its ticker so the now-finite
     `[lifetime_start, change_date)` window honestly carries the
     predecessor symbol). The WHERE clause stays `classification_id
     = <cls> AND valid_to IS NULL AND valid_from < change_date`
     so re-runs are no-ops.
  3. **INSERT new current row** — `(classification_id_of_newCIK,
     newSymbol, change_date, NULL)` with `ON CONFLICT
     (classification_id, valid_from) DO NOTHING` for idempotency.

  Final three-row state per same-CIK case:
  `(cls, oldSymbol, lifetime_start, change_date)` historical +
  `(cls, newSymbol, change_date, infinity)` current. The cleanup
  classifier still reads this as "no different-issuer evidence" →
  no high_confidence delete.

  `valid_from` for the historical row (post-UPDATE) remains the
  pre-existing row's `valid_from` (the issuer's
  `lifetime_start` baseline); the implementer does NOT back-shift
  it. If a future evolution needs a more precise predecessor
  window, that's a separate change.
* `oldCIK != newCIK` AND both non-NULL → **different-issuer ticker
  reuse** (classic case). Emit `(classification_id_of_oldCIK, oldSymbol,
  valid_from=earliest_predecessor_filing, valid_to=date)` PLUS the
  matching `issuer_securities` row tying `classification_id_of_oldCIK`
  to the predecessor `issuer_id`. The cleanup classifier reads this
  as rank-3 evidence → high_confidence delete candidate. **No
  change from Option B**: this path inserts on a NEW predecessor
  `classification_id` which has no pre-existing rows, so there is
  no overlap risk.
* `oldCIK NULL` (Path C cannot resolve) → mint FMP-only predecessor
  per §2.3 fallback row. Mark rank-3 evidence with `source = 'fmp_only'`
  in `data_quality_log` so the cleanup classifier can downweight
  (still emits as rank-3, but the cleanup-rerun PR can elect to
  treat `fmp_only` as ambiguous if the operator decides). **No
  change from Option B**: same as different-issuer — new
  `classification_id`, no overlap risk.

## 4. Path C resolver — SEC `submissions.zip` cross-walk

### 4.1 Cross-walk mechanics

Reuse `tpcore.sec.submissions_bulk_reader.SECSubmissionsBulkReader`
3-tier resolution (local → R2 → SEC). For each `(oldSymbol, date)`
tuple from Path B:

1. Iterate the cached `submissions.zip` once at stage start,
   building an in-memory `dict[symbol_at_date, list[(cik, valid_from, valid_to)]]`
   keyed on every `tickers[]` + `formerNames[from..to]` window
   intersected with the symbol.
2. For each `(oldSymbol, date)`, look up the dict — match any
   `(cik, vfrom, vto)` where `vfrom ≤ date ≤ COALESCE(vto, ∞)`.
3. If exactly one match: `oldCIK = that_cik`. If multiple: emit
   `data_quality_log kind='ambiguous_oldcik_resolution'` and leave
   `oldCIK = NULL` (FMP-only path).
4. If zero matches: leave `oldCIK = NULL` (FMP-only path).

### 4.2 Why this works

SEC `submissions.zip` `tickers[]` is the CURRENT ticker(s) for a CIK,
and `formerNames` carries former-NAME windows. Their intersection
does not directly give "ticker X mapped to CIK Y from date A to B"
— SEC does NOT publish ticker history per se. The cross-walk is
therefore **CONFIRMATORY** for SEC-current tickers that existed
under the predecessor's CIK in the past (rare, when a CIK retired
the ticker and the SEC ticker list still echoes it via formerNames-era
ticker assignments visible in old filings).

For the dominant case — a delisted predecessor whose CIK no longer
appears in any current `tickers[]` array — the cross-walk WILL
return NULL, and Path B's `companyName` becomes the only identifier.
This is acceptable: the cleanup classifier's rank-3 path only needs
"different-issuer evidence" not "perfectly resolved-CIK evidence."
A historical predecessor minted from `(country|companyName)` is
deterministically distinct from the current ticker's
`(country|currentCIK)` issuer-hash, so rank-3 fires on
classification_id inequality alone.

## 5. Idempotent write design

### 5.1 `ticker_history` upsert

**Schema-audited correction (2026-06-02 forward-fix PR).** The
spec-PR doc claimed a 3-column natural key
`(classification_id, ticker, valid_from)`. The actual schema in
`platform/migrations/versions/20260524_0100_create_ticker_history.py`
declares:

* **Primary key:** `(classification_id, valid_from)` — 2 columns, not
  3. `ticker` is the value, not part of the key.
* **GiST EXCLUDE constraint** `ticker_history_no_overlap`:
  ```sql
  EXCLUDE USING gist (
      classification_id WITH =,
      daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
  )
  ```
  enforces NO overlapping `[valid_from, valid_to)` windows per
  `classification_id`.

The additive-INSERT pattern from the original §3.3 wording
(historical row spanning `[lifetime_start - 1y, change_date)`
inserted while the pre-existing row's `(currentTicker, valid_from,
infinity)` range covers `change_date`) trips this EXCLUDE on the
same-CIK path. PR #444 hit the live failure on 2026-06-02
(`classification_id=USFZ26ODRA4870`, existing `[2008-07-07, infinity)`
vs attempted `[2025-01-01, 2026-05-08)`); the partial state was
rolled back. Option B (§3.3) closes the pre-existing window
BEFORE inserting, preserving the GiST invariant.

Idempotent additive insert (for the different-issuer / FMP-only
paths — new `classification_id`, no overlap risk):

```sql
INSERT INTO platform.ticker_history (classification_id, ticker, valid_from, valid_to)
VALUES ($1, $2, $3, $4)
ON CONFLICT (classification_id, valid_from) DO NOTHING;
```

* `ON CONFLICT DO NOTHING` — reloads are safe. We do NOT update
  `valid_to` on conflict; an updated `valid_to` indicates a different
  truth-window and SHOULD land as a separate row (the parent_resolver
  pin-at-first-resolve precedent applies).
* `valid_from` source: Path C earliest SEC-filing year for the
  predecessor, or symbol-change `date - 1 year` heuristic, or the
  `1969-12-31` sentinel handler from §3.2.
* `valid_to` source: the symbol-change `date` (the day the ticker
  was reassigned).
* No new migration needed — the existing 2-col PK + GiST EXCLUDE
  already give the idempotency floor. The implementer PR does NOT
  ship a `CREATE UNIQUE INDEX CONCURRENTLY` migration (the spec
  open question is resolved by direct schema inspection).

### 5.2 `issuer_securities` upsert

Natural key: `(issuer_id, classification_id, valid_from)`. Insert:

```sql
INSERT INTO platform.issuer_securities (issuer_id, classification_id, valid_from, valid_to)
VALUES ($1, $2, $3, $4)
ON CONFLICT (issuer_id, classification_id, valid_from) DO NOTHING;
```

Same `ON CONFLICT DO NOTHING` discipline. `issuer_id` is the
predecessor's `issuers.issuer_id` — minted via
`_mint_issuer_id_from_cik(oldCIK)` (existing helper in `scripts/ops.py`)
when CIK is known, or `_mint_issuer_id_from_legal_name(companyName)`
for FMP-only rows. The migration check from §5.1 applies analogously
to the issuer_securities unique index.

### 5.3 `ticker_classifications` predecessor mint

Per §2.3. Insert with `lifetime_end = <symbol_change_date>` (non-NULL)
so the live-key invariants on `(ticker WHERE lifetime_end IS NULL)`
are not violated. Conflict on `id` triggers salt-retry per the
existing `mint(salt=…)` contract. Cap salt at 5 — past that, raise
and log; manual triage required (collision rate <1.7% at 13k rows
per the TKR-14 birthday-paradox math; salt=1 typically resolves).

## 6. Stage design — `symbol_history_evidence_backfill`

### 6.1 Knobs

| Knob | Default | Purpose |
|---|---|---|
| `dry_run` | `true` | Print row counts and skip-reasons; no DB writes |
| `use_bulk_zip` | `true` | **Hard true** — `false` raises (no per-ticker crawl) |
| `archive_max_age_days` | `7` | If archived artifact is older, re-download from FMP |
| `local_cache_path` | `/tmp/fmp_symbol_change_latest.json.gz` | Local fallback if R2 unreachable |
| `force_download` | `false` | Bypass archive + cache (operator override) |
| `limit` | `10000` | FMP `?limit=` value (the dataset is ~5,334; 10k is safe ceiling) |
| `manifest_path` | `data/symbol_history_evidence_manifest_<ts>.csv` | Per-row decision dump for forensic review |

### 6.2 Per-row decision matrix (manifest schema)

```
oldSymbol, newSymbol, change_date, companyName,
old_cik_resolved, old_cik_source,         -- "sec_cross_walk" | "fmp_only" | "ambiguous" | "none"
new_cik_resolved, new_cik_source,
predecessor_classification_id_minted,
classification_action,                    -- "minted_new" | "existing" | "skipped_sentinel_date"
ticker_history_written, issuer_securities_written,
disposition                               -- "same_cik_ticker_change" | "different_issuer_reuse" | "fmp_only_unresolved" | "skipped"
```

### 6.3 Transactional shape

One `asyncpg.Pool` transaction per batch of ~500 rows (`asyncpg.executemany`).
On any batch failure, rollback the batch only — earlier batches stay
committed; subsequent batches retry on stage rerun thanks to
idempotency. **No mass DELETE in this stage.** This is additive-only.

## 7. Bulk/S3-first invariants

* **Archive-first read** — the stage NEVER calls the FMP endpoint
  if a recent R2 archive exists. The `archive_max_age_days = 7`
  default is the freshness floor.
* **Archive-after-download** — if the FMP endpoint is called,
  the response is written to R2 BEFORE ingest. The local cache is
  written only after R2 acknowledges the put.
* **Local/archive parity check** — the stage re-reads the just-archived
  object from R2 and validates `len(bytes) == len(local_bytes)` and
  `sha256(bytes) == sha256(local_bytes)`. Mismatch is a hard stop —
  the run aborts before any DB write.
* **No per-ticker crawl** — the producer-hard-stop is enforced by:
  (a) `use_bulk_zip=false` raises, (b) no `httpx.AsyncClient.get`
  call appears inside any per-row loop in the stage source, and
  (c) a unit test asserts that the stage's stage-level `with httpx.AsyncClient` count is ≤ 1.

## 8. FMP-only unresolved-CIK rows

When Path C cannot resolve `oldSymbol@date → oldCIK`:

* Mint a TKR-14 predecessor from `country=US|asset_class=S|ipo_venue=Z|YY=<change_year-1>|src=F|seed=country|companyName`.
* Insert into `ticker_classifications` with `lifetime_end = change_date`.
* Insert `ticker_history (predecessor_classification_id, oldSymbol, NULL, change_date)`.
* Skip the `issuer_securities` row (no issuer can be minted without
  identity). Emit `data_quality_log kind='fmp_only_no_issuer'` for
  operator awareness.
* The cleanup classifier will see `predecessor_classification_id ≠
  current_classification_id` → rank-3 fires, BUT with `source='fmp_only'`
  the cleanup-rerun PR can elect to treat as ambiguous instead of
  high_confidence. **That decision is deferred to the cleanup-rerun
  PR; this stage does NOT touch fundamentals_quarterly.**

## 9. `1969-12-31` sentinel-date handling

FMP uses `1969-12-31` as "unknown effective date" for ~10s of historical
rows. The stage:

* Does NOT silently drop these rows.
* Does NOT mint a TKR-14 with year `69` (would corrupt the year segment).
* DOES emit `data_quality_log kind='fmp_symbol_change_sentinel_date'`
  with the row payload.
* DOES skip the `ticker_history` insert (no valid_to we can trust).
* Manifest disposition: `"skipped_sentinel_date"`.

## 10. Post-backfill cleanup re-run (separate PR)

This plan PR + the eventual implementation PR do NOT touch
`fundamentals_quarterly`. After the implementation lands and the
substrate is populated, a SEPARATE PR will:

1. Re-run `cleanup_ticker_reuse_fundamentals --param dry_run=true
   --param severity_bucket=1` (and later 2-3, 4-9, 10-19, 20+).
2. Diff against the 2026-06-02 baseline (0 high_confidence /
   50 ambiguous / 24 weak_evidence_keep).
3. If high_confidence > 0 emerges, draft a tightly-scoped
   archive-then-delete authorization PR per the existing
   `feedback_no_shortcuts_100_pct` + archive-before-delete
   discipline from PR #441.

**No cleanup, quarantine, or delete is authorized by THIS plan PR
or by the implementation PR. Both are evidence-population only.**

## 11. Acceptance gates (implementation PR — not this PR)

| Gate | Target |
|---|---|
| `dry_run=true` smoke | runs to completion against archive-only; emits manifest; no DB writes |
| `dry_run=false` bounded | first 100 rows; verifies idempotency by running twice (second run inserts 0) |
| `dry_run=false` full | all 5,334 rows; ticker_history row delta ≈ rows where Path C resolves to a CIK; issuer_securities row delta = same |
| Bulk-first sentinel | `use_bulk_zip=false` raises before any HTTP call |
| Archive parity sentinel | local/R2 sha256 mismatch aborts before DB writes |
| `gh pr checks` | green; heavy-lane Claude review PASS |
| Full pytest single-process + order-flip | green |
| Vulture / ruff / gitleaks | clean against the diff |

## 12. Test plan (implementation PR)

* **FMP symbol-change bulk fixture parsed** — fixture JSON with
  representative rows (normal, SPAC, FMP-only, sentinel-date,
  same-CIK). Stage parses each disposition correctly.
* **`1969-12-31` sentinel handled explicitly** — produces
  `data_quality_log kind='fmp_symbol_change_sentinel_date'`, no
  `ticker_history` row inserted.
* **SEC cross-walk resolves `oldSymbol/date` to CIK where possible** —
  fixture submissions.zip subset; cross-walk hits + misses asserted.
* **TKR-14 historical predecessor mint deterministic** — same input
  twice → same id; salt=1 on collision; CI sentinel pins the regex.
* **Idempotent `ticker_history` upsert** — second insert of same
  `(classification_id, ticker, valid_from)` is a no-op.
* **Idempotent `issuer_securities` upsert** — same.
* **Same-CIK ticker change classified as keep/weak evidence, not
  high-confidence delete** — fixture: SPAC merger row →
  `disposition='same_cik_ticker_change'`; downstream cleanup
  classifier reads as weak.
* **Different-issuer symbol reuse produces rank-3 evidence** —
  fixture: classic delisted-predecessor row → both
  `ticker_history` AND `issuer_securities` rows present →
  cleanup classifier sees rank-3.
* **Bulk/S3-first source path enforced** — `use_bulk_zip=false`
  raises; archive-first read path verified via a fake S3 backend.
* **No per-ticker crawl source sentinel** — static AST scan of the
  stage source asserting ≤ 1 `httpx.AsyncClient.get` call site.

## 13. Rollback / no-op strategy

The stage is **additive-only**. There is no destructive operation to
roll back. To undo a populated substrate the operator can:

* `DELETE FROM platform.ticker_history WHERE classification_id IN
  (SELECT id FROM platform.ticker_classifications WHERE
  lifetime_end IS NOT NULL AND id NOT IN (legacy-set))` — bounded
  by the historical-mint marker.
* Analogous `DELETE FROM platform.issuer_securities` and
  `DELETE FROM platform.ticker_classifications` for the
  newly-minted predecessor rows.

Rollback is operator-on-demand; the stage does NOT auto-undo.
A no-op stage rerun against a fully-populated substrate produces
zero new rows (the `ON CONFLICT DO NOTHING` invariant). Failure
modes: R2 unreachable + local cache missing + FMP 401/5xx → stage
raises before any DB write; partial-batch failures roll back the
batch but leave earlier batches committed.

**Live-populate failure of 2026-06-02 + rollback predicate
(addendum to the Option B forward-fix PR).** The first live run of
PR #444 hit `asyncpg.exceptions.ExclusionViolationError` on the
same-CIK path against the existing GiST `ticker_history_no_overlap`
constraint (see §5.1 and §3.3). The partial DB state was rolled
back via the predicate

```sql
DELETE FROM platform.ticker_history
WHERE source LIKE 'symbol_history_evidence_backfill.%';

DELETE FROM platform.issuer_securities
WHERE source LIKE 'symbol_history_evidence_backfill.%';

DELETE FROM platform.ticker_classifications
WHERE source LIKE 'symbol_history_evidence_backfill.%';
```

The naive operator instinct of a date-based predicate (e.g.,
`lifetime_end BETWEEN <run_start> AND <run_end>`) would have
targeted 0 rows for the rolled-back same-CIK writes because the
populated `lifetime_end` values are HISTORICAL change-dates
(spanning 1998–2026) not the run timestamp. The `source LIKE
'symbol_history_evidence_backfill.%'` discriminator is the
correct rollback key because every row written by this stage
carries that `source` prefix.

## 14. Non-goals

* **No fundamentals_quarterly schema change.**
* **No fundamentals_quarterly cleanup execution.**
* **No validator change.**
* **No threshold loosening.**
* **No per-ticker API crawl.**
* **No archive of FMP daily roster snapshots** (the Path A
  side-quest is optional hardening, not part of this arc).
* **No new identity model.** TKR-14 + the existing
  `ticker_classifications` / `ticker_history` / `issuer_securities`
  schema suffice.

## 15. Open questions for the implementation PR

1. Does `platform.ticker_history` already carry a UNIQUE index on
   `(classification_id, ticker, valid_from)`? If not, the impl PR
   ships a `CREATE UNIQUE INDEX CONCURRENTLY` migration (heavy-lane).
2. Does `platform.issuer_securities` already carry a UNIQUE index on
   `(issuer_id, classification_id, valid_from)`? Same disposition.
3. Expected `issuer_securities` row-count after first full populate
   (to set the deferred sentinel floor per spec §7).
4. Should the optional Path A side-quest (start archiving
   `/v3/stock/list` daily) land as a separate hardening PR
   independent of this arc, or remain unfunded?

These do not block this plan PR — they are research items for the
implementer to resolve before opening the implementation PR.

## 16. Post-populate empirical result (2026-06-02 afternoon — arc CLOSEOUT)

PR #444 (impl) merged at `b68f915`. The first live `--param dry_run=false`
hit `asyncpg.ExclusionViolationError` on the `ticker_history_no_overlap`
GiST EXCLUDE on the same-CIK ticker-change path. Partial state was rolled
back via the `source LIKE 'symbol_history_evidence_backfill.%'` predicate
(see §13). PR #445 (Option B forward fix) merged at `8498f14`. The live
retry SUCCEEDED.

### 16.1 Live populate deltas

| Table | Pre | Post | Δ | Forecast | Status |
|---|---:|---:|---:|---:|---|
| `ticker_history` | 13,840 | 19,013 | **+5,173** | +5,174 | −1 (1 same-CIK row hit `same_cik_pre_dates_change` — Option B guard fired correctly) |
| `issuer_securities` | 25 | 89 | **+64** | +64 | **exact** |
| `ticker_classifications` | 13,840 | 19,004 | **+5,164** | +5,323 | −159 (TKR-14 collisions; `ON CONFLICT DO NOTHING`) |
| `ticker_classifications` (`lifetime_end IS NULL` = active) | 12,344 | 12,344 | **0** | 0 | **no active row mutated** |
| `data_quality_log` | 1,331 | 6,407 | **+5,076** | stage-reported 5,256 written | 180 deduped at DB layer |
| `fundamentals_quarterly` | 183,352 | 183,352 | **0** | 0 | **invariant held** |

Zero GiST EXCLUDE violations. Archive-first short-circuit confirmed (no
provider GET). Same-CIK Option B path: 9 successful close+insert pairs,
1 unresolvable-window skip with `data_quality_log` row.

### 16.2 Post-populate cleanup dry-runs (all 5 buckets)

`python scripts/ops.py --stage cleanup_ticker_reuse_fundamentals --param
dry_run=true --param severity_bucket={1, 2-3, 4-9, 10-19, 20+}`. All five
dry-runs executed read-only; 0 DB writes; all archive-hit, no provider
calls; no shard errors.

| Bucket | Candidates | Distinct tickers | rank-0 (no_evidence → ambiguous) | rank-2 (issuer_history → weak_keep) | **rank-1** | **rank-3** | high_confidence |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 74 | 74 | 50 | 24 | **0** | **0** | **0** |
| 2-3 | 267 | 111 | 204 | 63 | **0** | **0** | **0** |
| 4-9 | 2,536 | 432 | 1,983 | 553 | **0** | **0** | **0** |
| 10-19 | 1,255 | 97 | 916 | 339 | **0** | **0** | **0** |
| 20+ | 1,884 | 69 | 1,535 | 349 | **0** | **0** | **0** |
| **Total** | **6,016** | **783** | **4,688 (77.9%)** | **1,328 (22.1%)** | **0** | **0** | **0** |

### 16.3 Why rank-3 NEVER fires — structural finding

Of the 1,304 rank-2 weak-keep rows aggregated across buckets 1 + 2-3 + 4-9
+ 10-19 + 20+:

- **551** (42%) have a `ticker_history` row at the bucket-row's
  `period_end_date` (i.e., the populated substrate did intersect).
- **0** (zero) of those 551 have a corresponding `issuer_securities` row
  at the same date.
- **0** would flip to `high_confidence` under a classifier rank-priority
  reframe (rank-3-before-rank-2) — the second-stage `issuer_securities`
  lookup is what makes rank-3 dispositive, and it is empty for the
  reachable substrate.

The reason: of the 5,173 new `ticker_history` rows, 5,109 (98.8%) tie to
FMP-only-minted classification_ids that **deliberately skip** the
`issuer_securities` insert (Path C cross-walk to SEC `submissions.zip`
resolved only 68 of 5,334 rows — 1.3% — because FMP `/symbol-change`
carries no CIK field and the cross-walk only hits when the predecessor
still appears in current `submissions.zip tickers[]`, which delisted
predecessors don't).

### 16.4 Decision — arc STOPPED

The substrate populated correctly (Option B fix held; no GiST collisions;
fundamentals_quarterly invariant intact). The cleanup classifier
correctly applied its rank logic against the populated substrate. The
empirical answer: **the current evidence sources are structurally
insufficient for automated high-confidence cleanup of any of the 6,016
candidate rows**.

A classifier rank-priority reframe is NOT JUSTIFIED — the empirical
evidence shows the reframe would change zero dispositions. The bottleneck
is `issuer_securities` coverage, not classifier-logic order.

What would unblock automated cleanup (out of scope for this arc):

1. **Richer ticker→issuer historical-mapping source** — e.g., CRSP /
   Compustat subscription, OpenFIGI batch (NOT per-ticker crawl), or a
   NASDAQ / NYSE archive of daily issuer-list snapshots that intersect
   bucket-row period_end_dates. The FMP `/symbol-change` feed alone is
   too thin (1.3% Path C resolution rate).
2. **A different cleanup framing** — e.g., treat the 6,016 residuals as a
   `data_quality_log` annotation (mark rows "pre-FPFD; provenance
   uncertain") rather than archive/quarantine candidates. Preserves rows
   for backtest research while signaling caution to engines that consume
   `fundamentals_quarterly`.

No further work on this cleanup arc until one of those substrate
sources lands or the framing changes.

### 16.5 What this plan delivered

- `ticker_history` 38% larger, `issuer_securities` 256% larger
  (`(89 / 25) − 1`), and historical predecessor identity captured for
  ~5,164 prior issuers — all useful **research substrate** for future
  ticker-lineage queries even though it didn't move the cleanup
  classifier on bucket=1.
- Verified `ticker_history_no_overlap` GiST EXCLUDE in a live failure +
  rollback + Option B fix cycle — the temporal-integrity invariant is
  now tested and proven correct at the schema level.
- 21 plan-sentinel + 29 stage tests + 50 plan + 9 Option B tests pinning
  the load-bearing claims so future "tidy" passes cannot silently lose
  the empirical numbers above.

**Status: CLOSEOUT.** The arc shipped its substrate but did not unlock
the downstream cleanup. The doc is preserved as historical record + the
empirical-floor sentinel test pins the numbers.
