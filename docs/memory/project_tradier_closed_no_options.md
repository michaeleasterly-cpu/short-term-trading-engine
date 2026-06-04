---
name: project_tradier_closed_no_options
description: "Tradier account CLOSED 2026-06-03 — decommission adapter, drop tradier_options_chains, no options data/engines, Tradier no longer a price fallback."
metadata: 
  node_type: memory
  type: project
  originSessionId: 6e6788c1-ed3f-4f00-b0a7-f58ee0eba1ab
---

The operator's Tradier account was closed 2026-06-03. Data-layer consequences:

- **Decommission the Tradier adapter** — DFCR change (never hand-edit `tpcore/providers.py`).
- **`tradier_options_chains` → DROP** (113k frozen rows, no consumer). `options_max_pain`/greeks already RETIRED 2026-06-01.
- **No options data → no options-based engines or features.**
- Tradier was the *secondary daily-price fallback*; the price roster is now **FMP primary, Alpaca IEX/SIP fallback** only — no Tradier price leg.

**Why:** removes options entirely from scope and trims the source roster; directly shapes the data-layer rebuild target.

**How to apply:** exclude Tradier + options from the rebuilt source roster and schema; reflected in [[reference_data_layer_index]] (DATABASE_AND_DATAFLOW.md §0 source roster). Part of [[project_data_layer_rebuild_arc]].
