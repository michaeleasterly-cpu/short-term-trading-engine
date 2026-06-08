# US Market-Health Composite — Authoritative References

**Purpose.** Ground every component of the "US market health" composite score
(`console/src/app/market/page.tsx`, `computeComposite`, ~L458–558) in a published,
primary source so the methodology can be defended as research-derived rather than
arbitrary. For each indicator this doc gives: (a) the authoritative source(s) with
URL, (b) what the source *actually* supports and what it does *not*, (c) an honest
assessment of whether the weight or band used in code is grounded or a heuristic,
and (d) an **evidence-quality** rating.

**Evidence-quality scale**
- **Strong academic** — peer-reviewed paper(s) establish the signal AND its operating range.
- **Official methodology** — the index/series is defined by its issuer (Fed bank, CBOE/ICE, FRED) with published construction and interpretation bands.
- **Practitioner consensus** — widely used by markets/analysts, repeatedly documented, but no single peer-reviewed anchor.
- **Heuristic** — sensible judgment call; thresholds are author-chosen, not literature-derived.

**Method note / honesty caveat.** Federal Reserve (`newyorkfed.org`, `chicagofed.org`)
and FRED (`fred.stlouisfed.org`) endpoints return HTTP 403 to the automated fetcher
used here, so the PDFs/series pages below were **confirmed via web search surfacing
the live canonical URLs and multiple corroborating secondary sources**, not by a
direct page fetch. The URLs are the well-established canonical endpoints; treat the
*numeric quotes* (formula coefficients, historical levels) as search-sourced and
re-verify against the live page before citing publicly. CBOE methodology PDFs and the
academic/issuer pages for valuation and sentiment indicators were reachable. Where a
component has **no** solid published anchor, that is stated as a finding.

> **Scope note — what is actually wired vs. "proposed."** The audited
> `computeComposite` uses: NFCI, HY OAS, IG credit spread, 10Y-3M and 10Y-2Y term
> spreads, Sahm rule, CFNAI-MA3, initial claims, unemployment, fed-funds rate,
> `bear_score`, VIX, CAPE, Buffett indicator, breadth (RSP-vs-GSPC concentration),
> CNN Fear&Greed `score`, AAII `bullish_pct`, Michigan sentiment, and EPU.
> **MOVE, VVIX, and a top-10-weight breadth band are NOT in this file** — they are
> *proposed* additions. Their bands are reviewed below and flagged accordingly.
> **`net_liquidity` is also NOT in `computeComposite`** — reviewed below as a
> proposed/thesis component, with its evidence quality flagged honestly.

---

## 1. Core recession / stress predictors

### 1.1 Yield curve — 10Y-3M (`t10y3m`, weight 0.13) and 10Y-2Y (`yield_curve`, weight 0.06)
**Code band:** `interp(x, [[1.5,0],[0,70],[-0.5,100]])` — i.e. inversion (≤0) maps to high risk; deep inversion (-0.5) = 100.

**Authoritative sources**
- NY Fed, *The Yield Curve as a Leading Indicator* (model landing page + monthly recession-probability data): https://www.newyorkfed.org/research/capital_markets/ycfaq and https://www.newyorkfed.org/medialibrary/media/research/capital_markets/prob_rec.pdf
- Estrella, A. & Mishkin, F. (1996), *The Yield Curve as a Predictor of U.S. Recessions*, FRBNY Current Issues in Economics and Finance 2(7): https://www.newyorkfed.org/medialibrary/media/research/current_issues/ci2-7.pdf
- FRED series: T10Y3M https://fred.stlouisfed.org/series/T10Y3M and T10Y2Y https://fred.stlouisfed.org/series/T10Y2Y
- SF Fed corroboration: https://www.frbsf.org/research-and-insights/publications/economic-letter/2022/05/current-recession-risk-according-to-yield-curve/

