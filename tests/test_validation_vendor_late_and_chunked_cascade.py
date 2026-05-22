"""D11 + D14 deterministic self-heal cascade regression.

Pins the spec rows
``docs/superpowers/specs/2026-05-21-deterministic-self-heal-coverage-
expansion-design.md`` §1 D11 + D14:

* **D11** — Freshness vendor_late classification. When a freshness check
  is red but the vendor's ``latest_published`` probe says the vendor has
  nothing newer than what we hold (``has_newer == False``), the cascade
  emits ``INGESTION_VENDOR_LATE_SKIPPED`` and the Wave-1 cascade
  ``_auto_cascade_validation_failures`` skips dispatching a (useless)
  refresh for that check. The freshness check stays red — D11 is a
  CLASSIFICATION (not-our-defect), not a relaxation.

* **D14** — ``data_validation`` stage TIMEOUT. The Wave-1 cascade is
  keyed on a FAILED check_name list which a TIMEOUT does NOT produce.
  The D14 cascade detects the TIMEOUT, runs the suite chunked (each
  chunk under its own 60s budget), aggregates failed-check names across
  chunks, and synthesises a FAILED data_validation entry whose error
  matches the canonical ``"validation suite failed: [<names>]"`` shape
  the Wave-1 cascade parser consumes. Emits
  ``INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED``.

These tests assert end-to-end through ``cmd_update`` so the dispatch
order (D14 → D11 → Wave-1) is exercised, not just the helpers.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

# Load scripts/ops.py by path under a private name (canonical ops-shadow
# pattern — same trick used by the Wave-1 / Wave-2 cascade tests).
_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"
_spec = importlib.util.spec_from_file_location(
    "_ops_under_test_d11_d14_cascade", _OPS_PATH,
)
assert _spec is not None and _spec.loader is not None
ops = importlib.util.module_from_spec(_spec)
sys.modules["_ops_under_test_d11_d14_cascade"] = ops
_spec.loader.exec_module(ops)


# pytest-xdist: pin this ops-shadow module to one worker so the
# scripts/ops.py load stays single-process (ops-package-shadow invariant).
pytestmark = pytest.mark.xdist_group("ops_shadow")


# ────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────


class _FakePool:
    """No-op pool — stages are stubbed so the pool is never touched."""

    async def close(self):
        return None


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
    return structlog.get_logger("test.d11_d14_cascade")


# ════════════════════════════════════════════════════════════════════════
# D11 — vendor_late classification
# ════════════════════════════════════════════════════════════════════════


def _patch_validation_only_pipeline(
    monkeypatch,
    *,
    failed_check_names: list[str],
):
    """Reduce ``cmd_update`` to a single-stage data_validation pipeline
    that fails with the given check names. Wave-1 and Wave-2 cascades
    are left wired so D11 → Wave-1 ordering is observable.
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 7}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_coverage_collapse", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_stage_robustness", _noop)
    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop)

    async def _failing_data_validation(_pool):
        raise RuntimeError(
            f"validation suite failed: {failed_check_names!r}"
        )

    monkeypatch.setattr(
        ops, "_stage_data_validation", _failing_data_validation,
    )

    # Reduce _STAGE_SPECS to data_validation only — keeps the cmd_update
    # loop fast and prevents incidental cascade-target dispatch.
    monkeypatch.setattr(
        ops,
        "_STAGE_SPECS",
        (
            (
                "data_validation",
                lambda pool, cfg: (
                    lambda: ops._stage_data_validation(pool)
                ),
                300.0,
            ),
        ),
    )
    # Suppress LLM-side skipped event noise so the assertion set is clean.


async def test_d11_aaii_vendor_late_emits_distinct_event_and_skips_refresh(
    monkeypatch,
):
    """AAII freshness red + vendor probe says vendor has nothing newer.

    Cascade emits ``INGESTION_VENDOR_LATE_SKIPPED`` (the D11 distinct
    event), classifies the check as vendor_late in the data_validation
    detail, and the Wave-1 cascade does NOT route AAII to the unmapped
    long-tail skip path (would emit INGESTION_AUTO_RECOVERY_VALIDATION_
    SKIPPED) — D11 has claimed it.
    """
    _patch_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["aaii_sentiment_freshness"],
    )

    # Patch VENDOR_PROBES so the AAII probe says vendor has nothing newer.
    from tpcore.selfheal import probes

    async def _vendor_late_probe(_pool):
        return probes.VendorState(
            our_latest=date(2026, 5, 14),
            vendor_latest=date(2026, 5, 14),
            has_newer=False,
        )

    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", _vendor_late_probe,
    )

    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. The distinct vendor-late event landed once.
    vl_events = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_VENDOR_LATE_SKIPPED"
    ]
    assert len(vl_events) == 1, [e["event_type"] for e in db_log.events]
    assert vl_events[0]["data"]["check"] == "aaii_sentiment_freshness"
    assert vl_events[0]["data"]["feed"] == "aaii_sentiment"
    assert vl_events[0]["data"]["our_latest"] == "2026-05-14"
    assert vl_events[0]["data"]["vendor_latest"] == "2026-05-14"

    # 2. The Wave-1 cascade did NOT emit the long-tail skipped event
    # (D11 claimed the check).
    longtail = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERY_VALIDATION_SKIPPED"
    ]
    assert longtail == [], "D11 must claim the check before Wave-1 long-tail"

    # 3. The data_validation entry stays FAILED — D11 is a CLASSIFICATION,
    # not a relaxation.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert len(val_rows) == 1
    assert val_rows[0].status == "FAILED"

    # 4. The detail carries the vendor_late classification.
    assert val_rows[0].detail.get("vendor_late_checks") == [
        "aaii_sentiment_freshness",
    ]
    assert val_rows[0].detail.get("vendor_late") == [
        "aaii_sentiment_freshness",
    ]


