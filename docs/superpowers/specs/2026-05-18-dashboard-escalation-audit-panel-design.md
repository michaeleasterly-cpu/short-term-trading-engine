# Dashboard Escalation/Integrity Audit Panel — Design

**Status:** spec 2026-05-18 (DATA lane / operator console). Brainstorm
→ **spec (this doc)** → plan → phased subagent build. #189 (the
deferred-LAST item; its precondition — finish the data-lane hardening
— is now met).

**Investigation finding (verified, not assumed):** a grep of the
entire console (`dashboard.py`, 3255 lines, + `dashboard_components/`)
for every concept this session shipped returns **0 hits** — no
`DATA_SOURCE_HELD/CLEARED/ESCALATED`, no `DATA_REPAIR_ESCALATED/
COMPLETE`, no `AdapterContractDrift`, no `cross_table_audit.*`, no
`DATA_ESCALATION_DISPOSITIONED`/undispositioned, no auditheal /
datasupervisor / Ladder. The operator's *audit layer* is
systematically blind to the escalation/hardening posture it exists to
audit. `classify_validation` reads only `validation.%` (blind to the
`cross_table_audit.*` auditheal layer); `classify_cross_ref` consumes
the stale pre-session `{check, table, count}` print shape.

So the operator's "the dashboard will probably need a refactor" is
**half right**: the load-bearing problem is an **audit-correctness /
coverage gap**, not the file size. This spec closes the gap; the
3255-line monolith decomposition is explicitly **out** (separate,
deferrable, higher-risk, low audit-value).

## 1. Principle — the console is a read-only renderer of the SoT

The console drifted because it **reimplemented** predicates that have
since changed. The fix's invariant: the panel **reuses the existing
single sources of truth and renders them** — it computes nothing the
weekly digest / Ladder / Data Supervisor don't already compute. The
live console and the weekly digest must be *incapable of disagreeing*
because they call the same function. Reimplementation here is the
exact bug being fixed and is forbidden by this design.

## 2. Reused SoT (no predicate rewritten in the dashboard)

- **Undispositioned escalations** → `ops.weekly_digest.
  build_weekly_digest(pool).undispositioned` (already annotated with
  the `tpcore.ladder.policy_for` disposition + reason). Rendered
  verbatim.
- **Open per-source holds** → the `DATA_SOURCE_HELD` with no later
  `DATA_SOURCE_CLEARED` anti-join (the exact shape in
  `tpcore/datasupervisor/state.py`; reused, not re-derived).
- **Cross-table audit roll-up** → `data_quality_log` rows
  `source LIKE 'cross_table_audit.%'` (the auditheal-persisted layer
  `classify_validation` is blind to) — same red predicate the rest of
  the platform uses (`stale OR confidence < 1.0`).
- **Recent escalations** → `application_log` `DATA_REPAIR_ESCALATED` /
  `INGESTION_FAILED` with `data->>'exception_type'='AdapterContractDrift'`
  in the trailing window, paired with their resolving terminals
  (`DATA_REPAIR_COMPLETE` / disposition).

## 3. New focused component — `dashboard_components/escalation.py`

Pure, Streamlit-free `classify_*` functions (mirrors
`dashboard_components/health.py`'s tested pattern; each returns the
`(color, summary, detail_rows)` tuple shape `health.py` already uses,
so the existing `_render_health_row` renders them unchanged):

- `classify_source_holds(rows) -> (color, summary, detail)` — open
  `DATA_SOURCE_HELD`; red if any open, each row = `source` + age + the
  hold reason. Green = "no source holds".
- `classify_undispositioned(items: list[str]) -> (color, summary,
  detail)` — input is `build_weekly_digest(...).undispositioned`
  verbatim (already policy-annotated). Red if non-empty (rung-3:
  undispositioned escalations are an operator-action gate).
- `classify_cross_table_audit(rows) -> (color, summary, detail)` —
  latest `cross_table_audit.%` per source; red on `stale OR
  confidence<1.0`. Closes the auditheal blind spot.
- `classify_recent_escalations(rows) -> (color, summary, detail)` —
  trailing-window `DATA_REPAIR_ESCALATED` + `AdapterContractDrift`;
  each marked resolved (has terminal) vs OPEN.

Each is unit-tested with fabricated row dicts (no DB, no Streamlit) —
the `health.py` `classify_*` test precedent.

## 4. Wiring (thin, in `dashboard.py`)

