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
