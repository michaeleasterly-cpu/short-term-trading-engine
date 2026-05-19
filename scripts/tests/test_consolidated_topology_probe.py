"""DA-3 T5 — engine-lane `consolidated_daemon_topology` --check probe.

Asserts the probe is registered in the engine-lane `_CHECK_FNS` list
(adjacent to `trade_monitor_heartbeat`) and is NOT in the data-lane
`_AUDIT_CHECKS` tuple, and that it goes green only for the exact
expected daemon label set (retired daemons absent).
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# `scripts/ops.py` (the data-ops CLI module) and the `ops/` daemons
# package share the top-level name `ops`. Under full-suite collection
# another test (e.g. test_engine_service.py) may bind sys.modules['ops']
# to the PACKAGE (has `__path__`) before this file imports. Python won't
# re-resolve a cached name, so evict any cached `ops`/`ops.*` that is the
# package (has `__path__`) — leaving only the scripts/ops.py MODULE — and
# put scripts/ FIRST so a fresh `import ops` resolves to the module.
# (Root name collision = pre-existing tech-debt; see test_engine_service.py.)
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if hasattr(sys.modules[_m], "__path__"):  # the ops/ package, not the module
        del sys.modules[_m]

import ops as opsmod  # noqa: E402 — scripts/ops.py (sys.path/modules fixed above)

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_probe_registered_in_check_fns_not_audit() -> None:
    names = [n for n, _ in opsmod._CHECK_FNS]  # noqa: SLF001
    assert "consolidated_daemon_topology" in names
    # immediately after trade_monitor_heartbeat (precedented engine probe)
    idx = names.index("trade_monitor_heartbeat")
    assert names[idx + 1] == "consolidated_daemon_topology"
    # MUST NOT be in the data-lane data-pipeline audit. _AUDIT_CHECKS is a
    # tuple of (table, check_name, sql) 3-tuples — check the check-name
    # field too, and assert the probe name appears in none of the fields.
    audit_names = [check for _table, check, _sql in opsmod._AUDIT_CHECKS]  # noqa: SLF001
    assert "consolidated_daemon_topology" not in audit_names
    flat = {f for row in opsmod._AUDIT_CHECKS for f in row}  # noqa: SLF001
    assert "consolidated_daemon_topology" not in flat


async def test_probe_ok_for_expected_label_set() -> None:
    out = (
        "1\t0\tcom.michael.trading.engine-service\n"
        "2\t0\tcom.michael.trading.data-repair-service\n"
        "3\t0\tcom.michael.trading.data-operations\n"
    )
    with patch.object(
        opsmod.subprocess,
        "run",
        return_value=type("R", (), {"stdout": out, "returncode": 0, "stderr": ""})(),
    ):
        res = await opsmod._check_consolidated_daemon_topology(None)  # noqa: SLF001
    assert res["ok"] is True


async def test_probe_red_when_retired_daemon_present() -> None:
    out = (
        "1\t0\tcom.michael.trading.engine-service\n"
        "2\t0\tcom.michael.trading.data-repair-service\n"
        "3\t0\tcom.michael.trading.data-operations\n"
        "4\t0\tcom.michael.trading.trade-monitor\n"
    )
    with patch.object(
        opsmod.subprocess,
        "run",
        return_value=type("R", (), {"stdout": out, "returncode": 0, "stderr": ""})(),
    ):
        res = await opsmod._check_consolidated_daemon_topology(None)  # noqa: SLF001
    assert res["ok"] is False
    assert "trade-monitor" in str(res)


async def test_probe_red_when_expected_daemon_missing() -> None:
    out = (
        "1\t0\tcom.michael.trading.engine-service\n"
        "2\t0\tcom.michael.trading.data-repair-service\n"
    )
    with patch.object(
        opsmod.subprocess,
        "run",
        return_value=type("R", (), {"stdout": out, "returncode": 0, "stderr": ""})(),
    ):
        res = await opsmod._check_consolidated_daemon_topology(None)  # noqa: SLF001
    assert res["ok"] is False
    assert "data-operations" in str(res)


async def test_probe_red_when_launchctl_absent() -> None:
    """Graceful degradation on non-macOS/CI hosts where launchctl is absent."""
    with patch.object(
        opsmod.subprocess,
        "run",
        side_effect=FileNotFoundError("launchctl"),
    ):
        res = await opsmod._check_consolidated_daemon_topology(None)  # noqa: SLF001
    assert res["ok"] is False
    assert "launchctl" in res["reason"]


async def test_probe_red_when_unexpected_label_present() -> None:
    """An unrecognised com.michael.trading.* label must also be red (exact-set gate)."""
    out = (
        "1\t0\tcom.michael.trading.engine-service\n"
        "2\t0\tcom.michael.trading.data-repair-service\n"
        "3\t0\tcom.michael.trading.data-operations\n"
        "4\t0\tcom.michael.trading.something-new\n"
    )
    with patch.object(
        opsmod.subprocess,
        "run",
        return_value=type("R", (), {"stdout": out, "returncode": 0, "stderr": ""})(),
    ):
        res = await opsmod._check_consolidated_daemon_topology(None)  # noqa: SLF001
    assert res["ok"] is False
    assert "something-new" in str(res)
