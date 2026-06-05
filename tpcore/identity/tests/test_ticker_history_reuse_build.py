"""Pure-function tests for ``tpcore.identity.ticker_history_reuse_build``.

Hermetic: no DB, no network. Exercises the derivation of the SCD-2
``ticker_history`` timeline from ``ticker_classifications`` lifetimes —
the delisted-then-reused ticker (G3) gets MULTIPLE contiguous rows, one
per classification, half-open ``[valid_from, valid_to)`` so the
``ticker_history_no_overlap`` EXCLUDE constraint is satisfied.
"""
from __future__ import annotations

from datetime import date

import pytest

from tpcore.identity.ticker_history_reuse_build import (
    ClassificationLifetime,
    TickerHistoryRow,
    derive_ticker_history,
)


def _cl(
    classification_id: str,
    ticker: str,
    start: date,
    end: date | None = None,
) -> ClassificationLifetime:
    return ClassificationLifetime(
        classification_id=classification_id,
        ticker=ticker,
        lifetime_start=start,
        lifetime_end=end,
    )


def test_single_classification_one_open_row() -> None:
    rows = derive_ticker_history(
        [_cl("ID_A", "AAPL", date(1994, 12, 12), None)]
    )
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, TickerHistoryRow)
    assert r.classification_id == "ID_A"
    assert r.ticker == "AAPL"
    assert r.valid_from == date(1994, 12, 12)
    assert r.valid_to is None


def test_delisted_classification_closed_row() -> None:
    rows = derive_ticker_history(
        [_cl("ID_D", "DEAD", date(2005, 1, 1), date(2010, 6, 1))]
    )
    assert len(rows) == 1
    assert rows[0].valid_from == date(2005, 1, 1)
    assert rows[0].valid_to == date(2010, 6, 1)


def test_reused_ticker_multiple_contiguous_rows() -> None:
    """G3: a delisted-then-reused ticker → MULTIPLE rows, one per
    classification, ordered by lifetime_start, half-open non-overlapping."""
    rows = derive_ticker_history(
        [
            # Reuse: 'XYZ' belonged to entity A (2000-2008), then entity B
            # (2010-now). Two separate classifications, same ticker.
            _cl("ID_B", "XYZ", date(2010, 1, 1), None),
            _cl("ID_A", "XYZ", date(2000, 1, 1), date(2008, 1, 1)),
        ]
    )
    xyz = [r for r in rows if r.ticker == "XYZ"]
    assert len(xyz) == 2
    # ordered by valid_from.
    assert xyz[0].classification_id == "ID_A"
    assert xyz[0].valid_from == date(2000, 1, 1)
    assert xyz[0].valid_to == date(2008, 1, 1)
    assert xyz[1].classification_id == "ID_B"
    assert xyz[1].valid_from == date(2010, 1, 1)
    assert xyz[1].valid_to is None
    # half-open windows do NOT overlap (gap is fine).
    assert xyz[0].valid_to <= xyz[1].valid_from


def test_contiguous_handoff_no_overlap() -> None:
    """Predecessor valid_to == successor valid_from is allowed under the
    half-open '[)' EXCLUDE — emitted as-is (contiguity, not overlap)."""
    rows = derive_ticker_history(
        [
            _cl("ID_1", "RE", date(2000, 1, 1), date(2005, 1, 1)),
            _cl("ID_2", "RE", date(2005, 1, 1), None),
        ]
    )
    re_rows = sorted(
        (r for r in rows if r.ticker == "RE"), key=lambda r: r.valid_from
    )
    assert re_rows[0].valid_to == re_rows[1].valid_from == date(2005, 1, 1)


def test_overlap_hard_stops() -> None:
    """Two classifications for the SAME ticker whose half-open windows
    OVERLAP are a data defect the DB EXCLUDE would reject — hard-stop,
    never silently mangle (a surfaced defect)."""
    with pytest.raises(ValueError, match="overlap"):
        derive_ticker_history(
            [
                _cl("ID_1", "OV", date(2000, 1, 1), date(2006, 1, 1)),
                # starts before predecessor's valid_to → overlap.
                _cl("ID_2", "OV", date(2005, 1, 1), None),
            ]
        )


def test_bad_lifetime_order_dropped() -> None:
    """A classification with lifetime_end <= lifetime_start (garbled) is a
    row the ticker_history (and tc) CHECK would reject → DROPPED + WARN, not
    emitted, and never poisons the batch."""
    rows = derive_ticker_history(
        [
            _cl("ID_OK", "GOOD", date(2001, 1, 1), date(2003, 1, 1)),
            _cl("ID_BAD", "BADT", date(2005, 1, 1), date(2004, 1, 1)),
        ]
    )
    tickers = {r.ticker for r in rows}
    assert "GOOD" in tickers
    assert "BADT" not in tickers


def test_null_lifetime_start_hard_stops() -> None:
    """A NULL lifetime_start cannot anchor a ticker_history row — the
    no-sentinel invariant (A6) means universe_build always sets it; a
    missing one is a defect to surface, not silently default."""
    with pytest.raises(ValueError, match="lifetime_start"):
        ClassificationLifetime(
            classification_id="ID_X",
            ticker="X",
            lifetime_start=None,  # type: ignore[arg-type]
            lifetime_end=None,
        )


def test_idempotent_deterministic_order() -> None:
    """Same input (any order) → identical output rows (the stage upserts
    ON CONFLICT (classification_id, valid_from); a re-run is a no-op)."""
    cls = [
        _cl("ID_B", "XYZ", date(2010, 1, 1), None),
        _cl("ID_A", "XYZ", date(2000, 1, 1), date(2008, 1, 1)),
        _cl("ID_C", "OTH", date(2015, 1, 1), None),
    ]
    a = derive_ticker_history(cls)
    b = derive_ticker_history(list(reversed(cls)))
    key = lambda r: (r.ticker, r.valid_from)  # noqa: E731
    assert sorted(
        ((r.classification_id, r.ticker, r.valid_from, r.valid_to) for r in a),
        key=lambda t: (t[1], t[2]),
    ) == sorted(
        ((r.classification_id, r.ticker, r.valid_from, r.valid_to) for r in b),
        key=lambda t: (t[1], t[2]),
    )
    assert sorted(a, key=key) == sorted(b, key=key)
