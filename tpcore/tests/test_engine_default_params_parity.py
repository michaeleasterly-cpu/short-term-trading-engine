"""O1 default_params() parity (SP3 T1). The cannot-be-forgotten
clockwork: a new searched param without a default fails CI (HealSpec-
coverage discipline). Lazy in-body import (H-S3-10)."""
from __future__ import annotations

import pytest

_PARAM_RANGES_ENGINES = ("reversion", "vector", "momentum")


@pytest.mark.parametrize("engine", _PARAM_RANGES_ENGINES)
def test_each_param_ranges_engine_default_keyset_equals_param_ranges(engine):
    from ops.engine_sdlc.default_params import default_params
    from ops.lab.run import PARAM_RANGES
    got = default_params(engine)
    assert set(got) == set(PARAM_RANGES[engine]), (
        f"{engine}: default_params() keyset {sorted(got)} != PARAM_RANGES "
        f"keyset {sorted(PARAM_RANGES[engine])} — a searched param with no "
        f"default (or a stale default) fails CI")
    for v in got.values():
        assert v is not None


def test_sentinel_canary_have_no_accessor():
    # sentinel/canary have NO search space (not in PARAM_RANGES) ⇒ no
    # backtest.default_params accessor (spec §7.1).
    import importlib
    for engine in ("sentinel", "canary"):
        mod = importlib.import_module(f"{engine}.backtest")
        assert not hasattr(mod, "default_params"), (
            f"{engine}: has no PARAM_RANGES search space — must NOT expose "
            f"default_params()")


def test_dispatcher_rejects_unknown_engine():
    from ops.engine_sdlc.default_params import default_params
    with pytest.raises(ValueError, match="unknown engine: nope"):
        default_params("nope")
