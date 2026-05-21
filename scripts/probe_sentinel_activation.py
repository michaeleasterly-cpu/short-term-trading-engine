"""Offline Sentinel activation-score distribution probe (one-off).

Diagnoses whether the FAILED ``sentinel_bear_score`` Lab probe
(dossier ``docs/lab/2026-05-21-sentinel_bear_score-FAILED-seed0.md``)
failed because the graduated activation gate is **structurally dormant**
(composite < 0.45 floor across the OOS window) OR merely
**threshold-clipped** (composite fires but the binary
``ACTIVATION_SCORE_THRESHOLD=60`` path / band-to-execution wiring drops
the trade).

This is a read-only probe. It reuses the PURE composite + band-scale
helpers from ``sentinel.backtest`` and the wide-panel loader; it does
NOT touch ``_run_graduated_bear_score`` (which requires the full
``SentinelWindowContext`` setup-detection I/O).

PIT semantics mirror the live behavior (``sentinel/backtest.py``
1071-1077): per-date row is the most-recent observation at or before
the date in the panel — NOT a naive ``.loc[d]``.

Outputs
-------
- One-line floor stdout: sample count + OOS p50 / p95 + verdict + rationale.
- JSON sidecar ``data/sentinel_activation_probe/<probe-date>.json``
  with full distribution stats (verdict / per-bucket counts + percentiles
  + max contiguous DORMANT streak + indicator coverage).

No Lab spend, no n_trials increment, no dossier. Defect ref:
``SENTINEL-ACTIVATION-DORMANT-2026-05-21``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date as date_t
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from sentinel.backtest import (
    _GRAD_BAND_DEEP_LO,
    _GRAD_BAND_HEAVY_LO,
    _GRAD_BAND_LIGHT_LO,
    _GRAD_INDICATORS,
    _fetch_graduated_macro_panel,
    _grad_composite,
)
from tpcore.db import build_asyncpg_pool

log = structlog.get_logger(__name__)

_PROBE_DATE = date_t(2026, 5, 21)
_FULL_START = date_t(2018, 1, 1)
_FULL_END = _PROBE_DATE
_OOS_START = date_t(2024, 1, 1)
_OOS_END = _PROBE_DATE

# Authoritative weights — pinned from sentinel.backtest._GRAD_W_* (spec §2.3).
_WEIGHTS = {
    "sahm": 0.30,
    "sos": 0.15,
    "curve": 0.20,
    "cfnai": 0.15,
    "hy_oas": 0.20,
}

_OUT_DIR = Path("data/sentinel_activation_probe")


def _bucket(composite: float) -> str:
    """Classify ``composite`` into the four activation buckets (spec §2.4)."""
    if composite < _GRAD_BAND_LIGHT_LO:
        return "DORMANT"
    if composite < _GRAD_BAND_HEAVY_LO:
        return "LIGHT"
    if composite < _GRAD_BAND_DEEP_LO:
        return "HEAVY"
    return "DEEP"


def _composites_for_window(
    panel: pd.DataFrame,
    start: date_t,
    end: date_t,
) -> list[tuple[date_t, float]]:
    """Per-date PIT composite over the daily index in ``[start, end]``.

    Reproduces ``sentinel/backtest.py`` 1071-1077 verbatim: row =
    ``panel.loc[panel.index <= d].iloc[-1]``; missing ⇒ all-None dict
    (composite = 0)."""
    out: list[tuple[date_t, float]] = []
    daily_idx = pd.date_range(start, end, freq="D").date
    for d in daily_idx:
        try:
            row = panel.loc[panel.index <= d].iloc[-1]
            row_dict = {k: row.get(k) for k in _GRAD_INDICATORS}
        except (IndexError, KeyError):
            row_dict = {k: None for k in _GRAD_INDICATORS}
        out.append((d, _grad_composite(row_dict)))
    return out


def _window_stats(samples: list[tuple[date_t, float]]) -> dict[str, Any]:
    """Distribution stats + max contiguous DORMANT streak for one window."""
    if not samples:
        return {
            "total_samples": 0,
            "per_bucket": {},
            "composite_percentiles": {},
            "max_contiguous_dormant_streak_days": 0,
        }
    composites = [c for _, c in samples]
    buckets = [_bucket(c) for c in composites]
    total = len(samples)
    per_bucket: dict[str, dict[str, float]] = {}
    for b in ("DORMANT", "LIGHT", "HEAVY", "DEEP"):
        count = buckets.count(b)
        per_bucket[b] = {"count": count, "pct": count / total if total else 0.0}
    series = pd.Series(composites)
    percentiles = {
        "p50": float(series.quantile(0.50)),
        "p75": float(series.quantile(0.75)),
        "p90": float(series.quantile(0.90)),
        "p95": float(series.quantile(0.95)),
        "p99": float(series.quantile(0.99)),
    }
    # Max contiguous DORMANT streak (consecutive calendar days < LIGHT_LO).
    max_streak = 0
    cur = 0
    for b in buckets:
        if b == "DORMANT":
            cur += 1
            if cur > max_streak:
                max_streak = cur
        else:
            cur = 0
    return {
        "total_samples": total,
        "per_bucket": per_bucket,
        "composite_percentiles": percentiles,
        "max_contiguous_dormant_streak_days": max_streak,
    }


def _indicator_coverage(panel: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per-indicator first/last non-null date + non-null pct (over the
    full daily index of the loaded panel)."""
    cov: dict[str, dict[str, Any]] = {}
    if panel.empty:
        for ind in _GRAD_INDICATORS:
            cov[ind] = {"first_date": None, "last_date": None, "non_null_pct": 0.0}
        return cov
    total = len(panel.index)
    for ind in _GRAD_INDICATORS:
        col = panel[ind]
        non_null = col.notna()
        nn_count = int(non_null.sum())
        if nn_count == 0:
            cov[ind] = {"first_date": None, "last_date": None, "non_null_pct": 0.0}
            continue
        first_idx = panel.index[non_null.values.argmax()]
        # last non-null = reverse-search
        last_idx = panel.index[len(panel.index) - 1 - non_null.values[::-1].argmax()]
        cov[ind] = {
            "first_date": str(first_idx),
            "last_date": str(last_idx),
            "non_null_pct": nn_count / total,
        }
    return cov


