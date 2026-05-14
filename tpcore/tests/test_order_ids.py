"""Tests for ``tpcore.order_ids`` — the cross-engine attribution registry.

These tests are load-bearing: a regression here means two engines could
unknowingly act on each other's orders. Heavy coverage warranted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tpcore.order_ids import (
    ENGINE_PREFIX,
    LEGACY_PREFIX,
    build_cid,
    engine_for_cid,
    is_engine_cid,
    parse_cid,
)

# ─── Builders ───────────────────────────────────────────────────────────


def test_build_cid_momentum_canonical():
    ts = datetime(2026, 5, 14, tzinfo=UTC)
    cid = build_cid("momentum", "AAPL", constructed_at=ts)
    expected_epoch = int(ts.timestamp())
    assert cid == f"mo_AAPL_{expected_epoch}"


def test_build_cid_sigma_tier1_and_tier2_share_trade_key():
    ts = datetime(2026, 5, 14, tzinfo=UTC)
    tier1 = build_cid("sigma", "YUMC", constructed_at=ts, tier="tier1")
    tier2 = build_cid("sigma", "YUMC", constructed_at=ts, tier="tier2")
    expected_epoch = int(ts.timestamp())
    assert tier1 == f"sg_YUMC_{expected_epoch}_tier1"
    assert tier2 == f"sg_YUMC_{expected_epoch}_tier2"
    # The trade_key (engine+ticker+ts) is identical — that's how the
    # reconcile path pairs the two legs.
    assert parse_cid(tier1).trade_key == parse_cid(tier2).trade_key


def test_build_cid_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unknown engine"):
        build_cid("nonsense", "AAPL")


def test_build_cid_rejects_invalid_tier():
    with pytest.raises(ValueError, match="invalid tier"):
        build_cid("sigma", "AAPL", tier="tier3")


# ─── Parsers — canonical format ─────────────────────────────────────────


def test_parse_cid_canonical_momentum():
    p = parse_cid("mo_AAPL_1778803200")
    assert p.engine == "momentum"
    assert p.tier is None
    assert p.trade_key == "AAPL_1778803200"


def test_parse_cid_canonical_sigma_tier1():
    p = parse_cid("sg_YUMC_1778803200_tier1")
    assert p.engine == "sigma"
    assert p.tier == "tier1"
    assert p.trade_key == "YUMC_1778803200"


def test_parse_cid_canonical_reversion_tier2():
    p = parse_cid("rv_XOM_1778803200_tier2")
    assert p.engine == "reversion"
    assert p.tier == "tier2"
    assert p.trade_key == "XOM_1778803200"


def test_parse_cid_canonical_vector():
    p = parse_cid("vc_TSLA_1778803200")
    assert p.engine == "vector"
    assert p.tier is None
    assert p.trade_key == "TSLA_1778803200"


# ─── Parsers — legacy formats (in-flight orders) ────────────────────────


def test_parse_cid_legacy_vector_prefix():
    """Vector's pre-migration ``vector_<TICKER>_<TS>`` still attributes."""
    p = parse_cid("vector_TSLA_1778582356")
    assert p.engine == "vector"
    assert p.tier is None


def test_parse_cid_legacy_sigma_or_reversion_tier_suffix():
    """Old ``<TICKER>_<TS>_tier1`` cid: engine is ambiguous (could be
    sigma or reversion). Returns engine=None so callers know to fall
    back to in-process state."""
    p = parse_cid("YUMC_1778582356_tier1")
    assert p.engine is None
    assert p.tier == "tier1"
    assert p.trade_key == "YUMC_1778582356"


def test_parse_cid_unrecognized():
    p = parse_cid("manual_buy_AAPL")
    assert p.engine is None
    assert p.tier is None
    assert p.trade_key is None


def test_parse_cid_none_and_empty():
    for v in (None, ""):
        p = parse_cid(v)
        assert p.engine is None
        assert p.tier is None
        assert p.trade_key is None


# ─── Round-trip: every builder output parses back to the same engine ────


@pytest.mark.parametrize("engine", list(ENGINE_PREFIX))
def test_round_trip_no_tier(engine):
    cid = build_cid(engine, "AAPL", constructed_at=datetime(2026, 5, 14, tzinfo=UTC))
    assert engine_for_cid(cid) == engine
    assert parse_cid(cid).tier is None


@pytest.mark.parametrize("engine", ["sigma", "reversion"])
@pytest.mark.parametrize("tier", ["tier1", "tier2"])
def test_round_trip_with_tier(engine, tier):
    cid = build_cid(engine, "AAPL", constructed_at=datetime(2026, 5, 14, tzinfo=UTC), tier=tier)
    p = parse_cid(cid)
    assert p.engine == engine
    assert p.tier == tier


# ─── is_engine_cid — the helper engines + dashboard will lean on ────────


def test_is_engine_cid_canonical():
    assert is_engine_cid("sg_AAPL_1_tier1", "sigma") is True
    assert is_engine_cid("sg_AAPL_1_tier1", "reversion") is False
    assert is_engine_cid("mo_AAPL_1", "momentum") is True
    assert is_engine_cid("mo_AAPL_1", "vector") is False


def test_is_engine_cid_legacy_vector():
    """Legacy ``vector_`` prefix still attributes to vector."""
    assert is_engine_cid("vector_TSLA_1", "vector") is True
    assert is_engine_cid("vector_TSLA_1", "sigma") is False


def test_is_engine_cid_legacy_tier_suffix_returns_false():
    """Legacy tier-only cids: caller must fall back to in-process state."""
    assert is_engine_cid("YUMC_1_tier1", "sigma") is False
    assert is_engine_cid("YUMC_1_tier1", "reversion") is False


def test_is_engine_cid_handles_none():
    assert is_engine_cid(None, "momentum") is False
    assert is_engine_cid("", "momentum") is False


# ─── Prefix-collision invariant — load-bearing ──────────────────────────


def test_no_engine_prefix_is_a_prefix_of_another():
    """No engine's prefix can start with another engine's prefix; otherwise
    attribution is ambiguous. The ENGINE_PREFIX import-time check enforces
    this for canonical AND legacy prefixes; this test re-asserts so a
    future PR can't silently regress."""
    all_prefixes = list(ENGINE_PREFIX.values()) + list(LEGACY_PREFIX)
    for i, p1 in enumerate(all_prefixes):
        for p2 in all_prefixes[i + 1 :]:
            assert not p1.startswith(p2), f"'{p1}' starts with '{p2}'"
            assert not p2.startswith(p1), f"'{p2}' starts with '{p1}'"


# ─── Cross-engine isolation property ────────────────────────────────────


def test_cross_engine_cids_never_misattribute():
    """For every pair of engines, building a cid for engine A must NEVER
    attribute to engine B. This is the property the prefix scheme exists
    to guarantee."""
    engines = list(ENGINE_PREFIX)
    for engine_a in engines:
        cid = build_cid(engine_a, "AAPL")
        for engine_b in engines:
            if engine_a == engine_b:
                continue
            assert not is_engine_cid(cid, engine_b), (
                f"{engine_a} cid {cid!r} misattributes to {engine_b}"
            )
