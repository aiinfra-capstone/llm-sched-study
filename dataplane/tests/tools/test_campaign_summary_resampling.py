"""tools/campaign_summary.py: block length, the autocorrelation estimate and the steady-state gate.

An interval from a bootstrap that resamples autocorrelated latencies one request at a time
is too narrow, and a too-narrow interval is how a noise difference gets written up as a
finding. These tests pin how long the blocks are, which cells are allowed to set that
length, and when a cell counts as still climbing.
"""

from __future__ import annotations

import itertools
import math
from types import SimpleNamespace

import campaign_summary as cs
import numpy as np
import pytest
from support import cell_rows, frame


def _ar1(n: int, phi: float, seed: int, loc: float = 1000.0, scale: float = 50.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = rng.normal(0, scale / math.sqrt(1 - phi**2))
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal(0, scale)
    return loc + x


def _iid(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(1000.0, 50.0, n)


def _block(series_by_policy: dict[str, np.ndarray], n_boot: int = 50) -> int:
    data = frame(*(cell_rows(p, v) for p, v in series_by_policy.items()))
    return cs.summarise(data, n_boot, 3)["points"][0]["block_length_requests"]


# ------------------------------------------------------------------------ block length


def test_iid_latencies_get_the_cube_root_block() -> None:
    assert _block({"round_robin": _iid(570, 1), "jsq": _iid(570, 2)}) == 9


def test_autocorrelated_latencies_get_twice_tau() -> None:
    series = {"round_robin": _ar1(570, 0.95, 3), "jsq": _ar1(570, 0.9, 4)}
    tau = max(cs.integrated_tau(v) for v in series.values())
    assert 2 * tau > 9, "the fixture has to be correlated enough for tau to set the block"
    assert _block(series) == math.ceil(2 * tau)


def test_the_block_is_capped_at_a_fifth_of_the_window(monkeypatch) -> None:
    """The Sokal window stops the estimate near n/10 on any real series, so the cap is
    pinned with a stubbed estimate rather than a fixture that happens to reach it."""
    monkeypatch.setattr(cs, "integrated_tau", lambda x: 500.0)
    assert _block({"round_robin": _iid(100, 5)}) == 20
    assert _block({"round_robin": _iid(4, 5)}) == 1


def test_a_climbing_cell_does_not_set_the_block_for_the_level_ones() -> None:
    level = {"round_robin": _iid(570, 6), "jsq": _iid(570, 7)}
    ramp = np.linspace(1000.0, 3000.0, 570) + _ar1(570, 0.99, 8, loc=0.0, scale=40.0)
    climbing = {**level, "static_weighted": ramp}
    assert cs.third_ratio(SimpleNamespace(e2e=ramp[None, :])) >= cs.CLIMB_RATIO
    assert math.ceil(2 * cs.integrated_tau(ramp)) > 9
    assert _block(climbing) == _block(level) == 9


def test_integrated_tau_recovers_a_known_ar1_time() -> None:
    """For AR(1), tau_int = (1 + phi) / (1 - phi): 9 at phi = 0.8."""
    assert cs.integrated_tau(_ar1(40_000, 0.8, 9)) == pytest.approx(9.0, rel=0.15)
    assert cs.integrated_tau(_iid(5_000, 10)) == pytest.approx(1.0, abs=0.25)


def test_integrated_tau_removes_a_linear_trend_first() -> None:
    trend = np.linspace(0.0, 5000.0, 2000) + _iid(2000, 11)
    assert cs.integrated_tau(trend) < 1.5


def test_integrated_tau_of_a_short_zero_or_anticorrelated_series_is_one() -> None:
    assert cs.integrated_tau(np.array([1.0, 2.0, np.nan])) == 1.0
    assert cs.integrated_tau(np.zeros(20)) == 1.0
    assert cs.integrated_tau(np.tile([1.0, -1.0], 50)) == 1.0


def test_integrated_tau_of_a_constant_series_is_one() -> None:
    """The detrended residual of a constant is zero, and the guard says tau is then 1. The
    least-squares fit leaves residuals near 1e-13 instead, which slip past `denom <= 0`."""
    assert cs.integrated_tau(np.full(50, 7.0)) == 1.0


def test_lag1_is_the_mean_centred_autocorrelation() -> None:
    assert cs.lag1(_ar1(20_000, 0.7, 12)) == pytest.approx(0.7, abs=0.03)
    assert math.isnan(cs.lag1(np.array([1.0, 2.0])))
    assert math.isnan(cs.lag1(np.full(10, 3.0)))


def _plain_iid_ci_width(x: np.ndarray, draws: int, seed: int) -> float:
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, x.size, size=(draws, x.size))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(hi - lo)


def _summary_ci_width(x: np.ndarray, draws: int) -> float:
    s = cs.summarise(frame(cell_rows("round_robin", x)), draws, 13)
    lo, hi = s["points"][0]["policies"]["round_robin"]["mean"]["ci95"]
    return hi - lo


def test_blocks_widen_the_interval_on_correlated_data_and_not_on_iid() -> None:
    ar = _ar1(570, 0.9, 14)
    iid = _iid(570, 15)
    assert _summary_ci_width(ar, 2000) > 2.0 * _plain_iid_ci_width(ar, 2000, 16)
    assert _summary_ci_width(iid, 2000) == pytest.approx(
        _plain_iid_ci_width(iid, 2000, 17), rel=0.3
    )


def test_block_positions_are_contiguous_runs_that_wrap() -> None:
    idx = cs.block_positions(np.random.default_rng(0), draws=40, n=10, block=4)
    assert idx.shape == (40, 10)
    for row in idx:
        for start in (0, 4):
            run = row[start : start + 4]
            assert all((b - a) % 10 == 1 for a, b in itertools.pairwise(run))


# ---------------------------------------------------------------------- the climb gate


def _cell(*repeats: np.ndarray) -> SimpleNamespace:
    return SimpleNamespace(e2e=np.vstack(repeats), repeats=list(range(1, len(repeats) + 1)))


def _thirds(first: float, middle: float, last: float, n: int = 90) -> np.ndarray:
    third = n // 3
    return np.concatenate([np.full(third, first), np.full(third, middle), np.full(third, last)])


def _climb(c: SimpleNamespace, block: int = 3) -> dict:
    return cs.climb(c, block, 1000, np.random.default_rng(21))


def test_a_flat_series_is_not_transient() -> None:
    gate = _climb(_cell(_iid(90, 22), _iid(90, 23)))
    assert gate["transient"] is False


def test_a_quiet_ramp_to_one_point_two_is_transient() -> None:
    noise = np.random.default_rng(24).normal(0, 5.0, 90)
    gate = _climb(_cell(_thirds(1000, 1100, 1200) + noise))
    assert gate["last_over_first_third"] == pytest.approx(1.2, abs=0.01)
    assert gate["ci95"][0] > 1.0
    assert gate["transient"] is True


def test_a_rise_the_noise_cannot_separate_from_one_is_not_transient() -> None:
    """Third means are exactly 1000 and 1150 (the noise is +-900 in equal numbers inside each
    third), so the ratio is 1.15, but the interval reaches below 1."""
    swing = np.tile([900.0, -900.0], 45)
    gate = _climb(_cell(_thirds(1000, 1075, 1150) + swing), block=1)
    assert gate["last_over_first_third"] == pytest.approx(1.15)
    assert gate["ci95"][0] <= 1.0
    assert gate["transient"] is False


def test_exactly_the_threshold_with_an_interval_above_one_is_transient() -> None:
    gate = _climb(_cell(_thirds(100.0, 105.0, 110.0)))
    assert gate["last_over_first_third"] == 1.1
    assert gate["ci95"] == [1.1, 1.1]
    assert gate["transient"] is True


def test_a_falling_series_is_not_transient() -> None:
    gate = _climb(_cell(_thirds(1200, 1100, 1000)))
    assert gate["last_over_first_third"] < 1
    assert gate["transient"] is False
    assert gate["rise_ms"] == -200.0


# ------------------------------------------------------------------- small helpers


def test_interval_of_nothing_is_nan_and_rounding_keeps_none() -> None:
    lo, hi = cs.interval(np.array([np.nan, np.nan]))
    assert math.isnan(lo) and math.isnan(hi)
    assert cs.interval(np.arange(1.0, 101.0)) == pytest.approx([3.475, 97.525])
    assert cs.r1(None) is None and cs.r1(float("nan")) is None and cs.r1(2.345) == 2.3
    assert cs.r4(None) is None and cs.r4(float("inf")) is None and cs.r4(0.123456) == 0.1235


def test_repeat_of_reads_the_suffix_and_defaults_to_one() -> None:
    assert cs.repeat_of("t_jsq_s0_heavy_r12") == 12
    assert cs.repeat_of("anchor1b_heavy_1788376820") == 1
