"""Golden-fixture characterization test for ``tpcore.backtest.price_loader``.

Lean P5.3 (#2) — ``_load_prices`` is byte-identical SQL/parse across
reversion + vector; the ONLY divergence is the min-bar filter
(reversion ``MA_50_PERIOD + 5`` == 55, vector ``SMA_200 + 5`` == 205).
That divergence is **intentional** and is preserved as the ``min_bars``
parameter — NOT erased.

The expected surviving-ticker sets below are constructed **independently**
from the fixture (the explicit set arithmetic ``len(rows) >= min_bars``),
NOT by calling an engine function as the oracle (de-tautologized, mirroring
P5.1/P5.2). The two ``min_bars`` values MUST yield DIFFERENT surviving
sets — that is the proof the divergence is preserved, not flattened.

No real DB / network: an in-memory deterministic fake pool only.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tpcore.backtest.price_loader import load_prices

# Engine thresholds (read from the actual constants — see Step 1 of the plan).
# reversion: reversion.plugs.setup_detection.MA_50_PERIOD == 50  -> min_bars 55
# vector:    vector.backtest.SMA_200 == 200                      -> min_bars 205
REVERSION_MIN_BARS = 50 + 5  # 55
VECTOR_MIN_BARS = 200 + 5  # 205

_START = dt.date(2020, 1, 1)
_END = dt.date(2025, 12, 31)

# (ticker, bar_count) — counts deliberately straddle BOTH thresholds so the
# surviving set differs between the two min_bars values.
_FIXTURE_SPEC: dict[str, int] = {
    "T_BIG": 210,  # >= 205 and >= 55  -> survives both
    "T_MID": 100,  # >= 55, < 205      -> reversion only
    "T_SMALL": 10,  # < 55             -> neither
    "T_REV_EDGE": 55,  # exactly 55    -> reversion (55 < 55 is False), not vector
    "T_VEC_EDGE": 205,  # exactly 205  -> both
    "T_REV_JUST_UNDER": 54,  # 54 < 55 -> neither
}


def _make_rows() -> list[dict]:
    """Deterministic price rows. Values vary per row so DataFrame content,
    not just the surviving set, is characterized."""
    rows: list[dict] = []
    for ticker, n in _FIXTURE_SPEC.items():
        base = dt.date(2021, 1, 4)
        for i in range(n):
            d = base + dt.timedelta(days=i)
            px = 100.0 + i + (hash(ticker) % 7)
            rows.append(
                {
                    "ticker": ticker,
                    "date": d,
                    "open": px,
                    "high": px + 1.5,
                    "low": px - 1.5,
                    "close": px + 0.25,
                    "volume": 1000 + i,
                }
            )
    return rows


class _FakeConn:
    """PR-13: load_prices now goes through IdentityDispatcher + PricesRepo.

    The dispatcher calls ``fetchval`` against ticker_history (returns the
    cid); PricesRepo then calls ``fetch`` against prices_daily using
    ``classification_id = ANY``. For test simplicity we use ticker == cid
    (identity mapping). The fetch route keys on classification_id and
    populates the row dict with the classification_id column so PricesRepo
    can group correctly.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchval(self, sql: str, *args):  # noqa: ANN001
        # Dispatcher ticker → classification_id lookup. ticker == cid.
        ticker = args[0]
        return ticker if any(r["ticker"] == ticker for r in self._rows) else None

    async def fetch(self, sql: str, *args):  # noqa: ANN001
        assert "platform.prices_daily" in sql
        # PricesRepo SQL binds (classification_ids_list, start, end).
        cids = set(args[0])
        start = args[1]
        end = args[2]
        return [
            {**r, "classification_id": r["ticker"]}
            for r in self._rows
            if r["ticker"] in cids and start <= r["date"] <= end
        ]


class _AcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:  # noqa: ANN002
        return None


class _FakePool:
    def __init__(self, rows: list[dict]) -> None:
        self._conn = _FakeConn(rows)

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)


def _expected_surviving(min_bars: int) -> set[str]:
    """Independent oracle: a ticker survives iff its row count >= min_bars
    (engine code does ``if len(rows) < min_bars: continue``)."""
    return {t for t, n in _FIXTURE_SPEC.items() if n >= min_bars}


@pytest.fixture
def pool() -> _FakePool:
    return _FakePool(_make_rows())


def _assert_df_content(out: dict[str, pd.DataFrame]) -> None:
    """Per-ticker DataFrame must match what the original engine parse yields:
    set_index('date').sort_index(), float OHLC, int volume."""
    for ticker, df in out.items():
        n = _FIXTURE_SPEC[ticker]
        assert len(df) == n
        assert df.index.name == "date"
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert list(df.index) == sorted(df.index)
        assert df["volume"].iloc[0] == 1000
        assert df["volume"].iloc[-1] == 1000 + (n - 1)
        assert df["open"].iloc[0] == pytest.approx(100.0 + (hash(ticker) % 7))
        assert str(df["volume"].dtype).startswith("int")
        assert str(df["open"].dtype).startswith("float")


async def test_reversion_threshold_surviving_set(pool: _FakePool) -> None:
    out = await load_prices(
        pool, list(_FIXTURE_SPEC), _START, _END, min_bars=REVERSION_MIN_BARS
    )
    assert set(out) == _expected_surviving(REVERSION_MIN_BARS)
    assert set(out) == {"T_BIG", "T_MID", "T_REV_EDGE", "T_VEC_EDGE"}
    _assert_df_content(out)


async def test_vector_threshold_surviving_set(pool: _FakePool) -> None:
    out = await load_prices(
        pool, list(_FIXTURE_SPEC), _START, _END, min_bars=VECTOR_MIN_BARS
    )
    assert set(out) == _expected_surviving(VECTOR_MIN_BARS)
    assert set(out) == {"T_BIG", "T_VEC_EDGE"}
    _assert_df_content(out)


def test_min_bars_divergence_is_preserved_not_flattened() -> None:
    """The two engine thresholds MUST produce DIFFERENT surviving sets —
    this is the proof the per-engine divergence is parameterized, not erased."""
    rev = _expected_surviving(REVERSION_MIN_BARS)
    vec = _expected_surviving(VECTOR_MIN_BARS)
    assert rev != vec
    assert vec < rev  # vector's stricter threshold is a strict subset here
    assert "T_MID" in rev and "T_MID" not in vec


async def test_reversion_delegate_matches_expected(pool: _FakePool) -> None:
    """The reversion engine delegate must return exactly the independent
    expected surviving set + content (delegate == expected, not fn-as-oracle)."""
    from reversion.backtest import _load_prices as rev_load

    out = await rev_load(pool, list(_FIXTURE_SPEC), _START, _END)
    assert set(out) == _expected_surviving(REVERSION_MIN_BARS)
    _assert_df_content(out)


async def test_vector_delegate_matches_expected(pool: _FakePool) -> None:
    from vector.backtest import _load_prices as vec_load

    out = await vec_load(pool, list(_FIXTURE_SPEC), _START, _END)
    assert set(out) == _expected_surviving(VECTOR_MIN_BARS)
    _assert_df_content(out)
