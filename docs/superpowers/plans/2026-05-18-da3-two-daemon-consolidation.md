# DA-3 Two-Daemon Consolidation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Consolidate the engine-lane scheduled surface into ONE long-lived engine daemon — fold `trade_monitor` into `ops/engine_service.py` and relocate the weekly-digest cron-trigger into it (subprocess seam) — then audit-enforce the per-lane two-daemon invariant.

**Architecture:** `ops/engine_service.py` becomes the single engine daemon: one process, one event loop, one shared asyncpg pool, hosting two supervised co-tasks (the existing sweep poll-loop + `TradeMonitor.run_forever()`) plus a deterministic day-rollover weekly-digest subprocess trigger. `data_repair_service`/`data_operations` stay separate data-lane processes (descoped CL-1). Event contracts frozen. Atomic single PR.

**Tech Stack:** Python 3.11, asyncio, asyncpg, structlog, pytest (`asyncio_mode="auto"`). venv `/Users/michael/short-term-trading-engine/.venv/bin/python`; `ruff` on PATH. Worktree `/Users/michael/short-term-trading-engine/.claude/worktrees/da3-consolidation` (branch `worktree-da3-consolidation`, base `234fb0d`).

**Spec:** `docs/superpowers/specs/2026-05-18-da3-two-daemon-consolidation-design.md` (§11 hardening H-1..H-12 are BINDING).

**Lane discipline:** Touch ONLY `ops/engine_service.py`, `scripts/run_engine_service.sh`, `scripts/install_all_daemons.sh`, `scripts/ops.py` (add one engine-lane `_CHECK_FNS` probe — precedented), `dashboard.py` (drop one phantom daemon tuple), `CLAUDE.md` (daemon lines), the new/edited test files, and DELETE `scripts/install_launchd_trade_monitor.sh` + `scripts/install_launchd_weekly_digest.sh`. NEVER edit `ops/weekly_digest.py`, `ops/data_repair_service.py`, `scripts/run_data_operations.sh`, `scripts/run_weekly_digest.sh`, `scripts/install_launchd_data_*.sh`, `tpcore/trade_monitor.py` (consumed only — NOT modified), `tpcore/selfheal|ladder|feeds|ingestion|datasupervisor`, `tpcore/auditheal*`, `ops/cutover_agent.py`. The `ops.weekly_digest` subprocess seam is NOT a data-lane edit (Sub-project-C `_invoke_allocator` precedent). CI-exact: `ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/`; `python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore` (no arg change — no new engine pkg).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `ops/engine_service.py` | The single engine daemon: sweep loop + monitor co-task + digest trigger | Modify (the structural core) |
| `scripts/run_engine_service.sh` | Daemon wrapper env | Verify (already sources .env + IPv4 pin) — test only |
| `scripts/install_all_daemons.sh` | Daemon manifest | Modify: 5→3 installers + stale-plist unload/rm loop |
| `scripts/install_launchd_trade_monitor.sh` | Retired | DELETE |
| `scripts/install_launchd_weekly_digest.sh` | Retired | DELETE |
| `scripts/ops.py` | `--check` probe registry | Add `_check_consolidated_daemon_topology` + `_CHECK_FNS` entry |
| `dashboard.py` | Daemon health roll-up spec list | Drop the phantom `trade_monitor` tuple |
| `CLAUDE.md` | Daemon topology docs | Modify (two-daemon statement + daemons line) |
| `scripts/tests/test_engine_service.py` | Daemon tests | Add consolidation tests |
| `scripts/tests/test_two_daemon_invariant.py` | The invariant gate | Create |
| `scripts/tests/test_consolidated_topology_probe.py` | Probe test | Create |

---

## Task 1: Co-host `TradeMonitor` in the engine daemon (structural core)

**Files:**
- Modify: `ops/engine_service.py`
- Test: `scripts/tests/test_engine_service.py`

The current `_amain` runs only `_main_loop`. Add a co-hosted `TradeMonitor.run_forever()` task + the weekly-digest day-rollover trigger, under a dual per-task supervisor (H-6: NOT TaskGroup), one shared pool (H-1, H-8), one stop_event/signal path, one `pool.close()`.

- [ ] **Step 1: Write failing tests**

Append to `scripts/tests/test_engine_service.py` (header already has the ops-collision guard + `_Pool`/`_Conn`; `es` is the bound module). Add imports at top with the existing ones: `import asyncio`, `from datetime import date`, `from unittest.mock import AsyncMock, MagicMock, patch`.

