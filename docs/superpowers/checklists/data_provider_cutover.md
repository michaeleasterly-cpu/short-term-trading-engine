# Data Provider CUTOVER — automated (no operator approval)

Stage 4 of the Data Provider Lifecycle. **CUTOVER is automated and
deterministic.** Per the authoritative operator-interaction policy
(lifecycle spec §10), a provider swap for an *existing* feed is **not**
an operator decision — the operator approves only ADD (ONBOARD) and
REMOVE (RETIRE). This doc supersedes the earlier "operator-confirmed,
reviewed-PR" framing (PR #15, never merged).

## Why automated is correct (and safe)

A cutover only ever promotes a `FALLBACK`, and a `FALLBACK` is by
definition **parity-verified** (it passed EVALUATE —
`data_provider_evaluate.md` — proving it ≥ the incumbent on
coverage/freshness/accuracy). The parity gate already supplied the
human-equivalent judgement, so an autonomous swap cannot silently
degrade. The swap is also **reversible** (the demoted incumbent stays a
parity-verified FALLBACK). Reversible + gate-verified ⇒ automate.

## The mechanism (deterministic)

1. **Trigger:** an ACTIVE provider's per-feed validation/parity goes
   red **and** a parity-verified `FALLBACK` exists for that feed.
2. **Guard:** `tpcore.providers.plan_cutover(feed, fallback)` — pure,
   deterministic. Validates: target is a bound `FALLBACK`
   (parity-verified); exactly-one-ACTIVE preserved; incumbent demoted
   to `FALLBACK` (reversible) or `RETIRED` only via the separate
   RETIRE gate. A non-FALLBACK target is **blocked** (must pass
   EVALUATE first — skipping parity is the silent-degradation class).
3. **Apply:** the system applies the validated status change in the
   runtime binding state, re-validates the feed, and emits a
   `PROVIDER_CUTOVER` audit event. No PR, no human.
4. **Surface:** every cutover appears in the next **weekly digest**
   (`weekly_digest_runbook.md`) — the operator sees, weekly, every
   swap the system made without asking, with the chance to
   pressure-test it (verify-the-verifier).

## Operator's role

None, at cutover time. The operator's only levers are the
[Data Feed Change Request](data_feed_change_request.md) ADD/REMOVE
operations and the weekly-digest ack. A `MODIFY → provider` change
request is routed here and executed automatically (done-receipt
returned), never queued for approval.

## Runtime-mutable status (implementation note)

Automated cutover requires binding *status* to be runtime-mutable —
the code `_BINDINGS` declares defaults + parity-verified fallbacks; the
live ACTIVE is resolved through a state overlay the cutover agent
flips (symmetric to how `ingestion_jobs` overlays config /
`application_log` is the bus). The pure `plan_cutover` guard is the
legality check the agent calls before applying. (Phase-5 build item;
the guard exists, the state-overlay + agent is the remaining wiring.)

## Reversibility boundary (the honest caveat)

CUTOVER is *config*-reversible (re-promote the demoted FALLBACK). It is
**not consequence-reversible**: trades made on a provider's data while
it was ACTIVE stand. The defenses for that are upstream — the parity
gate (a FALLBACK is proven ≥ incumbent before it can ever be
promoted) and the weekly digest's adversarial "most likely silently
wrong" slot. RETIRE (true removal) is the only irreversible operation
and stays operator-gated.
