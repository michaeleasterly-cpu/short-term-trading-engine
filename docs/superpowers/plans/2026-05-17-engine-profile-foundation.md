# Engine Profile Foundation (Sub-project A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `tpcore/engine_profile.py` — the single declarative SoT for *when an engine may fire* (cadence + market-closed + data-ready + not-already-run), composing the existing `capital_gate`. Pure library, landed dark.

**Architecture:** Flat-SoT pattern mirroring `tpcore/feeds/profile.py`: a `Cadence` StrEnum, a frozen `EngineProfile` pydantic model, a `_PROFILE` dict, `profile_for()`, and one `async should_fire(engine, now, pool) -> FireDecision` composing four short-circuit, fail-closed precondition checks. Zero scheduler/daemon/trade-path changes (Sub-project B wires it).

**Tech Stack:** Python 3.11, Pydantic v2, `tpcore.calendar` (XNYS), `tpcore.quality.validation.capital_gate`, asyncpg pool, pytest (`asyncio_mode=auto`).

---

## File Structure

- **Create:** `tpcore/engine_profile.py` — the entire module (enum, model, registry, lookups, helpers, `should_fire`). One responsibility: decide if an engine may fire now. ~150 lines.
- **Create:** `tpcore/tests/test_engine_profile.py` — all unit tests (pure; fake pool, injected `now`, monkeypatched `calendar`/`capital_gate`).
- **No other file is modified.** Acceptance: `git diff --stat origin/main...HEAD` shows exactly these two files.

Verified real APIs (file:line):
- `tpcore/calendar.py`: `is_trading_day(dt)->bool` (53), `session_contains(dt)->bool` (60), `first_session_of_month(year,month)->date` (135), `sessions_in_range(start,end)->list[date]` (152).
- `tpcore/quality/validation/capital_gate.py:148` `async assert_passed_for_engine(pool, engine, *, require_all_green=False, max_age_days=7) -> None` — RAISES `ValidationStaleError`/`ValidationFailedError` (defined :70/:74) on failure, returns None on pass.
- `tpcore/logging/db_handler.py`: run-start event literal `"STARTUP"` (115). `platform.application_log` cols: `engine`,`run_id`,`event_type`,`severity`,`message`,`data`,`recorded_at` (migration `20260511_0100`).
- Live roster (from `scripts/run_all_engines.sh:73`): `reversion vector momentum sentinel` (sigma archived; do NOT use `check_imports.ENGINE_PACKAGES` which still lists sigma).
- Test convention: `tpcore/tests/test_<x>.py`, bare `async def`/`def` (`pyproject.toml:80` `asyncio_mode=auto`). Coverage-test pattern: `tpcore/tests/test_selfheal.py::test_registry_in_lockstep_with_suite`.

---

### Task 1: Module skeleton — Cadence, EngineProfile, _PROFILE, profile_for + coverage guard

**Files:**
- Create: `tpcore/engine_profile.py`
- Test: `tpcore/tests/test_engine_profile.py`

- [ ] **Step 1: Write failing tests**

```python
# tpcore/tests/test_engine_profile.py
from tpcore.engine_profile import Cadence, EngineProfile, _PROFILE, profile_for


def test_profile_for_known_engines():
    assert profile_for("reversion").cadence is Cadence.DAILY
    assert profile_for("vector").cadence is Cadence.DAILY
    assert profile_for("sentinel").cadence is Cadence.DAILY
    assert profile_for("momentum").cadence is Cadence.MONTHLY_FIRST_TRADING_DAY
    assert profile_for("allocator").cadence is Cadence.WEEKLY_FIRST_TRADING_DAY


def test_profile_for_unknown_returns_none():
    assert profile_for("does_not_exist") is None


def test_profiles_are_frozen_and_self_consistent():
    for name, p in _PROFILE.items():
        assert isinstance(p, EngineProfile)
        assert p.engine == name
        with __import__("pytest").raises(Exception):
            p.cadence = Cadence.DAILY  # frozen


def test_profile_covers_live_engine_roster():
    # SoT: scripts/run_all_engines.sh:73 (sigma archived — excluded).
    live = {"reversion", "vector", "momentum", "sentinel"}
    missing = live - set(_PROFILE)
    assert not missing, f"engines without an EngineProfile: {missing}"
```

