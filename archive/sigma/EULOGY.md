# Sigma — Eulogy (archived 2026-05-16)

Sigma was the platform's first engine: a daily-timeframe range-scalping
strategy on Bollinger Bands. It is retired here after exhausting its
last credible hypothesis. The code is preserved under `archive/sigma/`
for provenance — it is no longer imported, scheduled, gated, or swept.

## Cause of death

Sigma had **two** independent, honestly-run shots at the credibility
gate (DSR ≥ 0.95 **and** credibility ≥ 60), and failed both:

1. **Static Bollinger range-fade** (the original design: lower-band
   fade with ADX/CHOP gates + SPY-regime suppressor). Last canonical
   search topped out at OOS **+1.150** — FAILED the DSR gate. The
   academic prior was already against it: Fang & Jacobsen document
   Bollinger-band mean-reversion edge decaying to insignificance
   post-publication. The static form was a decayed factor.

2. **Failed-expansion redesign** (#168, this session — the steelman):
   volatility-compression → attempted breakout → *failed* breakout
   entry → VWAP/value-mid exit, with VIX>25 and Fear&Greed
   Extreme-Fear (<25) macro suppressors. This was the research-backed
   reformulation, not a parameter tweak. It was run end-to-end through
   the **canonical** `scripts/search_parameters.py` pipeline (no
   one-off script): random search, walk-forward, held-back DSR.

   Verdict — **decisive FAILED**:
   - 50/50 trials negative held-back Sharpe (best **−0.1185**, mean
     **−2.55**, worst **−5.75**).
   - Credibility pinned at **45** every trial — below the 60 gate.
   - DSR **0.0000** — nowhere near the 0.95 bar.
   - Smoke confirmed the new signal produced real trades with VIX/F&G
     series loaded, so the FAILED verdict is a **true negative**, not
     a dead-signal artifact.

   Raw sweep output preserved alongside this file:
   `sigma_failed_expansion_search.csv` (51 lines incl. header).

   Methodological caveat (recorded honestly, not as an excuse): the
   platform only has clean data from ~2018→2026. To preserve a
   genuinely held-back 2020–2026 segment, the walk-forward collapsed
   to ~1 train/holdout window (2018–2019). With 50/50 negative and
   DSR 0.0000 this caveat is moot — there is no configuration of the
   WF split under which this result flips to a pass.

## Scoping caveat — what this archival does NOT adjudicate

This eulogy retires the **directional failed-expansion form of
Sigma**. It does **not** adjudicate the **sector-neutral residual
variant** that the Sigma Research Synthesis marks as the *next* step.

That variant is a **different engine**, not a Sigma rescue: it trades
the cross-sectional residual after removing sector beta, which is a
distinct signal-construction and distinct risk model. Reviving it
would be a new engine build (start from `tpcore/templates/
engine_template/`, follow `docs/superpowers/checklists/
engine_readiness.md`), not an un-archiving of this code. Nothing in
the failed-expansion result here is evidence for *or* against the
sector-neutral idea — they were never tested. Do not let "Sigma
failed" be read as "compression/residual mean-reversion is dead." It
means *this directional form, on this universe, under honest gates,
did not earn the right to trade.*

## What Sigma leaves behind (still in tpcore — not archived)

The reusable infrastructure Sigma pioneered stays in the shared
library and is used by the surviving engines:

- `tpcore/indicators/` — ADX, Bollinger Bands, CHOP.
- `tpcore/order_management/BaseOrderManager` — Tier-1/Tier-2 OCO path.
- `tpcore/order_ids` — the `sg_` prefix is **retained, historical-only**
  so any past Sigma paper orders remain attributable; it is never
  minted for new orders.
- The canonical search/WF/DSR pipeline and the FilterDiagnostics
  instrumentation pattern.

## Retirement checklist (all done 2026-05-16)

- `sigma/` → `archive/sigma/`; `run_sigma_search.sh`, the sigma-only
  `smoke_test.py`, and the sweep CSV moved alongside.
- Removed from `pyproject.toml` packages + testpaths.
- Removed from the engine sweep (`run_all_engines.sh`,
  `ops/platform_pipeline.py`), smoke roster (`run_smoke_test.sh`),
  search roster (`run_all_searches.sh`), the per-engine data gate
  (`tpcore.quality.validation.capital_gate.ENGINE_TABLES`), and
  tip-sheet engine registry.
- Every real `from sigma … / import sigma` outside `archive/` removed;
  `tpcore` tests duck-type `ExecutionDecision` locally (tpcore must
  never import an engine — the layering invariant, enforced by
  `tpcore/scripts/check_imports.py`).
- Docs updated: CLAUDE.md, MASTER_PLAN.md, glossary.md, TODO.md.

Sigma's role — proving the plug architecture and the credibility gate
actually have teeth — is complete. The gate worked: it refused to
graduate a strategy that had not earned it. That is the system
functioning as designed.
