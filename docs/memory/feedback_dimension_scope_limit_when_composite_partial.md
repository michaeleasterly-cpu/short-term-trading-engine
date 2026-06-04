---
name: dimension-scope-limit-pattern
description: "When a composite-score dimension only captures partial reality (e.g., Housing = cost burden, not stock quality), document the scope limit explicitly + allow operator's lived signal to complement it"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 1ba8810f-bdd4-42cd-bc94-d926a6018c32
---

When a composite-score dimension is computed from a single proxy that doesn't capture the full concept the dimension name suggests, **explicitly document the scope limit** in a Known Limits row classified `DIMENSION_SCOPE_LIMIT` rather than silently letting the proxy stand in for the broader concept.

**Why:** Mt. Vernon (Jefferson Co, LWA-25) scored **100 on Housing** in the town context score because Jefferson has the lowest renter cost-burden share in LWA-25 (43.3%). The operator immediately flagged: "mt vernon has some old ass houses too" — the lived signal is correct, and the cost-burden composite literally cannot capture housing-stock vintage/age/quality. The fix isn't to add more inputs to the composite (mission creep); the fix is to **label the dimension accurately + document the scope limit explicitly** so a reader knows the 100 means "lowest cost burden in set" not "best housing overall."

**Sibling masking pattern (same fix family):** When a dimension is a mean of sub-components (Health = mean of Disability + Age65), either sub-component can collapse to 0 (min-max worst-in-set) while the dimension-level average masks it. Mt. Vernon Health: D 73 · A 22 → mean 48 looks healthy at the dimension level but Age 65+ is at worst-in-set. Fix: severe-dimension flag checks the COMBINED dimension AND each sub-component independently. After the fix, Mt. Vernon now correctly surfaces `⚠ Health (age 65+)` despite composite 81 Strong.

**How to apply:**
1. Rename column header to scope-accurate label: `Housing` → `Housing (cost burden only)`.
2. Add inline sub-component breakdown in cell display: `48 (D73 · A22)` instead of just `48`.
3. Update methodology paragraph to spell out what the dimension does AND does not capture.
4. Add a `DIMENSION_SCOPE_LIMIT` Known Limits row that explicitly names the gap + names the town where it shows up most starkly (e.g., "Mt. Vernon scores 100 on Housing because Jefferson has lowest rent burden — but Mt. Vernon's actual stock includes pre-1970 single-family; that's not captured by the composite, tracked here in Known Limits").
5. Extend severe-flag logic to check each sub-component independently when the dimension is a mean (use `else if (sub_score < 25)` so the dimension-level flag isn't doubled).

**When does this apply:** Any composite metric where a dimension's name implies a broader concept than its input data can support, AND the operator's lived knowledge contradicts the proxy result for a specific entity. The operator is dispositive in this scenario — they live in the region.

Related: [[lwa-report-standardization-2026-05-29]].