- [ ] **Step 2: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.engine_profile'`.

- [ ] **Step 3: Implement skeleton**

```python
# tpcore/engine_profile.py
"""Single source of truth for WHEN an engine may fire.

The event-driven model (operator directive 2026-05-17): an engine
fires the moment its preconditions hold — data ready + market closed +
its cadence boundary — never on a clock. Time is a GATE, never a
trigger. This module is the declarative SoT for those preconditions,
mirroring tpcore.feeds.profile / tpcore.risk.limits_profile. It
COMPOSES tpcore.quality.validation.capital_gate (the existing
per-engine data-readiness authority — called, never re-implemented).

Landed dark: nothing imports should_fire yet (Sub-project B wires the
engine_service to it). See
docs/superpowers/specs/2026-05-17-event-driven-engine-services-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from enum import StrEnum

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class Cadence(StrEnum):
    DAILY = "daily"
    MONTHLY_FIRST_TRADING_DAY = "monthly_first_trading_day"
    WEEKLY_FIRST_TRADING_DAY = "weekly_first_trading_day"


class EngineProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    engine: str
    cadence: Cadence
    market_closed_required: bool = True


_PROFILE: dict[str, EngineProfile] = {
    "reversion": EngineProfile(engine="reversion", cadence=Cadence.DAILY),
    "vector":    EngineProfile(engine="vector",    cadence=Cadence.DAILY),
    "sentinel":  EngineProfile(engine="sentinel",  cadence=Cadence.DAILY),
    "momentum":  EngineProfile(engine="momentum",  cadence=Cadence.MONTHLY_FIRST_TRADING_DAY),
    # allocator profile present (this is the SoT); consumed in Sub-project C.
    "allocator": EngineProfile(engine="allocator", cadence=Cadence.WEEKLY_FIRST_TRADING_DAY),
}


def profile_for(engine: str) -> EngineProfile | None:
    """The EngineProfile for an engine, or None if unprofiled."""
    return _PROFILE.get(engine)
```

- [ ] **Step 4: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "feat(engine_profile): Cadence/EngineProfile SoT skeleton + coverage guard

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `_cadence_boundary(profile, now)` — is `now` this engine's cadence point

**Files:**
- Modify: `tpcore/engine_profile.py` (add helper)
- Test: `tpcore/tests/test_engine_profile.py`

- [ ] **Step 1: Write failing tests**

```python
from datetime import UTC, datetime
from unittest.mock import patch
from tpcore.engine_profile import _cadence_boundary, profile_for

# 2026-05-04 is the first NYSE session of May 2026 (Mon); 2026-05-05 is not month-first.
# 2026-05-04 is a Monday → first session of that week too.

def test_daily_boundary_true_on_trading_day():
    with patch("tpcore.engine_profile.cal.is_trading_day", return_value=True):
        assert _cadence_boundary(profile_for("reversion"), datetime(2026,5,5,21,30,tzinfo=UTC)) is True

def test_daily_boundary_false_on_non_trading_day():
    with patch("tpcore.engine_profile.cal.is_trading_day", return_value=False):
        assert _cadence_boundary(profile_for("reversion"), datetime(2026,5,9,21,30,tzinfo=UTC)) is False

def test_monthly_boundary_true_only_on_first_session_of_month():
    from datetime import date
    with patch("tpcore.engine_profile.cal.first_session_of_month", return_value=date(2026,5,4)):
        p = profile_for("momentum")
        assert _cadence_boundary(p, datetime(2026,5,4,21,30,tzinfo=UTC)) is True
        assert _cadence_boundary(p, datetime(2026,5,5,21,30,tzinfo=UTC)) is False

def test_weekly_boundary_true_only_on_first_session_of_week():
    from datetime import date
    # week of 2026-05-04: sessions Mon..Fri; first = 2026-05-04
    with patch("tpcore.engine_profile.cal.sessions_in_range", return_value=[date(2026,5,4),date(2026,5,5)]):
        p = profile_for("allocator")
        assert _cadence_boundary(p, datetime(2026,5,4,13,0,tzinfo=UTC)) is True
        assert _cadence_boundary(p, datetime(2026,5,5,13,0,tzinfo=UTC)) is False
```

