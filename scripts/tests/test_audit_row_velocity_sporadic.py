"""Unit tests for the row_velocity sporadic-cadence degradation predicate.

The sporadic branch of ``run_unknown_unknowns``'s velocity loop must:

* WARN on total silence (recent=0, prior>0) — preserved unchanged.
* WARN on a *severe sustained partial* collapse (#248) measured over a
  CLUSTER-ROBUST window: recent far below the rate-normalized
  expectation across a 180d span that, for clustered/seasonal cadence,
  is guaranteed to contain ≥1 full season — so it cannot be a
  legitimate inter-cluster lull — while a few stragglers still trickle
  in (the gap the silence check missed).
* stay OK for a legitimate clustered inter-cluster LULL (the #248 spec
  review's verified Critical false-positive: a healthy clustered table
  whose recent window lands between seasons with only stragglers).
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
        if "FILTER (" in sql and "COUNT(*)" in sql:
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


# ─── (a) clustered inter-cluster LULL → OK (the #248 Critical FP) ───────


async def test_reviewer_clustered_lull_scenario_is_OK() -> None:
    """The exact false-positive the #248 spec review proved real.

    A healthy CLUSTERED table (corporate_actions: splits/divs/earnings
    cluster). With the OLD 30d/90d predicate: prior-90d caught 2
    earnings seasons (prior=100), the recent-30d window fell in a
    legitimate inter-cluster lull with 3 straggler rows (recent=3, NOT
    zero so silence didn't fire) → 100/3*0.15=5 → recent=3<5 → WARN on
    perfectly healthy data.

    Under the cluster-robust 180d-recent / full-prior-year windows the
    SAME healthy table presents very differently: a 180d recent window
    necessarily spans ≥1 full season, so a healthy table reports a
    season's worth of rows there (recent≈250 against a prior year of
    ~1500) — comfortably OK. The scenario MUST be OK.
    """
    f = _velocity(await _run({"corporate_actions": (250, 1500)}),
                  "corporate_actions")
    assert f.severity == "OK"
    assert "within event-cadence variance" in f.summary
    assert f.recommended_action is None


async def test_quarterly_off_season_window_is_OK() -> None:
    # fundamentals_quarterly: ~4 filing seasons/yr. Any 180d recent
    # window spans ≥1 full filing season → a healthy table cannot be
    # near-zero there. recent=400 vs a prior-year baseline of 1600
    # (rate-normalized recent expectation ≈ 1600*180/365 ≈ 789;
    # recent 400 is ~half of that — well above the 10% severe floor of
    # ~79). MUST stay OK (legitimate seasonal variance).
    f = _velocity(await _run({"fundamentals_quarterly": (400, 1600)}),
                  "fundamentals_quarterly")
    assert f.severity == "OK"
    assert "within event-cadence variance" in f.summary


async def test_sporadic_just_above_severe_frac_is_OK() -> None:
    # recent just ABOVE prior * rate_factor * 0.10 boundary must NOT
    # WARN. prior=730 → expected≈730*180/365=360 → threshold≈36.
    # recent=40 → OK.
    f = _velocity(await _run({"corporate_actions": (40, 730)}),
                  "corporate_actions")
    assert f.severity == "OK"


# ─── (b) total silence → WARN (preserved, byte-identical summary) ───────


async def test_sporadic_total_silence_still_WARNs() -> None:
    f = _velocity(await _run({"corporate_actions": (0, 900)}),
                  "corporate_actions")
    assert f.severity == "WARN"
    # Preserved summary string (silence branch), byte-for-byte.
    assert f.summary == (
        "corporate_actions (sporadic): 0 rows last 180d vs "
        "900 prior 365d — SILENT (stalled ingest?)"
    )
    assert f.recommended_action == (
        "re-run the corporate_actions stage — zero rows in 180d "
        "but history shows activity"
    )
    assert f.evidence == {"recent_180d": 0, "prior_365d": 900,
                          "cadence": "sporadic"}


# ─── (c) genuine sustained ≥180d collapse → WARN (must bite) ────────────


async def test_sustained_180d_collapse_WARNs() -> None:
    # A genuine sustained collapse: prior FULL YEAR shows the regular
    # seasonal cycle (prior=1500) but the last 180d (which MUST contain
    # ≥1 full season for a healthy table) yielded only 20 straggler
    # rows — physically impossible for a healthy clustered table to
    # miss every season for 180d. expected ≈ 1500*180/365 ≈ 740;
    # severe threshold ≈ 74; recent=20 < 74 → WARN. Not zero, so the
    # silence check did NOT fire — this is the gap #248 closes.
    f = _velocity(await _run({"corporate_actions": (20, 1500)}),
                  "corporate_actions")
    assert f.severity == "WARN"
    assert "severe sustained degradation" in f.summary
    assert "20 in 180d vs ~740 rate-normalized expectation" in f.summary
    assert "1,500 prior-365d" in f.summary
    assert f.recommended_action is not None
    assert "silence check did not fire" in f.recommended_action
    assert "full-season window" in f.recommended_action
    assert f.evidence == {"recent_180d": 20, "prior_365d": 1500,
                          "cadence": "sporadic"}


async def test_sustained_collapse_bites_against_prechange_silence_only() -> None:
    # The pre-#248 sporadic branch ONLY WARNed on recent==0. With
    # recent=15 the OLD code emitted OK — this asserts the NEW
    # cluster-robust code genuinely flips it to WARN (the bite) on a
    # real sustained 180d collapse.
    f = _velocity(await _run({"fundamentals_quarterly": (15, 1600)}),
                  "fundamentals_quarterly")
    pre_change_severity = "WARN" if (15 == 0 and 1600 > 0) else "OK"
    assert pre_change_severity == "OK"  # what the old silence-only code did
    assert f.severity == "WARN"          # what the cluster-robust code does


# ─── (d) tiny history → never partial-WARN (only silence) ───────────────


async def test_sporadic_tiny_history_does_not_partial_WARN() -> None:
    # prior=30 < SPORADIC_PRIOR_FLOOR(40): a low recent count must NOT
    # trip the partial predicate (ratio on noise). recent=1 → OK.
    assert audit.SPORADIC_PRIOR_FLOOR == 40
    f = _velocity(await _run({"corporate_actions": (1, 30)}),
                  "corporate_actions")
    assert f.severity == "OK"
    assert "within event-cadence variance" in f.summary


async def test_sporadic_tiny_history_total_silence_still_WARNs() -> None:
    # Silence WARN does NOT depend on the prior floor: recent=0,
    # prior=30 still WARNs (preserved behaviour).
    f = _velocity(await _run({"fundamentals_quarterly": (0, 30)}),
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


# ─── (f) constants / windows are cluster-robust ─────────────────────────


async def test_constants_are_cluster_robust() -> None:
    # Guardrail-on-live-money: the recent window must span ≥1 full
    # quarterly/earnings season (≥120d; 180d gives buffer), the prior
    # baseline a full year, the severe frac unreachable by seasonal
    # variance, the prior floor ≥1 year of real history.
    assert audit.SPORADIC_RECENT_DAYS >= 120
    assert audit.SPORADIC_PRIOR_BAND_DAYS >= 365
    assert 0.05 <= audit.SPORADIC_SEVERE_FRAC <= 0.10
    assert audit.SPORADIC_PRIOR_FLOOR >= 40
    # Rate factor is recent/prior-band, used to normalize expectation.
    assert audit.SPORADIC_RATE_FACTOR == (
        audit.SPORADIC_RECENT_DAYS / audit.SPORADIC_PRIOR_BAND_DAYS
    )
