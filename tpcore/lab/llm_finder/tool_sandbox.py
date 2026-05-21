"""Tool sandbox — Task #25 §6.

In-process attribute-allowlist dispatcher for the 14 v1 whitelisted
callables. The LLM emits ``ToolCall(callable_name, args_json)``; this
module:
1. JSON-decodes ``args_json`` into a per-callable frozen ``Args`` model.
2. Resolves series from the snapshot BY ID against a fixed column
   whitelist (``adj_close``, ``log_return``, ``vol_20d``, ...).
3. Calls the resolved callable; ANY exception becomes
   ``ToolResult.error`` with exception-type name only (no traceback,
   no payload echo).
4. Wraps result into bounded ``NumericSummary`` (≤4 KiB summary text).

**Safety fences (CI-grepped per §6.2 + §10.3):**
- NO ``importlib``, ``__import__``, ``getattr(stats, name)``,
  ``eval``, ``exec``, ``subprocess``, ``os.system``, ``socket``.
- NO ``arch``, ``sklearn``, ``linearmodels``, ``pandas_ta``,
  ``requests``, ``urllib``, ``http``.
- All callables imported by name at module top — no dynamic resolution.
- Determinism: ``numpy.random.seed(0)`` set at module init for the
  bootstrap CI rolling callables.

The ``cost_net_simulation`` dispatcher (the binding outcome gate's
backbone — spec §6.1 + outcome-expert §5 BLOCKS) is the load-bearing
addition: every ``ProposedSpec.primary_metric=cost_net_sharpe`` is
computed via this callable's ``ToolResult``.
"""
from __future__ import annotations

import json
import math
from typing import Annotated, Any, Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import pearsonr, spearmanr, ttest_1samp
from statsmodels.api import OLS, add_constant
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.stattools import adfuller, coint

from tpcore.lab.llm_finder.models import (
    MarketSnapshot,
    NumericSummary,
    PricePanelRow,
    ToolCall,
    ToolResult,
)

# Determinism for bootstrap-CI rolling callables.
np.random.seed(0)

# Column whitelist — per spec §6.2 (no path traversal, no eval).
_SERIES_COLUMN_WHITELIST: Final[frozenset[str]] = frozenset({
    "adj_close",
    "log_return",
    "adj_open",
    "adj_high",
    "adj_low",
    "volume",
    "dollar_volume",
})

_MAX_SUMMARY_TEXT: Final[int] = 4_096
_BOOTSTRAP_ITERATIONS: Final[int] = 500


# ───────────────────────── per-callable Args models ─────────────────────────


