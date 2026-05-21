# Consolidated Defect Register (#254) — Design **v1 (expert-scoped)**

**Status:** **BUILT** 2026-05-19 (ops-lane, read-model + one minimal
primitive). Expert scope pass (skeptical staff-architect,
code-grounded) → spec → operator review gate → plan → phased subagent
build, all three phases shipped.

**Build record:**
- **DR1 (PR #90)** — `ops/defect_register.py` derived read-model +
  `list` CLI; composes both Ladders' read APIs verbatim; the parity
  forcing-test (register escalation rows ≡ both Ladders'
  undispositioned set). Dark.
- **DR2 (PR #91)** — the missing primitive:
  `REVIEW_DEFECT_LOGGED`/`REVIEW_DEFECT_RESOLVED` via the thin `_emit`
  mirror + `log`/`resolve` CLI; retention-exemption for the two event
  types; the TODO-parity forcing CI test.
- **DR3 (this PR)** — render-only Health-tab panel
  (`render_defect_register`, reuses the `_fetch_escalation_state`
  fetch+cache idiom verbatim; pure classifier
  `dashboard_components/defect_register.py`; recomputes nothing, NO
  write button per §5 OUT) + this docs reconcile.

## 0. Problem

Defect/issue state is **distributed with no single tracker**:
escalation events (`platform.application_log`
`ENGINE_ESCALATED`/`DATA_*_ESCALATED`/`*_LLM_TRIAGE_PROPOSAL`), the
Ladder disposition SoTs (`ops/engine_ladder.py` `list_undispositioned`
/ `DISPOSITION_POLICIES` / `ENGINE_ESCALATION_DISPOSITIONED`; the
data-lane Ladder via `ops/weekly_digest.build_weekly_digest`;
`tpcore/datasupervisor`, `tpcore/auditheal`), the weekly digest, the
dashboard escalation panel, `TODO.md`, GitHub PRs. **The genuinely
missing piece:** human/review-found defects that are NOT
escalation-class (this session's #245 RiskGovernor weekly-cap, #250
FMP 3-tuple, #251 B1 false-premise — found by verify-before-acting /
a failing test / a code review, never by a deterministic agent
escalating) have **no durable home except an ad-hoc `TODO.md` line**.

## 1. Verdict — derived read-model + ONE small new event class (NOT a new authoritative table)

A new `defects` table everything writes to is the **parallel-SoT /
rat's-nest anti-pattern** (CLAUDE.md "one canonical mechanism, no
rat's nest"; `docs/superpowers/specs/2026-05-18-dashboard-escalation-
audit-panel-design.md` §1: *"the console and the weekly digest must
be incapable of disagreeing because they call the same function;
reimplementation here is the exact bug"*). **Rejected.**

**The register is a derived consolidated READ-MODEL** over the
existing SoTs, PLUS exactly one missing primitive: a durable record
for the **non-escalation review-found defect**, implemented on the
**existing `application_log` substrate** (schemaless `data jsonb` —
no migration, no new table) via a new event class
`REVIEW_DEFECT_LOGGED` / `REVIEW_DEFECT_RESOLVED`, emitted by a thin
helper that mirrors `engine_ladder._emit` byte-for-byte. **No new
write-coupling on any existing producer; no new daemon; no new
table.**

## 2. Taxonomy / identity / lifecycle

- **escalation** — deterministic-agent emission, already in the
  Ladders.
- **disposition** — terminal verb on an escalation
  (`converted|structural|removed`, `engine_ladder.py:50-57`). A
  **`structural`** disposition *means "a human must convert this to a
  permanent code fix"* — it IS a defect; the register treats every
  open `structural`-class/dispositioned escalation as a defect row
  (the bridge from escalation-world to defect-world).
- **review-found defect** — the new `REVIEW_DEFECT_LOGGED` event
  (origin: human / failing test / code review; no escalation).
- **TODO follow-up** — an open `TODO.md` `[lane/effort]` line;
  reconciled into, never duplicated by, the register.
- **identity / dedup**: `defect_ref` = `hold_id` (escalation-origin)
  | `#NNN` PR/issue (review-origin) | normalized TODO anchor. One
  defect that is an escalation + a TODO line + a PR collapses to ONE
  row keyed on `defect_ref` — the register **joins, never sums**.
- **lifecycle**: `open → triaged → fix-in-progress (PR open) → fixed
  (PR merged) → verified/closed` | `wont-fix / structural-parked`.
  Fix linkage via `data.pr` / commit SHA on a `REVIEW_DEFECT_RESOLVED`
  event using the SAME anti-join open-predicate pattern as
  `_DISPOSITIONED_EVENT` (`engine_ladder.py:183-186`); merged-PR
  closure is *derived* (`gh pr` ref), not a hand-maintained state
  machine.
- **lowest-friction durable mechanism**: a CLI `python -m
  ops.defect_register {log|list|resolve}` mirroring the
  `engine_ladder` / `weekly_digest` CLI shape exactly.
- **forcing function** (platform DNA — clockwork): a CI test
  asserting every `TODO.md` line tagged a still-open defect has a
  matching open `REVIEW_DEFECT_LOGGED` — a review-found defect
  **cannot live only in TODO.md and be forgotten**.

## 3. Never-mask invariants (+ parity forcing-function)

1. **No re-derivation:** the register MUST call
   `engine_ladder.list_undispositioned` and
   `weekly_digest.build_weekly_digest().undispositioned` *verbatim*
   — never re-query `application_log` for escalation state (honors
   the panel-spec doctrine; the register and the digest are
   incapable of disagreeing because they call the same function).
2. **No silent loss:** a defect closes ONLY on a durable
   disposition / `*_RESOLVED` event, never by omission (anti-join
   open-predicate, `engine_ladder.py:183-191`).
3. **No double-count:** `defect_ref` join; enforced by a **parity
   test** — register's escalation-derived rows ≡ (`list_undispositioned`
   ∪ data-lane undispositioned) as a set (same forcing-test pattern
   as `escalation_drift()`, `engine_ladder.py:139-146`).

## 4. Where it lives / surfaces

- **`ops/defect_register.py`** — a consumer/composer (ops-lane, like
  `weekly_digest`). Imports + calls BOTH Ladders' read APIs
  (symmetry-not-copy: reimplements neither). **Read everywhere; the
  ONLY write is the thin `REVIEW_DEFECT_LOGGED`/`_RESOLVED` helper.**
- **Dashboard**: a render-only panel reusing the
  `_fetch_escalation_state` pattern (`dashboard.py:2118-2197`) — the
  engine Ladder currently has **zero dashboard consumers** (grep-
  confirmed blind spot); this panel renders the register, recomputes
  nothing, adds no write surface.

## 5. Scope boundary / fatal-objection

**OUT:** a Jira/workflow engine; a new daemon; a new authoritative
table; a schema migration; any dashboard write button
(`…panel-design.md:131-138` non-goal); any new write-coupling on
existing escalation producers.

**The one real risk (mitigated, in-scope):** `application_log` has a
7-day `DBLogHandler` retention; an open `REVIEW_DEFECT_LOGGED` must
not silently expire. Mitigation: the helper tags these
**retention-exempt** (reuse the existing retention-exemption
precedent — confirm/extend it in the plan) and the register's "open"
predicate must never depend on a row retention can delete. No other
fatal objection (JSONB is schemaless → no migration; both Ladders
expose read APIs → no reimplementation).

## 6. Phasing (gated PR per phase; subagent-driven; build held behind #253)

| Phase | Deliverable |
|---|---|
| **DR1** | `ops/defect_register.py` read-model + `{list}` CLI: composes `engine_ladder.list_undispositioned` + `weekly_digest.build_weekly_digest().undispositioned` (verbatim, no re-query) into the unified `defect_ref`-keyed view with lifecycle states; the **parity forcing-test** (register escalation rows ≡ both Ladders' undispositioned set). Dark (no new event yet). One gated PR. |
| **DR2** | The missing primitive: `REVIEW_DEFECT_LOGGED`/`REVIEW_DEFECT_RESOLVED` via the thin `_emit` mirror + the `{log,resolve}` CLI; **retention-exemption** for these events (confirm the existing exempt mechanism, extend minimally); the **TODO-parity forcing CI test** (every still-open defect TODO line ⇒ a matching open event). Register now unifies escalation + review-found. One gated PR. |
| **DR3** | Render-only dashboard panel (reuse `_fetch_escalation_state` pattern; recompute nothing) + docs reconcile (`TODO.md`, CLAUDE.md ops line, this spec → BUILT + build record, memory). One gated PR. |

**Spec ready for the operator review gate** — it adds a unifying
read-model + ONE minimal `application_log` event class (no new
table/daemon/SoT), explicitly honoring the render-the-SoT /
no-rat's-nest doctrine; the only material risk (log retention) is
bounded by an existing precedent. Build is **held behind #253**
(merge queue blocked) — spec/plan only for now.
