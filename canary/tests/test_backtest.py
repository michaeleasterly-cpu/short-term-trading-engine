import inspect

import canary.backtest as cb


def test_backtest_deliberately_never_writes_credibility():
    src = inspect.getsource(cb)
    assert "write_credibility_score" not in src, (
        "canary is non-graduating BY CONSTRUCTION (spec §4b) — it must "
        "NEVER write a credibility rubric")


async def test_run_backtest_is_an_explicit_noop():
    out = await cb.run_backtest()
    assert out["graduating"] is False
    assert "canary" in out["reason"]
