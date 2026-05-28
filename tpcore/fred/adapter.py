"""FRED macro-indicators adapter.

Pulls daily/weekly/monthly observations for the macro series listed
in :data:`INDICATOR_SERIES` from the St. Louis Fed FRED API:

* ``sahm_rule``            — SAHMREALTIME (monthly recession indicator)
* ``industrial_production`` — INDPRO (monthly PMI proxy)
* ``initial_claims``       — IC4WSA (weekly 4-wk MA jobless claims)
* ``yield_curve``          — T10Y2Y (daily 10y-2y Treasury spread)
* ``credit_spread``        — BAA10Y (Moody's Seasoned Baa Corporate Bond
                              Yield relative to the 10-Year Treasury,
                              daily — credit stress proxy)
* ``hy_spread``            — BAMLH0A0HYM2 (daily HY OAS; FRED-rolling
                              tail + recovered pre-2023 history)
* ``vix``                  — VIXCLS (daily CBOE Volatility Index close)
* ``cfnai_ma3``            — CFNAIMA3 (monthly Chicago Fed National
                              Activity Index, 3-month MA — Sentinel
                              Bear Score band anchor, added 2026-05-20)
* ``phci_<state>`` × 50    — {XX}PHCI (monthly Coincident Economic
                              Activity Index per US state, Phila Fed;
                              1979→present; substrate for the derived
                              ``sos_state_diffusion`` series consumed
                              by the Sentinel graduated Bear Score Lab
                              candidate, added 2026-05-21)

**2026-05-15 — BAA10Y replaces BAMLH0A0HYM2.** FRED permanently truncated
the HY OAS series (``BAMLH0A0HYM2``) to a rolling 3-year window starting
April 2026; the full pre-2023 history is no longer accessible through
any free source. BAA10Y is a free FRED series with full history back to
1996, strong correlation with the HY OAS in crises, and no truncation.
The historical ``hy_spread`` rows in ``platform.macro_indicators`` are
retained for audit but no longer refreshed.

FRED API docs: https://fred.stlouisfed.org/docs/api/fred/
Rate limit: 120 requests per minute (we pull 5 series → 5 calls per
run — well under the limit; courtesy delay is symbolic).

Reference implementation: ``tpcore.sec.SECEdgarAdapter``. Same shape:
fail-fast at construction on missing API key, ``@with_retry`` on the
HTTP layer, ``DataProviderOutage`` mapping at the public-method
boundary, structured logging.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from tpcore.outage import DataProviderOutage, with_retry

logger = structlog.get_logger(__name__)

# ── Configuration constants ────────────────────────────────────────────

_PROVIDER_NAME = "fred"
_API_KEY_ENV = "FRED_API_KEY"
_BASE_URL = "https://api.stlouisfed.org/fred"
_DEFAULT_TIMEOUT_S = 30.0
_INTER_REQUEST_SLEEP_S = 0.5  # well under FRED's 120/min courtesy budget


INDICATOR_SERIES: tuple[tuple[str, str], ...] = (
    ("sahm_rule",            "SAHMREALTIME"),
    ("industrial_production", "INDPRO"),
    ("initial_claims",       "IC4WSA"),
    ("yield_curve",          "T10Y2Y"),
    ("credit_spread",        "BAA10Y"),
    # hy_spread re-activated 2026-05-16: the full pre-truncation history
    # was recovered (eco-archive + Scribd gap, validated 772/772 exact)
    # and is contiguous 1996→2026 in macro_indicators. FRED still serves
    # the rolling ~3yr window for BAMLH0A0HYM2, so keeping it here lets
    # the weekly stage keep the recent tail fresh going forward
    # (idempotent ON CONFLICT — never touches the recovered history).
    # BAA10Y stays the Sentinel Bear-Score signal; the HY→Sentinel
    # scoring switch is a separate, deferred, backtest-gated decision.
    ("hy_spread",            "BAMLH0A0HYM2"),
    # VIX close (CBOE Volatility Index) — added 2026-05-16 for the
    # Fear & Greed volatility component. FRED VIXCLS has full daily
    # history from 1990-01-02; no new provider (FRED is existing).
    ("vix",                  "VIXCLS"),
    # Chicago Fed National Activity Index, 3-month moving average —
    # added 2026-05-20 to unblock the Sentinel graduated Bear Score Lab
    # candidate, which uses a ``CFNAI ≤ -0.70`` band anchor. CFNAIMA3
    # publishes MONTHLY (FRED release calendar: monthly, around the 4th
    # week of the following month). No new provider (FRED existing).
    ("cfnai_ma3",            "CFNAIMA3"),
    # Chicago Fed National Financial Conditions Index — weekly composite
    # of credit + vol + liquidity. Added 2026-05-23 per financial-expert
    # recommendation (Brave & Butters 2011; Carver Ch. 15 endorses
    # single-composite regime gates). Subsumes credit + vol + liquidity
    # into one number — sentinel + carver consume as primary regime gate;
    # threshold NFCI > 0 = tighten, > +1σ = full defensive. Series ID is
    # the FRED canonical (no new provider; FRED existing).
    ("nfci",                 "NFCI"),
    # Secured Overnight Financing Rate — daily, since 2018-04-03.
    # SOFR replaces LIBOR as the US dollar reference rate; useful as a
    # short-rate signal + as the building block for SOFR-OIS spread
    # (expert-recommended as an alt to the discontinued TED spread).
    # Added 2026-05-24. FRED-verified (series 'SOFR').
    ("sofr",                 "SOFR"),
    # Economic Policy Uncertainty Index — daily, since 1985-01-01.
    # Baker-Bloom-Davis newspaper-text-based index. Pástor & Veronesi 2013
    # show EPU regime conditioning affects equity returns around news
    # events — useful for catalyst engine event-window risk weighting.
    # Added 2026-05-24. FRED-verified (series 'USEPUINDXD').
    ("epu_index",            "USEPUINDXD"),
    # University of Michigan Consumer Sentiment Index — monthly, since
    # 1952. Tracks how consumers feel about the economy + their own
    # finances; a leading indicator that historically turned down 6-12
    # months before recessions (1973, 1980, 1990, 2001, 2008). Added
    # 2026-05-27 for the public market-health page (Mood → Consumer).
    # FRED-verified (series 'UMCSENT'). License: free, used widely.
    ("michigan_sentiment",   "UMCSENT"),
    # Unemployment rate — monthly, since 1948. The headline labor-
    # market read. Sahm rule is derived from this; included alongside
    # for direct display on the public market-health page.
    ("unemployment_rate",    "UNRATE"),
    # Federal funds effective rate — daily, since 1954. The Fed's
    # policy lever; useful context for every other rate-sensitive
    # indicator on the page.
    ("fed_funds_rate",       "DFF"),
    # 10-year minus 3-month Treasury yield — the canonical
    # Estrella-Mishkin (1996, NY Fed) recession predictor. Sibling to
    # the existing T10Y2Y `yield_curve` series; T10Y3M is the version
    # most academic recession-probability models use because the
    # 3-month rate tracks the Fed policy stance more directly.
    ("t10y3m",               "T10Y3M"),
    # 3-month VIX (VXVCLS) — for VIX term structure / VIX:VXV ratio.
    # Per the financial-expert improvement report (Whaley 2009; Whaley
    # on VIX statistical properties), term-structure indicators
    # de-double-count momentum that a simple VIX-above-MA kicker
    # introduces. Daily since 2007-12-04.
    ("vxv",                  "VXVCLS"),
    # ──── Carbondale, IL economic-development panel (2026-05-27) ────
    # Sub-state / MSA-level series for the /carbondale public page.
    # Mix of Jackson County (FIPS 17077), Carbondale-Marion MSA (CBSA
    # 16060), and Williamson County (17199, the other MSA county).
    # All freely licensed via BLS LAUS, BEA, Census ACS, Realtor.com
    # via FRED. Annual series lag 6-18 months; monthly series lag 1-2.
    # Jackson County, IL (FIPS 17077)
    # Man-Tra-Con LWA-25 (Southern Illinois Workforce Development Board)
    # 5-county service area. FRED LAUCN format is annual-only (-A suffix);
    # monthly BLS LAUS county data lives under the IL{XX}{UR,LF}N shorthand.
    # Jackson (FIPS 17077), Franklin (17055), Jefferson (17081),
    # Perry (17145), Williamson (17199).
    ("crb_jackson_unemployment_rate",   "ILJAURN"),                  # monthly, 1990+
    ("crb_jackson_labor_force",         "ILJALFN"),                  # monthly, 1990+
    ("crb_franklin_unemployment_rate",  "ILFRURN"),                  # monthly, 1990+
    ("crb_franklin_labor_force",        "ILFRLFN"),                  # monthly, 1990+
    ("crb_jefferson_unemployment_rate", "ILJEURN"),                  # monthly, 1990+
    ("crb_jefferson_labor_force",       "ILJELFN"),                  # monthly, 1990+
    ("crb_perry_unemployment_rate",     "ILPRURN"),                  # monthly, 1990+
    ("crb_perry_labor_force",           "ILPRLFN"),                  # monthly, 1990+
    ("crb_williamson_unemployment_rate","ILWMURN"),                  # monthly, 1990+
    ("crb_williamson_labor_force",      "ILWMLFN"),                  # monthly, 1990+
    ("crb_jackson_personal_income",     "PI17077"),                  # annual, 1969+
    ("crb_jackson_real_gdp",            "REALGDPALL17077"),         # annual, 2001+
    ("crb_jackson_median_hh_income",    "MHIIL17077A052NCEN"),       # annual, 1989+
    ("crb_jackson_snap_recipients",     "CBR17077ILA647NCEN"),       # annual, 1989+
    ("crb_jackson_poverty_universe",    "PUAAIL17077A647NCEN"),      # annual, 1998+
    ("crb_jackson_single_parent_pct",   "S1101SPHOUSE017077"),       # annual, 2009+
    # Carbondale-Marion MSA (CBSA 16060) — Jackson + Williamson counties
    ("crb_msa_population",              "CRBPOP"),                   # annual, 2010+
    ("crb_msa_unemployment_rate",       "LAUMT171606000000003"),    # monthly, 1990+
    ("crb_msa_labor_force",             "LAUMT171606000000006"),    # monthly, 1990+
    ("crb_msa_private_service_jobs",    "SMU17160600800000001SA"),   # monthly, 1990+
    ("crb_msa_avg_hourly_earnings",     "SMU17160600500000003SA"),   # monthly, 2011+
    ("crb_msa_avg_weekly_earnings",     "SMU17160600500000011SA"),   # monthly, 2011+
    ("crb_msa_housing_days_on_market",  "MEDDAYONMAR16060"),         # monthly, 2016+ (Realtor.com)
    ("crb_msa_housing_new_listings_mom","NEWLISCOUMM16060"),         # monthly, 2017+
    ("crb_msa_housing_price_inc_yoy",   "PRIINCCOUYY16060"),         # monthly, 2017+
    # ──── East Central Illinois / LWA-23 13-county panel (2026-05-28) ────
    # Parallel substrate for the /east-central-illinois regional page (mirrors
    # the crb_<county>_* 5-county LWA-25 panel above). LWA-23 admin: CEFS
    # Economic Opportunity Corporation, Effingham. 13 counties: Clark (17023),
    # Clay (17025), Coles (17029), Crawford (17033), Cumberland (17035), Edgar
    # (17045), Effingham (17049), Fayette (17051), Jasper (17079), Lawrence
    # (17101), Marion (17121), Moultrie (17139), Richland (17159).
    # FRED IL-county abbrevs that disambiguate counties starting with the same
    # letters use a 4-letter abbrev + numeric suffix (CLAR3 = Clark; COLE3 =
    # Coles; CUMB5 = Cumberland; EDGA5 = Edgar; MOUL9 = Moultrie).
    ("eci_clark_unemployment_rate",     "ILCLAR3URN"),               # monthly
    ("eci_clark_labor_force",           "ILCLAR3LFN"),
    ("eci_clay_unemployment_rate",      "ILCYURN"),                  # monthly
    ("eci_clay_labor_force",            "ILCYLFN"),
    ("eci_coles_unemployment_rate",     "ILCOLE3URN"),               # monthly
    ("eci_coles_labor_force",           "ILCOLE3LFN"),
    ("eci_crawford_unemployment_rate",  "ILCWURN"),                  # monthly
    ("eci_crawford_labor_force",        "ILCWLFN"),
    ("eci_cumberland_unemployment_rate","ILCUMB5URN"),               # monthly
    ("eci_cumberland_labor_force",      "ILCUMB5LFN"),
    ("eci_edgar_unemployment_rate",     "ILEDGA5URN"),               # monthly
    ("eci_edgar_labor_force",           "ILEDGA5LFN"),
    ("eci_effingham_unemployment_rate", "ILEFURN"),                  # monthly
    ("eci_effingham_labor_force",       "ILEFLFN"),
    ("eci_fayette_unemployment_rate",   "ILFAURN"),                  # monthly
    ("eci_fayette_labor_force",         "ILFALFN"),
    ("eci_jasper_unemployment_rate",    "ILJSURN"),                  # monthly
    ("eci_jasper_labor_force",          "ILJSLFN"),
    ("eci_lawrence_unemployment_rate",  "ILLWURN"),                  # monthly
    ("eci_lawrence_labor_force",        "ILLWLFN"),
    ("eci_marion_unemployment_rate",    "ILMRURN"),                  # monthly
    ("eci_marion_labor_force",          "ILMRLFN"),
    ("eci_moultrie_unemployment_rate",  "ILMOUL9URN"),               # monthly
    ("eci_moultrie_labor_force",        "ILMOUL9LFN"),
    ("eci_richland_unemployment_rate",  "ILRIURN"),                  # monthly
    ("eci_richland_labor_force",        "ILRILFN"),
    # Annual panel for the other 12 LWA-23 counties (Coles already covered
    # below under cle_coles_* for the /charleston city profile). 72 tuples =
    # 12 counties × {personal_income, real_gdp, median_hh_income,
    # snap_recipients, poverty_universe, single_parent_pct}. All verified
    # against FRED 2026-05-28 — every entry returns 200.
    ("eci_clark_personal_income",       "PI17023"),
    ("eci_clark_real_gdp",              "REALGDPALL17023"),
    ("eci_clark_median_hh_income",      "MHIIL17023A052NCEN"),
    ("eci_clark_snap_recipients",       "CBR17023ILA647NCEN"),
    ("eci_clark_poverty_universe",      "PUAAIL17023A647NCEN"),
    ("eci_clark_single_parent_pct",     "S1101SPHOUSE017023"),
    ("eci_clay_personal_income",        "PI17025"),
    ("eci_clay_real_gdp",               "REALGDPALL17025"),
    ("eci_clay_median_hh_income",       "MHIIL17025A052NCEN"),
    ("eci_clay_snap_recipients",        "CBR17025ILA647NCEN"),
    ("eci_clay_poverty_universe",       "PUAAIL17025A647NCEN"),
    ("eci_clay_single_parent_pct",      "S1101SPHOUSE017025"),
    ("eci_crawford_personal_income",    "PI17033"),
    ("eci_crawford_real_gdp",           "REALGDPALL17033"),
    ("eci_crawford_median_hh_income",   "MHIIL17033A052NCEN"),
    ("eci_crawford_snap_recipients",    "CBR17033ILA647NCEN"),
    ("eci_crawford_poverty_universe",   "PUAAIL17033A647NCEN"),
    ("eci_crawford_single_parent_pct",  "S1101SPHOUSE017033"),
    ("eci_cumberland_personal_income",  "PI17035"),
    ("eci_cumberland_real_gdp",         "REALGDPALL17035"),
    ("eci_cumberland_median_hh_income", "MHIIL17035A052NCEN"),
    ("eci_cumberland_snap_recipients",  "CBR17035ILA647NCEN"),
    ("eci_cumberland_poverty_universe", "PUAAIL17035A647NCEN"),
    ("eci_cumberland_single_parent_pct","S1101SPHOUSE017035"),
    ("eci_edgar_personal_income",       "PI17045"),
    ("eci_edgar_real_gdp",              "REALGDPALL17045"),
    ("eci_edgar_median_hh_income",      "MHIIL17045A052NCEN"),
    ("eci_edgar_snap_recipients",       "CBR17045ILA647NCEN"),
    ("eci_edgar_poverty_universe",      "PUAAIL17045A647NCEN"),
    ("eci_edgar_single_parent_pct",     "S1101SPHOUSE017045"),
    ("eci_effingham_personal_income",   "PI17049"),
    ("eci_effingham_real_gdp",          "REALGDPALL17049"),
    ("eci_effingham_median_hh_income",  "MHIIL17049A052NCEN"),
    ("eci_effingham_snap_recipients",   "CBR17049ILA647NCEN"),
    ("eci_effingham_poverty_universe",  "PUAAIL17049A647NCEN"),
    ("eci_effingham_single_parent_pct", "S1101SPHOUSE017049"),
    ("eci_fayette_personal_income",     "PI17051"),
    ("eci_fayette_real_gdp",            "REALGDPALL17051"),
    ("eci_fayette_median_hh_income",    "MHIIL17051A052NCEN"),
    ("eci_fayette_snap_recipients",     "CBR17051ILA647NCEN"),
    ("eci_fayette_poverty_universe",    "PUAAIL17051A647NCEN"),
    ("eci_fayette_single_parent_pct",   "S1101SPHOUSE017051"),
    ("eci_jasper_personal_income",      "PI17079"),
    ("eci_jasper_real_gdp",             "REALGDPALL17079"),
    ("eci_jasper_median_hh_income",     "MHIIL17079A052NCEN"),
    ("eci_jasper_snap_recipients",      "CBR17079ILA647NCEN"),
    ("eci_jasper_poverty_universe",     "PUAAIL17079A647NCEN"),
    ("eci_jasper_single_parent_pct",    "S1101SPHOUSE017079"),
    ("eci_lawrence_personal_income",    "PI17101"),
    ("eci_lawrence_real_gdp",           "REALGDPALL17101"),
    ("eci_lawrence_median_hh_income",   "MHIIL17101A052NCEN"),
    ("eci_lawrence_snap_recipients",    "CBR17101ILA647NCEN"),
    ("eci_lawrence_poverty_universe",   "PUAAIL17101A647NCEN"),
    ("eci_lawrence_single_parent_pct",  "S1101SPHOUSE017101"),
    ("eci_marion_personal_income",      "PI17121"),
    ("eci_marion_real_gdp",             "REALGDPALL17121"),
    ("eci_marion_median_hh_income",     "MHIIL17121A052NCEN"),
    ("eci_marion_snap_recipients",      "CBR17121ILA647NCEN"),
    ("eci_marion_poverty_universe",     "PUAAIL17121A647NCEN"),
    ("eci_marion_single_parent_pct",    "S1101SPHOUSE017121"),
    ("eci_moultrie_personal_income",    "PI17139"),
    ("eci_moultrie_real_gdp",           "REALGDPALL17139"),
    ("eci_moultrie_median_hh_income",   "MHIIL17139A052NCEN"),
    ("eci_moultrie_snap_recipients",    "CBR17139ILA647NCEN"),
    ("eci_moultrie_poverty_universe",   "PUAAIL17139A647NCEN"),
    ("eci_moultrie_single_parent_pct",  "S1101SPHOUSE017139"),
    ("eci_richland_personal_income",    "PI17159"),
    ("eci_richland_real_gdp",           "REALGDPALL17159"),
    ("eci_richland_median_hh_income",   "MHIIL17159A052NCEN"),
    ("eci_richland_snap_recipients",    "CBR17159ILA647NCEN"),
    ("eci_richland_poverty_universe",   "PUAAIL17159A647NCEN"),
    ("eci_richland_single_parent_pct",  "S1101SPHOUSE017159"),
    # LWA-23 annual labor + education panel — 60 series = 12 counties × 5
    # metrics: annual employed / unemployed / labor force + HS-grad pct +
    # Associate's-or-higher pct. All verified against FRED 2026-05-28.
    ("eci_clark_emp_persons_a",        "LAUCN170230000000005A"),
    ("eci_clark_unemp_persons_a",      "LAUCN170230000000004A"),
    ("eci_clark_labor_force_a",        "LAUCN170230000000006A"),
    ("eci_clark_hs_grad_pct",          "HC01ESTVC1617023"),
    ("eci_clark_assoc_or_higher_pct",  "S1501ACSTOTAL017023"),
    ("eci_clay_emp_persons_a",         "LAUCN170250000000005A"),
    ("eci_clay_unemp_persons_a",       "LAUCN170250000000004A"),
    ("eci_clay_labor_force_a",         "LAUCN170250000000006A"),
    ("eci_clay_hs_grad_pct",           "HC01ESTVC1617025"),
    ("eci_clay_assoc_or_higher_pct",   "S1501ACSTOTAL017025"),
    ("eci_crawford_emp_persons_a",     "LAUCN170330000000005A"),
    ("eci_crawford_unemp_persons_a",   "LAUCN170330000000004A"),
    ("eci_crawford_labor_force_a",     "LAUCN170330000000006A"),
    ("eci_crawford_hs_grad_pct",       "HC01ESTVC1617033"),
    ("eci_crawford_assoc_or_higher_pct","S1501ACSTOTAL017033"),
    ("eci_cumberland_emp_persons_a",   "LAUCN170350000000005A"),
    ("eci_cumberland_unemp_persons_a", "LAUCN170350000000004A"),
    ("eci_cumberland_labor_force_a",   "LAUCN170350000000006A"),
    ("eci_cumberland_hs_grad_pct",     "HC01ESTVC1617035"),
    ("eci_cumberland_assoc_or_higher_pct","S1501ACSTOTAL017035"),
    ("eci_edgar_emp_persons_a",        "LAUCN170450000000005A"),
    ("eci_edgar_unemp_persons_a",      "LAUCN170450000000004A"),
    ("eci_edgar_labor_force_a",        "LAUCN170450000000006A"),
    ("eci_edgar_hs_grad_pct",          "HC01ESTVC1617045"),
    ("eci_edgar_assoc_or_higher_pct",  "S1501ACSTOTAL017045"),
    ("eci_effingham_emp_persons_a",    "LAUCN170490000000005A"),
    ("eci_effingham_unemp_persons_a",  "LAUCN170490000000004A"),
    ("eci_effingham_labor_force_a",    "LAUCN170490000000006A"),
    ("eci_effingham_hs_grad_pct",      "HC01ESTVC1617049"),
    ("eci_effingham_assoc_or_higher_pct","S1501ACSTOTAL017049"),
    ("eci_fayette_emp_persons_a",      "LAUCN170510000000005A"),
    ("eci_fayette_unemp_persons_a",    "LAUCN170510000000004A"),
    ("eci_fayette_labor_force_a",      "LAUCN170510000000006A"),
    ("eci_fayette_hs_grad_pct",        "HC01ESTVC1617051"),
    ("eci_fayette_assoc_or_higher_pct","S1501ACSTOTAL017051"),
    ("eci_jasper_emp_persons_a",       "LAUCN170790000000005A"),
    ("eci_jasper_unemp_persons_a",     "LAUCN170790000000004A"),
    ("eci_jasper_labor_force_a",       "LAUCN170790000000006A"),
    ("eci_jasper_hs_grad_pct",         "HC01ESTVC1617079"),
    ("eci_jasper_assoc_or_higher_pct", "S1501ACSTOTAL017079"),
    ("eci_lawrence_emp_persons_a",     "LAUCN171010000000005A"),
    ("eci_lawrence_unemp_persons_a",   "LAUCN171010000000004A"),
    ("eci_lawrence_labor_force_a",     "LAUCN171010000000006A"),
    ("eci_lawrence_hs_grad_pct",       "HC01ESTVC1617101"),
    ("eci_lawrence_assoc_or_higher_pct","S1501ACSTOTAL017101"),
    ("eci_marion_emp_persons_a",       "LAUCN171210000000005A"),
    ("eci_marion_unemp_persons_a",     "LAUCN171210000000004A"),
    ("eci_marion_labor_force_a",       "LAUCN171210000000006A"),
    ("eci_marion_hs_grad_pct",         "HC01ESTVC1617121"),
    ("eci_marion_assoc_or_higher_pct", "S1501ACSTOTAL017121"),
    ("eci_moultrie_emp_persons_a",     "LAUCN171390000000005A"),
    ("eci_moultrie_unemp_persons_a",   "LAUCN171390000000004A"),
    ("eci_moultrie_labor_force_a",     "LAUCN171390000000006A"),
    ("eci_moultrie_hs_grad_pct",       "HC01ESTVC1617139"),
    ("eci_moultrie_assoc_or_higher_pct","S1501ACSTOTAL017139"),
    ("eci_richland_emp_persons_a",     "LAUCN171590000000005A"),
    ("eci_richland_unemp_persons_a",   "LAUCN171590000000004A"),
    ("eci_richland_labor_force_a",     "LAUCN171590000000006A"),
    ("eci_richland_hs_grad_pct",       "HC01ESTVC1617159"),
    ("eci_richland_assoc_or_higher_pct","S1501ACSTOTAL017159"),
    # ──── Charleston, IL / Coles County / LWA-23 panel (2026-05-28) ────
    # Parallel substrate for the /charleston public page (mirrors the
    # crb_jackson_* + crb_msa_* Jackson County panel above). Coles County
    # FIPS 17029. Mattoon Micropolitan SA = CBSA 31380 but Micropolitan
    # SAs don't carry the SMU* CES series MSAs do — so the wage / private-
    # service-jobs cards remain MSA-only (no Mattoon equivalent).
    # LWA-23 (East Central IL) admin: CEFS Economic Opportunity Corporation,
    # Effingham. 13-county footprint (Coles is one of 13). FRED IL-county
    # 2-letter abbrev for Coles is `COLE` (4-char + numeric suffix `3`
    # disambiguates from other IL counties starting with C).
    ("cle_coles_unemployment_rate",     "ILCOLE3URN"),               # monthly, 1990+
    ("cle_coles_labor_force",           "ILCOLE3LFN"),               # monthly, 1990+
    ("cle_coles_population",            "ILCOLE3POP"),               # annual, 1970+
    ("cle_coles_personal_income",       "PI17029"),                  # annual, 1969+
    ("cle_coles_real_gdp",              "REALGDPALL17029"),          # annual, 2001+
    ("cle_coles_median_hh_income",      "MHIIL17029A052NCEN"),       # annual, 1989+
    ("cle_coles_snap_recipients",       "CBR17029ILA647NCEN"),       # annual, 1989+
    ("cle_coles_poverty_universe",      "PUAAIL17029A647NCEN"),      # annual, 1998+
    ("cle_coles_single_parent_pct",     "S1101SPHOUSE017029"),       # annual, 2009+
    # Realtor.com housing series (Coles County; Mattoon Micro CBSA-level
    # data not published — county is the smallest geography Realtor.com
    # publishes for non-metro areas)
    ("cle_coles_housing_median_listing",       "MEDLISPRI17029"),     # monthly, 2016+
    ("cle_coles_housing_new_listings",         "NEWLISCOU17029"),     # monthly, 2017+
    ("cle_coles_housing_new_listings_mom",     "NEWLISCOUMM17029"),   # monthly, 2017+
    # Illinois state context (already have phci_il via 50-state panel)
    ("il_unemployment_rate",            "ILUR"),                     # monthly, 1976+
    ("il_nonfarm_payrolls",             "ILNA"),                     # monthly, 1990+
    # ── Philadelphia Fed state coincident indices — 50 USPS states,
    # monthly, 1979→present. Substrate for the derived
    # ``sos_state_diffusion`` series (Crone/Clayton-Matthews 2005
    # 3-month span) consumed by the Sentinel graduated Bear Score Lab
    # candidate. Live-probed 2026-05-21: all 50 series valid, frequency
    # Monthly, observation_start 1979-01-01 (TX 1979-04-01). No new
    # provider (FRED existing); license-free.
    ("phci_al", "ALPHCI"), ("phci_ak", "AKPHCI"), ("phci_az", "AZPHCI"),
    ("phci_ar", "ARPHCI"), ("phci_ca", "CAPHCI"), ("phci_co", "COPHCI"),
    ("phci_ct", "CTPHCI"), ("phci_de", "DEPHCI"), ("phci_fl", "FLPHCI"),
    ("phci_ga", "GAPHCI"), ("phci_hi", "HIPHCI"), ("phci_id", "IDPHCI"),
    ("phci_il", "ILPHCI"), ("phci_in", "INPHCI"), ("phci_ia", "IAPHCI"),
    ("phci_ks", "KSPHCI"), ("phci_ky", "KYPHCI"), ("phci_la", "LAPHCI"),
    ("phci_me", "MEPHCI"), ("phci_md", "MDPHCI"), ("phci_ma", "MAPHCI"),
    ("phci_mi", "MIPHCI"), ("phci_mn", "MNPHCI"), ("phci_ms", "MSPHCI"),
    ("phci_mo", "MOPHCI"), ("phci_mt", "MTPHCI"), ("phci_ne", "NEPHCI"),
    ("phci_nv", "NVPHCI"), ("phci_nh", "NHPHCI"), ("phci_nj", "NJPHCI"),
    ("phci_nm", "NMPHCI"), ("phci_ny", "NYPHCI"), ("phci_nc", "NCPHCI"),
    ("phci_nd", "NDPHCI"), ("phci_oh", "OHPHCI"), ("phci_ok", "OKPHCI"),
    ("phci_or", "ORPHCI"), ("phci_pa", "PAPHCI"), ("phci_ri", "RIPHCI"),
    ("phci_sc", "SCPHCI"), ("phci_sd", "SDPHCI"), ("phci_tn", "TNPHCI"),
    ("phci_tx", "TXPHCI"), ("phci_ut", "UTPHCI"), ("phci_vt", "VTPHCI"),
    ("phci_va", "VAPHCI"), ("phci_wa", "WAPHCI"), ("phci_wv", "WVPHCI"),
    ("phci_wi", "WIPHCI"), ("phci_wy", "WYPHCI"),
    # NOTE 2026-05-27: tried to add DC (DCPHCI) but the Philadelphia Fed
    # state coincident-index series does NOT cover the District of
    # Columbia — FRED returns 400 "series does not exist". The PHCI
    # set is 50 USPS states only.
)
"""(canonical_name, FRED series_id) pairs — the platform's vocabulary
on the left, FRED's identifier on the right. Adding a new indicator
means appending one tuple here plus a glossary entry."""


def _parse_observation_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except Exception:
        return None


def _parse_value(raw: Any) -> Decimal | None:
    """FRED encodes missing values as ``"."``; reject those upstream of
    the DB CHECK constraint."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s == ".":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


