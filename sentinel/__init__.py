"""Sentinel — macro defense engine.

Monitors FRED macro indicators + a VIX proxy, computes a daily Bear Score,
and when a recession regime is confirmed, allocates up to 20% of platform
capital to a defensive ETF basket (SH, PSQ, TLT, GLD, SQQQ). Activates
on 3 consecutive days of Bear Score ≥ 60 with no SPY counter-trend
rally > 5%; de-activates when the score falls below 60 (50% reduction
immediately, remainder over one week). See ``docs/MASTER_PLAN.md`` §4.6.

Unlike Sigma/Reversion/Vector (per-trade engines with bracket orders),
Sentinel is a portfolio allocation engine like Momentum — it places batch
market orders for the basket directly via the broker adapter and uses no
per-name stops. Risk is managed by the daily activation/deactivation
discipline and the 20% capital cap.
"""
from __future__ import annotations
