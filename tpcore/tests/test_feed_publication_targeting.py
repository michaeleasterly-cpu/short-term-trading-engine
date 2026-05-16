"""#165 facets 3+4: demand-driven targeting + publication-availability.

All pure/mocked — no network, no DB. Pins the vendor-anchored
(UTC, vendor calendar, not our clock) freshness behaviour.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from tpcore.feeds import publication, targeting
from tpcore.feeds.targeting import prioritise

# ── publication: vendor-anchored expected publish (UTC) ────────────────

def test_expected_latest_publish_anchors_to_vendor_weekday() -> None:
    # AAII publishes Thursday (ISO 4). Tue 2026-05-19 → last Thu = 05-14.
    tue = datetime(2026, 5, 19, 3, 0, tzinfo=UTC)
    assert publication.expected_latest_publish("aaii_sentiment", tue) == date(2026, 5, 14)
    # On Thursday itself, before dissemination lag elapses → prior Thu.
    thu = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    got = publication.expected_latest_publish("aaii_sentiment", thu)
    assert got in (date(2026, 5, 21), date(2026, 5, 14))


def test_expected_latest_publish_none_for_unscheduled_feed() -> None:
    # FINRA has no fixed weekday (settlement-calendar anchored elsewhere).
    assert publication.expected_latest_publish("finra_short_interest") is None
    assert publication.expected_latest_publish("unknown_feed") is None


def test_expected_latest_publish_uses_utc_not_local() -> None:
    # Naive datetime is coerced to UTC, not treated as local.
    naive = datetime(2026, 5, 19, 3, 0)  # noqa: DTZ001 - intentional
    assert publication.expected_latest_publish("aaii_sentiment", naive) == date(2026, 5, 14)


async def test_source_has_newer_none_without_probe() -> None:
    # No probe registered for FINRA → undeterminable (caller stays strict).
    assert await publication.source_has_newer("finra_short_interest", date(2026, 1, 1)) is None
    # None held data → undeterminable.
    assert await publication.source_has_newer("aaii_sentiment", None) is None


async def test_source_has_newer_true_false_via_probe(monkeypatch) -> None:
    async def fake_probe() -> date:
        return date(2026, 5, 14)
    monkeypatch.setitem(publication.PUBLICATION_PROBES, "aaii_sentiment", fake_probe)
    # vendor latest 05-14 > we hold 05-07 → True (our gap, heal)
    assert await publication.source_has_newer("aaii_sentiment", date(2026, 5, 7)) is True
    # we already hold 05-14 → False (vendor-late, quiet)
    assert await publication.source_has_newer("aaii_sentiment", date(2026, 5, 14)) is False


# ── targeting: demand-driven, no engine code ───────────────────────────

class _Conn:
    def __init__(self, rows): self._rows = rows
    async def fetch(self, *_a): return [{"ticker": t} for t in self._rows]


class _Pool:
    def __init__(self, rows): self._rows = rows
    def acquire(self):
        rows = self._rows

        class _CM:
            async def __aenter__(self): return _Conn(rows)
            async def __aexit__(self, *e): return None
        return _CM()


async def test_demand_targets_none_for_whole_universe_feed() -> None:
    # prices_daily is WHOLE_UNIVERSE → never narrowed.
    assert await targeting.demand_targets(_Pool(["AAPL"]), "prices_daily") is None


async def test_demand_targets_list_for_constrained_feed() -> None:
    out = await targeting.demand_targets(
        _Pool(["msft", "AAPL", "msft"]), "iborrowdesk_borrow_rates")
    assert out == ["AAPL", "MSFT"]  # deduped, upper, sorted


async def test_demand_targets_empty_is_valid() -> None:
    assert await targeting.demand_targets(_Pool([]), "iborrowdesk_borrow_rates") == []


def test_prioritise_orders_demand_first_then_rest() -> None:
    uni = ["A", "B", "C", "D"]
    assert prioritise(uni, ["C", "Z", "A"]) == ["C", "A", "B", "D"]


def test_prioritise_noop_when_no_demand() -> None:
    uni = ["A", "B", "C"]
    assert prioritise(uni, None) == uni
    assert prioritise(uni, []) == uni


# ── AAII check is vendor-anchored + PURE (no network in the suite) ─────

def _pool_with(latest: date):
    class _C:
        async def fetchval(self, *a): return latest

    class _P:
        def acquire(self):
            class _CM:
                async def __aenter__(self): return _C()
                async def __aexit__(self, *e): return None
            return _CM()
    return _P()


async def test_aaii_check_green_when_current_to_vendor_schedule() -> None:
    """Holding today's date is ≥ the last scheduled Thursday → not
    behind → green. Deterministic, NO network call in the check."""
    from datetime import UTC, datetime

    from tpcore.quality.validation.checks import aaii_sentiment_freshness as M
    res = await M.check_aaii_sentiment_freshness(
        _pool_with(datetime.now(UTC).date()))
    assert res.passed is True


async def test_aaii_check_red_when_behind_vendor_schedule() -> None:
    """Newest row long predates the last scheduled vendor publish →
    our ingestion gap → red (genuinely heal-by-repull)."""
    from tpcore.quality.validation.checks import aaii_sentiment_freshness as M
    res = await M.check_aaii_sentiment_freshness(_pool_with(date(2026, 1, 1)))
    assert res.passed is False and res.failures[0].reason == "stale"
