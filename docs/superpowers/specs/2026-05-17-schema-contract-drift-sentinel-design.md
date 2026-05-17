# Schema/Contract-Drift Sentinel — Design

**Status:** spec 2026-05-17 (DATA lane). Brainstorm → **spec (this
doc)** → plan → phased subagent build. #186 candidate (6), the last
genuinely-unbuilt agent of the "remaining deterministic data agents"
epic (candidate (5) auditheal shipped 2026-05-17; (3)/(4) largely
realized by #165).

**Place in the Escalation & Hardening Ladder (operator's name).** The
Ladder is the layered response to data-layer failure:

- **Rung 1 — deterministic detectors + bounded repair:** the
  validation suite + `tpcore/selfheal`, the cross-table audit +
  `tpcore/auditheal`, and **this sentinel**. Detect → bounded
  canonical repair (or, for a vendor contract change, no safe
  repair) → on exhaustion, escalate.
- **Rung 2 — Data Supervisor (separate spec, follows this):** the
  structural twin of `ops/engine_supervisor.py` (DA-1) — turns a
  rung-1 escalation into an event-sourced **per-source hold** +
  conservative **auto-clear**, instead of a blunt whole-cycle freeze.
- **Rung 3 — LLM triage (#187, later):** advisory only at the
  escalation boundary; never the mutating actor.

This sentinel is a **rung-1 detector**. It escalates exactly the way
`selfheal`/`auditheal` do today (raise → `INGESTION_FAILED` →
hard-stop, no `DATA_OPERATIONS_COMPLETE`). §6 records the forward
integration point so rung 2 consumes it with zero rework.

Operator decisions captured (brainstorm 2026-05-17):
- Drift baseline = **declared per-feed schema SoT** (not prior-archive
  comparison).
- Fail action = **producer hard-stop (raise before the load) + a
  Step-4c audit check**.
- Launch scope = **declared-contract registry for ALL CSV-first feeds
  (clockwork drift test); enforced guard on a bounded high-risk first
  set; the rest explicitly `guard_pending`** (the HealSpec #132
  per-source-rollout precedent).

## 1. Problem

`detect_shrinkage` (`tpcore/ingestion/csv_archive.py`) catches a
**row-count** collapse by comparing the new archive to the prior one.
Its blind spot — and candidate (6) — is a **vendor contract change**:
a renamed/removed vendor column. Verified mechanism: handlers map raw
vendor records into our normalized keys *before* `write_archive`
(e.g. `a.get("type") or a.get("action_type")`, then
`fieldnames=["ticker","action_type",…]`). When a vendor renames a
column, `record.get("old_col")` silently returns `None`, our
normalized row is structurally fine, the row count is fine, and the
archive header is *our* fieldnames — so the bad data passes shrinkage,
passes the header, loads, and silently corrupts the table. Re-pulling
cannot undo a load of mis-shaped rows. "Shrinkage's cousin", but
**not detectable the same way** (the archive header is normalized, so
an archive-vs-archive or archive-vs-contract header diff is
tautological).

## 2. Design — guard the raw-vendor-record boundary against a declared contract

**Hook point.** Inside each handler, at the **adapter→handler
raw-record boundary** — after the adapter returns raw vendor records,
**before** the handler maps them into our schema / `write_archive` /
load. This is the only point where the vendor's actual shape is still
visible.

**Declared contract SoT.** A new frozen registry
`tpcore/ingestion/vendor_schema.py`:

```
class VendorSchema(BaseModel):  # frozen, extra=forbid
    feed: str                      # canonical feed/source name
    required_keys: frozenset[str]  # raw vendor columns the handler
                                   # depends on (missing ⇒ drift)
    guard_pending: bool = False    # True ⇒ contract declared but the
                                   # enforced call is not yet wired
                                   # (honest rollout, tracked)
    evidence: str = ""             # how required_keys was determined
                                   # (no-vendor-blame: from the adapter,
                                   # not guessed)

VENDOR_SCHEMAS: dict[str, VendorSchema]   # keyed by feed
```

Mirrors `HealSpec` / `RemediationSpec` / `FeedProfile`: declarative,
frozen, evidence-backed, one entry per feed.

**The guard.** A pure helper:

```
class VendorSchemaDrift(RuntimeError): ...

def assert_vendor_schema(feed: str, raw_records: Sequence[Mapping]) -> None:
    """Raise VendorSchemaDrift if any declared required_key is absent
    from the raw vendor records. No-op when raw_records is empty (an
    empty pull is a freshness/coverage concern, not a contract change —
    shrinkage / freshness checks own that; raising here would false-
    positive on a legitimately empty window). Inspects a bounded
    sample (first N records) — a contract change is uniform across the
    payload, so a sample is sufficient and O(1)."""
```

Drift = **a declared required key missing** from the raw records.
Extra/new vendor keys are benign (the handler ignores them) and never
drift. No dtype/value inference (stringly CSV → noisy; YAGNI).

**Fail action (producer hard-stop).** A handler in the enforced set
calls `assert_vendor_schema(feed, raw_records)` before mapping/load.
On drift it raises → the stage fails → `INGESTION_FAILED` → the
wrapper does NOT emit `DATA_OPERATIONS_COMPLETE` → engines do not
trade on a cycle whose ingest hit an unrecognised vendor contract.
**No auto-heal**: a vendor contract change has no safe canonical
repair (re-pull returns the same new shape; blindly remapping is
guesswork) — it is escalate-only by nature, exactly like the
`healable=False` / `_SOURCE_OF_TRUTH` class.

## 3. Clockwork drift test (no silent gap)

`test_vendor_schema` asserts `set(VENDOR_SCHEMAS)` ==
the set of CSV-first feeds (the feeds whose handler calls
`write_archive`, enumerated in the test from
`tpcore/ingestion/handlers.py` + `scripts/ops.py`). A new CSV-first
feed **fails the build** until its `VendorSchema` is declared —
identical guarantee to the `selfheal`/`auditheal` registry-drift
tests. Plus: a test pins the `guard_pending=True` set so it can only
shrink by a deliberate, reviewed change (a feed silently regressing
to unguarded fails the build).

## 4. Launch rollout (bounded, honest, tracked)

- **Registry: ALL CSV-first feeds declared** from day one (the
  clockwork test enforces it).
- **Enforced guard wired into the bounded high-risk first set:** the
  silent-truncation / scrape-fragile class —
  `fred_macro`, `iborrowdesk_borrow_rates`, `finra_short_interest`,
  `apewisdom_social_sentiment`. These are the feeds where a silent
  vendor reshape is both most likely and most damaging.
- **Every other CSV-first feed:** `guard_pending=True` — contract
  declared (so coverage is complete and visible) but the enforced
  call not yet wired. Tracked, build-pinned, escalated as a known gap
  by §5 — never a silent uncovered feed. Subsequent increments flip
  `guard_pending=False` + wire the one call (the HealSpec `healable`
  rollout pattern: a one-line change per feed, zero mechanism edits).

## 5. Step-4c audit check (honest asymmetry — not a shrinkage twin)

Unlike `shrinkage_detector`, the Step-4c audit **cannot re-derive**
vendor-schema drift from disk archives (they store our normalized
header, not the vendor's). Pretending otherwise would be the exact
fake-green this codebase forbids. So the
`audit_data_pipeline.py` known_knowns `schema_drift` check is
deliberately **thinner**:

1. **Registry coverage:** `VENDOR_SCHEMAS` is in lockstep with the
   CSV-first feed set (the runtime mirror of the build-time drift
   test — catches a feed added on a branch that skipped CI).
2. **Pending-gap visibility:** emits a WARN finding enumerating the
   `guard_pending=True` feeds (the honest, visible "not yet covered"
   list — turns the rollout gap into a tracked dashboard signal, not
   an invisible hole).
3. **Recent-escalation surface:** FAIL if an unacknowledged
   `INGESTION_FAILED … reason=schema_drift` was recorded since the
   last clean cycle (defense-in-depth visibility; the producer raise
   is the authoritative stop).

This is defense-in-depth + visibility, NOT authoritative detection
(the producer hard-stop is). Stated plainly so no one mistakes the
audit check for a re-derivation.

## 6. Forward integration with the Data Supervisor (Ladder rung 2)

This sentinel emits the same escalation shape rung 1 already uses
(`INGESTION_FAILED` with `reason=schema_drift`, the feed, the missing
keys). When the **Data Supervisor** spec lands, that escalation
becomes one of its event-sourced detectors — feeding the per-source
`DATA_HELD`/`DATA_CLEARED` hold + auto-clear primitive instead of a
blunt whole-cycle hard-stop. **No rework here:** the Supervisor is a
new *consumer* of an escalation this sentinel already emits. Auto-clear
for the schema-drift class will be conservative by construction (a
clean verified cycle for that feed *after* the operator/declared
contract was updated to the vendor's new shape) — designed in the
Supervisor spec, not here.

## 7. Non-goals

- Not prior-archive header comparison (tautological — archive header
  is our normalized fieldnames).
- Not dtype/value-shape inference (noisy on stringly CSV; YAGNI).
- Not auto-healing a vendor contract change (no safe canonical repair;
  escalate-only by nature).
- Not building the Data Supervisor / hold-gate / auto-clear here
  (Ladder rung 2 — its own spec, §6 is just the seam).
- Not an LLM (Ladder rung 3, #187, advisory, separate).
- Not wiring the enforced guard into every CSV-first handler at once
  (bounded first set; rest `guard_pending`, tracked).
- Operator interaction unchanged: internal data-layer hardening; the
  operator's only touchpoints remain the ADD/REMOVE Data Feed Change
  Request + the weekly digest ack.

## 8. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `tpcore/ingestion/vendor_schema.py`: `VendorSchema` model + `VENDOR_SCHEMAS` registry (ALL CSV-first feeds declared; high-risk set `guard_pending=False`, rest `True`) + `assert_vendor_schema` + `VendorSchemaDrift`. Clockwork tests: registry == CSV-first feed set; `guard_pending` set pinned; `assert_vendor_schema` raises on missing required key, no-ops on empty, ignores extra keys, samples bounded. **Landed dark** (no handler calls it yet). |
| 2 | Wire `assert_vendor_schema(feed, raw_records)` into the 4 high-risk handlers (`fred_macro`, `iborrowdesk_borrow_rates`, `finra_short_interest`, `apewisdom_social_sentiment`) at the raw-record boundary, before mapping/`write_archive`. Per-handler test: a synthetic vendor payload missing a required key raises `VendorSchemaDrift` before any DB write; the normal payload passes. |
| 3 | `audit_data_pipeline.py` known_knowns `schema_drift` check (§5: coverage + pending-gap WARN + recent-escalation FAIL). Tests with a fake `data_quality_log`/event surface. |
| 4 | Doc reconciliation: CLAUDE.md (the producer-guard list + the Escalation & Hardening Ladder framing), TODO.md #186 candidate (6) DONE, this spec Status → BUILT with the build record. |

## 9. Open questions for the plan phase

- **Exact CSV-first feed set** (the clockwork-test denominator): the
  plan MUST enumerate it directly from the `write_archive` call sites
  in `tpcore/ingestion/handlers.py` + `scripts/ops.py` (do NOT guess;
  read the code — same discipline that caught the auditheal v1
  premise defect).
- **`required_keys` per high-risk feed:** derived from each adapter's
  actual parsed response (read the adapter + handler mapping), with an
  `evidence` string — never guessed (no-vendor-blame rule).
- **Sample size N** for `assert_vendor_schema`: lean small (e.g. 5) —
  a contract change is uniform across a payload; confirm no feed
  legitimately has per-record-optional keys that would false-positive
  a sample (if so, that key is NOT a `required_key`).
- **Raw-record availability per handler:** confirm each of the 4
  handlers still has the raw vendor records in scope at the chosen
  call site (some may map inline in a generator/comprehension —
  the plan checks each and, if needed, names the minimal local
  refactor to expose the raw list, no behavior change).
