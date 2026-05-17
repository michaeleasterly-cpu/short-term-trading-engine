# Data-Lane Escalation & Hardening Ladder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the rung-3 gap — make every data-lane escalation class carry a recorded disposition (converted | structural | removed) enforced by a clockwork build test, and surface undispositioned live escalation instances in the existing weekly digest.

**Architecture:** `tpcore/ladder/disposition.py` enumerates the full data-lane escalation-class set by DERIVING dispositions from the existing rung-2 registries (`HEAL_SPECS`/`REMEDIATION_SPECS`/`ADAPTER_CONTRACTS` — no duplicate SoT) plus an explicit `DISPOSITION_POLICIES` registry for the non-rung-2 classes (the 2 escalation event types + audit known_knowns checks); a clockwork drift-test fails the build if a class lacks a disposition. `ops/weekly_digest.py` gains one section listing open undispositioned escalation instances + a `disposition` CLI verb (mirrors `ack`); enforcement reuses the digest's existing non-skippable ack + ≥2-unacked→live-de-escalation teeth (no new gate/daemon/table).

**Tech Stack:** Python 3.11, pydantic v2 frozen models, pytest (`asyncio_mode=auto`), ruff. Spec: `docs/superpowers/specs/2026-05-17-data-escalation-hardening-ladder-design.md`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `tpcore/ladder/__init__.py` | package marker + re-exports (mirror `tpcore/auditheal/__init__.py`) | P1 |
| `tpcore/ladder/disposition.py` | `Disposition` enum, `DispositionPolicy`, `DISPOSITION_POLICIES`, `data_lane_escalation_classes()`, `policy_for()`, `disposition_drift()` | P1 |
| `tpcore/tests/test_ladder_disposition.py` | derivation + clockwork drift tests | P1 |
| `ops/weekly_digest.py` | `undispositioned` digest section + open-escalation read + `disposition` CLI verb | P2 |
| `tests/test_weekly_digest_disposition.py` | fake-pool: open vs resolved vs dispositioned; section render; CLI verb | P2 |
| `docs/ESCALATION_HARDENING_LADDER.md` | canonical data-lane convention doc | P3 |
| `CLAUDE.md`, spec, memory pointer | reconciliation | P3 |

One phase = one gated PR. Branch off fresh `main` per phase; CI green before merge; verify branch before every commit. Implementers commit only; controller opens/merges PRs after spec + code-quality review (the prior data-lane pattern).

---

## Ground truth (verified from source — do not re-derive/guess)

**Rung-2 registries + derivation fields:**
- `tpcore/selfheal/registry.py`: `HEAL_SPECS: dict[str, HealSpec]` keyed by `check_name`. `HealSpec` (`tpcore/selfheal/spec.py`): `.healable: bool`, `.unhealable_reason: str`, `.stage` (the bounded-repair stage). Derivation: `healable=True` → `AUTO_CONVERTED` (capability pointer = `stage`); `healable=False` → `ESCALATE_OPERATOR` (reason = `unhealable_reason`).
- `tpcore/auditheal/registry.py`: `REMEDIATION_SPECS: dict[str, RemediationSpec]` keyed by `check_key`. `RemediationSpec` (`tpcore/auditheal/spec.py`): `.remediable: bool`, `.escalate_reason: str`, `.stage`. Derivation: `remediable=True` → `AUTO_CONVERTED` (`stage`); `False` → `ESCALATE_OPERATOR` (`escalate_reason`).
- `tpcore/ingestion/adapter_contract.py`: `ADAPTER_CONTRACTS: dict[str, AdapterContract]` keyed by `feed`. `AdapterContract`: `.guard_pending: bool`. A contract drift has NO auto-heal (the sentinel hard-stops + escalates) → ALL contract classes derive `ESCALATE_OPERATOR` (reason: `"adapter contract drift — escalate-only by design"` + `" (guard pending)"` when `guard_pending`).

**Non-rung-2 escalation classes (need an explicit `DISPOSITION_POLICIES` entry):**
- Event types: `ops/data_repair_service.ESCALATED_EVENT_TYPE == "DATA_REPAIR_ESCALATED"`; `tpcore/datasupervisor/state.ESCALATED_EVENT == "DATA_SOURCE_ESCALATED"`.
- `audit_data_pipeline.py` known_knowns check names — the implementer MUST re-derive from `run_known_knowns` (read the `check_name="..."` literals in that function, the same way the contract-sentinel build did). Expected set for cross-check (verify, do not trust blindly): `row_count, freshness, validation_status, ingestion_jobs, sentinel_basket, credit_spread_history, csv_archive_presence, shrinkage_detector, governor_enforcement, hy_spread_decommission, insider_sentiment_period`. (`validation_status` overlaps selfheal conceptually but is a distinct AUDIT-emitter class — it still needs an explicit disposition; derived-wins only applies when the SAME class key appears in a rung-2 registry, which these audit check names do not.)