- [ ] **Step 2: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -k cadence_boundary`
Expected: FAIL — `ImportError: cannot import name '_cadence_boundary'`.

- [ ] **Step 3: Implement**

Add to `tpcore/engine_profile.py` (after imports add `from tpcore import calendar as cal`; then):

```python
def _week_start_date(d):
    """Monday of d's ISO week (date)."""
    from datetime import timedelta
    return d - timedelta(days=d.weekday())


def _cadence_boundary(profile: EngineProfile, now: datetime) -> bool:
    """True iff ``now``'s date is this profile's cadence boundary (XNYS)."""
    d = now.date()
    if profile.cadence is Cadence.DAILY:
        return cal.is_trading_day(now)
    if profile.cadence is Cadence.MONTHLY_FIRST_TRADING_DAY:
        return d == cal.first_session_of_month(d.year, d.month)
    if profile.cadence is Cadence.WEEKLY_FIRST_TRADING_DAY:
        wk_start = _week_start_date(d)
        sessions = cal.sessions_in_range(wk_start, d)
        return bool(sessions) and sessions[0] == d
    return False  # unknown cadence → fail-closed
```

- [ ] **Step 4: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -k cadence_boundary -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "feat(engine_profile): _cadence_boundary (daily/monthly/weekly via tpcore.calendar)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `_cadence_window_start(profile, now)` — start of the current cycle (for idempotency)

**Files:**
- Modify: `tpcore/engine_profile.py`
- Test: `tpcore/tests/test_engine_profile.py`

- [ ] **Step 1: Write failing tests**

```python
from datetime import UTC, datetime, date
from unittest.mock import patch
from tpcore.engine_profile import _cadence_window_start, profile_for

def test_daily_window_start_is_midnight_utc_of_now_date():
    ws = _cadence_window_start(profile_for("reversion"), datetime(2026,5,5,21,30,tzinfo=UTC))
    assert ws == datetime(2026,5,5,0,0,tzinfo=UTC)

def test_monthly_window_start_is_first_session_midnight():
    with patch("tpcore.engine_profile.cal.first_session_of_month", return_value=date(2026,5,4)):
        ws = _cadence_window_start(profile_for("momentum"), datetime(2026,5,4,21,30,tzinfo=UTC))
        assert ws == datetime(2026,5,4,0,0,tzinfo=UTC)

def test_weekly_window_start_is_week_first_session_midnight():
    with patch("tpcore.engine_profile.cal.sessions_in_range", return_value=[date(2026,5,4),date(2026,5,5)]):
        ws = _cadence_window_start(profile_for("allocator"), datetime(2026,5,5,13,0,tzinfo=UTC))
        assert ws == datetime(2026,5,4,0,0,tzinfo=UTC)
```

- [ ] **Step 2: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -k window_start`
Expected: FAIL — `ImportError: cannot import name '_cadence_window_start'`.

- [ ] **Step 3: Implement**

Add to `tpcore/engine_profile.py`:

```python
def _midnight_utc(d) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _cadence_window_start(profile: EngineProfile, now: datetime) -> datetime:
    """Start (UTC) of the cadence cycle containing ``now``.

    A run record at/after this instant means the engine already ran
    this cycle. Daily = midnight UTC of now's date; monthly = midnight
    of the month's first session; weekly = midnight of the week's
    first session.
    """
    d = now.date()
    if profile.cadence is Cadence.DAILY:
        return _midnight_utc(d)
    if profile.cadence is Cadence.MONTHLY_FIRST_TRADING_DAY:
        return _midnight_utc(cal.first_session_of_month(d.year, d.month))
    if profile.cadence is Cadence.WEEKLY_FIRST_TRADING_DAY:
        sessions = cal.sessions_in_range(_week_start_date(d), d)
        return _midnight_utc(sessions[0] if sessions else d)
    return _midnight_utc(d)  # unknown → narrowest safe window (today)
```

- [ ] **Step 4: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -k window_start -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "feat(engine_profile): _cadence_window_start for idempotency window

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `FireDecision` + `should_fire()` composition (fail-closed)

**Files:**
- Modify: `tpcore/engine_profile.py`
- Test: `tpcore/tests/test_engine_profile.py`

