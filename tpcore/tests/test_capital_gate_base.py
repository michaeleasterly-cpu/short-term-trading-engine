"""Permanent characterization suite for the consolidated capital gate
(HIGHEST-RISK live-money risk gate; clusters #3/#4/#7) plus momentum's
shared graduate gate.

This file was the Lean P5.5a/b TDD + `_legacy_*` parallel-diff harness
for the staged reversion→vector→momentum cutover onto
:class:`tpcore.interfaces.capital_gate_base.PerTradeCapitalGateBase` and
the shared ``assert_can_graduate`` free function. The staged cutover is
complete: the `_legacy_*` parallel-diff scaffolding (kept verbatim
pre-refactor bodies + the differential grid tests) was **retired
post-cutover at Lean P5.5c** once byte-equivalence was locked in CI. What
remains is the PERMANENT regression asset — pure characterization with an
*independent* expectation (the plug is NOT used as its own oracle):

* Reversion / vector ``check_trade``: every reject branch, the exact
  ``drawdown == -0.05`` boundary (both sides), the ``engine_equity == 0``
  skip, and the EXACT emitted structlog event NAME per branch
  (forensics/dashboards key on the event string — observable behavior).
* Reversion / vector / momentum ``assert_can_graduate``: the full
  raise-vs-return matrix (``is_graduated`` short-circuit,
  ``assert_passed_for_engine`` path, ``graduation_ready`` true/false)
  with the I/O dependencies mocked — no real DB/network. Momentum is a
  BATCH engine: it reuses ONLY the shared ``assert_can_graduate`` free
  fn, NOT per-trade ``check_trade`` (it must NOT subclass the per-trade
  base — spec §7 D2).
* ``is_graduated`` thresholds per engine (incl. reversion's PF floor and
  momentum's rebalance/sharpe/PF triple).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import structlog

import momentum.plugs.capital_gate as mom_mod
import tpcore.interfaces.capital_gate_base as base_mod
from momentum.models import (
    GRAD_MIN_PROFIT_FACTOR as MOM_GRAD_MIN_PROFIT_FACTOR,
)
from momentum.models import (
    GRAD_MIN_REBALANCES as MOM_GRAD_MIN_REBALANCES,
)
from momentum.models import (
    GRAD_MIN_SHARPE as MOM_GRAD_MIN_SHARPE,
)
from momentum.plugs.capital_gate import (
    MomentumCapitalGate,
    MomentumGraduationStats,
)
from reversion.plugs.capital_gate import GraduationStats, ReversionCapitalGate
from tpcore.backtest.credibility import CredibilityScoreInsufficientError
from tpcore.quality.validation.capital_gate import (
    ValidationFailedError,
    ValidationStaleError,
)
from vector.plugs.capital_gate import (
    GraduationStats as VectorGraduationStats,
)
from vector.plugs.capital_gate import VectorCapitalGate


def _patch_deps(
    monkeypatch: pytest.MonkeyPatch, val: object, cred: object
) -> None:
    """Patch the validation/credibility I/O deps for the consolidated
    gate. Post-P5.5c the single shared ``assert_can_graduate`` free
    function (and the base classmethod that delegates to it) resolves
    these from the ``base_mod`` globals ONLY — reversion/vector/momentum
    inherit/delegate and no longer carry their own module-level copies.
    Asserting that decoupling here (``not hasattr`` on every engine
    module) is a structural regression guard: a re-introduced engine-local
    copy would silently escape the mock."""
    for mod in (mom_mod,):
        assert not hasattr(mod, "assert_passed_for_engine")
        assert not hasattr(mod, "graduation_ready")
    monkeypatch.setattr(base_mod, "assert_passed_for_engine", val)
    monkeypatch.setattr(base_mod, "graduation_ready", cred)


# Reversion/vector inherit the same consolidated base path; momentum
# delegates to the same free function. All three resolve the I/O deps
# from ``base_mod`` only, so the patch helper is identical — kept under
# the per-engine names the §2 raise/return-matrix tests already call.
_patch_deps_vector = _patch_deps
_patch_deps_momentum = _patch_deps


def _capture() -> structlog.testing.LogCapture:
    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    return cap


def _event_names(cap: structlog.testing.LogCapture) -> list[str]:
    return [e["event"] for e in cap.entries]


# ── 1. check_trade characterization (independent expectation) ───────────────
#
# Defaults: engine_equity=10000, max_position_usd=2000, max_positions=5,
# DAILY_LOSS_FREEZE_PCT=0.05 → boundary engine_pnl at equity 10000 is -500.


@pytest.mark.parametrize(
    ("size", "engine_pnl", "open_positions", "kwargs", "expected", "events"),
    [
        # nonpositive size (zero and negative)
        (Decimal("0"), Decimal("0"), 0, {}, False, ["reversion.gate.reject_nonpositive"]),
        (Decimal("-1"), Decimal("0"), 0, {}, False, ["reversion.gate.reject_nonpositive"]),
        # oversize vs max_position_usd (strictly greater)
        (Decimal("2000.01"), Decimal("0"), 0, {}, False, ["reversion.gate.reject_oversize"]),
        # exactly at the cap is allowed (not > cap)
        (Decimal("2000"), Decimal("0"), 0, {}, True, []),
        # position-count limit: >= max_positions rejects
        (Decimal("100"), Decimal("0"), 5, {}, False, ["reversion.gate.reject_position_count"]),
        (Decimal("100"), Decimal("0"), 6, {}, False, ["reversion.gate.reject_position_count"]),
        # one below the count cap is allowed
        (Decimal("100"), Decimal("0"), 4, {}, True, []),
        # daily-loss: drawdown strictly past the threshold rejects (-600/10000=-0.06)
        (Decimal("100"), Decimal("-600"), 0, {}, False, ["reversion.gate.reject_daily_loss"]),
        # the EXACT drawdown == -0.05 boundary: -500/10000 == -0.05 → <= → REJECT
        (Decimal("100"), Decimal("-500"), 0, {}, False, ["reversion.gate.reject_daily_loss"]),
        # just inside the boundary: -499.99/10000 > -0.05 → ALLOW
        (Decimal("100"), Decimal("-499.99"), 0, {}, True, []),
        # engine_equity == 0 → drawdown block skipped entirely (no divide), ALLOW
        (Decimal("100"), Decimal("-99999"), 0, {"engine_equity": Decimal("0")}, True, []),
        # clean pass, no events
        (Decimal("1000"), Decimal("50"), 2, {}, True, []),
    ],
)
def test_check_trade_characterization(
    size: Decimal,
    engine_pnl: Decimal,
    open_positions: int,
    kwargs: dict,
    expected: bool,
    events: list[str],
) -> None:
    gate = ReversionCapitalGate(**kwargs)
    cap = _capture()
    try:
        result = gate.check_trade(size, engine_pnl, open_positions)
    finally:
        structlog.reset_defaults()
    assert result is expected
    assert _event_names(cap) == events


def test_check_trade_branch_precedence_nonpositive_before_oversize() -> None:
    """A nonpositive size that is also 'oversize' must emit ONLY the
    nonpositive event — branch order is observable behavior."""
    gate = ReversionCapitalGate()
    cap = _capture()
    try:
        result = gate.check_trade(Decimal("-9999"), Decimal("0"), 0)
    finally:
        structlog.reset_defaults()
    assert result is False
    assert _event_names(cap) == ["reversion.gate.reject_nonpositive"]


def test_healthcheck_payload_unchanged() -> None:
    assert ReversionCapitalGate().healthcheck() == {
        "engine": "reversion",
        "plug": "capital_gate",
        "ok": True,
        "details": {
            "engine_equity_usd": "10000",
            "max_position_usd": "2000",
            "max_positions": 5,
        },
    }


# ── 2. assert_can_graduate raise/return matrix ──────────────────────────────

_PASS_STATS = GraduationStats(
    n_trades=10, win_rate=0.6, avg_return=0.05, profit_factor=2.0
)
_FAIL_STATS = GraduationStats(
    n_trades=1, win_rate=0.6, avg_return=0.05, profit_factor=2.0
)


class _SentinelPool:
    """Stand-in for asyncpg.Pool — never touched (deps are monkeypatched)."""


async def test_assert_can_graduate_short_circuits_when_not_graduated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"validation": False, "cred": False}

    async def _val(*a: object, **k: object) -> None:
        called["validation"] = True

    async def _cred(*a: object, **k: object) -> bool:
        called["cred"] = True
        return True

    _patch_deps(monkeypatch, _val, _cred)

    result = await ReversionCapitalGate.assert_can_graduate(
        _FAIL_STATS, _SentinelPool()
    )
    assert result is False
    # Short-circuit: neither I/O dependency is consulted.
    assert called == {"validation": False, "cred": False}


async def test_assert_can_graduate_true_when_all_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return True

    _patch_deps(monkeypatch, _val, _cred)

    assert (
        await ReversionCapitalGate.assert_can_graduate(_PASS_STATS, _SentinelPool())
        is True
    )


async def test_assert_can_graduate_raises_credibility_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return False

    _patch_deps(monkeypatch, _val, _cred)

    with pytest.raises(CredibilityScoreInsufficientError):
        await ReversionCapitalGate.assert_can_graduate(_PASS_STATS, _SentinelPool())


@pytest.mark.parametrize("exc", [ValidationStaleError, ValidationFailedError])
async def test_assert_can_graduate_propagates_validation_errors(
    monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    async def _val(*a: object, **k: object) -> None:
        raise exc("data gate not satisfied")

    async def _cred(*a: object, **k: object) -> bool:  # pragma: no cover
        return True

    _patch_deps(monkeypatch, _val, _cred)

    with pytest.raises(exc):
        await ReversionCapitalGate.assert_can_graduate(_PASS_STATS, _SentinelPool())


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5), True),
        (GraduationStats(n_trades=9, win_rate=0.55, avg_return=0.02, profit_factor=1.5), False),
        (GraduationStats(n_trades=10, win_rate=0.54, avg_return=0.02, profit_factor=1.5), False),
        (GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.019, profit_factor=1.5), False),
        (GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.49), False),
        (GraduationStats(n_trades=0, win_rate=0.0, avg_return=0.0, profit_factor=0.0), False),
    ],
)
def test_is_graduated_thresholds(stats: GraduationStats, expected: bool) -> None:
    assert ReversionCapitalGate.is_graduated(stats) is expected


# ════════════════════════════════════════════════════════════════════════════
# Lean P5.5b — VECTOR cutover (SECOND staged per-engine cutover of the live
# risk gate). Identical structure to the reversion families above, with an
# INDEPENDENT expectation for vector: the exact `vector.gate.*` event names,
# the same `-0.05` boundary, vector's 3-threshold `is_graduated` (NO
# profit_factor — vector's GraduationStats is the plain
# PerTradeGraduationStats). The P5.5b `_legacy_*` parallel-diff was retired
# post-cutover (plan P5.5c); this is now the permanent characterization
# suite for the consolidated gate.
# ════════════════════════════════════════════════════════════════════════════


# ── 1. vector check_trade characterization (independent expectation) ────────
#
# Defaults: engine_equity=10000, max_position_usd=2000, max_positions=5,
# DAILY_LOSS_FREEZE_PCT=0.05 → boundary engine_pnl at equity 10000 is -500.


@pytest.mark.parametrize(
    ("size", "engine_pnl", "open_positions", "kwargs", "expected", "events"),
    [
        # nonpositive size (zero and negative)
        (Decimal("0"), Decimal("0"), 0, {}, False, ["vector.gate.reject_nonpositive"]),
        (Decimal("-1"), Decimal("0"), 0, {}, False, ["vector.gate.reject_nonpositive"]),
        # oversize vs max_position_usd (strictly greater)
        (Decimal("2000.01"), Decimal("0"), 0, {}, False, ["vector.gate.reject_oversize"]),
        # exactly at the cap is allowed (not > cap)
        (Decimal("2000"), Decimal("0"), 0, {}, True, []),
        # position-count limit: >= max_positions rejects
        (Decimal("100"), Decimal("0"), 5, {}, False, ["vector.gate.reject_position_count"]),
        (Decimal("100"), Decimal("0"), 6, {}, False, ["vector.gate.reject_position_count"]),
        # one below the count cap is allowed
        (Decimal("100"), Decimal("0"), 4, {}, True, []),
        # daily-loss: drawdown strictly past the threshold rejects (-600/10000=-0.06)
        (Decimal("100"), Decimal("-600"), 0, {}, False, ["vector.gate.reject_daily_loss"]),
        # the EXACT drawdown == -0.05 boundary: -500/10000 == -0.05 → <= → REJECT
        (Decimal("100"), Decimal("-500"), 0, {}, False, ["vector.gate.reject_daily_loss"]),
        # just inside the boundary: -499.99/10000 > -0.05 → ALLOW
        (Decimal("100"), Decimal("-499.99"), 0, {}, True, []),
        # engine_equity == 0 → drawdown block skipped entirely (no divide), ALLOW
        (Decimal("100"), Decimal("-99999"), 0, {"engine_equity": Decimal("0")}, True, []),
        # clean pass, no events
        (Decimal("1000"), Decimal("50"), 2, {}, True, []),
    ],
)
def test_vector_check_trade_characterization(
    size: Decimal,
    engine_pnl: Decimal,
    open_positions: int,
    kwargs: dict,
    expected: bool,
    events: list[str],
) -> None:
    gate = VectorCapitalGate(**kwargs)
    cap = _capture()
    try:
        result = gate.check_trade(size, engine_pnl, open_positions)
    finally:
        structlog.reset_defaults()
    assert result is expected
    assert _event_names(cap) == events


def test_vector_check_trade_branch_precedence_nonpositive_before_oversize() -> None:
    """A nonpositive size that is also 'oversize' must emit ONLY the
    nonpositive event — branch order is observable behavior."""
    gate = VectorCapitalGate()
    cap = _capture()
    try:
        result = gate.check_trade(Decimal("-9999"), Decimal("0"), 0)
    finally:
        structlog.reset_defaults()
    assert result is False
    assert _event_names(cap) == ["vector.gate.reject_nonpositive"]


def test_vector_healthcheck_payload_unchanged() -> None:
    assert VectorCapitalGate().healthcheck() == {
        "engine": "vector",
        "plug": "capital_gate",
        "ok": True,
        "details": {
            "engine_equity_usd": "10000",
            "max_position_usd": "2000",
            "max_positions": 5,
        },
    }


# ── 2. vector assert_can_graduate raise/return matrix ───────────────────────
#
# Vector's GraduationStats has NO profit_factor (unlike reversion); grad
# thresholds are n_trades>=30, win_rate>=0.55, avg_return>=0.03.

_VEC_PASS_STATS = VectorGraduationStats(
    n_trades=30, win_rate=0.6, avg_return=0.05
)
_VEC_FAIL_STATS = VectorGraduationStats(
    n_trades=1, win_rate=0.6, avg_return=0.05
)


async def test_vector_assert_can_graduate_short_circuits_when_not_graduated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"validation": False, "cred": False}

    async def _val(*a: object, **k: object) -> None:
        called["validation"] = True

    async def _cred(*a: object, **k: object) -> bool:
        called["cred"] = True
        return True

    _patch_deps_vector(monkeypatch, _val, _cred)

    result = await VectorCapitalGate.assert_can_graduate(
        _VEC_FAIL_STATS, _SentinelPool()
    )
    assert result is False
    assert called == {"validation": False, "cred": False}


async def test_vector_assert_can_graduate_true_when_all_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return True

    _patch_deps_vector(monkeypatch, _val, _cred)

    assert (
        await VectorCapitalGate.assert_can_graduate(
            _VEC_PASS_STATS, _SentinelPool()
        )
        is True
    )


async def test_vector_assert_can_graduate_raises_credibility_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return False

    _patch_deps_vector(monkeypatch, _val, _cred)

    with pytest.raises(CredibilityScoreInsufficientError):
        await VectorCapitalGate.assert_can_graduate(
            _VEC_PASS_STATS, _SentinelPool()
        )


@pytest.mark.parametrize("exc", [ValidationStaleError, ValidationFailedError])
async def test_vector_assert_can_graduate_propagates_validation_errors(
    monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    async def _val(*a: object, **k: object) -> None:
        raise exc("data gate not satisfied")

    async def _cred(*a: object, **k: object) -> bool:  # pragma: no cover
        return True

    _patch_deps_vector(monkeypatch, _val, _cred)

    with pytest.raises(exc):
        await VectorCapitalGate.assert_can_graduate(
            _VEC_PASS_STATS, _SentinelPool()
        )


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (VectorGraduationStats(n_trades=30, win_rate=0.55, avg_return=0.03), True),
        (VectorGraduationStats(n_trades=29, win_rate=0.55, avg_return=0.03), False),
        (VectorGraduationStats(n_trades=30, win_rate=0.54, avg_return=0.03), False),
        (VectorGraduationStats(n_trades=30, win_rate=0.55, avg_return=0.029), False),
        (VectorGraduationStats(n_trades=0, win_rate=0.0, avg_return=0.0), False),
    ],
)
def test_vector_is_graduated_thresholds(
    stats: VectorGraduationStats, expected: bool
) -> None:
    assert VectorCapitalGate.is_graduated(stats) is expected


# ════════════════════════════════════════════════════════════════════════════
# Lean P5.5c — MOMENTUM assert_can_graduate consolidation. Momentum is a
# BATCH engine: it shares ONLY the `assert_can_graduate` shape (via the
# shared free function), NOT per-trade `check_trade` (it must NOT subclass
# `PerTradeCapitalGateBase` — that would wrongly inherit per-trade
# `check_trade`; spec §7 D2). This characterization family pins momentum's
# CURRENT observable `assert_can_graduate` behavior (raise/return matrix +
# `is_graduated` thresholds over `MomentumGraduationStats`) with the I/O
# dependencies mocked — independent expectation, no real DB/network.
#
# Consistent with the reversion/vector §2 blocks above, the credibility
# assertion pins the exception TYPE (`CredibilityScoreInsufficientError`),
# NOT the message text. The one-word `row`→`run` message normalization
# (momentum's lone pre-existing outlier vs the canonical
# `{engine.capitalize()} ... rubric run on record` form used by the base /
# reversion / vector / engine_template) is the intended, documented dedup
# outcome of consolidating onto the shared free function — NOT masking.
# ════════════════════════════════════════════════════════════════════════════


_MOM_PASS_STATS = MomentumGraduationStats(
    n_rebalances=6, sharpe_annualized=1.5, profit_factor=2.0
)
_MOM_FAIL_STATS = MomentumGraduationStats(
    n_rebalances=1, sharpe_annualized=1.5, profit_factor=2.0
)


async def test_momentum_assert_can_graduate_short_circuits_when_not_graduated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"validation": False, "cred": False}

    async def _val(*a: object, **k: object) -> None:
        called["validation"] = True

    async def _cred(*a: object, **k: object) -> bool:
        called["cred"] = True
        return True

    _patch_deps_momentum(monkeypatch, _val, _cred)

    result = await MomentumCapitalGate.assert_can_graduate(
        _MOM_FAIL_STATS, _SentinelPool()
    )
    assert result is False
    # Short-circuit: neither I/O dependency is consulted.
    assert called == {"validation": False, "cred": False}


async def test_momentum_assert_can_graduate_true_when_all_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return True

    _patch_deps_momentum(monkeypatch, _val, _cred)

    assert (
        await MomentumCapitalGate.assert_can_graduate(
            _MOM_PASS_STATS, _SentinelPool()
        )
        is True
    )


async def test_momentum_assert_can_graduate_raises_credibility_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return False

    _patch_deps_momentum(monkeypatch, _val, _cred)

    # Pins the exception TYPE (consistent with reversion/vector §2) — the
    # message text is the deliberately-normalized `row`→`run` consolidation.
    with pytest.raises(CredibilityScoreInsufficientError):
        await MomentumCapitalGate.assert_can_graduate(
            _MOM_PASS_STATS, _SentinelPool()
        )


@pytest.mark.parametrize("exc", [ValidationStaleError, ValidationFailedError])
async def test_momentum_assert_can_graduate_propagates_validation_errors(
    monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    async def _val(*a: object, **k: object) -> None:
        raise exc("data gate not satisfied")

    async def _cred(*a: object, **k: object) -> bool:  # pragma: no cover
        return True

    _patch_deps_momentum(monkeypatch, _val, _cred)

    with pytest.raises(exc):
        await MomentumCapitalGate.assert_can_graduate(
            _MOM_PASS_STATS, _SentinelPool()
        )


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (
            MomentumGraduationStats(
                n_rebalances=MOM_GRAD_MIN_REBALANCES,
                sharpe_annualized=MOM_GRAD_MIN_SHARPE,
                profit_factor=MOM_GRAD_MIN_PROFIT_FACTOR,
            ),
            True,
        ),
        (
            MomentumGraduationStats(
                n_rebalances=MOM_GRAD_MIN_REBALANCES - 1,
                sharpe_annualized=MOM_GRAD_MIN_SHARPE,
                profit_factor=MOM_GRAD_MIN_PROFIT_FACTOR,
            ),
            False,
        ),
        (
            MomentumGraduationStats(
                n_rebalances=MOM_GRAD_MIN_REBALANCES,
                sharpe_annualized=MOM_GRAD_MIN_SHARPE - 0.01,
                profit_factor=MOM_GRAD_MIN_PROFIT_FACTOR,
            ),
            False,
        ),
        (
            MomentumGraduationStats(
                n_rebalances=MOM_GRAD_MIN_REBALANCES,
                sharpe_annualized=MOM_GRAD_MIN_SHARPE,
                profit_factor=MOM_GRAD_MIN_PROFIT_FACTOR - 0.01,
            ),
            False,
        ),
        (
            MomentumGraduationStats(
                n_rebalances=0, sharpe_annualized=0.0, profit_factor=0.0
            ),
            False,
        ),
    ],
)
def test_momentum_is_graduated_thresholds(
    stats: MomentumGraduationStats, expected: bool
) -> None:
    assert MomentumCapitalGate.is_graduated(stats) is expected
