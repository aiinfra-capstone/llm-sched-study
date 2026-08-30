"""MPR-1's estimator, checked against processes whose tau I already know.

This is the file I would point a reviewer at first. MPR-1's headline is a number — the
autocorrelation time of a real node — and a number is only worth reporting if the
estimator that produced it can recover a known answer. So the central test here
synthesizes an AR(1) throughput process with a *chosen* tau, samples it the way real
requests sample a real node, and asserts the pipeline gets it back.

The rest of the file is about the ways this measurement can lie:

  * binning shorter than a request induces correlation that is mine, not the node's, so
    `resolution_floor_s` exists and `tau_resolved` has to go false below it;
  * a handful of windows produces a confident tau fitted to noise, so `MIN_WINDOWS`
    refuses rather than reports;
  * a flat segment has no tau at all, and saying so beats returning zero.
"""

from __future__ import annotations

import numpy as np
import pytest

from dataplane.calibration import stationarity as st


def _ar1_requests(tau_true: float, *, n_req: int, req_s: float = 0.2, seed: int = 0):
    """Requests sampling a node whose decode rate is AR(1) with autocorrelation `tau_true`.

    This is how a node is actually observed: not as a continuous signal, but as a sequence
    of completions each reporting the rate that held while it ran. `req_s` is the mean
    request duration, and the token count is derived from it so that the AR step and the
    completion interval are the same thing — otherwise the fixture would carry a second,
    invisible timescale.

    The default is deliberately short. A fixture with one completion per window would be
    cadence-limited by the same rule that rejects a real segment of that shape, and a test
    suite that exempts itself from its own guard is not testing the guard.
    """
    rng = np.random.default_rng(seed)
    tokens = max(1, round(100.0 * req_s))
    phi = float(np.exp(-req_s / tau_true))
    x, now = 0.0, 0.0
    t_end, decode_ns, out = [], [], []
    for _ in range(n_req):
        x = phi * x + np.sqrt(1 - phi**2) * rng.normal()
        dur = tokens / (100.0 * (1 + 0.12 * x))
        now += dur
        t_end.append(int(now * 1e9))
        decode_ns.append(int(dur * 1e9))
        out.append(tokens)
    return t_end, decode_ns, out


@pytest.mark.parametrize("tau_true", [5.0, 20.0])
def test_recovers_a_known_autocorrelation_time(tau_true: float) -> None:
    """The whole point. Within a factor of 1.6 of truth, on 1200 requests.

    The tolerance is loose on purpose: tau is a timescale, and the claim MPR-1 makes is
    about its order of magnitude relative to a heartbeat interval, not its third digit.
    A tighter bound here would be a test of this seed rather than of the estimator.
    """
    t, d, o = _ar1_requests(tau_true, n_req=4000)
    report = st.characterize(t, d, o, node_class="synthetic", window_s=2.0, sigma=0.1)

    assert tau_true / 1.6 <= report.fit.tau_s <= tau_true * 1.6
    assert tau_true / 1.6 <= report.fit.e_folding_s <= tau_true * 1.6
    assert report.fit.fit_r2 > 0.5
    assert report.fit.agrees
    assert report.tau_resolved
    assert not report.cadence_limited


def test_a_stationary_node_reports_a_bound_not_a_tau() -> None:
    """A node that does not drift must not be handed a tau, and must not fail either.

    "No resolvable autocorrelation at this operating point" is an MPR-1 finding — it says
    the drift H3 is built on is absent here — so it comes back as a censored upper bound
    with the whole report intact, not as an exception that costs the campaign its samples.
    """
    t, d, o = _ar1_requests(0.02, n_req=3000)
    report = st.characterize(t, d, o, node_class="flat", window_s=2.0, sigma=0.1)

    assert report.fit.censored
    assert report.fit.tau_s == pytest.approx(2.0)  # one window: the tightest bound
    assert report.fit.lags_fitted == 0
    assert not report.tau_resolved
    assert report.to_dict()["tau_censored"] is True
    assert report.to_dict()["tau_resolved"] is False


def test_resolution_floor_is_the_larger_of_a_request_and_a_window() -> None:
    t, d, o = _ar1_requests(20.0, n_req=3000)
    coarse = st.characterize(t, d, o, node_class="n", window_s=5.0, sigma=0.1)
    assert coarse.resolution_floor_s == pytest.approx(5.0)


