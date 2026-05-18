# Dashboard Escalation/Integrity Audit Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the operator console surface the escalation/hardening state it is currently blind to (per-source holds, undispositioned escalations, the auditheal `cross_table_audit.*` layer, recent data escalations) by rendering — never recomputing — the existing single sources of truth.

**Architecture:** A new focused `dashboard_components/escalation.py` of pure, Streamlit-free `classify_*` functions (mirrors `dashboard_components/health.py`'s tested `(color, summary, detail)` tuple shape). Thin wiring in `dashboard.py`: one cached `_fetch_escalation_state()` that REUSES `ops.weekly_digest.build_weekly_digest()` for the policy-annotated undispositioned list plus three small read queries, and one `render_escalation_audit()` panel rendered inside the Health tab after `render_platform_health()`. The stale `_q_cross_ref` is repointed to the persisted `cross_table_audit.%` rows.

**Tech Stack:** Python 3.11, Streamlit, asyncpg, pytest (`asyncio_mode=auto`), ruff. Spec: `docs/superpowers/specs/2026-05-18-dashboard-escalation-audit-panel-design.md`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `dashboard_components/escalation.py` | 4 pure `classify_*(rows/list) -> (color, summary, detail)` — no Streamlit, no DB, no `ops` import | P1 |
| `tests/test_dashboard_escalation.py` | fabricated-row unit tests (mirrors `tests/test_dashboard_platform_health.py`) | P1 |
| `dashboard.py` | `_fetch_escalation_state()` + `_fetch_escalation_state_cached` + `render_escalation_audit()`; Health-tab call; repoint `_q_cross_ref`→`cross_table_audit.%` + swap its classifier at the `:2468` call site | P2 |
| `docs/superpowers/specs/2026-05-13-operator-dashboard.md`, `CLAUDE.md`, this spec | reconciliation | P3 |

One phase = one gated PR. Branch off fresh `main` per phase; CI green before merge; verify branch before every commit. Implementers commit only; controller opens/merges PRs after spec + code-quality review (the prior data-lane pattern).

---

## Ground truth (verified from source — do not re-derive/guess)

**Tests:** `dashboard_components/health.py` classifiers are unit-tested in **top-level `tests/test_dashboard_platform_health.py`** → the new tests go in `tests/test_dashboard_escalation.py`.

**Classifier contract (mirror `health.py`):** roll-up classifiers return `tuple[str, str, list[tuple[str, str, str]]]` = `(color, summary, detail)` where `color ∈ {"green","amber","red"}`, `summary` is a one-liner, and each `detail` row is `(label, color, text)`. Rendered exactly like the existing cross_ref/validation rows in `render_platform_health` (`dashboard.py:2468`):
```python
c, s, d = classify_x(h["x"])
_render_health_row("<Row label>", c, s, row_key="<key>")
if c != "green":
    with st.expander("<detail title>", expanded=True):
        for label, color, text in d:
            _render_health_row(label, color, text)
```
`_render_health_row(label, color, text, *, fix_action=None, row_key=None) -> None` (`dashboard.py:1691`).

**Cache idiom:** `@st.cache_data(ttl=180, show_spinner=False)` on a sync wrapper `_fetch_X_cached()` that calls `run_async(_fetch_X())`; the ↻ Refresh button calls `_fetch_X_cached.clear()` then `st.rerun()` (`dashboard.py:2152`, `:1969`). New fetch mirrors this verbatim (ttl=180).

**`ops.weekly_digest` is cycle-safe:** it imports only stdlib + `structlog` + `tpcore.db.build_asyncpg_pool` + `tpcore.ladder.policy_for`. It does NOT import `dashboard`. `from ops.weekly_digest import build_weekly_digest` in `dashboard.py` is safe (the `scripts/ops.py`↔`ops/` shadowing is a *pytest-collection* concern only — handled by P1 being pure with NO `ops` import, so P1 unit tests never touch it; the import lives only in P2's runtime fetch). `build_weekly_digest(pool, now=None) -> WeeklyDigest`; `.undispositioned: list[str]` is the policy-annotated string list (verbatim renderable).

**Stale `_q_cross_ref`** (`dashboard.py:699`, inner async fn in `_fetch_platform_health`, gathered `:894`, stored `out["cross_ref"]` `:915`, rendered `:2468` as row label `"Cross-table integrity"`, `row_key="cross_ref"`, classifier `classify_cross_ref(h["cross_ref"])`): its body runs hand-written `LEFT JOIN platform.prices_daily_tickers … COUNT(*)` checks — it does NOT read the persisted `cross_table_audit.%` rows `tpcore/audit/cross_table.py` now writes. Repoint = replace its body with the `cross_table_audit.%` roll-up query and swap the call-site classifier to `classify_cross_table_audit` (keep the row label + `row_key="cross_ref"` so the operator's row is unchanged — only its data becomes correct).

**Reused predicates (the anti-drift invariant — copy the shape, do not invent):**
- open holds: `DATA_SOURCE_HELD` with no later `DATA_SOURCE_CLEARED` on `data->>'hold_id'` (the `tpcore/datasupervisor/state._CURRENT_HOLD_SQL` anti-join, generalised to all sources).
- cross_table roll-up: latest row per `source LIKE 'cross_table_audit.%'`, red on `stale OR confidence < 1.0` (the platform's standard red predicate).
- recent escalations: `application_log` `DATA_REPAIR_ESCALATED` and `INGESTION_FAILED` w/ `data->>'exception_type'='AdapterContractDrift'` in trailing 7d, paired with `DATA_REPAIR_COMPLETE` / a disposition.

`run_async`, `_db_url`, `build_asyncpg_pool`, `st`, `_render_health_row` are already imported in `dashboard.py`.

---

## Phase 1 — `dashboard_components/escalation.py` (pure classifiers), dark (PR 1)

Branch: `feat/dash-escalation-p1`.

### Task 1.1: the 4 pure classifiers + tests

**Files:**
- Create: `dashboard_components/escalation.py`
- Test: `tests/test_dashboard_escalation.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_dashboard_escalation.py`:

```python
"""Unit tests for the escalation/integrity audit classifiers.

Pure: fabricated row dicts / string lists. No DB, no Streamlit.
Mirrors tests/test_dashboard_platform_health.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashboard_components.escalation import (
    classify_cross_table_audit,
    classify_recent_escalations,
    classify_source_holds,
    classify_undispositioned,
)


def test_source_holds_none_is_green() -> None:
    color, summary, detail = classify_source_holds([])
    assert color == "green" and detail == []
    assert "no" in summary.lower()


def test_source_holds_open_is_red_with_age() -> None:
    held_at = datetime.now(UTC) - timedelta(hours=30)
    color, summary, detail = classify_source_holds(
        [{"source": "validation:prices_daily", "held_at": held_at,
          "reason": "stuck"}]
    )
    assert color == "red"
    assert any("prices_daily" in row[0] for row in detail)
    assert any("30h" in row[2] or "1d" in row[2] for row in detail)


def test_undispositioned_empty_is_green() -> None:
    color, summary, detail = classify_undispositioned([])
    assert color == "green" and detail == []


def test_undispositioned_nonempty_is_red_verbatim() -> None:
    items = [
        "2026-05-01 [DATA_SOURCE_ESCALATED] ref=h1 stuck | "
        "policy:escalate_operator — operator dispositions via digest",
    ]
    color, summary, detail = classify_undispositioned(items)
    assert color == "red"
    # rendered VERBATIM (already policy-annotated by build_weekly_digest)
    assert detail[0][2] == items[0]
    assert "1" in summary


def test_cross_table_audit_all_green() -> None:
    color, summary, detail = classify_cross_table_audit(
        [{"source": "cross_table_audit.tradier_options_chains.x",
          "stale": False, "confidence": 1.0}]
    )
    assert color == "green"


def test_cross_table_audit_red_on_stale_or_low_conf() -> None:
    color, summary, detail = classify_cross_table_audit(
        [{"source": "cross_table_audit.t.a", "stale": False,
          "confidence": 0.0},
         {"source": "cross_table_audit.t.b", "stale": True,
          "confidence": 1.0}]
    )
    assert color == "red"
    assert len([r for r in detail if r[1] == "red"]) == 2


def test_cross_table_audit_no_rows_amber() -> None:
    color, summary, detail = classify_cross_table_audit([])
    assert color == "amber"


def test_recent_escalations_resolved_vs_open() -> None:
    color, summary, detail = classify_recent_escalations(
        [{"etype": "DATA_REPAIR_ESCALATED", "ref": "r1",
          "recorded_at": datetime.now(UTC), "message": "m1",
          "resolved": True},
         {"etype": "DATA_REPAIR_ESCALATED", "ref": "r2",
          "recorded_at": datetime.now(UTC), "message": "m2",
          "resolved": False}]
    )
    # an OPEN escalation in the window is red; all-resolved is amber/green
    assert color == "red"
    assert any("OPEN" in row[2] for row in detail)
    assert any("resolved" in row[2].lower() for row in detail)


def test_recent_escalations_none_is_green() -> None:
    color, summary, detail = classify_recent_escalations([])
    assert color == "green"
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: dashboard_components.escalation`).

Run: `source .venv/bin/activate && python -m pytest tests/test_dashboard_escalation.py -q`

- [ ] **Step 3: Create `dashboard_components/escalation.py`**

```python
"""Escalation / data-integrity audit classifiers (#189).

Pure, Streamlit-free, DB-free — mirrors dashboard_components/health.py.
The console is a READ-ONLY RENDERER of the existing SoT: these take
already-fetched rows / the already-policy-annotated undispositioned
list and only classify+format them. They recompute NO predicate (that
reimplementation is exactly how the console drifted).

Return contract (identical to health.py roll-up classifiers):
``(color, summary, detail)`` — color in {"green","amber","red"},
detail = list[(label, color, text)] — so the existing
``_render_health_row`` + expander loop render them unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_Detail = list[tuple[str, str, str]]
_Result = tuple[str, str, _Detail]


def _age(ts: Any) -> str:
    if not isinstance(ts, datetime):
        return "age?"
    delta = datetime.now(UTC) - ts
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def classify_source_holds(rows: list[dict[str, Any]]) -> _Result:
    """Open Data-Supervisor per-source holds (DATA_SOURCE_HELD with no
    later DATA_SOURCE_CLEARED). Any open hold is red — a held source is
    a stood-down feed the operator must drive to disposition."""
    if not rows:
        return "green", "No open source holds", []
    detail: _Detail = [
        (str(r.get("source", "?")), "red",
         f"held {_age(r.get('held_at'))} — {r.get('reason', '')}")
        for r in rows
    ]
    return "red", f"{len(rows)} source(s) held (Data Supervisor)", detail


def classify_undispositioned(items: list[str]) -> _Result:
    """Undispositioned data-lane escalations. ``items`` is
    ``build_weekly_digest(...).undispositioned`` VERBATIM (already
    policy-annotated). Non-empty = red (rung-3: an undispositioned
    escalation is an open operator-action gate)."""
    if not items:
        return "green", "No undispositioned escalations", []
    detail: _Detail = [(f"#{i + 1}", "red", s)
                        for i, s in enumerate(items)]
    return (
        "red",
        f"{len(items)} undispositioned — `python -m ops.weekly_digest "
        f"disposition <ref> <converted|structural|removed>`",
        detail,
    )


def _red(stale: Any, confidence: Any) -> bool:
    return bool(stale) or (confidence is not None and confidence < 1.0)


def classify_cross_table_audit(rows: list[dict[str, Any]]) -> _Result:
    """Latest cross_table_audit.* per source (the auditheal-persisted
    layer classify_validation is blind to). Red on stale OR
    confidence<1.0 — the platform's standard red predicate."""
    if not rows:
        return "amber", "No cross-table audit rows recorded", []
    detail: _Detail = []
    worst = "green"
    nred = 0
    for r in rows:
        src = str(r.get("source", "?")).replace("cross_table_audit.", "")
        if _red(r.get("stale"), r.get("confidence")):
            detail.append((src, "red", "latest: FAILED"))
            worst = "red"
            nred += 1
        else:
            detail.append((src, "green", "latest: clean"))
    summary = (
        f"All {len(rows)} cross-table check(s) clean" if worst == "green"
        else f"{nred}/{len(rows)} cross-table check(s) FAILED — see detail"
    )
    return worst, summary, detail


def classify_recent_escalations(rows: list[dict[str, Any]]) -> _Result:
    """Trailing-window DATA_REPAIR_ESCALATED + AdapterContractDrift.
    Each row carries a ``resolved`` bool (terminal/disposition seen).
    Any OPEN (unresolved) escalation in the window is red."""
    if not rows:
        return "green", "No escalations in the last 7 days", []
    detail: _Detail = []
    n_open = 0
    for r in rows:
        when = r.get("recorded_at")
        when_s = when.strftime("%Y-%m-%d") if isinstance(
            when, datetime) else "?"
        if r.get("resolved"):
            detail.append(
                (f"{r.get('etype', '?')} ref={r.get('ref', '?')}",
                 "amber", f"{when_s} resolved — {r.get('message', '')}"))
        else:
            n_open += 1
            detail.append(
                (f"{r.get('etype', '?')} ref={r.get('ref', '?')}",
                 "red", f"{when_s} OPEN — {r.get('message', '')}"))
    if n_open:
        return "red", f"{n_open} OPEN escalation(s) (last 7d)", detail
    return "amber", f"{len(rows)} escalation(s) resolved (last 7d)", detail


__all__ = [
    "classify_cross_table_audit",
    "classify_recent_escalations",
    "classify_source_holds",
    "classify_undispositioned",
]
```

- [ ] **Step 4: Run the tests — all 9 pass.** `python -m pytest tests/test_dashboard_escalation.py -q`. Fix implementation only (never tests). (The `30h`/`1d` age assertion: 30h → `_age` returns `"1d"`; the test accepts either token.)

- [ ] **Step 5: Lint + collection.** `ruff check dashboard_components/escalation.py tests/test_dashboard_escalation.py` (clean, no noqa) and `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` (collection clean).

- [ ] **Step 6: Commit**

```bash
test "$(git branch --show-current)" = "feat/dash-escalation-p1" || { echo WRONG; exit 1; }
git add dashboard_components/escalation.py tests/test_dashboard_escalation.py
git commit -m "feat(dashboard): pure escalation/integrity audit classifiers (dark)"
```
STOP. Report DONE (9/9, ruff, collection, commit SHA) or BLOCKED.

---

## Phase 2 — wire the panel into the Health tab + repoint cross_ref (PR 2)

Branch: `feat/dash-escalation-p2` off fresh `main`.

### Task 2.1: `_fetch_escalation_state` + `render_escalation_audit` + cross_ref repoint

**Files:** Modify `dashboard.py`

- [ ] **Step 1: Read the integration points.** `sed -n '690,720p' dashboard.py` (the `_q_cross_ref` inner fn), `sed -n '2455,2495p' dashboard.py` (the cross_ref + validation render block), `sed -n '2143,2165p' dashboard.py` (`_fetch_platform_health_cached`), `sed -n '1960,1990p' dashboard.py` (the ↻ Refresh `.clear()` idiom), and the Health-tab body (`grep -n "with tab_health:\|render_platform_health()" dashboard.py`). Confirm the exact line of `out["cross_ref"] = cross_ref` and the `classify_cross_ref(h["cross_ref"])` call site.

- [ ] **Step 2: Repoint `_q_cross_ref` to the persisted layer.** Replace the body of the inner `async def _q_cross_ref()` (the hand-written `LEFT JOIN prices_daily_tickers` COUNT list) with the persisted cross-table audit roll-up:

```python
    async def _q_cross_ref() -> list[dict]:
        """Latest cross_table_audit.* per source (the auditheal-
        persisted SoT — replaces the pre-session inline COUNT checks
        that drifted from tpcore/audit/cross_table.py)."""
        rows = await conn.fetch(
            """
            WITH latest AS (
                SELECT source, MAX(timestamp) AS t
                FROM platform.data_quality_log
                WHERE source LIKE 'cross_table_audit.%'
                GROUP BY source
            )
            SELECT q.source, q.stale, q.confidence
            FROM platform.data_quality_log q
            JOIN latest l ON l.source = q.source AND l.t = q.timestamp
            ORDER BY q.source
            """
        )
        return [dict(r) for r in rows]
```
(Use the same `conn` the surrounding `_fetch_platform_health` inner queries use — match the sibling `_q_*` style exactly; if they take `conn` as a closure vs param, mirror that.)

- [ ] **Step 3: Swap the cross_ref classifier at the render site.** At `dashboard.py:~2468`, change:
```python
    cr_color, cr_summary, cr_detail = classify_cross_ref(h["cross_ref"])
```
to:
```python
    cr_color, cr_summary, cr_detail = classify_cross_table_audit(h["cross_ref"])
```
and update the import line `from dashboard_components.health import (... classify_cross_ref ...)` → remove `classify_cross_ref`, add `from dashboard_components.escalation import classify_cross_table_audit` (place near the other `dashboard_components` imports). Keep the row label `"Cross-table integrity"` and `row_key="cross_ref"` unchanged. (Leave `classify_cross_ref` in `health.py` untouched — out of scope to delete; it's simply no longer called. Do NOT add a deprecation shim.)

- [ ] **Step 4: Add `_fetch_escalation_state` + cached wrapper.** Near `_fetch_platform_health` / `_fetch_platform_health_cached`, add:

```python
async def _fetch_escalation_state() -> dict:
    """Read-only escalation/integrity SoT for the audit panel. REUSES
    ops.weekly_digest.build_weekly_digest for the policy-annotated
    undispositioned list (so the console and the weekly digest cannot
    disagree); the rest are small reads of the same rows the platform
    already gates on. Recomputes no predicate."""
    from ops.weekly_digest import build_weekly_digest

    pool = await build_asyncpg_pool(_db_url(), max_size=4)
    try:
        digest = await build_weekly_digest(pool)
        async with pool.acquire() as conn:
            holds = await conn.fetch(
                """
                SELECT h.data->>'source' AS source,
                       h.data->>'reason'  AS reason,
                       h.recorded_at      AS held_at
                FROM platform.application_log h
                LEFT JOIN platform.application_log c
                  ON c.event_type = 'DATA_SOURCE_CLEARED'
                 AND (c.data->>'hold_id') = (h.data->>'hold_id')
                WHERE h.event_type = 'DATA_SOURCE_HELD'
                  AND c.event_type IS NULL
                ORDER BY h.recorded_at
                """
            )
            ct = await conn.fetch(
                """
                WITH latest AS (
                    SELECT source, MAX(timestamp) AS t
                    FROM platform.data_quality_log
                    WHERE source LIKE 'cross_table_audit.%'
                    GROUP BY source
                )
                SELECT q.source, q.stale, q.confidence
                FROM platform.data_quality_log q
                JOIN latest l ON l.source=q.source AND l.t=q.timestamp
                ORDER BY q.source
                """
            )
            esc = await conn.fetch(
                """
                SELECT e.data->>'request_id' AS ref,
                       'DATA_REPAIR_ESCALATED' AS etype,
                       e.recorded_at, e.message,
                       EXISTS (
                         SELECT 1 FROM platform.application_log t
                         WHERE t.event_type='DATA_REPAIR_COMPLETE'
                           AND t.data->>'request_id'=e.data->>'request_id'
                           AND t.recorded_at > e.recorded_at) AS resolved
                FROM platform.application_log e
                WHERE e.event_type='DATA_REPAIR_ESCALATED'
                  AND e.recorded_at > now() - interval '7 days'
                UNION ALL
                SELECT e.data->>'feed' AS ref,
                       'AdapterContractDrift' AS etype,
                       e.recorded_at, e.message,
                       false AS resolved
                FROM platform.application_log e
                WHERE e.event_type='INGESTION_FAILED'
                  AND e.data->>'exception_type'='AdapterContractDrift'
                  AND e.recorded_at > now() - interval '7 days'
                ORDER BY recorded_at DESC
                """
            )
    finally:
        await pool.close()
    return {
        "undispositioned": list(digest.undispositioned),
        "source_holds": [dict(r) for r in holds],
        "cross_table_audit": [dict(r) for r in ct],
        "recent_escalations": [dict(r) for r in esc],
    }


@st.cache_data(ttl=180, show_spinner=False)
def _fetch_escalation_state_cached() -> dict:
    """Sync cache wrapper (same idiom/TTL as
    _fetch_platform_health_cached)."""
    return run_async(_fetch_escalation_state())
```
(`AdapterContractDrift` `INGESTION_FAILED` rows have no clean resolving terminal — `resolved=false` is honest; they clear when the operator dispositions, which the undispositioned section already tracks. Do not invent a resolver.)

- [ ] **Step 5: Add `render_escalation_audit()`** (mirror `render_platform_health`'s row+expander structure exactly):

```python
def render_escalation_audit() -> None:
    """Escalation & integrity audit — the console's view of the
    Escalation & Hardening Ladder (rung-1/3) + Data Supervisor +
    auditheal layer. Read-only render of existing SoT (#189)."""
    hl, hr = st.columns([6, 1])
    hl.subheader("Escalation & data-integrity audit")
    if hr.button("↻ Refresh", help="Force-refresh (clears the 3-min cache)",
                 key="esc_force_refresh"):
        _fetch_escalation_state_cached.clear()
        st.rerun()
    try:
        e = _fetch_escalation_state_cached()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch escalation state: {exc}")
        return

    from dashboard_components.escalation import (
        classify_cross_table_audit,
        classify_recent_escalations,
        classify_source_holds,
        classify_undispositioned,
    )

    for label, key, rows, fn, detail_title in (
        ("Source holds (Data Supervisor)", "src_holds",
         e["source_holds"], classify_source_holds, "Held sources"),
        ("Undispositioned escalations (rung-3)", "undisp",
         e["undispositioned"], classify_undispositioned,
         "Undispositioned — disposition each"),
        ("Cross-table audit (auditheal)", "ct_audit",
         e["cross_table_audit"], classify_cross_table_audit,
         "Per cross-table check"),
        ("Recent escalations (7d)", "recent_esc",
         e["recent_escalations"], classify_recent_escalations,
         "Recent escalations"),
    ):
        c, s, d = fn(rows)
        _render_health_row(label, c, s, row_key=key)
        if c != "green" and d:
            with st.expander(detail_title, expanded=(c == "red")):
                for dl, dc, dt in d:
                    _render_health_row(dl, dc, dt)
```

- [ ] **Step 6: Call it in the Health tab** — in the `with tab_health:` block, immediately after `render_platform_health()`, add:
```python
        st.divider()
        render_escalation_audit()
```

- [ ] **Step 7: Verify.**
  - `source .venv/bin/activate && python -c "import ast; ast.parse(open('dashboard.py').read()); print('dashboard.py parse OK')"`.
  - Import smoke: `python -c "import importlib.util,sys; s=importlib.util.spec_from_file_location('dash','dashboard.py'); m=importlib.util.module_from_spec(s); sys.modules['dash']=m; s.loader.exec_module(m); print('dashboard import OK', hasattr(m,'render_escalation_audit'), hasattr(m,'_fetch_escalation_state'))"` → `dashboard import OK True True`. (If Streamlit bare-mode import emits warnings but no exception, that's acceptable — there is NO headless render harness; the test bar is import + the P1 classifier units, stated honestly.)
  - `ruff check dashboard.py` → clean (the two `# noqa: BLE001` on the broad `except Exception` match the file's existing fetch-guard precedent — confirm `git diff` adds no OTHER noqa).
  - `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` → collection clean.
  - `grep -n "classify_cross_ref" dashboard.py` → returns nothing (call site swapped; the stale classifier is no longer referenced from dashboard.py).

- [ ] **Step 8: Commit**

```bash
test "$(git branch --show-current)" = "feat/dash-escalation-p2" || { echo WRONG; exit 1; }
git add dashboard.py
git commit -m "feat(dashboard): escalation audit panel in Health tab + repoint cross_ref to persisted SoT"
```
STOP. Report DONE (parse + import smoke True/True, ruff, collection, the cross_ref-gone grep, commit SHA) or BLOCKED (quote the real `_q_cross_ref`/render-site code if its structure differs from Steps 2–3).

---

## Phase 3 — docs reconciliation (PR 3)

Branch: `docs/dash-escalation-p3` off fresh `main`.

### Task 3.1: reconcile docs

**Files:** `docs/superpowers/specs/2026-05-13-operator-dashboard.md`, `CLAUDE.md`, the #189 spec.

- [ ] **Step 1: `docs/superpowers/specs/2026-05-13-operator-dashboard.md`** — `grep -n "Health\|panel\|cross-ref\|validation\|heuristic" docs/superpowers/specs/2026-05-13-operator-dashboard.md`. Add a short subsection documenting the new "Escalation & data-integrity audit" Health-tab panel + the **read-only-renderer-of-SoT principle** (the console reuses `build_weekly_digest`/the platform red predicates, never reimplements — this is why the panel cannot drift again). Note the cross-ref row is now sourced from the persisted `cross_table_audit.*` SoT (was the pre-session inline COUNT shape). Match the doc's existing style; no emojis.

- [ ] **Step 2: `CLAUDE.md`** — `grep -n "dashboard\|operator console\|run_dashboard" CLAUDE.md`. Add/extend one line: the operator console's Health tab now surfaces the Escalation & Hardening Ladder / Data Supervisor / auditheal state via a read-only panel that renders the existing SoT (no recomputation); pointer to the #189 spec. Surgical, no emojis.

- [ ] **Step 3: #189 spec status** — set `**Status:**` to begin `**Status:** BUILT 2026-05-18` (keep lineage); append `**Build record:**` mirroring `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md`: P1 pure classifiers dark (PR #<p1>); P2 panel wiring + cross_ref repoint (PR #<p2>); P3 docs (this). (Controller fills PR #s at merge.)

- [ ] **Step 4: Verify scope + commit**

```bash
source .venv/bin/activate && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1   # docs-only: collects clean
git diff --stat   # exactly the 3 docs files
test "$(git branch --show-current)" = "docs/dash-escalation-p3" || { echo WRONG; exit 1; }
git add docs/superpowers/specs/2026-05-13-operator-dashboard.md CLAUDE.md docs/superpowers/specs/2026-05-18-dashboard-escalation-audit-panel-design.md
git commit -m "docs(dashboard): operator-console spec + CLAUDE.md + #189 spec BUILT"
```
STOP. Report DONE.

---

## Self-Review

**1. Spec coverage:**
- Spec §1 read-only-renderer principle → P1 classifiers are pure (take pre-fetched rows / the verbatim undispositioned list, recompute nothing); module docstring states it; `classify_undispositioned` renders `build_weekly_digest().undispositioned` verbatim (test asserts `detail[0][2] == items[0]`). ✓
- Spec §2 reused SoT → P2 `_fetch_escalation_state` calls `build_weekly_digest(pool)` (undispositioned), the datasupervisor open-hold anti-join, the `cross_table_audit.%` latest-per-source roll-up, the recent-escalation reads — no predicate rewritten. ✓
- Spec §3 four classifiers, health.py tuple shape → Task 1.1 (`classify_source_holds/undispositioned/cross_table_audit/recent_escalations` → `(color,summary,detail)`). ✓
- Spec §4 thin wiring, panel in Health tab after platform_health, cached like `_fetch_platform_health_cached` → Task 2.1 Steps 4–6 (ttl=180 `@st.cache_data`, `.clear()` refresh, Health-tab call). ✓
- Spec §5 the two drifts → Step 2 repoint `_q_cross_ref`→`cross_table_audit.%`, Step 3 swap to `classify_cross_table_audit`, `classify_validation` left unchanged. ✓
- Spec §6 non-goals → no `dashboard.py` decomposition; no recompute; no new tab; no new write path (panel surfaces; disposition stays the CLI verb — `classify_undispositioned.summary` only *prints the command*, adds no button); read-only. ✓
- Spec §7 phasing → 3 phases, 1 PR each. ✓
- Spec §8 read-don't-guess: tests in top-level `tests/` (verified); `ops.weekly_digest` cycle-safe (verified, isolated to P2 runtime); exact `_q_cross_ref`/render-site lines + label/row_key preserved; cache decorator/TTL/.clear() mirrored; `_render_health_row`/tuple shape mirrored. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has full code. P3 PR-number placeholders are controller-supplied-at-merge (explicit). The honest "no Streamlit render harness — bar is import smoke + P1 units" is stated, not a hidden gap.

**3. Type consistency:** classifiers `(color:str, summary:str, detail:list[tuple[str,str,str]])` consistent across `escalation.py`, the tests, and `render_escalation_audit`'s `c,s,d` + `for dl,dc,dt in d` loop; `_render_health_row(label,color,text,*,row_key=)` matches the verified signature; `_fetch_escalation_state()->dict` keys (`undispositioned`/`source_holds`/`cross_table_audit`/`recent_escalations`) match what `render_escalation_audit` indexes and what each classifier expects; `build_weekly_digest(pool).undispositioned: list[str]` consumed verbatim. The cross_ref repoint keeps `h["cross_ref"]` key + `row_key="cross_ref"` (no ripple).

(Carried to execution: the spec-compliance reviewer MUST verify P1 has ZERO `ops`/`dashboard`/Streamlit import (purity = the anti-drift guarantee + unit-testability) and that `classify_undispositioned` renders the digest string verbatim — not re-parsed/re-derived, which would reintroduce the exact drift this fixes.)
