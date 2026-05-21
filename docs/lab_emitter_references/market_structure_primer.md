# US Equity Market Structure Primer — Mandatory Reference (v1.0)

**Purpose.** Teach the LLM finder the trading environment it operates within — venues, order types, microstructure, cross-asset transmission, calendar effects, regime indicators — so its hypotheses are environment-grounded rather than textbook-anomaly re-implementations. Operator binding (2026-05-21):

> "(1) Explaining the environment you're trading in, such as market structure / micro-structure, and how everything interconnects. (2) A process for creating a workflow for collecting data, analysis, and finding trade ideas to automate on your own."

This file is the (1). The workflow doctrine lives in `lab_finder_persona.md`. Together they're the LLM's operating substrate.

**Caveat (McLean-Pontiff applied — see `dsr_ntrials_discipline.md` §5).** Re-implementing textbook anomalies is a publication-decay trap. This primer teaches WHY the environment looks the way it does so the LLM can reason about which edges plausibly survive vs which are already arbitraged.

---

## 1. The venue landscape

### 1.1 Regulated exchanges (NMS)

US has ~16 National Market System exchanges. Three operators (NYSE, Nasdaq, Cboe) own most. Each publishes its order book to the SIP (consolidated tape) + to direct feeds:
- **NYSE group:** NYSE, NYSE American, NYSE Arca, NYSE National, NYSE Chicago
- **Nasdaq group:** Nasdaq, Nasdaq BX, Nasdaq PSX
- **Cboe group:** Cboe BZX, Cboe BYX, Cboe EDGA, Cboe EDGX
- **Others:** IEX, MEMX, MIAX Pearl, LTSE

Cross-venue quotes differ microsecond-to-microsecond. We trade daily bars so the latency arbitrage is irrelevant; the takeaway is that NBBO aggregation hides individual-exchange spreads.

### 1.2 ATS / dark pools

Alternative Trading Systems — venues NOT regulated as exchanges. Operated by banks (Goldman SIGMA-X, JPM-X, UBS-ATS, MS-Pool) and independents (Instinet, Liquidnet, IEX-Dark). Quotes NOT published pre-trade; trades print to FINRA TRF with venue tag "D".

**Volume share (~2024):** ~40% of US equity volume executes off-exchange (ATS + internalizers). For mid/small caps, 50%+. **The visible NBBO is not the full liquidity picture.**

Implication: Amihud illiquidity + dollar-volume floors should use TOTAL traded volume (CTA/UTP + TRF), not just exchange volume.

### 1.3 Payment for order flow (PFOF) + retail wholesalers

Robinhood / Webull / TastyTrade / Public route most retail equity orders to wholesale market-makers (Citadel Securities, Virtu, Jane Street, Susquehanna) for PFOF revenue. Wholesalers internalize ~95% of retail volume at sub-NBBO improvement (0.0001-0.001 better than NBBO). Retail orders **never reach** the regulated exchanges.

**Implication for the finder:** retail-volume / odd-lot / small-lot signals from pre-2015 academic literature are STRUCTURALLY DIFFERENT now — the wholesaler keeps the easy wins and externalizes the hard wins. A finder proposing "small-lot indicator" must justify why the inverse-selection effect doesn't apply.

### 1.4 IEX speed bump

IEX has a 350μs speed bump on order arrival (the Flash Boys thing). **Daily-bar impact: zero.** But IEX-only feeds silently miss tickers that trade primarily off-IEX — which is why this codebase mandates **SIP feed** (Alpaca SIP, switched 2026-05-13).

---

## 2. Order types + execution venues

### 2.1 Continuous-session

- **MO:** market order. ~95% of retail MOs internalized.
- **LO:** limit order. Posts to book if not marketable.
- **Stop / stop-limit:** trigger at level; converts to MO/LO.
- **IOC:** marketable LO; unfilled cancels.
- **FOK:** all-or-nothing IOC.

**This codebase:** Reversion + Vector use Alpaca bracket orders (TP+SL). Momentum uses day-market orders (risk via diversification). Sentinel uses day-market batch (defensive ETF basket).

### 2.2 Auctions

- **MOO/LOO:** opening cross. Order received by 9:28 ET; cross 9:30:00.
- **MOC/LOC:** closing cross. Order received by 15:50 (15:55 for symbols with imbalance); cross 16:00.

