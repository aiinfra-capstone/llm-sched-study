"""MPR-1 — throughput non-stationarity: tau, the variance envelope, and what they cost.

This module is the headline result of my half, and it is deliberately the part with no
scheduler in it. §7 says MPR-1 stands alone: if the pool never reaches five nodes and no
policy comparison ever runs, a characterization of how consumer LLM serving throughput
drifts under sustained load is still a measurement contribution.

The claim MPR-1 makes is not "throughput varies" — everyone knows that. It is:

    a single calibrated tok/s figure is a moving average over a non-stationary process,
    and the error you make by treating it as a constant is quantifiable.

So this module produces three things, in increasing order of how much they cost a
scheduler that ignores them:

  tau                 the autocorrelation time. How long a measurement stays informative.
                      C-3's `stochastic.autocorr_time_s`, F-22's input to the DES, and the
                      x-axis H3 is defined against ("staleness approaching tau").
  variance envelope   the band the throughput actually occupies, as p05/p95 rather than a
                      standard deviation, because the distribution is not symmetric and a
                      +/-sigma band would claim it is.
  se_inflation        how badly the naive standard error of a calibration mean understates
                      itself once autocorrelation is accounted for. This is the number
                      that turns MPR-1 from an observation into an argument.

**How the throughput series is built matters more than the estimator applied to it.** A
request's decode does not happen at the instant it completes; it occupies the interval
`[t_end - decode_ns, t_end]`. Attributing all of its tokens to the completion stamp would
manufacture spikes at low concurrency and then measure the autocorrelation of my own
binning. So each request's tokens are spread uniformly across its own decode interval and
integrated over the window. That is the difference between measuring the node and
measuring the histogram.

**Decode tok/s, not end-to-end tok/s.** Prefill scales with prompt length, so
`output_tokens / service_ns` would fall when the workload's prompts got longer and a node
would look like it had slowed down. tau and the envelope are properties of the node, and
must not move when the trace does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "AutocorrFit",
    "StationarityReport",
    "ThroughputSeries",
    "VarianceEnvelope",
    "acf",
    "characterize",
    "fit_autocorr_time",
    "lognormal_sigma",
    "variance_envelope",
    "windowed_throughput",
]

# Sokal's automatic windowing constant for the integrated autocorrelation time: sum lags
# until the lag index reaches C * tau_int. 5 is the usual choice for a roughly
# exponential ACF; it stops the sum before it accumulates noise from lags where the true
# correlation is already zero.
_SOKAL_C = 5.0

_ONE_OVER_E = 1.0 / math.e

# An autocorrelation time cannot be estimated from a handful of windows. With fewer than
# this many points the lag-1 correlation is dominated by sampling noise, and the usual
# failure is not a wrong tau but a *confidently* wrong one: the ACF crosses zero early by
# chance, the exponential fit latches onto two noisy lags, and the number that comes out
# looks perfectly reportable. 30 is the conventional floor for a series whose leading
# lags carry the signal, and refusing below it is cheaper than discovering in Week 6 that
# MPR-1's headline was fitted to noise.
MIN_WINDOWS = 30

# llama.cpp decodes its `--parallel` slots as one batch, so requests do not finish
# independently — they finish in *bursts* of up to `batch_size`, roughly one burst per
# request duration. That makes the completion process near-periodic, and the ACF of a
# near-periodic signal has peaks and troughs an exponential fit will happily read as decay.
#
# So the density that matters is bursts per window, not requests per window. Below this
# many bursts the windowed series is measuring slot turnover rhythm rather than the node.
#
# This is not a theoretical worry. On a 660 s segment at 0.78 completions/s with 4 slots —
# about 1.9 bursts per 10 s window — tau came out censored at 6, 8 and 12 s windows and
# ~16 s at 10 and 15 s: the windows that happened to be near-integer multiples of the
# ~5 s burst period averaged it out, and the others aliased against it. A number that
# moves with the binning is a number about the binning. The fix is a denser completion
# stream (shorter outputs at the same concurrency), which is a property of the *campaign
# design* — so it is reported rather than silently tolerated.
MIN_BURSTS_PER_WINDOW = 5.0


@dataclass(frozen=True)
class ThroughputSeries:
    """Decode tok/s in fixed windows on one host's monotonic clock.

    `t0_ns` is a monotonic stamp and is meaningless across hosts — it exists so windows
    can be lined up with the calibration samples that produced them, not so it can be
    subtracted from anything measured elsewhere.
    """

    dt_s: float
    values: np.ndarray
    t0_ns: int

    def __len__(self) -> int:
        return int(self.values.size)

    @property
    def duration_s(self) -> float:
        return len(self) * self.dt_s


@dataclass(frozen=True)
class AutocorrFit:
    """tau, and enough of the fit to argue with.

    Two estimators are reported because they fail differently, and agreeing is evidence:

    `tau_s` fits rho(k) = exp(-k*dt/tau) by least squares on log rho. It is the number
    that goes in C-3 and it assumes the decay is exponential.

    `integrated_tau_s` is dt * (1 + 2*sum_k rho_k) with Sokal windowing, which assumes
    nothing about the shape. It is what `se_inflation` is actually computed from.

    `e_folding_s` is the first 1/e crossing, linearly interpolated — no fit at all, and
    the one a reader can check off the plotted ACF with a ruler.

    When the three disagree materially the decay is not exponential and the C-3 number
    should be reported with that caveat rather than quietly used. `fit_r2` is how you
    know: it is the R-squared of the log-linear fit, not of anything about service times.
    """

    tau_s: float
    fit_r2: float
    integrated_tau_s: float
    e_folding_s: float
    lags_fitted: int
    acf: np.ndarray = field(repr=False)
    censored: bool = False
    """True when the ACF showed no decay to fit, so `tau_s` is an upper bound.

    This is not a failure. A node whose throughput decorrelates faster than one window is
    a *stationary* node at that operating point, and saying so is an MPR-1 result — it is
    the observation that the drift H3 is built on is absent here. `tau_s` then carries the
    tightest bound the data supports (one window) so that C-3 stays satisfiable and the
    DES gets a conservative number, and `tau_resolved` on the report is what stops anyone
    quoting the bound as a measurement.
    """

    @property
    def agrees(self) -> bool:
        """True when the fitted and integrated estimates are within 2x of each other.

        A deliberately loose gate. It is not a test that the process is exponential; it
        is a tripwire for the case where the fit has latched onto a few noisy lags and
        the two estimators are telling different stories.
        """
        lo, hi = sorted((self.tau_s, self.integrated_tau_s))
        return hi <= 2.0 * lo


@dataclass(frozen=True)
class VarianceEnvelope:
    """The band throughput occupies, reported as percentiles rather than as +/- sigma.

    Serving throughput under batching is not symmetric about its mean — it has a ceiling
    set by the hardware and a long floor set by thermal and memory-pressure excursions.
    A standard deviation quoted alone would imply a symmetry that is not there, so
    `band_ratio` (p95/p05) is the single number I would put in a caption.
    """

    mean_tok_s: float
    sd_tok_s: float
    cv: float
    p05_tok_s: float
    p50_tok_s: float
    p95_tok_s: float
    min_tok_s: float
    max_tok_s: float

    @property
    def band_ratio(self) -> float:
        return self.p95_tok_s / self.p05_tok_s if self.p05_tok_s > 0 else math.inf

    def to_dict(self) -> dict[str, float]:
        return {
            "mean_tok_s": round(self.mean_tok_s, 4),
            "sd_tok_s": round(self.sd_tok_s, 4),
            "cv": round(self.cv, 5),
            "p05_tok_s": round(self.p05_tok_s, 4),
            "p50_tok_s": round(self.p50_tok_s, 4),
            "p95_tok_s": round(self.p95_tok_s, 4),
            "min_tok_s": round(self.min_tok_s, 4),
            "max_tok_s": round(self.max_tok_s, 4),
            "band_ratio": round(self.band_ratio, 4),
        }


@dataclass(frozen=True)
class StationarityReport:
    """Everything MPR-1 claims about one node class, in one object."""

    node_class: str
    fit: AutocorrFit
    envelope: VarianceEnvelope
    window_s: float
    n_windows: int
    n_samples: int
    sigma: float
    resolution_floor_s: float
    batch_size: int = 1

    @property
    def samples_per_window(self) -> float:
        """Mean completions per window — how densely the node reported itself."""
        return self.n_samples / self.n_windows

    @property
    def bursts_per_window(self) -> float:
        """Completions per window divided by batch size: how many slot turnovers.

        The one diagnostic that separates "this node is stationary" from "I could not see
        whether it was".
        """
        return self.samples_per_window / self.batch_size

    @property
    def cadence_limited(self) -> bool:
        """True when windows hold too few slot turnovers to average the burst rhythm out."""
        return self.bursts_per_window < MIN_BURSTS_PER_WINDOW

    @property
    def tau_resolved(self) -> bool:
        """Whether tau sits far enough above the resolution floor to be a measurement.

        A node only reports its throughput when a request finishes, so the finest
        timescale on which throughput can vary *as observed* is one request. Worse, the
        uniform-rate attribution in `windowed_throughput` spreads each request's tokens
        across every window its decode touches, which **induces** positive correlation out
        to a lag of one request duration. A tau of the same order as that duration is
        therefore indistinguishable from the binning, and reporting it would be reporting
        my own estimator.

        2x is the bar. Below it the honest statement is "tau is at or below the
        resolution floor of this operating point" — which is itself a finding: it says the
        node was stationary on every timescale the measurement could see.

        Density is the second condition and it is the one that actually bit. A segment
        whose windows hold only a request or two produces an ACF shaped by completion
        cadence, and a fit through it is an estimate of the slot turnover rhythm wearing
        tau's name.
        """
        return (
            not self.fit.censored
            and not self.cadence_limited
            and self.fit.tau_s >= 2.0 * self.resolution_floor_s
        )

    @property
    def n_eff(self) -> float:
        """Effective sample size: n * dt / integrated tau.

        The number of *independent* throughput observations the segment actually
        contains. It is what makes the MPR-1 sentence quantitative — a 10-minute
        calibration is not 300 samples of a 2-second window, it is 600s/tau of them.
        """
        return max(self.n_windows * self.window_s / self.fit.integrated_tau_s, 1.0)

    @property
    def se_inflation(self) -> float:
        """How much the naive standard error of the calibrated mean understates itself.

        sqrt(n / n_eff). A scheduler that averages a calibration window and treats the
        result as a precise constant is wrong by this factor, and this is the factor.
        """
        return math.sqrt(self.n_windows / self.n_eff)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_class": self.node_class,
            "window_s": self.window_s,
            "n_windows": self.n_windows,
            "n_samples": self.n_samples,
            "autocorr_time_s": round(self.fit.tau_s, 4),
            "integrated_autocorr_time_s": round(self.fit.integrated_tau_s, 4),
            "e_folding_s": round(self.fit.e_folding_s, 4),
            "fit_r2": round(self.fit.fit_r2, 4),
            "estimators_agree": self.fit.agrees,
            "sigma": round(self.sigma, 5),
            "resolution_floor_s": round(self.resolution_floor_s, 4),
            "tau_resolved": self.tau_resolved,
            "tau_censored": self.fit.censored,
            "samples_per_window": round(self.samples_per_window, 3),
            "batch_size": self.batch_size,
            "bursts_per_window": round(self.bursts_per_window, 3),
            "cadence_limited": self.cadence_limited,
            "n_eff": round(self.n_eff, 3),
            "se_inflation": round(self.se_inflation, 4),
            "envelope": self.envelope.to_dict(),
        }


def windowed_throughput(
    t_end_ns: np.ndarray | list[int],
    decode_ns: np.ndarray | list[int],
    output_tokens: np.ndarray | list[int],
    *,
    window_s: float,
    t0_ns: int | None = None,
) -> ThroughputSeries:
    """Decode tok/s per window, with each request's tokens spread over its own decode.

    Requests with a non-positive decode interval are dropped rather than treated as
    instantaneous: they are engine errors or timeouts, and a request that produced no
    tokens over no time carries no throughput information.
    """
    t_end = np.asarray(t_end_ns, dtype=np.float64)
    dur = np.asarray(decode_ns, dtype=np.float64)
    toks = np.asarray(output_tokens, dtype=np.float64)
    if not (t_end.size == dur.size == toks.size):
        raise ValueError("t_end_ns, decode_ns and output_tokens must be the same length")
    if window_s <= 0:
        raise ValueError(f"window_s must be > 0, got {window_s}")

    keep = (dur > 0) & (toks > 0)
    t_end, dur, toks = t_end[keep], dur[keep], toks[keep]
    if t_end.size == 0:
        raise ValueError("no usable samples: every request had zero decode time or zero tokens")

    t_start = t_end - dur
    origin = float(t0_ns if t0_ns is not None else t_start.min())
    span_ns = float(t_end.max()) - origin
    window_ns = window_s * 1e9
    n_windows = math.floor(span_ns / window_ns)
    if n_windows < 2:
        raise ValueError(
            f"segment spans {span_ns / 1e9:.2f}s, which is under two {window_s:g}s windows — "
            "tau cannot be estimated from fewer than two points; run a longer segment or "
            "shrink window_s"
        )

    edges = origin + np.arange(n_windows + 1, dtype=np.float64) * window_ns
    # Uniform-rate attribution: the fraction of each request's decode interval that falls
    # inside a window is the fraction of its tokens credited to that window. Vectorized as
    # an overlap matrix — the segments are minutes of requests, not millions.
    lo = np.maximum(t_start[:, None], edges[None, :-1])
    hi = np.minimum(t_end[:, None], edges[None, 1:])
    overlap = np.clip(hi - lo, 0.0, None)
    credited = (overlap / dur[:, None]) * toks[:, None]
    return ThroughputSeries(
        dt_s=window_s, values=credited.sum(axis=0) / window_s, t0_ns=round(origin)
    )


def acf(x: np.ndarray | list[float], max_lag: int | None = None) -> np.ndarray:
    """Normalized sample autocorrelation, lag 0 first and equal to 1.

    The 1/n normalization (rather than 1/(n-k)) is the biased estimator, and that is the
    right choice here: it damps the high-lag tail where only a handful of pairs
    contribute, which is exactly where an unbiased estimator would feed noise into both
    the exponential fit and the integrated sum.
    """
    a = np.asarray(x, dtype=np.float64)
    n = a.size
    if n < 2:
        raise ValueError(f"need at least 2 points to compute an ACF, got {n}")
    centered = a - a.mean()
    denom = float(np.dot(centered, centered))
    if denom <= 0:
        raise ValueError(
            "throughput series has zero variance — the segment is either constant or too "
            "short to have resolved any drift, and tau is undefined for it"
        )
    limit = n - 1 if max_lag is None else min(max_lag, n - 1)
    return np.array(
        [float(np.dot(centered[: n - k], centered[k:]) / denom) for k in range(limit + 1)]
    )


def _integrated_tau(rho: np.ndarray, dt_s: float) -> float:
    """dt * (1 + 2*sum rho_k), truncated by Sokal's automatic window.

    Summing to the end of the ACF would add up hundreds of near-zero lags whose noise does
    not cancel. Stopping at C*tau_int is self-consistent: the window is chosen from the
    answer it is producing.
    """
    total = 1.0
    tau = dt_s
    for k in range(1, rho.size):
        total += 2.0 * float(rho[k])
        tau = dt_s * max(total, 1.0)
        if k >= _SOKAL_C * tau / dt_s:
            break
    return tau


def _e_folding(rho: np.ndarray, dt_s: float) -> float:
    """First 1/e crossing, linearly interpolated between the bracketing lags."""
    for k in range(1, rho.size):
        if rho[k] < _ONE_OVER_E:
            prev = float(rho[k - 1])
            frac = (prev - _ONE_OVER_E) / (prev - float(rho[k]))
            return dt_s * (k - 1 + frac)
    # Never decayed within the observed lags: the segment is shorter than tau, and saying
    # so is more useful than extrapolating a number off the end of the data.
    return dt_s * (rho.size - 1)


def _censored(rho: np.ndarray, dt_s: float) -> AutocorrFit:
    """The no-decay outcome: tau is bounded above by one window, and nothing is fitted."""
    return AutocorrFit(
        tau_s=dt_s,
        fit_r2=0.0,
        integrated_tau_s=_integrated_tau(rho, dt_s),
        e_folding_s=_e_folding(rho, dt_s),
        lags_fitted=0,
        acf=rho,
        censored=True,
    )


def fit_autocorr_time(rho: np.ndarray, dt_s: float) -> AutocorrFit:
    """Fit rho(k) = exp(-k*dt/tau) over the leading decade of the ACF.

    Two truncations, and the second one is not obvious enough to leave implicit.

    Lags past the first non-positive value are excluded because the correlation there has
    decayed into noise and its logarithm is either undefined or an artifact.

    Lags past the **1/e crossing** are excluded as well, and this is what the estimator
    stands on. Fitting the whole positive run sounds more thorough and measurably is not:
    the biased ACF carries a `(1 - k/n)` damping that steepens the log-linear slope, and
    the far tail is estimated from few pairs, so the extra lags contribute bias and
    variance rather than information. Measured against AR(1) series with a known tau, over
    25 seeds at n=2000, fitting the full positive run gives a median tau of 46 against a
    true 60 with an inter-quartile range of [33, 55]; stopping at the 1/e crossing gives a
    median of 54 with an IQR of [44, 69]. Roughly half the spread, and centred. The same
    holds at tau = 5, 20 and 30.

    Stopping at 1/e is also the natural place: that crossing *is* the timescale being
    estimated, so the fit spans exactly the range that defines it and `e_folding_s` becomes
    a like-for-like cross-check rather than an independent guess.
    """
    positive = 1
    while positive < rho.size and rho[positive] > 0:
        positive += 1

    # First lag at or below 1/e, inclusive, so the crossing itself anchors the slope.
    cutoff = positive
    for k in range(1, positive):
        if rho[k] <= _ONE_OVER_E:
            cutoff = k + 1
            break
    # Never fit more than a quarter of the available lags: past that the ACF is estimated
    # from too few pairs to carry the weight the least-squares fit would give it.
    cutoff = min(cutoff, max(2, rho.size // 4))

    lags = np.arange(1, cutoff, dtype=np.float64)
    if lags.size < 2:
        # The correlation is gone by the first lag. There is nothing to fit, and inventing
        # a fit would be worse than reporting the bound.
        return _censored(rho, dt_s)

    x = lags * dt_s
    y = np.log(rho[1:cutoff])
    # Through the origin: rho(0) = 1 is not an observation to be fitted, it is the
    # definition of the ACF, and letting the fit choose an intercept lets it disown that.
    slope = float(np.dot(x, y) / np.dot(x, x))
    if slope >= 0:
        return _censored(rho, dt_s)
    tau = -1.0 / slope

    resid = y - slope * x
    ss_res = float(np.dot(resid, resid))
    ss_tot = float(np.dot(y - y.mean(), y - y.mean()))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0

    return AutocorrFit(
        tau_s=tau,
        fit_r2=r2,
        integrated_tau_s=_integrated_tau(rho, dt_s),
        e_folding_s=_e_folding(rho, dt_s),
        lags_fitted=int(lags.size),
        acf=rho,
    )


def variance_envelope(series: ThroughputSeries) -> VarianceEnvelope:
    """The observed band, as percentiles of the windowed throughput series."""
    v = series.values
    mean = float(v.mean())
    sd = float(v.std(ddof=1)) if v.size > 1 else 0.0
    p05, p50, p95 = (float(q) for q in np.percentile(v, [5, 50, 95]))
    return VarianceEnvelope(
        mean_tok_s=mean,
        sd_tok_s=sd,
        cv=sd / mean if mean > 0 else 0.0,
        p05_tok_s=p05,
        p50_tok_s=p50,
        p95_tok_s=p95,
        min_tok_s=float(v.min()),
        max_tok_s=float(v.max()),
    )


def lognormal_sigma(
    observed: np.ndarray | list[float], predicted: np.ndarray | list[float]
) -> float:
    """sigma of the multiplicative residual, for C-3's `lognormal_multiplier` (F-22).

    The DES model is `service = predicted * exp(N(-sigma^2/2, sigma^2))`, whose multiplier
    has mean 1 — so the stochastic component adds variance without shifting the mean, and
    the simulator does not end up systematically slower than the hardware it was fitted
    to. What is returned here is the standard deviation of `log(observed/predicted)`;
    applying the -sigma^2/2 correction is the simulator's job, and is noted here because
    that division of labour is exactly the kind of thing that gets lost across a seam.
    """
    obs = np.asarray(observed, dtype=np.float64)
    pred = np.asarray(predicted, dtype=np.float64)
    if obs.size != pred.size:
        raise ValueError("observed and predicted must be the same length")
    keep = (obs > 0) & (pred > 0)
    ratios = obs[keep] / pred[keep]
    if ratios.size < 2:
        raise ValueError("need at least 2 positive (observed, predicted) pairs to fit sigma")
    return float(np.log(ratios).std(ddof=1))


def characterize(
    t_end_ns: np.ndarray | list[int],
    decode_ns: np.ndarray | list[int],
    output_tokens: np.ndarray | list[int],
    *,
    node_class: str,
    window_s: float,
    sigma: float,
    batch_size: int = 1,
    max_lag: int | None = None,
) -> StationarityReport:
    """The whole MPR-1 pipeline for one sustained-load segment.

    `sigma` is passed in rather than computed here because it is a property of the *cost
    model's* residuals — service time against what the lookup table predicted — and the
    lookup table is fitted from the whole grid, not from this one segment.

    `batch_size` is the segment's concurrency — llama.cpp's `--parallel` in practice. It
    is not used to estimate anything; it is used to decide whether the estimate is
    trustworthy, via `cadence_limited`.
    """
    series = windowed_throughput(t_end_ns, decode_ns, output_tokens, window_s=window_s)
    if len(series) < MIN_WINDOWS:
        raise ValueError(
            f"{len(series)} windows of {window_s:g}s is under the {MIN_WINDOWS}-window floor "
            "for an autocorrelation estimate — run a longer sustained segment, or shrink "
            "window_s if the segment is already long and tau is short"
        )
    rho = acf(series.values, max_lag=max_lag)
    # The floor is one request, not one window: below that the series cannot carry
    # information about the node that is not an artifact of how I binned it.
    floor_s = float(np.median(np.asarray(decode_ns, dtype=np.float64))) / 1e9
    return StationarityReport(
        node_class=node_class,
        fit=fit_autocorr_time(rho, series.dt_s),
        envelope=variance_envelope(series),
        window_s=window_s,
        n_windows=len(series),
        n_samples=int(np.asarray(t_end_ns).size),
        sigma=sigma,
        resolution_floor_s=max(floor_s, window_s),
        batch_size=batch_size,
    )