async def test_d11_vendor_ahead_does_not_classify(monkeypatch):
    """Vendor IS ahead → genuine staleness; D11 must NOT emit
    INGESTION_VENDOR_LATE_SKIPPED. The Wave-1 cascade then routes the
    check to its normal path (in this case, the long-tail skip since
    aaii_sentiment_freshness isn't in ``_VALIDATION_CASCADE_MAP``).
    """
    _patch_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["aaii_sentiment_freshness"],
    )

    from tpcore.selfheal import probes

    async def _vendor_ahead_probe(_pool):
        return probes.VendorState(
            our_latest=date(2026, 5, 7),
            vendor_latest=date(2026, 5, 14),
            has_newer=True,
        )

    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", _vendor_ahead_probe,
    )

    log = _install_logger()
    db_log = _FakeDBLog()

    await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # No vendor-late event — vendor IS ahead.
    vl_events = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_VENDOR_LATE_SKIPPED"
    ]
    assert vl_events == [], [e["event_type"] for e in db_log.events]


async def test_d11_indeterminate_probe_stays_strict(monkeypatch):
    """Probe returns None → undeterminable → stay strict (no event).

    This pins the publication-gate contract: vendor_late MUST be PROVEN
    by a positive probe. An unprovable answer never silently classifies
    as vendor_late.
    """
    _patch_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["aaii_sentiment_freshness"],
    )

    from tpcore.selfheal import probes

    async def _broken_probe(_pool):
        return None  # probe couldn't determine vendor state

    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", _broken_probe,
    )

    log = _install_logger()
    db_log = _FakeDBLog()

    await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    vl_events = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_VENDOR_LATE_SKIPPED"
    ]
    assert vl_events == [], "indeterminate probe must stay strict"


async def test_d11_probe_exception_does_not_crash_cascade(monkeypatch):
    """A probe that raises must NOT abort the daemon — the cascade
    catches the exception, logs it, and skips the classification.
    """
    _patch_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["aaii_sentiment_freshness"],
    )

    from tpcore.selfheal import probes

    async def _crashing_probe(_pool):
        raise RuntimeError("transient probe failure")

    monkeypatch.setitem(
        probes.VENDOR_PROBES, "aaii_sentiment", _crashing_probe,
    )

    log = _install_logger()
    db_log = _FakeDBLog()

    # Must not raise — cascade is wrapped per the daemon-alive invariant.
    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )
    assert summary is not None


async def test_d11_check_not_in_map_is_passthrough(monkeypatch):
    """A red check that isn't in ``_VENDOR_LATE_CHECK_MAP`` is a no-op
    for D11. The Wave-1 cascade handles it normally.
    """
    _patch_validation_only_pipeline(
        monkeypatch,
        failed_check_names=["row_integrity"],  # NOT in _VENDOR_LATE_CHECK_MAP
    )

    log = _install_logger()
    db_log = _FakeDBLog()
    await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    vl_events = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_VENDOR_LATE_SKIPPED"
    ]
    assert vl_events == []


def test_d11_vendor_late_check_map_anchors_aaii_and_fear_greed():
    """Structural pin: the spec D11 wiring is for AAII Thursday +
    fear_greed daily. The map MUST contain both check names, and they
    MUST map to feed keys that are present in VENDOR_PROBES.
    """
    from tpcore.selfheal.probes import VENDOR_PROBES

    assert "aaii_sentiment_freshness" in ops._VENDOR_LATE_CHECK_MAP
    assert "fear_greed_freshness" in ops._VENDOR_LATE_CHECK_MAP

    for check_name, feed_key in ops._VENDOR_LATE_CHECK_MAP.items():
        assert feed_key in VENDOR_PROBES, (
            f"D11 map {check_name} → {feed_key} must have a probe in "
            f"VENDOR_PROBES (have: {sorted(VENDOR_PROBES)})"
        )