**What it supports / does NOT.** Strong. The NY Fed's official probit model uses the
**10Y-3M** spread to estimate P(recession within 12 months) — `Φ(-0.5333 - 0.6629·spread)`.
Inversion has preceded every U.S. recession since the late 1960s with a **6–24-month lead**.
It does **NOT** predict *equity-market* timing or magnitude — it is a recession-onset
probability over a ~1-year horizon. The model canonically uses **10Y-3M**, which is why
the code correctly weights `t10y3m` (0.13) above `yield_curve` 10Y-2Y (0.06); 10Y-2Y is
the popularly-watched but academically secondary spread.

**Assessment.** Signal: **grounded / strong academic + official methodology.** The
*relative* weighting (3M-spread > 2Y-spread) is well-justified. The specific band
breakpoints (+1.5 → 0 risk, 0 → 70, -0.5 → 100) are **a reasonable mapping but not
literature-calibrated** — the NY Fed model is non-linear (probit) and crosses its
own ~30% recession-probability alarm threshold near a *slightly positive-to-zero*
spread, so mapping flat-zero to "70/100 risk" is defensible and conservative, but the
exact knee points are author-chosen.
**Evidence quality: Strong academic / official methodology** (signal); **heuristic** (exact band knots).

### 1.2 Sahm rule (`sahm_rule`, weight 0.10)
**Code band:** `interp(x, [[0,0],[0.5,100]])` — 0.50pp trigger = 100 risk.

**Authoritative sources**
- FRED real-time series SAHMREALTIME: https://fred.stlouisfed.org/series/SAHMREALTIME and current SAHMCURRENT: https://fred.stlouisfed.org/series/SAHMCURRENT
- Claudia Sahm (2019), policy chapter introducing the rule (Hamilton Project / Brookings); FRED notes credit Sahm, C. (2019).

**What it supports / does NOT.** Strong/official. Triggers when the 3-month moving
average of U3 unemployment rises **≥0.50pp** above its trailing-12-month minimum. It is
a **real-time recession-*start* identifier**, deliberately simple, designed to green-light
fiscal stimulus at onset — not a leading indicator and not a market-timing tool. Sahm
herself frames it as a coincident trigger.

**Assessment.** **Grounded.** The 0.50pp = 100-risk anchor is *exactly* the published
trigger, so the band's top end is literature-defined. Treating any positive value below
0.50 as proportionally elevated is a reasonable linearization (the rule itself is binary at 0.50).
**Evidence quality: Official methodology / strong (the threshold is the published rule).**

### 1.3 NFCI — National Financial Conditions Index (`nfci`, weight 0.18 — the single largest core weight)
**Code band:** `interp(x, [[-0.5,0],[0,50],[1.0,100]])`.

**Authoritative sources**
- Chicago Fed NFCI "About": https://www.chicagofed.org/research/data/nfci/about
- NFCI FAQ (PDF): https://www.chicagofed.org/-/media/publications/nfci/nfci-faqs-pdf.pdf
- FRED series NFCI: https://fred.stlouisfed.org/series/NFCI
- "What Does the NFCI Tell Us About Future Economic Growth?" (2024): https://www.chicagofed.org/publications/chicago-fed-insights/2024/nfci-future-economic-growth

**What it supports / does NOT.** Official methodology. The NFCI synthesizes 100+
indicators via dynamic factor analysis, scaled to **mean 0, standard deviation 1** since
1971. **Positive = tighter-than-average** financial conditions; **negative = looser**.
It is a broad financial-conditions gauge with documented forward links to growth; it is
**not** itself a calibrated recession-probability or equity-timing model.

**Assessment.** **Grounded in construction; band is sensible.** Because the series is a
z-score, the code's mapping (`0 → 50` at the historical average, `+1.0 SD → 100`,
`-0.5 SD → 0`) is a clean, defensible use of its native units. The **0.18 weight (largest
core term)** is an editorial choice — reasonable given NFCI already aggregates credit/
leverage/risk, but it does **partially double-count** HY and IG spreads (which are also
standalone core terms *and* NFCI inputs). Flag: weight is heuristic and overlaps other terms.
**Evidence quality: Official methodology** (signal + units); **heuristic** (0.18 weight, overlap with credit terms).

### 1.4 High-yield credit spread — ICE BofA US HY OAS (`hy_spread`, weight 0.15)
**Code band:** `interp(x, [[3.0,0],[5.0,50],[8.0,100]])` (percentage points).