**Class key namespacing** (no false-merge across the 4 sources — the datasupervisor precedent): `selfheal:<check_name>`, `auditheal:<check_key>`, `contract:<feed>`, `audit_kk:<check_name>`, `event:DATA_REPAIR_ESCALATED`, `event:DATA_SOURCE_ESCALATED`. The clockwork test asserts the registry's known set == this full namespaced union. "Derived wins" means: a `selfheal:`/`auditheal:`/`contract:` key is ALWAYS derived (never in the explicit registry); the explicit registry holds ONLY `audit_kk:` + `event:` keys → no overlap by construction (namespacing makes "derived wins" structural, not a runtime tiebreak).

**weekly_digest patterns (`ops/weekly_digest.py`, 374 lines):**
- `@dataclass class WeeklyDigest`: fields `iso_week, period_start, period_end, cutovers, self_heals, near_miss_gates, most_likely_wrong, generated_at`; `render()` uses `_section(title, items)`.
- `build_weekly_digest(pool, now)`: pure read, trailing 7 days (`start = now - timedelta(days=7)`), `_q(pool, sql, *args)` helper.
- ack: `_emit(pool, ACK_EVENT, msg, {"iso_week": wk})`; `ACK_EVENT="WEEKLY_DIGEST_ACK"`, `DIGEST_EVENT`; `_INSERT_SQL` mirrors db_handler `(engine, run_id, event_type, severity, message, data)`. `live_clearance()` = the teeth (≥`DEESCALATE_AFTER_WEEKS` consecutive unacked → not cleared). The CLI dispatch is at the bottom (`python -m ops.weekly_digest ack`).
- Escalation↔resolving-terminal pairs: `DATA_REPAIR_ESCALATED` ↔ `DATA_REPAIR_COMPLETE` correlate on `data->>'request_id'`; `DATA_SOURCE_ESCALATED` ↔ `DATA_SOURCE_CLEARED` correlate on `data->>'hold_id'`.
- Disposition record (lowest-blast, mirrors the ack pattern): a new event `DATA_ESCALATION_DISPOSITIONED` with `data = {schema, ref, disposition, note}` (`ref` = the request_id/hold_id). An escalation instance is "dispositioned" iff a `DATA_ESCALATION_DISPOSITIONED` row exists with matching `ref`. No new gate — the existing weekly ack + `live_clearance` teeth are unchanged; the new section just makes undispositioned escalations visible in the artifact the operator must read to ack.

`build_asyncpg_pool` is `from tpcore.db import build_asyncpg_pool`. pytest `asyncio_mode=auto`.

---

## Phase 1 — `tpcore/ladder/disposition.py` + clockwork test, dark (PR 1)

Branch: `feat/ladder-p1`.

### Task 1.1: disposition SoT (derive from rung-2 + explicit for the rest)

**Files:**
- Create: `tpcore/ladder/__init__.py`
- Create: `tpcore/ladder/disposition.py`
- Test: `tpcore/tests/test_ladder_disposition.py`

- [ ] **Step 1: Write the failing test** — create `tpcore/tests/test_ladder_disposition.py`:

