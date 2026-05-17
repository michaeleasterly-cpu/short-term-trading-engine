# Adapter Contract-Population Sentinel (schema/contract-drift) — Design

**Status:** spec 2026-05-17 (DATA lane, **v2 — reframed**). Brainstorm
→ spec → **(this doc, v2)** → plan → phased subagent build. #186
candidate (6), the last genuinely-unbuilt agent of the "remaining
deterministic data agents" epic (cand (5) auditheal shipped
2026-05-17; (3)/(4) largely realized by #165).

**v2 reframe (2026-05-17).** v1 assumed the handler sees *raw vendor
records with vendor keys* to assert. Verified false across all 4
high-risk feeds: every adapter (`FREDAdapter`, `ApeWisdomAdapter`,
`FinraAdapter`, `IBorrowDeskAdapter`) parses the raw vendor payload
into a **typed model inside the adapter**; the handler only ever sees
normalized records (`rec.borrow_rate_pct`, `obs["value"]`, …).
`IBorrowDeskAdapter` *scrapes HTML* — there are no vendor "keys" at
all. So a key-set check at the handler boundary is the wrong
mechanism and the wrong layer. Operator decision: **reframe to an
adapter output-completeness assertion** — detect the *symptom*
(a vendor contract change makes a required output field come back
systematically empty) at the boundary we actually have.

