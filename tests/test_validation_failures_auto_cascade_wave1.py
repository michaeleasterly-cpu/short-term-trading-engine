"""Wave-1 deterministic self-heal cascade — D6 D7 D8 D9 D10 regression.

Pins each row in the spec
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md`` Wave 1 (D6..D10). Each test mocks
``data_validation`` reporting a specific failed check and asserts the
canonical refresh stage / dedupe / per-indicator pull is invoked with the
expected params + the expected ``INGESTION_AUTO_RECOVERED_*`` event lands
in application_log.

Tests:
* D6 — ``fundamentals_quarterly_completeness`` red → ``fundamentals_refresh``
  invoked with ``skip_guard_days=0``; ``INGESTION_AUTO_RECOVERED_VALIDATION``
  event lands.
* D7 — ``earnings_events_monotone`` red → ``dedupe_monotone`` runs (finds
  + deletes synthetic rogue rows) then ``earnings_refresh`` invoked;
  ``INGESTION_AUTO_RECOVERED_MONOTONE`` event lands.
* D8 — ``macro_indicators_completeness`` red → per-indicator FRED pull
  invoked for the specific gap indicators (NOT the full macro stage);
  ``INGESTION_AUTO_RECOVERED_MACRO_GAP`` event lands.
* D9 — ``liquidity_tiers_completeness`` red with 15 missing tickers →
  ``tier_refresh`` invoked with ``skip_guard_days=0``; the 15 missing
  tickers are carried in the cascade telemetry;
  ``INGESTION_AUTO_RECOVERED_TIER`` event lands.
* D10 — ``ticker_classifications_coverage`` red → ``classify_tickers``
  invoked with ``force=True, skip_guard_days=0``;
  ``INGESTION_AUTO_RECOVERED_CLASSIFICATION`` event lands.
"""
from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

# Load scripts/ops.py by path under a private name (canonical
# ops-shadow pattern — same trick used by
# test_daily_bars_coverage_collapse_auto_cascade.py).
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_validation_cascade", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_validation_cascade"] = ops
_spec.loader.exec_module(ops)


# pytest-xdist: pin this ops-shadow module to one worker so the
# scripts/ops.py load stays single-process.
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────


class _FakePool:
    """No-op pool — stages are stubbed so the pool is never touched."""


class _FakeDBLog:
    def __init__(self) -> None:
        self.run_id = uuid.uuid4()
        self.events: list[dict] = []

    async def log(
        self,
        event_type: str,
        message: str,
        severity: str = "INFO",
        data: dict | None = None,
    ) -> None:
        self.events.append({
            "event_type": event_type,
            "message": message,
            "severity": severity,
            "data": data or {},
        })


def _install_logger():
    import structlog
    return structlog.get_logger("test.validation_cascade")