- [ ] **Step 1: Write failing tests**

```python
from datetime import UTC, datetime
from unittest.mock import patch, AsyncMock
import contextlib
from tpcore.engine_profile import should_fire, FireDecision


class _FakeConn:
    def __init__(self, ran: bool):
        self._ran = ran
    async def fetchval(self, *_a, **_k):
        return 1 if self._ran else None


class _FakePool:
    def __init__(self, ran: bool = False):
        self._ran = ran
    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._ran)


def _patch_all(*, boundary=True, closed=True, data_ok=True):
    cm = contextlib.ExitStack()
    cm.enter_context(patch("tpcore.engine_profile._cadence_boundary", return_value=boundary))
    cm.enter_context(patch("tpcore.engine_profile.cal.session_contains", return_value=not closed))
    ag = AsyncMock(return_value=None) if data_ok else AsyncMock(side_effect=RuntimeError("stale"))
    cm.enter_context(patch("tpcore.engine_profile.assert_passed_for_engine", ag))
    return cm


async def test_should_fire_all_green_fires():
    with _patch_all():
        d = await should_fire("reversion", datetime(2026,5,5,21,30,tzinfo=UTC), _FakePool(ran=False))
    assert d.fire is True and d.reason == "ready"
    assert d.checks == {"profiled": True, "cadence": True, "market_closed": True,
                        "data_ready": True, "not_already_run": True}

async def test_unknown_engine_fail_closed():
    d = await should_fire("nope", datetime(2026,5,5,21,30,tzinfo=UTC), _FakePool())
    assert d.fire is False and "unprofiled" in d.reason and d.checks["profiled"] is False

async def test_not_a_cadence_boundary_no_fire():
    with _patch_all(boundary=False):
        d = await should_fire("momentum", datetime(2026,5,5,21,30,tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason == "not a cadence boundary"

async def test_market_open_no_fire():
    with _patch_all(closed=False):
        d = await should_fire("reversion", datetime(2026,5,5,15,0,tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason == "market open"

async def test_data_not_ready_no_fire():
    with _patch_all(data_ok=False):
        d = await should_fire("reversion", datetime(2026,5,5,21,30,tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason.startswith("data not ready")

async def test_already_ran_this_cycle_no_fire():
    with _patch_all():
        d = await should_fire("reversion", datetime(2026,5,5,21,30,tzinfo=UTC), _FakePool(ran=True))
    assert d.fire is False and d.reason == "already ran this cycle"

async def test_exception_in_check_fails_closed():
    with patch("tpcore.engine_profile._cadence_boundary", side_effect=RuntimeError("boom")):
        d = await should_fire("reversion", datetime(2026,5,5,21,30,tzinfo=UTC), _FakePool())
    assert d.fire is False and d.reason.startswith("error:")
```

- [ ] **Step 2: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -k should_fire`
Expected: FAIL — `ImportError: cannot import name 'should_fire'`.

- [ ] **Step 3: Implement**

Add to `tpcore/engine_profile.py` (add import `from tpcore.quality.validation.capital_gate import assert_passed_for_engine`):

```python
@dataclass(frozen=True)
class FireDecision:
    fire: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)


_RUN_START_EVENT = "STARTUP"  # tpcore/logging/db_handler.py:115 (canonical run-start)


async def _already_ran(engine: str, pool, window_start: datetime) -> bool:
    async with pool.acquire() as conn:
        hit = await conn.fetchval(
            """
            SELECT 1 FROM platform.application_log
            WHERE engine = $1 AND event_type = $2 AND recorded_at >= $3
            LIMIT 1
            """,
            engine, _RUN_START_EVENT, window_start,
        )
    return hit is not None


