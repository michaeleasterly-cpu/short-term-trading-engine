"""Unit tests for the row_velocity sporadic-cadence degradation predicate.

The sporadic branch of ``run_unknown_unknowns``'s velocity loop must:

* WARN on total silence (recent=0, prior>0) — preserved unchanged.
* WARN on a *severe sustained partial* collapse (#248): recent far
  below the rate-normalized 30d expectation (prior/3) while a few
  stragglers still trickle in — the gap the silence check missed.
* stay OK for legitimate 80%-wk/wk event-cadence variance (no
  false-positive on a live-money guardrail).
* never partial-WARN on a tiny-history table (prior < floor).
* leave the daily branch behaviour byte-for-byte unchanged.

The velocity loop drives only ``conn.fetchrow`` per ``platform.<table>``;
a SQL-routing fake conn returns controlled (recent, prior) per table and
makes every downstream query in ``run_unknown_unknowns`` benign (empty
fetch / 0 fetchval / NULL max) so the function runs end-to-end without a
real DB.
"""
from __future__ import annotations

import importlib

audit = importlib.import_module("scripts.audit_data_pipeline")

# (table, timestamp_col, cadence) from the production velocity_targets.
SPORADIC_TABLES = ("corporate_actions", "fundamentals_quarterly")
DAILY_TABLES = ("prices_daily", "sec_insider_transactions",
                "aar_events", "application_log")


class _Conn:
    """SQL-routing fake connection.

    ``counts`` maps a ``platform.<table>`` name to ``(recent, prior)``;
    the velocity SELECTs embed ``platform.{table}``. Every other query
    in ``run_unknown_unknowns`` returns a benign empty/zero result so
    the function completes without a real DB.
    """

    def __init__(self, counts: dict[str, tuple[int, int]]) -> None:
        self._counts = counts

    async def fetchrow(self, sql: str, *a, **k):
        # Velocity SELECTs are the only fetchrow with COUNT FILTER; the
        # correlated-staleness query is a MAX(...) AS mx (→ NULL here).
        if "FILTER (WHERE" in sql and "COUNT(*)" in sql:
            for table, (recent, prior) in self._counts.items():
                if f"platform.{table}\n" in sql:
                    return {"recent": recent, "prior": prior}
            return {"recent": 0, "prior": 0}
        return {"mx": None}

    async def fetch(self, sql: str, *a, **k):
        return []

    async def fetchval(self, sql: str, *a, **k):
        return 0


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, counts: dict[str, tuple[int, int]]) -> None:
        self._conn = _Conn(counts)

    def acquire(self): return _CM(self._conn)


def _velocity(findings, table):
    rows = [f for f in findings
            if f.check_name == "row_velocity" and f.source == table]
    assert len(rows) == 1, f"expected exactly one row_velocity for {table}"
    return rows[0]


async def _run(counts):
    return await audit.run_unknown_unknowns(_Pool(counts))


# ─── (a) legitimate sporadic variance → OK (no false positive) ──────────


async def test_sporadic_legitimate_80pct_swing_is_OK() -> None:
    # prior 90d = 900 → rate-normalized 30d expectation = 300. An 80%
    # week/week swing averaged over 30d still leaves ~150 rows — well
    # above the severe floor (300 * 0.15 = 45). MUST stay OK.
    f = _velocity(await _run({"corporate_actions": (150, 900)}),
                  "corporate_actions")
    assert f.severity == "OK"
    assert "within event-cadence variance" in f.summary
    assert f.recommended_action is None


async def test_sporadic_recent_above_expectation_is_OK() -> None:
    # An event cluster (recent ≈ rate-normalized expectation) → OK.
    f = _velocity(await _run({"fundamentals_quarterly": (310, 900)}),
                  "fundamentals_quarterly")
    assert f.severity == "OK"


async def test_sporadic_just_above_severe_frac_is_OK() -> None:
    # recent just ABOVE prior/3 * 0.15 boundary must NOT WARN.
    # prior=300 → expected=100 → threshold=15. recent=16 → OK.
    f = _velocity(await _run({"corporate_actions": (16, 300)}),
                  "corporate_actions")
    assert f.severity == "OK"


# ─── (b) total silence → WARN (preserved, same summary) ─────────────────