**Authoritative sources**
- FRED series BAMLH0A0HYM2 (ICE BofA US High Yield Index OAS): https://fred.stlouisfed.org/series/BAMLH0A0HYM2
- Academic anchor for credit spreads → real activity: Gilchrist, S. & Zakrajšek, E. (2012), *Credit Spreads and Business Cycle Fluctuations*, American Economic Review 102(4) — the "excess bond premium" / GZ-spread literature. (Cite the AER paper; the Fed FEDS working-paper version is the canonical free copy.)

**What it supports / does NOT.** Strong + official. HY OAS is the market-priced premium
of below-investment-grade corporate bonds over Treasuries, published daily by ICE via
FRED since 1996. The credit-spread literature (Gilchrist-Zakrajšek) shows spreads — and
especially the "excess bond premium" component — have **leading-indicator** power for
output and are a real-time stress gauge. HY OAS is a stress/risk-appetite signal, **not a
precise recession clock**.

**Assessment.** **Signal grounded; bands practitioner-reasonable.** Regime context: HY OAS
has historically run ~3–4pp in calm periods, ~5–6pp in stress, and spiked to ~10–20pp in
2008 and ~10pp in March 2020. The code's `3 → 0 / 5 → 50 / 8 → 100` ladder matches that
practitioner regime mapping well. Exact knots are judgment but well-aligned with history.
**Evidence quality: Strong academic (signal) / official (series); practitioner-consensus (bands).**

### 1.5 Investment-grade credit spread (`credit_spread`, weight 0.08)
**Code band:** `interp(x, [[1.5,0],[2.5,50],[4.0,100]])` (pp).

**Authoritative source.** Same lineage as 1.4 — ICE BofA US Corporate (IG) OAS, FRED
BAMLC0A0CM: https://fred.stlouisfed.org/series/BAMLC0A0CM ; Gilchrist-Zakrajšek (2012) is
the academic anchor.

**Assessment.** Same logic as HY but with IG-appropriate levels (IG OAS runs ~1–1.5pp
calm, ~2.5pp stress, ~3.5–6pp in crises). Bands are practitioner-reasonable. Including
both IG and HY plus NFCI means credit is represented three ways — defensible (different
risk tiers) but a source of correlation in the composite. **Evidence quality: official
(series) / practitioner-consensus (bands).**

### 1.6 Initial jobless claims (`initial_claims`, weight 0.06)
**Code band:** `interp(x, [[200000,0],[300000,50],[400000,100]])`.

**Authoritative sources**
- Conference Board, U.S. Leading Economic Index — initial claims is a component: https://www.conference-board.org/topics/us-leading-indicators/
- DOL weekly claims (the primary release); FRED ICSA: https://fred.stlouisfed.org/series/ICSA and 4-week MA IC4WSA: https://fred.stlouisfed.org/series/IC4WSA

**What it supports / does NOT.** Official + practitioner. Initial claims is a long-standing
**component of the Conference Board LEI** (the LEI leads turning points by ~7 months) and
the highest-frequency labor signal. It is a *leading* labor indicator; not a standalone
recession-probability model. Best read on the **4-week moving average**.

**Assessment.** **Signal grounded; bands heuristic-but-reasonable.** ~300k sustained is a
commonly cited "meaningful deterioration" line and ~400k is firmly recessionary in modern
samples, so the `200k → 0 / 300k → 50 / 400k → 100` ladder is sensible. Note the raw level
drifts with labor-force size, so fixed thresholds will need periodic recalibration — that
is a real (minor) weakness. **Evidence quality: official (LEI membership) / practitioner-consensus (thresholds).**

### 1.7 CFNAI-MA3 (`cfnai_ma3`, weight 0.08)
**Code band:** `interp(x, [[0.2,0],[-0.35,60],[-0.70,100]])`.

**Authoritative sources**
- Chicago Fed CFNAI background & current data: https://www.chicagofed.org/research/data/cfnai/current-data and https://www.chicagofed.org/publications/chicago-fed-letter/2008/may-250
- FRED series CFNAIMA3: https://fred.stlouisfed.org/series/CFNAIMA3
- Lineage: Stock, J. & Watson, M. (1989) coincident-index methodology.

