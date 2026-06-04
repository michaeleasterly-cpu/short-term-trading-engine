---
name: fact-check-operator-claims
description: "Operator's own lived-experience claims need primary-source verification before they hit a public-stakeholder page — same standard as expert verdicts, vendor claims, or anything else"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

STANDING RULE: Lived-experience signal from the operator is GOLD as a hypothesis-generating input — but it does not exempt the claim from primary-source verification before publication on any public stakeholder artifact (dashboard pages, briefing documents, public-facing reports).

**Why:** Operator 2026-05-27: "you need to fact check my ass." Triggered after multiple turns where I synthesized his lived account (river-barge culture, diesel-mechanic local market, auto-mechanic wage compression, Cora vs Sitran terminal routing) into prose that read as authoritative without me actually verifying any of it. He correctly flagged the gap. This is consistent with his other standing rules: "every claim backed by data, not anecdote" + "you need references for everything" + "i dont want a slander case" + the [[feedback_verify_expert_verdict_in_codebase_first]] principle (expert verdicts → verify before forward), which now extends to operator verdicts too.

**How to apply:**

1. **Capture lived signal as a HYPOTHESIS, not a finding.** When operator says "X happens in this region," log it as a verifiable claim — note the source ("primary source: operator account, regional lived experience") — and queue the verification.

2. **Match each lived claim to a primary source category before writing copy:**
   - Wage / employment claims → BLS OEWS by MSA + BLS QCEW
   - Injury / safety claims → BLS Injuries, Illnesses, Fatalities (IIF) by SOC
   - Industry-structure claims (who hires, who routes where) → company filings, SAM.gov, industry directories, state regulatory filings
   - Demographic / social claims (divorce rate, education attainment) → Census ACS, state vital records, peer-reviewed studies
   - Cultural / lifestyle claims that resist quantification → label "primary source: operator account, [region], [date]" on the page; never present as if from a quantitative source

3. **For claims that CAN'T be cleanly sourced** (e.g., divorce rates by occupation aren't published), present them on the page with the explicit attribution `primary source: operator account, regional lived experience` instead of inventing a stat or omitting the framing. Operator's lived signal is legitimate evidence; just label it for what it is.

4. **Before any of this lands on the page**: run the verification first. Don't write polished prose synthesizing lived claims, then verify after — that order anchors the operator on phrasing that may not survive the fact-check.

5. **Slander surface is the highest bar.** Any lived claim that names a specific company / person / institution negatively requires primary-source verification before the page mentions it. "Riverboat guys are a rough lot" is general enough not to slander; "Company X has a bad safety record" is specific enough to be defamatory if unsourced. Treat the bar accordingly.

Related: [[feedback_no_lazy_vendor_blame]] (same logic for vendor claims), [[feedback_investigate_dont_hand_wave_findings]] (don't adjective anomalies), [[feedback_no_shortcuts_100_pct]] (verify actual system state), [[feedback_verify_expert_verdict_in_codebase_first]] (verify expert before forward).
