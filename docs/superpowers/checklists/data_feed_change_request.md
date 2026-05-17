# Data Feed Change Request — the operator's single structured touchpoint

This is **the** way to add, remove, or modify a data feed / derived
datum. The operator does **not** hand-edit `_BINDINGS`, `FEED_PROFILES`,
the HealSpec registry, or the audit list — that is exactly how the
system gets broken (the Sigma/FRED ad-hoc retirements proved it). You
fill in the block below and feed it in; the system parses it, routes it
through the Data Provider Lifecycle gates, **prepares and validates the
exact change**, and hands you back either a binary **APPROVE? (y/n)**
on a proven-consistent diff, or — for the automated operations — a
done-receipt with the audit reference.

> **Operator-interaction policy (authoritative).** You approve **only**
> two things: **ADD** a feed/derived datum (ONBOARD) and **REMOVE** one
> (RETIRE). Everything else — provider CUTOVER for an existing feed,
> EVALUATE parity, self-heal, validation — is **automated,
> deterministically, with no operator approval**. This supersedes any
> earlier "CUTOVER is operator-confirmed" wording.

## The request block (copy, fill, feed in)

```
DATA FEED CHANGE REQUEST
operation:   ADD | REMOVE | MODIFY        # exactly one
feed:        <logical feed name>          # FeedProfile/HealSpec.source vocabulary
# ── ADD only ──────────────────────────────────────────────────────
kind:        external | derived           # external provider, or computed from other feeds
provider:    <provider id>                # e.g. alpaca, fred, fmp ; "internal" for derived
adapter:     <importable dotted path>     # the ingest entrypoint
derived_from: [<feed>, ...]               # derived only — upstream feeds
need:        <one line: why this feed exists / which engine consumes it>
cadence:     <expected refresh cadence + vendor publish day if any>
# ── REMOVE only ───────────────────────────────────────────────────
disposition: delete | replace_with:<provider>   # true removal, or cut to a fallback
reason:      <one line: why it is being retired>
# ── MODIFY only ───────────────────────────────────────────────────
change:      provider:<new_provider> | cadence:<n>d | threshold:<check>=<value>
reason:      <one line>
```

## What the system does with each (deterministic routing)

| operation | gate(s) it routes through | needs operator y/n? | what you get back |
|---|---|---|---|
| **ADD** | ONBOARD (`adapter_readiness.md` 6-stage contract) → if it's an alternative provider for an existing feed, EVALUATE (`data_provider_evaluate.md` parity gate) | **YES** — structural scope change | a prepared diff (the exact `ProviderBinding`+`FeedProfile`+`HealSpec`+audit additions) + a green validation run → **APPROVE? (y/n)** |
| **REMOVE** | RETIRE (`data_provider_retire.md` — CSV-archive provenance + 3-way-atomic removal) | **YES** — structural scope change | a prepared diff (the exact 3-way removals, archive path) + a green validation run → **APPROVE? (y/n)** |
| **MODIFY → provider** | CUTOVER (`plan_cutover` guard; only a parity-verified FALLBACK is eligible) | **NO — automated** | a done-receipt: the swap the system validated + applied + `PROVIDER_CUTOVER` audit event |
| **MODIFY → cadence/threshold** | config change to the relevant SoT (FeedProfile/check), validated | **NO — automated** | a done-receipt + the validated change + audit event |

Rules the system enforces on every request (so you cannot fuck it up):
- The prepared diff must pass **every** invariant test before you are
  asked to approve: registry drift, exactly-one-ACTIVE,
  3-way-retire consistency, the per-feed validation. A request that
  cannot produce a consistent diff is **rejected with the reason** —
  never handed to you to "force".
- ADD/REMOVE are the *only* approvals. If a request reduces to a
  CUTOVER or a config change, the system does it automatically and
  tells you — it does not ask.
- A `MODIFY → provider` to a non-FALLBACK (un-parity-verified) provider
  is **blocked**, not approved-around: it must pass EVALUATE first
  (skipping parity is the silent-degradation class the lifecycle
  exists to prevent).
- Every operation, automated or approved, emits an audit event and is
  surfaced in the **weekly digest** (`weekly_digest_runbook.md`) — the
  state-comprehension floor. You will see, weekly, every cutover and
  config change the system made without asking.

## Why this shape

Minimizing operator interaction is not the goal; minimizing
*opportunity for irreversible harm* is. The two operations that are
genuinely structural and irreversible-ish (a feed existing at all) are
the only ones you touch — and even then as a binary yes/no on a
system-prepared, system-validated diff, never as a hand-authored edit.
Everything reversible and gate-verified is automated. The weekly digest
keeps your mental model warm so the rare crisis is one you can still
reason about.

See: lifecycle spec `…/specs/2026-05-17-data-provider-lifecycle-design.md`,
plan `…/plans/2026-05-17-data-provider-lifecycle-plan.md`, and the
EVALUATE / RETIRE / cutover / weekly-digest docs in this directory.
