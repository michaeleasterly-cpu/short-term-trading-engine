---
name: authoritative-docs-override-claudemd
description: "Standing precedence rule (operator 2026-05-19): authoritative external docs + Python/community best practice OVERRIDE CLAUDE.md on conflict; update CLAUDE.md to match"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 2daba0e7-4abc-478f-b193-dae66fcbcce7
---

**Operator directive (2026-05-19, verbatim intent):** *"i dont care
what claude.md says, if the python best practice is different you use
best practice according to the python documentation and best
practices... you can update claude md to match that"* + *"also follow
the claude documentation, railway documentation, supabase
documentation or any other authoritative document that is used as a
reference... best practice from the community is also an option."*

**Rule (precedence, highest→lowest) for ENGINEERING/TECHNICAL
decisions:**
1. Authoritative external docs for the tool in play — Python official
   docs, Anthropic/Claude docs, Railway docs, Supabase docs, the
   library/SDK's own current docs (prefer context7 MCP / fetched
   official sources, per [[feedback_use_official_docs]]).
2. Established Python / community best practice.
3. CLAUDE.md / STYLE_GUIDE.md project conventions.

When (1) or (2) **conflicts** with (3): **follow (1)/(2)** and
**update CLAUDE.md (and STYLE_GUIDE.md) to match** — as its own
clean, gated docs change (or fold into the #252 docs-reconciliation),
citing the authoritative source. CLAUDE.md is no longer an absolute
override for technical conventions; it must track best practice, not
fossilize against it.

**Why:** the operator wants the codebase to follow real best practice,
not drift on stale project lore. (Note: this is about
technical/engineering conventions. Operator *workflow/safety*
directives — live-money invariants, never-fail-open, the
subagent-driven pipeline, never-mask, cross-session rules — are NOT
"CLAUDE.md technical convention"; they still stand and are governed by
the standing-persona / operating-identity memories.)

**How to apply:** (a) before citing a CLAUDE.md rule as the reason for
a decision, sanity-check it against the authoritative doc / best
practice; if they diverge, go with best practice and flag the
CLAUDE.md line for update. (b) Reviewers/implementers: frame findings
as best-practice conformance first, CLAUDE.md second. (c) Genuine
conflicts found → add a tracked CLAUDE.md-reconciliation item (cite
the source). (d) Most existing CLAUDE.md technical rules (UTC/DTZ,
no-private-access/SLF, asyncpg pooler, etc.) ALREADY equal best
practice — this rule resolves the rare divergence, it does not
invalidate the aligned majority. Pairs with
[[feedback_use_official_docs]], [[feedback_operating_identity_for_this_system]].
