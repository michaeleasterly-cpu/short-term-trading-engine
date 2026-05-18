"""H-S2-3 live-safety: the Lab credibility write MUST be Lab-namespaced.

``graduation_ready(pool, engine)`` (``tpcore/backtest/credibility.py``)
gates LIVE promotion off the NEWEST ``data_quality_log`` row for
``source=backtest_credibility.{engine}``. A ``fold_existing`` Lab
experiment targeting a LIVE engine (e.g. ``reversion``) persisting under
``engine_name="reversion"`` would make a deliberately-experimental
candidate the newest row the LIVE reversion capital gate reads —
corrupting live promotion (SDLC SP2 T6, spec §12 H-S2-3).

This test pins the invariant by construction: the Lab credibility
``engine_name`` MUST be ``lab.<candidate>`` so the resolved source is
``backtest_credibility.lab.<candidate>``, NEVER
``backtest_credibility.<live_engine>``.
"""
from __future__ import annotations

from tpcore.backtest.credibility import CREDIBILITY_SOURCE_PREFIX


def test_lab_credibility_source_is_namespaced():
    # the Lab MUST persist under backtest_credibility.lab.<candidate>,
    # never backtest_credibility.<live_engine> (would poison
    # graduation_ready for the live engine).
    from ops.lab.run import _lab_credibility_engine_name

    assert _lab_credibility_engine_name("reversion", "exp1") == "lab.exp1"
    src = f"{CREDIBILITY_SOURCE_PREFIX}.{_lab_credibility_engine_name('reversion', 'exp1')}"
    assert src == "backtest_credibility.lab.exp1"
    assert "backtest_credibility.reversion" != src
