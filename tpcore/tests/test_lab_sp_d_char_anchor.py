"""SP-D §5.1 — char-before-refactor anchor.

Pins the EXACT current Sharpe ranking BEFORE any SP-D code change so the
post-refactor defaulted (metric=SHARPE, no arg) path is provably
byte-identical, not merely asserted. The golden is the current closed
form character-for-character:
    n_trades < 3            -> -1.0
    else                    -> sharpe + 0.05 * log10(max(n_trades, 1))
plus the current rank_candidates grouping + descending mean-score sort.
This test must stay GREEN through every SP-D task with NO edit.
"""
from __future__ import annotations

import math

import pytest

import ops.lab.run as sp

pytestmark = pytest.mark.xdist_group("ops_shadow")


def _golden_score(n_trades: int, sharpe: float) -> float:
    if n_trades < 3:
        return -1.0
    return float(sharpe) + 0.05 * math.log10(max(n_trades, 1))


@pytest.mark.parametrize(
    "n_trades,sharpe",
    [
        (10, 0.5), (10, 1.5), (10, 0.2),   # the oracle's exact triple
        (2, 9.9),                          # thin -> -1.0 floor
        (3, 0.0),                          # boundary n_trades==3
        (250, 2.3),                        # high trade-count bonus arm
        (5, -0.4),                         # negative Sharpe
    ],
)
def test_score_for_ranking_matches_current_closed_form(n_trades, sharpe):
    m = sp.SliceMetrics(
        n_trades=n_trades, sharpe=sharpe, profit_factor=1.5,
        max_drawdown=-0.1, win_rate=0.5,
    )
    assert sp._score_for_ranking(m) == _golden_score(n_trades, sharpe)


def test_rank_candidates_current_grouping_and_sort_golden():
    def tr(tid, params, sharpe):
        return sp.TrialResult(
            trial_id=tid, window_label="w", parameters=params,
            holdout=sp.SliceMetrics(
                n_trades=10, sharpe=sharpe, profit_factor=1.5,
                max_drawdown=-0.1, win_rate=0.5),
            full_credibility_score=70, error=None,
        )

    p1 = {"z_threshold": 3.0}
    p2 = {"z_threshold": 2.5}
    ranked = sp.rank_candidates(
        [tr(0, p1, 0.5), tr(1, p1, 1.5), tr(2, p2, 0.2)]
    )
    # p1 mean score = mean(_golden(10,0.5), _golden(10,1.5))
    p1_mean = (_golden_score(10, 0.5) + _golden_score(10, 1.5)) / 2.0
    p2_mean = _golden_score(10, 0.2)
    assert ranked[0][0] == p1
    assert ranked[0][1] == pytest.approx(p1_mean)
    assert ranked[0][2] == 2
    assert ranked[1][0] == p2
    assert ranked[1][1] == pytest.approx(p2_mean)
    assert ranked[1][2] == 1
