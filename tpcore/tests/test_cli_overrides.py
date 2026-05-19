"""Characterization test for the consolidated ``overrides_from_args`` (Lean P5.1, #5).

Pins the exact current behaviour of each engine's private ``_overrides_from_args``
BEFORE the refactor: the new pure ``tpcore.backtest.cli_overrides.overrides_from_args``
must return the byte-identical dict each engine produces today, given that engine's
own ``*_OVERRIDE_KEYS`` tuple. Expected dicts are HAND-WRITTEN literals (not the
engine fn as oracle — post-refactor that would be tautological); we also assert
each engine's delegate returns the same literal, proving the delegate passes its
correct keys.
"""

from __future__ import annotations

import argparse

import pytest

from momentum.backtest import MOMENTUM_OVERRIDE_KEYS
from momentum.backtest import _overrides_from_args as momentum_overrides
from reversion.backtest import REVERSION_OVERRIDE_KEYS
from reversion.backtest import _overrides_from_args as reversion_overrides
from tpcore.backtest.cli_overrides import overrides_from_args
from vector.backtest import VECTOR_OVERRIDE_KEYS
from vector.backtest import _overrides_from_args as vector_overrides

# A representative Namespace: some override keys set to real values, some
# explicitly None, some absent entirely. Spans every engine's key set and
# includes unrelated attrs that must NOT leak into the result.
_REPRESENTATIVE_NS = argparse.Namespace(
    # reversion keys
    z_threshold=2.5,  # real value
    earnings_quality=None,  # explicit None -> dropped
    volume_climax_multiplier=1.75,  # real value
    # max_hold_days absent entirely -> dropped
    stop_pct=0.0,  # real falsy-but-not-None value -> kept (only None drops)
    # vector keys
    pb_ceiling=3.0,
    de_ceiling=None,
    catalyst_window_days=5,
    # swing_score_threshold absent
    # momentum keys
    lookback_days=252,
    skip_days=None,
    hold_days=21,
    # top_decile_pct absent
    # unrelated attr that must never appear in any result
    unrelated_attr="ignore-me",
)


# Hand-written expected dicts: independently computed from
# _REPRESENTATIVE_NS × each engine's *_OVERRIDE_KEYS (include key iff
# getattr(ns, k, None) is not None). NOT derived from the code under
# test or the engine delegate — a literal so a future logic drift of
# the shared fn (or a wrong delegate keys tuple) fails on a value
# mismatch.
@pytest.mark.parametrize(
    ("engine_private_fn", "keys", "expected"),
    [
        (
            reversion_overrides,
            REVERSION_OVERRIDE_KEYS,
            {"z_threshold": 2.5, "volume_climax_multiplier": 1.75, "stop_pct": 0.0},
        ),
        (
            vector_overrides,
            VECTOR_OVERRIDE_KEYS,
            {"pb_ceiling": 3.0, "catalyst_window_days": 5, "stop_pct": 0.0},
        ),
        (
            momentum_overrides,
            MOMENTUM_OVERRIDE_KEYS,
            {"lookback_days": 252, "hold_days": 21},
        ),
    ],
    ids=["reversion", "vector", "momentum"],
)
def test_overrides_from_args_matches_hardcoded_expected_and_engine_delegate(
    engine_private_fn: object,
    keys: tuple[str, ...],
    expected: dict[str, object],
) -> None:
    """Shared fn AND the engine delegate each equal the independent literal."""
    assert overrides_from_args(_REPRESENTATIVE_NS, keys) == expected
    assert engine_private_fn(_REPRESENTATIVE_NS) == expected  # type: ignore[operator]


def test_empty_namespace_yields_empty_dict() -> None:
    """No attributes present at all -> empty dict (getattr default path)."""
    ns = argparse.Namespace()
    assert overrides_from_args(ns, REVERSION_OVERRIDE_KEYS) == {}


def test_none_values_are_dropped_real_values_kept() -> None:
    """Only ``None`` is dropped; falsy non-None (0, '', 0.0) is kept."""
    ns = argparse.Namespace(z_threshold=0, earnings_quality=None, stop_pct="")
    assert overrides_from_args(ns, REVERSION_OVERRIDE_KEYS) == {
        "z_threshold": 0,
        "stop_pct": "",
    }