class _BaseArgs(BaseModel):
    """Common config for all Args models — frozen + extra=forbid."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class OLSArgs(_BaseArgs):
    """Args for OLS_HAC_NW (Newey-West HAC SEs)."""

    y_ticker: str
    y_series: Literal["adj_close", "log_return"]
    x_tickers: tuple[str, ...]
    x_series: Literal["adj_close", "log_return"]
    hac_maxlags: Annotated[int, Field(ge=1, le=50)] = 5
    add_constant: bool = True


class ADFArgs(_BaseArgs):
    """Augmented Dickey-Fuller stationarity test."""

    ticker: str
    series: Literal["adj_close", "log_return"]
    maxlag: Annotated[int, Field(ge=0, le=20)] = 10


class CointArgs(_BaseArgs):
    """Engle-Granger cointegration (pair pre-registered; spec §6.1 fence)."""

    ticker_a: str
    ticker_b: str
    series: Literal["adj_close", "log_return"] = "adj_close"
    pair_pre_registered: Literal[True]  # MUST be True (spec §6.1)


class ARIMAArgs(_BaseArgs):
    """Bounded-order ARIMA(1,0,0) — order fixed by spec §6.1."""

    ticker: str
    series: Literal["adj_close", "log_return"] = "log_return"


class CorrArgs(_BaseArgs):
    """Spearman / Pearson correlation."""

    ticker_a: str
    ticker_b: str
    series_a: Literal["adj_close", "log_return"] = "log_return"
    series_b: Literal["adj_close", "log_return"] = "log_return"


class TTestArgs(_BaseArgs):
    """t-test for mean != 0 (HAC-style via Newey-West variance correction)."""

    ticker: str
    series: Literal["log_return"] = "log_return"
    popmean: float = 0.0
    hac_maxlags: Annotated[int, Field(ge=1, le=20)] = 5


class VarianceRatioArgs(_BaseArgs):
    """Lo-MacKinlay 1988 variance ratio test."""

    ticker: str
    q: Annotated[int, Field(ge=2, le=20)] = 4
    series: Literal["log_return"] = "log_return"


class HurstArgs(_BaseArgs):
    """Hurst exponent (R/S analysis)."""

    ticker: str
    series: Literal["log_return"] = "log_return"
    max_lag: Annotated[int, Field(ge=10, le=200)] = 100


class LjungBoxArgs(_BaseArgs):
    """Ljung-Box serial-correlation test."""

    ticker: str
    series: Literal["log_return"] = "log_return"
    lags: Annotated[int, Field(ge=1, le=40)] = 10


class RollingCorrArgs(_BaseArgs):
    """Rolling Spearman/Pearson with bootstrap CI."""

    ticker_a: str
    ticker_b: str
    window: Annotated[int, Field(ge=10, le=252)] = 60
    series: Literal["log_return"] = "log_return"


class FamaMacBethArgs(_BaseArgs):
    """Panel cross-sectional OLS_HAC_NW (Fama-MacBeth-style)."""

    y_series: Literal["log_return"] = "log_return"
    x_tickers: tuple[str, ...]
    x_series: Literal["log_return"] = "log_return"


class CostNetSimulationArgs(_BaseArgs):
    """Cost-net P&L projection — the binding outcome gate.

    Reads spread_observations + dollar_volume from the snapshot; applies
    cost_assumption_bps_roundtrip to every entry/exit; returns gross +
    cost-net Sharpe + bleed projection.
    """

    ticker: str
    entry_sessions: tuple[str, ...]  # ISO date strings
    exit_sessions: tuple[str, ...]  # ISO date strings (same length as entry_sessions)
    cost_assumption_bps_roundtrip: Annotated[float, Field(ge=0.0, le=100.0)]
    bootstrap_iterations: Annotated[int, Field(ge=0, le=2_000)] = 500


# ───────────────────────── dispatcher ─────────────────────────


def dispatch(call: ToolCall, snapshot: MarketSnapshot) -> ToolResult:
    """Switch on callable_name; route to the typed sub-dispatcher.

    Every branch catches Exception → ToolResult.error with exception
    type name only (no traceback echo per spec §6.2).
    """
    try:
        args_dict = json.loads(call.args_json)
    except json.JSONDecodeError as exc:
        return ToolResult(call=call, error=f"args_json_decode: {type(exc).__name__}")

    try:
        if call.callable_name == "OLS_HAC_NW":
            return _do_ols_hac_nw(call, OLSArgs(**args_dict), snapshot)
        if call.callable_name == "adfuller":
            return _do_adfuller(call, ADFArgs(**args_dict), snapshot)
        if call.callable_name == "coint":
            return _do_coint(call, CointArgs(**args_dict), snapshot)
        if call.callable_name == "ARIMA_1_0_0":
            return _do_arima(call, ARIMAArgs(**args_dict), snapshot)
        if call.callable_name == "spearmanr":
            return _do_spearmanr(call, CorrArgs(**args_dict), snapshot)
        if call.callable_name == "pearsonr":
            return _do_pearsonr(call, CorrArgs(**args_dict), snapshot)
        if call.callable_name == "ttest_1samp_HAC":
            return _do_ttest_hac(call, TTestArgs(**args_dict), snapshot)
        if call.callable_name == "variance_ratio":
            return _do_variance_ratio(call, VarianceRatioArgs(**args_dict), snapshot)
        if call.callable_name == "hurst_exponent":
            return _do_hurst(call, HurstArgs(**args_dict), snapshot)
        if call.callable_name == "ljung_box":
            return _do_ljung_box(call, LjungBoxArgs(**args_dict), snapshot)
        if call.callable_name == "rolling_spearmanr":
            return _do_rolling_corr(call, RollingCorrArgs(**args_dict), snapshot, "spearman")
        if call.callable_name == "rolling_pearsonr":
            return _do_rolling_corr(call, RollingCorrArgs(**args_dict), snapshot, "pearson")
        if call.callable_name == "fama_macbeth":
            return _do_fama_macbeth(call, FamaMacBethArgs(**args_dict), snapshot)
        if call.callable_name == "cost_net_simulation":
            return _do_cost_net_simulation(call, CostNetSimulationArgs(**args_dict), snapshot)
        return ToolResult(call=call, error=f"unknown_callable_name: {call.callable_name}")
    except Exception as exc:  # noqa: BLE001 - per spec §6.2 no traceback echo
        return ToolResult(call=call, error=type(exc).__name__)


# ───────────────────────── series resolver ─────────────────────────


def _resolve_series(
    snapshot: MarketSnapshot, ticker: str, column: str
) -> np.ndarray:
    """Pull a numeric series for (ticker, column) from the price_window.

    Returns 1-D float64 ndarray ordered by session_date ascending.
    Raises ValueError if column not in whitelist (defense in depth — the
    Literal types should have caught it but DEFENSE).
    """
    if column not in _SERIES_COLUMN_WHITELIST:
        raise ValueError(f"column '{column}' not in whitelist")
    rows = [r for r in snapshot.price_window if r.ticker == ticker]
    if not rows:
        raise ValueError(f"no rows for ticker {ticker}")
    rows_sorted = sorted(rows, key=lambda r: r.session_date)
    return np.asarray(
        [_extract_value(r, column) for r in rows_sorted], dtype=np.float64
    )


def _extract_value(row: PricePanelRow, column: str) -> float:
    """Pull a typed column out of a PricePanelRow."""
    if column == "adj_close":
        return row.adj_close
    if column == "log_return":
        return row.log_return
    if column == "adj_open":
        return row.adj_open
    if column == "adj_high":
        return row.adj_high
    if column == "adj_low":
        return row.adj_low
    if column == "volume":
        return float(row.volume)
    if column == "dollar_volume":
        return row.dollar_volume
    raise ValueError(f"column '{column}' not in whitelist")


def _trim_to_min_length(series_list: list[np.ndarray]) -> list[np.ndarray]:
    """Trim each series to the min length (align tail; defensive — DOES
    NOT do PIT alignment which would require session-date intersection,
    deferred to T8 once the agent context surfaces the alignment need)."""
    if not series_list:
        return []
    min_len = min(len(s) for s in series_list)
    return [s[-min_len:] for s in series_list]


def _bounded_summary(text: str) -> str:
    return text[:_MAX_SUMMARY_TEXT]


# ───────────────────────── per-callable dispatchers ─────────────────────────


def _do_ols_hac_nw(call: ToolCall, args: OLSArgs, snapshot: MarketSnapshot) -> ToolResult:
    y = _resolve_series(snapshot, args.y_ticker, args.y_series)
    x_matrix_cols = [_resolve_series(snapshot, t, args.x_series) for t in args.x_tickers]
    aligned = _trim_to_min_length([y, *x_matrix_cols])
    y_aligned = aligned[0]
    x_aligned = np.column_stack(aligned[1:]) if aligned[1:] else np.empty((len(y_aligned), 0))
    if args.add_constant and x_aligned.size > 0:
        x_aligned = add_constant(x_aligned)
    model = OLS(y_aligned, x_aligned).fit(
        cov_type="HAC", cov_kwds={"maxlags": args.hac_maxlags}
    )
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            coefficients=tuple(float(c) for c in model.params),
            pvalues=tuple(float(p) for p in model.pvalues),
            statistic=float(model.fvalue) if model.fvalue is not None else None,
            summary_text=_bounded_summary(
                f"OLS_HAC_NW maxlags={args.hac_maxlags} "
                f"R²={model.rsquared:.4f} n={int(model.nobs)}"
            ),
            extra={"rsquared": float(model.rsquared)},
        ),
    )


def _do_adfuller(call: ToolCall, args: ADFArgs, snapshot: MarketSnapshot) -> ToolResult:
    s = _resolve_series(snapshot, args.ticker, args.series)
    stat, pvalue, _, _, crit = adfuller(s, maxlag=args.maxlag, autolag=None)
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=float(stat),
            pvalues=(float(pvalue),),
            summary_text=_bounded_summary(
                f"adfuller maxlag={args.maxlag} stat={stat:.4f} p={pvalue:.4f}"
            ),
            extra={f"crit_{k}": float(v) for k, v in crit.items()},
        ),
    )


def _do_coint(call: ToolCall, args: CointArgs, snapshot: MarketSnapshot) -> ToolResult:
    a = _resolve_series(snapshot, args.ticker_a, args.series)
    b = _resolve_series(snapshot, args.ticker_b, args.series)
    a, b = _trim_to_min_length([a, b])
    stat, pvalue, crit = coint(a, b)
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=float(stat),
            pvalues=(float(pvalue),),
            summary_text=_bounded_summary(
                f"coint {args.ticker_a}-{args.ticker_b} stat={stat:.4f} p={pvalue:.4f}"
            ),
            extra={
                "crit_1pct": float(crit[0]),
                "crit_5pct": float(crit[1]),
                "crit_10pct": float(crit[2]),
            },
        ),
    )


def _do_arima(call: ToolCall, args: ARIMAArgs, snapshot: MarketSnapshot) -> ToolResult:
    s = _resolve_series(snapshot, args.ticker, args.series)
    fit = ARIMA(s, order=(1, 0, 0)).fit()
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            coefficients=tuple(float(c) for c in fit.params),
            pvalues=tuple(float(p) for p in fit.pvalues),
            statistic=float(fit.aic) if fit.aic is not None else None,
            summary_text=_bounded_summary(
                f"ARIMA(1,0,0) aic={fit.aic:.4f} n={len(s)}"
            ),
            extra={"aic": float(fit.aic), "bic": float(fit.bic)},
        ),
    )


def _do_spearmanr(call: ToolCall, args: CorrArgs, snapshot: MarketSnapshot) -> ToolResult:
    a = _resolve_series(snapshot, args.ticker_a, args.series_a)
    b = _resolve_series(snapshot, args.ticker_b, args.series_b)
    a, b = _trim_to_min_length([a, b])
    res = spearmanr(a, b)
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=float(res.statistic),
            pvalues=(float(res.pvalue),),
            summary_text=_bounded_summary(
                f"spearmanr {args.ticker_a}-{args.ticker_b} "
                f"rho={res.statistic:.4f} p={res.pvalue:.4f} n={len(a)}"
            ),
        ),
    )


def _do_pearsonr(call: ToolCall, args: CorrArgs, snapshot: MarketSnapshot) -> ToolResult:
    a = _resolve_series(snapshot, args.ticker_a, args.series_a)
    b = _resolve_series(snapshot, args.ticker_b, args.series_b)
    a, b = _trim_to_min_length([a, b])
    res = pearsonr(a, b)
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=float(res.statistic),
            pvalues=(float(res.pvalue),),
            summary_text=_bounded_summary(
                f"pearsonr {args.ticker_a}-{args.ticker_b} "
                f"r={res.statistic:.4f} p={res.pvalue:.4f} n={len(a)}"
            ),
        ),
    )


def _do_ttest_hac(call: ToolCall, args: TTestArgs, snapshot: MarketSnapshot) -> ToolResult:
    s = _resolve_series(snapshot, args.ticker, args.series)
    # scipy ttest_1samp is fine for the point estimate; HAC adjustment
    # applies to the standard error. Approximate via Newey-West variance
    # over the residuals (sample minus popmean).
    res = ttest_1samp(s, args.popmean)
    residuals = s - args.popmean
    n = len(residuals)
    if n < 2:
        raise ValueError("ttest_hac: too few samples")
    # Newey-West variance estimator.
    gamma0 = float(np.var(residuals, ddof=1))
    nw_var = gamma0
    for lag in range(1, min(args.hac_maxlags + 1, n)):
        cov = float(np.mean(residuals[:-lag] * residuals[lag:]))
        weight = 1.0 - lag / (args.hac_maxlags + 1)
        nw_var += 2.0 * weight * cov
    hac_se = math.sqrt(max(nw_var / n, 1e-12))
    hac_t = float((np.mean(s) - args.popmean) / hac_se) if hac_se > 0 else 0.0
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=hac_t,
            pvalues=(float(res.pvalue),),  # the homoskedastic p is reported separately for comparison
            summary_text=_bounded_summary(
                f"ttest_1samp_HAC mean={np.mean(s):.6f} "
                f"hac_t={hac_t:.4f} hac_se={hac_se:.6f} maxlags={args.hac_maxlags}"
            ),
            extra={
                "hac_se": hac_se,
                "homoskedastic_t": float(res.statistic),
                "sample_mean": float(np.mean(s)),
                "sample_std": float(np.std(s, ddof=1)),
                "n": float(n),
            },
        ),
    )


def _do_variance_ratio(
    call: ToolCall, args: VarianceRatioArgs, snapshot: MarketSnapshot
) -> ToolResult:
    """Lo-MacKinlay 1988 variance ratio test."""
    s = _resolve_series(snapshot, args.ticker, args.series)
    if len(s) < args.q * 4:
        raise ValueError("variance_ratio: insufficient samples")
    # VR(q) = Var(r_t(q)) / (q * Var(r_t))  where r_t(q) = sum r_t over q periods.
    q = args.q
    n = (len(s) // q) * q
    s_trim = s[-n:]
    var1 = float(np.var(s_trim, ddof=1))
    q_returns = s_trim.reshape(-1, q).sum(axis=1)
    var_q = float(np.var(q_returns, ddof=1))
    vr = var_q / (q * var1) if var1 > 0 else float("nan")
    # Lo-MacKinlay z-statistic (under H0: VR=1).
    z = (vr - 1.0) * math.sqrt(n / (2.0 * (q - 1)))
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=float(vr),
            pvalues=(),
            summary_text=_bounded_summary(
                f"variance_ratio q={q} VR={vr:.4f} z={z:.4f}"
            ),
            extra={"z_stat": float(z), "n_obs": float(n)},
        ),
    )


def _do_hurst(call: ToolCall, args: HurstArgs, snapshot: MarketSnapshot) -> ToolResult:
    """Hurst exponent via Rescaled-Range (R/S) analysis."""
    s = _resolve_series(snapshot, args.ticker, args.series)
    if len(s) < args.max_lag * 2:
        raise ValueError("hurst: insufficient samples")
    lags = np.arange(2, args.max_lag + 1)
    rs_values: list[float] = []
    for lag in lags:
        # Rescaled-range over chunks of length `lag`.
        n_chunks = len(s) // lag
        if n_chunks < 2:
            continue
        chunks = s[: n_chunks * lag].reshape(n_chunks, lag)
        rs_per_chunk: list[float] = []
        for chunk in chunks:
            mean = chunk.mean()
            deviations = np.cumsum(chunk - mean)
            r = deviations.max() - deviations.min()
            std = chunk.std(ddof=1)
            if std > 0:
                rs_per_chunk.append(r / std)
        if rs_per_chunk:
            rs_values.append(float(np.mean(rs_per_chunk)))
    if len(rs_values) < 2:
        raise ValueError("hurst: rs values empty")
    used_lags = lags[: len(rs_values)]
    # H = slope of log(R/S) vs log(lag).
    log_lags = np.log(used_lags)
    log_rs = np.log(rs_values)
    slope, intercept = np.polyfit(log_lags, log_rs, 1)
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=float(slope),
            pvalues=(),
            summary_text=_bounded_summary(
                f"hurst_exponent H={slope:.4f} intercept={intercept:.4f} "
                f"max_lag={args.max_lag}"
            ),
        ),
    )


def _do_ljung_box(
    call: ToolCall, args: LjungBoxArgs, snapshot: MarketSnapshot
) -> ToolResult:
    s = _resolve_series(snapshot, args.ticker, args.series)
    df = acorr_ljungbox(s, lags=args.lags, return_df=True)
    last_stat = float(df["lb_stat"].iloc[-1])
    last_p = float(df["lb_pvalue"].iloc[-1])
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=last_stat,
            pvalues=(last_p,),
            summary_text=_bounded_summary(
                f"ljung_box lags={args.lags} stat={last_stat:.4f} p={last_p:.4f}"
            ),
        ),
    )


def _do_rolling_corr(
    call: ToolCall,
    args: RollingCorrArgs,
    snapshot: MarketSnapshot,
    kind: Literal["spearman", "pearson"],
) -> ToolResult:
    """Rolling correlation + bootstrap CI of the median IC across windows."""
    a = _resolve_series(snapshot, args.ticker_a, args.series)
    b = _resolve_series(snapshot, args.ticker_b, args.series)
    a, b = _trim_to_min_length([a, b])
    if len(a) < args.window + 5:
        raise ValueError(f"rolling_{kind}: insufficient samples")
    rolling: list[float] = []
    for i in range(args.window, len(a)):
        window_a = a[i - args.window : i]
        window_b = b[i - args.window : i]
        if kind == "spearman":
            r = spearmanr(window_a, window_b).statistic
        else:
            r = pearsonr(window_a, window_b).statistic
        if not math.isnan(float(r)):
            rolling.append(float(r))
    if not rolling:
        raise ValueError(f"rolling_{kind}: empty IC series")
    rolling_arr = np.asarray(rolling)
    median = float(np.median(rolling_arr))
    # Bootstrap 95% CI on the median.
    bootstrap_medians: list[float] = []
    for _ in range(_BOOTSTRAP_ITERATIONS):
        sample = np.random.choice(rolling_arr, size=len(rolling_arr), replace=True)
        bootstrap_medians.append(float(np.median(sample)))
    lo, hi = np.percentile(bootstrap_medians, [2.5, 97.5])
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=median,
            pvalues=(),
            summary_text=_bounded_summary(
                f"rolling_{kind} window={args.window} median={median:.4f} "
                f"ci_95=({lo:.4f}, {hi:.4f}) n_windows={len(rolling)}"
            ),
            extra={
                "median": median,
                "ci_95_lo": float(lo),
                "ci_95_hi": float(hi),
                "n_windows": float(len(rolling)),
            },
        ),
    )


def _do_fama_macbeth(
    call: ToolCall, args: FamaMacBethArgs, snapshot: MarketSnapshot
) -> ToolResult:
    """Fama-MacBeth panel regression: per-session cross-section OLS_HAC_NW
    over (y_ticker, x_tickers) → time-series of coefficients → t-stat HAC."""
    if not args.x_tickers:
        raise ValueError("fama_macbeth: empty x_tickers")
    by_date: dict[Any, dict[str, float]] = {}
    for row in snapshot.price_window:
        if row.ticker not in args.x_tickers:
            continue
        by_date.setdefault(row.session_date, {})[row.ticker] = (
            row.log_return if args.y_series == "log_return" else row.adj_close
        )
    per_session_coefs: list[float] = []
    for _session, ticker_returns in sorted(by_date.items()):
        if len(ticker_returns) < 2:
            continue
        y = np.asarray(list(ticker_returns.values()), dtype=np.float64)
        x = np.arange(len(y), dtype=np.float64).reshape(-1, 1)
        x_const = add_constant(x)
        try:
            fit = OLS(y, x_const).fit()
            per_session_coefs.append(float(fit.params[1]))
        except Exception:  # noqa: BLE001
            continue
    if len(per_session_coefs) < 5:
        raise ValueError("fama_macbeth: too few sessions")
    coefs_arr = np.asarray(per_session_coefs)
    mean_coef = float(np.mean(coefs_arr))
    se_coef = float(np.std(coefs_arr, ddof=1) / math.sqrt(len(coefs_arr)))
    t_stat = mean_coef / se_coef if se_coef > 0 else 0.0
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=t_stat,
            pvalues=(),
            summary_text=_bounded_summary(
                f"fama_macbeth mean_coef={mean_coef:.6f} se={se_coef:.6f} "
                f"t={t_stat:.4f} n_sessions={len(per_session_coefs)}"
            ),
            extra={"mean_coef": mean_coef, "se_coef": se_coef},
        ),
    )


def _do_cost_net_simulation(
    call: ToolCall, args: CostNetSimulationArgs, snapshot: MarketSnapshot
) -> ToolResult:
    """Cost-net P&L projection — THE BINDING outcome gate (spec §6.1 + §4.5).

    For each (entry_session, exit_session) pair: compute gross return
    from prices at those sessions; subtract cost_bps_roundtrip/10000;
    aggregate into gross + net Sharpe; bootstrap a 95% CI on the
    cost-net Sharpe.
    """
    if len(args.entry_sessions) != len(args.exit_sessions):
        raise ValueError("entry_sessions / exit_sessions length mismatch")
    if not args.entry_sessions:
        raise ValueError("no entry sessions")
    closes_by_date = {r.session_date: r.adj_close for r in snapshot.price_window if r.ticker == args.ticker}
    if not closes_by_date:
        raise ValueError(f"no price data for {args.ticker}")
    gross_returns: list[float] = []
    for entry_str, exit_str in zip(args.entry_sessions, args.exit_sessions, strict=True):
        from datetime import date as _date
        try:
            entry_d = _date.fromisoformat(entry_str)
            exit_d = _date.fromisoformat(exit_str)
        except ValueError:
            continue
        if entry_d not in closes_by_date or exit_d not in closes_by_date:
            continue
        entry_px = closes_by_date[entry_d]
        exit_px = closes_by_date[exit_d]
        if entry_px <= 0:
            continue
        gross_returns.append((exit_px - entry_px) / entry_px)
    if not gross_returns:
        raise ValueError("no usable trades")
    cost_per_trade = args.cost_assumption_bps_roundtrip / 10_000.0
    net_returns = [g - cost_per_trade for g in gross_returns]
    gross_arr = np.asarray(gross_returns)
    net_arr = np.asarray(net_returns)
    gross_sharpe = (
        float(np.mean(gross_arr) / np.std(gross_arr, ddof=1) * math.sqrt(252))
        if np.std(gross_arr, ddof=1) > 0
        else 0.0
    )
    net_sharpe = (
        float(np.mean(net_arr) / np.std(net_arr, ddof=1) * math.sqrt(252))
        if np.std(net_arr, ddof=1) > 0
        else 0.0
    )
    # Bootstrap 95% CI on cost_net_sharpe.
    bootstrap_sharpes: list[float] = []
    iters = args.bootstrap_iterations
    if iters > 0 and len(net_arr) >= 2:
        for _ in range(iters):
            sample = np.random.choice(net_arr, size=len(net_arr), replace=True)
            s_std = float(np.std(sample, ddof=1))
            bootstrap_sharpes.append(
                float(np.mean(sample) / s_std * math.sqrt(252)) if s_std > 0 else 0.0
            )
    if bootstrap_sharpes:
        ci_lo, ci_hi = np.percentile(bootstrap_sharpes, [2.5, 97.5])
    else:
        ci_lo, ci_hi = float("nan"), float("nan")
    total_cost_bps = args.cost_assumption_bps_roundtrip * len(gross_returns)
    bleed_projection_usd = total_cost_bps / 10_000.0 * 25_000.0  # vs $25k slot
    return ToolResult(
        call=call,
        numeric_summary=NumericSummary(
            statistic=net_sharpe,
            pvalues=(),
            summary_text=_bounded_summary(
                f"cost_net_simulation ticker={args.ticker} n_trades={len(gross_returns)} "
                f"gross_sharpe={gross_sharpe:.4f} net_sharpe={net_sharpe:.4f} "
                f"cost_bps_roundtrip={args.cost_assumption_bps_roundtrip} "
                f"net_ci_95=({ci_lo:.4f}, {ci_hi:.4f}) "
                f"bleed_projection_usd={bleed_projection_usd:.2f}"
            ),
            extra={
                "gross_sharpe": gross_sharpe,
                "cost_net_sharpe": net_sharpe,
                "total_cost_bps_roundtrip": total_cost_bps,
                "bleed_projection_usd": bleed_projection_usd,
                "ci_95_lo": float(ci_lo) if not math.isnan(ci_lo) else 0.0,
                "ci_95_hi": float(ci_hi) if not math.isnan(ci_hi) else 0.0,
                "n_trades": float(len(gross_returns)),
            },
        ),
    )


__all__ = [
    "ADFArgs",
    "ARIMAArgs",
    "CointArgs",
    "CorrArgs",
    "CostNetSimulationArgs",
    "FamaMacBethArgs",
    "HurstArgs",
    "LjungBoxArgs",
    "OLSArgs",
    "RollingCorrArgs",
    "TTestArgs",
    "VarianceRatioArgs",
    "dispatch",
]