- `_fetch_escalation_state()` — one async fetch (mirrors
  `_fetch_platform_health`): builds the pool, calls
  `build_weekly_digest(pool)` (reuse), runs the open-holds query, the
  `cross_table_audit.%` roll-up query, and the recent-escalations
  query; returns a dict. Cached behind a `_fetch_escalation_state_
  cached` wrapper exactly like `_fetch_platform_health_cached`
  (same TTL + the ↻ Refresh `.clear()` idiom).
- `render_escalation_audit()` — composes the four `classify_*` via the
  existing `_render_health_row` + collapsible detail expanders;
  byte-for-byte mirrors `render_platform_health`'s structure.
- Rendered **inside the Health tab, immediately after
  `render_platform_health()`** (operator decision 2026-05-18:
  escalations *are* system-status / heuristic #1 — a panel, not a new
  tab, so it cannot be missed).

## 5. The two confirmed drifts (fixed, minimally)

- `classify_cross_ref` / its `_q_cross_ref` fetch consume the stale
  pre-session `{check, table, count}` print shape. **Repoint** that
  fetch to the persisted `cross_table_audit.%` `data_quality_log`
  rows and route it through the new `classify_cross_table_audit`
  (retire the stale `classify_cross_ref` body; keep the call site /
  the panel row so the operator's existing "cross-reference" row now
  shows the *correct, persisted* data). No behavioural surprise — the
  row stays, its data source becomes the real one.
- `classify_validation` is correct as-is for `validation.%`
  (selfheal layer) — left unchanged; the auditheal layer is now
  covered by the new `classify_cross_table_audit`, not by overloading
  `classify_validation`.

## 6. Non-goals / scope boundary

- **No `dashboard.py` monolith decomposition** (separate, deferred,
  higher-risk; not what's broken).
- **No new business logic / no recomputation** — pure render of
  existing SoT (reimplementation is the bug).
- **No new top-level tab** — a panel in Health (operator decision).
- **No engine/aar panels** — data-lane / operator-console scope.
- **No new gate / write path** — read-only panel. The `disposition`
  action stays the CLI verb (`python -m ops.weekly_digest
  disposition …`); the panel *surfaces* undispositioned items, it
  does not add a dashboard write button (out of scope; the console's
  established convention is "dispatch existing scripts, no business
  logic here").

## 7. Phasing (each independently testable; gated PR per phase)

| Phase | Deliverable |
|---|---|
| 1 | `dashboard_components/escalation.py`: the four pure `classify_*` functions returning the `health.py` `(color, summary, detail)` tuple shape. `tpcore/tests/test_dashboard_escalation.py` (or `tests/…` per the repo's dashboard-test location — the plan reads where `health.py` classifiers are tested) with fabricated-row unit tests: each green/red/age/resolved-vs-open path. **Dark** (no dashboard.py caller yet). |
| 2 | `dashboard.py`: `_fetch_escalation_state()` + `_fetch_escalation_state_cached` + `render_escalation_audit()`; call it in the Health tab after `render_platform_health()`. Repoint `_q_cross_ref` → `cross_table_audit.%` through `classify_cross_table_audit` (retire the stale `classify_cross_ref` body). Smoke: `dashboard.py` imports clean; the Streamlit app builds (import-level / `python -c` smoke — no headless-render harness exists, so the test bar is import + the P1 unit tests, stated honestly). |
| 3 | Docs: update `docs/superpowers/specs/2026-05-13-operator-dashboard.md` (the console design doc) with the new panel + the read-only-renderer-of-SoT principle; CLAUDE.md dashboard line; this spec → BUILT + build record. |

## 8. Open questions for the plan phase (resolve by READING code, not guessing)

- **Dashboard-test location/harness.** The plan must read where the
  existing `dashboard_components/health.py` `classify_*` are unit-tested
  (`tpcore/tests/` vs top-level `tests/`) and mirror that exactly; and
  state honestly that Streamlit `render_*` has no unit harness (the
  bar is the pure-classifier tests + an import smoke), not invent one.
- **`build_weekly_digest` import cost from `dashboard.py`.** Confirm
  importing `ops.weekly_digest` into `dashboard.py` introduces no
  import cycle / heavy cost (it already imports `tpcore.ladder`,
  `tpcore.db`; the dashboard already imports `tpcore.db`). If
  `ops.weekly_digest` pulls something dashboard-hostile, the plan
  isolates the reused read (still SoT — call the same query the digest
  builds, not a copy) rather than importing the module; decide from
  the actual import graph, do not assume.
- **Exact `_q_cross_ref` call site + the panel row label** in
  `dashboard.py` — read it; the repoint must preserve the operator's
  existing row so this is a data-source correction, not a UX change.
- **Cache TTL/idiom** — match `_fetch_platform_health_cached`'s exact
  decorator + `.clear()` refresh pattern (read it; don't guess the TTL).