```python
"""Unit tests for the data-lane Ladder disposition SoT (rung-3)."""
from __future__ import annotations

from tpcore.ladder.disposition import (
    DISPOSITION_POLICIES,
    Disposition,
    data_lane_escalation_classes,
    disposition_drift,
    policy_for,
)
from tpcore.auditheal.registry import REMEDIATION_SPECS
from tpcore.ingestion.adapter_contract import ADAPTER_CONTRACTS
from tpcore.selfheal.registry import HEAL_SPECS


def test_no_drift_full_class_set_covered() -> None:
    missing, extra = disposition_drift()
    assert missing == set(), f"escalation classes with no disposition: {missing}"
    assert extra == set(), f"disposition entries for unknown classes: {extra}"


def test_classes_are_namespaced_union() -> None:
    classes = data_lane_escalation_classes()
    assert {f"selfheal:{k}" for k in HEAL_SPECS} <= classes
    assert {f"auditheal:{k}" for k in REMEDIATION_SPECS} <= classes
    assert {f"contract:{k}" for k in ADAPTER_CONTRACTS} <= classes
    assert "event:DATA_REPAIR_ESCALATED" in classes
    assert "event:DATA_SOURCE_ESCALATED" in classes


def test_selfheal_healable_derives_auto_converted() -> None:
    # prices_daily_completeness is healable=True (stage daily_bars).
    p = policy_for("selfheal:prices_daily_completeness")
    assert p.disposition is Disposition.AUTO_CONVERTED
    assert p.capability  # the stage pointer, non-empty
    assert p.derived is True


def test_selfheal_unhealable_derives_escalate_operator() -> None:
    # row_integrity is healable=False (corruption class).
    p = policy_for("selfheal:row_integrity")
    assert p.disposition is Disposition.ESCALATE_OPERATOR
    assert p.reason  # the unhealable_reason, non-empty
    assert p.derived is True


def test_contract_classes_derive_escalate_operator() -> None:
    any_feed = next(iter(ADAPTER_CONTRACTS))
    p = policy_for(f"contract:{any_feed}")
    assert p.disposition is Disposition.ESCALATE_OPERATOR
    assert p.derived is True


def test_event_classes_are_explicit_not_derived() -> None:
    p = policy_for("event:DATA_SOURCE_ESCALATED")
    assert p.derived is False
    assert p.disposition in set(Disposition)
    assert "event:DATA_SOURCE_ESCALATED" in DISPOSITION_POLICIES


def test_explicit_registry_only_holds_non_rung2_keys() -> None:
    # DRY: explicit registry must NOT redeclare any selfheal:/auditheal:
    # /contract: key (those are derived; derived-wins is structural).
    for key in DISPOSITION_POLICIES:
        assert key.startswith(("audit_kk:", "event:")), key


def test_policy_for_unknown_raises() -> None:
    import pytest
    with pytest.raises(KeyError):
        policy_for("selfheal:does_not_exist")
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.ladder`).

Run: `source .venv/bin/activate && python -m pytest tpcore/tests/test_ladder_disposition.py -q`

- [ ] **Step 3: Create `tpcore/ladder/__init__.py`**

```python
"""Data-lane Escalation & Hardening Ladder (rung-3 forcing function).

Codifies the principle: every data-lane escalation terminates in
converted | structural | removed — never by loosening an agent. The
disposition SoT is DERIVED from the rung-2 registries
(HEAL_SPECS/REMEDIATION_SPECS/ADAPTER_CONTRACTS — no duplicate SoT)
plus an explicit registry for the non-rung-2 classes.
"""
from tpcore.ladder.disposition import (
    DISPOSITION_POLICIES,
    Disposition,
    DispositionPolicy,
    data_lane_escalation_classes,
    disposition_drift,
    policy_for,
)

__all__ = [
    "DISPOSITION_POLICIES",
    "Disposition",
    "DispositionPolicy",
    "data_lane_escalation_classes",
    "disposition_drift",
    "policy_for",
]
```

- [ ] **Step 4: Create `tpcore/ladder/disposition.py`**

