"""DFCR parser tests — parse_dfcr is the single entry point; a block
that does not parse rejects with the exact reason, never best-effort-
interpreted (mirrors the ECR parser's discipline)."""
from __future__ import annotations

import pytest

from ops.data_feed_sdlc.dfcr import DFCRAction, parse_dfcr


def test_add_external_minimum_required_fields() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: fundamentals_quarterly\n"
        "kind: external\n"
        "provider: fmp\n"
        "adapter: tpcore.fmp.handle_fundamentals_quarterly\n"
        "need: financial fundamentals for value engines\n"
        "cadence: quarterly\n"
    )
    dfcr = parse_dfcr(text)
    assert dfcr.operation is DFCRAction.ADD
    assert dfcr.feed == "fundamentals_quarterly"
    assert dfcr.kind == "external"
    assert dfcr.provider == "fmp"
    assert dfcr.adapter == "tpcore.fmp.handle_fundamentals_quarterly"


def test_add_external_missing_provider_rejects() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: foo\n"
        "kind: external\n"
        "adapter: tpcore.foo\n"
        "need: x\n"
    )
    with pytest.raises(ValueError, match="provider"):
        parse_dfcr(text)


def test_add_derived_minimum_required_fields() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: my_derived\n"
        "kind: derived\n"
        "derived_from: [vix, hy_spread]\n"
        "need: derived signal\n"
    )
    dfcr = parse_dfcr(text)
    assert dfcr.operation is DFCRAction.ADD
    assert dfcr.kind == "derived"
    assert dfcr.derived_from == ["vix", "hy_spread"]


def test_remove_minimum_required_fields() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: REMOVE\n"
        "feed: doomed_feed\n"
        "disposition: delete\n"
        "reason: vendor shut down\n"
    )
    dfcr = parse_dfcr(text)
    assert dfcr.operation is DFCRAction.REMOVE
    assert dfcr.disposition == "delete"


def test_modify_cutover_provider() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: MODIFY\n"
        "feed: macro_indicators\n"
        "change: provider:eco_archive\n"
        "reason: FRED truncation incident\n"
    )
    dfcr = parse_dfcr(text)
    assert dfcr.operation is DFCRAction.MODIFY
    assert dfcr.change == "provider:eco_archive"


def test_unknown_key_rejected() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: x\n"
        "weirdfield: nope\n"
    )
    with pytest.raises(ValueError, match="unknown DFCR key"):
        parse_dfcr(text)


def test_duplicate_key_rejected() -> None:
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: ADD\n"
        "feed: x\n"
        "feed: y\n"
    )
    with pytest.raises(ValueError, match="duplicate key"):
        parse_dfcr(text)


def test_no_dfcr_block_rejected() -> None:
    with pytest.raises(ValueError, match="no DFCR block"):
        parse_dfcr("just some text\n")


def test_missing_operation_rejected() -> None:
    text = "DATA FEED CHANGE REQUEST\nfeed: x\n"
    with pytest.raises(ValueError, match="missing required key: operation"):
        parse_dfcr(text)


def test_cross_action_field_rejected() -> None:
    """ADD-only field on a REMOVE block must reject (multi-action smuggle)."""
    text = (
        "DATA FEED CHANGE REQUEST\n"
        "operation: REMOVE\n"
        "feed: x\n"
        "disposition: delete\n"
        "provider: fmp\n"
    )
    with pytest.raises(ValueError, match="not valid for operation REMOVE"):
        parse_dfcr(text)
