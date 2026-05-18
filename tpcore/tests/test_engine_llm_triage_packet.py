"""EngineTriagePacket: read-only context assembly, deterministic hash,
size cap + truncation marker (mirrors the #187 packet behavior).
Fake pool, NO LLM, NO writes.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tpcore.engine_llm_triage.packet import EngineTriagePacket, build_packet
from tpcore.engine_llm_triage.select import EngineNovelEscalation


class _Conn:
    def __init__(self, hold_row, forensics_rows):
        self._hold = hold_row
        self._for = forensics_rows

    async def fetchrow(self, sql, *a):
        # current_hold() issues a fetchrow
        return self._hold

    async def fetch(self, sql, *a):
        return list(self._for)


class _CM:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *e):
        return None


class _Pool:
    def __init__(self, hold_row=None, forensics_rows=()):
        self._hold = hold_row
        self._for = list(forensics_rows)

    def acquire(self):
        return _CM(_Conn(self._hold, self._for))


def _esc(hold_id="h1", engine="reversion",
         fc="scheduler_crash") -> EngineNovelEscalation:
    return EngineNovelEscalation(
        hold_id=hold_id, engine=engine, failure_class=fc,
        reason="non-zero exit", recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        shape="escalate-only", policy_default="structural",
        policy_rationale="survived self-heal ⇒ structural fix",
    )


async def test_packet_contains_escalation_hold_profile_policy() -> None:
    pool = _Pool(
        hold_row=None,
        forensics_rows=[{"id": 7, "trigger_kind": "loss_cluster",
                         "payload": {"engine": "reversion",
                                     "fingerprint": "fp1"}}],
    )
    pkt = await build_packet(pool, _esc())
    assert isinstance(pkt, EngineTriagePacket)
    assert "h1" in pkt.text          # the escalation hold_id
    assert "reversion" in pkt.text   # engine + profile
    assert "structural" in pkt.text  # advisory policy default
    assert "loss_cluster" in pkt.text  # open forensics trigger
    assert "DAILY" in pkt.text or "daily" in pkt.text  # engine profile cadence


async def test_current_hold_included_when_present() -> None:
    hold = {"hold_id": "h1", "failure_class": "scheduler_crash",
            "reason": "boom", "held_at": datetime(2026, 5, 1, tzinfo=UTC),
            "cleared": None}
    pkt = await build_packet(_Pool(hold_row=hold), _esc())
    assert "current_hold" in pkt.text


async def test_identical_inputs_identical_hash() -> None:
    pool = _Pool()
    e = _esc()
    p1 = await build_packet(pool, e)
    p2 = await build_packet(pool, e)
    assert p1.packet_hash == p2.packet_hash
    assert len(p1.packet_hash) == 64  # sha256 hex


async def test_oversized_blob_truncated_and_hash_stable() -> None:
    big = [{"id": i, "trigger_kind": "x",
            "payload": {"engine": "reversion", "note": "a" * 3000}}
           for i in range(20)]
    pool = _Pool(forensics_rows=big)
    p1 = await build_packet(pool, _esc())
    assert p1.text.endswith("...[truncated]...")
    p2 = await build_packet(pool, _esc())
    assert p1.packet_hash == p2.packet_hash


async def test_unprofiled_engine_does_not_crash() -> None:
    pkt = await build_packet(_Pool(), _esc(engine="nonexistent_engine"))
    assert isinstance(pkt, EngineTriagePacket)
    assert "nonexistent_engine" in pkt.text
