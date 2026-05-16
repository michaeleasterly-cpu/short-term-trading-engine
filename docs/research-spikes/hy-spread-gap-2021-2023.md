# Research spike — recover BAMLH0A0HYM2 gap 2021-03-20 → 2023-05-14

Status: OPEN. Created 2026-05-16. Owner decision: ingest what we have
(1996–2021), keep BAA10Y, fill this gap "eventually."

## How to use this

Paste everything in the fenced block below into ChatGPT (or another
research assistant). It is self-contained. The goal is **evidence, not
suggestions** — exact URLs, access method, and sample values we can
cross-check, not "try Quandl."

---

```
I need to recover a specific, narrow slice of a financial time series.
Give me VERIFIED sources with exact URLs and access instructions — not
generic suggestions. If you are not sure a source actually has the data
for the exact dates, say so; do not guess.

SERIES: ICE BofA US High Yield Index Option-Adjusted Spread.
  - FRED series id: BAMLH0A0HYM2
  - Daily frequency, value is the OAS in PERCENT (e.g. 3.55 = 355 bp;
    2008 GFC peak ≈ 20%, 2020 COVID peak ≈ 10%).

EXACT NEED: daily observations for 2021-03-20 through 2023-05-14
inclusive (~26 months, ~550 business days). I do NOT need anything
outside that window.

WHAT IS ALREADY CONCLUSIVELY RULED OUT (do not re-suggest these):
  1. FRED API (api.stlouisfed.org): the series was permanently
     truncated to a rolling ~3-year window. It now starts 2023-05-15.
     Nothing before that is retrievable.
  2. ALFRED (FRED's archival/vintage service): TESTED with my API key
     across vintages 2024-01-01, 2025-06-01, 2026-01-01, 2026-04-01,
     2026-04-15 and the realtime_start/realtime_end form. The
     truncation was applied RETROACTIVELY across ALL vintages — every
     vintage returns earliest = 2023-05-16, zero pre-2023 rows. A
     2020-01-01 vintage returns "series does not exist in ALFRED."
     ALFRED is dead for this series (ICE BofA proprietary licensing).
  3. Nasdaq Data Link / Quandl FRED mirror: public API returns HTTP
     403 (bot block) without a paid key; the FRED database is a
     downstream FRED mirror that would inherit the same truncation.
  4. GitHub csaladenes/eco-archive BAMLH0A0HYM2.csv: genuine and
     accurate (verified: 2008 peak 21.82%, 2020 peak 10.87%) but it
     ENDS 2021-03-19 — it does not cover the gap.

WHAT I NEED FROM YOU:
  - Concrete sources that plausibly hold daily BAMLH0A0HYM2 (or the
    identical ICE BofA US HY OAS) for 2021-03-20..2023-05-14.
    Candidates to investigate and VERIFY (exist? free? exact dates?):
    other public GitHub/data archives that snapshotted FRED before the
    truncation; academic/research data mirrors (e.g. university econ
    data libraries); the Wayback Machine / archive.org snapshots of the
    FRED BAMLH0A0HYM2 page or its CSV download URL between 2021 and
    2023; data.world / Kaggle datasets mirroring FRED; broker/terminal
    exports (Bloomberg/Refinitiv) IF the data is the same ICE index.
  - For EACH candidate: the exact URL, whether access is free or
    paid/keyed, and whether you can confirm it covers the 2021-2023
    window specifically.
  - If a source is a proxy (not the exact ICE BofA US HY OAS), label it
    explicitly as a proxy and state the expected basis difference.

SANITY CHECK any candidate against known values (so we don't ingest
garbage): in this window HY OAS was roughly ~3.5–5% through 2021,
compressed to ~3% in early 2022, then widened to ~5–6% in mid/late
2022 (Fed hiking / 2022 drawdown), easing back toward ~4–4.5% by
early-mid 2023. A source whose 2022 values don't widen is wrong.

Deliver: a ranked list of verified sources with URLs + access method +
confirmed date coverage, or a clear statement that no free source
covers this exact window and the only path is a licensed terminal
export.
```

---

## Context for whoever picks this up (not for the prompt)

- Platform impact: `platform.macro_indicators` now has `hy_spread`
  1996-12-31→2021-03-19 (eco-archive) + 2023-05-15→2026-05-12 (FRED
  live). The 2021-03-20→2023-05-14 hole is **0 rows**.
- `credit_spread` (BAA10Y) remains the contiguous credit-stress series
  1996→present and is unaffected — it is the operational fallback.
- If a source is found: ingest via the canonical knob, no one-off —
  `python scripts/ops.py --stage macro_indicators --param
  hist_csv_path=<file> --param hist_indicator=hy_spread --force`
  (idempotent `ON CONFLICT DO NOTHING`; will only fill the missing
  dates, won't touch existing rows or `credit_spread`).
- Sentinel Bear Score still uses BAA10Y; switching it to `hy_spread`
  is deferred until the series is contiguous (a holey credit signal
  feeding the live Bear Score is a production-risk change — not done).
