"""Fundamentals analysis toolkit (earnings quality, FCF trend, insider, comps, moat)."""

from .comps_analysis import CompsTable, build_comps_table, compare_to_peer_median
from .earnings_quality import EarningsQualityResult, check_earnings_quality
from .fcf_trend import FCFTrendResult, analyze_fcf_trend
from .insider_analysis import InsiderClusterResult, analyze_insider_transactions
from .moat_scorecard import MoatScore, MoatScorecardTemplate, get_moat_discount

__all__ = [
    "CompsTable",
    "EarningsQualityResult",
    "FCFTrendResult",
    "InsiderClusterResult",
    "MoatScore",
    "MoatScorecardTemplate",
    "analyze_fcf_trend",
    "analyze_insider_transactions",
    "build_comps_table",
    "check_earnings_quality",
    "compare_to_peer_median",
    "get_moat_discount",
]
