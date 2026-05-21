# Task #25 — Spec review against the broader research foundation

**Spec under review:** `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md` (662 lines; merged via PR #213).
**Reviewer instruction:** the operator clarified the research surface 2026-05-21 — *"use the references that I already gave you as initial research then venture out with other sources."* Two operator-anchored framings carry the whole exercise:
**(F1)** Explaining the trading environment — market structure / micro-structure / interconnection.
**(F2)** A repeatable workflow for collecting data, analysing it, and finding trade ideas to automate.
**Pass:** spec-review (NOT a plan, NOT a build). Output is a docs-only PR. Operator decides on the verdict.

---

## §0 TL;DR

- **Phase 4 verdict: GO-WITH-EDITS.** The spec's safety architecture and SP-G composition are tight; the fence stack is build-ready and the v1 success criterion is correctly scoped. But the `MarketSnapshot` payload (§4.1) starves the LLM of macro / sentiment / breadth / event-calendar context the operator already ingests — directly contradicting framing (F1). And the toolkit whitelist (§6.1) is missing two non-negotiable callables (Newey-West HAC SEs; variance-ratio test) that v1 statistical claims cannot be made honestly without. Net: 3 spec edits land first; the in-flight plan is a draft until the edits are merged; re-dispatch planning after.
- **The three blocking findings** (full detail §3):
  1. **`MarketSnapshot` is too narrow.** `prices_daily` + `fundamentals_quarterly` only — no macro (`platform.macro_indicators`), no sentiment (`platform.aaii_sentiment`, `platform.social_sentiment`, `platform.insider_sentiment`, `platform.fear_greed`), no breadth (derived from prices), no event calendar (`platform.earnings_events`, `platform.sec_material_events`), no liquidity (`platform.short_interest`, `platform.borrow_rates`, `platform.spread_observations`). The operator's "trading environment" framing (F1) cannot be satisfied with a price-only snapshot. (`§3.1`, blocking.)
  2. **Toolkit whitelist is statistically unsafe and incomplete.** Plain `OLS().fit()` defaults to homoskedastic SEs — any time-series regression on overlapping returns is *guaranteed* to over-report significance without Newey-West HAC. Variance-ratio (Lo-MacKinlay 1988) is the canonical mean-reversion screen, complementary to ADF, and is *not* in scipy/statsmodels under `adfuller` (it lives at `statsmodels.tsa.stattools.variance_ratio` in some builds, or is computed from `coint`-adjacent primitives). Without these, every `coint`/`adfuller`/`OLS` result the LLM cites is at risk of being a stat-snooping artefact. (`§3.2`, blocking.)
  3. **Pending reference bundle `dsr_ntrials_discipline.md` is the load-bearing safety doc and is undefined.** The spec calls it "mandatory-always-include" (table §7.1) but specifies only the *position*, not the *content*. v1 will run the LLM against an empty mandatory bundle unless authoring is scheduled BEFORE the build PR. (`§3.4`, blocking.)
- **Open operator question (must answer before either spec edits or plan moves):** *Is "venturing out" intended to mean a richer SNAPSHOT (more local data in `MarketSnapshot`) plus richer REFERENCES (more `docs/lab_emitter_references/*.md` files), with the LLM strictly bounded to those at runtime — OR is it a license for the LLM to roam its trained knowledge unguided?* Spec §2.8 forbids runtime browsing (correctly); but the persona language and reference-bundle scope need to be explicit that "venture out" = "broader operator-staged context," not "LLM's free-roaming intuition." See §3.7.

---

## Phase 1 — what the shipped references already internalize

Reading `docs/lab_emitter_references/carver_systematic_trading.md` (101 lines) and `docs/lab_emitter_references/chan_algorithmic_trading.md` (99 lines), the spec's persona / toolkit / fences already lean on the following concepts at point-of-precision:

### From Carver (2015) — `carver_systematic_trading.md`

| Concept | Where it shows up in the spec |
| --- | --- |
| Forecast diversification across uncorrelated rules (Carver §5–§7) | Spec §2.2 single-hypothesis-per-emission is the ANTI-grid-search rule; Carver's "correlation ceiling" framing is the doctrine why. |
| Volatility scaling on EVERY position (Carver §10) | Spec §2.3 "gate is sacred" + Readiness §9 "sizing unchanged" — Carver's vol-target is the *engine's* job, not the finder's. |
| Slow rules dominate fast rules at retail capital (Carver §6) | Spec §6 toolkit omits any sub-daily callable; v1 stays daily, AR(1) order-pinned. The horizon-mismatch warning is implicit. |
| Diversification ceiling = anti-overfit (Carver §15) | Spec §2.1 + §2.10 cumulative `n_trials` ledger is the operationalised Carver ceiling: every rule strictly tightens the gate. |
| Honest cost modelling (Carver §11) | Spec §2.3 — finder doesn't touch entry/exit/sizing, so the engine's existing cost model carries forward. |

### From Chan (2013) — `chan_algorithmic_trading.md`

| Concept | Where it shows up in the spec |
| --- | --- |
| Mean-reversion vs momentum regime conditioning (Chan §2) | Spec §6.1 `adfuller` (stationarity) + `coint` (cointegration) are the only two regime-relevant callables. |
| Stat-arb pairs / cointegrated baskets (Chan §3, §7) | Spec §6.1 `coint` (Engle-Granger 1987). Johansen multivariate cointegration is *not* in v1 — Chan ch. 3 specifically uses it. |
| Kelly / fractional-Kelly sizing (Chan §6) | Spec §2.3 — sizing is engine-owned; finder cannot propose Kelly. Symmetric with Carver vol-targeting. |
| Realistic backtest discipline (Chan §3 — survivorship, look-ahead, snooping) | Spec §2.1 + §2.10 ledger; CLAUDE.md survivorship-free `prices_daily`; Readiness §9 strictly-backward window. Chan §3 IS the operator's prior intuition for the SP-A ledger. |
| Walk-forward (Chan §3) | Spec §8 step 7 — `ops.lab` runs candidate through walk-forward gate. Finder proposes the hypothesis; existing infra evaluates. |

### What both bundles agree on (and the spec inherits)

- The **gate is sacred** — neither Carver nor Chan grants a finder license to relax DSR/credibility. The spec's §2.3 + the `validate_no_gate_override` grep (SP-G `GATE_OVERRIDE_FORBIDDEN_FLAGS`) is the mechanical instantiation.
- The **LLM cannot RUN** Carver/Chan math — it can NAME it. Spec §6.1 is the bridge: `OLS`/`adfuller`/`coint`/`ARIMA(1,0,0)` are the *executable subset* of Carver/Chan vocabulary the LLM has runtime access to.
- **Reference bundles are operator-staged context, not directives.** Both shipped bundles end with the same "this is NOT a directive" maintenance clause. The spec inherits that posture (§7.3).

### Notable Carver/Chan concepts the spec does NOT yet leverage

- **Carver's "speed of trading" decomposition** (combine fast/medium/slow rules at fractional weight). No place to land it in v1 (single-hypothesis discipline) — fine deferral.
- **Chan's regime filter on top of mean-reversion** is *explicitly* named in `chan_algorithmic_trading.md` as a "one-toggle `fold_existing` candidate." Spec §3.2 says "fold_existing > promote_new for early cycles" implicitly but does NOT codify regime filter as a recommended early-cycle hypothesis shape. Low-severity nudge: the persona should *favour* regime filters over parameter sweeps in cycle 1.
- **Chan's mean-reversion half-life (Ornstein-Uhlenbeck fit)** is not exposed via `§6.1`. AR(1) is a proxy but the half-life transform is the standard reporting unit in Chan. Medium-severity gap if the operator wants Chan-faithful output.

---

## Phase 2 — venturing out: US-equity literature gaps relative to the spec

The operator's framing (F1) + (F2) maps onto a well-documented body of academic / practitioner literature. Below is a topic-by-topic critique, citing the canonical source. Coverage targets are representative, not exhaustive: I picked what's actually load-bearing for the operator's two goals.

### §2.1 — Market structure (the (F1) half: "the environment you're trading in")

**Spec coverage:** the spec acknowledges (in `§2`, CLAUDE.md universal invariant) SIP default vs IEX; paper-only via Alpaca; XNYS calendar via `tpcore.calendar`. Beyond that, market-structure context is implicit at best.

**Literature the LLM should be able to NAME (per reference-bundle), even if not RUN:**

- **Harris (2003), *Trading and Exchanges*** — venue fragmentation, order types (MOC/LOC/MOO/LOO auction inputs), tick size rules, the role of market-makers vs HFTs vs retail brokers, LULD halt mechanics. *Especially* `Trading and Exchanges` chs. 4–6 (the auction-mechanism explainer) and ch. 11 (the bid-ask spread decomposition). **Missing from spec.** The pending `market_structure_primer.md` (spec §7.1) is the place this lands; spec gives only the title.
- **O'Hara (1995), *Market Microstructure Theory*** — the price-discovery / adverse-selection canonical text. Specifically: Kyle (1985) lambda, Glosten-Milgrom (1985) information-asymmetry spread decomposition, Roll (1984) effective spread. These are the formal models that ground *why* a mean-reversion edge in liquid large-cap names is harder to find than in mid/small-cap (adverse selection compensation differs). **Spec mentions none.**
- **Amihud (2002) illiquidity ratio** — `abs(return) / dollar_volume` averaged over a window; the gold-standard illiquidity proxy and the *cheapest* "venturing out" beyond the spec's `vol_20d` whitelisted column. Computable from `platform.prices_daily` alone, would slot into the `series_id` whitelist directly. **Spec under-specifies.**
- **Hasbrouck (2007), *Empirical Market Microstructure*** — the contemporary text on price-discovery / data-flow at microstructure level. Most of it is intraday so v1 daily-timeframe restriction makes much of it inapplicable; relevance is the framing of *what daily-bar features proxy intraday dynamics* (e.g. overnight gaps as information-arrival proxies).

### §2.2 — Micro-structure: bid-ask, adverse selection, illiquidity premia

- **Kyle (1985) lambda** = price impact per unit signed order flow. v1 finder cannot compute (no order-flow data in `platform.prices_daily`), but `platform.spread_observations` exists and is unused by the spec — *that's* the closest substrate the snapshot can reach.
- **Roll (1984) effective spread** = `2 * sqrt(-Cov(ΔP_t, ΔP_{t+1}))` from daily-bar returns. Computable from `prices_daily` alone. **Cheap to add to the whitelist as a derived series_id.**
- **Glosten-Milgrom (1985)** — informational vs uninformed order-flow spread decomposition. NAMING-only; v1 can't run it.

**The spec's `spread_observations` blind spot is structural.** The codebase ingests this table (`grep platform.spread_observations` returns hits in `tpcore/`) but `§4.1 MarketSnapshot` does not read it.

### §2.3 — Execution: Almgren-Chriss, VWAP, IS, arrival price

- **Almgren-Chriss (2001)** — optimal execution with quadratic cost and Brownian price. Standard reference; the finder does not produce execution decisions (engine-owned) so v1 NAMING-only is correct.
- **VWAP / TWAP / POV / Implementation Shortfall** — Bertsimas-Lo (1998) arrival-price algorithm framing. Same NAMING-only logic.

**Verdict:** spec correctly defers execution to the engine. **No gap.** The Carver bundle's "honest cost modelling" already covers the conceptual surface. Just need a `market_structure_primer.md` mention so the LLM doesn't propose execution-level hypotheses (which would be a category error).

### §2.4 — Anomalies + factor literature: the n_trials discipline source

- **Fama-French (1993) three-factor; FF (2015) five-factor** — Market / Size / Value / Profitability / Investment. The operator's `vector` engine is implicitly an FF-style cross-sectional ranker; `reversion` is the residual after factor exposure.
- **Hou-Xue-Zhang (2015) q-factor** — alternative four-factor (Mkt, ME, I/A, ROE). Competitor to FF5; the canonical "factor zoo" expansion.
- **Harvey-Liu-Zhu (2016), *...and the cross-section of expected returns*** — **THE** load-bearing paper for the n_trials discipline. 316 anomalies in published literature, ~half don't survive after multiple-testing correction. The DSR `n_trials` deflation in SP-A is a direct mechanical response. **Spec mentions `project_ml_research_track` but does NOT cite HLZ in the pending `dsr_ntrials_discipline.md` outline.** Blocking gap in the bundle that doesn't exist yet.
- **McLean-Pontiff (2016), "Does academic research destroy stock return predictability?"** — post-publication decay (~58% out-of-sample) of published anomalies. THE empirical complement to HLZ. **Same gap.**

### §2.5 — Workflow: López de Prado (2018) is the spec's intellectual parent

- **López de Prado (2018), *Advances in Financial Machine Learning*** — this book IS the source of:
  - **Deflated Sharpe Ratio (DSR)** (ch. 14) — the operator's cumulative-DSR gate (SP-A) is the DSR with cumulative `n_trials` accounting. Spec §2.1 cites SP-A; the LLM should be able to NAME DSR from the reference bundle.
  - **Probability of Backtest Overfitting (PBO)** (ch. 11) — the multiple-testing failure-rate metric. The operator's Readiness §6 references it.
  - **Purged k-fold cross-validation** (ch. 7) — required for time-series. The Lab's walk-forward is the deployed instance; the LLM should NAME this.
  - **Meta-labeling** (ch. 3) — already deferred to spec §9.5 (v3.0). Good.
  - **Combinatorial Purged CV** (ch. 12) — even more conservative than walk-forward. Could land as a future v1.5+ enrichment.

**Spec gap:** `dsr_ntrials_discipline.md` MUST cite López de Prado (2018) ch. 14 by name. The operator's gate IS this chapter. Mandatory-always-include bundle should reference the book that defines its own arithmetic.

### §2.6 — Cross-asset interconnection: the (F1) framing's second half

The (F1) framing explicitly includes "how everything interconnects." US-equity literature on this:

- **VIX as risk gauge** (Whaley 1993) — implied-vol index, leading indicator of equity drawdowns. **Not in spec MarketSnapshot.** Is it ingested? Yes — `platform.fear_greed` includes a VIX-derived score; `platform.macro_indicators` likely carries VIX directly.
- **USD / rates / credit → equity transmission** — Treasury yield curve (10Y-2Y inversion), HY-OAS (high-yield credit spreads), DXY (dollar index). All in `platform.macro_indicators` per the FRED adapter. **Not in spec MarketSnapshot.**
- **Earnings calendar** — `platform.earnings_events` table exists. Crucial for "interconnection" — earnings clustering, sector rotation, blackout windows. **Not in spec MarketSnapshot.**
- **FOMC calendar** — meeting effects on equity (Lucca-Moench 2015 — "The Pre-FOMC Announcement Drift"). Ingestable via FRED or a static schedule. **Not in spec.**
- **Treasury auction calendar** — secondary; could matter for cross-asset RV strategies but v1 is single-asset equity, so deferral OK.

### §2.7 — STE-specific environment: the data-source roster

The codebase already wires (from my data-feed inventory):

| Adapter | Table | In `MarketSnapshot` §4.1? |
| --- | --- | --- |
| Alpaca (SIP) | `platform.prices_daily` | YES |
| Tradier historical | `platform.prices_daily` (parallel) | YES |
| FMP | `platform.fundamentals_quarterly` | YES |
| FRED | `platform.macro_indicators` | **NO** |
| FINRA | `platform.short_interest` | **NO** |
| SEC EDGAR | `platform.sec_insider_transactions`, `platform.sec_material_events` | **NO** |
| AAII | `platform.aaii_sentiment` | **NO** |
| ApeWisdom | `platform.social_sentiment` | **NO** |
| IBorrowDesk | `platform.borrow_rates` | **NO** |
| Finnhub insider | `platform.insider_sentiment` | **NO** |
| Tradier options | `platform.tradier_options_chains`, `platform.options_max_pain` | **NO** |
| Spread observations | `platform.spread_observations` | **NO** |
| Earnings | `platform.earnings_events` | **NO** |
| Fear/Greed | `platform.fear_greed` | **NO** |
| Catalyst | `platform.catalyst_events` | **NO** |

**This is the heart of the (F1) gap.** The operator has spent months building a multi-source data substrate; spec §4.1 reads two tables of fifteen+. The LLM cannot explain "the trading environment" if it only sees price and EPS.

---

## Phase 3 — spec critique (the actionable output)

### §3.1 — `MarketSnapshot` §4.1 is too narrow [BLOCKING]

- **§ pointer:** spec §4.1 `MarketSnapshot`.
- **Gap kind:** missing-concept / under-specified.
- **Evidence:** operator framing (F1) + the data-feed inventory in §2.7 above. 13 of 15 ingested-but-relevant tables are not exposed.
- **Recommended edit:** extend the `MarketSnapshot` payload to include (at minimum, for v1) THREE additional read paths:
  1. **`macro_state: tuple[MacroRow, ...]`** — last-180-session window of `platform.macro_indicators` for a fixed indicator whitelist (VIX, DXY, US10Y, US2Y, HY-OAS, US-CPI-yoy, US-unemployment). 7 indicators × 180 = 1260 rows; ~30 KB. Cheap.
  2. **`sentiment_state: tuple[SentimentRow, ...]`** — latest-N readings of `platform.aaii_sentiment` (weekly), `platform.fear_greed` (daily), `platform.social_sentiment` (weekly aggregate). ~10 KB.
  3. **`event_calendar: tuple[EventRow, ...]`** — next-21-session earnings events (`platform.earnings_events`) + last-180-session FOMC meeting dates. Filters to the snapshot's universe. ~20 KB.
  - **Hold `MAX_SNAPSHOT_BYTES = 512 KiB` constant**; the 60-KB total addition is well within budget.
- **Severity:** BLOCKING. v1 cannot ship "explain the trading environment" without these.

**Defensive counter-argument the spec implicitly makes:** *every extra column expands LLM hypothesis surface and inflates n_trials.* This is the `project_ml_research_track` low-DOF discipline applied to inputs as well as tools. **Rebuttal:** the LLM cannot inflate `n_trials` on data it cannot see, but it ALSO cannot propose Chan-flavoured regime-conditioned hypotheses without VIX or macro context, nor Carver-flavoured cost-aware hypotheses without spread data. The discipline is *one hypothesis per emission*, not *one input column per hypothesis*. The ledger gates emissions, not snapshot bytes.

### §3.2 — `tool_sandbox` §6.1 whitelist is statistically unsafe [BLOCKING]

- **§ pointer:** spec §6.1 + §4.2 `ToolCall.callable_name` Literal.
- **Gap kind:** wrong-default + missing-concept.
- **Evidence:**
  - **Newey-West HAC SEs (Newey-West 1987).** Plain `statsmodels.api.OLS().fit()` returns homoskedastic SEs by default. For ANY time-series regression on returns with serial correlation (which is *all* return regressions), homoskedastic SEs underestimate variance and over-state significance. This is textbook (Hayashi 2000 ch. 6; Hamilton 1994 ch. 10). The fix is `.fit(cov_type='HAC', cov_kwds={'maxlags': N})`. v1 spec's `OLS` does NOT pin this — the LLM is invited to publish false significance.
  - **Variance-ratio test (Lo-MacKinlay 1988)** — the canonical mean-reversion screen, complementary to ADF (which tests unit root; VR tests random-walk-vs-mean-reversion across multiple horizons). `statsmodels` exposes this as a primitive (`statsmodels.stats.diagnostic.acorr_ljungbox` for related Q-test; VR is computable directly). Chan ch. 2 leans on it heavily.
  - **Hurst exponent (R/S analysis)** — mean-reversion / momentum / random-walk classifier; long-memory detection. Computable in ~10 lines from numpy + a series; could be a derived `series_id` rather than a new callable.
  - **Newey-West-corrected `ttest_1samp`** — same logic; the t-test for "mean Sharpe ≠ 0" must adjust for autocorrelation in overlapping returns. The current `ttest_1samp` does not.
- **Recommended edit:**
  - **Pin `OLS` to HAC SEs** (`maxlags = ceil(4 * (T/100)^(2/9))` — Newey-West's own default rule; T = window length). Make this a kwarg of `OLSArgs` with a sensible-default-of-None-falls-back-to-NW.
  - **Add `variance_ratio` to the whitelist.** Implementation: roll it as a 10-line helper in `tool_sandbox.py` rather than depending on the right statsmodels version.
  - **Add `hurst` to the whitelist** (same: small in-house helper).
  - **Add `acorr_ljungbox` (Ljung-Box)** as a residual-diagnostic callable — anything the LLM regresses needs a "residuals are white noise" check.
  - **Document the HAC default in `dsr_ntrials_discipline.md`** — the reference bundle should explain WHY HAC is the default.
- **Severity:** BLOCKING. Every published Sharpe in finance has been HAC-adjusted since the late 1980s. v1 emitting un-HAC'd OLS p-values is publishing the same noise the SP-A ledger is built to suppress.

### §3.3 — Persona §7 will under-internalize (F1) and (F2) framings unless authored explicitly [HIGH]

- **§ pointer:** spec §3.1 (persona path `docs/lab_finder_persona.md`); §7 reference-bundle system implicitly carries the persona.
- **Gap kind:** under-specified.
- **Evidence:** the spec NAMES the persona file (path) and the PERSONA_VERSION SHA-pinning convention (mirrors SP-G `_persona_sha()` in `ops/llm_lab_emitter.py:140`), but does NOT define the persona's contractual sections. SP-G's persona file (`docs/lab_emitter_persona.md`) is also currently absent — both will be authored simultaneously, so the precedent is missing too.
- **Recommended edit:** the spec should mandate a contractual persona structure with at least these sections:
  1. **Operator framing block (verbatim).** The (F1) + (F2) framing pasted as the persona's first paragraph. Non-negotiable.
  2. **The gate is sacred.** SP-A cumulative ledger framing; the LLM CANNOT propose any change to the gate.
  3. **Workflow shape (F2).** The PHASE A → PHASE B → PHASE C → STOP-AT-`emit_once` loop, with explicit prose forbidding the LLM from "trying again" outside the loop.
  4. **The reference bundles are authoritative; trained-knowledge is supplementary.** This is the spec's answer to the operator's "venture out to other sources" — the persona must explicitly say *"the bundles are your in-context truth; your training carries broader context but reference-bundle text wins on conflict."*
  5. **Hypothesis-shape priors.** Favour `fold_existing` over `promote_new` in early cycles (Carver doctrine); favour regime filters over parameter sweeps (Chan doctrine); favour HAC-adjusted t-stats over raw p-values.
  6. **PERSONA_VERSION** is the SHA-12 of the file (matches SP-G mechanism).
- **Severity:** HIGH. Should land before plan PR. Otherwise the build PR is doing persona-design under build-PR review pressure, which is exactly the failure mode `feedback_cut_process_overhead_ship` flags.

### §3.4 — Pending reference bundle `dsr_ntrials_discipline.md` is undefined [BLOCKING]

- **§ pointer:** spec §7.1 table (bundle status "NEW (v1.0), **mandatory always-include**").
- **Gap kind:** missing-concept (content).
- **Evidence:** the bundle is the load-bearing safety doc — `dsr_ntrials_discipline.md` is the LLM's only structured reminder that the gate is cumulative-DSR-deflated. Spec mentions ONLY that it should exist. No outline.
- **Recommended edit:** spec §7.1 (or a new §7.3 sub-clause) should sketch a 5–10-bullet content outline this PR's spec-edit lands. Suggested skeleton (operator can edit):
  1. **What DSR is.** López de Prado (2018) ch. 14. The deflation formula. Why raw Sharpe is wrong with N hypotheses.
  2. **Cumulative `n_trials` is the ledger primitive.** SP-A `tpcore.lab.ledger.cumulative_n_trials` reads it; SP-A `record_trial_spend` writes it. Every emission STRICTLY tightens the gate; "tighten" is the only direction it moves.
  3. **HLZ (2016) is the empirical motivator.** 316 anomalies; ~half don't survive multiple-testing. Cite by name.
  4. **McLean-Pontiff (2016)** — post-publication 58% decay; the OOS failure mode the gate catches.
  5. **PBO** (López de Prado ch. 11) is the complement. Above-gate Sharpe with high PBO is still rejected.
  6. **The LLM's analysis turns are NOT formally counted in `n_trials`** (per spec §2.10) — but the LLM should treat them AS IF they were, when budgeting which hypothesis to emit.
  7. **No-relax pledge.** The LLM cannot, ever, propose a candidate whose criterion of success involves relaxing DSR/credibility. v1 fence: the diff-scope allow-list reds the build.
  8. **The HAC default.** Cross-ref to §3.2 above — all OLS callables default to NW-HAC; the LLM cannot ask for homoskedastic SEs.
- **Severity:** BLOCKING. Spec calls it mandatory-always-include; bundle must have content or "mandatory" is empty.

### §3.5 — Pending reference bundle `market_structure_primer.md` outline [HIGH]

- **§ pointer:** spec §7.1 table (bundle status "NEW (v1.0), operator-authored later").
- **Gap kind:** missing-concept (content).
- **Evidence:** the (F1) half of operator framing is *exactly* this bundle. Spec gives only the title.
- **Recommended edit:** spec should sketch the bundle's outline. Suggested skeleton (operator can edit, ~5–10 bullets):
  1. **Venue / order-flow primer.** SIP vs IEX (and why operator picked SIP 2026-05-13); the role of dark pools / ATS; payment-for-order-flow effects on retail-touch quotes.
  2. **Order types and auctions.** Market, limit, MOC, LOC, MOO, LOO. Why MOC dominates daily-bar close prices (sentinel's anchor).
  3. **LULD / circuit breakers.** When prices stop being prices.
  4. **Tick sizes.** Sub-penny vs penny; the small-cap pilot.
  5. **The bid-ask spread decomposition** (Roll 1984; Glosten-Milgrom 1985; Stoll 2003). Effective spread vs quoted spread. Why `platform.spread_observations` matters.
  6. **Liquidity proxies the finder can compute.** Amihud (2002); dollar volume; relative volume. ALL computable from `platform.prices_daily`.
  7. **Cross-asset transmission.** VIX (Whaley 1993). 10Y-2Y inversion. HY-OAS as risk-on/off gauge. DXY rallies as equity headwind.
  8. **Calendar effects.** FOMC pre-announcement drift (Lucca-Moench 2015). Earnings clustering. Russell rebal Q3. Year-end tax-loss harvest.
  9. **The finder's environment posture.** "You are observing a market that is mostly-efficient at the daily timeframe in liquid large-caps. Most published anomalies decay (McLean-Pontiff 2016). Your edge, if any, comes from underexploited interactions among existing engines + clean data."
  10. **What the finder CANNOT do.** Intraday signals. Order-flow microstructure. Anything sub-daily.
- **Severity:** HIGH. The spec marked this "operator-authored later" — that's fine, but the outline should ship with the spec so the operator (or expert-subagent) is editing a known skeleton, not authoring blind.

### §3.6 — Quotas: `ANALYSIS_TURN_QUOTA = 8`, `EDGE_FINDER_RUN_QUOTA = 3` [MEDIUM]

- **§ pointer:** spec §3.2, §9.
- **Gap kind:** under-specified justification.
- **Evidence:** 8 analysis turns × 4 tool calls/turn = 32 tool dispatches max. A hand-run of the (F2) workflow looks like:
  1. Survey snapshot (1 turn — look at prices + macro + sentiment).
  2. Pick a candidate target engine (1 turn — read roster + ledger spend).
  3. Form 2–3 candidate hypotheses (1–2 turns of `pearsonr`/`spearmanr` factor IC checks).
  4. Stationarity / cointegration screens for the top hypothesis (2 turns — `adfuller`, `coint`, residual diagnostics).
  5. OLS factor-exposure regression on the candidate spec (1–2 turns — `OLS` with HAC, t-test).
  6. Synthesize into 1–3 `ProposedSpec`s (1 turn).
  - **Total: 7–9 turns.** 8 is *tight* but defensible. Could go to 10 without harm.
  - **`EDGE_FINDER_RUN_QUOTA = 3` × 1 run/day:** for the v1 success criterion (ONE candidate to PAPER), 3 is more than enough; this is right.
- **Recommended edit:** raise `ANALYSIS_TURN_QUOTA` to **10** to give one turn each for snapshot review + roster review + synthesis without crowding the analysis core. Document the breakdown in spec §3.2 prose. NO change to `EDGE_FINDER_RUN_QUOTA`.
- **Severity:** MEDIUM. Workable as-is; loose-budget enrichment improves first-cycle quality.

### §3.7 — "Venture out to other sources" constraint [HIGH]

- **§ pointer:** spec §2.8 (no network beyond Anthropic SDK call) vs operator's 2026-05-21 framing.
- **Gap kind:** under-specified.
- **Evidence:** operator said *"use the references that I already gave you as initial research then venture out with other sources."* Spec §2.8 forbids runtime browsing. These compose three ways:
  - **(a) Broader reference set.** Operator adds more `docs/lab_emitter_references/*.md` files over time. Spec §7.1's "later" slot for `market_structure_primer.md` is the precedent. **This is the right reading of "venture out."**
  - **(b) Richer persona.** Persona text explicitly tells the LLM to draw on its trained knowledge (Carver/Chan/Harris/O'Hara/Hasbrouck/LdP/etc.) — but bundles win on conflict.
  - **(c) FORBIDDEN: runtime browsing / unguided trained-knowledge.** The LLM must not propose strategies from training data not represented in bundle/snapshot — that's `n_trials` smuggling.
- **Recommended edit:** spec §2.8 should add a clause: *"'Venturing out to other sources' is operator-authored bundle expansion + persona-mediated trained-knowledge supplement; it is NEVER runtime browsing or unguided trained-knowledge mining. The LLM's training carries broader context but reference-bundle text wins on conflict; trained-knowledge alone cannot ground a `ProposedSpec.rationale`."*
- **Severity:** HIGH. Without this, the operator's "venture out" framing has two plausible interpretations and the build PR may pick the wrong one.

### §3.8 — Calendar / session-anchor metadata is missing [MEDIUM]

- **§ pointer:** spec §4.1 (`MarketSnapshot` fields).
- **Gap kind:** missing-concept.
- **Evidence:** the snapshot has `snapshot_ts` and `session_date` but nothing else calendar-aware. FOMC dates, earnings clustering, Russell rebal, half-day sessions, holiday-adjacent sessions all materially shift the LLM's hypothesis quality.
- **Recommended edit:** add to MarketSnapshot:
  - `calendar_context: CalendarContext` field — `next_fomc_date: date | None`, `is_earnings_season: bool`, `sessions_until_quarter_end: int`, `next_session_is_holiday_adjacent: bool`. All cheap from `tpcore.calendar` + the FRED FOMC schedule + `platform.earnings_events` count.
- **Severity:** MEDIUM. Defer to v1.5 acceptable but cite as known follow-up.

### §3.9 — `series_id` whitelist is under-specified [HIGH]

- **§ pointer:** spec §6.2 ("Resolves series from `snapshot` BY ID against a fixed column whitelist (`adj_close`, `log_return`, `vol_20d`, ...)"; spec §9.1 item 4 defers to plan PR).
- **Gap kind:** under-specified.
- **Evidence:** the `series_id` whitelist is the LLM's column-access surface. Spec gives 3 examples + `...`. Plan PR is deferred to define the rest. **This means the build agent picks the whitelist** — outside the spec-review gate.
- **Recommended edit:** spec should pin a v1 minimum:
  - Per-ticker: `adj_close`, `log_return_1d`, `log_return_5d`, `log_return_20d`, `vol_20d`, `vol_60d`, `dollar_volume_20d`, `amihud_illiq_20d`, `effective_spread` (from `spread_observations`), `roll_implied_spread` (Roll 1984 derived).
  - Cross-section: `cross_section_return_zscore_20d`, `cross_section_vol_zscore_20d` (for ranking-based hypotheses).
  - Macro (if snapshot enriched per §3.1): `vix_level`, `vix_change_20d`, `us10y_minus_us2y`, `hy_oas_level`, `hy_oas_change_20d`, `dxy_change_20d`.
  - Sentiment: `aaii_bull_bear_spread_4wma`, `fear_greed_index`, `social_sentiment_change_7d`.
- **Severity:** HIGH. This is the LLM's actual data surface; deferring to plan PR makes the spec-review gate unable to evaluate (F1) framing satisfaction.

### §3.10 — Determinism: `numpy.random.seed(0)` is insufficient [LOW]

- **§ pointer:** spec §6.3.
- **Gap kind:** under-specified (technical).
- **Evidence:** v1 callables ARE deterministic given inputs; `np.random.seed(0)` is belt-and-braces. But `statsmodels.tsa.arima.model.ARIMA(...).fit()` uses MLE optimisation with default starting values — convergence can differ across statsmodels minor versions. Pin a `requirements.txt` / pyproject pin for statsmodels and scipy versions.
- **Recommended edit:** spec §6.3 should add: *"v1 pins `statsmodels >= 0.14, < 0.15` and `scipy >= 1.11, < 1.13` in pyproject; minor-version drift is a v1.5 re-validation event."*
- **Severity:** LOW. Defer to plan PR fine.

### §3.11 — `analysis_evidence_refs` is a weak provenance link [MEDIUM]

- **§ pointer:** spec §4.3 `ProposedSpec.analysis_evidence_refs: tuple[int, ...]`.
- **Gap kind:** under-specified.
- **Evidence:** indices into `tool_results` are fragile (re-order = silent referent change) and don't capture WHICH part of the tool result the spec cites. A `ProposedSpec` claiming "`coint` p < 0.05 on AAPL/MSFT pair" with `analysis_evidence_refs = (3,)` requires the reviewer to read the tool result to verify.
- **Recommended edit:** make the reference shape richer:
  ```python
  class EvidenceRef(BaseModel):
      tool_result_index: int
      callable_name: str            # redundant with the result, but pins it
      claimed_statistic: str        # e.g. "coint.p_value"
      claimed_value: float
      claimed_threshold: float | None
  ```
  Then `ProposedSpec.analysis_evidence_refs: tuple[EvidenceRef, ...]`. Build-time CI sentinel asserts the claimed value matches the actual tool result.
- **Severity:** MEDIUM. Without this, the operator review at §8 step 5 is unable to mechanically verify rationale ↔ evidence.

### §3.12 — Missing safety: LLM cannot READ the gate code [LOW]

- **§ pointer:** spec §5 safety table.
- **Gap kind:** under-specified.
- **Evidence:** the spec's safety story prevents the LLM from MODIFYING the gate. But snapshot does not include `tpcore/lab/scorer.py` / `ops/engine_sdlc/lab_criteria.py` source text either. **That's fine** — the LLM should NOT see implementation; it should see the *contract* (which the persona + reference bundles describe).
- **Recommended edit:** explicit clause in §2: *"The LLM never reads gate source text. The persona + reference bundles describe the gate behaviourally; the LLM has no in-context view of `_assess_new_engine_signal` or `_assess_improvement`."*
- **Severity:** LOW. Currently implicit by §2.8 + §3.1 (snapshot is data-only). Make it explicit.

### §3.13 — Composition with SP-G: persona path collision [MEDIUM]

- **§ pointer:** spec §3.1 (`docs/lab_finder_persona.md`); existing `ops/llm_lab_emitter.py:124` (`docs/lab_emitter_persona.md`).
- **Gap kind:** under-specified.
- **Evidence:** spec proposes a NEW persona file for the finder, distinct from SP-G's. Justified — the finder's contract is broader (analysis loop + emission, vs SP-G's emission-only). But the spec doesn't say what happens when the SAME `EmissionContext.persona_version` field needs to carry SHA from one of two files. Per `ops/llm_lab_emitter.py:339`, `_persona_sha()` reads `_PERSONA_PATH` (SP-G's file) — does the finder's `emit_once` call use SP-G's persona SHA (correct, since the *emission* step is SP-G's, even if the analysis was driven by finder persona)?
- **Recommended edit:** spec §3.1 + §3.3 should clarify: *"`EmissionContext.persona_version` continues to be SP-G's `_persona_sha()` (SHA of `docs/lab_emitter_persona.md`); the finder's `PERSONA_VERSION` (SHA of `docs/lab_finder_persona.md`) appears separately in `FinderRun.persona_version`. Two persona-version provenance fields, two persona files, both SHA-pinned, both CI-sentinel-gated."*
- **Severity:** MEDIUM. Without this clarification, the build agent has to guess.

### §3.14 — `coint` on the snapshot universe: combinatorial explosion [HIGH]

- **§ pointer:** spec §6.1 `coint` callable.
- **Gap kind:** under-specified.
- **Evidence:** `coint` (Engle-Granger) is a PAIRWISE test. On `sp500` universe that's C(500, 2) = 124,750 pairs. The LLM cannot run them all (`ANALYSIS_TURN_QUOTA = 8`, max 32 calls). So the LLM will pick ~5–10 pairs by hand (i.e. by trained-knowledge intuition — the "venture out" channel). That's a non-randomised selection BIAS that inflates apparent cointegration rate (since the LLM picks pairs that "should" cointegrate). **This is `n_trials` smuggling.**
- **Recommended edit:** spec §6.1 should constrain `coint` use: *"Cointegration tests on pairs the LLM selected by trained-knowledge intuition are a known selection bias; the reported `p_value` must be deflated by Bonferroni-or-stricter for the number of pairs the LLM could plausibly have considered. v1: the persona explicitly forbids `coint` as a primary hypothesis substrate; `coint` is a SECONDARY screen on a hypothesis grounded in another callable (e.g. correlation IC + cointegration as a robustness check). v1.5: implement Bonferroni-deflated `coint` as a separate callable."*
- **Severity:** HIGH. Without this, `coint`-based proposals are systematically biased.

### §3.15 — The two SP-G bundle files are SHARED — invariant fragile [MEDIUM]

- **§ pointer:** spec §3.1 ("Reference-bundle dir is **shared** with SP-G").
- **Gap kind:** under-specified.
- **Evidence:** edits to `carver_systematic_trading.md` for finder-needs may degrade SP-G's emission quality, and vice versa. No mechanism distinguishes "edits ratified by finder-spec-review" from "edits ratified by SP-G-spec-review."
- **Recommended edit:** either (a) split bundles into `lab_emitter_references/sp_g/` and `lab_emitter_references/finder/` directories with explicit cross-symlink for the shared two; OR (b) document in each shared bundle the OWNERSHIP and the change-control protocol (which spec sign-off authorises an edit). Lower-friction is (b).
- **Severity:** MEDIUM. Low-probability collision, but a known land-mine.

---

## §4 — `MarketSnapshot` exhaustive coverage table (the (F1) gap visualised)

| Operator framing element | Source table | In v1 spec? | Recommended action |
| --- | --- | --- | --- |
| Prices / returns / volatility | `platform.prices_daily` | YES | Keep. |
| Fundamentals | `platform.fundamentals_quarterly` | YES | Keep. |
| Roster + ledger | `_PROFILE` / `lab_trial_ledger` | YES | Keep. |
| Macro (VIX, rates, credit, DXY, CPI) | `platform.macro_indicators` | **NO** | **ADD** per §3.1. |
| Sentiment (AAII) | `platform.aaii_sentiment` | **NO** | **ADD** per §3.1. |
| Sentiment (social) | `platform.social_sentiment` | **NO** | **ADD** per §3.1. |
| Sentiment (Fear/Greed) | `platform.fear_greed` | **NO** | **ADD** per §3.1. |
| Sentiment (insider) | `platform.insider_sentiment` | **NO** | Add or v1.5. |
| Short interest | `platform.short_interest` | **NO** | Add or v1.5. |
| Borrow rates | `platform.borrow_rates` | **NO** | v1.5. |
| Earnings calendar | `platform.earnings_events` | **NO** | **ADD** per §3.1. |
| Material events (SEC) | `platform.sec_material_events` | **NO** | v1.5. |
| Spreads (Roll 1984 substrate) | `platform.spread_observations` | **NO** | **ADD** per §3.2 series-derivation. |
| Options chains / max-pain | `platform.tradier_options_chains` / `platform.options_max_pain` | **NO** | v1.5 (options surface adds complexity). |
| Calendar context (FOMC, season) | `tpcore.calendar` + `platform.earnings_events` derived | **NO** | **ADD** per §3.8. |
| Catalyst events | `platform.catalyst_events` | **NO** | v1.5. |

**13 of 16 relevant substrates are unused in v1 as currently specified.** Three are blocking-additions (macro, sentiment, earnings); five are v1.5 deferrals; the rest are noted.

---

## Phase 4 — go / no-go on planning

**Verdict: GO-WITH-EDITS.**

### What the in-flight plan PR (`a68fdf65...`) should be treated as

A useful but premature draft. The build agent will design tables, contract shapes, test sentinels — all of which depend on the snapshot scope (§3.1), the toolkit shape (§3.2), and the persona contract (§3.3). Two of those three pivot under this review's recommendations. The plan is therefore likely to need >50% revision after the spec edits land.

### Three specific spec edits required before plan-PR is useful

1. **Spec §4.1 + §3.2 — `MarketSnapshot` enrichment.** Add `macro_state`, `sentiment_state`, `event_calendar`, `calendar_context` fields per §3.1 + §3.8 above. Pin the per-field schemas at spec time. (Blocking #1.)
2. **Spec §6.1 + §6.2 — Toolkit safety.** Pin HAC SEs on `OLS`; add `variance_ratio`, `hurst`, `acorr_ljungbox`; pin `coint` as secondary-only with selection-bias warning. (Blocking #2.)
3. **Spec §7.1 — Bundle outlines.** Sketch 5–10-bullet outlines for `dsr_ntrials_discipline.md` and `market_structure_primer.md` per §3.4 + §3.5 above. (Blocking #3.)

The four HIGH findings (§3.3 persona contract, §3.7 "venture out" constraint, §3.9 `series_id` whitelist, §3.14 `coint` selection-bias) should also land in the spec edits — they're low-cost prose additions and the plan-PR cannot make these decisions correctly.

### Re-dispatch sequence

1. Operator reads this review.
2. Operator dispatches a spec-edit subagent against `docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md` carrying the three blocking + four high recommendations.
3. Spec-edit PR lands.
4. Re-dispatch planning against the *edited* spec.
5. Build proceeds against the *re-planned* plan.

Adds one cycle. Worth it: a v1 build that ships without macro/sentiment/calendar context cannot satisfy the operator's (F1) framing, and a v1 build that ships without HAC-defaulted OLS publishes the same noise the SP-A ledger is built to suppress.

### Open operator question (must answer before either spec edits or plan moves)

**Is "venturing out" intended as (a) richer operator-staged context — both more `MarketSnapshot` substrates and more reference bundles — with the LLM strictly bounded to those at runtime, OR (b) license for the LLM to roam its trained knowledge unguided?**

Spec §2.8 forbids runtime browsing (correct). But the operator's prose framing is ambiguous between (a) and (b). The review's recommendations all assume (a). If operator's intent is (b), the spec needs a fundamentally different posture and §3.7 above is the wrong recommendation. **Answer this first.**

---

## §5 — Citation index (literature referenced in this review)

| Citation | Used at |
| --- | --- |
| Amihud (2002) — illiquidity ratio | §2.2, §3.9 |
| Almgren-Chriss (2001) — optimal execution | §2.3 |
| Bertsimas-Lo (1998) — arrival-price | §2.3 |
| Carver (2015) — Systematic Trading | Phase 1; §3.3 |
| Chan (2013) — Algorithmic Trading | Phase 1; §3.3, §3.14 |
| Engle-Granger (1987) — cointegration | §3.14 |
| Fama-French (1993, 2015) — factor models | §2.4 |
| Glosten-Milgrom (1985) — info-asymmetry spread | §2.2 |
| Hamilton (1994) — Time-Series Analysis ch. 10 | §3.2 |
| Harris (2003) — Trading and Exchanges | §2.1, §3.5 |
| Harvey-Liu-Zhu (2016) — and the cross-section… | §2.4, §3.4 |
| Hasbrouck (2007) — Empirical Microstructure | §2.1 |
| Hayashi (2000) — Econometrics ch. 6 | §3.2 |
| Hou-Xue-Zhang (2015) — q-factor | §2.4 |
| Johansen (1988) — multivariate cointegration | Phase 1 |
| Kyle (1985) — lambda | §2.2 |
| Lo-MacKinlay (1988) — variance ratio | §3.2 |
| López de Prado (2018) — Adv. Financial ML | §2.5, §3.4 |
| Lucca-Moench (2015) — pre-FOMC drift | §2.6, §3.5 |
| McLean-Pontiff (2016) — anomaly decay | §2.4, §3.4, §3.5 |
| Newey-West (1987) — HAC SEs | §3.2 |
| O'Hara (1995) — Market Microstructure Theory | §2.1 |
| Roll (1984) — effective spread | §2.2, §3.9 |
| Stoll (2003) — spread decomposition | §3.5 |
| Whaley (1993) — VIX | §2.6, §3.5 |

---

## §6 — Maintenance

This review is a snapshot against the spec as of `git log -1 docs/superpowers/specs/2026-05-21-task-25-llm-edge-finder-design.md` at branch-creation time. If the spec is edited per §4's recommendations, this review's findings are SUPERSEDED — the next review pass should re-baseline.

Author: code-review subagent acting under operator instruction 2026-05-21.
Source authority: operator's (F1)+(F2) framing; the two shipped reference bundles; the data-feed roster in `tpcore/providers.py`; the platform table inventory in `platform/migrations/`; and the literature cited in §5.