```python
"""Data-lane escalation disposition SoT + clockwork drift.

Every data-lane escalation CLASS must carry a disposition: how it
terminates (converted | structural | removed) or that it is honestly
escalate-operator. Rung-2-covered classes (selfheal/auditheal/
contract) DERIVE their disposition from the existing registries (no
duplicate SoT); the non-rung-2 classes (the audit known_knowns checks
+ the two escalation event types) are declared explicitly here. A
clockwork test asserts the union is fully covered — a new escalation
class fails the build until a disposition decision is recorded.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from tpcore.auditheal.registry import REMEDIATION_SPECS
from tpcore.ingestion.adapter_contract import ADAPTER_CONTRACTS
from tpcore.selfheal.registry import HEAL_SPECS


class Disposition(str, Enum):
    AUTO_CONVERTED = "auto_converted"     # a bounded capability terminates it
    ESCALATE_OPERATOR = "escalate_operator"  # honest; operator dispositions
    STRUCTURAL = "structural"             # terminated by a structural fix
    REMOVED = "removed"                   # source removed from live capital


class DispositionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cls: str                 # namespaced class key
    disposition: Disposition
    derived: bool            # True = read from a rung-2 registry
    capability: str = ""     # AUTO_CONVERTED: the bounded-repair pointer
    reason: str = ""         # ESCALATE_OPERATOR/STRUCTURAL/REMOVED: why
    evidence: str = ""       # explicit entries: how the decision was made


# The audit known_knowns FAIL check names (re-derived from
# scripts/audit_data_pipeline.py run_known_knowns — see plan ground
# truth; a P1 step asserts this matches the live source).
_AUDIT_KK_CHECKS: tuple[str, ...] = (
    "row_count", "freshness", "validation_status", "ingestion_jobs",
    "sentinel_basket", "credit_spread_history", "csv_archive_presence",
    "shrinkage_detector", "governor_enforcement", "hy_spread_decommission",
    "insider_sentiment_period",
)

# Explicit registry: ONLY non-rung-2 classes (audit_kk: + event:). A
# rung-2 key here would violate DRY (derived-wins is structural via
# namespacing) — the drift test enforces that.
DISPOSITION_POLICIES: dict[str, DispositionPolicy] = {
    "event:DATA_REPAIR_ESCALATED": DispositionPolicy(
        cls="event:DATA_REPAIR_ESCALATED",
        disposition=Disposition.ESCALATE_OPERATOR, derived=False,
        reason="data_repair_service exhausted bounded self-heal for a "
               "request; the operator dispositions each open instance "
               "via the weekly digest (rung-3 instance teeth).",
        evidence="ops/data_repair_service.ESCALATED_EVENT_TYPE; "
                 "resolving terminal DATA_REPAIR_COMPLETE."),
    "event:DATA_SOURCE_ESCALATED": DispositionPolicy(
        cls="event:DATA_SOURCE_ESCALATED",
        disposition=Disposition.ESCALATE_OPERATOR, derived=False,
        reason="datasupervisor escalated a source held ≥ M cycles; "
               "operator dispositions each open instance via the "
               "weekly digest.",
        evidence="tpcore/datasupervisor/state.ESCALATED_EVENT; "
                 "resolving terminal DATA_SOURCE_CLEARED."),
    **{
        f"audit_kk:{c}": DispositionPolicy(
            cls=f"audit_kk:{c}",
            disposition=Disposition.ESCALATE_OPERATOR, derived=False,
            reason="audit_data_pipeline known_knowns FAIL — hard-gated "
                   "(no DATA_OPERATIONS_COMPLETE); operator investigates "
                   "+ dispositions (convert to a bounded check / "
                   "structural fix / remove the source).",
            evidence="scripts/audit_data_pipeline.py run_known_knowns "
                     f"check_name={c!r}.")
        for c in _AUDIT_KK_CHECKS
    },
}


def _derive(cls: str) -> DispositionPolicy | None:
    if cls.startswith("selfheal:"):
        spec = HEAL_SPECS.get(cls.removeprefix("selfheal:"))
        if spec is None:
            return None
        if spec.healable:
            return DispositionPolicy(
                cls=cls, disposition=Disposition.AUTO_CONVERTED,
                derived=True, capability=f"ops.py --stage {spec.stage}")
        return DispositionPolicy(
            cls=cls, disposition=Disposition.ESCALATE_OPERATOR,
            derived=True, reason=spec.unhealable_reason)
    if cls.startswith("auditheal:"):
        spec = REMEDIATION_SPECS.get(cls.removeprefix("auditheal:"))
        if spec is None:
            return None
        if spec.remediable:
            return DispositionPolicy(
                cls=cls, disposition=Disposition.AUTO_CONVERTED,
                derived=True, capability=f"ops.py --stage {spec.stage}")
        return DispositionPolicy(
            cls=cls, disposition=Disposition.ESCALATE_OPERATOR,
            derived=True, reason=spec.escalate_reason)
    if cls.startswith("contract:"):
        c = ADAPTER_CONTRACTS.get(cls.removeprefix("contract:"))
        if c is None:
            return None
        suffix = " (guard pending)" if c.guard_pending else ""
        return DispositionPolicy(
            cls=cls, disposition=Disposition.ESCALATE_OPERATOR,
            derived=True,
            reason=f"adapter contract drift — escalate-only by design"
                   f"{suffix}")
    return None


def data_lane_escalation_classes() -> set[str]:
    """Full namespaced known set: rung-2 registry keys (derived) ∪ the
    explicit non-rung-2 keys."""
    out: set[str] = set()
    out |= {f"selfheal:{k}" for k in HEAL_SPECS}
    out |= {f"auditheal:{k}" for k in REMEDIATION_SPECS}
    out |= {f"contract:{k}" for k in ADAPTER_CONTRACTS}
    out |= set(DISPOSITION_POLICIES)
    return out


def policy_for(cls: str) -> DispositionPolicy:
    """Derived (rung-2) wins; else the explicit registry. KeyError if
    the class is unknown (an unknown escalation class must never be
    silently dispositioned)."""
    derived = _derive(cls)
    if derived is not None:
        return derived
    pol = DISPOSITION_POLICIES.get(cls)
    if pol is None:
        raise KeyError(f"no disposition for escalation class {cls!r}")
    return pol


def disposition_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) — classes with no resolvable policy, and
    explicit entries for unknown/rung-2 classes. Both empty == covered."""
    known = data_lane_escalation_classes()
    missing = {c for c in known if _resolvable(c) is False}
    # explicit entries must be a subset of known AND non-rung-2
    rung2 = (
        {f"selfheal:{k}" for k in HEAL_SPECS}
        | {f"auditheal:{k}" for k in REMEDIATION_SPECS}
        | {f"contract:{k}" for k in ADAPTER_CONTRACTS}
    )
    extra = {k for k in DISPOSITION_POLICIES if k not in known or k in rung2}
    return missing, extra


def _resolvable(cls: str) -> bool:
    try:
        policy_for(cls)
        return True
    except KeyError:
        return False


__all__ = [
    "DISPOSITION_POLICIES",
    "Disposition",
    "DispositionPolicy",
    "data_lane_escalation_classes",
    "disposition_drift",
    "policy_for",
]
```

