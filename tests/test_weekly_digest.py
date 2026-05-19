"""Unit tests for the weekly digest + ack + auto-de-escalate.

Deterministic, no DB: a fake pool backed by an in-memory
application_log + data_quality_log, dispatched on the stable
event-type / source literals the module's queries use.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Multi-line spec_from_file_location of an ops/ module — grouped by the
# over-inclusion rule (verified non-poisoning, but safe to co-locate).
pytestmark = pytest.mark.xdist_group("ops_shadow")

_SPEC = importlib.util.spec_from_file_location(
    "_wd_under_test",
    Path(__file__).resolve().parents[1] / "ops" / "weekly_digest.py",
)
wd = importlib.util.module_from_spec(_SPEC)
sys.modules["_wd_under_test"] = wd
_SPEC.loader.exec_module(wd)

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)


class _Conn:
    def __init__(self, store: _Store) -> None:
        self._s = store

    async def fetch(self, sql: str, *args):
        s = self._s
        if "event_type = 'PROVIDER_CUTOVER'" in sql:
            return [r for r in s.applog if r["event_type"] == "PROVIDER_CUTOVER"]
        if "DATA_REPAIR_COMPLETE" in sql and "INGESTION_FAILED" in sql:
            keep = {"DATA_REPAIR_COMPLETE", "DATA_REPAIR_ESCALATED", "INGESTION_FAILED"}
            return [r for r in s.applog if r["event_type"] in keep]
        if "data_quality_log" in sql:
            return list(s.nearmiss)
        # event_type=$1 (+ optional iso_week=$2) lookups
        et = args[0] if args else None
        rows = [r for r in s.applog if r["event_type"] == et]
        if len(args) >= 2 and "iso_week" in sql:
            rows = [r for r in rows if r["data"].get("iso_week") == args[1]]
        if "DISTINCT data->>'iso_week' AS wk, MAX(recorded_at)" in sql:
            seen: dict[str, dict] = {}
            for r in sorted(rows, key=lambda x: x["recorded_at"]):
                seen[r["data"].get("iso_week")] = {
                    "wk": r["data"].get("iso_week"), "r": r["recorded_at"]}
            return sorted(seen.values(), key=lambda x: x["r"], reverse=True)[:8]
        if "DISTINCT data->>'iso_week' AS wk" in sql:
            return [{"wk": r["data"].get("iso_week")} for r in rows]
        if "ORDER BY recorded_at DESC LIMIT 1" in sql:
            rows = sorted(rows, key=lambda x: x["recorded_at"], reverse=True)[:1]
            return [{"wk": r["data"].get("iso_week")} for r in rows]
        if "LIMIT 1" in sql:
            return [{"1": 1}] if rows else []
        return rows

    async def execute(self, sql: str, *a):
        eng, rid, et, sev, msg, data = a
        import json
        self._s.applog.append({
            "event_type": et, "message": msg, "recorded_at": self._s.tick(),
            "data": json.loads(data),
        })


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Store:
    def __init__(self) -> None:
        self.applog: list[dict] = []
        self.nearmiss: list[dict] = []
        self._n = 0

    def tick(self):
        self._n += 1
        return NOW + timedelta(seconds=self._n)

    def acquire(self):
        return _CM(_Conn(self))


async def test_build_digest_assembles_sections() -> None:
    s = _Store()
    s.applog += [
        {"event_type": "PROVIDER_CUTOVER", "message": "macro→eco_archive",
         "recorded_at": NOW, "data": {}},
        {"event_type": "INGESTION_FAILED", "message": "fred_macro: shrank",
         "recorded_at": NOW, "data": {}},
    ]
    s.nearmiss = [{"source": "validation.short_interest_freshness", "confidence": 0.97}]
    d = await wd.build_weekly_digest(s, NOW)
    assert d.cutovers and d.self_heals and d.near_miss_gates
    assert "short_interest_freshness" in d.most_likely_wrong  # tightest near-miss
    assert "WEEKLY DATA-LAYER DIGEST" in d.render()


async def test_emit_is_idempotent_per_iso_week() -> None:
    s = _Store()
    assert await wd.emit_digest(s, NOW) is True
    assert await wd.emit_digest(s, NOW) is False  # same ISO week → skip
    digests = [r for r in s.applog if r["event_type"] == wd.DIGEST_EVENT]
    assert len(digests) == 1


async def test_ack_idempotent_and_nothing_to_ack() -> None:
    s = _Store()
    assert await wd.ack_digest(s, NOW) == ""        # nothing emitted yet
    await wd.emit_digest(s, NOW)
    wk = await wd.ack_digest(s, NOW)
    assert wk == wd._iso_week(NOW)
    assert await wd.ack_digest(s, NOW) == wk        # idempotent
    acks = [r for r in s.applog if r["event_type"] == wd.ACK_EVENT]
    assert len(acks) == 1


async def test_live_clearance_bootstrap_and_current() -> None:
    s = _Store()
    cleared, why = await wd.live_clearance(s, NOW)
    assert cleared and "bootstrap" in why
    await wd.emit_digest(s, NOW)
    await wd.ack_digest(s, NOW)
    cleared, why = await wd.live_clearance(s, NOW)
    assert cleared and "current" in why


async def test_auto_deescalate_after_two_unacked_then_ack_restores() -> None:
    s = _Store()
    # Week 1 + Week 2 digests, neither acked.
    await wd.emit_digest(s, NOW)
    await wd.emit_digest(s, NOW + timedelta(days=7))
    cleared, why = await wd.live_clearance(s, NOW + timedelta(days=8))
    assert cleared is False and "auto-de-escalated" in why
    # One miss only → warn but still cleared.
    s2 = _Store()
    await wd.emit_digest(s2, NOW)
    c1, w1 = await wd.live_clearance(s2, NOW)
    assert c1 is True and "one more miss" in w1
    # Ack restores after de-escalation.
    await wd.ack_digest(s, NOW + timedelta(days=8))   # acks latest week
    cleared2, _ = await wd.live_clearance(s, NOW + timedelta(days=8))
    assert cleared2 is True
