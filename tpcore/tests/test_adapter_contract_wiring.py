"""Each enforced feed: a blanked required field across the whole
payload raises AdapterContractDrift; a normal payload (incl. a
legitimately-null NON-required field) passes."""
from __future__ import annotations

import pytest

from tpcore.ingestion.adapter_contract import (
    AdapterContractDrift,
    assert_contract_populated,
)


class _R:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_finra_blanked_required_raises_but_null_optional_ok() -> None:
    drift = [_R(ticker="AAA", settlement_date="2026-05-01",
                short_position_qty=None, days_to_cover=None)]
    with pytest.raises(AdapterContractDrift, match="short_position_qty"):
        assert_contract_populated("finra_short_interest", drift)
    ok = [_R(ticker="AAA", settlement_date="2026-05-01",
             short_position_qty=10, days_to_cover=None)]
    assert_contract_populated("finra_short_interest", ok)


def test_fred_value_all_null_ok_date_all_null_raises() -> None:
    assert_contract_populated(
        "fred_macro",
        [{"date": "2026-05-01", "value": None},
         {"date": "2026-05-02", "value": None}],
    )
    with pytest.raises(AdapterContractDrift, match="date"):
        assert_contract_populated(
            "fred_macro",
            [{"date": None, "value": "1.2"}, {"date": "", "value": "1.3"}],
        )


def test_iborrowdesk_and_apewisdom_required_blank_raises() -> None:
    with pytest.raises(AdapterContractDrift, match="borrow_rate_pct"):
        assert_contract_populated(
            "iborrowdesk_borrow_rates",
            [_R(ticker="AAA", date="2026-05-01", borrow_rate_pct=None)],
        )
    with pytest.raises(AdapterContractDrift, match="mentions"):
        assert_contract_populated(
            "apewisdom_social_sentiment",
            [_R(ticker="AAA", name="x", rank=1, mentions=None, upvotes=2)],
        )
