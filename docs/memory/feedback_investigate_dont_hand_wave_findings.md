---
name: investigate-dont-hand-wave-findings
description: "Never characterize an audit/anomaly finding as benign (\"weekday artifact\", \"expected noise\") without empirically checking the underlying data first."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

When an audit or anomaly heuristic flags something, **investigate it
empirically before characterizing it** — do not pre-emptively dismiss
it as benign in the report.

**Why:** On 2026-05-15 I described the audit's `row_velocity` −87%
finding as "likely a weekday/weekend artifact" in a summary, without
querying per-day counts. When the operator said "investigate the row
velocity drops," the real cause was a **91% daily_bars coverage
collapse** (6,956 of 7,669 tickers missing bars after 05-08) — a
genuine data incident, not noise. The same session also had the
operator push me to investigate `ingestion_jobs` (turned out a real
check-miscalibration) and `prices_daily_gaps` (250-ticker SPAC noise).
In every case the empirical dig changed the conclusion. Hand-waved
characterizations were wrong each time.

**How to apply:**
- A flagged finding gets a query, not an adjective. Pull the
  per-entity / per-day breakdown before saying "expected" or
  "artifact."
- Three outcomes are all common and only distinguishable by digging:
  (a) real incident (fix the data), (b) miscalibrated check (fix the
  check), (c) genuinely benign (say so — *with the evidence*).
- If summarizing a finding before investigating, label it explicitly
  as unverified ("not yet investigated") rather than guessing a cause.
- MAX(date)/recency checks are blind to coverage collapse — when a
  freshness check is green but a velocity/volume heuristic is red,
  trust the velocity signal until proven otherwise. Related:
  [[stream-long-running-output]], [[research-builder-persona]] (CI IS SHIP GATE / PROOF OF DONE).
