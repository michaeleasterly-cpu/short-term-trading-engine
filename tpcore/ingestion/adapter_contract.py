"""Adapter contract-population sentinel — #186(6), Escalation &
Hardening Ladder rung 1.

A vendor renames/removes a field; the adapter absorbs it with a silent
``.get()``/default; the table loads structurally-fine but
semantically-empty rows (shrinkage- and header-blind). This detects
the SYMPTOM: a declared ``required_field`` empty in EVERY record of a
non-empty pull = unambiguous contract drift -> producer hard-stop
before the load (no safe auto-heal — escalate-only).

Declarative SoT (mirrors HealSpec/RemediationSpec): one frozen
``AdapterContract`` per CSV-first feed. ``required_fields`` are the
adapter-output fields a valid vendor record ALWAYS populates; fields
legitimately nullable in some valid window (finra ``days_to_cover``,
fred ``value``) are deliberately excluded — see each ``evidence``.
"""
from __future__ import annotations

import pathlib
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class AdapterContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feed: str
    required_fields: frozenset[str]
    accessor: Literal["attr", "key"]
    guard_pending: bool = False
    evidence: str = ""


class AdapterContractDrift(RuntimeError):
    """A required adapter-output field is empty across an entire
    non-empty pull — the vendor contract changed. Escalate-only."""


ADAPTER_CONTRACTS: dict[str, AdapterContract] = {
    "fred_macro": AdapterContract(
        feed="fred_macro", accessor="key",
        required_fields=frozenset({"date"}),
        evidence="get_all_indicators -> {'date': date, 'value': "
                 "Decimal|None} (tpcore/fred/adapter.py:168). value is "
                 "legitimately null (missing FRED obs) so excluded; "
                 "date always parsed or the obs is dropped."),
    "iborrowdesk_borrow_rates": AdapterContract(
        feed="iborrowdesk_borrow_rates", accessor="attr",
        required_fields=frozenset({"ticker", "date", "borrow_rate_pct"}),
        evidence="BorrowRateRecord(ticker:str, date:date, "
                 "borrow_rate_pct) all always populated "
                 "(tpcore/iborrowdesk/adapter.py:39-49)."),
    "finra_short_interest": AdapterContract(
        feed="finra_short_interest", accessor="attr",
        required_fields=frozenset(
            {"ticker", "settlement_date", "short_position_qty"}),
        evidence="ShortInterestRecord (tpcore/finra/adapter.py:47-60): "
                 "days_to_cover Decimal|None -> excluded; "
                 "short_interest_pct is handler-derived not an adapter "
                 "field -> excluded; the 3 listed always populated."),
    "apewisdom_social_sentiment": AdapterContract(
        feed="apewisdom_social_sentiment", accessor="attr",
        required_fields=frozenset(
            {"ticker", "name", "rank", "mentions", "upvotes"}),
        evidence="SocialSentimentRecord (tpcore/apewisdom/adapter.py:"
                 "36-47): rank_24h_ago/mentions_24h_ago int|None -> "
                 "excluded; the 5 listed are non-optional."),
    "fmp_fundamentals": AdapterContract(
        feed="fmp_fundamentals", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: contract declared for coverage; "
                 "enforced wiring is a later increment."),
    "alpaca_corporate_actions": AdapterContract(
        feed="alpaca_corporate_actions", accessor="key",
        guard_pending=True, required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; wiring later."),
    "alpaca_daily_bars": AdapterContract(
        feed="alpaca_daily_bars", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; wiring later."),
    "fred_macro_hist": AdapterContract(
        feed="fred_macro_hist", accessor="key", guard_pending=True,
        required_fields=frozenset({"date"}),
        evidence="guard_pending: declared for coverage; wiring later."),
    "fmp_earnings_events": AdapterContract(
        feed="fmp_earnings_events", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; wiring later."),
    "greeks_max_pain": AdapterContract(
        feed="greeks_max_pain", accessor="key", guard_pending=True,
        required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; wiring later."),
    "finnhub_insider_sentiment": AdapterContract(
        feed="finnhub_insider_sentiment", accessor="key",
        guard_pending=True, required_fields=frozenset({"ticker"}),
        evidence="guard_pending: declared for coverage; wiring later."),
    "aaii_sentiment": AdapterContract(
        feed="aaii_sentiment", accessor="key", guard_pending=True,
        required_fields=frozenset({"date"}),
        evidence="guard_pending: declared for coverage; wiring later."),
}


def _is_empty(v: Any) -> bool:
    # None or "" is empty. 0 / 0.0 / Decimal(0) are VALID values.
    return v is None or v == ""


def _read(rec: Any, field: str, accessor: str) -> Any:
    # accessor is declared per-feed in the registry; attr-feeds always emit named records, key-feeds always dicts (never mixed).
    if accessor == "attr":
        return getattr(rec, field, None)
    try:
        return rec[field]
    except (KeyError, TypeError):
        return None


def contract_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) vs the CSV-first feed set, re-derived from the
    write_archive call sites. Both empty == in lockstep."""
    pat = re.compile(r"write_archive\(\s*\n?\s*\"([a-z0-9_]+)\"")
    feeds: set[str] = set()
    for rel in ("tpcore/ingestion/handlers.py", "scripts/ops.py"):
        feeds.update(pat.findall(pathlib.Path(rel).read_text()))
    have = set(ADAPTER_CONTRACTS)
    return feeds - have, have - feeds


def assert_contract_populated(feed: str, records: Sequence[Any]) -> None:
    """Raise AdapterContractDrift if any declared required_field is
    empty (None/"") in EVERY record of a non-empty payload. No-op on an
    empty payload (a freshness/coverage concern other checks own)."""
    contract = ADAPTER_CONTRACTS[feed]  # KeyError = unknown feed (loud)
    if not records:
        return
    for field in sorted(contract.required_fields):
        if all(
            _is_empty(_read(r, field, contract.accessor)) for r in records
        ):
            raise AdapterContractDrift(
                f"adapter_contract_drift: feed={feed!r} required field "
                f"{field!r} is empty in all {len(records)} records — "
                f"vendor contract changed (escalate-only; no auto-heal)"
            )


__all__ = [
    "ADAPTER_CONTRACTS",
    "AdapterContract",
    "AdapterContractDrift",
    "assert_contract_populated",
    "contract_drift",
]