class FREDAdapter:
    """Pulls macro time-series from FRED.

    Args:
        api_key: FRED API key. Defaults to ``FRED_API_KEY`` env var.
            Raises ``DataProviderOutage`` at construction if missing —
            fail-fast per the adapter readiness checklist.
        client: optional pre-built ``httpx.AsyncClient`` for tests.
        timeout: per-request timeout in seconds. Defaults to 30.
        inter_request_sleep_s: courtesy delay between requests.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        inter_request_sleep_s: float = _INTER_REQUEST_SLEEP_S,
    ) -> None:
        key = api_key or os.getenv(_API_KEY_ENV)
        if not key:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} adapter requires {_API_KEY_ENV} env var "
                "(free signup at https://fred.stlouisfed.org/docs/api/api_key.html)"
            )
        self._api_key = key
        self._client = client
        self._timeout = timeout
        self._inter_sleep = inter_request_sleep_s
        self._owned_client = client is None

    async def __aenter__(self) -> FREDAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
            self._owned_client = True
        return self._client

    # ── Public API ─────────────────────────────────────────────────────
    async def get_observations(
        self,
        series_id: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch observations for a single FRED series.

        Returns a list of ``{"date": date, "value": Decimal | None}``.
        Missing observations (FRED's ``.``) are filtered out before
        return — the loader doesn't need to repeat the check.

        Raises ``DataProviderOutage`` on permanent failure
        (4xx-not-429, exhausted retries).
        """
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        if start is not None:
            params["observation_start"] = start.isoformat()
        if end is not None:
            params["observation_end"] = end.isoformat()
        try:
            payload = await self._fetch_raw("/series/observations", params)
        except DataProviderOutage:
            raise
        except httpx.HTTPError as exc:
            raise DataProviderOutage(
                f"{_PROVIDER_NAME} get_observations({series_id}) unreachable: {exc}"
            ) from exc

        raw_obs = (payload or {}).get("observations", []) or []
        rows: list[dict[str, Any]] = []
        for o in raw_obs:
            d = _parse_observation_date(str(o.get("date", "")))
            v = _parse_value(o.get("value"))
            if d is None or v is None:
                continue
            rows.append({"date": d, "value": v})
        logger.info(
            f"{_PROVIDER_NAME}.observations_fetched",
            series_id=series_id,
            count_total=len(raw_obs),
            count_valid=len(rows),
        )
        return rows

    async def latest_published(self, series_id: str) -> date | None:
        """Cheap publication-availability probe (#165 facet 4): GET
        ``/fred/series?series_id=X`` and read ``observation_end`` — the
        date of FRED's latest observation for that series — WITHOUT
        downloading any actual observations. Lets the self-heal
        orchestrator distinguish "we are stale (our defect → heal)"
        from "FRED simply hasn't published a newer observation yet
        (vendor-late → quiet, no churn)" per the no-lazy-vendor-blame
        rule.

        Returns ``None`` if the response is malformed or the probe
        fails — caller falls back to the strict (assume-behind)
        behaviour, never silently green.

        Per-series rather than the AAII single-HEAD pattern because
        FRED is a multi-series feed (one ``observation_end`` per
        series). The feed-level probe in
        ``tpcore.feeds.publication`` composes per-series answers into
        a conservative "feed has nothing newer" verdict (MIN across
        series).
        """
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        try:
            payload = await self._fetch_raw("/series", params)
        except (DataProviderOutage, httpx.HTTPError):
            return None
        seriess = (payload or {}).get("seriess", []) or []
        if not seriess:
            return None
        raw_end = seriess[0].get("observation_end")
        if not raw_end:
            return None
        return _parse_observation_date(str(raw_end))

    async def get_all_indicators(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch observations for every indicator in :data:`INDICATOR_SERIES`.

        Returns ``{canonical_name: [{date, value}, ...]}``. Inter-series
        courtesy delay applied between calls (well under FRED's 120/min
        cap). Failures on a single series log a warning and continue —
        a partial result is more useful than nothing.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for name, series_id in INDICATOR_SERIES:
            try:
                out[name] = await self.get_observations(
                    series_id, start=start, end=end,
                )
            except DataProviderOutage as exc:
                logger.warning(
                    f"{_PROVIDER_NAME}.series_failed",
                    series_id=series_id, name=name, error=str(exc),
                )
                out[name] = []
            await asyncio.sleep(self._inter_sleep)
        return out

    # ── Internal: HTTP layer ──────────────────────────────────────────
    @with_retry(max_attempts=4, backoff_base_sec=1.0, backoff_cap_sec=30.0)
    async def _fetch_raw(self, path: str, params: dict[str, Any]) -> Any:
        client = await self._ensure_client()
        resp = await client.get(path, params=params)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"{_PROVIDER_NAME} {path} → {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        # 4xx-not-429 → permanent. Raise DataProviderOutage with the
        # provider's error message so the operator can diagnose
        # (invalid key, bad series_id, etc.).
        raise DataProviderOutage(
            f"{_PROVIDER_NAME} {path} returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )


__all__ = ["FREDAdapter", "INDICATOR_SERIES"]
