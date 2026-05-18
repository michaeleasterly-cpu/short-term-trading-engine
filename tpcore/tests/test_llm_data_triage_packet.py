"""TriagePacket: read-only context assembly, deterministic hash, size cap.
Fake pool, no LLM."""
from __future__ import annotations

from datetime import UTC, datetime

from tpcore.llm_data_triage.packet import TriagePacket, build_packet
from tpcore.llm_data_triage.select import NovelEscalation


class _Conn:
    def __init__(self, dql_rows): self._dql = dql_rows
    async def fetch(self, sql, *a): return list(self._dql)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, dql_rows=()):
        self._rows = list(dql_rows)
    def acquire(self): return _CM(_Conn(self._rows))


def _esc(ref: str = "h1", cls: str = "event:DATA_SOURCE_ESCALATED") -> NovelEscalation:
    return NovelEscalation(
        ref=ref, etype="DATA_SOURCE_ESCALATED", cls=cls,
        recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        message="test escalation",
    )


async def test_packet_contains_ref_and_policy_reason() -> None:
    pool = _Pool()
    pkt = await build_packet(pool, _esc())
    assert isinstance(pkt, TriagePacket)
    assert "h1" in pkt.text
    # cls is event:DATA_SOURCE_ESCALATED — ladder returns reason or "unknown"
    assert "reason" in pkt.text


async def test_identical_inputs_identical_hash() -> None:
    pool = _Pool()
    e = _esc()
    pkt1 = await build_packet(pool, e)
    pkt2 = await build_packet(pool, e)
    assert pkt1.packet_hash == pkt2.packet_hash


async def test_oversized_blob_truncated_and_hash_stable() -> None:
    # inject a large dql blob to trigger truncation
    big_row = {"source": "x", "timestamp": "2026-05-01", "confidence": 1.0,
               "stale": False, "notes": "a" * 3000}
    pool = _Pool(dql_rows=[big_row] * 10)
    pkt = await build_packet(pool, _esc())
    assert pkt.text.endswith("...[truncated]...")
    # hash is stable — same inputs produce same truncated text + same hash
    pkt2 = await build_packet(pool, _esc())
    assert pkt.packet_hash == pkt2.packet_hash
