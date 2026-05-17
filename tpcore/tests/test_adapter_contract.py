"""Unit tests for the adapter contract-population sentinel (#186(6))."""
from __future__ import annotations

import pathlib
import re

import pytest

from tpcore.ingestion.adapter_contract import (
    ADAPTER_CONTRACTS,
    AdapterContract,  # noqa: F401
    AdapterContractDrift,
    assert_contract_populated,
    contract_drift,
)


def _csv_first_feeds() -> set[str]:
    feeds: set[str] = set()
    pat = re.compile(r"write_archive\(\s*\n?\s*\"([a-z0-9_]+)\"")
    for rel in ("tpcore/ingestion/handlers.py", "scripts/ops.py"):
        feeds.update(pat.findall(pathlib.Path(rel).read_text()))
    return feeds


def test_registry_in_lockstep_with_csv_first_feeds() -> None:
    missing, extra = contract_drift()
    assert missing == set(), f"CSV-first feeds with no AdapterContract: {missing}"
    assert extra == set(), f"AdapterContracts for non-CSV-first feeds: {extra}"
    assert set(ADAPTER_CONTRACTS) == _csv_first_feeds()


def test_guard_pending_set_is_pinned() -> None:
    enforced = {f for f, c in ADAPTER_CONTRACTS.items() if not c.guard_pending}
    assert enforced == {
        "fred_macro",
        "iborrowdesk_borrow_rates",
        "finra_short_interest",
        "apewisdom_social_sentiment",
    }


def test_every_contract_self_consistent() -> None:
    for feed, c in ADAPTER_CONTRACTS.items():
        assert c.feed == feed
        assert c.required_fields, f"{feed}: required_fields empty"
        assert c.accessor in ("attr", "key")
        assert c.evidence, f"{feed}: evidence empty (no-vendor-blame)"


class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def test_empty_payload_is_noop() -> None:
    assert_contract_populated("apewisdom_social_sentiment", [])


async def test_all_null_required_field_raises() -> None:
    recs = [_Rec(ticker=None, name="A", rank=1, mentions=2, upvotes=3),
            _Rec(ticker=None, name="B", rank=4, mentions=5, upvotes=6)]
    with pytest.raises(AdapterContractDrift, match="ticker"):
        assert_contract_populated("apewisdom_social_sentiment", recs)


async def test_single_stray_null_tolerated() -> None:
    recs = [_Rec(ticker=None, name="A", rank=1, mentions=2, upvotes=3),
            _Rec(ticker="MSFT", name="B", rank=4, mentions=5, upvotes=6)]
    assert_contract_populated("apewisdom_social_sentiment", recs)


async def test_zero_is_not_empty() -> None:
    recs = [_Rec(ticker="MSFT", name="A", rank=0, mentions=0, upvotes=0)]
    assert_contract_populated("apewisdom_social_sentiment", recs)


async def test_key_accessor_for_dict_records() -> None:
    ok = [{"date": "2026-05-01"}, {"date": "2026-05-02"}]
    assert_contract_populated("fred_macro", ok)
    bad = [{"date": None}, {"date": ""}]
    with pytest.raises(AdapterContractDrift, match="date"):
        assert_contract_populated("fred_macro", bad)


async def test_unknown_feed_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        assert_contract_populated("not_a_feed", [_Rec(x=1)])
