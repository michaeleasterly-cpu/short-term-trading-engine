---
name: always-use-iso-standards
description: "Operator standing rule 2026-05-23: 'always go with standards such as ISO'. For any identifier scheme, check digit, country/currency/date format, protocol, or specification choice — pick the documented ISO/IEC/IETF/RFC/W3C/OMG standard over a custom one. Inventing our own buys nothing and creates an interop tax forever."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Standing rule (operator 2026-05-23):** *"always go with standards such as ISO"*.

For ANY technical decision involving identifiers, formats, protocols, encodings, or interchange — the standard-body answer wins over a custom one. ISO is the canonical example; the same principle applies to IEC, IETF, IEEE, ANSI, W3C, OASIS, OMG, NIST.

## Why (failure-derived 2026-05-23)

During the v2.2 referential-integrity smart-key design, I framed the check-digit decision as "ISO 7064 Mod-97-10 (LEI precedent) vs roll our own". The operator collapsed it to: *"always go with standards such as ISO"* — making it a standing rule, not a per-decision call.

The "roll our own" option buys nothing:
- A standard's wheel is already invented, debugged, ported to every language, validated across decades
- Custom = interop tax forever (every future tooling/library has to learn our flavor)
- Custom = silent compatibility bugs when someone else's system expects standard semantics
- Custom = legitimate code-review red flag ("why not the standard?")

## How to apply

Before ANY technical decision involving:
- **Identifier schemes** — pick the closest applicable ISO/industry standard FIRST (ISIN, LEI, FIGI, CUSIP, VIN, container, ISBN). Build custom only if no standard covers the use case.
- **Check digits** — ISO 7064 family (Mod-97-10, Mod-11-10, Mod-37-2). Luhn is older but still ISO/IEC 7812. Don't invent new ones.
- **Country codes** — ISO 3166-1 alpha-2 (always; never custom abbreviations)
- **Currency codes** — ISO 4217 (always)
- **Date/time formats** — ISO 8601 (always)
- **Language codes** — ISO 639
- **Floating-point** — IEEE 754
- **Character encoding** — UTF-8 (Unicode standard)
- **Protocols** — RFC over custom (HTTP, TCP, gRPC, WebSocket, etc.)
- **API patterns** — OpenAPI 3.x, JSON:API, GraphQL
- **Security** — OWASP, NIST SP 800-series, FIPS

Default: standards FIRST, custom only with a documented "why no standard fits" rationale.

## Where this overrides custom temptations

- Smart-key check digit → ISO 7064 (not Luhn, not custom)
- Country segment in any identifier → ISO 3166-1 alpha-2 (not "USA"/"UK"/etc.)
- Date columns → ISO 8601 / `timestamptz` / `date` (not custom string formats)
- Time-series intervals → ISO 8601 duration (not custom encoding)
- Currency amounts → ISO 4217 + numeric (not custom enum)
- Cross-vendor identity → industry standards (FIGI, LEI, ISIN, CUSIP) over custom mapping tables

## Related

- [[authoritative-docs-override-claudemd]] — official docs win over CLAUDE.md on technical conflicts (this rule is the standards-equivalent)
- [[use-official-docs]] — fetch the current spec, capture the doc reference
- [[ask-expert-then-execute]] — the expert dispatches should always cite the relevant standard
- TKR-13 smart-key uses ISO 7064 Mod-97-10 + ISO 3166-1 alpha-2 + (implicitly) Crockford base32 — this rule is the principle behind those choices
