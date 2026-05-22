# TODO

Cross-cutting personal action items that don't fit existing docs. Operational
build queues belong in `docs/DATABASE_AND_DATAFLOW.md §5 Implementation Queue`
or `docs/MASTER_PLAN.md §9 Build Order`.

## ⚠ PUBLIC REPO — recurring secret-audit gate (2026-05-21)

The repo went public 2026-05-21 (operator's GitHub Actions quota was exhausted; public repos get unlimited free Actions, hence the flip). Preliminary in-thread scan today found **zero committed secrets** in either current code or git history (no `sk-ant-*`, no `AKIA*`, no SSH/RSA private keys, no real Postgres credentials, no Alpaca/Finnhub/FMP/Tradier/Greeks-pro env-var assignments). `.env` is gitignored — only `.env.example` is tracked. The only PII-shaped strings are the public repo identifier itself (`michaeleasterly-cpu/short-term-trading-engine` in `railway.json` × 3 + one spec doc), which is necessarily public on a public repo.

`[lane: platform-wide] [gate: pre-commit hook + CI sentinel] [needs operator decision: which scanner — gitleaks vs trufflehog vs custom regex] [effort: S — install + one config file + CI step]`

**What to ship (now that the repo is public — defense in depth):**

1. **Recurring CI gate** — every PR scans for secret patterns. Recommended: `gitleaks` (industry standard, regex-based, fast) or `trufflehog` (also scans git history blob-by-blob, slower but more thorough). Add as a `.github/workflows/secret-scan.yml` step on every push.
2. **Pre-commit hook** — block accidental local commits before they hit origin. `.pre-commit-config.yaml` with the same scanner.
3. **Audit existing PRs/commits in the last N days** — operator-discretion on whether to retroactively scan PRs that landed 2026-05-19 → 2026-05-21 (the Carver task-25 series in particular touched many new LLM files; verify those don't have accidental SDK examples with real keys).
4. **Operator pre-flight checklist before next public-repo session** — read of `.env`/`.envrc` ensures no work-in-progress credentials are sitting in tracked files.

**Trigger context:** operator 2026-05-21: "make sure that none of my api keys are in the repo... its public now". Initial scan green; this entry captures the recurring-gate work so the next leak (when it happens) gets caught BEFORE it hits public history.

## WEEK GOAL (2026-05-16): Data layer finalization + hardening

Single focus until further notice — no engine/Sigma-redesign work. Sequence:

1. ✅ **SEC backfill — DONE 2026-05-16.** Per-ticker crawl root-caused
   as wrong tool; built two-phase bulk Form-345 ETL (insider 646,107
   rows / 84.1% T1-T2) + full-history-shard 8-K API backfill (237,680
   rows / 85.1%), 2018→2026, DB-verified, CI-green. `sec_filings_freshness`
   GREEN. **Still owed:** the catalyst/SEC 180d coverage *verdict vs
   thresholds* (our-defect-until-proven-per-ticker; no vendor-blame).
   3 suite checks red for **structural** reasons, not pull-staleness —
   `short_interest_freshness` (FINRA bi-monthly cadence > 35d
   threshold), `social_sentiment_freshness` (ApeWisdom ~23% < 30%
   floor), `prices_daily_freshness` (needs investigation). Belongs in
   threshold calibration, NOT a re-pull.
2. ✅ **Self-heal rollout — DONE 2026-05-16.** Honest end state:
   **14/20 checks genuinely self-heal** (all named to real bounded ops
   stages; zero fake specs — verified), **6/20 honest permanent
   escalate-for-investigation** (row/fundamentals/corporate_actions
   integrity = corruption class; delistings/constituent/splits =
   source-of-truth reconciliation — these can NEVER honestly
   auto-heal; healable=False is correct, not pending). The expert-
   flagged "11/11 self-heal" target was rejected as fake-green. Root
   causes fixed not masked: FINRA adapter missing offset-pagination
   (only 1 stale period ever ingested — our defect, not cadence;
   commit 16840f7); ApeWisdom 30% floor structurally unreachable →
   evidence-derived 15% (proven 23% source ceiling, full-overlap
   ingest verified; a58304c); per-class honest unhealable reasons
   (69e84b2); 3 + 2 healable flips (556cc9e, 51fb643). Force param
   added to tier_refresh/classify_tickers.
3. ✅ Validation/self-heal honest-green path proven (macro + classify
   force-repull live-verified).
3a. ✅ **Per-feed cadence profile (#163) — cadence facet DONE
   2026-05-16.** `tpcore/feeds/` is the single source of truth: one
   evidence-backed `FeedProfile` per feed (13 feeds), frozen, with an
   `evidence` string (no-vendor-blame). The 9 single-MAX_AGE freshness
   checks now READ `freshness_max_age_days` from the profile instead
   of scattered guessed constants — this also fixed the live
   short_interest docstring/constant lie (said 42, constant was still
   35 → now 42 from the profile). Clockwork drift test: every healable
   HealSpec source must declare a profile (can't ship a self-healing
   feed without an evidence-backed cadence). The other 3 facets are
   **declared as profile fields with per-feed values but enforcement
   is honestly phased, NOT dropped**: TRIGGER (scheduler re-arch off
   the blanket daily sweep — launchd-level), TARGETING (demand-driven
   set for constrained feeds — crosses the engine boundary),
   PUBLICATION-AVAILABILITY GATE (per-adapter "source has newer?"
   probe so vendor-late ≠ red). Those three are the remaining #163
   work, each a deliberate phase.
3b. ✅ **TRIGGER facet (#165) — DONE 2026-05-16.** `tpcore/feeds/
   dispatcher.py` (pure, tested) + `python -m tpcore.feeds`: reads
   the canonical per-stage last-success from `application_log` + the
   XNYS close gate, returns only feeds whose trigger/cadence is due
   per FeedProfile. The EXISTING data-ops daemon (no new daemon)
   calls it → `ops.py --update --only <due>`; absent `--only` =
   today's full sweep (preserved/reversible); empty-due = infra +
   Step-4 self-heal only (NONE_DUE sentinel — green-gate unaffected);
   launchd timing untouched. Live-proven; 879 tests.
3c. ✅ **TARGETING + PUBLICATION facets (#165) — DONE 2026-05-16.**
   TARGETING: `tpcore/feeds/targeting.py` — `demand_targets` (DB-
   derived active interest: open_orders ∪ recent aar_events ∪ recent
   universe_candidates; NO engine code — engine *output* in shared
   tables) + `prioritise`; CONSTRAINED_DEMAND_DRIVEN feeds spend their
   bounded budget on demand tickers first, WHOLE_UNIVERSE never
   narrowed; empty demand → unchanged. Wired exemplar:
   IBorrowDesk handler. PUBLICATION: `tpcore/feeds/publication.py` —
   freshness is now VENDOR-ANCHORED (UTC, the vendor's calendar, NOT
   today−N): `FeedProfile.publish_weekday` (AAII=Thu) +
   `expected_latest_publish` (pure, offline — last scheduled publish
   minus dissemination lag) wired into the AAII check, so a red means
   "vendor published, we're behind" (genuine our-gap) and normal
   vendor lag never false-fires. Live HEAD `Last-Modified` probe
   (`AAIIAdapter.latest_published` + `source_has_newer`) built +
   registered + tested as the mechanism. 891 tests; ruff/imports
   clean; no engine code modified.
   **Honest remaining (incremental adoption, not unbuilt design):**
   ✅ **Per-constrained-feed targeting rollout beyond IBorrowDesk —
   DONE 2026-05-20.** Surveyed the four other CONSTRAINED_DEMAND_DRIVEN
   feeds; only `finnhub_insider_sentiment` shares the IBorrowDesk
   structural fit (per-ticker API loop + rate cap). Wired it: the
   handler calls `demand_targets` + `prioritise` so demand tickers
   land at the front of the ~27-min loop (Finnhub free-tier
   60/min → 1.1s/ticker × ~1500 T1/T2 = 27 min wall-clock; a mid-run
   interruption still covers the demand set). The other three are
   intentionally probe-less + carry inline notes explaining the
   structural mismatch: `finra_short_interest` + `apewisdom_social_sentiment`
   are single bulk pulls (vendor's global response ceiling, no
   per-ticker API call to prioritise); `greeks_max_pain` is a
   single-symbol snapshot (no universe to prioritise; engines
   consume specific symbols). They stay CONSTRAINED_DEMAND_DRIVEN
   because their budget constraint is real, but the wedge is in DFCR
   provider augmentation / cadence, not ticker prioritisation. The
   targeting docstring + the inline NOTEs in handlers.py document
   the rollout state.
   ✅ **Self-heal-orchestrator probe consult — DONE 2026-05-20.**
   `tpcore/selfheal/probes.py` owns the per-source vendor-state probes
   (`VENDOR_PROBES["aaii_sentiment"]`, `VENDOR_PROBES["macro_indicators"]`
   — the two adapters with a `latest_published()` method). Each probe
   queries our DB for `our_latest` (MAX(date) for AAII;
   MIN-across-series for FRED, matching the publication.py MIN
   composition) and consults `source_has_newer()`. Orchestrator
   classifies each red BEFORE heal: probe-says-vendor-newer → heal
   as usual; probe-says-vendor-nothing-newer → vendor_late
   classification, skip heal, emit `selfheal.vendor_late` distinct
   event; probe unavailable / probe returns None → fall back to the
   existing heal flow unchanged.
   `SelfHealOutcome.vendor_late: list[tuple[source, our_iso, vendor_iso]]`
   surfaces the data for the wrapper's TRIGGER_VENDOR_LATE INFO event.
   Early exit when every remaining red is vendor-late (no point
   looping until max_iterations on a hopeless re-probe).
   **Sacred 100%-green invariant preserved:** vendor-late reds leave
   the data_quality_log row red so `green=False` and
   `DATA_OPERATIONS_COMPLETE` stays gated; the orchestrator-internal
   "RED→WARN" downgrade is the visibility/cycle-saving win, not a
   gate change.
   **FRED probe added 2026-05-20:** `tpcore.fred.FREDAdapter.latest_published(series_id)`
   reads `observation_end` from `/fred/series` (one small JSON GET per
   series, NO observations downloaded); the feed-level `_fred_probe`
   in `tpcore.feeds.publication` composes per-series answers into a
   conservative MIN-across-series verdict (taking MAX would silently
   green a stuck series); registered as `PUBLICATION_PROBES["macro_indicators"]`.
   Per the AAII precedent, validation stays offline — the live probe
   is for the self-heal-orchestrator to consult before spending a
   heal cycle on a stale macro_indicators result.
   **Alpaca prices_daily probe added 2026-05-20:**
   `tpcore.alpaca.AlpacaDataAdapter.latest_published(symbol="SPY")`
   uses the SDK's `get_stock_latest_bar` against the IEX feed (the
   Algo Trader Plus tier 403s the latest-bar endpoint on SIP —
   historical SIP queries still work for production ingestion; this
   one is a separate cheap "is there a new session?" question).
   Single-anchor design (SPY only, NOT MIN-across-universe): a
   delisted/halted ticker in a universe-MIN would peg the answer to
   its last-trade-date forever. SPY is the universal anchor
   (CRITICAL_TICKERS member, every NYSE session, never delisted).
   Registered as `PUBLICATION_PROBES["prices_daily"]` and
   `VENDOR_PROBES["prices_daily"]` (the orchestrator's vendor-late
   consult layer) — high-leverage because prices_daily is the data
   substrate every engine reads through the per-engine data gate.
   **ApeWisdom probed 2026-05-20 — honest-stop, no usable
   timestamp.** Verified live: the JSON response top-level keys are
   only `count` / `current_page` / `pages` / `results`; per-record
   has no `updated_at` / `scraped_at`. HTTP headers from Cloudflare
   carry only `Date` (no `Last-Modified`, no `ETag`,
   `cf-cache-status: DYNAMIC`). The `rank_24h_ago` /
   `mentions_24h_ago` fields are rate-of-change values, not a
   publish timestamp. ApeWisdom stays probe-less — the
   strict-cadence + 15%-floor recalibration (2026-05-16) is the
   canonical mechanism. Documented in
   `tpcore/apewisdom/adapter.py` + `tpcore/feeds/publication.py`
   docstrings so the finding doesn't get re-investigated.
   FINRA still has no cheap latest-probe (its API exposes no
   max-settlement without full pagination) — intentionally absent;
   the strict cadence fallback already honest post-recalibration.
4. **Hardening pass** (some items NOT blocked on the verdict — run in
   parallel while SEC backfills):
   - ✅ **`prices_daily_gaps` 14-day-recency blind spot — CLOSED (DONE-
     stale).** Superseded by the ungameable zero-tolerance invariant
     `tpcore/quality/validation/checks/prices_daily_completeness.py`
     (its module docstring L1-9 names this exact blind spot; no recency
     window, no >7d-run minimum — ANY missing (ticker, session) in the
     30-session liquid window fails). The widening of the heuristic
     `prices_daily_gaps` audit check is moot — the invariant gate is the
     correct mechanism (registered in `KNOWN_CHECK_NAMES`, healable via
     `daily_bars --param repair_gaps=true`).
   - ✅ **sporadic `row_velocity` — DONE in PR #78 (b61c1ce, 2026-05-18).**
     The sporadic branch now WARNs on BOTH (a) total silence
     (`recent == 0 and prior > 0`, preserved byte-for-byte) and (b)
     severe sustained partial degradation: `recent < expected *
     SPORADIC_SEVERE_FRAC` with `expected = prior * SPORADIC_RATE_FACTOR`
     and the `prior >= SPORADIC_PRIOR_FLOOR` guardrail, measured over a
     cluster-robust 180d-recent / 365d-prior-band window
     (`scripts/audit_data_pipeline.py` L222-228 constants; L1257-1300
     branch). Closes the gap where a sustained ~98% partial collapse
     read OK because it was not strictly zero. Test coverage:
     `scripts/tests/test_audit_row_velocity_sporadic.py` (12 cases:
     silence preserved, severe partial bites, clustered inter-cluster
     lull stays OK, daily branch unchanged, constants cluster-robust).
     TODO entry stale — the surface-snippet line numbers (L1136-1144)
     referred to a pre-#78 layout that no longer exists.
   - ✅ **FMP handler-path CSV archive: verify end-to-end — DONE
     2026-05-20.** End-to-end proof + schema-drift fence both in place.
     `tpcore/tests/test_handle_fundamentals_archive_e2e.py` exercises
     `handle_fundamentals_refresh` (fake adapter/cache/pool, `TP_DATA_DIR`
     seam to tmp_path) and asserts (a) gzipped CSV archive lands in
     `data/fmp_fundamentals_archive/`, (b) non-zero bytes, (c) CSV
     header equals the canonical `FUNDAMENTALS_ARCHIVE_FIELDS` tuple
     extracted to module scope in `tpcore/ingestion/handlers.py`.
     Sibling DB-gated test `test_handle_fundamentals_archive_db_schema.py`
     (wired into the `lab-isolation-db` CI job) pins that tuple to the
     live `platform.fundamentals_quarterly` information_schema — both
     directions: every DB data column (excluding the surrogate `id`)
     must appear in the tuple, every tuple entry must be a real DB
     column. A future migration that adds a column without updating
     the archive tuple fails CI loud.
   - ✅ **Wire `CFNAIMA3` (Chicago Fed National Activity Index, 3mo MA) to
     FRED ingestion — DONE 2026-05-20.** Appended `("cfnai_ma3",
     "CFNAIMA3")` to `INDICATOR_SERIES` in `tpcore/fred/adapter.py`; added
     to `EXPECTED_INDICATORS` + `INDICATOR_CADENCE` (monthly) in both
     `tpcore/quality/validation/checks/macro_indicators_completeness.py`
     and the sibling `macro_indicators_freshness.py`; conftest fake-pool
     indicator lists extended for e2e coverage. Surfaced 2026-05-20 by
     the Sentinel Bear Score Lab-candidate subagent — the candidate's
     `CFNAI ≤ -0.70` band anchor needs this series ingested. Unblocks
     the Sentinel graduated Bear Score Lab candidate (TODO §Deep-research).
     Next FRED ingestion cycle populates rows (no historical backfill
     bundled with the wire-up PR).
   - ✅ **SOS (Sum-of-States diffusion) substrate — DONE 2026-05-21.**
     Operator picked option (c): construct the diffusion index from
     the 50 Philadelphia Fed state coincident series (`{XX}PHCI`,
     monthly, 1979→present, license-free). Live-probed all 50: every
     series valid, monthly cadence, observation_start 1979-01-01
     (TX 1979-04-01). Wired as 50 raw `phci_<state>` entries in
     `tpcore/fred/adapter.py::INDICATOR_SERIES` + the derived
     `sos_state_diffusion` indicator computed via the new pure
     `tpcore/fred/diffusion.py::compute_sos_diffusion` (Crone/Clayton-
     Matthews 2005, default 3-month span; zero-tolerance month
     exclusion when ANY anchor state is missing the anchor pair).
     Persisted by `handle_macro_indicators` on the same ON CONFLICT
     idempotent path as raw series. EXPECTED_INDICATORS +
     INDICATOR_CADENCE extended in both `macro_indicators_completeness`
     and the sibling `macro_indicators_freshness` (all 51 monthly).
     Unblocks the Sentinel graduated Bear Score Lab candidate (TODO
     §Deep-research). Next FRED ingestion cycle populates the
     historical rows (no manual backfill bundled with the wire-up PR).
   - ✅ **HY-spread recovery — DONE 2026-05-16.** ALFRED/Nasdaq ruled
     out empirically; full history recovered (eco-archive 1996-2021 +
     Scribd FRED-graph gap, validated 772/772 exact). `hy_spread`
     contiguous 1996→present, re-activated as a maintained
     `INDICATOR_SERIES` member (FRED rolling window keeps tail fresh).
     BAA10Y also still maintained. Research spike RESOLVED.
     **Deferred (held by operator):** the HY→Sentinel Bear-Score
     scoring switch — original was binary HY>5%; current is graduated
     BAA10Y. Requires backtest-derived HY-OAS graduated thresholds
     before going live. NOT done; awaiting explicit go + validation.
   - then the tracked `catalyst→earnings` rename (below).

## Vector engine — internal "Catalyst" vocabulary rename (operator decision pending)

The data feed was renamed `catalyst_* → earnings_*` (DONE 2026-05-16; see session-log). The Vector engine's **internal scoring vocabulary** is still NOT renamed — `VectorScore.catalyst` Pydantic field (0–35 component), `catalyst_magnitude` backtest CSV column header, `_has_catalyst` / `_catalyst_window_days`, "Catalyst-Driven Swing" branding. Touches a serialized model field + CSV schema + dashboard reads (artifact-breaking). Operator decides: purge Vector's internal "Catalyst" vocabulary, or leave the engine concept as-is. `[lane: engine-owned] [needs operator decision: yes] [effort: M (artifact-breaking)]`

## Autonomous self-heal — EVERY data source (P0, 2026-05-15)

> **STATUS 2026-05-20 — ALL 5 P0 SOURCES COMPLETE.** Per-source
> ungameable physical-truth completeness invariants now ship for the
> full set: macro_indicators (#168), fundamentals_quarterly (#172),
> corporate_actions (#174), sec_insider_transactions (#179),
> earnings_events (#181 in CI). Each invariant has a paired HealSpec
> routed to the canonical `ops.py --stage X` infrastructure with
> detector/healer symmetry via a shared `_evaluate()`. The 2026-05-16
> bounded-heal substrate (14/20 self-healing, 6/20 honest escalate) was
> the prerequisite; this P0 round adds the ZERO-TOLERANCE invariant
> half — what `prices_daily_completeness` is for daily bars, now
> generalized to every other source. P1 follow-ons: (a) liquidity_tiers
> + ticker_classifications completeness shape (item 6 below);
> (b) ✅ DONE 2026-05-20 — earnings_events NO_BEAT-sentinel ingestion
> (Path B) resolves the prior KNOWN GAP, monotone invariant now
> filters on `event_type IN ('EARNINGS_BEAT','EARNINGS_NO_BEAT')` so
> truncation AND missed-detection both gated.
>
> Prior STATUS 2026-05-16 (preserved for rationale): bounded heal
> shipped; 14/20 genuinely self-heal, 6/20 escalate-for-investigation;
> per-feed cadence + feed-driven dispatch + vendor-anchored freshness;
> "runs on its own, no fake-green" spirit met for the heal substrate.

> **🔴 OPEN INCIDENT — prices_daily coverage collapse (logged 2026-05-17).**
> `validation.prices_daily_freshness` red (ran 2026-05-16 21:30 UTC):
> `stale=True confidence=0.889`, reason `coverage_collapse` — the
> 2026-05-15 (Fri) NYSE session has only **506 tickers = 7%** of the
> ~7,634 trailing-20-session avg (MAX(date) is current so the recency
> check passes; coverage cratered underneath it — same failure class as
> the prior 91% collapse). Core ETFs SPY/GLD/IWM/SH/PSQ stop at
> 2026-05-14. Canonical fix is the existing bounded heal
> (`prices_daily_freshness` → `daily_bars --param repair_gaps=true`).
> **Decision (operator, 2026-05-17): report-only — no manual repair;
> left for the next `run_data_operations.sh` self-heal cycle to clear.**
> Re-check this entry after the next cycle; if still red, the bounded
> heal is not converging and needs root-cause (why did 2026-05-15 ingest
> only 506/7,634?). Not caused by the concurrent reversion/backtest
> session (backtests read prices_daily, they don't write daily_bars).

**Mandate (operator, verbatim intent):** "100% data, no gaps, no
bullshit, runs on its own — I cannot babysit this." This applies to the
WHOLE data layer, not just daily bars. The 2026-05-15 build delivered
true end-to-end auto-heal for `prices_daily` ONLY (zero-tolerance
completeness invariant + Step-4 auto-heal loop in
`run_data_operations.sh`). Every other source is currently
*detected + hard-gated* (red blocks the emit / engine sweep) but
*escalates to the operator* instead of self-healing. That residual
babysitting is unacceptable per the mandate — close it.

**Scope — bring each source to the same bar as `prices_daily`** —
`[lane: data-lane-mine] [gate: none] [needs operator decision: no]
[effort: L]` **VERIFIED GENUINELY OPEN 2026-05-18:** only
`prices_daily_completeness.py` is an ungameable completeness invariant.
The other 6 sources have `*_freshness` checks + `healable=True` re-pull
HealSpecs (`tpcore/selfheal/registry.py` L114-177) but NO completeness
invariant module — `ls tpcore/quality/validation/checks/` shows no
`fundamentals/corporate_actions/earnings/sec/macro/liquidity/classif`
`_completeness.py`. Auto-heal-via-re-pull exists; the *zero-tolerance
physical-truth invariant* per source does not. This is the binding
residual of the "runs on its own" mandate:
1. ✅ **`fundamentals_quarterly`** (FMP) — SHIPPED PR #172
   (`fundamentals_quarterly_completeness.py`, MAX_QUARTERLY_GAP_DAYS=100,
   `_infer_missing_period_ends` healer-symmetric). HealSpec routed to
   `fundamentals_refresh` stage. Zero-tolerance gap invariant.
2. ✅ **`corporate_actions`** (Alpaca) — SHIPPED PR #174
   (`corporate_actions_completeness.py`). Composes existing
   `tpcore.ingestion.csv_archive.detect_shrinkage` at
   `GATE_SHRINKAGE_THRESHOLD_PCT=0.0` (zero-tolerance vs the 20% WARN
   band the detector ships with). Live DB row count must be ≥ latest
   CSV archive snapshot.
3. ✅ **`earnings_events`** (FMP) — SHIPPED PR #181
   (`earnings_events_monotone.py`, `platform.earnings_events_count_snapshot`,
   per-ticker EARNINGS_BEAT count monotone-non-decrease, HealSpec routed
   to `earnings_refresh`). KNOWN GAP resolved 2026-05-20 — see follow-on.
   - ✅ **DONE 2026-05-20 — NO_BEAT sentinel ingestion (Path B,
     surfaced 2026-05-20 by the `earnings_events_monotone` P0 source
     3/5 PR):** `scripts/backfill_earnings_events.py::_classify_earnings`
     now emits a `NO_BEAT` sentinel row when `actual_eps` is present
     but doesn't clear the >5% beat threshold (miss, in-line,
     zero-estimate-with-non-positive-actual, negative-estimate);
     `magnitude_pct = NULL` on NO_BEAT rows. The monotone invariant
     SQL filter widened to
     `event_type IN ('EARNINGS_BEAT','EARNINGS_NO_BEAT')` so the
     monotone-on-the-union now gates against truncation AND
     missed-detection from FMP outages. Downstream consumers
     (`vector/backtest.py`, `catalyst/backtest.py`) still filter
     `event_type='EARNINGS_BEAT'` — NO_BEAT is invisible to them, no
     change needed. No schema migration (free-text `event_type`
     column accommodates the new literal; snapshot column
     `beat_count` retains its name with documented semantics shift to
     reported-earnings count). Rationale preserved here rather than
     deleted: the KNOWN GAP was "BEAT-only ingestion can't catch
     missed-detection from FMP outages" — Path B (NO_BEAT sentinel)
     was the chosen resolution over Path A (per-quarter completeness
     check) because it requires no quarter inference and is honest
     about the underlying ingestion population.
     `[lane: data-lane-mine] [gate: none] [needs operator decision: no]
     [effort: M] [resolved: 2026-05-20]`
4. ✅ **`sec_insider_transactions` / SEC filings** (EDGAR) — SHIPPED PR
   #179 (`sec_insider_monotone.py`,
   `platform.sec_insider_row_counts_snapshot`, per-ticker COUNT(*)
   monotone-non-decrease, HealSpec routed to `sec_filings` stage with
   `repair=true`). Append-only Form-4 invariant ⇒ ANY negative delta on
   ANY ticker FAILs.
5. ✅ **`macro_indicators`** (FRED) — SHIPPED PR #168
   (`macro_indicators_completeness.py`, per-cadence
   DAILY/WEEKLY/MONTHLY zero-tolerance check;
   `_expected_dates_for_cadence` healer-symmetric;
   `WEEKLY_ANCHOR_WEEKDAY=3` Thursday). HealSpec routed to
   `macro_indicators` stage.
6. **`liquidity_tiers`, `ticker_classifications`** — invariant +
   auto-heal/recompute. **STILL OPEN as P1.** Both are derived/recomputed
   from upstream sources; completeness shape is "every active T1/T2
   stock has a current row" — different from the append-only Form-4
   / monotone-BEAT pattern. Next slice.

**ARCHITECTURE MANDATE (binding — the shape, not negotiable):**
Self-heal is a GENERIC `tpcore` capability, NOT per-source bash.
1. **One self-heal orchestrator in `tpcore`**, beside the validation
   suite (detector + healer in the same layer). Input: the suite
   result. Per red check → dispatch to the registered healer for that
   source → bounded retry → re-validate → escalate if exhausted or
   unhealable. Pure Python, unit-testable with fake healers.
2. **Each data feed contributes only a declarative `HealSpec`**:
   {invariant = the existing validation check; canonical repair =
   which `ops.py --stage X --param …`; is-auto-healable; bounded
   retry/backoff policy}. Adding a source = registering a spec —
   ZERO bash edits, zero new branches.
3. **Heal executes ONLY via the canonical `ops.py --stage` infra.**
   The orchestrator INVOKES it; it never reimplements ingestion. No
   one-off scripts. (Standard: data_adapter_pipeline.md.)
4. **Every HealSpec is BOUNDED/targeted.** Proven 2026-05-15: a
   whole-universe `force_refresh` exceeds the 3600s stage timeout and
   can never self-heal. Targeted repair only (the `repair_gaps`
   pattern: re-pull just the invariant-flagged tickers/window).
5. **Detector/healer symmetry.** The healer's target set is computed
   from the SAME code as the check (cf. `_evaluate` shared by
   `check_prices_daily_completeness` + `compute_gap_repair_targets`)
   so they can never disagree.
6. **Process concerns stay in the bash wrapper, thin:** never emit
   `DATA_OPERATIONS_COMPLETE` unless 100% green; self-exclusion lock;
   post-close/`tpcore.calendar` gating. `run_data_operations.sh`
   becomes a thin caller of the tpcore orchestrator.
7. **`prices_daily` is the reference implementation, migrated INTO
   the orchestrator** — not a bash special case. One canonical
   mechanism, no N variants (operating-identity: symmetry/standard).

**Per-source design constraints (within the architecture above):**
- Each invariant is ungameable: physical-truth, zero-tolerance, no
  recency window, no percentage knob. Scoped to exactly the data the
  engines depend on.
- Honest heal only: a source's HealSpec must actually be able to fix
  that source's failure class. No dishonest cross-source "heal";
  not-bars-fixable → escalate, never fake-green.
- **No lazy vendor-blame.** A shortfall on authoritative data (SEC
  EDGAR especially) is OUR ingestion defect until proven per-ticker
  against the source. Threshold recalibration only after the our-gap
  hypothesis is empirically killed.
- Each source's required tickers registered where the freshness check
  can see them; add/retire the matching `audit_data_pipeline.py` check in
  the same change.

This is the path to the operator never touching data again. Until every
item above is done, the "runs on its own" mandate is only partially met
and that must be stated plainly, not glossed.

## #186 — Remaining deterministic data agents

- ✅ **candidate (5): audit-driven referential remediation — DONE
  2026-05-17.** `tpcore/auditheal/` — structured cross-table audit
  (`tpcore/audit/cross_table.py`, persisted to `data_quality_log` as
  `cross_table_audit.*` rows) + bounded `cross_ref_cleanup` remediation
  loop + ENFORCED Step-3 gate (previously theatre: `audit_all_tables.py`
  always exited 0, a 🔴 printed and the cycle continued). Launch scope
  strictly the two `tradier_options_chains` checks (expired / orphan);
  all other cross-table checks are escalate-only. PRs #26 (P1 structured
  audit + persistence), #28 (P2 `tpcore/auditheal` loop, dark), #29
  (P3 wire Step 3 + enforce gate).
- **candidates (3)/(4): largely realized by #165** (per-feed cadence
  profile, TRIGGER facet, TARGETING, PUBLICATION — see WEEK GOAL §3a-c
  above). Remaining: incremental per-adapter targeting/probe rollout
  (each a one-entry increment, not unbuilt architecture).
- ✅ **candidate (6): schema/contract-drift sentinel — DONE 2026-05-17.** `tpcore/ingestion/adapter_contract.py` — declared `ADAPTER_CONTRACTS` SoT (all 12 CSV-first feeds; clockwork drift test == CSV-first feed set); `assert_contract_populated` raises before load when a required adapter-output field is systematically empty across a non-empty pull (producer hard-stop; symptom-level detection; escalate-only, no auto-heal); 4 high-risk feeds enforced (fred_macro/iborrowdesk_borrow_rates/finra_short_interest/apewisdom_social_sentiment), rest `guard_pending`; thin Step-4c `adapter_contract` known_knowns check adds coverage/visibility + 24h-escalation FAIL. PRs #32 (P1 registry+helper dark) / #33 (P2 enforce 4 high-risk handlers) / #35 (P3 thin Step-4c check). (3)/(4) realized by #165; (5) auditheal done; **(6) done** ⇒ remaining deterministic-agents work = the Data Supervisor (Escalation & Hardening Ladder rung 2) + #187 LLM triage (rung 3).

## Naming convention sweep — across the board (2026-05-21)

The operator noticed module naming drift: `engine_llm_triage.py` puts the lane FIRST, while `llm_data_triage.py` / `llm_data_recovery.py` / `llm_lab_emitter.py` put the `llm_` prefix FIRST. Same logical kind of module, different filename pattern. The drift snuck in because `docs/STYLE_GUIDE.md` §Naming only documents engine/score/service IDENTIFIER conventions (glossary-pinned, deprecated-blacklist) — there's **no rule for Python module filename patterns**.

`[lane: docs + tpcore + ops] [gate: structural sentinel test] [needs operator decision: pick the canonical pattern] [effort: M — convention doc + sentinel + ~25-30 file renames]`

**Scope (across-the-board sweep):**

1. **Document the convention** in `docs/STYLE_GUIDE.md` §Naming. Pick one canonical pattern. Recommended: `llm_<lane>_<purpose>.py` everywhere (puts the LLM prefix first universally; matches the majority that already follow this — `llm_data_triage`, `llm_data_recovery`, `llm_lab_emitter`). Engine-lane outlier renames to `llm_engine_triage.py`.
2. **Add a structural sentinel test** that walks `ops/` + `tpcore/` and asserts every `llm_*` / `*_llm_*` file matches the convention. Fails CI on the next drift.
3. **Rename the engine-lane outliers:**
   - `ops/engine_llm_triage.py` → `ops/llm_engine_triage.py`
   - `tpcore/engine_llm_triage/` → `tpcore/llm_engine_triage/`
4. **Audit other module families for hidden inconsistencies:** the `lab_*` family (`tpcore/lab/`, `ops/lab/`, persona files), the `engine_*` family, the validation `check_*` family, the ingestion `handle_*` family. Surface any further drift.
5. **Update the CI check job names** that reference the old paths (`engine-llm-triage deterministic fence` → `llm-engine-triage` per the rename) — `.github/workflows/*.yml`.
6. **Update operator memory + docs** that reference the old names.

**Why now-ish, not blocking:** the inconsistency is cosmetic until someone reaches for the wrong form and produces a third pattern. The sentinel test makes it impossible to drift further. Operator decision needed on canonical pattern (lane-first vs llm-first); rest is mechanical.

**Trigger context:** noticed 2026-05-21 while reviewing the autonomous self-heal stack (PRs #227 / #231 / #233 / #235 / #236 / #239). Operator verbatim: "why didn't they name them consistently?" / "i thought we had naming conventions".

## Engine structural redesign (post-2026-05-15 sweep)

The 2026-05-15 parameter sweeps validated the targeted fixes (Sigma SPY-
regime filter, Reversion Z-relaxation + T3 expansion) at the metric level
but DSR/credibility gates remain structurally blocked.

Sigma archive scoping caveat: the sector-neutral residual idea
(Avellaneda & Lee) is pursued as the Reversion PCA-residual enhancement
below, NOT a Sigma revival. See `archive/sigma/EULOGY.md` for the
archival record.

- **Reversion PCA-residual sweep run + adjudication (#171-175).** `[lane:
  engine-owned] [gate: operator verdict bar — held-back DSR≥0.95 /
  cred≥60 / PBO≤0.20 / trades-param≥25 / ≥150 held-back trades / no
  single-crisis PnL concentration] [needs operator decision: yes —
  adjudication on sweep results] [effort: operator-run sweep]` The Lab-
  candidate **build** shipped 2026-05-20 (PR #187 — Avellaneda-Lee PCA-
  residual `signal_mode` opt-in, byte-identical-when-off; spec
  `docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-
  candidate.md`). Live `reversion/scheduler.py` + plugs UNTOUCHED per the
  Sigma lesson. **Remaining:** (a) operator runs the sweep via
  `python -m ops.lab --candidate reversion_pca_residual --target-engine
  reversion --intent fold_existing`, spending 2 trials against the SP-A
  cumulative ledger (primary signal + the ONE pre-declared volume-overlay
  robustness arm); (b) operator reads the dossier verdict against the
  bar above; (c) on SURVIVED → ECR-MODIFY `signal_mode=pca_residual` +
  follow-up #173 live `setup_detection` parity (deferred until sweep
  clears). Survivorship leg already wired (full wipe-out at terminal-
  delisting close per Shumway 1997; `survivorship_inclusive=False` caps
  credibility).

## ⚠ LOCAL-LLM-BRIDGE — required for all 4 LLM lanes (operator decision 2026-05-21)

**Operator binding 2026-05-21 post-gate-pilot:** **no Anthropic API credit
top-up**. All LLM lanes that currently call `AsyncAnthropic.messages.create`
via the API key path must instead route through the **operator's local
Claude Max Pro session** (i.e. the same Claude Code session the operator
is actively running). The build proved (gate pilot PASS, dossier
`docs/lab/gate_pilot/2026-05-21-gate-pilot-PASS.md`) that the loop CAN
find edges; the API billing path is rejected as a production posture.

**Affected lanes (all 4 — same fix shape applies to each):**
1. `ops/llm_edge_finder_sdk.py` (Task #25 T9) — `make_sdk_llm_callable`
2. `ops/llm_lab_emitter.py` (SP-G PR #152) — uses `_default_pr_runner` →
   `default_pr_runner` shared via `ops.llm_data_triage`
3. `ops/llm_data_triage.py` (Epic E Phase 3, data lane) — shared SDK surface
4. `ops/engine_llm_triage.py` (Epic E Phase 3, engine lane)

**Design sketch (single-source the bridge):**
- New module `ops/llm_local_bridge.py` — implements the same async callable
  contract (`(system_prompt, user_prompt, transcript) -> dict`) but
  delegates to the operator's local Claude session via the Claude Code
  Agent SDK or via a structured prompt-paste-to-file → operator-reply
  round-trip.
- Reuse the existing shared surface (`ANTHROPIC_MODEL`, `_AuthSkip`,
  `scrubbed_env`) — replace the `AsyncAnthropic` instantiation with the
  bridge.
- Fallback path: if the operator is offline, lane co-task sleeps + emits
  a `LAB_FINDER_BRIDGE_OFFLINE` event for the §12 dashboard.

**Order of work (most-load-bearing first):**
- `[lane: ops] [decision: made] [effort: M]` Task #25 edge finder bridge
  first — this is the only lane that's autonomous-loop-critical AND was
  built to call the API last (T9).
- `[lane: ops] [decision: made] [effort: M-S each]` Then SP-G emitter +
  data-triage + engine-triage in parallel since they share `default_pr_runner`.

**Hosting posture (post-bridge):**
- **Edge finder + 3 other LLM lanes: LOCAL-ONLY** via the bridge. NOT on
  Railway (Railway can't reach the operator's Claude Max session).
- **Rest of the platform (data ops + engines + daemons): Railway** per
  the existing Pre-Railway migration roadmap (TODO L662 archive substrate
  + R3 object storage). The LLM lanes stay on the operator's Mac.

**Why this is the right call:** API credits at scale ($0.01-0.05/turn ×
10 turns × 3 specs × N runs/day × 4 lanes) compounds to real money the
operator already pays for via the Max subscription. Routing the lanes
through the same session avoids double-billing.

---

## ⚠ RUN-EVERYTHING-TO-SURFACE-BUGS (operator directive 2026-05-21)

**Operator directive post-gate-pilot:** **run EVERY component end-to-end
against the real system** so we surface all the design-vs-real-data
drift bugs (like today's 7 column-name mismatches + LLM-shape gap) +
make the self-heal layer airtight before scaling up.

The gate pilot exposed bugs no mocked test could catch. The next
discoveries land via THE SAME PATTERN: actually-run.

**Components to actually run:**
- `python -m ops.llm_edge_finder` (post-bridge) — exercise Phase A→F end-to-end
- `python -m ops.llm_lab_emitter` against each PAPER engine
- `python -m ops.llm_data_triage` against a synthetic data escalation
- `python -m ops.engine_llm_triage` against a synthetic engine escalation
- `python scripts/ops.py --update` — full data sweep — surface column drift
- `bash scripts/run_all_engines.sh` — every PAPER engine fires
- `python -m ops.engine_service` — DA-3 consolidated daemon
- `python -m ops.data_repair_service` — recovery lane
- `python -m ops.llm_triage_service` — 5-cotask daemon

**Per actual-run discovery cadence:**
1. Run the component.
2. Capture every error.
3. Decide: real-bug-to-fix vs design-vs-data drift.
4. If self-heal coverage MISSING: add the HealSpec.
5. Re-run until green-as-cat-piss.
6. Commit fixes + log to defect_register if appropriate.

Self-heal coverage shouldn't have any "we'll find it in production" gaps.

---

## Task #25 — autonomous LLM+quant edge finder (follow-on epic)

The richer ambition the operator raised 2026-05-20 when SP-G's scope was
locked: an LLM that finds tradeable edges **on its own**, driving a real
quantitative toolkit (statsmodels / arch / linearmodels / scikit-learn /
scipy.stats — factor / time-series / regime models), internalising
trading-environment context from the curated reference set
([[ref_carver_systematic_trading]], [[ref_chan_algorithmic_trading]],
future adds), and operating a disciplined
**data → analysis → idea → Lab → graduation gate** pipeline. Distinct
from SP-G (the thin advisory spec-emitter that JUST shipped its design
spec via PR #146 and is in build); SP-G is the minimum, hardest-fenced
form of the LLM-proposes / deterministic-gate-disposes fence, task #25
inherits that fence verbatim and extends it with autonomous search.

**Status:** backlog, **unblocked** — SP-G build landed via PR #152 (2026-05-20). Only remaining gate is the operator's explicit go to start the brainstorm. Operator answered "keep going / stick to the plan" 2026-05-20 when offered an early restructure of SP-G into this larger ambition — task #25 stays its own follow-on epic with its own brainstorm → spec → plan → build sequence.

**HARD CONSTRAINT (inherited from
[[project_research_llm_edge_discovery]] + [[project_ml_research_track]]
— binding, non-negotiable):** the commissioned-expert verdict is that
naïve automated edge-search inflates the DSR `n_trials` /
multiple-testing count and manufactures overfit "edges" that die
out-of-sample. The LLM proposes; the deterministic gate (DSR ≥ 0.95 ∧
credibility ≥ 60, cumulatively deflated via the SP-A ledger) disposes.
Specifically:
- (a) Every candidate routes through the existing graduation gate; the
  LLM never bypasses or re-weights the gate.
- (b) The LLM's exploration IS counted against `n_trials` honestly.
- (c) Prefer expert-blessed framings (meta-labeling / cross-engine
  combiner) over free-form strategy mining.
- (d) Forensics / allocator / governor / graduation-gate stay
  deterministic. The autonomous finder sits ATOP them, never
  re-implements them.

**Operator framing 2026-05-20 (carry into the brainstorm):** the
reference toolkit is chosen to teach TWO things — (1) the **trading
environment**: market structure / micro-structure and how everything
interconnects; (2) a **repeatable workflow**: collect data → analyse →
find trade ideas to automate. Operator: *"this is what the LLM edge
finder will do … future roadmap."* The autonomous finder is intended
to internalise (1) as domain context and operate (2) as its loop —
NOT free-form strategy mining but a disciplined environment-aware
pipeline.

`[lane: engine-owned] [gate: SP-G build landed + operator explicit go]
[needs operator decision: YES — kick-off brainstorm] [effort: XL —
multi-PR epic]`

### Task #25 — STATE UPDATE 2026-05-22 (v1.0 SHIPPED + first real edge signal)

**v1.0 SHIPPED end-to-end** (all 12 build tasks + Phase D-F + persona v2.0 → v2.1):
- T1-T12 + Phase D auto-promote + Phase E/F outcome monitor + auto-retire all landed
- Real-API gate pilot PASSED (PR #270): 5 emissions across 5 engines, all structurally distinct from the 4 failed deep-research candidates, operator-judged ✓
- Prompt caching: 3.7x token cost reduction (PR #266)
- v2.1 persona: testability pre-check directive after first Lab probe FAIL (PR #273)
- 529 self-heal added (PR #275) — known platform-overload error has recovery logic now

**First real edge signal from autonomous finder — 2026-05-22:**

Engine surface enrichment (PR #277 — catalyst PEAD-only mode + hold_days knob) unblocked the `catalyst_pead_expansion_range` candidate. Re-probe verdict:
- Sharpe **+1.24** (was +0.18 pre-enrichment)
- Profit factor **3.50** (was 1.69)
- Win rate **70%** (was 50%)
- n_trades 10 (was 2; gate ≥30)
- DSR still ~0 (gate ≥ 0.95) — fails because (a) test universe = 15 names; (b) cumulative trial count

**Headline:** the autonomous finder DID find a real edge. The constraint isn't hypothesis quality — it's engine surface (LAB_TARGET expressiveness) + test universe scope.

### Engine-surface-enrichment epic (active, in progress)

Goal: open enough LAB_TARGET knobs in each engine for the LLM's structurally-distinct hypotheses to be testable. Pattern established by catalyst PR #277.

- [x] **catalyst** — `beat_30d_only` PEAD arm + `hold_days` Lab knob (PR #277)
- [x] **reversion** — partial-axis `regime_filter_v1` choice (PR #278 + #282 probe-driver wiring)
- [x] **Lab orchestrator** — `--param-overrides` now reaches engines (PR #279, silently dropped before)
- [x] **sentinel** — `macro_stress_count` mode + 4 threshold knobs + signal count (PR #286)
- [x] **persona v2.2** — exclude canary from finder's target_engine choices (PR #284)
- [ ] **vector** — BLOCKED on `insider_sentiment` daily-granularity backfill. Adapter work, not engine.
- [—] **canary** — EXCLUDED per operator clarification 2026-05-22 (heartbeat, non-graduating per spec §4b).

**Epic structurally complete 2026-05-22** (vector pending adapter backfill).

### Probe-readiness matrix (operator-discretion to invoke; ledger spend per probe)

| Candidate | Engine | Surface | Probe verdict | Next move |
|---|---|---|---|---|
| `catalyst_pead_expansion_range` | catalyst | ✅ | Sharpe +1.24, PF 3.50, 70% WR; n_trades=10 on 15-ticker test universe | T1+T2 production-universe re-probe |
| `reversion_earnings_season_5d_range_normal` | reversion | ✅ | n=0 on trend_only (range axis rare) | swap PARTIAL_AXIS_CHOICE to macro_only/vol_only |
| `sentinel_macro_stress_gate_v1` | sentinel | ✅ | not yet probed | invoke probe with mode=macro_stress_count |
| `vector_beat_reversal_insider_filter_v1` | vector | ⚠️ adapter | STOPPED at substrate gap | insider_sentiment daily backfill |
| `canary_range_reversion_5d_earnings_conditional` | canary | — | misemission; persona now excludes canary | — |

`[lane: engine-owned] [signal: real-edge-found 2026-05-22 catalyst PEAD]`

## Deep-research spike adjudication — Lab-candidate backlog (2026-05-19)

Decision record from the two commissioned edge-research spikes
(`deep-research-report.md` / `deep-research-report2.md`, expert-reviewed
2026-05-19). Binding lens: the DSR/n_trials overfit verdict is THE
constraint. Every accepted edge is ONE pre-registered single-primary-spec
Lab candidate routed `python -m ops.lab` → DSR/credibility graduation gate
→ ECR (`python -m ops.engine_sdlc`); honestly counted against n_trials; at
most ONE pre-declared robustness check (counted as a trial, NOT a sweep);
the reports' own success bars preserved/strengthened, never relaxed. NEVER
bypass the gate. Meta-track cross-ref: #242. The reports' multi-value
grids (`--pca-components 8,10,12,15`, `--family-weights` menus) ARE the
n_trials hazard and are explicitly rejected — single config only.

- **Reversion PCA-residual — CORROBORATED, folds into #171-175 (no new
  item).** `[lane: engine-owned] [gate: operator verdict bar — held-back
  DSR≥0.95/cred≥60/PBO≤0.20/trades-param≥25/≥150 held-back trades/no
  single-crisis PnL] [decision: fold] [effort: L]` Both spikes' flagship
  rec (Avellaneda–Lee daily PCA residuals) IS #171-175 — do NOT create a
  duplicate. Literature Sharpe (1.44, 1997–2007) is NOT evidence it
  survives THIS data/period/costs. Genuinely-new nuance captured as
  sub-notes under #171-175 ONLY, each at most ONE pre-declared robustness
  check (NOT sweep dimensions): (a) volume / "trading-time" overlay
  (Avellaneda ETF 1.51); (b) ETF-residual crisis fallback when systematic
  correlation dominates PCA. Cross-ref #171-175, #242.

- **Sentinel — graduated Bear Score (single-spec Lab candidate).**
  `[lane: engine-owned] [gate: maxDD reduction ≥30% vs base + ulcer
  improvement + median inverse-ETF hold <20d + no single-recession PnL
  concentration] [decision: ADOPT — route via ops.lab] [effort: M]`
  Graduated (scaled-defense) vs binary flip. ONE pre-registered config,
  literature-anchored thresholds (Sahm ≥0.50, CFNAI-MA3 ≤−0.70,
  SOS ≥0.20 — external, not fitted: the anti-overfit anchor): weights
  0.30/0.15/0.20/0.15/0.20 (Sahm/SOS/curve/CFNAI/HY-OAS), bands
  0.45/0.60/0.80, inverse-ETF cap 25% of defensive capital, Treasuries/
  gold-first. n_trials caveat: weight×band surface is large — ONE spec
  only, ONE pre-declared robustness check max. Data prereq: confirm
  credit-spread (hy_spread/credit_spread) series wired into live FRED
  ingestion BEFORE the Lab run. Via `python -m ops.lab --candidate
  sentinel_bear_score --target-engine sentinel --intent fold_existing` →
  graduation gate → ECR; counts against n_trials; NEVER bypass the gate.
  Offline probe verdict (`scripts/probe_sentinel_activation.py`,
  `data/sentinel_activation_probe/2026-05-21.json`,
  `[defect_ref: SENTINEL-ACTIVATION-DORMANT-2026-05-21]`): FAIL —
  structurally dormant; OOS (2024-01-01 → 2026-05-21) composite
  p95=0.237 < 0.45 LIGHT floor, 100% DORMANT (872/872 days);
  full-window (2018-01-01 → 2026-05-21) DORMANT=96.2% with only
  0.4% DEEP days — the FAILED Lab probe's zero trades is the
  composite never lighting up, NOT a downstream threshold-clipping
  defect.

- **Catalyst — event-confirmed insider-cluster drift (single-spec Lab
  candidate; 8-K leg data-gated).**
  🔴 **FAILED 2026-05-21.** Probe ran via `event_confirmation_mode=
  positive_beat_30d`. Verdict: DSR=0.0000, credibility=45, held-back
  n_trades=**1** (vs gate ≥150). Crucially: the legacy `off` arm
  ALSO FAILS (n=1 held-back trade) — the underlying catalyst engine
  itself does not currently pass the Lab gate; the variant cannot
  improve on a non-viable base. Root cause: 15-ticker
  `CATALYST_TEST_UNIVERSE` × cluster gate floors
  (≥3 distinct insiders + min aggregate USD) produces too few
  eligible events in the 2024-2025 window. 80 trials spent →
  `lab_trial_ledger.catalyst` cumulative = 80 (subsequent Catalyst
  probes face a strictly harder DSR-deflated gate). Dossier:
  `docs/lab/2026-05-21-catalyst_insider_drift-FAILED-seed0.md`.
  Genuine falsification; NOT re-run with tweaked params
  (n_trials laundering). **Open follow-up:** signal-strength is the
  binding constraint — fix is universe-expansion + cluster-floor
  re-calibration (NOT a candidate edit). Matches the standing
  CLAUDE.md note "all engines currently FAIL the DSR/credibility
  gate — signal strength is the binding constraint" — empirically
  reconfirmed across all 4 deep-research candidates this week
  (Vector / Reversion / Sentinel / Catalyst all FAILED).
  Defect logged: `[defect_ref: CATALYST-SIGNAL-SPARSITY-2026-05-21]`
  Original spec preserved below for reference:
  `[lane: engine-owned] [gate:
  held-back DSR≥0.95 + cred≥60 + PBO≤0.20 + ≥150 held-back trades +
  positive post-2020 held-back alpha + better hit-rate than pure
  post-beat drift] [decision: ADOPT (insider-cluster primary) — route via
  ops.lab] [effort: M]` Plain large-cap PEAD discarded (both spikes;
  too arbitraged). Primary leg = non-routine insider-cluster buying
  (≥2 insiders, exclude routine, 30d window) confirming a positive
  corporate event/earnings beat — DATA READY (WEEK-GOAL SEC backfill:
  646,107 Form-345 rows 84.1% T1-T2). 8-K item-level drift leg is GATED:
  do NOT run until 8-K item-code parsing is confirmed (backfill landed
  237,680 filings 85.1% but item-level extraction not verified). ONE
  primary config, entry filing+1, hold 20/60d. Via `python -m ops.lab
  --candidate catalyst_insider_drift --target-engine catalyst --intent
  promote_new` → graduation gate → ECR; counts against n_trials; NEVER
  bypass the gate.

- **Momentum — vol-managed 12-1 + earnings/revenue overlay.** `[lane:
  engine-owned] [gate: held-back DSR≥0.95 + lower crash DD than current
  paper spec] [decision: DEFER — paper-research lane] [effort: M]` Real
  structural direction (vol-targeting + fundamental overlay) but lowest
  (impact×prob)/effort vs the binding constraint; monthly rebalance ⇒
  slow DSR evidence accrual; engine already paper-trading + self-gated.
  Deferred to the paper-research lane; promote to a single-spec Lab
  candidate only if a top-three slot frees and capacity exists.

- **REJECTED: Sigma sector-neutral failed-break / compression+
  failed-expansion residual fade.** Sigma ARCHIVED 2026-05-16 (two honest
  FAILED gate attempts; `archive/sigma/EULOGY.md`). The sector-neutral
  residual idea is already the Reversion enhancement #171-175 per the
  EULOGY scoping caveat — NOT a Sigma revival, NOT a new item. Durable
  decision; do not re-litigate.

- **REJECTED: S2 systematic short-squeeze engine.** Data-parked
  (point-in-time securities-lending + options-positioning history absent;
  FINRA short-interest structurally bi-monthly). Both spikes independently
  say archive/manual-only; matches the existing platform decision. Not
  backtestable now — a DATA limitation, not modeling. Reopen ONLY if
  point-in-time securities-lending + options-positioning history is
  acquired; then route as a single-spec Lab candidate. Do not re-litigate.

## ⚠ PRE-RAILWAY MIGRATION BLOCKER — archive substrate (LOCKED design 2026-05-18)

**Do NOT let a Railway cutover silently ship the broken substrate.**
The vendor-truncation `shrinkage_detector` + the whole CSV-first
archive are hardwired to a persistent **local FS**
(`csv_archive.repo_data_dir()` = `Path(__file__).parents[2]/"data"`;
no env/volume override; `railway.json` has no volume). On Railway's
**ephemeral container FS**: detection silently always-passes (empty
`data/` → emits OK = "checked nothing" — worst class for live money),
`csv_archive_presence` flaps, recovery substrate evaporates. Expert
verdict (2026-05-18): snapshot-vs-single-prior-CSV is the wrong
substrate even on the Mac (poisoned baseline; gradual <20%/snapshot
erosion invisible; only 5 full-snapshot sources).

**LOCKED design (operator-approved 2026-05-18; built AT migration,
not now — Railway paused, re-enable deferred until an engine proves
edge):** `[lane: data-mine][gate: Railway-re-enable][decision: made][effort: L]`
- **Detection → D2:** persist per-source row-count / min-max-date /
  coverage to **Postgres** each ingest; shrinkage = deviation vs
  rolling-median of durable history (host-agnostic; reuses the
  `prices_daily_completeness`/freshness pattern; fixes the local
  flaws too). [D3 = fold full-snapshot sources into a completeness
  physical invariant — stronger/larger; D2 is primary.]
- **Recovery → R3:** CSV-first archive → an **S3-compatible
  object-storage bucket attached to the service** (Railway-attached /
  Supabase Storage / R2 / S3) via S3 API + env-injected creds. Keeps
  the CSV-first canonical workflow; host-agnostic. [R2 Volume =
  weaker fallback; R4 Postgres-BYTEA rejected — 8GB Supabase budget.]
- A bucket alone is necessary-for-recovery, NOT sufficient: detection
  must become DB-derived regardless. Exact Railway bucket wiring is a
  migration-time detail to verify vs current Railway docs.
- **Zero-risk preps done now (separate PR, no Railway infra):**
  (1) `repo_data_dir()` honors `TP_DATA_DIR` env (default unchanged)
  — the R2/R3 seam; (2) empty-archive shrinkage path → WARN/UNKNOWN,
  never silent OK — a "no fake-green" latent-bug fix.
- Memory: `project_railway_archive_substrate_migration`. Sequencing:
  re-base detection onto Postgres BEFORE Railway re-enable.

## Publishing

- **Publish a GitHub gist of the entire project.** Scope: everything —
  architecture (`docs/MASTER_PLAN.md`), database + dataflow
  (`docs/DATABASE_AND_DATAFLOW.md`), operations (`docs/OPERATIONS.md`),
  style guide, engine specs (Sigma, Reversion, Vector, Momentum) with
  credibility scorecards, parameter-search methodology + walk-forward +
  held-back DSR, 5-plug architecture, FilterDiagnostics + baseline-
  equivalence framework, dashboard, the Railway/Supabase ops story.
  Public-facing — review for any embedded keys, paths, or PII before
  publishing.
- **Publish to PyPI.** Open scope — decide what gets packaged. Most likely
  candidate: `tpcore/` as a standalone library (RiskGovernor, AAR,
  parity, backtest harness, filter diagnostics, baseline-equivalence) —
  the parts that are genuinely reusable outside this repo. Engines
  (`sigma/`, `reversion/`, `vector/`, `momentum/`) and `platform/`
  schema stay private. Prereqs: pick a name (likely not `tpcore` —
  reserved/generic), pin a license, add `pyproject.toml` package
  metadata, set up `python -m build` + `twine upload`, decide on
  versioning scheme. Same key/PII review as the gist.

## Review-found defects — the durable surface (#254 register)

A review-found defect (found by verify-before-acting / a failing test /
a code review — NOT a deterministic-agent escalation) no longer lives
ONLY as an ad-hoc TODO line. ✅ **Consolidated Defect Register — BUILT
2026-05-19 (#254: DR1 #90, DR2 #91, DR3 this PR).** The durable home is
`python -m ops.defect_register log --ref <#NNN|slug> --summary "…"`
(retention-exempt `REVIEW_DEFECT_LOGGED`; resolve with `… resolve --ref
<r> --pr <#NNN|sha>`). It composes BOTH Escalation & Hardening Ladders
verbatim + the review class, joined by `defect_ref`; surfaced read-only
on the dashboard Health tab and via `python -m ops.defect_register
list`. **Convention:** a TODO line for a still-open review-found defect
carries a `[defect_ref: X]` tag and MUST have a matching open
`REVIEW_DEFECT_LOGGED` (CI forcing-test — a review defect cannot live
only in TODO.md and be forgotten). `[lane: ops] [gate: none] [needs
operator decision: no] [effort: done]`

- **OPEN — `test_lab_ntrials_ledger.py` collection-time `del sys.modules`
  eviction defect.** `[lane: engine] [defect_ref: #148] [gate: none]
  [needs operator decision: no] [effort: S]` Pre-existing engine-lane
  defect (NOT a code-sweep finding — its own tracked task #148, surfaced
  alongside the SP-A n_trials ledger work): `tpcore/tests/
  test_lab_ntrials_ledger.py` does a collection-time `del sys.modules[...]`
  that evicts a shared module — **subset-collection-order-only**; the full
  single-process suite is GREEN (no production / CI-gate impact).
  Canonical fix = scope the eviction per-test (not at collection time).
  Do **NOT** fix opportunistically — it is its own task.

## Discovered follow-ups — RiskGovernor work + architecture review (2026-05-17)

Surfaced while making the RiskGovernor real + uniform (branch
`worktree-risk-governor-fix`). Recorded here so they are not lost.

**Architecture epics (operator directives 2026-05-17 — see memory
`project_three_service_architecture`):**
- **Event-driven engine services (P1 epic).** Entire engine service
  event-driven: an engine fires the moment its preconditions are met
  (data ready + market closed + setup ready), never on a clock. Time is
  a GATE/precondition, never a TRIGGER. Engine service is already
  event-driven (`DATA_OPERATIONS_COMPLETE`); the allocator is the
  time-driven outlier to convert.
- **Two-daemon consolidation.** Collapse to exactly two daemons: data
  daemon (emits readiness event) + engine daemon. AAR, forensics, and
  the allocator all move INTO the engine daemon (no separate launchd
  jobs).
- ✅ **Declarative `engine_profile` (the vehicle) — DONE 2026-05-20.**
  Per-engine cadence + precondition SoT, same proven pattern as
  `tpcore.feeds` / `tpcore.risk.limits_profile`. Extends the existing
  per-engine data gate ("Per-engine data gates — DONE 2026-05-16"),
  NOT a parallel mechanism: `EngineProfile.data_dependencies:
  frozenset[str]` field added; 7 engines (`reversion`, `vector`,
  `momentum`, `sentinel`, `allocator`, `canary`, `catalyst`) migrated
  byte-equivalent from the hand-curated
  `capital_gate.ENGINE_TABLES`; that dict is now a PEP-562-derived
  read-model over `_PROFILE.data_dependencies` (3 external import
  sites preserved). `capital_gate._required_sources` +
  `failing_sources_for_engine` read from `engine_data_dependencies()`
  directly. New drift clockwork
  `test_dispatchable_engine_declares_data_dependencies` reds CI on
  any PAPER/LIVE engine with an empty declaration. Spec:
  `docs/superpowers/specs/2026-05-20-declarative-engine-profile-
  data-dependencies.md`. Follow-up (out of scope here, tracked in
  spec §7): ECR `data_dependencies` key + planner threading.
- ✅ **Allocator → event-driven — DONE (Sub-project C 2026-05-17, PR #17;
  safety-net heartbeat added 2026-05-20).** Primary trigger: the
  allocator is the first gated step in `ops/engine_dispatch.py`
  (`_dispatch_allocator`), event-driven on `DATA_OPERATIONS_COMPLETE`
  via `ops/engine_service.py` → `scripts/run_all_engines.sh`. The
  idempotency guard is structural and uses
  `tpcore.engine_profile.should_fire` (cadence boundary
  `WEEKLY_FIRST_TRADING_DAY` + `_already_ran` STARTUP-row check +
  fail-CLOSED). Safety net: `ops/allocator_heartbeat.py` +
  `scripts/install_launchd_allocator_heartbeat.sh` (daily cron at
  22:30 UTC; reuses `should_fire` so a daemon-up day is a no-op, a
  daemon-down first-trading-day-of-week fires inline). Two-daemon
  invariant preserved (heartbeat is a sibling cron, NOT in the
  `install_all_daemons.sh` closed-whitelist for-loop). `(engine,
  allocation_date)` unique constraint remains the last-line backstop.

**Pre-existing bugs discovered (NOT introduced by this work; out of
scope here, flagged honestly):**
- ✅ **Allocator `_engines` stale default — FIXED (DONE-stale).** The
  design decision was made and the default unified to a canonical SoT:
  `AllocatorService.__init__` now defaults to `_DEFAULT_ENGINES =
  allocator_eligible_engines()` (`tpcore/allocator/service.py` L44,
  L85-87, L151) — derived from `tpcore.engine_profile`, NOT the
  hardcoded `("sigma","reversion","vector","momentum")`. Decision
  recorded inline (service.py L141-150): **sigma removed** (archived),
  **sentinel intentionally excluded** (defensive macro overlay budgeted
  by `SentinelCapitalGate` 10–20% cap, not the inverse-vol pool),
  **canary excluded by omission** (spec §5a). `_ARCHIVED_ENGINES =
  archived_engines()` (L85) keeps the prune fail-safe. This was a
  pre-existing bug, now closed.
- ✅ **`audit_pipeline.shrinkage_detector` re-keyed — FIXED (DONE-
  stale).** No longer keyed off the never-written `application_log`
  structlog event. `scripts/audit_data_pipeline.py` `_detect_archive_
  shrinkage()` (L184-214) is now **pool-free and disk-only**: it
  compares each `ARCHIVE_SOURCES` source's latest on-disk `.csv.gz`
  archive to its predecessor via `tpcore.ingestion.csv_archive.
  detect_shrinkage` — real persisted evidence, not theatre. Finding
  rendered at L217-260.

**Governor follow-ups:**
- ✅ **Batch-engine slot accounting — RESOLVED 2026-05-19 (B1#82 + B2#87 + A1#88) + per-engine attribution SHIPPED 2026-05-20.** Root fixed, not deferred: B1 introduced the idempotent `record_close`/`risk_close_ledger` arbiter (never-fail-open hardening + reusable primitive); B2 fixed the REAL dual-decrement (reversion/vector `order_manager.reconcile()` `−1` now routes through `record_close`, keyed by the shared bare `open_orders.trade_id`); A1 added the `max(proxy, broker_floor)` never-fail-open last-line raise (opt-in `reconcile_open_floor=True` for momentum/sentinel). **2026-05-20 follow-up SHIPPED:** per-engine broker-floor attribution — `_count_engine_broker_floor` joins broker positions to recent orders via `client_order_id` engine prefix; unattributed positions still count against the gating engine (over-count fail-safe) + `tpcore.risk.unattributed_broker_position` WARNING for operator cleanup; broker without `list_recent_orders` degrades to the pre-change cross-engine count + `tpcore.risk.broker_attribution_unavailable` WARNING (still tighter than proxy-only; never-fail-open invariant preserved). `[lane: platform-overlay (RiskGovernor)] [gate: none] [needs operator decision: no] [effort: S]`
- ✅ **`ALLOCATOR_PRUNED_RISK_STATE` `live_engines` payload — MOOT
  (resolved as a side-effect).** `self._engines` no longer includes
  stale sigma (now `allocator_eligible_engines()` — see the fixed
  allocator default above), so the payload at
  `tpcore/allocator/service.py` L242 is now accurate. No separate
  cosmetic cleanup needed.
- **Verify real-state substrate end-to-end once an engine graduates**
  (allocator feeds `engine_equity`; trade_monitor/AAR feed pnl/
  positions). The `tpcore.risk.equity_unallocated` WARNING surfaces a
  still-placeholder equity — watch for it post-graduation. `[lane:
  platform-overlay] [gate: blocked — no engine has graduated (all 4
  fail DSR)] [needs operator decision: no] [effort: M]` — VERIFIED
  genuinely open AND gated; cannot be actioned until a graduation
  event exists. Park until then.