**Closing auction matters:** ~10% of daily volume (~6% for liquid names) executes in the final minute. THE most liquid moment. Engines that rebalance at close get superior execution + cleaner reference prices for next-day strategy.

### 2.3 LULD halts

SEC Rule 201: single-stock halt on 5%/10%/20% (price-tier dependent) move in 5 minutes. Halt 5 minutes (or more if repeated). For daily-bar engines on T1/T2: rare noise. For T3+: relevant.

### 2.4 Pre-market + after-hours

Regular session 9:30-16:00 ET. Pre-market (4:00-9:30) + after-hours (16:00-20:00): ~5% of daily volume; ~3x the spread. **Earnings announcements happen after-hours**; the next-day open is the price-discovery event.

Implication for `event_confirmation_mode=positive_beat_30d` (Catalyst): the gap between prior-close + next-open captures the after-hours move. Timing-sensitive.

---

## 3. Microstructure — the academic body that fences "obvious" edges

### 3.1 Kyle (1985) — lambda

`λ = Δprice / Δsigned_volume` — the price impact per unit order flow. High-λ = illiquid; low-λ = liquid. **Amihud (2002) illiquidity ratio (`|return| / dollar_volume`) is the low-frequency proxy.** This codebase ingests `spread_observations` (Corwin-Schultz) as the complementary spread proxy.

Operator-relevant: a "trade low-λ stocks" hypothesis MUST net out execution cost — Kyle's λ IS the cost surface.

### 3.2 Glosten-Milgrom (1985) — adverse selection + bid-ask spread

Quoted spreads exist BECAUSE the market-maker doesn't know whether an order is informed (private info) or uninformed (liquidity demand). Spread = adverse-selection compensation.

Operator-relevant: spread is a **direct cost** per trade. Strategies trading frequently (daily mean-reversion) eat the spread × N trades. Monthly rebalance eats it × few. The finder MUST declare `holding_horizon` in `ProposedSpec`; spread cost over horizon is computable from `spread_observations`.

### 3.3 Roll (1984) — effective spread from price-only data

Auto-covariance of consecutive returns at high frequency contains the effective spread estimate (bid→ask→bid alternation creates a negative autocorrelation whose magnitude is the spread). This codebase's `tpcore.backtest.spread_estimator` uses Corwin-Schultz (a sibling estimator).

### 3.4 Amihud (2002) — ILLIQ ratio

Reciprocal of Kyle's λ. Standard liquidity screen + factor in many anomaly studies. This codebase's `liquidity_tiers` table is the operator's analog: T1 (most liquid 500), T2 (next 1000), T3+ (long tail).

**Decay caveat:** Amihud's original 1964-1997 ILLIQ-return premium has largely faded for T1/T2 (institutionally-screened universes) post-publication. T3+ may still carry some premium but those names fail this codebase's tier ≤2 floor.

### 3.5 Hasbrouck (2007) + Kyle-Obizhaeva (2016) — microstructure invariants

Hasbrouck synthesis: prices follow martingale with microstructure noise (bid-ask bounce, latent value updates). Kyle-Obizhaeva trading invariants: trade-size × volatility / volume is scale-invariant across stocks.

**Operator-relevant doctrine:** ratios that are SCALE-INVARIANT (work for AAPL and small-caps alike) are likely real. Ratios that work only for one liquidity tier are likely artifacts.

---

## 4. Execution-cost analysis (TCA) — the cost surface every strategy lives on

### 4.1 Almgren-Chriss (2001) — optimal execution

Minimizes a quadratic cost function combining price-impact (linear in execution rate) + volatility risk (variance over the execution horizon). Risk-neutral solution: VWAP-like deterministic schedule. Risk-averse: more front-loaded.

This codebase doesn't run execution algorithms — we send market orders to Alpaca and accept broker-side execution. **Implementation shortfall** (Perold 1988) = average execution price minus decision price = our actual transaction cost. For a daily-bar engine entering at close: IS dominated by closing-imbalance signed cost (small for liquid, larger for illiquid).

### 4.2 VWAP / TWAP / POV

- **VWAP:** execute proportional to historical volume (smile-shaped: heavy open + close, light midday).
- **TWAP:** execute linearly in time.
- **POV:** execute as X% of real-time observed volume.

