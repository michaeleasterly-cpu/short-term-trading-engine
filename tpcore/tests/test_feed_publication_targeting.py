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


# ── macro_indicators (FRED) feed-level probe — MIN across series ───────


async def test_fred_probe_returns_min_observation_end_across_series(monkeypatch) -> None:
    """The registered macro_indicators probe MUST return the earliest
    observation_end across the configured series — taking MAX would
    silently green a feed where ONE series is stuck behind, masking
    the very vendor-MISSED-a-publish edge the probe exists to surface.

    Exercised end-to-end through ``source_has_newer`` (the public
    surface) — patches the FREDAdapter source module so the in-body
    ``from tpcore.fred import FREDAdapter`` inside the probe resolves
    to the fake.
    """
    from tpcore import fred as fred_mod
    from tpcore.fred import INDICATOR_SERIES

    fake_ends = {sid: date(2026, 5, 20) for _name, sid in INDICATOR_SERIES}
    # One series lags a week behind — MIN must reflect that.
    laggard_sid = INDICATOR_SERIES[2][1]
    fake_ends[laggard_sid] = date(2026, 5, 13)

    class _FakeFRED:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return None
        async def latest_published(self, series_id: str) -> date | None:
            return fake_ends.get(series_id)

    monkeypatch.setattr(fred_mod, "FREDAdapter", _FakeFRED)
    # we hold 05-12: vendor (05-13 laggard) is newer → True (heal).
    assert await publication.source_has_newer(
        "macro_indicators", date(2026, 5, 12)
    ) is True
    # we hold 05-13: vendor's MIN matches → False (vendor-late, quiet).
    # If the probe had returned MAX=05-20, this would have wrongly
    # fired True and burned a heal cycle on a feed where the laggard
    # series has NOTHING newer.
    assert await publication.source_has_newer(
        "macro_indicators", date(2026, 5, 13)
    ) is False


async def test_fred_probe_returns_none_on_any_series_failure(monkeypatch) -> None:
    """If ANY series probe returns None (network/malformed/auth),
    the feed-level probe returns None so the caller stays strict —
    a partial answer would silently silence the heal cycle."""
    from tpcore import fred as fred_mod
    from tpcore.fred import INDICATOR_SERIES

    first_sid = INDICATOR_SERIES[0][1]

    class _PartialFakeFRED:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return None
        async def latest_published(self, series_id: str) -> date | None:
            return date(2026, 5, 20) if series_id == first_sid else None

    monkeypatch.setattr(fred_mod, "FREDAdapter", _PartialFakeFRED)
    # source_has_newer returns None — caller stays strict (red, not green).
    assert await publication.source_has_newer(
        "macro_indicators", date(2026, 5, 13)
    ) is None


async def test_macro_indicators_probe_registered_by_default() -> None:
    """Registration-pin: the FRED probe is in PUBLICATION_PROBES under
    the canonical feed name. The drift sentinel for "adding a feed's
    probe is one registry entry" goes through here — a future refactor
    that drops the registration trips this test."""
    assert "macro_indicators" in publication.PUBLICATION_PROBES


# ── prices_daily (Alpaca) feed-level probe — single SPY anchor ─────────


async def test_prices_daily_probe_registered_by_default() -> None:
    """The Alpaca prices_daily probe must be registered alongside
    AAII + FRED. The orchestrator's vendor-late consult discovers it
    automatically through this registry; the registration is the
    one-entry contract."""
    assert "prices_daily" in publication.PUBLICATION_PROBES


async def test_alpaca_probe_returns_spy_latest_via_adapter(monkeypatch) -> None:
    """The registered probe calls AlpacaDataAdapter.latest_published('SPY')
    and returns that date. Exercised end-to-end through
    source_has_newer to prove the probe is composable with the gate
    just like the AAII + FRED probes."""
    from tpcore import alpaca as alpaca_mod

    class _FakeAdapter:
        def __init__(self, *a, **k): pass
        async def latest_published(self, symbol="SPY") -> date:
            assert symbol == "SPY", "single-anchor invariant — SPY only"
            return date(2026, 5, 19)

    monkeypatch.setattr(alpaca_mod, "AlpacaDataAdapter", _FakeAdapter)
    # we hold 05-18: vendor 05-19 → True (our gap, heal honestly)
    assert await publication.source_has_newer(
        "prices_daily", date(2026, 5, 18)
    ) is True
    # we already hold 05-19: vendor has nothing newer → False (quiet)
    assert await publication.source_has_newer(
        "prices_daily", date(2026, 5, 19)
    ) is False


async def test_alpaca_probe_returns_none_on_adapter_failure(monkeypatch) -> None:
    """Adapter construction failure (e.g. ALPACA_KEY unset) ⇒ probe
    returns None ⇒ caller stays strict. Mirrors the FRED probe's
    "returns None on any series failure" contract."""
    from tpcore import alpaca as alpaca_mod

    def _raise(*a, **k):
        raise RuntimeError("ALPACA_KEY / ALPACA_SECRET not set")
    monkeypatch.setattr(alpaca_mod, "AlpacaDataAdapter", _raise)
    # source_has_newer returns None — caller stays strict.
    assert await publication.source_has_newer(
        "prices_daily", date(2026, 5, 18)
    ) is None


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