async def should_fire(engine: str, now: datetime, pool) -> FireDecision:
    """Fail-CLOSED gate: True only if every precondition holds.

    Order (short-circuit): profiled → cadence boundary → market closed
    → data ready (capital_gate) → not already run this cycle. ANY
    error/ambiguity → fire=False (never trade on doubt).
    """
    checks: dict[str, bool] = {}
    try:
        profile = profile_for(engine)
        checks["profiled"] = profile is not None
        if profile is None:
            return FireDecision(False, "unprofiled engine", checks)

        checks["cadence"] = _cadence_boundary(profile, now)
        if not checks["cadence"]:
            return FireDecision(False, "not a cadence boundary", checks)

        if profile.market_closed_required:
            closed = not cal.session_contains(now)
            checks["market_closed"] = closed
            if not closed:
                return FireDecision(False, "market open", checks)
        else:
            checks["market_closed"] = True

        try:
            await assert_passed_for_engine(pool, engine)
            checks["data_ready"] = True
        except Exception as exc:  # noqa: BLE001 — any data-gate failure = not ready
            checks["data_ready"] = False
            return FireDecision(False, f"data not ready: {exc}", checks)

        ran = await _already_ran(engine, pool, _cadence_window_start(profile, now))
        checks["not_already_run"] = not ran
        if ran:
            return FireDecision(False, "already ran this cycle", checks)

        return FireDecision(True, "ready", checks)
    except Exception as exc:  # noqa: BLE001 — fail-closed on ANYTHING unexpected
        logger.warning("tpcore.engine_profile.should_fire_error",
                        engine=engine, error=str(exc))
        return FireDecision(False, f"error: {exc}", checks)
```

- [ ] **Step 4: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py -k should_fire -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "feat(engine_profile): should_fire() fail-closed 4-precondition gate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Verification gate (dark-landing + suite + lint)

**Files:** none (verification only)

- [ ] **Step 1: Prove it landed dark (no other file changed)**

Run: `git diff --stat origin/main...HEAD`
Expected: ONLY `tpcore/engine_profile.py` and `tpcore/tests/test_engine_profile.py` (plus the spec/plan docs from earlier commits). NO scheduler/daemon/engine file. If anything else appears, STOP — scope violation.

- [ ] **Step 2: Full suite + lint (no regression; dark landing means everything still green)**

Run:
```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests/test_engine_profile.py
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/tests tpcore/quality/validation/tests
```
Expected: engine_profile tests all PASS; ruff `All checks passed!`; the broader suites unchanged & green (engine_profile is imported by nothing yet — dark).

- [ ] **Step 3: check_imports layering**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python tpcore/scripts/check_imports.py`
Expected: clean (engine_profile imports only `tpcore.calendar`, `tpcore.quality.validation.capital_gate`, stdlib, pydantic, structlog — no engine packages; no layering violation).

- [ ] **Step 4: Final commit (plan completion marker)**

```bash
git commit --allow-empty -m "chore(engine_profile): Sub-project A complete — landed dark, suite green

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** §3.1 architecture → Task 1 (module, compose pattern) + Task 5 (dark verification). §3.2 data model → Task 1 (Cadence/EngineProfile/_PROFILE/profile_for) + Task 4 (FireDecision/should_fire). §3.3 composition order → Task 4 (exact short-circuit order + Tasks 2/3 helpers). §3.4 fail-closed → Task 4 (both inner data-gate catch and outer catch-all; `test_exception_in_check_fails_closed`). §3.5 testing matrix → Tasks 1–4 tests (cadence×boundary, market, data, already-ran, unknown, exception) + Task 1 coverage guard. §3.6 scope/acceptance → Task 5 Step 1 (`git diff --stat` only two files). D-A1 idempotency=application_log STARTUP reuse → Task 4 `_already_ran`. D-A2 compose capital_gate → Task 4 import + call. D-A3 cadence taxonomy → Task 2. D-A4 declarative+pure → Task 1. ✅ No gaps.

**Placeholder scan:** No TBD/TODO/"handle errors" — every step has real code, real commands, real expected output. Exact `"STARTUP"` literal pinned (db_handler.py:115). No "similar to Task N".

**Type consistency:** `EngineProfile`/`Cadence`/`_PROFILE`/`profile_for` defined Task 1, used identically Tasks 2–4. `_cadence_boundary(profile, now)->bool` (T2) and `_cadence_window_start(profile, now)->datetime` (T3) signatures match their Task-4 call sites. `FireDecision(fire, reason, checks)` consistent across Task 4 tests and impl. `cal` alias (`from tpcore import calendar as cal`) introduced Task 2, reused Tasks 3/4 and patched by that path in tests. `assert_passed_for_engine` imported Task 4 matching the real `capital_gate` signature (raises → caught). All consistent.
