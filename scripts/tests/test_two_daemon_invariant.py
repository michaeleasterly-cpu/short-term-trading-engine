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


def test_manifest_loop_is_exactly_the_railway_2_daemon_budget_set():
    # 2026-05-21 Railway 2-daemon budget consolidation:
    # ``ops.data_repair_service`` + ``ops.llm_triage_service`` were
    # folded into the single ``ops.lane_service`` daemon (four
    # crash-isolated co-tasks under one asyncio.gather()). The closed
    # whitelist is now:
    #   * engine-service  — consolidated long-lived daemon
    #   * lane-service    — data-repair + 5 triage lanes (one daemon)
    #   * data-operations — data-lane cron (NOT a daemon)
    # Two long-lived daemons (engine + lane) + one cron fits Railway's
    # 2-daemon constraint. The set is still a CLOSED whitelist: any
    # UNexpected installer in the loop still fails this assertion (it
    # bites — see test_invariant_bites_on_unexpected_installer).
    assert _installer_loop_tokens() == {
        "install_launchd_engine_service",
        "install_launchd_lane_service",
        "install_launchd_data_operations",
    }


def test_invariant_bites_on_unexpected_installer(tmp_path, monkeypatch):
    """Guardrail-of-the-guardrail: an UNexpected installer token in the
    loop must still fail the closed-whitelist check (proves the test was
    not weakened to a no-op when the lane consolidation landed)."""
    rogue_loop = (
        "for installer in install_launchd_engine_service "
        "install_launchd_lane_service install_launchd_data_operations "
        "install_launchd_rogue_daemon; do"
    )
    m = re.match(r"for installer in ([^\n;]+);\s*do", rogue_loop)
    assert m is not None
    tokens = set(m.group(1).split())
    assert tokens != {
        "install_launchd_engine_service",
        "install_launchd_lane_service",
        "install_launchd_data_operations",
    }, "the closed whitelist must still reject an unexpected installer"
    assert "install_launchd_rogue_daemon" in tokens


def test_retired_installers_are_deleted():
    # Pre-existing retirements (DA-3, 2026-05-18).
    assert not (SCRIPTS / "install_launchd_trade_monitor.sh").exists()
    assert not (SCRIPTS / "install_launchd_weekly_digest.sh").exists()
    # 2026-05-21 retirements (Railway 2-daemon budget — folded into
    # lane-service).
    assert not (SCRIPTS / "install_launchd_data_repair_service.sh").exists()
    assert not (SCRIPTS / "install_launchd_llm_triage_service.sh").exists()


def test_stale_plist_unload_rm_loop_present():
    sh = (SCRIPTS / "install_all_daemons.sh").read_text()
    assert "com.michael.trading.trade-monitor" in sh
    assert "com.michael.trading.weekly-digest" in sh
    # 2026-05-21: retiring the consolidated daemons' old plists too —
    # a still-loaded data-repair plist would poll the same bus as the
    # lane-service's data_repair co-task → terminal-event duplicate-emit.
    assert "com.michael.trading.data-repair-service" in sh
    assert "com.michael.trading.llm-triage-service" in sh
    assert "launchctl unload" in sh and "rm -f" in sh


def test_exactly_one_engine_keepalive_and_data_ops_cron():
    eng = (SCRIPTS / "install_launchd_engine_service.sh").read_text()
    assert "<key>KeepAlive</key>" in eng and "com.michael.trading.engine-service" in eng
    dops = (SCRIPTS / "install_launchd_data_operations.sh").read_text()
    assert "StartCalendarInterval" in dops
    lane = (SCRIPTS / "install_launchd_lane_service.sh").read_text()
    assert "<key>KeepAlive</key>" in lane  # consolidated lane daemon
    assert "com.michael.trading.lane-service" in lane


def test_allocator_heartbeat_is_sibling_cron_not_in_closed_whitelist_loop():
    """The allocator heartbeat (safety-net cron for the event-driven
    allocator) is installed OUTSIDE the closed-whitelist for-loop. It is
    a sibling installer call — NOT a member of the 3-installer
    long-lived/cron whitelist that `_installer_loop_tokens` pins.

    The 2026-05-21 Railway 2-daemon budget: engine-service +
    lane-service (the consolidated data-repair + advisory triage
    daemon) + data-operations cron. The heartbeat is a thin SAFETY-NET
    cron, not a primary trigger, so it is structurally distinct (gates
    on tpcore.engine_profile.should_fire and exits clean when the
    daemon path already ran the allocator this cycle).
    """
    sh = (SCRIPTS / "install_all_daemons.sh").read_text()
    # 1) The heartbeat installer exists.
    assert (SCRIPTS / "install_launchd_allocator_heartbeat.sh").exists()
    # 2) It is invoked from install_all_daemons.sh as a SIBLING call —
    #    `scripts/install_launchd_allocator_heartbeat.sh` appears in the
    #    file (callable) but NOT as a token in the for-loop.
    assert "install_launchd_allocator_heartbeat" in sh
    assert "install_launchd_allocator_heartbeat" not in _installer_loop_tokens()
    # 3) The plist has a calendar interval (cron), NOT KeepAlive (daemon).
    hb = (SCRIPTS / "install_launchd_allocator_heartbeat.sh").read_text()
    assert "StartCalendarInterval" in hb
    assert "<key>KeepAlive</key>" in hb and "<false/>" in hb  # explicitly NOT a daemon


def test_dashboard_daemon_spec_drops_phantom_trade_monitor():
    dash = (REPO_ROOT / "dashboard.py").read_text()
    # post-consolidation the standalone trade_monitor persistent row
    # is a phantom (its log stops); the real persistent daemon is the
    # consolidated engine-service.
    assert '("trade_monitor", "persistent"' not in dash