```python
async def test_shared_pool_built_once_and_closed_once(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")
    built = []

    class _P:
        def __init__(self): self.closed = 0
        async def close(self): self.closed += 1

    async def _fake_build(dsn, **kw):
        p = _P(); built.append((p, kw)); return p
    monkeypatch.setattr(es, "build_asyncpg_pool", _fake_build)
    # both co-tasks return immediately so _amain falls through
    monkeypatch.setattr(es, "_run_supervised",
                        AsyncMock(return_value=None))
    rc = await es._amain()
    assert rc == 0
    assert len(built) == 1                       # pool built exactly once
    assert built[0][1].get("max_size", 0) >= 5   # H-8 sizing
    assert built[0][0].closed == 1               # closed exactly once


async def test_supervised_restarts_crashed_task_without_killing_sibling():
    calls = {"n": 0}
    stop = asyncio.Event()

    async def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        stop.set()  # second run: signal we recovered, let supervisor exit

    # _run_supervised(name, factory, stop_event, backoff=0) must catch
    # the exception, log, and re-run until stop_event is set.
    await es._run_supervised("flaky", _flaky, stop, backoff=0.0)
    assert calls["n"] == 2  # restarted after the crash, did not propagate


async def test_supervised_propagates_cancellation():
    stop = asyncio.Event()

    async def _hang():
        await asyncio.sleep(3600)

    task = asyncio.create_task(
        es._run_supervised("hang", _hang, stop, backoff=0.0))
    await asyncio.sleep(0)
    task.cancel()
    with __import__("pytest").raises(asyncio.CancelledError):
        await task


async def test_slow_sweep_does_not_block_monitor_tick(monkeypatch):
    """Make-or-break: the sweep runs in an executor; a slow sweep must
    NOT delay an event-loop coroutine tick (the monitor stream)."""
    monkeypatch.setattr(es, "_find_new_trigger",
                        AsyncMock(return_value=es.datetime.now(es.UTC)))

    def _slow_sweep():
        import time; time.sleep(0.5); return 0
    monkeypatch.setattr(es, "_run_engine_sweep", _slow_sweep)

    ticked = []
    stop = asyncio.Event()

    async def _ticker():
        for _ in range(5):
            ticked.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.05)
        stop.set()

    pool = _Pool(None)
    await asyncio.gather(es._main_loop(pool, stop), _ticker())
    # 5 ticks ~0.05s apart finished well within the 0.5s blocking sweep
    assert len(ticked) == 5
    assert ticked[-1] - ticked[0] < 0.45


async def test_digest_trigger_fires_once_per_utc_day(monkeypatch):
    spawns = []

    async def _fake_exec(*args, **kw):
        spawns.append(args)
        class _P:
            returncode = 0
            async def wait(self): return 0
        return _P()
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _fake_exec)

    state = {"last": None}
    d1 = date(2026, 5, 18)
    await es._maybe_fire_weekly_digest(state, today=d1)
    await es._maybe_fire_weekly_digest(state, today=d1)   # same day → no
    await es._maybe_fire_weekly_digest(state, today=date(2026, 5, 19))
    assert len(spawns) == 2
    assert spawns[0][1:] == (sys.executable_mp := spawns[0][1:])  # shape
    # exact arg shape: (sys.executable, "-m", "ops.weekly_digest", "emit")
    assert spawns[0][1:4] == ("-m", "ops.weekly_digest", "emit") or \
           spawns[0][0:4][1:] == ("-m", "ops.weekly_digest", "emit")


async def test_digest_trigger_crash_isolated(monkeypatch):
    async def _boom(*a, **k):
        raise OSError("spawn failed")
    monkeypatch.setattr(es.asyncio, "create_subprocess_exec", _boom)
    state = {"last": None}
    # must NOT raise — crash-isolated like _invoke_allocator
    await es._maybe_fire_weekly_digest(state, today=date(2026, 5, 18))
```

- [ ] **Step 2: Run, expect FAIL**