**Place in the Escalation & Hardening Ladder (operator's name).**
Rung 1 = deterministic detectors + bounded repair (`tpcore/selfheal`,
`tpcore/auditheal`, **this sentinel**). Rung 2 = the Data Supervisor
(separate spec, follows this) — event-sourced per-source hold +
auto-clear, the structural twin of `ops/engine_supervisor.py` (DA-1).
Rung 3 = LLM triage (#187, advisory, later). This sentinel is a
**rung-1 detector**; it escalates exactly as `selfheal`/`auditheal`
do today (raise → `INGESTION_FAILED` → hard-stop, no
`DATA_OPERATIONS_COMPLETE`). §6 is the zero-rework seam to rung 2.

Operator decisions captured (brainstorm + v2 correction, 2026-05-17):
- Baseline = **declared per-feed SoT** (not prior-archive comparison).
- Fail action = **producer hard-stop (raise before the load) + a
  Step-4c audit check**.
- Launch scope = **registry for ALL CSV-first feeds (clockwork drift
  test); enforced on a bounded high-risk first set; the rest
  `guard_pending`** (the HealSpec #132 rollout precedent).
- Mechanism = **adapter output-completeness assertion** (symptom-level;
  v2), not a raw-vendor-key check.

## 1. Problem

`detect_shrinkage` catches a **row-count** collapse. Its blind spot —
candidate (6) — is a **vendor contract change** (a renamed/removed
vendor field) that the adapter absorbs with a silent `.get()` /
default, so the row count is normal, the archive header is *our*
fieldnames, and the table loads structurally-fine but
**semantically-empty** rows (e.g. every `borrow_rate_pct` is `None`
because the vendor renamed the field). Re-pulling cannot undo the
load. "Shrinkage's cousin", but invisible to row-count, header, and
freshness checks.

v1's hook (raw vendor keys at the handler) does not exist: adapters
normalize first. The detectable, honest signal at the boundary we
*do* have is the **symptom**: across a non-empty pull, a field the
handler depends on — and that a valid vendor record ALWAYS populates
— is null/empty in **every** record. That is unambiguous contract
drift (a single stray null is data noise; *systematic* emptiness is
the vendor no longer supplying it).

## 2. Design — declared required-field SoT + output-completeness assertion

**Hook point.** Inside each handler, **after** the adapter returns its
records and **before** the load / `write_archive`. Operates on the
adapter's normalized output (typed records or dicts) — uniform across
JSON adapters and the HTML-scrape adapter (no raw-payload access
needed).

**Declared contract SoT.** New frozen registry
`tpcore/ingestion/adapter_contract.py`:

```
class AdapterContract(BaseModel):  # frozen, extra=forbid
    feed: str                          # canonical feed/source name
    required_fields: frozenset[str]    # adapter-output fields that a
                                       # valid vendor record ALWAYS
                                       # populates (ticker, date, the
                                       # core measure). MUST exclude
                                       # legitimately-nullable fields
                                       # (e.g. finra short_interest_pct
                                       # = None when shares unavailable;
                                       # days_to_cover optional).
    accessor: Literal["attr", "key"]   # rec.<f> vs rec[<f>] — adapters
                                       # differ (typed model vs dict)
    guard_pending: bool = False        # contract declared but enforced
                                       # call not yet wired (tracked)
    evidence: str = ""                 # how required_fields was
                                       # derived (from the adapter +
                                       # handler, NEVER guessed)

ADAPTER_CONTRACTS: dict[str, AdapterContract]   # keyed by feed
```

Mirrors `HealSpec`/`RemediationSpec`/`FeedProfile`: declarative,
frozen, evidence-backed, one per feed.

**The guard.** A pure helper:

```
class AdapterContractDrift(RuntimeError): ...

def assert_contract_populated(
    feed: str, records: Sequence[object]
) -> None:
    """No-op if records is empty (an empty window is a freshness /
    coverage concern other checks own — raising here would false-
    positive). Otherwise, for each declared required_field: read it
    from every record (via the contract's accessor) and raise
    AdapterContractDrift if it is null/empty (None or "") in EVERY
    record. Rationale: a required field a valid vendor record always
    populates being absent across the WHOLE non-empty payload is
    unambiguous contract drift; a single stray null is tolerated
    noise. O(n) over the in-memory records, no I/O."""
```

Drift = a declared `required_field` empty in **all** records of a
non-empty pull. Per-record optional/legitimately-null fields are NOT
`required_fields` (excluded by construction — see §9).

**Fail action (producer hard-stop).** An enforced handler calls
`assert_contract_populated(feed, records)` right after the adapter
returns, before mapping/`write_archive`/load. On drift it raises → the
stage fails → `INGESTION_FAILED` → the wrapper does NOT emit
`DATA_OPERATIONS_COMPLETE` → engines do not trade on a cycle whose
ingest silently lost a required field. **No auto-heal**: a vendor
contract change has no safe canonical repair (re-pull returns the
same shape; remapping is guesswork) — escalate-only by nature, like
the `_SOURCE_OF_TRUTH` / `healable=False` class.

## 3. Honest scope (what it does and does NOT catch)

It detects the **symptom** (a required field went systematically
empty), not every cause:

- ✅ Vendor renames/removes a field the adapter reads with a silent
  `.get()`/default → that output field all-null across the payload →
  caught.
- ✅ Vendor returns the right shape but blanks a column → caught.
- ❌ Vendor *adds*/reorders fields → not drift (handler ignores;
  benign — correctly not flagged).
- ❌ Vendor rename where the adapter has a working fallback
  (`a.get("x") or a.get("y")`) and still finds data → no symptom, not
  flagged (correctly — output is still correct).
- ❌ A field that is *legitimately* all-null in some valid windows
  (finra `short_interest_pct`) → deliberately NOT a `required_field`
  (would false-positive). §9 mandates evidence-deriving the
  exclusions.

Stated plainly so no one mistakes this for a complete vendor-schema
differ. It is the high-signal, low-false-positive **silent-corruption
tripwire** — exactly the case that matters (a load of structurally-OK
but semantically-empty rows).

## 4. Clockwork drift test (no silent gap)

`test_adapter_contract` asserts `set(ADAPTER_CONTRACTS)` == the set of
CSV-first feeds (enumerated in the test from every `write_archive`
call site in `tpcore/ingestion/handlers.py` + `scripts/ops.py`). A new
CSV-first feed **fails the build** until its `AdapterContract` is
declared — identical to the `selfheal`/`auditheal` registry-drift
tests. A second test pins the `guard_pending=True` set so it only
shrinks by a deliberate, reviewed change.

## 5. Launch rollout (bounded, honest, tracked)

- **Registry: ALL CSV-first feeds declared** day one (clockwork test
  enforces it).
- **Enforced guard wired into the bounded high-risk first set:** the
  silent-truncation / scrape-fragile class — `fred_macro`,
  `iborrowdesk_borrow_rates`, `finra_short_interest`,
  `apewisdom_social_sentiment` (`guard_pending=False`).
- **Every other CSV-first feed:** `guard_pending=True` — contract
  declared (coverage complete + visible) but the enforced call not yet
  wired. Tracked, build-pinned, surfaced by §6 — never a silent gap.
  Increments flip one flag + wire one call (HealSpec rollout pattern;
  zero mechanism edits).

## 6. Step-4c audit check (honest asymmetry — not a shrinkage twin)

The audit **cannot re-derive** this post-cycle (archives are
normalized; the adapter output is gone). So the
`audit_data_pipeline.py` known_knowns `adapter_contract` check is
deliberately **thinner** than `shrinkage_detector`:

1. **Registry coverage:** `ADAPTER_CONTRACTS` in lockstep with the
   CSV-first feed set (runtime mirror of the build-time drift test).
2. **Pending-gap visibility:** WARN finding enumerating
   `guard_pending=True` feeds (the honest "not yet covered" list).
3. **Recent-escalation surface:** FAIL if an unacknowledged
   `INGESTION_FAILED … reason=adapter_contract_drift` was recorded
   since the last clean cycle.

Defense-in-depth + visibility, NOT authoritative detection (the
producer raise is). Stated plainly — no fake re-derivation claim.

## 7. Forward integration with the Data Supervisor (Ladder rung 2)

This sentinel emits the rung-1 escalation shape (`INGESTION_FAILED`,
`reason=adapter_contract_drift`, the feed, the empty field(s)). When
the Data Supervisor spec lands, that escalation becomes one of its
event-sourced detectors → the per-source `DATA_HELD`/`DATA_CLEARED`
hold + auto-clear primitive. **No rework here** — the Supervisor is a
new *consumer* of an escalation this sentinel already emits.
Auto-clear for this class is conservative by construction (a clean
verified cycle for the feed *after* the contract/adapter was updated
to the vendor's new shape) — designed in the Supervisor spec.

## 8. Non-goals

- Not a raw-vendor-key differ (v1; impossible at the handler boundary
  / for the scrape adapter).
- Not prior-archive comparison (archive header is our normalized
  fieldnames — tautological).
- Not dtype/value-shape inference (noisy; YAGNI).
- Not auto-healing a vendor contract change (no safe repair;
  escalate-only).
- Not the Data Supervisor / hold-gate / auto-clear (Ladder rung 2 —
  its own spec; §7 is the seam).
- Not an LLM (Ladder rung 3, #187).
- Not enforcing every CSV-first handler at once (bounded first set;
  rest `guard_pending`, tracked).
- Operator interaction unchanged: internal hardening; touchpoints
  remain the ADD/REMOVE Data Feed Change Request + the weekly digest.

## 9. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `tpcore/ingestion/adapter_contract.py`: `AdapterContract` model + `ADAPTER_CONTRACTS` registry (ALL CSV-first feeds; high-risk set `guard_pending=False`, rest `True`) + `assert_contract_populated` + `AdapterContractDrift`. Clockwork tests: registry == CSV-first feed set; `guard_pending` set pinned; helper raises when a required field is all-null over a non-empty payload, no-ops on empty, tolerates a single stray null, supports both `attr` and `key` accessors. **Landed dark** (no handler calls it). |
| 2 | Wire `assert_contract_populated(feed, records)` into the 4 high-risk handlers at the post-adapter / pre-load boundary (`handle_macro_indicators`, `handle_iborrowdesk_borrow_rates`, `handle_finra_short_interest`, `handle_apewisdom_social_sentiment`). Per-handler test: a synthetic adapter output with a required field blanked in every record raises before any DB write; a normal payload (incl. one with a legitimately-null *non-required* field) passes. |
| 3 | `audit_data_pipeline.py` known_knowns `adapter_contract` check (§6: coverage + pending-gap WARN + recent-escalation FAIL). Tests with a fake `data_quality_log`/event surface. |
| 4 | Doc reconciliation: CLAUDE.md (producer-guard list + the Escalation & Hardening Ladder framing), TODO.md #186 cand (6) DONE, this spec Status → BUILT + build record. |

## 10. Open questions for the plan phase (resolve by READING code, not guessing — the auditheal-v1 discipline)

- **Exact CSV-first feed set** (clockwork denominator): enumerate
  directly from every `write_archive` call site in
  `tpcore/ingestion/handlers.py` + `scripts/ops.py`. (Preliminary
  read found ~12 archive sources incl. `fmp_fundamentals`,
  `alpaca_corporate_actions`, `alpaca_daily_bars`, `fred_macro`,
  `fred_macro_hist`, `fmp_earnings_events`, `greeks_max_pain`,
  `finnhub_insider_sentiment`, `apewisdom_social_sentiment`,
  `finra_short_interest`, `iborrowdesk_borrow_rates`,
  `aaii_sentiment` — the plan MUST re-derive, not trust this list.)
- **Per high-risk feed: `required_fields` + `accessor` + the
  legitimately-null EXCLUSIONS**, derived from the adapter + handler
  with an `evidence` string. Known constraints from the v2 read:
  finra `short_interest_pct`/`days_to_cover` are legitimately null →
  excluded; `rec.ticker`/`rec.settlement_date` always populated →
  included. fred output is `obs["date"]/obs["value"]` (`key`
  accessor) plus the `INDICATOR_SERIES` names present. apewisdom
  records expose attributes (`attr`: `rec.ticker`, `rec.mentions`,
  `rec.rank`). iborrowdesk record `attr`: `rec.ticker`, `rec.date`,
  `rec.borrow_rate_pct`. The plan must confirm each from source.
- **Records object in scope at each enforced call site:** confirm the
  adapter-output collection (e.g. fred `per_indicator`, apewisdom
  `records`, finra `records`, iborrowdesk `rows`/`rec` loop) is
  available as a Sequence at the chosen pre-load point; if a handler
  only has it inside a generator/loop, name the minimal
  no-behavior-change local to expose it. (iborrowdesk builds `rows`
  tuple-by-tuple in a per-ticker loop — the contract check likely
  operates on the accumulated `rows`/the per-`rec` path; the plan
  specifies exactly where, per handler.)
- **Accessor uniformity:** `assert_contract_populated` must handle
  both attribute records and dict records via the contract's
  `accessor` field — the plan defines the exact read+empty test
  (`None` or `""`; for numerics, `None` only — `0`/`0.0` is a valid
  value, NOT empty).