async def test_sporadic_total_silence_still_WARNs() -> None:
    f = _velocity(await _run({"corporate_actions": (0, 900)}),
                  "corporate_actions")
    assert f.severity == "WARN"
    # Preserved summary string (silence branch), byte-for-byte.
    assert f.summary == (
        "corporate_actions (sporadic): 0 rows last 30d vs "
        "900 prior 90d — SILENT (stalled ingest?)"
    )
    assert f.recommended_action == (
        "re-run the corporate_actions stage — zero rows in 30d "
        "but history shows activity"
    )
    assert f.evidence == {"recent_30d": 0, "prior_90d": 900,
                          "cadence": "sporadic"}


# ─── (c) severe sustained partial → WARN (new; must bite) ───────────────


async def test_sporadic_severe_sustained_partial_WARNs() -> None:
    # prior 90d = 900 → expectation 300 → severe threshold 45.
    # recent = 5 (not zero, so NOT silent) → ~98% sustained collapse.
    f = _velocity(await _run({"corporate_actions": (5, 900)}),
                  "corporate_actions")
    assert f.severity == "WARN"
    assert "severe sustained degradation" in f.summary
    assert "5 in 30d vs ~300 rate-normalized expectation" in f.summary
    assert "900 prior-90d" in f.summary
    assert f.recommended_action is not None
    assert "silence check did not fire" in f.recommended_action
    assert f.evidence == {"recent_30d": 5, "prior_90d": 900,
                          "cadence": "sporadic"}


async def test_severe_partial_bites_against_prechange_silence_only() -> None:
    # The pre-change sporadic branch ONLY WARNed on recent==0. With
    # recent=5 the OLD code emitted OK — this asserts the NEW code
    # genuinely flips it to WARN (the bite).
    f = _velocity(await _run({"fundamentals_quarterly": (5, 900)}),
                  "fundamentals_quarterly")
    pre_change_severity = "WARN" if (5 == 0 and 900 > 0) else "OK"
    assert pre_change_severity == "OK"  # what the old code did
    assert f.severity == "WARN"          # what the new code does


# ─── (d) tiny history → never partial-WARN (only silence) ───────────────


async def test_sporadic_tiny_history_does_not_partial_WARN() -> None:
    # prior=20 < SPORADIC_PRIOR_FLOOR(30): a low recent count must NOT
    # trip the partial predicate (ratio on noise). recent=1 → OK.
    assert audit.SPORADIC_PRIOR_FLOOR == 30
    f = _velocity(await _run({"corporate_actions": (1, 20)}),
                  "corporate_actions")
    assert f.severity == "OK"
    assert "within event-cadence variance" in f.summary


async def test_sporadic_tiny_history_total_silence_still_WARNs() -> None:
    # Silence WARN does NOT depend on the prior floor: recent=0,
    # prior=20 still WARNs (preserved behaviour).
    f = _velocity(await _run({"fundamentals_quarterly": (0, 20)}),
                  "fundamentals_quarterly")
    assert f.severity == "WARN"
    assert "SILENT (stalled ingest?)" in f.summary


async def test_sporadic_both_zero_emits_no_finding() -> None:
    # prior==0 and recent==0 → `continue` (no row_velocity finding).
    findings = await _run({"corporate_actions": (0, 0)})
    rows = [f for f in findings if f.check_name == "row_velocity"
            and f.source == "corporate_actions"]
    assert rows == []


# ─── (e) daily branch unchanged (regression guard) ──────────────────────


async def test_daily_branch_unchanged_warn() -> None:
    # Daily: abs(change_pct) > 0.5 and prior > 100 → WARN. recent=10,
    # prior=1000 → change_pct=-0.99 → WARN, exactly as before.
    f = _velocity(await _run({"prices_daily": (10, 1000)}), "prices_daily")
    assert f.severity == "WARN"
    assert f.summary == (
        "prices_daily: 10 rows last 7d vs 1,000 prior 7d (-99.0%)"
    )
    assert f.evidence["cadence"] == "daily"
    assert f.evidence["recent_7d"] == 10
    assert f.evidence["prior_7d"] == 1000


async def test_daily_branch_unchanged_ok() -> None:
    # Daily within tolerance → OK (unchanged). recent=950, prior=1000.
    f = _velocity(await _run({"sec_insider_transactions": (950, 1000)}),
                  "sec_insider_transactions")
    assert f.severity == "OK"
    assert f.evidence["cadence"] == "daily"


async def test_constants_are_conservative() -> None:
    # Guardrail-on-live-money: the severe frac must be well clear of
    # legitimate 80%-wk/wk variance and the prior floor non-trivial.
    assert 0.10 <= audit.SPORADIC_SEVERE_FRAC <= 0.20
    assert audit.SPORADIC_PRIOR_FLOOR >= 30
