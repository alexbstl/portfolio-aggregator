"""
All portfolio risk & performance computations, in ONE file for easy auditing.

Pure functions over numpy arrays (no scipy) — no DB, no I/O, no app imports. The database
layer fetches raw daily values / benchmark closes / external cashflows and hands
them here. Every formula is documented inline so the math can be checked
independently of the plumbing.

Conventions
-----------
- Returns are SIMPLE daily returns (r_t = V_t / V_{t-1} - 1), not log returns.
- Portfolio returns are FLOW-STRIPPED (deposits/withdrawals removed) so that
  contributions don't masquerade as performance — same basis as the TWR curve.
- Annualization uses `periods_per_year` trading days (default 252): returns scale
  linearly, volatilities by sqrt(periods).
- VaR / ES are reported as POSITIVE loss fractions at a 1-day horizon (e.g.
  0.031 = a 3.1% loss). Multiply by the portfolio value for a dollar figure, or
  by sqrt(h) to scale to an h-day horizon.
- Risk-free is supplied as an ANNUAL rate and converted to a daily rate.
"""
from __future__ import annotations

import math

import numpy as np

TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Distribution helpers (pure numpy / stdlib — no scipy dependency)
# ---------------------------------------------------------------------------

def _norm_pdf(z: float) -> float:
    """Standard-normal probability density at z."""
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _norm_ppf(p: float) -> float:
    """
    Inverse standard-normal CDF (quantile) via Acklam's rational approximation.
    Accurate to ~1.1e-9 over the open interval (0, 1) — plenty for VaR quantiles.
    Replaces scipy.stats.norm.ppf.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
               ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
               (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
            ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)


def _skew(r) -> float:
    """Sample skewness (biased / population form, matching scipy default)."""
    d = r - r.mean()
    m2 = np.mean(d ** 2)
    m3 = np.mean(d ** 3)
    return float(m3 / m2 ** 1.5) if m2 > 0 else 0.0


def _kurtosis(r) -> float:
    """Excess kurtosis (Fisher; biased form, matching scipy default). 0 = normal."""
    d = r - r.mean()
    m2 = np.mean(d ** 2)
    m4 = np.mean(d ** 4)
    return float(m4 / m2 ** 2 - 3.0) if m2 > 0 else -3.0


# ---------------------------------------------------------------------------
# Return series
# ---------------------------------------------------------------------------

def daily_returns(values, flows, floor_frac: float = 0.01):
    """
    Flow-stripped simple daily returns, aligned to `values` (length N, NaN where
    no valid return exists — day 0 and any day whose prior value is below the
    funding floor).

        r_t = (V_t - flow_t) / V_{t-1} - 1

    `flows[t]` = net external cashflow on day t (deposits +, withdrawals -), so
    subtracting it removes the mechanical value change a contribution causes.

    The floor (1% of the peak value by default) skips the early near-zero period
    of a reconstructed curve, where a tiny denominator makes the daily ratio blow
    up. Days with a non-positive gross ratio (pathological / full wipeout, almost
    always a data error) are also skipped.
    """
    v = np.asarray(values, dtype=float)
    f = np.asarray(flows, dtype=float)
    n = len(v)
    r = np.full(n, np.nan)
    if n < 2:
        return r
    peak = np.nanmax(v)
    floor = max(peak * floor_frac, 1.0)
    for i in range(1, n):
        pv = v[i - 1]
        if pv is not None and pv >= floor:
            gross = (v[i] - f[i]) / pv
            if gross > 0:
                r[i] = gross - 1.0
    return r


def twr_index(returns):
    """
    Time-weighted cumulative growth index parallel to `returns` (starts at 1.0,
    compounds only on valid days). NaN returns are treated as no-change. This is
    what the performance chart plots in TWR mode.
    """
    r = np.asarray(returns, dtype=float)
    idx = np.ones(len(r))
    acc = 1.0
    for i in range(len(r)):
        if not np.isnan(r[i]):
            acc *= (1.0 + r[i])
        idx[i] = acc
    return idx


def simple_returns(prices):
    """Plain simple returns of a price series (NaN for the first point and any
    point without a valid prior price). Used to turn benchmark closes into a
    return series comparable to the portfolio's."""
    p = np.asarray(prices, dtype=float)
    n = len(p)
    r = np.full(n, np.nan)
    for i in range(1, n):
        if np.isfinite(p[i]) and np.isfinite(p[i - 1]) and p[i - 1] > 0:
            r[i] = p[i] / p[i - 1] - 1.0
    return r


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------

