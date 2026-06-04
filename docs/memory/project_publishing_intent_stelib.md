---
name: publishing-intent-stelib
description: "Operator 2026-05-23: plans to publish certain artifacts that aren't readily available elsewhere as a future thing. The `publishing/stelib/` carve-out (Apache, Alpha v0.1.0) is the first vehicle. Internal artifacts that look 'dormant' (zero internal callers) may actually be intentional outbound publishing targets — don't treat them as dead weight."
metadata: 
  node_type: memory
  type: project
  originSessionId: 87291947-e0b8-4be5-9ca9-a3730fae9c55
---

**Operator-stated 2026-05-23:** *"i plan to publish certain artifacts that are not readily available as a future thing"*.

The vehicle exists: `publishing/stelib/` — Apache-licensed Python package, v0.1.0 Alpha, package name `stelib`, description "Short-term trading engine library: risk governor, AAR, parity, backtest, lab, forensics, indicators, and order management primitives carved from the short-term-trading-engine monorepo."

Currently exported (per `stelib/` directory walk):
- `indicators/` — adx, fear_greed (4-component formula), chop, bbands
- `order_management/` — base manager, stale_order_cancel
- `lab/` — context, models, target
- `parity/` — harness, data_parity
- `calendar.py`, `order_ids.py`, errors/exceptions

**Critical detail:** `grep "from stelib\|import stelib"` returns ZERO across all internal engines / tpcore / scripts. stelib is NOT imported internally; it's purely outbound. The "dormant" internal-caller signal is by design — the package is built to be published, not consumed internally.

## What this means for design decisions

When evaluating whether to drop / consolidate / retire any artifact, **the "no internal consumers" signal is NOT sufficient** for the call. Additionally check:

1. **Is it under `publishing/stelib/`?** If yes, it's an OUTBOUND artifact; do not drop based on internal-only consumer analysis.
2. **Does it have publishing-quality novelty?** Operator's phrase "not readily available [elsewhere]" suggests stelib contents will lean toward derivations + patterns that AREN'T commercially available off-the-shelf. Likely future publishing candidates:
   - **4-component fear_greed** (vol/credit/momentum/safe_haven formula, pure-FRED inputs, no CNN scrape) — already in stelib
   - **sos_state_diffusion** (Crone/Clayton-Matthews 2005 50-state PHCI derivation) — unique synthesis
   - **TKR-14 smart-key encoding** (ISO 7064 check + Crockford base32 issuer hash) — novel identifier scheme
   - **n_trials accounting / sacred DSR/credibility gate** — disciplined-research framework
   - **Risk governor + capital-gate primitives** — production-tested risk patterns
   - **Parity harness** — provider-cutover invariant testing
   - **HealSpec / per-feed self-heal contract** — operational pattern

3. **Schema changes have a published-version blast radius.** If a public stelib release exposes a column / function / class signature, internal refactors that break it require a stelib version bump + migration notice. Plan accordingly when designing schema changes that touch published-surface modules.

## How this composes with other rules

- `[[verify-expert-verdict-in-codebase-first]]` — extend the verification: also check `publishing/stelib/` for the candidate-for-drop. Even if internal callers = 0, stelib export = "keep."
- `[[tpcore-reuse]]` — `tpcore/` patterns are reused INTERNALLY; `stelib/` patterns are reused EXTERNALLY (post-publish). Both are reuse vehicles.
- `[[no-lazy-vendor-blame]]` — when a stelib publishing candidate parallels something a vendor sells, the operator's value-prop is "ours is better / open / cheaper / unique" — preserve quality.

## Anti-pattern to avoid

Treating `publishing/stelib/` artifacts as "dormant code with no callers." That's the dependency map talking; the publishing intent talks differently. The whole point is that stelib has no internal callers — it's outbound.

## Likely next publishing artifacts to harden

When the operator says "publish these" — the leading candidates per the existing stelib carve-out + the novelty filter:

1. **fear_greed.py** — already in stelib; needs PyPI release + README
2. **TKR-14 encoder** (tpcore/identity/tkr14.py) — port to stelib once internally stable
3. **sos_state_diffusion** — port to stelib (currently in tpcore/fred/diffusion.py)
4. **HealSpec + adapter-readiness contract** — operational patterns worth open-sourcing

## Investigation TODO (next session)

- Check `publishing/stelib/PACKAGE_README.md` for the operator's stated publishing roadmap
- Diff stelib's `lab/` against tpcore's `lab/` — are there divergences that mean stelib is ahead/behind?
- Check `publishing/gist/` (sibling directory) — what's its purpose?

## Related

- `[[tpcore-reuse]]`, `[[verify-expert-verdict-in-codebase-first]]`, `[[no-lazy-vendor-blame]]`
- `project_macro_consumer_audit_2026_05_23` — this audit surfaced stelib as an outbound consumer of macro
