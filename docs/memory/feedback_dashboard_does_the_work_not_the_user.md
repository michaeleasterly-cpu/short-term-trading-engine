---
name: dashboard-does-the-work-not-the-user
description: "Action ladders on the public dashboard must lead with what the dashboard ALREADY does — only the truly human-only step (introductions, decisions, negotiations) should be assigned to the stakeholder"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

STANDING RULE: On the public dashboard pages (`/southern-illinois`, `/carbondale`, `/murphysboro`, `/market`), every action-ladder / "what the workforce board should do" / "next steps" framing must lead with what the dashboard ALREADY automates, ending with only the human-only residual step.

**Wrong (the framing operator caught 2026-05-27):**
> "Action ladder: the workforce board + Marion Chamber + Southern Illinois Business Alliance pull GD-OTS subaward export quarterly → identify out-of-region subs by NAICS → match against local-firm capability + SBA certification status → broker introductions..."

That delegates four steps to the user when the dashboard ALREADY does the first three (the supply-chain subaward integration auto-pulls GD-OTS subawards quarterly, identifies NAICS lanes, flags out-of-region candidates, and the KNOWN_SBA_STATUS lookup table marks the SBA certification status of named recipients).

**Right:**
> "Quarterly automation: the page pulls GD-OTS subawards by NAICS lane and flags out-of-region candidates above. **Your one step:** for the lanes with an out-of-region top sub-recipient + a local firm in the same NAICS code, broker the introduction between the prime's procurement team and the local firm. The data is already in your hand."

**Why:** Operator 2026-05-27: "you do the action ladders you dont give the user a job." A dashboard's value is automation. Handing the user a checklist of API queries / data joins / lookup work to do themselves fails the value-prop and reads as "we surfaced the problem; you fix the workflow." The dashboard's job is to surface the problem AND do the workflow up to the point a human has to make a judgment call or place a phone call.

**How to apply:**

1. **Before writing any action ladder**, audit what the page already automates. If a step in the ladder maps to an existing query / table / aggregation already on the page, don't put it in the ladder — point to it.
2. **Identify the human-only residual.** What's the one (or two) thing that can't be automated? Common residuals: introductions / outreach calls; negotiation; political alignment; final approval / vote. These are the only steps that belong on the user's plate.
3. **Frame as: "We did X, Y, Z (with links to the section showing the data). Your one step: A."** Not "the board pulls X, then identifies Y, then matches Z, then does A."
4. **Audit existing pages** — multiple action-ladder sections likely violate this rule. Rewrite each pass:
   - "Recruit second-tier primes" — page already lists top recipients; the user's only step is the contact
   - "CEJA clean-energy alignment" — page already cross-references awards against credentialing pipelines; user's step is the curriculum decision
   - "Coordinate with city pages" — page already cross-links; user's step is the human coordination meeting
5. **Same logic applies to any "what the workforce board can do" / "next steps" / "action ladder" / "use this to" / "this enables" framing.**

**Exception:** if the dashboard genuinely can't automate the step (e.g., a primary-source pull that requires login credentials the dashboard doesn't have, like HRSA HPSA county-level data behind an interactive tool), it's legitimate to assign that to the user — but say so explicitly ("the dashboard can't access this; you walk the tool"), don't disguise an automation gap as an action item.

Related: [[feedback_research_builder_persona]] (the build-mode is to ship working automation, not surface a checklist); [[feedback_no_judgment_headlines_on_public_dashboards]] (same data-first principle applied to action ladders — show the data, name the residual step).