def _patch_data_validation_only_pipeline(
    monkeypatch,
    *,
    failed_check_names: list[str],
    validation_stub_calls: list[dict],
    cascade_stubs: dict[str, object],
):
    """Reduce ``cmd_update`` to a single-stage data_validation pipeline
    that fails with the given check names, then enable specific cascade
    stages to be stubbed for assertion.

    ``cascade_stubs``: ``{stage_name: callable(pool, cfg) -> dict|raises}``
    — patched into ops module so the cascade dispatches them.
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 7}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop_tripwire(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop_tripwire)

    async def _noop_self_heal(*a, **k):
        return None

    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop_self_heal)
    monkeypatch.setattr(
        ops, "_auto_cascade_coverage_collapse", _noop_self_heal,
    )

    # data_validation stub — raises with the requested failed-check list.
    async def _failing_data_validation(_pool):
        validation_stub_calls.append({"called": True})
        raise RuntimeError(
            f"validation suite failed: {failed_check_names!r}"
        )

    monkeypatch.setattr(
        ops, "_stage_data_validation", _failing_data_validation,
    )

    # Build a minimal _STAGE_SPECS with data_validation FIRST + every
    # cascade-target stage. Daily-cycle re-runs are suppressed by also
    # extending _OFF_CYCLE_STAGES with the cascade targets — the daily
    # loop skips them, only the cascade reaches them via spec_by_name.
    spec_list: list[tuple] = [
        (
            "data_validation",
            lambda pool, cfg: (
                lambda: ops._stage_data_validation(pool)
            ),
            300.0,
        ),
    ]
    cascade_target_names: set[str] = set()
    for stage_name, stub_fn in cascade_stubs.items():
        spec_list.append(
            (
                stage_name,
                lambda pool, cfg, _fn=stub_fn: (lambda: _fn(pool, cfg)),
                ops.HEAVY_STAGE_TIMEOUT_SEC,
            ),
        )
        cascade_target_names.add(stage_name)
        # Also patch the underlying _stage_* attr so per-name lookups
        # in helpers go through our stub.
        attr = f"_stage_{stage_name}"
        if hasattr(ops, attr):
            monkeypatch.setattr(ops, attr, stub_fn)

    monkeypatch.setattr(ops, "_STAGE_SPECS", tuple(spec_list))
    # Mark cascade-target stages as off-cycle so the daily loop skips
    # them — the cascade still reaches them via spec_by_name.
    monkeypatch.setattr(
        ops, "_OFF_CYCLE_STAGES",
        frozenset(ops._OFF_CYCLE_STAGES | cascade_target_names),
    )


# ────────────────────────────────────────────────────────────────────────
# Parse-helper unit test — the regex extractor for failed-check names.
# ────────────────────────────────────────────────────────────────────────


def test_parse_failed_check_names_extracts_list_repr():
    msg = (
        "validation suite failed: "
        "['fundamentals_quarterly_completeness', "
        "'liquidity_tiers_completeness']"
    )
    names = ops._parse_failed_check_names(msg)
    assert names == [
        "fundamentals_quarterly_completeness",
        "liquidity_tiers_completeness",
    ]


def test_parse_failed_check_names_returns_empty_on_unmatched():
    assert ops._parse_failed_check_names(None) == []
    assert ops._parse_failed_check_names("some other failure") == []
    assert ops._parse_failed_check_names("") == []


def test_parse_failed_check_names_deduplicates():
    msg = (
        "validation suite failed: "
        "['fundamentals_quarterly_completeness', "
        "'fundamentals_quarterly_completeness']"
    )
    assert ops._parse_failed_check_names(msg) == [
        "fundamentals_quarterly_completeness",
    ]


# ────────────────────────────────────────────────────────────────────────
# D6 — fundamentals_quarterly_completeness red → fundamentals_refresh
# ────────────────────────────────────────────────────────────────────────


async def test_d6_fundamentals_completeness_red_invokes_fundamentals_refresh(
    monkeypatch,
):
    validation_calls: list[dict] = []
    refresh_calls: list[dict] = []

    async def _stub_fundamentals_refresh(pool, cfg):
        refresh_calls.append({"cfg": dict(cfg)})
        return {"rows_loaded": 42, "stub": True}

    _patch_data_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["fundamentals_quarterly_completeness"],
        validation_stub_calls=validation_calls,
        cascade_stubs={"fundamentals_refresh": _stub_fundamentals_refresh},
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. data_validation ran; cascade refresh stage ran exactly once.
    assert len(validation_calls) == 1
    assert len(refresh_calls) == 1, refresh_calls
    # 2. The cascade passed skip_guard_days=0 to force past skip-guard.
    assert refresh_calls[0]["cfg"].get("skip_guard_days") == 0

    # 3. The cascade's event lands.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_VALIDATION" in event_types, event_types
    recovered = next(
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION"
    )
    assert recovered["data"].get("check") == "fundamentals_quarterly_completeness"
    assert recovered["data"].get("refresh_stage") == "fundamentals_refresh"
    assert recovered["data"].get("refresh_status") == "OK"

    # 4. data_validation StageResult is annotated with the cascade meta.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert len(val_rows) == 1
    assert val_rows[0].detail.get("cascade") is True
    assert val_rows[0].detail.get("handled") == [
        "fundamentals_quarterly_completeness",
    ]


# ────────────────────────────────────────────────────────────────────────
# D7 — earnings_events_monotone red → dedupe + earnings_refresh
# ────────────────────────────────────────────────────────────────────────


async def test_d7_monotone_red_runs_dedupe_then_earnings_refresh(monkeypatch):
    validation_calls: list[dict] = []
    refresh_calls: list[dict] = []
    dedupe_calls: list[dict] = []

    async def _stub_earnings_refresh(pool, cfg):
        refresh_calls.append({"cfg": dict(cfg)})
        return {"rows_loaded": 7, "stub": True}

    # Synthetic dedupe — assert it was called with the right table.
    async def _stub_dedupe_monotone(pool, cfg):
        dedupe_calls.append({"cfg": dict(cfg)})
        # Simulate finding + deleting one rogue row.
        return {
            "earnings_events": {"found": 1, "deleted": 1},
        }

    _patch_data_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["earnings_events_monotone"],
        validation_stub_calls=validation_calls,
        cascade_stubs={
            "earnings_refresh": _stub_earnings_refresh,
            "dedupe_monotone": _stub_dedupe_monotone,
        },
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. Dedupe ran on the earnings_events table.
    assert len(dedupe_calls) == 1, dedupe_calls
    assert dedupe_calls[0]["cfg"].get("table") == "platform.earnings_events"
    # 2. earnings_refresh ran after dedupe.
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["cfg"].get("skip_guard_days") == 0
    # 3. Cascade event fired.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_MONOTONE" in event_types, event_types
    ev = next(
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_MONOTONE"
    )
    assert ev["data"].get("check") == "earnings_events_monotone"
    assert ev["data"].get("target_table") == "platform.earnings_events"
    assert ev["data"].get("dedupe") == {
        "earnings_events": {"found": 1, "deleted": 1},
    }
    assert ev["data"].get("refresh_status") == "OK"
    # 4. data_validation annotation.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert val_rows[0].detail.get("handled") == ["earnings_events_monotone"]


async def test_d7_dedupe_stage_function_finds_and_deletes_rogues():
    """Direct unit test for _stage_dedupe_monotone — synthetic pool that
    returns 3 rogue rows then 0 after delete."""

    class _SyntheticConn:
        def __init__(self):
            self.scan_calls = 0
            self.deletes: list[str] = []

        async def fetchval(self, sql, *args):
            # First call returns 3 rogues, second 0 — emulates the
            # post-delete recount (not used in current impl but defensive).
            self.scan_calls += 1
            return 3

        async def execute(self, sql, *args):
            self.deletes.append(sql)
            return "DELETE 3"

    class _SyntheticPool:
        def __init__(self):
            self.conn = _SyntheticConn()

        def acquire(self):  # type: ignore[no-redef]
            pool_self = self

            class _Cm:
                async def __aenter__(self_inner):
                    return pool_self.conn

                async def __aexit__(self_inner, *exc):
                    return False

            return _Cm()

    pool = _SyntheticPool()
    out = await ops._stage_dedupe_monotone(
        pool, {"table": "platform.earnings_events"},
    )
    assert out["earnings_events"] == {"found": 3, "deleted": 3}
    assert len(pool.conn.deletes) == 1
    # ``platform.earnings_events`` was the target.
    assert "platform.earnings_events" in pool.conn.deletes[0]


# ────────────────────────────────────────────────────────────────────────
# D8 — macro_indicators_completeness red → per-indicator FRED pull
# ────────────────────────────────────────────────────────────────────────


async def test_d8_macro_gap_red_triggers_per_indicator_repull(monkeypatch):
    validation_calls: list[dict] = []
    repull_calls: list[dict] = []

    async def _stub_per_indicator_pull(pool, indicators, *, start=None, end=None):
        repull_calls.append({
            "indicators": list(indicators),
            "start": start,
            "end": end,
        })
        return {ind: 100 for ind in indicators}

    async def _stub_macro_repair_targets(pool):
        # Two indicators with gaps, oldest missing 14 days back.
        return ["hy_spread", "initial_claims"], 21

    # Patch the helpers used by the cascade — these come from a deferred
    # import inside the cascade function, so set on the module path.
    import tpcore.fred
    monkeypatch.setattr(
        tpcore.fred, "per_indicator_fred_repull",
        _stub_per_indicator_pull,
    )
    import tpcore.quality.validation.checks.macro_indicators_completeness as macro_mod
    monkeypatch.setattr(
        macro_mod, "compute_macro_repair_targets",
        _stub_macro_repair_targets,
    )

    _patch_data_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["macro_indicators_completeness"],
        validation_stub_calls=validation_calls,
        cascade_stubs={},
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. Targeted re-pull called for exactly the two failed indicators.
    assert len(repull_calls) == 1, repull_calls
    assert repull_calls[0]["indicators"] == ["hy_spread", "initial_claims"]
    # 2. start date set to ~21 days ago (or close — within 2 days).
    today = date.today()  # noqa: DTZ011
    assert repull_calls[0]["start"] is not None
    delta_days = (today - repull_calls[0]["start"]).days
    assert 19 <= delta_days <= 23, (
        f"start should be ~21d ago, got {repull_calls[0]['start']} "
        f"(delta={delta_days})"
    )
    # 3. Cascade event fired with the indicator list.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_MACRO_GAP" in event_types, event_types
    ev = next(
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_MACRO_GAP"
    )
    assert ev["data"].get("indicators") == ["hy_spread", "initial_claims"]
    assert ev["data"].get("lookback_days") == 21
    # 4. data_validation annotation.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert val_rows[0].detail.get("handled") == [
        "macro_indicators_completeness",
    ]


# ────────────────────────────────────────────────────────────────────────
# D9 — liquidity_tiers_completeness red w/ 15 tickers → tier_refresh
# ────────────────────────────────────────────────────────────────────────


async def test_d9_tier_completeness_red_invokes_tier_refresh_with_15_tickers(
    monkeypatch,
):
    validation_calls: list[dict] = []
    refresh_calls: list[dict] = []
    missing_15 = [f"TICK{i:02d}" for i in range(15)]

    async def _stub_tier_refresh(pool, cfg):
        refresh_calls.append({"cfg": dict(cfg)})
        return {"tickers_assigned": 7000, "stub": True}

    async def _stub_repair_targets(pool):
        return list(missing_15)

    import tpcore.quality.validation.checks.liquidity_tiers_completeness as liq_mod
    monkeypatch.setattr(
        liq_mod, "compute_liquidity_tiers_repair_targets",
        _stub_repair_targets,
    )

    _patch_data_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["liquidity_tiers_completeness"],
        validation_stub_calls=validation_calls,
        cascade_stubs={"tier_refresh": _stub_tier_refresh},
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. tier_refresh called once with skip_guard_days=0.
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["cfg"].get("skip_guard_days") == 0
    # 2. Cascade event carries the 15 missing tickers as telemetry.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_TIER" in event_types, event_types
    ev = next(
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_TIER"
    )
    assert ev["data"].get("missing_tickers_count") == 15
    assert ev["data"].get("missing_tickers_sample")[:15] == missing_15
    assert ev["data"].get("refresh_status") == "OK"
    # 3. data_validation annotation.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert val_rows[0].detail.get("handled") == [
        "liquidity_tiers_completeness",
    ]


# ────────────────────────────────────────────────────────────────────────
# D10 — ticker_classifications_coverage red → classify_tickers (force)
# ────────────────────────────────────────────────────────────────────────


async def test_d10_classifications_red_invokes_classify_tickers_force(
    monkeypatch,
):
    validation_calls: list[dict] = []
    refresh_calls: list[dict] = []

    async def _stub_classify_tickers(pool, cfg):
        refresh_calls.append({"cfg": dict(cfg)})
        return {"stocks": 7000, "etfs": 1000, "stub": True}

    _patch_data_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["ticker_classifications_coverage"],
        validation_stub_calls=validation_calls,
        cascade_stubs={"classify_tickers": _stub_classify_tickers},
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. classify_tickers called with force=True + skip_guard_days=0.
    assert len(refresh_calls) == 1
    cfg = refresh_calls[0]["cfg"]
    assert cfg.get("force") is True, cfg
    assert cfg.get("skip_guard_days") == 0, cfg
    # 2. Cascade event fired.
    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERED_CLASSIFICATION" in event_types, event_types
    ev = next(
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_CLASSIFICATION"
    )
    assert ev["data"].get("check") == "ticker_classifications_coverage"
    assert ev["data"].get("refresh_stage") == "classify_tickers"
    assert ev["data"].get("refresh_status") == "OK"
    # 3. data_validation annotation.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert val_rows[0].detail.get("handled") == [
        "ticker_classifications_coverage",
    ]


# ────────────────────────────────────────────────────────────────────────
# Unmapped check — long-tail skipped event, no cascade.
# ────────────────────────────────────────────────────────────────────────


async def test_unmapped_check_emits_skipped_event_and_no_cascade(monkeypatch):
    validation_calls: list[dict] = []

    _patch_data_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["some_brand_new_unmapped_check"],
        validation_stub_calls=validation_calls,
        cascade_stubs={},
    )
    log = _install_logger()
    db_log = _FakeDBLog()

    await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    event_types = [e["event_type"] for e in db_log.events]
    assert "INGESTION_AUTO_RECOVERY_VALIDATION_SKIPPED" in event_types
    assert "INGESTION_AUTO_RECOVERED_VALIDATION" not in event_types
    assert "INGESTION_AUTO_RECOVERED_MONOTONE" not in event_types


# ────────────────────────────────────────────────────────────────────────
# Structural pin — every event name the cascade emits is referenced in
# the spec's exposed contract. Keeps the spec ↔ code names locked.
# ────────────────────────────────────────────────────────────────────────


def test_cascade_event_name_contract_pinned():
    """The 5 documented Wave-1 cascade event names must all live in
    scripts/ops.py — drift between spec + code surfaces as a missing
    event-name. The names are also documented in the cascade function's
    docstring; a rename must propagate."""
    expected_event_names = {
        "INGESTION_AUTO_RECOVERED_VALIDATION",
        "INGESTION_AUTO_RECOVERED_MONOTONE",
        "INGESTION_AUTO_RECOVERED_MACRO_GAP",
        "INGESTION_AUTO_RECOVERED_TIER",
        "INGESTION_AUTO_RECOVERED_CLASSIFICATION",
    }
    src = _OPS_PATH.read_text()
    for name in expected_event_names:
        assert name in src, f"missing cascade event name in ops.py: {name}"
