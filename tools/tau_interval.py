#!/usr/bin/env python3
"""Report tau with an interval, with and without a linear detrend, from a calibration's log.

`calibrate` reports tau as a point estimate. On the CPU 8B class that point is 69.5 s from 31
windows of 48 s, with `tau_resolved: false` in the same record, and it was written up as
resolved. At that window length tau = 69.5 s is a lag-1 correlation of about 0.5, and a
lag-1 correlation from 31 points has a standard error near 0.18. The exponential fit rests
on two or three lags, and the ACF is mean-centred with no detrend, so a single slow thermal
ramp across the segment produces the same shape as a stationary process with a long tau.

This re-derives tau from the sustained segment in `observations.jsonl` four ways and puts a
moving-block bootstrap interval on each:

  raw          the calibration's own estimator (stationarity.fit_autocorr_time)
  detrended    the same after removing a least-squares line from the windowed series
  lag-1        rho(1) itself, the number the fit mostly stands on
  n/tau        how many tau the segment spans; below about 40 a tau estimate is not stable

The bootstrap resamples blocks of windows (block length ceil(n^(1/3)), at least 2) and
refits. A draw whose ACF shows no decay counts as censored at one window.

Usage:
  uv run --project dataplane python tools/tau_interval.py \\
      runs/calibration/llama3-8b/cal_cpu_ngl0_p4_q4km_llama3_8b_1788099741 --out <file>.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from dataplane.calibration.stationarity import (
    MIN_WINDOWS,
    acf,
    fit_autocorr_time,
    windowed_throughput,
)

# Block length as a multiple of the fitted tau. Ten keeps the join bias at a few percent
# while still leaving enough blocks for the interval to have any width. Measured against AR(1)
# series with a known tau: coverage of a 95% interval goes 6 of 20 at the n^(1/3) rule, 15 at
# ten tau per block and 17 at twenty-five.
BLOCKS_PER_TAU = 25.0


def fit(values: np.ndarray, dt: float) -> tuple[float, bool, float]:
    rho = acf(values)
    f = fit_autocorr_time(rho, dt)
    return f.tau_s, f.censored, float(rho[1]) if rho.size > 1 else float("nan")


def detrend(values: np.ndarray) -> np.ndarray:
    t = np.arange(values.size, dtype=float)
    slope, intercept = np.polyfit(t, values, 1)
    return values - (slope * t + intercept) + values.mean()


def block_length(values: np.ndarray, dt_s: float) -> int:
    """Blocks long enough to carry the correlation the estimator is about to measure.

    A moving block bootstrap breaks the series at every join, so a fraction of about 1/L of
    the lag-1 pairs in a resampled series are junk. That biases rho(1), and therefore tau,
    downwards by roughly the same fraction. The usual n^(1/3) rule is chosen for estimating
    a mean and is far too short here: at n = 400 it gives 8, and an AR(1) with tau = 2.8 s
    comes back near 2.0 s with an interval that misses the truth four times in five.

    So the block is set from the quantity being estimated: BLOCKS_PER_TAU times the fitted
    tau, floored at 2 and capped at a quarter of the series so a draw still holds four
    blocks. The residual bias is about 1/L, which at twenty-five tau per block is a few percent.
    """
    tau, _, _ = fit(values, dt_s)
    in_samples = max(tau / dt_s, 1.0)
    return int(min(max(2, math.ceil(BLOCKS_PER_TAU * in_samples)), max(2, values.size // 4)))


def block_boot(values: np.ndarray, rng: np.random.Generator, draws: int, block: int) -> np.ndarray:
    n = values.size
    out = np.empty((draws, n))
    for i in range(draws):
        starts = rng.integers(0, n - block + 1, size=math.ceil(n / block))
        out[i] = np.concatenate([values[s : s + block] for s in starts])[:n]
    return out


def summarise(values: np.ndarray, dt: float, rng: np.random.Generator, draws: int) -> dict:
    if values.size < MIN_WINDOWS:
        raise ValueError(
            f"{values.size} windows is under the {MIN_WINDOWS}-window floor for an "
            "autocorrelation estimate: the leading lags would be dominated by sampling "
            "noise and the fit would be confidently wrong. Run a longer segment, or shrink "
            "the window if the segment is already long"
        )
    tau, censored, r1 = fit(values, dt)
    block = block_length(values, dt)
    taus, lag1s, cens = [], [], 0
    for sample in block_boot(values, rng, draws, block):
        if np.ptp(sample) == 0:
            continue
        t, c, r = fit(sample, dt)
        taus.append(t)
        lag1s.append(r)
        cens += int(c)
    taus_a = np.asarray(taus)
    return {
        "tau_s": round(tau, 2),
        "censored": censored,
        "block_length_windows": block,
        "tau_ci95_s": [round(float(x), 2) for x in np.percentile(taus_a, [2.5, 97.5])],
        "lag1": round(r1, 3),
        "lag1_ci95": [round(float(x), 3) for x in np.percentile(lag1s, [2.5, 97.5])],
        "censored_share_of_draws": round(cens / max(len(taus), 1), 3),
        "segment_over_tau": round(values.size * dt / tau, 1) if tau > 0 else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path, help="a calibration run with observations.jsonl")
    ap.add_argument("--window-s", type=float, help="default: the calibration's own window")
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    campaign = json.loads((args.run_dir / "campaign.json").read_text())
    window = args.window_s or campaign["stationarity"]["window_s"]
    obs = [
        json.loads(line) for line in (args.run_dir / "observations.jsonl").read_text().splitlines()
    ]
    sus = [o for o in obs if o.get("segment") == "sustained" and o.get("status") == "ok"]
    series = windowed_throughput(
        [o["t_end_ns"] for o in sus],
        [o["decode_ns"] for o in sus],
        [o["output_tokens"] for o in sus],
        window_s=window,
    ).values
    rng = np.random.default_rng(args.seed)
    t = np.arange(series.size, dtype=float) * window
    slope = float(np.polyfit(t, series, 1)[0])
    report = {
        "run_id": campaign["run_id"],
        "node_class": campaign["node_class"],
        "window_s": window,
        "n_windows": int(series.size),
        "segment_s": round(series.size * window, 1),
        "calibration_reported": {
            k: campaign["stationarity"][k]
            for k in ("autocorr_time_s", "tau_resolved", "n_eff", "fit_r2")
        },
        "linear_trend_tok_s_per_min": round(slope * 60, 4),
        "linear_trend_percent_over_segment": round(
            100 * slope * series.size * window / float(series.mean()), 2
        ),
        "raw": summarise(series, window, rng, args.draws),
        "detrended": summarise(detrend(series), window, rng, args.draws),
    }
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
