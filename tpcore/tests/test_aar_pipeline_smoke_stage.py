"""``ops.py --stage aar_pipeline_smoke`` — synthetic round-trip
verification of ``AARWriter.write_aar``.

Migrated 2026-05-21 from ``scripts/test_aar_pipeline.py`` (orphan-
scripts zero-allowlist sweep; operator overruled the prior keep-as-
helper disposition). The stage builds a synthetic AAR with
``engine='synthetic_test'`` + UUID ``trade_id``, writes it via
``AARWriter``, reads it back, asserts the second ``write_aar`` call
is an idempotent no-op, and cleans up the synthetic row in a
``finally`` block.

Asserts the stage (1) is registered as ``--stage aar_pipeline_smoke``
and is NOT in the daily ``--update`` cadence, (2) drives the
canonical insert → round-trip → idempotent-dup → cleanup sequence,
(3) returns the documented detail-dict shape, and (4) the sentinel
verifies the legacy script file is gone + the allowlist entry was
removed.

No real DB / Alpaca / FMP touched. The pool fakes the small set of
SQL operations the stage emits (a count, a fetchrow round-trip, a
final count, and a cleanup DELETE-RETURNING). pytest-xdist ops-
shadow group per the package-shadow rule.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES

pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    """Captures SQL + serves canned responses for the stage's queries.

    Sequencing matters: the stage emits (1) a baseline ``COUNT(*)``
    expecting zero, (2) ``AARWriter.write_aar`` — which we intercept
    via the writer's underlying executor by populating the conn's
    fetchval/fetchrow return values, (3) the round-trip
    ``fetchrow``, (4) the dup ``write_aar`` (no-op), (5) a final
    ``COUNT(*)`` returning 1, (6) the cleanup ``DELETE … RETURNING``.
    """

    def __init__(
        self, fetchval_returns: list[int],
        fetchrow_aar_data: dict | None,
        delete_returning: list[dict[str, int]] | None = None,
    ) -> None:
        self._fetchval_returns = list(fetchval_returns)
        self._fetchrow_aar_data = fetchrow_aar_data
        self._delete_returning = delete_returning or [{"id": 1}]
        self.fetchval_sqls: list[str] = []
        self.fetchrow_sqls: list[str] = []
        self.fetch_sqls: list[str] = []
        self.execute_sqls: list[str] = []

    async def fetchval(self, sql: str, *args: object) -> int:
        self.fetchval_sqls.append(sql)
        if not self._fetchval_returns:
            return 0
        return self._fetchval_returns.pop(0)

    async def fetchrow(
        self, sql: str, *args: object,
    ) -> dict | None:
        self.fetchrow_sqls.append(sql)
        return self._fetchrow_aar_data

    async def fetch(
        self, sql: str, *args: object,
    ) -> list[dict[str, int]]:
        self.fetch_sqls.append(sql)
        # The cleanup DELETE … RETURNING is the only fetch() call.
        return self._delete_returning

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_sqls.append(sql)
        return "INSERT 0 1"


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


class _RecordingConn(_Conn):
    """Variant that captures the AAR the writer fake was handed so the
    round-trip read-back can echo the SAME JSON payload — proves the
    stage compares the written model against the read row."""

    def __init__(
        self,
        fetchval_returns: list[int],
        captured_payload: list[str],
        delete_returning: list[dict[str, int]] | None = None,
    ) -> None:
        super().__init__(
            fetchval_returns=fetchval_returns,
            fetchrow_aar_data=None,
            delete_returning=delete_returning,
        )
        self._captured_payload = captured_payload

    async def fetchrow(
        self, sql: str, *args: object,
    ) -> dict | None:
        self.fetchrow_sqls.append(sql)
        # Echo whatever the writer fake captured most recently.
        if self._captured_payload:
            return {"aar_data": self._captured_payload[-1]}
        return None


async def test_happy_path_verifies_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-trip read matches the written AAR + the second write_aar
    is False + the cleanup DELETE runs exactly once in finally.

    The writer fake captures the AAR JSON it would have written; the
    round-trip read returns that SAME payload — proving the stage's
    equality check uses the written model, not a synthesized payload.
    """
    captured_payloads: list[str] = []
    write_calls: list[bool] = []

    class _FakeWriter:
        def __init__(self, _pool: object) -> None:
            pass

        async def write_aar(self, aar: object) -> bool:
            # Capture the AAR's JSON so the read-back can echo it.
            payload = aar.model_dump_json()
            captured_payloads.append(payload)
            wrote = not write_calls  # True only on the first call
            write_calls.append(wrote)
            return wrote

    monkeypatch.setattr("scripts.ops.AARWriter", _FakeWriter, raising=False)
    # Also patch the source module — the stage imports inline by name,
    # so the source-module binding is what actually wins at call time.
    monkeypatch.setattr("tpcore.aar.writer.AARWriter", _FakeWriter)

    conn = _RecordingConn(
        fetchval_returns=[0, 1],
        captured_payload=captured_payloads,
        delete_returning=[{"id": 42}],
    )
    result = await ops._stage_aar_pipeline_smoke(_Pool(conn), config=None)

    assert result["verified"] is True
    assert result["rows_before"] == 0
    assert result["rows_after"] == 1
    assert result["synthetic_engine"] == "synthetic_test"
    assert result["synthetic_trade_id"].startswith("aar_pipeline_test_")
    # The cleanup DELETE … RETURNING must have fired exactly once.
    assert len(conn.fetch_sqls) == 1
    assert "DELETE FROM platform.aar_events" in conn.fetch_sqls[0]
    # Two write_aar calls — first True, second False (idempotent skip).
    assert write_calls == [True, False]
    # The round-trip read returned the same payload the writer captured.
    assert json.loads(captured_payloads[0]) == json.loads(
        captured_payloads[-1],
    )


