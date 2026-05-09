"""Peer-comps table builder."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CompsRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    pe: Decimal | None = None
    ev_ebitda: Decimal | None = None
    ev_sales: Decimal | None = None
    fcf_yield: Decimal | None = None
    roic: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None


class CompsTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    rows: list[CompsRow] = Field(default_factory=list)


def build_comps_table(ticker: str, peers: list[CompsRow]) -> CompsTable:
    """Assemble the table of subject + peer rows.

    The caller is expected to fetch the underlying multiples through
    ``DataProviderInterface`` — this function does no I/O.
    TODO: add subject row at index 0 and validate uniqueness.
    """
    _ = (ticker, peers)
    raise NotImplementedError


def compare_to_peer_median(subject: CompsRow, peers: list[CompsRow]) -> dict:
    """Return per-metric (subject value, peer median, percentile) for ``subject``.

    TODO: numpy median + percentile over each metric column.
    """
    _ = (subject, peers)
    raise NotImplementedError