`cd /Users/michael/short-term-trading-engine/.claude/worktrees/da3-consolidation && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_service.py -q`
Expected: FAIL (`_run_supervised`/`_maybe_fire_weekly_digest` missing; `_amain` doesn't build pool with `max_size`).

- [ ] **Step 3: Implement in `ops/engine_service.py`**

Add to the import block (keep ruff order — stdlib first): add `from datetime import UTC, date, datetime, timedelta` (extend existing `datetime` import to include `date`), `import sys` (already present). Add the monitor imports AFTER the existing `from tpcore.db import build_asyncpg_pool`:

```python
from tpcore.aar import AARWriter
from tpcore.brokers.alpaca_paper import AlpacaPaperBrokerAdapter
from tpcore.trade_monitor import TradeMonitor
```
(Verify the exact import paths by reading `tpcore/trade_monitor.py`'s own imports of `AARWriter`/`AlpacaPaperBrokerAdapter` and reuse those EXACT module paths — the recon shows `amain()` constructs `AlpacaPaperBrokerAdapter()` and `AARWriter(pool)`; match its import lines verbatim.)

Add module constant near the others:
```python
POOL_MAX_SIZE = 6  # sweep-poll (1) + co-hosted monitor (~4) + headroom (H-8)
```

Add the day-rollover digest trigger (mirrors `_invoke_allocator` crash-isolation, H-7):
```python
async def _maybe_fire_weekly_digest(state: dict, today: date | None = None) -> None:
    """Deterministic day-rollover trigger for the (idempotent-per-ISO-week)
    weekly digest — relocated from the retired launchd cron. Fires
    `python -m ops.weekly_digest emit` as a crash-isolated subprocess
    (the Sub-project-C `_invoke_allocator` seam). NEVER raises."""
    today = today or datetime.now(UTC).date()
    if state.get("last") == today:
        return
    state["last"] = today
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ops.weekly_digest", "emit",
        )
        rc = await proc.wait()
    except Exception as exc:  # noqa: BLE001 — isolate: never abort the daemon
        logger.error("engine_daemon.weekly_digest_failed", error=str(exc))
        return
    if rc == 0:
        logger.info("engine_daemon.weekly_digest_done")
    else:
        logger.error("engine_daemon.weekly_digest_failed", returncode=rc)
```

Add the per-task supervisor (H-6 — NOT TaskGroup; catches Exception, restarts, re-raises CancelledError):
```python
async def _run_supervised(name: str, factory, stop_event: asyncio.Event,
                          backoff: float = 5.0) -> None:
    """Run `factory()` (a 0-arg coroutine fn) until stop_event; an
    Exception is logged and the task restarted after `backoff` (one
    crashed co-task must NEVER kill its sibling — H-6). CancelledError
    propagates (clean shutdown)."""
    while not stop_event.is_set():
        try:
            await factory()
            return  # clean completion
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — restart, don't propagate
            logger.error("engine_daemon.task_crashed", task=name,
                         error=str(exc))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
```

Fold the digest trigger into `_main_loop` — replace the existing `_main_loop` body's loop so each poll iteration also checks day-rollover. Change the `_main_loop` signature to accept the digest state and add the call (one-shot startup kick preserves the retired plist's `RunAtLoad`, H-7/O-2):
```python
async def _main_loop(pool, stop_event: asyncio.Event) -> None:
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    digest_state: dict = {"last": None}
    logger.info(
        "engine_service.started",
        triggers=list(TRIGGER_EVENT_TYPES),
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
    )
    await _maybe_fire_weekly_digest(digest_state)  # startup kick (O-2)
    while not stop_event.is_set():
        try:
            newest = await _find_new_trigger(pool, cursor)
        except Exception as exc:
            logger.error("engine_service.poll_failed", error=str(exc))
            newest = None
        if newest is not None and newest > cursor:
            logger.info("engine_service.trigger_seen",
                        recorded_at=newest.isoformat())
            cursor = newest
            await asyncio.get_event_loop().run_in_executor(
                None, _run_engine_sweep)
        await _maybe_fire_weekly_digest(digest_state)
        try:
            await asyncio.wait_for(stop_event.wait(),
                                   timeout=POLL_INTERVAL_SEC)
        except TimeoutError:
            pass
```

Rewrite `_amain` to build ONE shared pool (sized), construct the monitor (H-1: replicate `amain()`'s construction block; do NOT call `tpcore.trade_monitor.amain()`), and run both co-tasks under the dual supervisor with one stop_event/signal path and one `pool.close()`:
```python
async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("engine_service.no_dsn",
                     note="set DATABASE_URL or DATABASE_URL_IPV4")
        return 1
    pool = await build_asyncpg_pool(dsn, max_size=POOL_MAX_SIZE)
    stop_event = asyncio.Event()

    def _handle_signal(signum):
        logger.info("engine_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # H-1: construct the monitor against the SHARED pool (mirror
    # tpcore.trade_monitor.amain()'s construction block — NOT amain()).
    monitor = TradeMonitor(
        pool=pool, broker=AlpacaPaperBrokerAdapter(),
        aar_writer=AARWriter(pool))

    async def _sweep_factory():
        await _main_loop(pool, stop_event)

    async def _monitor_factory():
        await monitor.run_forever()

    sweep_task = asyncio.create_task(
        _run_supervised("sweep", _sweep_factory, stop_event))
    monitor_task = asyncio.create_task(
        _run_supervised("monitor", _monitor_factory, stop_event))
    try:
        await stop_event.wait()
    finally:
        for t in (sweep_task, monitor_task):
            t.cancel()
        await asyncio.gather(sweep_task, monitor_task,
                             return_exceptions=True)
        await pool.close()
        logger.info("engine_service.stopped")
    return 0
```
(`build_asyncpg_pool` must accept `max_size` — confirm by reading `tpcore/db.py`; `trade_monitor.amain` already calls `build_asyncpg_pool(db_url, max_size=4)`, so the kwarg exists.)

- [ ] **Step 4: Run, expect PASS**

`/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_service.py -q`
Expected: PASS (existing trigger/green-filter tests + the 6 new). Fix the `test_digest_trigger_fires_once_per_utc_day` arg-shape assertion to the real `create_subprocess_exec(sys.executable, "-m", "ops.weekly_digest", "emit")` positional shape if the loose assertion mis-fires (keep the invariant: exactly 2 spawns, args are `sys.executable,-m,ops.weekly_digest,emit`).

- [ ] **Step 5: ruff + commit**
```bash
ruff check ops/engine_service.py scripts/tests/test_engine_service.py
git add ops/engine_service.py scripts/tests/test_engine_service.py
git commit -m "$(cat <<'EOF'
feat(engine_daemon): co-host trade_monitor + weekly-digest trigger (DA-3 T1)

One engine daemon: shared pool (max_size>=5), TradeMonitor co-task
constructed against the shared pool (H-1, not amain()), dual per-task
supervisor (H-6, not TaskGroup — a crashed task restarts without
killing its sibling), deterministic day-rollover weekly-digest
subprocess trigger + startup kick (H-7), single stop_event/pool.close.
Sweep stays off-loop in run_in_executor (fill-latency safe).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Verify the daemon env wrapper (H-2)

**Files:**
- Verify: `scripts/run_engine_service.sh` (recon shows it ALREADY `set -a; source .env; set +a` + the `DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}"` pin → ALPACA creds already available to the co-hosted monitor; likely NO code change — a regression test locks it)
- Test: `scripts/tests/test_engine_service.py`

- [ ] **Step 1: Write the failing test**

Append:
```python
def test_run_engine_service_wrapper_has_env_for_monitor():
    """H-2: the consolidated daemon's wrapper must source .env (so the
    co-hosted TradeMonitor sees ALPACA_KEY/ALPACA_SECRET) AND keep the
    IPv4-pooler pin (launchd network-namespace requirement)."""
    sh = (REPO_ROOT / "scripts" / "run_engine_service.sh").read_text()
    assert "source .env" in sh, "wrapper must source .env for ALPACA creds"
    assert 'DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}"' in sh
    assert "-m ops.engine_service" in sh
```

- [ ] **Step 2: Run** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_service.py -k env_for_monitor -q`. If it PASSES immediately, the wrapper is already correct (recon confirms) — no code change; proceed to Step 4. If it FAILS, Step 3.

- [ ] **Step 3: (only if Step 2 failed) fix `scripts/run_engine_service.sh`** to add the `set -a; source .env; set +a` block before the `exec env DATABASE_URL=...` line (mirror `scripts/run_trade_monitor.sh`'s `source .env`). Do not change the exec target.

- [ ] **Step 4: ruff (n/a for .sh) + commit**
```bash
git add scripts/tests/test_engine_service.py scripts/run_engine_service.sh
git commit -m "$(cat <<'EOF'
test(engine_daemon): lock run_engine_service.sh env contract (DA-3 T2, H-2)

Wrapper must source .env (ALPACA creds for the co-hosted monitor) +
keep the IPv4-pooler pin. Regression test; wrapper already compliant.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```
(If `run_engine_service.sh` was unchanged, `git add` it harmlessly no-ops; commit the test only.)

---

## Task 3: Two-daemon invariant test (H-9) — written BEFORE the manifest edit

**Files:**
- Create: `scripts/tests/test_two_daemon_invariant.py`

Write the invariant test first (TDD: it must FAIL against the current 5-installer manifest, then Task 4 makes it pass).

- [ ] **Step 1: Create the test**

```python
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

SCRIPTS = REPO_ROOT / "scripts"


def _installer_loop_tokens() -> set[str]:
    sh = (SCRIPTS / "install_all_daemons.sh").read_text()
    m = re.search(r"for installer in ([^\n;]+);\s*do", sh)
    assert m, "could not find the `for installer in ...; do` loop"
    return set(m.group(1).split())


def test_manifest_loop_is_exactly_the_three_surviving_installers():
    assert _installer_loop_tokens() == {
        "install_launchd_engine_service",
        "install_launchd_data_repair_service",
        "install_launchd_data_operations",
    }


def test_retired_installers_are_deleted():
    assert not (SCRIPTS / "install_launchd_trade_monitor.sh").exists()
    assert not (SCRIPTS / "install_launchd_weekly_digest.sh").exists()


def test_stale_plist_unload_rm_loop_present():
    sh = (SCRIPTS / "install_all_daemons.sh").read_text()
    assert "com.michael.trading.trade-monitor" in sh
    assert "com.michael.trading.weekly-digest" in sh
    assert "launchctl unload" in sh and "rm -f" in sh


def test_exactly_one_engine_keepalive_and_data_ops_cron():
    eng = (SCRIPTS / "install_launchd_engine_service.sh").read_text()
    assert "<key>KeepAlive</key>" in eng and "com.michael.trading.engine-service" in eng
    dops = (SCRIPTS / "install_launchd_data_operations.sh").read_text()
    assert "StartCalendarInterval" in dops
    drep = (SCRIPTS / "install_launchd_data_repair_service.sh").read_text()
    assert "<key>KeepAlive</key>" in drep  # data-lane daemon untouched
```

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_two_daemon_invariant.py -q`. Expected: `test_manifest_loop...` and `test_retired_installers...` and `test_stale_plist...` FAIL (current manifest still has 5 installers, no unload loop, installers still exist).

- [ ] **Step 3: ruff + commit (red test is intentional, locked by Task 4)**
```bash
ruff check scripts/tests/test_two_daemon_invariant.py
git add scripts/tests/test_two_daemon_invariant.py
git commit -m "$(cat <<'EOF'
test(engine_daemon): two-daemon invariant gate (DA-3 T3, H-9)

Structural parse of install_all_daemons.sh (not substring): the loop
must be exactly the 3 surviving installers; the 2 retired installers
deleted; the stale-plist unload/rm loop present. RED until T4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Retire the two plists + manifest cutover (H-3, H-11)

**Files:**
- Modify: `scripts/install_all_daemons.sh`
- Delete: `scripts/install_launchd_trade_monitor.sh`, `scripts/install_launchd_weekly_digest.sh`
- Test: `scripts/tests/test_two_daemon_invariant.py` (now goes green)

- [ ] **Step 1: Edit `scripts/install_all_daemons.sh`.** Replace the `for installer in install_launchd_trade_monitor install_launchd_engine_service install_launchd_data_repair_service install_launchd_data_operations install_launchd_weekly_digest; do` line with the 3-token loop, and add the idempotent stale-plist retirement loop immediately BEFORE the installer loop:

```bash
# DA-3 (2026-05-18): trade_monitor + weekly_digest folded into the
# single engine daemon (ops/engine_service.py). Retire their launchd
# plists idempotently — a deleted per-installer cannot self-unload,
# and a still-loaded trade-monitor plist would run a SECOND Tier-2
# cascade (H-3). Symmetric to Sub-project C retiring the allocator cron.
for stale in com.michael.trading.trade-monitor com.michael.trading.weekly-digest; do
    p="$HOME/Library/LaunchAgents/${stale}.plist"
    launchctl unload "$p" 2>/dev/null || true
    rm -f "$p"
done

for installer in install_launchd_engine_service install_launchd_data_repair_service install_launchd_data_operations; do
    echo ""
    echo "▶ ${installer}"
    echo "────────────────────────────────────────────────────────────────────────"
    scripts/${installer}.sh
done
```
Update the header comment block of `install_all_daemons.sh` to state the daemon set is now: engine-service (consolidated: sweep + trade-monitor + weekly-digest trigger), data-repair-service (data-lane), data-operations (data-lane cron). Mirror the existing allocator-retirement note style (lines ~14-17).

- [ ] **Step 2: Delete the two retired installers**
```bash
git rm scripts/install_launchd_trade_monitor.sh scripts/install_launchd_weekly_digest.sh
```

- [ ] **Step 3: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_two_daemon_invariant.py -q` → all green.

- [ ] **Step 4: commit**
```bash
git add scripts/install_all_daemons.sh
git commit -m "$(cat <<'EOF'
feat(engine_daemon): retire trade_monitor + weekly_digest plists (DA-3 T4, H-3/H-11)

Manifest 5→3 installers; idempotent stale-plist unload/rm loop INSIDE
install_all_daemons.sh (the deleted per-installer cannot self-unload;
prevents a double Tier-2 cascade). Atomic cutover — lands after T1-T3
in the same PR.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `consolidated_daemon_topology` `--check` probe (H-4)

**Files:**
- Modify: `scripts/ops.py`
- Test: `scripts/tests/test_consolidated_topology_probe.py`

- [ ] **Step 1: Write failing test**
```python
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ops as opsmod  # scripts/ops.py


def test_probe_registered_in_check_fns_not_audit():
    names = [n for n, _ in opsmod._CHECK_FNS]
    assert "consolidated_daemon_topology" in names
    # MUST NOT be in the data-lane audit
    audit = [n for n, _ in opsmod._AUDIT_CHECKS]
    assert "consolidated_daemon_topology" not in audit


async def test_probe_ok_for_expected_label_set():
    out = (
        "1\t0\tcom.michael.trading.engine-service\n"
        "2\t0\tcom.michael.trading.data-repair-service\n"
        "3\t0\tcom.michael.trading.data-operations\n"
    )
    with patch.object(opsmod.subprocess, "run",
                      return_value=type("R", (), {"stdout": out})()):
        res = await opsmod._check_consolidated_daemon_topology(None)
    assert res["ok"] is True


async def test_probe_red_when_retired_daemon_present():
    out = (
        "1\t0\tcom.michael.trading.engine-service\n"
        "2\t0\tcom.michael.trading.data-repair-service\n"
        "3\t0\tcom.michael.trading.data-operations\n"
        "4\t0\tcom.michael.trading.trade-monitor\n"
    )
    with patch.object(opsmod.subprocess, "run",
                      return_value=type("R", (), {"stdout": out})()):
        res = await opsmod._check_consolidated_daemon_topology(None)
    assert res["ok"] is False
    assert "trade-monitor" in str(res)
```
(Read `scripts/ops.py` to confirm `subprocess` is imported at module level; if it imports `subprocess` locally elsewhere, the probe must `import subprocess` at module scope or the test patches the right symbol — align the patch target to the real import.)

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_consolidated_topology_probe.py -q`.

- [ ] **Step 3: Implement in `scripts/ops.py`.** Add the probe next to `_check_trade_monitor_heartbeat` (~line 2741):
```python
_EXPECTED_DAEMON_LABELS = {
    "com.michael.trading.engine-service",
    "com.michael.trading.data-repair-service",
    "com.michael.trading.data-operations",
}
_RETIRED_DAEMON_LABELS = {
    "com.michael.trading.trade-monitor",
    "com.michael.trading.weekly-digest",
}


async def _check_consolidated_daemon_topology(pool) -> dict[str, Any]:
    """DA-3 two-daemon invariant (engine-lane probe; NOT in the
    data-pipeline audit). Live `launchctl list` label set must be
    exactly the 3 expected daemons, with the 2 retired ones absent."""
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True,
                               text=True, check=False)
        labels = {
            ln.split("\t")[-1].strip()
            for ln in proc.stdout.splitlines()
            if "com.michael.trading." in ln
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"launchctl list failed: {exc}"}
    present_retired = labels & _RETIRED_DAEMON_LABELS
    missing_expected = _EXPECTED_DAEMON_LABELS - labels
    ok = not present_retired and not missing_expected
    res: dict[str, Any] = {"ok": ok, "labels": sorted(labels)}
    if present_retired:
        res["reason"] = f"retired daemon still loaded: {sorted(present_retired)}"
    elif missing_expected:
        res["reason"] = f"expected daemon missing: {sorted(missing_expected)}"
    return res
```
Append `("consolidated_daemon_topology", _check_consolidated_daemon_topology),` to the `_CHECK_FNS` list immediately AFTER the `("trade_monitor_heartbeat", _check_trade_monitor_heartbeat),` entry. Do NOT touch `_AUDIT_CHECKS`. Ensure `import subprocess` exists at `scripts/ops.py` module scope (add if absent, ruff-ordered).

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_consolidated_topology_probe.py -q`.

- [ ] **Step 5: ruff + commit**
```bash
ruff check scripts/ops.py scripts/tests/test_consolidated_topology_probe.py
git add scripts/ops.py scripts/tests/test_consolidated_topology_probe.py
git commit -m "$(cat <<'EOF'
feat(engine_daemon): consolidated_daemon_topology --check probe (DA-3 T5, H-4)

Engine-lane probe in ops.py _CHECK_FNS (adjacent to
trade_monitor_heartbeat, precedented) — live launchctl label set must
be exactly the 3 expected daemons, retired ones absent. NOT added to
_AUDIT_CHECKS (data-lane).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Dashboard roll-up cleanup (H-10) + smoke verification (H-5)

**Files:**
- Modify: `dashboard.py` (drop the phantom `trade_monitor` daemon-spec tuple)
- Test: `scripts/tests/test_two_daemon_invariant.py` (add a dashboard-spec assertion)

- [ ] **Step 1: Add failing assertion** to `scripts/tests/test_two_daemon_invariant.py`:
```python
def test_dashboard_daemon_spec_drops_phantom_trade_monitor():
    dash = (REPO_ROOT / "dashboard.py").read_text()
    # the consolidated daemon is engine-service; the standalone
    # trade_monitor persistent row is now a phantom (its log stops).
    assert '("trade_monitor", "persistent"' not in dash
```

- [ ] **Step 2: Run, expect FAIL** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_two_daemon_invariant.py -k phantom -q`.

- [ ] **Step 3: Edit `dashboard.py`.** Read the daemon-spec list (the recon shows tuples `("trade_monitor", "persistent", "trade-monitor.log")`, `("data_operations", "scheduled", "data-operations.log")`, `("allocator", "scheduled", "allocator.log")`). Replace the `("trade_monitor", "persistent", "trade-monitor.log")` tuple with `("engine_service", "persistent", "engine-service.log")` (the consolidated daemon — its log is now the engine-service log; this keeps a persistent-daemon health row that is real, not phantom). Leave `data_operations`/`allocator` tuples unchanged (allocator drift is pre-existing, out of DA-3 scope). `weekly_digest` is not in the list — nothing to remove there.

- [ ] **Step 4: Run, expect PASS** — `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_two_daemon_invariant.py -q` (all green).

- [ ] **Step 5: Smoke verification (H-5 — no smoke-file edit).** Read `scripts/pipeline_smoke_test.py` to confirm it does NOT spawn `tpcore.trade_monitor` (it depends on the installed daemon + polls `engine='trade_monitor'` EVENT rows). Document in the commit message that the consolidated daemon emits the same `engine='trade_monitor'` rows (the `TradeMonitor` instance is unchanged), so smoke needs no edit. NO code change here — verification only.

- [ ] **Step 6: ruff + commit**
```bash
ruff check dashboard.py scripts/tests/test_two_daemon_invariant.py
git add dashboard.py scripts/tests/test_two_daemon_invariant.py
git commit -m "$(cat <<'EOF'
refactor(engine_daemon): dashboard daemon-spec → engine_service (DA-3 T6, H-10)

Drop the now-phantom standalone trade_monitor persistent row (its log
stops post-consolidation); the real persistent daemon is the
consolidated engine-service. Smoke unaffected (H-5): the consolidated
daemon emits the same engine='trade_monitor' EVENT rows — no smoke edit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Docs + full-suite + CI/lane gate

**Files:**
- Modify: `CLAUDE.md`
- Verify only: full suite, ruff, check_imports, lane discipline

- [ ] **Step 1: Update `CLAUDE.md`.** Find the operator-workflow bullet listing daemons installed via `install_all_daemons.sh` (it currently reads "trade_monitor + engine_service + data_repair_service + data_operations + weekly_digest"). Edit `git add -p`-style (guard vs the concurrent data session — stage ONLY your hunk). Change that list to: "engine-service (consolidated: data-ops-triggered sweep + co-hosted trade-monitor stream + day-rollover weekly-digest trigger), data_repair_service (data-lane), data_operations (data-lane cron)" and add a one-line DA-3 note mirroring the allocator-retirement style: `**Two-daemon consolidation (DA-3, 2026-05-18):** trade_monitor + the weekly-digest cron-trigger folded into the single long-lived engine daemon (ops/engine_service.py); data_repair_service + data_operations remain the data lane's. "Exactly two daemons" = one long-lived daemon per lane + the data-ops cron; enforced by scripts/tests/test_two_daemon_invariant.py + the consolidated_daemon_topology --check probe.`

- [ ] **Step 2: Commit docs**
```bash
git add -p CLAUDE.md   # stage ONLY the DA-3 hunk; if a foreign hunk exists, report it
git commit -m "$(cat <<'EOF'
docs(engine_daemon): CLAUDE.md two-daemon consolidation (DA-3 T7)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Full suite**
`/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -4`
Expected: full repo suite green (baseline + the new DA-3 tests; no regression in `test_engine_service.py`/`test_engine_dispatch.py`).

- [ ] **Step 4: CI-exact lint + import-layering + lane discipline**
```bash
ruff check reversion/ vector/ momentum/ sentinel/ canary/ tpcore/ scripts/ ops/
/Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel canary tpcore
BASE=$(git merge-base HEAD origin/main); git diff --name-only $BASE..HEAD | grep -E "ops/weekly_digest\.py|ops/data_repair_service\.py|scripts/run_data_operations\.sh|scripts/run_weekly_digest\.sh|scripts/install_launchd_data_|tpcore/(selfheal|ladder|feeds|ingestion|datasupervisor|trade_monitor)|tpcore/auditheal|ops/cutover_agent\.py" && echo "LANE VIOLATION" || echo "lane-clean"
```
Expected: `All checks passed!`, `ok: no forbidden imports found`, `lane-clean`. Note `tpcore/trade_monitor.py` MUST be unmodified (consumed only) — the grep above includes it; `lane-clean` confirms.

- [ ] **Step 5: Finish the branch**
Use **superpowers:finishing-a-development-branch**. Per the established pattern: push `worktree-da3-consolidation`, open a PR (atomic — all 7 tasks, H-11), fetch origin/main and resolve conflicts combining intents (data session may have touched CLAUDE.md — keep BOTH bullets), CI must be green before merge, squash-merge, clean the worktree. Do NOT local-merge into the shared checkout.

---

## Self-Review

**1. Spec coverage:** §1 honest scope (already-done = T-none/docs; in-scope = T1-T7) ✓. §2 lane discipline + subprocess seam → T1 digest trigger mirrors `_invoke_allocator`, T7 Step 4 lane assertion ✓. §3 single engine daemon, 3 co-hosted units → T1 ✓. §4 failure isolation (dual supervisor, off-loop sweep, shared pool, signal/shutdown) → T1 tests (crash-restart, cancellation, slow-sweep-no-block, pool-once) ✓. §5 migration (manifest, retired installers, label kept) → T4 ✓. §6 invariant test + probe → T3, T5 ✓. §7 all D-D3 decisions honored (D-D3-7 label kept in T1 `_amain` unchanged exec target/T4 manifest) ✓. §8 tests incl. fill-latency + digest cadence ✓. §11 H-1..H-12 each mapped: H-1 T1 construction seam; H-2 T2; H-3 T4 stale loop; H-4 T5; H-5 T6 Step 5; H-6 T1 `_run_supervised`; H-7 T1 `_maybe_fire_weekly_digest`+startup kick; H-8 T1 POOL_MAX_SIZE; H-9 T3; H-10 T6; H-11 atomic PR (T7 Step 5); H-12 rollback (documented in spec §11, no code) ✓.

**2. Placeholder scan:** every step has literal code + exact command + expected result. The two "verify-then-maybe-edit" steps (T2 wrapper, T5 subprocess-import) are explicit bounded contingencies with the invariant pinned + the real fallback spelled out — the accepted style, not deferred work. The one "read X to confirm exact import path" (T1 monitor imports, T5 subprocess symbol) is a pin-against-reality instruction with the verbatim recon already giving the answer (`AARWriter`/`AlpacaPaperBrokerAdapter` per `trade_monitor.amain()`), not a gap.

**3. Type/name consistency:** `_run_supervised(name, factory, stop_event, backoff)`, `_maybe_fire_weekly_digest(state, today)`, `POOL_MAX_SIZE`, `_check_consolidated_daemon_topology`, `_EXPECTED_DAEMON_LABELS`/`_RETIRED_DAEMON_LABELS`, `_CHECK_FNS` entry name `consolidated_daemon_topology` — consistent across T1/T5 defs, tests, and CLAUDE.md/probe. Atomic-PR ordering (T1→T3 before T4 cutover) explicit in T4 commit msg + H-11. No mismatches.