def _daily_rf(risk_free_annual: float, periods: int) -> float:
    """Annual risk-free rate -> equivalent daily rate (geometric)."""
    return (1.0 + risk_free_annual) ** (1.0 / periods) - 1.0


def _clean(r):
    """Finite returns only."""
    r = np.asarray(r, dtype=float)
    return r[np.isfinite(r)]


# ---------------------------------------------------------------------------
# Performance & risk metrics for a single return series
# ---------------------------------------------------------------------------

def compute_metrics(returns, risk_free_annual: float = 0.0,
                    periods: int = TRADING_DAYS,
                    confidences=(0.95, 0.99)) -> dict:
    """
    Full standalone metric block for one return series. `returns` may contain
    NaN (they're dropped). Returns a dict of floats (None where undefined, e.g.
    too few points).
    """
    r = _clean(returns)
    n = int(r.size)
    out: dict = {"n": n}
    if n < 2:
        return out  # not enough data for anything meaningful

    rf_d = _daily_rf(risk_free_annual, periods)
    mean = float(np.mean(r))
    # ddof=1 (sample) std — we're estimating from a sample, not a population.
    sd = float(np.std(r, ddof=1))

    # --- growth / return ---
    wealth = np.cumprod(1.0 + r)
    total_return = float(wealth[-1] - 1.0)
    years = n / periods
    cagr = float((1.0 + total_return) ** (1.0 / years) - 1.0) if years > 0 and (1.0 + total_return) > 0 else None

    # --- volatility ---
    ann_vol = sd * np.sqrt(periods)
    # downside deviation: RMS of returns below the risk-free target (MAR = rf)
    downside = np.minimum(r - rf_d, 0.0)
    downside_dev_d = float(np.sqrt(np.mean(downside ** 2)))
    downside_dev = downside_dev_d * np.sqrt(periods)

    # --- risk-adjusted ---
    excess_mean = mean - rf_d
    sharpe = float(excess_mean / sd * np.sqrt(periods)) if sd > 0 else None
    sortino = float(excess_mean / downside_dev_d * np.sqrt(periods)) if downside_dev_d > 0 else None

    # --- drawdown (on the cumulative wealth curve) ---
    running_max = np.maximum.accumulate(wealth)
    drawdown = wealth / running_max - 1.0
    max_drawdown = float(drawdown.min())
    current_drawdown = float(drawdown[-1])
    calmar = float(cagr / abs(max_drawdown)) if (cagr is not None and max_drawdown < 0) else None

    # --- distribution shape ---
    skew = _skew(r) if n >= 3 else None
    # excess kurtosis (Fisher): 0 for a normal distribution
    exkurt = _kurtosis(r) if n >= 4 else None

    out.update({
        "total_return": total_return,
        "cagr": cagr,
        "ann_vol": float(ann_vol),
        "downside_dev": float(downside_dev),
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "current_drawdown": current_drawdown,
        "calmar": calmar,
        "skew": skew,
        "excess_kurtosis": exkurt,
        "var": {},
        "es": {},
    })

    # --- tail risk: VaR & Expected Shortfall (1-day, positive-loss) ---
    for c in confidences:
        alpha = 1.0 - c                      # tail probability, e.g. 0.05
        z = _norm_ppf(alpha)                 # standard-normal quantile (negative)
        key = f"{int(round(c * 100))}"

        # (a) Gaussian / parametric
        var_gauss = -(mean + sd * z)
        # Gaussian ES: E[loss | loss > VaR] under normality
        es_gauss = -(mean - sd * _norm_pdf(z) / alpha)

        # (b) Cornish-Fisher: adjust the normal quantile for skew (S) and excess
        # kurtosis (K), capturing fat tails / asymmetry without fitting a full
        # distribution.
        S = skew if skew is not None else 0.0
        K = exkurt if exkurt is not None else 0.0
        z_cf = (z
                + (z ** 2 - 1) * S / 6.0
                + (z ** 3 - 3 * z) * K / 24.0
                - (2 * z ** 3 - 5 * z) * (S ** 2) / 36.0)
        var_cf = -(mean + sd * z_cf)

        # (c) Historical / empirical (non-parametric — reflects the actual tail)
        var_hist = -float(np.percentile(r, alpha * 100.0))
        # Empirical ES: mean of losses at or beyond the empirical VaR threshold
        tail = r[r <= -var_hist]
        es_hist = -float(np.mean(tail)) if tail.size > 0 else var_hist

        out["var"][key] = {
            "gaussian": float(var_gauss),
            "cornish_fisher": float(var_cf),   # heavy-tailed
            "historical": float(var_hist),
        }
        out["es"][key] = {
            "gaussian": float(es_gauss),
            "historical": float(es_hist),      # heavy-tailed (empirical)
        }

    return out


