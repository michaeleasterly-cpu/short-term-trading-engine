"""Rung-3 instance teeth: the weekly digest surfaces OPEN
undispositioned data-lane escalations; DATA_ESCALATION_DISPOSITIONED
clears them."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime

_spec = importlib.util.spec_from_file_location(
    "wd", pathlib.Path(__file__).resolve().parents[1] / "ops" / "weekly_digest.py")
wd = importlib.util.module_from_spec(_spec)
sys.modules["wd"] = wd
_spec.loader.exec_module(wd)


class _Conn:
    def __init__(self, rows_by_marker):
        self._m = rows_by_marker
        self.emitted = []

    async def fetch(self, sql, *a):
        for marker, rows in self._m.items():
            if marker in sql:
                return [dict(r) for r in rows]
        return []

    async def execute(self, sql, *a):
        self.emitted.append(a)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, rows_by_marker=None):
        self.conn = _Conn(rows_by_marker or {})

    def acquire(self): return _CM(self.conn)


async def test_open_escalation_listed_and_rendered() -> None:
    pool = _Pool({
        "OPEN_ESCALATIONS": [
            {"ref": "h1", "etype": "DATA_SOURCE_ESCALATED",
             "recorded_at": datetime(2026, 5, 1, tzinfo=UTC),
             "message": "source prices_daily stuck"},
        ],
    })
    d = await wd.build_weekly_digest(pool, datetime(2026, 5, 17, tzinfo=UTC))
    assert any("prices_daily" in x for x in d.undispositioned)
    assert "UNDISPOSITIONED" in d.render().upper()


async def test_no_open_escalations_renders_none() -> None:
    d = await wd.build_weekly_digest(_Pool(), datetime(2026, 5, 17, tzinfo=UTC))
    assert d.undispositioned == []
    assert "UNDISPOSITIONED" in d.render().upper()  # section still shown


async def test_disposition_cli_emits_event() -> None:
    pool = _Pool()
    rc = await wd.disposition_escalation(pool, "h1", "converted", "added HealSpec X")
    assert rc == 0
    assert len(pool.conn.emitted) == 1
    a = pool.conn.emitted[0]
    # _INSERT_SQL positional args: (engine, run_id, event_type, sev,
    # message, data). event_type idx 2, data json idx 5.
    assert a[2] == "DATA_ESCALATION_DISPOSITIONED"
    assert "h1" in a[5] and "converted" in a[5]


async def test_invalid_disposition_rejected() -> None:
    pool = _Pool()
    rc = await wd.disposition_escalation(pool, "h1", "bogus", "")
    assert rc != 0 and pool.conn.emitted == []
