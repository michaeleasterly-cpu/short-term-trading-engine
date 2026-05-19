"""Lean P5.5a — EXHAUSTIVE characterization + `_legacy_*` parallel-diff for
the per-trade capital gate (HIGHEST-RISK live risk gate; cluster #3/#4).

Two test families, both in this one file:

1. **Characterization (TDD, written BEFORE the refactor):** pins
   ``reversion.plugs.capital_gate.ReversionCapitalGate``'s CURRENT
   observable behavior with an *independent* expectation (the plug is
   NOT used as its own oracle): every ``check_trade`` reject branch, the
   exact ``drawdown == -0.05`` boundary (both sides), the
   ``engine_equity == 0`` skip, and the EXACT emitted structlog event
   NAME per branch (forensics/dashboards key on the event string — it is
   observable behavior). Plus ``assert_can_graduate``'s full
   raise-vs-return matrix (``is_graduated`` short-circuit,
   ``assert_passed_for_engine`` path, ``graduation_ready`` true/false)
   with the I/O dependencies mocked — no real DB/network.

2. **Differential (`_legacy_*` parallel-diff):** over a fuzzed input
   grid (sizes, equities, position counts, drawdowns incl. the exact
   boundary, graduation states), asserts the consolidated method ==
   the kept ``_legacy_*`` method for EVERY case — same return / same
   exception type / same emitted event name. This proves byte-equivalence
   of the staged reversion cutover.

Engine-private access (``ReversionCapitalGate._legacy_*``) is the
documented purpose of the parallel-diff oracle — covered by the scoped
per-file ``SLF`` ignore in ``pyproject.toml`` (mirrors the
``test_stale_order_cancel.py`` precedent; never an inline noqa).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import structlog

import reversion.plugs.capital_gate as rev_mod
import tpcore.interfaces.capital_gate_base as base_mod
from reversion.plugs.capital_gate import GraduationStats, ReversionCapitalGate
from tpcore.backtest.credibility import CredibilityScoreInsufficientError
from tpcore.quality.validation.capital_gate import (
    ValidationFailedError,
    ValidationStaleError,
)


def _patch_deps(
    monkeypatch: pytest.MonkeyPatch, val: object, cred: object
) -> None:
    """Patch the validation/credibility I/O deps in BOTH the consolidated
    base module and the reversion module — the new (base) and `_legacy_*`
    (rev_mod) code paths resolve these from their own module globals, so
    a parity test must mock both for an apples-to-apples comparison."""
    for mod in (base_mod, rev_mod):
        monkeypatch.setattr(mod, "assert_passed_for_engine", val)
        monkeypatch.setattr(mod, "graduation_ready", cred)


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


# ── 3. `_legacy_*` parallel-diff (new == legacy over a fuzzed grid) ─────────
#
# The kept `_legacy_check_trade` / `_legacy_assert_can_graduate` are the
# pre-refactor reversion implementations. The consolidated overrides MUST
# be byte-equivalent: same return, same exception type, same emitted
# structlog event name, for EVERY grid point.

_SIZES = [Decimal("-50"), Decimal("0"), Decimal("1"), Decimal("2000"),
          Decimal("2000.01"), Decimal("5000")]
_PNLS = [Decimal("100"), Decimal("0"), Decimal("-499.99"), Decimal("-500"),
         Decimal("-500.01"), Decimal("-600"), Decimal("-99999")]
_POSCOUNTS = [0, 1, 4, 5, 6, 99]
_EQUITIES = [Decimal("10000"), Decimal("0"), Decimal("1")]


def test_check_trade_equals_legacy_over_fuzzed_grid() -> None:
    for equity in _EQUITIES:
        gate = ReversionCapitalGate(engine_equity=equity)
        for size in _SIZES:
            for pnl in _PNLS:
                for n in _POSCOUNTS:
                    cap_new = _capture()
                    try:
                        new = gate.check_trade(size, pnl, n)
                    finally:
                        structlog.reset_defaults()
                    cap_old = _capture()
                    try:
                        old = gate._legacy_check_trade(size, pnl, n)
                    finally:
                        structlog.reset_defaults()
                    ctx = f"equity={equity} size={size} pnl={pnl} n={n}"
                    assert new == old, ctx
                    assert _event_names(cap_new) == _event_names(cap_old), ctx


@pytest.mark.parametrize(
    "stats",
    [
        _PASS_STATS,
        _FAIL_STATS,
        GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.5),
        GraduationStats(n_trades=10, win_rate=0.55, avg_return=0.02, profit_factor=1.49),
    ],
)
@pytest.mark.parametrize(
    ("val_behavior", "cred_ready"),
    [
        ("ok", True),
        ("ok", False),
        ("stale", True),
        ("failed", True),
    ],
)
async def test_assert_can_graduate_equals_legacy_over_grid(
    monkeypatch: pytest.MonkeyPatch,
    stats: GraduationStats,
    val_behavior: str,
    cred_ready: bool,
) -> None:
    async def _val(*a: object, **k: object) -> None:
        if val_behavior == "stale":
            raise ValidationStaleError("stale")
        if val_behavior == "failed":
            raise ValidationFailedError("failed")
        return None

    async def _cred(*a: object, **k: object) -> bool:
        return cred_ready

    _patch_deps(monkeypatch, _val, _cred)

    async def _outcome(fn) -> object:
        try:
            return ("return", await fn(stats, _SentinelPool()))
        except Exception as exc:  # noqa: BLE001 — parity comparison of any raise
            return ("raise", type(exc))

    new = await _outcome(ReversionCapitalGate.assert_can_graduate)
    old = await _outcome(ReversionCapitalGate._legacy_assert_can_graduate)
    assert new == old
