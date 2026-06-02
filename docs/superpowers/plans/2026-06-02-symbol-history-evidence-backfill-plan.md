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
  pattern). Emit a `ticker_history` row for `(classification_id_of_newCIK,
  oldSymbol, valid_from=?, valid_to=date)` — proves the SAME issuer
  used both symbols. The cleanup classifier will read this as
  "no different-issuer evidence" → no high_confidence delete.
* `oldCIK != newCIK` AND both non-NULL → **different-issuer ticker
  reuse** (classic case). Emit `(classification_id_of_oldCIK, oldSymbol,
  valid_from=earliest_predecessor_filing, valid_to=date)` PLUS the
  matching `issuer_securities` row tying `classification_id_of_oldCIK`
  to the predecessor `issuer_id`. The cleanup classifier reads this
  as rank-3 evidence → high_confidence delete candidate.
* `oldCIK NULL` (Path C cannot resolve) → mint FMP-only predecessor
  per §2.3 fallback row. Mark rank-3 evidence with `source = 'fmp_only'`
  in `data_quality_log` so the cleanup classifier can downweight
  (still emits as rank-3, but the cleanup-rerun PR can elect to
  treat `fmp_only` as ambiguous if the operator decides).

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

Natural key: `(classification_id, ticker, valid_from)`. Insert:

```sql
INSERT INTO platform.ticker_history (classification_id, ticker, valid_from, valid_to)
VALUES ($1, $2, $3, $4)
ON CONFLICT (classification_id, ticker, valid_from) DO NOTHING;
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
* Migration: the `(classification_id, ticker, valid_from)` UNIQUE
  index needs to exist. **Plan check**: confirm in the implementer
  PR's migration whether the existing
  `20260524_0100_create_ticker_history.py` already declares this
  uniqueness — if not, the implementer PR ships a `CREATE UNIQUE INDEX
  CONCURRENTLY` migration.

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