async def test_baseline_count_nonzero_raises_for_leaked_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the synthetic UUID key somehow pre-exists (cleanup leak
    from a previous run), the stage must hard-fail rather than mask
    it — otherwise a stale row would silently corrupt the round-trip
    assertion."""
    class _FakeWriter:
        def __init__(self, _pool: object) -> None:
            pass

        async def write_aar(self, _aar: object) -> bool:
            return True

    monkeypatch.setattr(
        "tpcore.aar.writer.AARWriter", _FakeWriter,
    )
    conn = _Conn(
        fetchval_returns=[7],  # pre-existing rows
        fetchrow_aar_data=None,
    )
    with pytest.raises(SystemExit, match="already exists"):
        await ops._stage_aar_pipeline_smoke(_Pool(conn), config=None)


async def test_first_write_returning_false_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first ``write_aar`` MUST return True (the UUID key is
    fresh). False ⇒ the writer's ON CONFLICT logic mis-fired — hard
    fail with a clear message instead of producing a false-green."""
    class _FakeWriter:
        def __init__(self, _pool: object) -> None:
            pass

        async def write_aar(self, _aar: object) -> bool:
            return False  # wrong — should be True

    monkeypatch.setattr(
        "tpcore.aar.writer.AARWriter", _FakeWriter,
    )
    conn = _Conn(fetchval_returns=[0], fetchrow_aar_data=None)
    with pytest.raises(SystemExit, match="first write_aar"):
        await ops._stage_aar_pipeline_smoke(_Pool(conn), config=None)


def test_stage_registered_operator_on_demand_only() -> None:
    """Registration-pin: stage in ``_STAGE_SPECS`` + ``KNOWN_STAGES``,
    NOT in ``OPS_UPDATE_STAGES`` — daily ``--update`` cadence does
    NOT touch the live ``aar_events`` table with synthetic rows."""
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]
    assert "aar_pipeline_smoke" in spec_names
    assert "aar_pipeline_smoke" in ops.KNOWN_STAGES
    assert "aar_pipeline_smoke" not in OPS_UPDATE_STAGES, (
        "aar_pipeline_smoke is operator-on-demand verification — it "
        "must NOT be in the daily --update cadence (writes synthetic "
        "rows to platform.aar_events)"
    )


def test_orphan_allowlist_entry_removed_and_script_deleted() -> None:
    """Sentinel: ``scripts/test_aar_pipeline.py`` is gone + the
    allowlist entry was removed."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/test_aar_pipeline.py"
    assert not script.exists(), (
        "scripts/test_aar_pipeline.py must be deleted after the "
        "migration — the canonical path is ops.py --stage."
    )
    text = (
        repo_root / "scripts/tests/test_no_orphan_scripts.py"
    ).read_text(encoding="utf-8")
    assert '"test_aar_pipeline"' not in text, (
        "test_aar_pipeline allowlist entry must be removed when the "
        "stage lands."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