**What it supports / does NOT.** Strong/official. CFNAI is a weighted average of 85
monthly activity indicators (SD units; **0 = trend GDP growth**). The Chicago Fed
publishes explicit thresholds: a CFNAI-MA3 **below −0.70 following an expansion**
signals increasing recession likelihood; **above +0.20** signals significant expansion
likelihood.

**Assessment.** **Grounded — band knots map to the Chicago Fed's own published thresholds**
(`+0.2 → 0` and `−0.70 → 100` are literally the +0.20 and −0.70 lines). The −0.35 → 60
mid-knot is interpolation but consistent. The code's own card text (L184) already cites the
Chicago Fed methodology and Stock-Watson lineage. **Evidence quality: Official methodology / strong.**

### 1.8 Unemployment rate (`unemployment_rate`, w 0.04) & Fed-funds rate (`fed_funds_rate`, w 0.02)
- UNRATE: https://fred.stlouisfed.org/series/UNRATE ; FEDFUNDS: https://fred.stlouisfed.org/series/FEDFUNDS
- **Assessment.** Standard official series; the bands (`3.5/4.5/6.0` for UNRATE; `0/3/6` for
fed funds as "policy restrictiveness") are **heuristic**. They overlap with the Sahm rule
(UNRATE) and with NFCI/term-spread (policy stance), and carry the two smallest weights —
appropriately, since they add little independent information. No specific paper supports
the exact cutoffs. **Evidence quality: official (series) / heuristic (bands).**