def test_too_few_windows_refuses_rather_than_reporting_noise() -> None:
    t, d, o = _ar1_requests(20.0, n_req=100)
    with pytest.raises(ValueError, match="under the 30-window floor"):
        st.characterize(t, d, o, node_class="n", window_s=5.0, sigma=0.1)


def test_windowed_throughput_credits_tokens_across_the_decode_interval() -> None:
    """One request decoding 100 tokens over 4s at a steady rate is 25 tok/s in each second.

    Attributing all 100 tokens to the completion stamp would instead read as one 100 tok/s
    window flanked by three empty ones — a spike this module would then measure the
    autocorrelation of.
    """
    series = st.windowed_throughput([4_000_000_000], [4_000_000_000], [100], window_s=1.0, t0_ns=0)
    assert len(series) == 4
    assert series.values == pytest.approx([25.0, 25.0, 25.0, 25.0])
    assert series.duration_s == pytest.approx(4.0)


def test_windowed_throughput_drops_requests_that_carry_no_information() -> None:
    """A timeout produced no tokens over no time; averaging it in would invent a zero."""
    series = st.windowed_throughput(
        [4_000_000_000, 4_000_000_000], [4_000_000_000, 0], [100, 0], window_s=1.0, t0_ns=0
    )
    assert series.values == pytest.approx([25.0, 25.0, 25.0, 25.0])


def test_windowed_throughput_rejects_inputs_it_cannot_interpret() -> None:
    with pytest.raises(ValueError, match="same length"):
        st.windowed_throughput([1, 2], [1], [1], window_s=1.0)
    with pytest.raises(ValueError, match="window_s must be > 0"):
        st.windowed_throughput([1], [1], [1], window_s=0.0)
    with pytest.raises(ValueError, match="no usable samples"):
        st.windowed_throughput([10], [0], [0], window_s=1.0)
    with pytest.raises(ValueError, match="under two"):
        st.windowed_throughput([1_000_000_000], [1_000_000_000], [10], window_s=5.0)


def test_acf_is_normalized_and_refuses_a_degenerate_series() -> None:
    rho = st.acf([1.0, 2.0, 3.0, 4.0, 5.0], max_lag=2)
    assert rho[0] == pytest.approx(1.0)
    assert rho.size == 3

    with pytest.raises(ValueError, match="at least 2 points"):
        st.acf([1.0])
    with pytest.raises(ValueError, match="zero variance"):
        st.acf([3.0, 3.0, 3.0, 3.0])


def test_an_acf_with_no_decay_is_censored_rather_than_fitted() -> None:
    """Both no-decay shapes: correlation gone by lag 1, and correlation that never falls."""
    # Correlation gone by the first lag: nothing to fit at all.
    gone = st.fit_autocorr_time(np.array([1.0, -0.2, -0.3]), 1.0)
    # Correlation that never falls: enough lags to fit, and a slope that says the segment
    # never relaxed. Long enough that the quarter-of-the-lags clamp still leaves two.
    flat = st.fit_autocorr_time(np.ones(12), 1.0)

    assert flat.lags_fitted == 0
    for fit in (gone, flat):
        assert fit.censored and fit.tau_s == pytest.approx(1.0) and fit.fit_r2 == 0.0


def test_e_folding_reports_the_observed_span_when_the_acf_never_decays_that_far() -> None:
    """Extrapolating a crossing off the end of the data would be inventing one."""
    fit = st.fit_autocorr_time(np.array([1.0, 0.95, 0.9, 0.85]), 2.0)
    assert fit.e_folding_s == pytest.approx(6.0)


def test_agreement_flag_catches_two_estimators_telling_different_stories() -> None:
    close = st.AutocorrFit(
        tau_s=10.0,
        fit_r2=0.9,
        integrated_tau_s=15.0,
        e_folding_s=9.0,
        lags_fitted=5,
        acf=np.array([1.0]),
    )
    far = st.AutocorrFit(
        tau_s=10.0,
        fit_r2=0.9,
        integrated_tau_s=90.0,
        e_folding_s=9.0,
        lags_fitted=5,
        acf=np.array([1.0]),
    )
    assert close.agrees and not far.agrees