# ════════════════════════════════════════════════════════════════════════
# D14 — data_validation TIMEOUT → chunked re-run
# ════════════════════════════════════════════════════════════════════════


async def test_d14_data_validation_timeout_triggers_chunked_re_run(monkeypatch):
    """data_validation TIMEOUT → cascade calls the chunked helper, gets
    a synthesised failed-check list, replaces the TIMEOUT entry with a
    FAILED entry whose error matches the canonical Wave-1 parser shape,
    and emits ``INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED``. The
    Wave-1 cascade then dispatches refreshes for the failed checks.
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 7}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_coverage_collapse", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_stage_robustness", _noop)
    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop)
    # Neutralise Wave-1 — D14 tests focus on TIMEOUT → synthesise.
    monkeypatch.setattr(ops, "_auto_cascade_validation_failures", _noop)

    # data_validation stage stub — raises TimeoutError so _run_stage
    # records status=TIMEOUT (the cascade's trigger).
    async def _timing_out_data_validation(_pool):
        # Sleep longer than the stage timeout we install below.
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(
        ops, "_stage_data_validation", _timing_out_data_validation,
    )

    # Install a tiny stage timeout (0.05s) so the validation stage races
    # past the budget. _run_stage's asyncio.wait_for emits the TIMEOUT.
    monkeypatch.setattr(
        ops,
        "_STAGE_SPECS",
        (
            (
                "data_validation",
                lambda pool, cfg: (
                    lambda: ops._stage_data_validation(pool)
                ),
                0.05,
            ),
        ),
    )

    # Stub the chunked helper so it returns a known failed-check list.
    async def _stub_chunked(pool, *, log, chunk_budget_sec=60.0):
        return {
            "failed_checks": [
                "fundamentals_quarterly_completeness",
                "liquidity_tiers_completeness",
            ],
            "chunks": [
                {
                    "chunk": "completeness",
                    "checks": [
                        "fundamentals_quarterly_completeness",
                        "liquidity_tiers_completeness",
                    ],
                    "failed": [
                        "fundamentals_quarterly_completeness",
                        "liquidity_tiers_completeness",
                    ],
                    "timed_out": False,
                    "duration_ms": 1234,
                },
            ],
            "total_duration_ms": 1234,
            "any_chunk_timed_out": False,
        }

    monkeypatch.setattr(ops, "_chunk_validation_suite", _stub_chunked)

    log = _install_logger()
    db_log = _FakeDBLog()

    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    # 1. D14's event landed.
    chunked_events = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED"
    ]
    assert len(chunked_events) == 1, [e["event_type"] for e in db_log.events]
    assert chunked_events[0]["data"]["failed_checks"] == [
        "fundamentals_quarterly_completeness",
        "liquidity_tiers_completeness",
    ]
    assert chunked_events[0]["data"]["any_chunk_timed_out"] is False

    # 2. The data_validation entry was REPLACED with a synthetic FAILED
    # whose error matches the canonical Wave-1 parser shape.
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert len(val_rows) == 1
    val = val_rows[0]
    assert val.status == "FAILED"
    # The cascade's parser must be able to extract the failed-check list.
    parsed = ops._parse_failed_check_names(val.error)
    assert parsed == [
        "fundamentals_quarterly_completeness",
        "liquidity_tiers_completeness",
    ]
    # 3. The synthetic entry carries the chunked-recovery breadcrumb.
    assert val.detail.get("cascade_mode") == "validation_chunked"
    assert val.detail.get("chunked_recovery") is True


async def test_d14_chunked_re_run_green_marks_stage_ok(monkeypatch):
    """Chunked re-run produces zero failed checks → the suite green-
    flipped under the per-chunk budget. The cascade replaces the
    TIMEOUT entry with an OK entry so downstream cascades don't
    re-trigger; the recovered event still lands.
    """

    async def _fake_load_config(_pool):
        return {"universe": "active", "lookback_days": 7}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_coverage_collapse", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_stage_robustness", _noop)
    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_validation_failures", _noop)

    async def _timing_out(_pool):
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(ops, "_stage_data_validation", _timing_out)
    monkeypatch.setattr(
        ops,
        "_STAGE_SPECS",
        (
            (
                "data_validation",
                lambda pool, cfg: (
                    lambda: ops._stage_data_validation(pool)
                ),
                0.05,
            ),
        ),
    )

    async def _stub_green(pool, *, log, chunk_budget_sec=60.0):
        return {
            "failed_checks": [],
            "chunks": [],
            "total_duration_ms": 100,
            "any_chunk_timed_out": False,
        }

    monkeypatch.setattr(ops, "_chunk_validation_suite", _stub_green)

    log = _install_logger()
    db_log = _FakeDBLog()
    summary = await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    chunked_events = [
        e for e in db_log.events
        if e["event_type"] == "INGESTION_AUTO_RECOVERED_VALIDATION_CHUNKED"
    ]
    assert len(chunked_events) == 1
    val_rows = [s for s in summary.stages if s.name == "data_validation"]
    assert val_rows[0].status == "OK"


async def test_d14_helper_handles_per_chunk_timeout(monkeypatch):
    """Per-chunk timeout safe-degrade: every check in the chunk is
    treated as failed (joins the aggregate failed-check list); the
    chunks_telemetry records ``timed_out: True``.
    """
    # Patch out the safe-run + check imports with synchronous stubs that
    # don't touch the DB. Replace _safe_run with one that sleeps so the
    # per-chunk timeout fires.

    async def _slow_safe_run(name, fn, pool, source):
        await asyncio.sleep(10)
        return None  # never reached

    from tpcore.quality.validation import suite as suite_mod
    monkeypatch.setattr(suite_mod, "_safe_run", _slow_safe_run)

    # Reduce chunk specs to one tiny chunk with a sub-second budget.
    monkeypatch.setattr(
        ops,
        "_VALIDATION_CHUNK_SPECS",
        (
            ("test_chunk", ("aaii_sentiment_freshness",)),
        ),
    )

    log = _install_logger()
    out = await ops._chunk_validation_suite(
        _FakePool(), log=log, chunk_budget_sec=0.05,
    )

    assert out["any_chunk_timed_out"] is True
    assert out["failed_checks"] == ["aaii_sentiment_freshness"]
    assert len(out["chunks"]) == 1
    assert out["chunks"][0]["timed_out"] is True
    assert out["chunks"][0]["chunk"] == "test_chunk"


async def test_d14_helper_no_action_when_no_timeout(monkeypatch):
    """The cascade is a no-op when data_validation status is anything
    other than TIMEOUT (FAILED, OK, SKIPPED, …). Pin against accidental
    over-trigger.
    """

    async def _fake_load_config(_pool):
        return {"universe": "active"}

    monkeypatch.setattr(ops, "_load_daily_bars_config", _fake_load_config)
    monkeypatch.setattr(ops, "_market_open_block_reason", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ops, "_per_feed_tripwire", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_coverage_collapse", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_stage_robustness", _noop)
    monkeypatch.setattr(ops, "_auto_cascade_validation_failures", _noop)
    monkeypatch.setattr(ops, "_self_heal_failed_stages", _noop)

    # data_validation passes — no TIMEOUT.
    async def _passing(_pool):
        return {"passed": True, "checks": 25}

    monkeypatch.setattr(ops, "_stage_data_validation", _passing)
    monkeypatch.setattr(
        ops,
        "_STAGE_SPECS",
        (
            (
                "data_validation",
                lambda pool, cfg: (
                    lambda: ops._stage_data_validation(pool)
                ),
                300.0,
            ),
        ),
    )

    # Spy on the chunked helper — it must NOT be called.
    called: list[bool] = []

    async def _spy_chunked(pool, *, log, chunk_budget_sec=60.0):
        called.append(True)
        return {
            "failed_checks": [],
            "chunks": [],
            "total_duration_ms": 0,
            "any_chunk_timed_out": False,
        }

    monkeypatch.setattr(ops, "_chunk_validation_suite", _spy_chunked)

    log = _install_logger()
    db_log = _FakeDBLog()
    await ops.cmd_update(
        _FakePool(), log, db_log, dry_run=False, force=True,
    )

    assert called == [], "D14 must not fire when data_validation isn't TIMEOUT"


def test_d14_chunk_specs_partition_every_known_check():
    """Structural pin: the chunk specs must partition every check in
    the suite's KNOWN_CHECK_NAMES — no check silently dropped from the
    chunked re-run (would degrade D14 coverage vs the monolithic suite).
    """
    from tpcore.quality.validation.suite import KNOWN_CHECK_NAMES

    chunked: set[str] = set()
    for _name, check_names in ops._VALIDATION_CHUNK_SPECS:
        for cn in check_names:
            assert cn not in chunked, (
                f"check {cn} appears in multiple chunks — chunks must "
                f"partition, not overlap"
            )
            chunked.add(cn)

    missing = set(KNOWN_CHECK_NAMES) - chunked
    assert not missing, (
        f"D14 chunk specs are missing these checks from the canonical "
        f"suite: {sorted(missing)}. Every check in KNOWN_CHECK_NAMES "
        f"must appear in exactly one chunk so the chunked re-run has "
        f"the same coverage as the monolithic suite."
    )
    extra = chunked - set(KNOWN_CHECK_NAMES)
    assert not extra, (
        f"D14 chunk specs reference unknown checks: {sorted(extra)}. "
        f"These won't run because the chunked helper's check_fns "
        f"registry won't find them."
    )
