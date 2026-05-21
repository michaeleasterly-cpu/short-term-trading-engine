---
name: no-lazy-vendor-blame
description: "Never attribute a data gap to \"vendor can't cover it\" without ticker-level evidence; SEC/EDGAR is authoritative and should be ~100% — a gap there is almost always our ingestion defect"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

A missing-data finding is **our ingestion defect until proven
otherwise**. "The vendor doesn't have it" / "vendor limitation" is not
a conclusion you may state without per-ticker evidence pulled directly
from the source.

**Why:** I floated "if the vendor structurally can't cover it,
recalibrate the threshold" for the catalyst/SEC coverage reds. The
operator (correctly) flagged this as lazy: SEC/EDGAR is authoritative,
legally-mandated, peer-reviewed regulatory data — every public company
*must* file Form 4 / 8-K, so EDGAR essentially has ~100% coverage. A
50-ticker SEC table is our incomplete backfill (per-run cap, never-run
historical bootstrap), NOT EDGAR missing data. Reaching for "vendor
limitation" shifts blame off our pipeline and is exactly the
lazy-developer move the operator despises.

**How to apply:**
- Default: gap = our fault. A backfill's job is to prove we *can* pull
  it; design the investigation to disprove our-defect, not to confirm
  vendor-blame.
- Authoritative/regulatory sources (SEC EDGAR, exchange data) should be
  treated as ~complete. A shortfall there is presumed an ingestion bug
  until a **ticker-level cross-check against the source itself** proves
  the records genuinely don't exist there.
- Derived/computed vendor products (e.g. FMP earnings-beats) can have
  legitimately variable per-ticker coverage, but the same rule holds:
  only call it a true vendor gap with explicit per-ticker evidence in
  hand, never as a hand-wave.
- Threshold recalibration is allowed ONLY after the our-gap hypothesis
  is empirically killed with evidence — never as the first/easy out.
- This is the data-specific sharpening of
  [[investigate-dont-hand-wave-findings]] and is bound by the
  100%/no-shortcuts standard [[no-shortcuts-100-pct]].