def _verdict(
    oos_stats: dict[str, Any],
    oos_start: date_t,
    oos_end: date_t,
) -> tuple[str, str]:
    """Verdict per spec.

    PASS: ``oos.p95(composite) >= 0.45`` AND
          ``max_contiguous_dormant_streak < (oos_end - oos_start days)``.
    FAIL: ``oos.p95 < 0.45`` (structurally dormant).
    """
    p95 = float(oos_stats.get("composite_percentiles", {}).get("p95", 0.0))
    streak = int(oos_stats.get("max_contiguous_dormant_streak_days", 0))
    window_days = (oos_end - oos_start).days
    pass_p95 = p95 >= _GRAD_BAND_LIGHT_LO
    pass_streak = streak < window_days
    if pass_p95 and pass_streak:
        return "PASS", (
            f"OOS p95={p95:.3f} >= 0.45 AND max_dormant_streak={streak}d < "
            f"window={window_days}d — activation fires; defect lives "
            f"downstream (binary threshold=60, band-to-execution wiring, "
            f"or graduated cycle gate)."
        )
    return "FAIL", (
        f"OOS p95={p95:.3f} < 0.45 (structurally dormant); composite never "
        f"reaches the LIGHT floor on the OOS window — sub-score floors / "
        f"weights / 2024-onwards regime do not light the gate."
    )


async def _run() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set (and no DATABASE_URL_IPV4 fallback)", file=sys.stderr)
        return 1

    log.info("probe.start",
             full_window=(str(_FULL_START), str(_FULL_END)),
             oos_window=(str(_OOS_START), str(_OOS_END)))

    pool = await build_asyncpg_pool(db_url, max_size=2, read_only=True)
    try:
        panel = await _fetch_graduated_macro_panel(
            pool, start=_FULL_START, end=_FULL_END
        )
    finally:
        await pool.close()

    log.info("probe.panel_loaded",
             rows=len(panel.index),
             columns=list(panel.columns))

    full_samples = _composites_for_window(panel, _FULL_START, _FULL_END)
    oos_samples = _composites_for_window(panel, _OOS_START, _OOS_END)

    full_stats = _window_stats(full_samples)
    oos_stats = _window_stats(oos_samples)
    coverage = _indicator_coverage(panel)
    verdict, rationale = _verdict(oos_stats, _OOS_START, _OOS_END)

    payload = {
        "candidate": "sentinel_bear_score",
        "probe_date": str(_PROBE_DATE),
        "window": {"start": str(_FULL_START), "end": str(_FULL_END)},
        "oos_window": {"start": str(_OOS_START), "end": str(_OOS_END)},
        "bucket_thresholds": {
            "LIGHT_LO": _GRAD_BAND_LIGHT_LO,
            "HEAVY_LO": _GRAD_BAND_HEAVY_LO,
            "DEEP_LO": _GRAD_BAND_DEEP_LO,
        },
        "indicators": list(_GRAD_INDICATORS),
        "weights": _WEIGHTS,
        "full_window": full_stats,
        "oos_window_stats": oos_stats,
        "indicator_coverage": coverage,
        "verdict": verdict,
        "verdict_rationale": rationale,
    }

    # Floor print — operator sees the answer at the terminal BEFORE the
    # JSON write (visible-progress rule). Plain print is the deliberate
    # operator-visibility channel.
    oos_p50 = oos_stats["composite_percentiles"].get("p50", 0.0)
    oos_p95 = oos_stats["composite_percentiles"].get("p95", 0.0)
    print("=== Sentinel Activation-Score Distribution Probe ===")
    print(f"full window: {_FULL_START} → {_FULL_END}  "
          f"samples={full_stats['total_samples']}")
    print(f"oos  window: {_OOS_START} → {_OOS_END}  "
          f"samples={oos_stats['total_samples']}")
    print(f"OOS composite p50={oos_p50:.4f}  p95={oos_p95:.4f}")
    print(f"OOS DORMANT pct={oos_stats['per_bucket'].get('DORMANT', {}).get('pct', 0.0):.3%}  "
          f"max_contiguous_dormant_streak_days={oos_stats['max_contiguous_dormant_streak_days']}")
    print(f"VERDICT: {verdict} — {rationale}")

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / f"{_PROBE_DATE}.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    log.info("probe.wrote_sidecar", path=str(out_path), verdict=verdict)
    print(f"wrote: {out_path}")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