- [ ] **Step 5: Re-derive the audit known_knowns set from source + assert it matches.** Append this test to `tpcore/tests/test_ladder_disposition.py`:

```python
def test_audit_kk_checks_match_live_source() -> None:
    # Re-derive run_known_knowns' check_name literals from source; the
    # explicit _AUDIT_KK_CHECKS must equal them (read-don't-guess: a
    # new known_knowns check must fail this until dispositioned).
    import pathlib
    import re
    from tpcore.ladder.disposition import _AUDIT_KK_CHECKS

    src = pathlib.Path("scripts/audit_data_pipeline.py").read_text()
    # run_known_knowns spans from its def to the next "async def ".
    m = re.search(r"async def run_known_knowns\(.*?\n(?=async def )",
                  src, re.S)
    body = m.group(0) if m else src
    found = set(re.findall(r'check_name="([a-z_]+)"', body))
    # Some known_knowns findings are emitted by helpers called from
    # run_known_knowns (e.g. shrinkage_detector) — union those by
    # scanning the whole file for phase="known_knowns" check_names.
    kk = set(re.findall(
        r'phase="known_knowns",\s*check_name="([a-z_]+)"', src))
    live = found | kk
    assert set(_AUDIT_KK_CHECKS) == live, (
        f"_AUDIT_KK_CHECKS drift: missing={live - set(_AUDIT_KK_CHECKS)} "
        f"extra={set(_AUDIT_KK_CHECKS) - live}")
```

