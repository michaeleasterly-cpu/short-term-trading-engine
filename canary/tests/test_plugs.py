from decimal import Decimal

from canary.plugs.aar_logging import CanaryAARLogging
from canary.plugs.capital_gate import CanaryCapitalGate
from canary.plugs.execution_risk import CanaryExecutionRisk
from canary.plugs.lifecycle_analysis import CanaryLifecycleAnalysis
from canary.plugs.setup_detection import CanarySetupDetection
from tpcore.aar.models import ExitReason
from tpcore.interfaces.engine_plug import BaseEnginePlug


def test_all_five_plugs_subclass_baseengineplug_and_healthcheck():
    plugs = [CanarySetupDetection(), CanaryLifecycleAnalysis(),
             CanaryExecutionRisk(), CanaryAARLogging(), CanaryCapitalGate()]
    for p in plugs:
        assert isinstance(p, BaseEnginePlug)
        assert p.validate_dependencies() is True
        hc = p.healthcheck()
        assert hc["engine"] == "canary" and hc["ok"] is True


def test_setup_detection_emits_one_spy_signal_with_filter_diagnostics():
    sd = CanarySetupDetection()
    sig, diag = sd.detect()
    assert sig.ticker == "SPY" and sig.qty == 1
    assert diag.universe_total == 1 and diag.candidates_passed == 1


def test_execution_risk_builds_one_share_spy_market_order():
    d = CanaryExecutionRisk().decide(price=Decimal("500"))
    assert d.ticker == "SPY" and d.qty == 1
    assert d.notional_usd == Decimal("500")


def test_aar_logging_uses_classify_exit_reason_time_stop_for_no_tp_sl():
    aar = CanaryAARLogging().build_aar(
        trade_id="ca_SPY_x", entry_ts_iso="2026-05-05T21:00:00+00:00",
        exit_ts_iso="2026-05-06T21:00:00+00:00",
        entry_price=Decimal("500"), exit_price=Decimal("501"), qty=Decimal("1"),
        engine_equity_usd=Decimal("10000"))
    assert aar.engine == "canary" and aar.ticker == "SPY"
    assert aar.exit_reason is ExitReason.TIME_STOP
    assert aar.pnl_net == Decimal("1")


def test_capital_gate_tiny_cap_and_never_graduates():
    g = CanaryCapitalGate()
    assert g.check_trade(size=Decimal("500"), engine_pnl=Decimal("0"),
                         open_positions=0) is True
    assert g.check_trade(size=Decimal("5000"), engine_pnl=Decimal("0"),
                         open_positions=0) is False
    assert g.check_trade(size=Decimal("500"), engine_pnl=Decimal("0"),
                         open_positions=1) is False  # ≤1 position rule