This codebase: Alpaca `time_in_force="day"` market order → wholesaler internalization at sub-NBBO → approximate VWAP for the seconds-scale execution window (liquid names).

### 4.3 Transaction cost decomposition

`per-trade cost ≈ half-spread + impact + opportunity cost`. For T1 names: half-spread ~1-3 bps, impact ~0-2 bps (orders <1% of daily volume), opportunity ~0-5 bps. **Total: ~5-10 bps per round-trip.**

**The binding equation for an edge:** `trade-frequency × per-trade gross-alpha ≥ trade-frequency × cost` → **net alpha after cost must be positive.** A 15-bp gross alpha per trade nets ~5-10. An 8-bp gross alpha nets NEGATIVE.

---

## 5. Cross-asset interconnection — why equities don't trade in a vacuum

### 5.1 Rates → equities (discount-rate channel)

DCF identity: price = sum of future cash flows discounted at the rate. Higher rates → lower price (mechanical). Empirical: -0.5 to -1.0 stock-bond beta in normal regimes; sometimes POSITIVE in stagflation.

**This codebase's signal:** `yield_curve` (10Y-3M term spread) in `macro_indicators`. Inverted curve has preceded recessions in 6 of 6 post-WWII cycles at ~18-month horizon (Estrella-Mishkin 1998). Sentinel engine uses it as a sub-score.

### 5.2 Credit → equities (risk-appetite channel)

HY OAS widens BEFORE equity drawdowns in most cycles — credit markets are more risk-averse + better-informed about default risk. HY-OAS ≥ 500bp = classic stress signal.

**This codebase's signal:** `hy_spread` (BAML HY OAS) in `macro_indicators`. Sentinel uses it as a sub-score.

### 5.3 FX → equities (regime-dependent)

For US large-caps, strong USD penalizes multinational earnings via FX translation. Commodity-exporter sectors (energy, materials) get doubly hit (commodities priced in USD + revenue FX-translation loss). Relationship is regime-dependent: strong-USD-as-flight-to-safety vs strong-USD-as-rate-differential.

**This codebase:** no FX series ingested today. DXY is FRED-available (`DTWEXBGS`) but not yet in `INDICATOR_SERIES`. **Honest data gap.**

### 5.4 Volatility regime — VIX

VIX = implied vol on SPX options (30-day). Low (≤15) = calm. Medium (15-25) = normal. High (≥25) = stress. Extreme (≥35) = crisis. VIX is mean-reverting (spike + decay); strong inverse correlation with SPX returns at lead/contemporaneous lags.

**This codebase's signal:** `vix` in `macro_indicators`.

### 5.5 Sentiment regime — AAII + Fear & Greed

- **AAII Investor Sentiment Survey:** weekly bull/neutral/bear %. Extreme readings (>50% bull or >50% bear) historically contrarian (subsequent 4-12 week returns mean-revert).
- **CNN Fear & Greed:** composite of 7 indicators (momentum, breadth, options, junk-bond demand, vol, safe-haven demand). 0-100.

**This codebase's signals:** `aaii_sentiment` + `fear_greed` + `social_sentiment` (ApeWisdom Reddit-derived).

---

## 6. Calendar effects + event-driven structure

### 6.1 Earnings season

- Q1 April-May, Q2 July-August, Q3 October-November, Q4 January-February.
- During earnings season, single-stock idio dominates; index-level alpha is muted. Earnings dates pre-announced 2-4 weeks ahead via `earnings_calendar` (FMP).

A finder proposing a daily-frequency cross-sectional strategy needs to condition on whether it works DURING earnings season — idio explodes there.

### 6.2 FOMC weeks

~8 meetings/year (every ~6 weeks). Decision day (Wed 14:00 ET) + Chair press conference. Equities historically had STRONGER returns in the 24-hour window BEFORE FOMC ("pre-FOMC drift" — Lucca-Moench 2015 *JF*).

**Decay caveat:** strong 1994-2011, somewhat decayed 2012-2019, unclear post-COVID. A finder proposing "trade pre-FOMC drift" must check whether the signal persists in recent data — McLean-Pontiff applies.

### 6.3 Opex weeks (third Friday)