# ---------------------------------------------------------------------------
# Benchmark-relative metrics (portfolio vs one benchmark)
# ---------------------------------------------------------------------------

def compute_vs_benchmark(returns, benchmark_returns,
                         risk_free_annual: float = 0.0,
                         periods: int = TRADING_DAYS) -> dict:
    """
    Relative metrics of a return series against a benchmark's return series
    (same alignment/length; NaN in either day drops that day from the pairing).
    """
    r = np.asarray(returns, dtype=float)
    b = np.asarray(benchmark_returns, dtype=float)
    m = np.isfinite(r) & np.isfinite(b)
    r, b = r[m], b[m]
    n = int(r.size)
    out: dict = {"n": n}
    if n < 2:
        return out

    rf_d = _daily_rf(risk_free_annual, periods)
    var_b = float(np.var(b, ddof=1))
    # beta = cov(r, b) / var(b); ddof=1 for sample estimates
    cov = float(np.cov(r, b, ddof=1)[0, 1])
    beta = cov / var_b if var_b > 0 else None
    corr = float(np.corrcoef(r, b)[0, 1]) if (np.std(r) > 0 and np.std(b) > 0) else None

    # Jensen's alpha (annualized): a = E[r] - (rf + beta*(E[b]-rf))
    alpha = None
    if beta is not None:
        alpha_d = (np.mean(r) - rf_d) - beta * (np.mean(b) - rf_d)
        alpha = float(alpha_d * periods)

    # tracking error & information ratio on active returns (r - b)
    active = r - b
    te = float(np.std(active, ddof=1) * np.sqrt(periods))
    ir = float(np.mean(active) / np.std(active, ddof=1) * np.sqrt(periods)) if np.std(active, ddof=1) > 0 else None

    # up/down capture: portfolio's avg return on the benchmark's up (down) days,
    # relative to the benchmark's own avg on those days.
    up = b > 0
    down = b < 0
    up_capture = float(np.mean(r[up]) / np.mean(b[up])) if up.any() and np.mean(b[up]) != 0 else None
    down_capture = float(np.mean(r[down]) / np.mean(b[down])) if down.any() and np.mean(b[down]) != 0 else None

    out.update({
        "beta": beta,
        "correlation": corr,
        "alpha_annual": alpha,
        "tracking_error": te,
        "information_ratio": ir,
        "up_capture": up_capture,
        "down_capture": down_capture,
    })
    return out