def test_variance_envelope_is_reported_as_a_band_not_a_sigma() -> None:
    series = st.ThroughputSeries(
        dt_s=1.0, values=np.array([80.0, 90.0, 100.0, 110.0, 120.0]), t0_ns=0
    )
    env = st.variance_envelope(series)

    assert env.p50_tok_s == pytest.approx(100.0)
    assert env.mean_tok_s == pytest.approx(100.0)
    assert env.band_ratio > 1.0
    assert env.to_dict()["band_ratio"] == pytest.approx(env.band_ratio, rel=1e-3)


def test_variance_envelope_survives_a_floor_of_zero() -> None:
    """p05 of zero makes the band unbounded; that is reported, not divided by."""
    series = st.ThroughputSeries(dt_s=1.0, values=np.array([0.0, 0.0, 0.0, 50.0]), t0_ns=0)
    env = st.variance_envelope(series)
    assert env.band_ratio == float("inf")

    single = st.ThroughputSeries(dt_s=1.0, values=np.array([0.0]), t0_ns=0)
    assert st.variance_envelope(single).cv == 0.0


def test_lognormal_sigma_is_the_spread_of_the_multiplicative_residual() -> None:
    """F-22's noise term is a multiplier, so its sigma lives in log space."""
    predicted = [100.0] * 200
    rng = np.random.default_rng(7)
    observed = list(100.0 * np.exp(rng.normal(0, 0.25, 200)))
    assert st.lognormal_sigma(observed, predicted) == pytest.approx(0.25, abs=0.04)

    with pytest.raises(ValueError, match="same length"):
        st.lognormal_sigma([1.0, 2.0], [1.0])
    with pytest.raises(ValueError, match="at least 2 positive"):
        st.lognormal_sigma([1.0, 0.0], [1.0, 0.0])


def test_effective_sample_size_is_the_mpr1_argument() -> None:
    """A calibration mean is not sqrt(n) precise once the samples are correlated.

    This is the number that turns "throughput drifts" into "and here is what believing a
    single calibrated figure costs you".
    """
    t, d, o = _ar1_requests(20.0, n_req=4000)
    report = st.characterize(t, d, o, node_class="n", window_s=2.0, sigma=0.1)

    assert report.n_eff < report.n_windows
    assert report.se_inflation > 1.0
    assert report.to_dict()["se_inflation"] == pytest.approx(report.se_inflation, rel=1e-3)
    assert report.to_dict()["node_class"] == "n"


def test_integrated_tau_stops_before_it_accumulates_noise() -> None:
    """Sokal windowing: a long ACF of pure noise must not sum into a huge tau."""
    rng = np.random.default_rng(3)
    rho = np.concatenate([[1.0, 0.6, 0.3, 0.1], rng.normal(0, 0.02, 400)])
    fit = st.fit_autocorr_time(rho, 1.0)
    assert fit.integrated_tau_s < 10.0


def test_a_sparse_completion_stream_is_refused_as_cadence_limited() -> None:
    """The failure that actually happened, kept as a test.

    llama.cpp finishes its `--parallel` slots as a batch, so completions arrive in bursts.
    A segment whose windows hold only a burst or two produces an ACF shaped by slot
    turnover, and the fit reads that rhythm as decay — on real hardware this gave tau
    "censored" at 6, 8 and 12 s windows and ~16 s at 10 and 15 s, from the same samples.
    A number that moves with the binning must not be reported as a measurement.
    """
    t, d, o = _ar1_requests(20.0, n_req=3000)
    # 10 completions per window, but 16 slots turning over together: under one burst per
    # window, so the series is a picture of the batch rhythm.
    sparse = st.characterize(t, d, o, node_class="n", window_s=2.0, sigma=0.1, batch_size=16)

    assert sparse.bursts_per_window < st.MIN_BURSTS_PER_WINDOW
    assert sparse.cadence_limited
    assert not sparse.tau_resolved
    assert sparse.to_dict()["cadence_limited"] is True
    assert sparse.to_dict()["batch_size"] == 16

    # Same samples, same windows, one slot: no burst structure to alias against.
    dense = st.characterize(t, d, o, node_class="n", window_s=2.0, sigma=0.1, batch_size=1)
    assert not dense.cadence_limited
    assert dense.tau_resolved