### 1.9 `bear_score` (weight 0.10) — RESEARCHED: Goldman BMRI twin + literature-anchored + Lab-validated
**Correction (2026-06-08): this was researched, not arbitrary.** `d.bear_score.score` is the
platform's **"Bear Market Risk Score"**, explicitly built as an **architectural twin of the
Goldman Sachs Bear Market Risk Indicator** (Mueller-Glissmann, Oppenheimer et al., GS Portfolio
Strategy Research, 2017) — the published five/six-factor recession-regime indicator (valuation,
growth momentum, yield-curve slope, inflation, unemployment/ISM). See the in-code attribution at
`console/src/app/market/page.tsx:104-105` ("Architectural twin of the Goldman Sachs Bear Market
Risk Indicator (Mueller-Glissmann et al., 2017)"). Its sub-thresholds are **literature-anchored,
external anti-overfit anchors** — Sahm ≥ 0.50, CFNAI-MA3 ≤ −0.70, SOS ≥ 0.20 — the same published
breakpoints used elsewhere in this doc (§1.x). It was **pre-registered as a single-hypothesis Lab
candidate** (`docs/superpowers/specs/2026-05-21-sentinel-bear-score-lab-candidate.md`) and run
through the platform's held-back DSR/credibility graduation gate (`python -m ops.lab --candidate
sentinel_bear_score`), with one pre-declared equal-weight robustness ablation; the constants are
pinned in `sentinel/` (engine impl + `test_bear_score_byte_identical.py`). So it is **not** an
unreferenced black box — its lineage is a published GS indicator + literature-anchored thresholds
+ an anti-overfit Lab validation.
**Real residual caveat (kept):** because bear_score blends macro sub-signals (Sahm, CFNAI, yield
curve, credit) that ALSO appear as standalone CORE terms, there is **partial double-counting** at
the 0.10 weight — worth a one-time audit of overlap, but this is a weighting nuance, not an
"unreferenced" problem.
**Evidence quality: research-backed** (published-indicator architecture + literature-anchored
breakpoints + pre-registered Lab validation); the only open item is the intra-CORE overlap audit.

---

## 2. Valuation — bounded multiplier (0.85–1.15), `valSub` from CAPE + Buffett

### 2.1 Shiller CAPE (`cape`)
**Code band:** `interp(x, [[16,0],[27,50],[38,100]])`.

**Authoritative sources**
- Robert Shiller online data ("irrationalexuberance" / Yale): http://www.econ.yale.edu/~shiller/data.htm
- Campbell, J. & Shiller, R. (1988), *Stock Prices, Earnings, and Expected Dividends*, Journal of Finance 43(3).
- Shiller, R. (2000), *Irrational Exuberance*.

**What it supports / does NOT.** Strong academic. CAPE (price ÷ 10-yr inflation-adjusted
average earnings) predicts **long-horizon (10–20 yr) real returns** — high CAPE ⇒ low
subsequent returns. It explicitly **does NOT time crashes or short-term moves**; Shiller
states it is not a crash-timing tool. 20th-century average ≈ 15–16.

**Assessment.** **Grounded as a long-horizon valuation read; band knots reasonable.** 16 ≈
historical average (→ neutral-low), high-20s ≈ rich, ~38 ≈ dot-com-peak territory.
Critically, the **design choice to use valuation only as a bounded ±15% multiplier — not a
timing input — is exactly what the literature supports** (CAPE forecasts returns, not
timing). That architectural decision is research-correct.
**Evidence quality: Strong academic (signal) / practitioner-consensus (band knots).**

### 2.2 Buffett indicator (`buffett`)
**Code band:** `interp(x, [[90,0],[120,50],[180,100]])`.

**Authoritative sources**
- Buffett, W. & Loomis, C., Fortune (Dec 10, 2001) — "probably the best single measure of where valuations stand at any given moment."
- Buffett indicator overview / Wilshire-to-GDP: https://en.wikipedia.org/wiki/Buffett_indicator ; FRED constructs market-cap/GDP via Wilshire 5000 (WILL5000PR) ÷ GDP.

**What it supports / does NOT.** Practitioner/famous-quote. Total US equity market cap ÷
GDP (originally GNP). A **long-horizon valuation gauge**, like CAPE — not a timing tool.
Caveat worth surfacing: **Buffett himself later walked back** endorsing any single measure,
and the ratio drifts structurally upward over decades (rising corporate-profit share, foreign
revenue, falling rates), so fixed thresholds age. The denominator/numerator choice (GDP vs
GNP, Wilshire vs total cap) materially shifts the level.

**Assessment.** **Reasonable but more heuristic than CAPE.** 90–120% spanned "fair-to-rich"
historically, 180%+ is extreme by historical standards — defensible band — **but** the
structural upward drift means today's "extreme" line is contested. Same correct architectural
treatment (bounded multiplier, not timing). **Evidence quality: practitioner-consensus (signal); heuristic (band, due to secular drift).**

### 2.3 Valuation → bounded multiplier (`valMult` 0.85–1.15)
The decision to convert valuation into a **±15% multiplier** on the core stress score,
rather than a direct additive risk term, is the single most research-aligned design choice
in the composite: it encodes "valuation raises the stakes / shapes long-run returns but does
not say *when*." This is well-supported by the CAPE literature and matches the code's own
prose ("a backdrop, not a timing signal," L548–549). **Evidence quality: strong (the *use* of valuation is correct); the ±15% magnitude is heuristic.**

---

## 3. Timing overlay — stress bands (bounded ±8)

> The code's timing overlay uses **VIX + breadth only**. **MOVE and VVIX are NOT wired in**
> — their proposed bands are validated below as forward guidance.

### 3.1 VIX (`vix`, wired) — `interp(x, [[12,0],[20,40],[30,70],[40,100]])`
**Authoritative sources**
- CBOE VIX Methodology white paper: https://cdn.cboe.com/resources/indices/Volatility_Index_Methodology_Cboe_Volatility_Index.pdf
- FRED VIXCLS: https://fred.stlouisfed.org/series/VIXCLS

**What it supports / does NOT.** Official methodology. VIX = market's expected 30-day S&P 500
volatility from option prices. The CBOE white paper defines *construction*; it does **not**
publish "calm/elevated/stress" regime cutoffs — those are practitioner convention. Common
convention: <20 calm, 20–30 elevated, >30 stressed, >40 panic.

**Assessment.** **Signal: official; bands: practitioner-consensus and well-aligned.** The
`12/20/30/40` ladder matches the widely-used regime convention. Grounded enough to defend.
**Evidence quality: official (index) / practitioner-consensus (regime bands).**

### 3.2 Breadth / concentration (`breadth.conc_1y`, wired) — `interp(x, [[3,0],[0,50],[-6,100]])`
**Implementation reality:** this is **RSP (equal-weight S&P 500) minus ^GSPC (cap-weight) trailing
total-return spread in percentage points** (see card detail, L361), *not* a top-10-weight measure.
Negative = cap-weight beating equal-weight = mega-caps carrying the index = narrow breadth.

**Authoritative sources (concentration/fragility literature)**
- Goldman Sachs, *Market concentration: how big a worry?*: https://www.goldmansachs.com/insights/top-of-mind/market-concentration-how-big-a-worry
- Morgan Stanley, *Concentration Risk Remains High in S&P 500*: https://www.morganstanley.com/ideas/concentration-risk-high-s-and-p-500-q2-2023
- S&P DJI S&P 500 Top 10 Index (for the actual top-10 weight series): https://www.spglobal.com/spdji/en/indices/equity/sp-500-top-10-index/
- RBC, *The "Great Narrowing"*: https://www.rbcwealthmanagement.com/en-us/insights/the-great-narrowing-sp-500-concentration

**What it supports / does NOT.** Practitioner-consensus. Sell-side research (GS, MS) documents
that record concentration → **fragility / larger drawdown potential / higher single-name
sensitivity** — but explicitly notes concentration is **not a timing signal** and can persist
for years. The RSP-vs-GSPC spread is a *valid, commonly-used* market-internals proxy for
breadth/narrowness (and avoids needing live index-weight data).

**Assessment.** **Signal direction grounded; band knots heuristic.** Treating cap-weight
out-running equal-weight as "narrow/higher fragility" is well-supported. The specific knots
(`+3pp → 0`, `0 → 50`, `−6pp → 100`) are **author-chosen with no published calibration** —
defensible as a judgment call but flag them as heuristic. **Note the proposed "top-10 weight
20/30/40%" band (task brief) is a *different* indicator than what's wired**; for context, top-10
weight hit a **record ~37% at end-2024 and ~40%+ in 2025**, so a ">35–40% = extreme" framing is
consistent with GS/MS commentary — but no paper sets a hard ">40% ⇒ crash" line. **Evidence
quality: practitioner-consensus (signal) / heuristic (specific bands, both the wired RSP-GSPC knots and the proposed top-10 cutoffs).**

### 3.3 MOVE index — **PROPOSED, NOT WIRED.** Proposed bands 60/100/140/180.
**Authoritative sources**
- ICE Data Indices MOVE page: https://developer.ice.com/fixed-income-data-services/catalog/ice-data-indices-move-index
- Schwab explainer: https://www.schwab.com/learn/story/whats-move-index-and-why-it-might-matter

**What it supports / does NOT.** Official (issuer) for *construction*: MOVE = yield-curve-
weighted basket of 1-month at-the-money options on 2/5/10/30-yr Treasuries — "the VIX for bonds."
ICE/issuer does **not** publish calm/stress regime cutoffs.

**Assessment of proposed 60/100/140/180.** **Sensible-but-judgment-call, NOT literature-backed.**
Historical context corroborates the *spirit*: long-run "normal" range ~55–130, record low ~36
(Sep 2020), COVID spike ~170 (Mar 2020), 2023 banking turmoil ~200, GFC peak ~264. So
`60 ≈ calm`, `100 ≈ elevated`, `140 ≈ stressed`, `180 ≈ near-crisis` is a **reasonable
mapping onto observed history** — but the breakpoints are author-chosen, not from any
published regime study. **Flag: heuristic (sensible). Evidence quality: official (index) / heuristic (bands).**

### 3.4 VVIX — **PROPOSED, NOT WIRED.** Proposed bands 80/100/120/150.
**Authoritative sources**
- CBOE VVIX primer ("Double the Fun with CBOE's VVIX"): https://cdn.cboe.com/resources/indices/documents/vvix-termstructure.pdf
- CBOE VVIX dashboard: https://www.cboe.com/us/indices/dashboard/vvix/

**What it supports / does NOT.** Official for construction: VVIX = expected 30-day vol of VIX,
computed with VIX-option prices via the VIX method. No issuer-published regime cutoffs.

**Assessment of proposed 80/100/120/150.** **Sensible-but-judgment-call, NOT literature-backed.**
Historical context: VVIX **long-run average ≈ 85–90**; readings <75 = complacency, sustained
>120 = elevated fear/dislocation; recent 52-wk range roughly 82–147. So the proposed
`80/100/120/150` ladder lands the average near the low end and 150 near observed extremes —
**reasonable**, but again author-chosen, not from a published study. **Flag: heuristic (sensible).
Evidence quality: official (index) / heuristic (bands).**

---

## 4. Sentiment tilt (bounded ±4) — contrarian, small weights

### 4.1 CNN Fear & Greed (`score`, tilt cap 1.5) — greed treated as risk
**Authoritative source.** CNN Fear & Greed (methodology + live): https://www.cnn.com/markets/fear-and-greed
**What it supports / does NOT.** Official (issuer) methodology: equal-weighted blend of 7
indicators (momentum, price strength, breadth, put/call, junk-bond demand, volatility, safe-haven
demand), 0–100; <25 extreme fear, >75 extreme greed. It's a *sentiment* gauge; **CNN does not
publish forward-return evidence**, and it partially overlaps the composite's own inputs
(volatility, breadth, junk-bond demand ≈ VIX/breadth/HY). **Assessment:** correctly used as a
**small contrarian tilt** (greed → risk), bounded — appropriate given weak/overlapping evidence.
**Evidence quality: official methodology (construction) / practitioner-consensus (contrarian use).**

### 4.2 AAII bull-bear (`bullish_pct`, tilt cap 1.0)
**Authoritative sources**
- AAII Sentiment Survey: https://www.aaii.com/sentimentsurvey
- AAII, *Investor Sentiment as a Contrarian Indicator*: https://www.aaii.com/journal/article/feature-investor-sentiment-as-a-contrarian-indicator

**What it supports / does NOT.** Practitioner + AAII's own analysis: weekly survey since 1987;
historical average **bullish ≈ 38–39%**. AAII documents it as a **contrarian** indicator at
*extremes* (record 70%+ bearish marked the March 2009 bottom) — but **AAII itself states the
record at ±1 SD is "mixed"** and it is "not a flawless contrarian indicator." **Assessment:**
the `20/40/60` band (40 ≈ average → neutral) and the small contrarian tilt are well-matched to
AAII's own guidance — *and* the small cap honestly reflects the weak signal. **Evidence quality:
practitioner-consensus / issuer-documented (with issuer-acknowledged weakness).**

### 4.3 Michigan consumer sentiment (`michigan_inv`, tilt cap 0.75)
**Authoritative source.** University of Michigan Surveys of Consumers: http://www.sca.isr.umich.edu/ ; FRED UMCSENT: https://fred.stlouisfed.org/series/UMCSENT
**Assessment.** Long-standing official survey; used inverted (low sentiment → risk) at tiny cap.
The `100/70/50` band is heuristic but the weight is negligible. **Evidence quality: official (series) / heuristic (bands).**

### 4.4 EPU — Economic Policy Uncertainty (`epu`, tilt cap 0.75)
**Authoritative source.** Baker, S., Bloom, N. & Davis, S. (2016), *Measuring Economic Policy
Uncertainty*, Quarterly Journal of Economics 131(4): https://academic.oup.com/qje/article/131/4/1593/2468873 ; index site: https://www.policyuncertainty.com/ ; FRED USEPUINDXD.
**What it supports / does NOT.** **Strong academic** for the *index itself*; the code's own card
(L310) already cites Baker-Bloom-Davis 2016 correctly and honestly flags the caveat
("news-attention-driven; can spike on political theater"). **Assessment:** well-grounded index,
correctly caveated, tiny tilt weight. **Evidence quality: strong academic (index) / heuristic (band knots).**

---

## 5. `net_liquidity` (Fed balance sheet − TGA − RRP) — PROPOSED / THESIS, NOT WIRED

> **Not present in `computeComposite`.** Reviewed because the brief asked. If added, flag carefully.

**Sources / mechanics**
- WALCL (Fed total assets): https://fred.stlouisfed.org/series/WALCL ; WTREGEN (TGA): https://fred.stlouisfed.org/series/WTREGEN ; RRPONTSYD (ON RRP): https://fred.stlouisfed.org/series/RRPONTSYD
- The "net liquidity = WALCL − TGA − RRP drives risk assets" framing is a **practitioner thesis**, popularized by analyst **Max Anderson** and tracked on GuruFocus/TradingView — not a peer-reviewed finding.

**What it supports / does NOT — HONEST EVIDENCE FLAG.** The **mechanics are real and FRED-sourced**
(reserves = assets − non-reserve liabilities; TGA and RRP drains/adds shift bank reserves). There
**is** legitimate Fed/academic work on **reserve balances and asset prices / repo and money-market
dynamics** (e.g., post-2019 ample-reserves regime literature). **But** the specific "net liquidity
predicts the S&P 500 with ~0.95 correlation, 2-week lead" claim is a **practitioner assertion with
no peer-reviewed support**; that correlation is sample-period-dependent, prone to spurious
co-trending (both QE and equities rose 2020–21), and has weakened in later samples. **Assessment:
practitioner-thesis only — defensible as a *contextual* macro-liquidity read, NOT as a calibrated
predictor. If wired in, weight it small and label it explicitly as thesis-grade.**
**Evidence quality: practitioner-consensus at best; the headline correlation claim is heuristic/unverified.**

---

## 6. Honest summary

**Research-backed core (defensible without caveat):** the recession/stress backbone is strong.
The **yield-curve term spread** (Estrella-Mishkin / NY Fed), **Sahm rule** (its 0.50pp anchor),
**NFCI** (official z-score construction), **HY/IG credit spreads** (Gilchrist-Zakrajšek + ICE/FRED),
**CFNAI-MA3** (Chicago Fed's own −0.70 / +0.20 thresholds), and **initial claims** (LEI component)
are all genuine, primary-sourced signals, and several band *knots* (Sahm 0.50, CFNAI ±0.70/+0.20)
map exactly to published thresholds. The **valuation block (CAPE strong-academic, Buffett
practitioner) used as a bounded ±15% multiplier rather than a timing input is the most
research-aligned design decision in the whole composite** — it does exactly what the literature
says valuation can and cannot do.

**Defensible heuristics (sensible, judgment-calibrated, not literature-derived):** essentially
all the **band breakpoints** outside the few that map to published thresholds — the VIX
12/20/30/40 ladder (practitioner convention, well-aligned), the proposed **MOVE 60/100/140/180**
and **VVIX 80/100/120/150** ladders (sensible vs. observed history but author-chosen — flag as
heuristic), the **breadth/concentration knots** (RSP-GSPC and the proposed top-10 20/30/40%; signal
direction is GS/MS-supported, exact cutoffs are not), the UNRATE/fed-funds bands, and the **core
weights** themselves (0.18 NFCI etc.), which are reasonable but introduce **credit/financial-
conditions double-counting** across NFCI ⊃ HY ⊃ IG and possibly `bear_score`.

**Flag honestly to the operator:** (1) **`bear_score` (w 0.10) has no external reference** — it's an
internal composite; its credibility must come from auditing *its* construction, and it risks
double-counting. (2) **`net_liquidity` is practitioner-thesis only** — the famous "0.95 correlation"
is unverified/sample-dependent; use as context, not a predictor. (3) **MOVE/VVIX/top-10-weight bands
are sensible judgment calls, not literature-backed** — the operator can stand behind them as
"calibrated to observed historical regimes," which is true, but should not claim peer-reviewed
thresholds. (4) Several Fed/FRED source pages were search-confirmed, not directly fetched (servers
403 the fetcher) — re-verify numeric quotes against the live pages before any public citation.

**Bottom line:** the composite's *signal selection* and the *valuation-as-multiplier architecture*
are genuinely research-grounded; the *thresholds and weights* are mostly defensible heuristics —
honest framing is "evidence-based indicators combined with judgment-calibrated bands," not
"fully literature-derived."
