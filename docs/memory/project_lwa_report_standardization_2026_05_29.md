---
name: lwa-report-standardization-2026-05-29
description: LWA-23 + LWA-25 dashboards standardized to same 19-section trajectory with parallel town context scores + DCEO wage clearance + Known Limits
metadata: 
  node_type: memory
  type: project
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

LWA-23 (East Central IL · /east-central-illinois) and LWA-25 (Southern IL · /southern-illinois) both follow the standardized 19-section numbered report trajectory as of 2026-05-29.

**Why:** Operator directive 2026-05-28/29 — "both regional dashboards should read like the same standardized report product, with different facts and different regional strategy." Before standardization, LWA-23 used inline numbered §01-§24 sections and LWA-25 used block-based component layout without numbering; same content, different shapes.

**How to apply:** When adding a NEW regional dashboard for a different IL workforce area, follow the same 19-section flow (01 Executive verdict → 02 Root causes → 03 Theory of change → 04 County strategy matrix → 05 Labor evidence → 06 Industry mix → 07 Federal awards → 08 Town context score → 09 Mobility → 10 Childcare → 11 Housing → 12 Healthcare → 13 Wage benchmark → 14 Training ROI → 15 DCEO occupations → 16 PIRL accountability → 17 Anchor attraction → 18 Action ladder → 19 Methodology + Known Limits). Use the `SectionHeader` helper component on the LWA-25 page as the model.

**Key identifiers (verified 2026-05-29):**
- LWA-23 = DCEO Local Area ID **17115** = Lake Land College fiscal agent + CEFS Economic Opportunity Corp operator (Effingham IL). EDR 7 (Southeastern Regional Data Packet).
- LWA-25 = DCEO Local Area ID **17125** = Man-Tra-Con Corp operator (Marion IL). EDR 8 (Southern Regional Data Packet). NOT EDR 9 — EDR 9 is Southwestern / Metro East / LWA-22.
- The two regions have DIFFERENT theses, NOT interchangeable: LWA-23 = participation-recovery-first (driven by 17-20% disability + carceral economy); LWA-25 = anchor-concentration-at-risk + structural gateways (GD-OTS 95.6% concentration + SIU decline + gateway barriers).

**Components / patterns landed:**
- Town context scores: 4-dimension within-set min-max composite (Safety / Participation / Health / Housing) with sub-component severe-flag visibility (Health splits into Disability + Age 65+/median age; each can trigger flag below 25 even when dimension mean is above) — see [[dimension-scope-limit-pattern]]
- IL DCEO In-Demand Occupations table with 1A+2C wage clearance (Strong ladder / Viable single-adult / Low-wage trap / Missing wage data); IL statewide MIT LWC $23.56 / $40.41 used in DCEO tables. LWA-25 also retains Jackson Co. $18.95 / $46.76 in TrainingAlignmentSection — keep both with explicit cross-reference between them.
- Known Limits table with closure classes: CLOSED / PARTIALLY CLOSED / COUNTY_PROXY_BY_DESIGN / DIMENSION_SCOPE_LIMIT / TIME-WINDOWED / PUBLIC_IDENTIFIER_ONLY / NOT_YET_PUBLISHED.

**PIRL/WIOA data state (verified 2026-05-28):** IL DCEO PY24 narrative is the authoritative public source for LWIA PY24+PY25 negotiated targets (LWIA-23 pp.14-17; LWIA-25 pp.14-18). LWIA-specific actuals NOT publicly available until early-2026 DCEO release. PY22 actuals for LWIA-23 in CEFS planning packet only; LWIA-25 PY22 actuals not located in any public source.

**Public-routing reminder:** all regional + city dashboards must be in `src/middleware.ts` matcher exclude list — see [[nextauth-public-route-pattern]].
