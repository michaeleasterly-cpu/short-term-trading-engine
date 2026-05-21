---
name: use-official-docs-not-assumed-knowledge
description: "For ANY external API/SDK/library integration, fetch & follow the official current documentation — never implement from assumed/trained knowledge"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

When integrating any external API/SDK/library (Anthropic/Claude, a
vendor data API, any third-party client), **fetch the official current
documentation and implement against it — do NOT code from what you
think you know**.

**Why:** operator directive (2026-05-18, on the #187 Anthropic
integration): *"use claude documentation dont do what you think you
know."* Trained knowledge of SDKs/APIs is stale or wrong often enough
that in a live-money system an assumed request/response shape is a
real defect. This is the no-vendor-blame / verify-don't-assume
discipline ([[feedback_no_lazy_vendor_blame]],
[[feedback_investigate_dont_hand_wave_findings]]) applied to API
knowledge.

**How to apply:**
- Prefer the context7 MCP server for library/API docs (it exists for
  exactly this; prefer it over web search). Fall back to WebFetch of
  the official docs site if needed.
- Capture the doc reference (URL / version) in the spec or plan so the
  implementer builds against the same source, and the reviewer can
  verify request/response shape vs the official spec — not vs my
  assertion.
- Tests may mock the client, but the mocked request/response MUST
  match the current official API shape (a mock that encodes a guessed
  shape passes CI and fails in production).
- Applies to the Anthropic Messages API for the #187 LLM triage layer
  specifically, and to every future external integration generally.