If this fails, the live known_knowns set differs — update `_AUDIT_KK_CHECKS` (and the explicit entries are generated from it, so coverage stays correct) to match the SOURCE; do NOT edit the test to pass. (This is the read-don't-guess guard; a new audit check now fails the build until dispositioned.)

- [ ] **Step 6: Run all tests** — `python -m pytest tpcore/tests/test_ladder_disposition.py -q` → all 8 pass. Fix implementation only.

- [ ] **Step 7: Lint + collection** — `ruff check tpcore/ladder/ tpcore/tests/test_ladder_disposition.py` (clean, no noqa) and `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` (collection clean).

- [ ] **Step 8: Commit**

```bash
test "$(git branch --show-current)" = "feat/ladder-p1" || { echo WRONG; exit 1; }
git add tpcore/ladder/__init__.py tpcore/ladder/disposition.py tpcore/tests/test_ladder_disposition.py
git commit -m "feat(ladder): rung-3 disposition SoT — derived from rung-2 + clockwork drift (dark)"
```
STOP. Report DONE (8/8, the audit-kk re-derivation result, ruff, collection, commit SHA) or BLOCKED (if `_AUDIT_KK_CHECKS` differs from source — report the diff).

---

## Phase 2 — weekly-digest instance teeth (PR 2)

Branch: `feat/ladder-p2` off fresh `main`.

### Task 2.1: undispositioned-escalations section + `disposition` CLI verb

**Files:**
- Modify: `ops/weekly_digest.py`
- Test: `tests/test_weekly_digest_disposition.py`

- [ ] **Step 1: Read the current shape.** `sed -n '40,130p' ops/weekly_digest.py` and the CLI dispatch at the bottom (`grep -n "argv\|sys.argv\|\"ack\"\|def main\|__main__\|emit_digest\|ack_digest" ops/weekly_digest.py`). Confirm: `WeeklyDigest` dataclass fields, `_section`, `_q`, `_emit`, `_INSERT_SQL`, `ACK_EVENT`, the `python -m ops.weekly_digest <verb>` dispatch.

- [ ] **Step 2: Write the failing test** — create `tests/test_weekly_digest_disposition.py`:

```python
"""The weekly digest surfaces OPEN undispositioned data-lane
escalations; a recorded DATA_ESCALATION_DISPOSITIONED clears them."""
from __future__ import annotations

import importlib.util
import pathlib
from datetime import UTC, datetime

# ops/ is a package shadowed by scripts/ops.py in some paths; load the
# module file directly (the established data-lane test dodge).
_spec = importlib.util.spec_from_file_location(
    "wd", pathlib.Path("ops/weekly_digest.py"))
wd = importlib.util.module_from_spec(_spec)
import sys
sys.modules["wd"] = wd
_spec.loader.exec_module(wd)


class _Conn:
    def __init__(self, rows_by_marker):
        self._m = rows_by_marker
        self.emitted = []

    async def fetch(self, sql, *a):
        for marker, rows in self._m.items():
            if marker in sql:
                return [dict(r) for r in rows]
        return []

    async def execute(self, sql, *a):
        self.emitted.append(a)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, rows_by_marker=None):
        self.conn = _Conn(rows_by_marker or {})

    def acquire(self): return _CM(self.conn)


async def test_open_escalation_listed_resolved_excluded() -> None:
    pool = _Pool({
        # one open DATA_SOURCE_ESCALATED (no later CLEARED, no disp)
        "OPEN_ESCALATIONS": [
            {"ref": "h1", "etype": "DATA_SOURCE_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "source prices_daily stuck"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    assert any("prices_daily" in x for x in d.undispositioned)
    txt = d.render()
    assert "UNDISPOSITIONED" in txt.upper()


async def test_disposition_cli_emits_event() -> None:
    pool = _Pool()
    rc = await wd.disposition_escalation(
        pool, "h1", "converted", "added HealSpec X")
    assert rc == 0
    (a,) = [e for e in pool.conn.emitted]
    # _INSERT_SQL positional args: (engine, run_id, event_type, sev,
    # message, data) — event_type at idx 2, data json at idx 5.
    assert a[2] == "DATA_ESCALATION_DISPOSITIONED"
    assert "h1" in a[5] and "converted" in a[5]


async def test_invalid_disposition_rejected() -> None:
    pool = _Pool()
    rc = await wd.disposition_escalation(pool, "h1", "bogus", "")
    assert rc != 0 and pool.conn.emitted == []
```

- [ ] **Step 3: Run — expect FAIL** (`AttributeError: module 'wd' has no attribute 'disposition_escalation'` / `undispositioned`).

- [ ] **Step 4: Add the `undispositioned` field + section.** In `ops/weekly_digest.py`:

(a) Add `undispositioned: list[str]` to the `WeeklyDigest` dataclass (after `near_miss_gates`).

(b) In `render()`, add a section line after the near-miss section, before "MOST LIKELY SILENTLY WRONG":

```python
            *_section(
                f"UNDISPOSITIONED DATA-LANE ESCALATIONS "
                f"({len(self.undispositioned)}) — rung-3: each MUST be "
                f"converted | structural | removed:",
                self.undispositioned,
            ),
```

(c) In `build_weekly_digest`, add this read before the `return WeeklyDigest(...)` and pass `undispositioned=undispositioned`:

```python
    # Rung-3 instance teeth: OPEN escalations (escalation event with no
    # resolving terminal AND no DATA_ESCALATION_DISPOSITIONED for its
    # ref) older than the digest window's start. Reuses the existing
    # one-terminal-liveness anti-join shape.
    open_esc = await _q(
        pool,
        """
        WITH esc AS (
          SELECT e.data->>'request_id' AS ref, 'DATA_REPAIR_ESCALATED' AS etype,
                 e.recorded_at, e.message
          FROM platform.application_log e
          WHERE e.event_type = 'DATA_REPAIR_ESCALATED'
          UNION ALL
          SELECT e.data->>'hold_id' AS ref, 'DATA_SOURCE_ESCALATED' AS etype,
                 e.recorded_at, e.message
          FROM platform.application_log e
          WHERE e.event_type = 'DATA_SOURCE_ESCALATED'
        )
        SELECT ref, etype, recorded_at, message FROM esc x
        WHERE x.ref IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log t
            WHERE t.event_type IN ('DATA_REPAIR_COMPLETE','DATA_SOURCE_CLEARED')
              AND (t.data->>'request_id' = x.ref OR t.data->>'hold_id' = x.ref)
              AND t.recorded_at > x.recorded_at)
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log dp
            WHERE dp.event_type = 'DATA_ESCALATION_DISPOSITIONED'
              AND dp.data->>'ref' = x.ref)
          AND x.recorded_at < $1
        ORDER BY x.recorded_at
        """,
        start,  # the existing 7-day window start = the grace bound
    )
    undispositioned = [
        f"{r['recorded_at']:%Y-%m-%d} [{r['etype']}] ref={r['ref']} "
        f"{r['message']}" for r in open_esc
    ]
```
The fake `_Conn.fetch` matches the marker substring `OPEN_ESCALATIONS`; add that exact token as a SQL comment on the query's first line so the test routes it: make the query string start with `-- OPEN_ESCALATIONS\n`. (Other `_q` calls in the file have their own distinct SQL; this comment only affects routing in the fake.)

(d) Add the CLI function + verb. Add near `ack_digest`:

```python
_VALID_DISPOSITIONS = {"converted", "structural", "removed"}


async def disposition_escalation(
    pool: Any, ref: str, disposition: str, note: str
) -> int:
    """Record an operator disposition for an open escalation instance.
    Mirrors ack_digest's emit pattern. Returns 0 on success, 1 on a
    bad disposition value (must be converted|structural|removed)."""
    if disposition not in _VALID_DISPOSITIONS:
        logger.error("weekly_digest.bad_disposition", value=disposition)
        return 1
    await _emit(
        pool, "DATA_ESCALATION_DISPOSITIONED",
        f"escalation {ref} dispositioned: {disposition}",
        {"schema": 1, "ref": ref, "disposition": disposition, "note": note},
    )
    logger.info("weekly_digest.dispositioned", ref=ref,
                disposition=disposition)
    return 0
```

(e) Wire the CLI verb into the existing `python -m ops.weekly_digest` dispatch (read the bottom of the file; it dispatches `ack`/`emit`-style verbs). Add a `disposition` verb: `python -m ops.weekly_digest disposition <ref> <converted|structural|removed> [note...]` → `await disposition_escalation(pool, ref, disp, " ".join(note))`, exit its return code. Match the file's existing arg-parsing/dispatch idiom exactly (do not introduce argparse if it uses `sys.argv` slicing).

- [ ] **Step 5: Run** — `source .venv/bin/activate && python -m pytest tests/test_weekly_digest_disposition.py -q` → 3 pass. Fix impl only.

- [ ] **Step 6: Regression + lint** — `python -m pytest tests/test_weekly_digest.py tpcore/tests/test_ladder_disposition.py -q` (the existing weekly-digest tests MUST still pass — the new field/section is additive) ; `ruff check ops/weekly_digest.py tests/test_weekly_digest_disposition.py` (clean, no noqa) ; `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` (collection clean).

- [ ] **Step 7: Commit**

```bash
test "$(git branch --show-current)" = "feat/ladder-p2" || { echo WRONG; exit 1; }
git add ops/weekly_digest.py tests/test_weekly_digest_disposition.py
git commit -m "feat(ladder): weekly-digest undispositioned-escalations section + disposition verb"
```
STOP. Report DONE (3/3 new + existing weekly-digest tests still green, ruff, collection, commit SHA) or BLOCKED (quote the real CLI-dispatch idiom if it differs).

---

## Phase 3 — convention doc + reconciliation (PR 3)

Branch: `docs/ladder-p3` off fresh `main`.

### Task 3.1: canonical doc + pointers

**Files:** Create `docs/ESCALATION_HARDENING_LADDER.md`; modify `CLAUDE.md`, the spec.

- [ ] **Step 1: Create `docs/ESCALATION_HARDENING_LADDER.md`** — the canonical data-lane convention. Content (write it in full, no placeholders):
  - **Principle** (verbatim from spec §1): every data-lane escalation terminates in converted | structural | removed; never loosen an agent; never silent best-effort.
  - **The 5 rungs, data-lane** (from spec §2 table): rung 1 fail-closed escalation (list the concrete events: selfheal/auditheal exit-gate, `DATA_REPAIR_ESCALATED`, `DATA_SOURCE_ESCALATED`, contract-drift `INGESTION_FAILED`, audit known_knowns FAIL); rung 2 coverage drift-tests (HealSpec/RemediationSpec/ADAPTER_CONTRACTS); rung 3 = this (the `tpcore/ladder` disposition SoT + clockwork drift + the weekly-digest instance teeth + the `disposition` verb); rung 4 structural removal (RiskGovernor kill-switch, `live_clearance`, DSR/credibility, provider RETIRE); rung 5 = Epic E, deferred (advisory/human-gated, out of scope).
  - **Disposition vocabulary** (spec §4.1): the 4 `Disposition` values + that rung-2 classes are DERIVED not redeclared.
  - **Operator workflow**: undispositioned escalations appear in the weekly digest; disposition with `python -m ops.weekly_digest disposition <ref> <converted|structural|removed> [note]`; the existing ≥2-unacked-weeks → live-de-escalation is the teeth (unchanged).
  - **Scope note**: data lane only; cross-lane unification is an operator cross-session decision, not implied here.

- [ ] **Step 2: CLAUDE.md** — `grep -n "Escalation\|self-heal\|Ladder\|Session Rules\|Conventions" CLAUDE.md`; add ONE line in the conventions/operator-workflow area pointing to `docs/ESCALATION_HARDENING_LADDER.md` as the canonical data-lane escalation contract (every data-lane escalation class carries a disposition; clockwork-enforced; instance teeth via the weekly digest). No emojis, surgical.

- [ ] **Step 3: Spec status** — set the spec `**Status:**` to begin `**Status:** BUILT 2026-05-17` (keep lineage); append a `**Build record:**` (mirror `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md`): P1 disposition SoT + clockwork (PR #<p1>); P2 weekly-digest teeth + verb (PR #<p2>); P3 doc (this). (Controller supplies PR #s at merge.)

- [ ] **Step 4: Verify scope + commit**

```bash
source .venv/bin/activate && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1   # docs-only: collects clean
git diff --stat   # exactly the 3 files (new doc + CLAUDE.md + spec)
test "$(git branch --show-current)" = "docs/ladder-p3" || { echo WRONG; exit 1; }
git add docs/ESCALATION_HARDENING_LADDER.md CLAUDE.md docs/superpowers/specs/2026-05-17-data-escalation-hardening-ladder-design.md
git commit -m "docs(ladder): canonical data-lane convention + CLAUDE.md pointer + spec BUILT"
```
STOP. Report DONE.

---

## Self-Review

**1. Spec coverage:**
- Spec §1 principle → Deliverable A doc (P3 Step 1) + module docstrings. ✓
- Spec §2 rung table (codify 1/2/4 by reference, 3 built, 5 out) → P3 doc. ✓
- Spec §4.1 vocabulary → `Disposition` enum (4 values) Task 1.1. ✓
- Spec §4.2 class SoT, DERIVE from rung-2 (no duplicate), explicit only for non-rung-2, clockwork drift → `_derive` (selfheal/auditheal/contract), `DISPOSITION_POLICIES` (only `event:`/`audit_kk:`), `disposition_drift`, `test_explicit_registry_only_holds_non_rung2_keys` + `test_no_drift_full_class_set_covered` + the audit-kk source re-derivation test. ✓
- Spec §4.3 instance teeth reuse weekly digest, open = escalation w/ no resolving terminal & no disposition, age-bounded, no new gate → P2 query (anti-join on the two escalation↔terminal pairs, `recorded_at < start`), `disposition_escalation` verb mirroring ack, existing `live_clearance` untouched. ✓
- Spec §5 non-goals (data only; no auto-convert; no new gate/daemon/table; no duplicate SoT; rung 5 out) → no engine/aar files; disposition records a decision only; reuses digest event reads; derived-from-rung-2; rung 5 absent. ✓
- Spec §6 phasing → 3 phases, 1 PR each. ✓
- Spec §7 read-don't-guess: audit-kk re-derived from source w/ a failing-guard test; event constants cited; weekly_digest patterns read in P2 Step 1; rung-2 overlap dedup structural via namespacing + `disposition_drift` `extra` check. ✓

**2. Placeholder scan:** No TBD/TODO. P3 PR-number placeholders are controller-supplied-at-merge (explicit), not gaps. Every code step has full code.

**3. Type consistency:** `Disposition` (4 enum values), `DispositionPolicy(cls, disposition, derived, capability, reason, evidence)`, `data_lane_escalation_classes()->set[str]`, `policy_for(cls)->DispositionPolicy` (KeyError unknown), `disposition_drift()->(missing,extra)`, `disposition_escalation(pool, ref, disposition, note)->int`, event `DATA_ESCALATION_DISPOSITIONED` with `data={schema,ref,disposition,note}`, namespaced keys `selfheal:`/`auditheal:`/`contract:`/`audit_kk:`/`event:` — consistent across Task 1.1, 2.1, tests, and P3 doc. The P2 `_INSERT_SQL` arg positions (event_type idx 2, data idx 5) match the verified `weekly_digest._emit`/`_INSERT_SQL`.

(Carried to execution: the spec-compliance reviewer MUST verify `disposition_drift().extra` genuinely rejects a rung-2 key wrongly placed in `DISPOSITION_POLICIES` — the DRY/no-duplicate-SoT guarantee — and that the audit-kk source re-derivation test fails if `_AUDIT_KK_CHECKS` drifts from `run_known_knowns` (the read-don't-guess guard).)