Equity-option expirations cluster on the third Friday. Index-option + futures-option expirations cluster on quarterly opex (Mar/Jun/Sep/Dec). Opex weeks are higher-volume + higher-vol than non-opex (gamma effects from dealer hedging).

### 6.4 Year-end effects

- **December:** tax-loss selling pressures losers; January bounce (the January effect, mostly decayed for large-caps).
- **Last week December:** Santa rally (weak signal).
- **January effect:** small-cap outperformance (largely decayed post-2000).
- **Window-dressing:** institutions sell losers prior to quarter-end reporting.

**Decay caveat (all four):** mostly gone in T1/T2. Small-cap January effect persists weakly but those names aren't in T1/T2.

---

## 7. STE-specific environment — what THIS codebase ingests

The finder's `MarketSnapshot` is bounded by what we already pull:

| Source | Table | Cadence | Notes |
|---|---|---|---|
| Alpaca SIP | `prices_daily` | Daily | Survivorship-free (delisted rows persist) |
| FMP Starter | `fundamentals_quarterly` | Quarterly | pb/de computed via `ops.py --stage compute_fundamental_ratios` |
| FMP Earnings | `earnings_events` | Quarterly | BEAT + NO_BEAT sentinel rows (post-PR #186) |
| SEC EDGAR | `sec_insider_transactions` + `sec_material_events` | Continuous | 646k Form-4 + 237k 8-K filings |
| Alpaca corp-actions | `corporate_actions` | Daily | Splits + dividends |
| FRED macro | `macro_indicators` | Per-cadence | 58 series: sahm_rule (M), industrial_production (M), initial_claims (W), yield_curve (D), credit_spread (D), hy_spread (D), vix (D), cfnai_ma3 (M), sos_state_diffusion (M, derived from 50 PHCI), 50 phci_<state> (M) |
| AAII | `aaii_sentiment` | Weekly | Bull/neutral/bear % |
| ApeWisdom | `social_sentiment` | Daily | Reddit ticker mention rank |
| Fear & Greed | `fear_greed` | Daily | Composite gauge |
| Finnhub | `insider_sentiment` | Monthly | MSPR scores per T1/T2 ticker |
| FINRA | `short_interest` | Bi-monthly | Settlement-date PIT-anchored |
| IBorrowDesk | `borrow_rates` | Daily | T1/T2 universe |
| greeks.pro | `options_max_pain` | Daily | 1-symbol/day SPY snapshot |

**Notable absences (honest gaps):**
- FX series (DXY).
- VIX term structure (only spot).
- Vol surface (no per-strike IV; greeks.pro only carries max-pain).
- 13F holdings (no institutional positioning).
- Per-insider track record (transactions yes, "consistent winners" no).

A finder proposing institutional-positioning hypotheses MUST flag the data gap — this codebase doesn't have 13F yet (DFCR-track item).

---

## 8. Regime indicators in this codebase (`MarketRegime`)

Path B `MarketSnapshot.market_regime` decomposes into five axes — all derivable from already-ingested tables:

- **`vol_regime` ∈ {calm, normal, stress, crisis}** — from `vix` bands: <15 / 15-20 / 20-30 / ≥30 (plus realized-vol cross-check from `prices_daily`).
- **`trend_regime` ∈ {range, trend_up, trend_down}** — from SPY 200d slope (50-bp threshold) + ADX (>25 trending vs <20 range).
- **`macro_regime` ∈ {expansion, slowing, contraction}** — from Sahm rule (≥0.50 → contraction) + CFNAI-MA3 (<-0.70 → contraction) + yield-curve inversion (<0 → slowing if 6-month-leading).
- **`sentiment_regime` ∈ {extreme_bull, neutral, extreme_bear}** — from AAII bull-bear cross + Fear & Greed extremes (<25 → extreme_bear; >75 → extreme_bull).
- **`cycle_position` ∈ {early_earnings, mid_earnings, fomc_window, opex_week, year_end, none}** — from XNYS calendar + `earnings_calendar` + Fed calendar (operator-pinned).

Implementation: `tpcore/lab/llm_finder/snapshot.py::compute_market_regime` (Path B spec §4.1).

**Why these specific decompositions** — they correspond to dimensions of literature-validated regime variation:
- Vol regime: Sentinel's whole basis (literature-anchored bands).
- Trend regime: Carver / Chan dichotomy (Chan 2013 ch. 1: mean-reversion in range, momentum in trend).
- Macro regime: Sahm / CFNAI / yield-curve composite.
- Sentiment regime: contrarian indicators (decades of empirical support).
- Cycle position: known calendar effects.

A finder reading `market_regime` FIRST can propose hypotheses that are **conditional on the regime**, not unconditional. Conditional hypotheses are LESS prone to McLean-Pontiff decay because original literature mostly tested unconditional versions.

---

## 9. The takeaway — what the LLM should internalize

**For (1) "explaining the trading environment":**
- Venue + retail-flow + PFOF structure means SIP-visible quotes ≠ true tradeable liquidity. Liquidity screens need care.
- Microstructure (Kyle / Glosten-Milgrom / Roll / Amihud) is THE cost surface every strategy lives on. Half-spread + impact + slippage is the binding execution cost (~5-10 bps round-trip on T1).
- Cross-asset transmission (rates / credit / FX / vol / sentiment) is regime-dependent. The vol-regime decomposition (§8) is the platform's structured way of reading it.
- Calendar effects are publication-decayed for T1/T2. Textbook January / pre-FOMC / opex strategies are mostly arbitraged.

**For (2) "workflow for collecting / analyzing / finding edges":**
1. READ `MarketRegime` first. Hypotheses should be regime-conditional.
2. COLLECT from the existing 14+ ingested tables — the `MarketSnapshot` carries them. Do NOT propose hypotheses needing un-ingested data; flag the gap, suggest a DFCR.
3. ANALYZE via the tool-sandbox whitelist (`OLS_HAC_NW` default, NEVER raw OLS). Pre-register `label_window_days`, primary metric, deflation N.
4. FIND edges LIKELY NOT in McLean-Pontiff's 97-anomaly survey — novel composites + regime-conditional applications > textbook re-runs.
5. AUTOMATE via SP-G `emit_once_with_auto_promote` (existing engine path) OR ENGINE-ADD via `engine_template` (new engine). Path B autonomous loop closes both.

The finder's value-add is **synthesizing the structural understanding above with the codebase's ingested data into hypotheses the textbook didn't already test.** Everything else is fenced as multiple-testing waste.

---

## References (literature)

- Kyle, A. S. (1985). "Continuous Auctions and Insider Trading." *Econometrica* 53(6).
- Glosten, L. R., Milgrom, P. R. (1985). "Bid, Ask and Transaction Prices in a Specialist Market." *Journal of Financial Economics* 14(1).
- Roll, R. (1984). "A Simple Implicit Measure of the Effective Bid-Ask Spread." *Journal of Finance* 39(4).
- Amihud, Y. (2002). "Illiquidity and Stock Returns: Cross-Section and Time-Series Effects." *Journal of Financial Markets* 5(1).
- Hasbrouck, J. (2007). *Empirical Market Microstructure*. Oxford University Press.
- Almgren, R., Chriss, N. (2001). "Optimal Execution of Portfolio Transactions." *Journal of Risk* 3(2).
- Lucca, D. O., Moench, E. (2015). "The Pre-FOMC Announcement Drift." *Journal of Finance* 70(1).
- Estrella, A., Mishkin, F. S. (1998). "Predicting U.S. Recessions: Financial Variables as Leading Indicators." *Review of Economics and Statistics* 80(1).
- McLean, R. D., Pontiff, J. (2016). "Does Academic Research Destroy Stock Return Predictability?" *Journal of Finance* 71(1).
- Crone, T. M., Clayton-Matthews, A. (2005). "Consistent Economic Indexes for the 50 States." *Review of Economics and Statistics* 87(4).

## In-codebase pointers

- `tpcore/fred/adapter.py` — INDICATOR_SERIES (58 series).
- `tpcore/fred/diffusion.py` — Crone-Clayton-Matthews SOS state diffusion.
- `tpcore/feeds/profile.py` — FeedProfile registry (13 feeds).
- `tpcore/providers.py` — ProviderBinding SoT.
- `dsr_ntrials_discipline.md` (this directory) — multiple-testing fences.
- `carver_systematic_trading.md` + `chan_algorithmic_trading.md` (this directory) — doctrinal grounding.
