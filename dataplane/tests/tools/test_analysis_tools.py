"""The four analysis scripts behind the audit's intervals: tau, cost-model cells, engine age, length mixes.

Each one produces a number the writing brief quotes with an interval. The tests put in a
series whose answer is known and check the script gets it, and that its interval covers it.
"""

from __future__ import annotations

import itertools
import json
import math
import os

import cell_intervals
import make_length_mix
import numpy as np
import pandas as pd
import pytest
import restart_effect
import tau_interval
from support import CONFIGS


def _ar1(n: int, phi: float, seed: int, loc: float = 100.0, scale: float = 5.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = rng.normal(0, scale / math.sqrt(1 - phi**2))
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal(0, scale)
    return loc + x


# --------------------------------------------------------------------------- tau_interval


def test_the_tau_interval_covers_a_known_ar1_tau() -> None:
    """For AR(1) sampled every second, rho(k) = phi^k, so tau = -1 / ln(phi): 2.80 s at 0.7.
    A 95% interval should cover it in at least 17 of 20 independent series."""
    true_tau = -1.0 / math.log(0.7)
    covered = 0
    for seed in range(20):
        report = tau_interval.summarise(_ar1(400, 0.7, seed), 1.0, np.random.default_rng(seed), 300)
        lo, hi = report["tau_ci95_s"]
        covered += lo <= true_tau <= hi
    assert covered >= 17, f"covered the true tau in {covered} of 20 series"


def test_the_tau_interval_contains_its_own_point_estimate() -> None:
    report = tau_interval.summarise(_ar1(400, 0.7, 3), 1.0, np.random.default_rng(3), 300)
    lo, hi = report["tau_ci95_s"]
    assert lo <= report["tau_s"] <= hi


def test_a_linear_trend_reads_as_a_long_tau_until_it_is_removed() -> None:
    """The M4 confound: a slow ramp plus white noise has no memory, but a mean-centred ACF
    sees one. Detrended, the same series is censored at the one-window floor."""
    t = np.arange(120.0)
    series = 100 + 0.5 * t + np.random.default_rng(1).normal(0, 3, 120)
    raw = tau_interval.summarise(series, 1.0, np.random.default_rng(1), 200)
    flat = tau_interval.summarise(tau_interval.detrend(series), 1.0, np.random.default_rng(1), 200)
    assert raw["tau_s"] > 10
    assert raw["lag1"] > 0.9
    assert flat["tau_s"] <= 1.0 and flat["censored"] is True
    assert tau_interval.detrend(series).mean() == pytest.approx(series.mean())


def test_a_bootstrap_draw_with_no_variation_is_skipped_not_fitted() -> None:
    """Level throughput with one spike at the end: a draw that misses the last block is
    constant and has no autocorrelation to fit."""
    series = np.full(64, 5.0)
    series[-1] = 9.0
    report = tau_interval.summarise(series, 1.0, np.random.default_rng(0), 200)
    assert len(report["tau_ci95_s"]) == 2
    assert 0.0 <= report["censored_share_of_draws"] <= 1.0


def test_a_series_too_short_to_fit_is_refused() -> None:
    with pytest.raises(ValueError):
        tau_interval.summarise(_ar1(5, 0.5, 1), 1.0, np.random.default_rng(1), 50)


def test_tau_interval_main_reads_a_calibration_run(tmp_path, capsys) -> None:
    run = tmp_path / "cal"
    run.mkdir()
    values = _ar1(120, 0.6, 7, loc=150.0, scale=4.0)
    obs = [
        {
            "segment": "sustained",
            "status": "ok",
            "t_end_ns": int((i + 1) * 1e9),
            "decode_ns": int(0.5e9),
            "output_tokens": max(1, int(v * 0.5)),
        }
        for i, v in enumerate(values)
    ]
    obs.append(
        {"segment": "grid", "status": "ok", "t_end_ns": 1, "decode_ns": 1, "output_tokens": 1}
    )
    obs.append(
        {
            "segment": "sustained",
            "status": "timeout",
            "t_end_ns": 1,
            "decode_ns": 0,
            "output_tokens": 0,
        }
    )
    (run / "observations.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
    stationarity = {
        "window_s": 1.0,
        "autocorr_time_s": 2.0,
        "tau_resolved": True,
        "n_eff": 40,
        "fit_r2": 0.9,
    }
    (run / "campaign.json").write_text(
        json.dumps({"run_id": "cal_x", "node_class": "nc", "stationarity": stationarity})
    )
    out = tmp_path / "tau.json"

    assert tau_interval.main([str(run), "--draws", "50", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["run_id"] == "cal_x" and report["window_s"] == 1.0
    assert report["calibration_reported"]["autocorr_time_s"] == 2.0
    assert {"raw", "detrended", "linear_trend_tok_s_per_min"} <= set(report)
    assert json.loads(capsys.readouterr().out) == report
    assert tau_interval.main([str(run), "--draws", "20", "--window-s", "2"]) == 0


# ------------------------------------------------------------------------- cell_intervals


def _grid_run(path, cells: dict[tuple[int, int, int], list[tuple[float, float, float, int]]]):
    """Observations for a calibration grid. Each sample is (service, prefill, decode) ms and tokens."""
    path.mkdir()
    lines = []
    for (p, o, c), samples in cells.items():
        for service, prefill, decode, toks in samples:
            lines.append(
                {
                    "segment": "grid",
                    "status": "ok",
                    "prompt_len": p,
                    "output_len": o,
                    "concurrency": c,
                    "service_ns": service * 1e6,
                    "prefill_ns": prefill * 1e6,
                    "decode_ns": decode * 1e6,
                    "output_tokens": toks,
                }
            )
    lines.append({"segment": "sustained", "status": "ok"})
    lines.append({"segment": "grid", "status": "timeout"})
    (path / "observations.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    return path


def test_a_batch_of_four_is_resampled_whole(tmp_path) -> None:
    """Two batches of four, all 100 in one and all 200 in the other. Any draw of whole
    batches has a mean of 100, 150 or 200; splitting a batch would give 125 or 175."""
    run = _grid_run(
        tmp_path / "g",
        {(128, 64, 4): [(100.0, 10.0, 90.0, 64)] * 4 + [(200.0, 20.0, 180.0, 64)] * 4},
    )
    cell = cell_intervals.grid(run)[(128, 64, 4)]
    assert cell.shape == (2, 4, 4)
    means = cell_intervals.draw_means(cell, np.random.default_rng(0), 500)[:, 0]
    assert set(np.round(means, 6)) <= {100.0, 150.0, 200.0}


def test_a_partial_batch_at_the_end_is_dropped(tmp_path) -> None:
    run = _grid_run(tmp_path / "g", {(128, 64, 4): [(100.0, 10.0, 90.0, 64)] * 6})
    assert cell_intervals.grid(run)[(128, 64, 4)].shape == (1, 4, 4)


def test_nearest_is_the_smallest_cell_that_covers_the_bucket() -> None:
    cells = {(128, 64, 1): 0, (512, 128, 1): 0, (256, 64, 1): 0, (128, 64, 4): 0}
    assert cell_intervals.nearest(cells, 100, 64, 1) == (128, 64, 1)
    assert cell_intervals.nearest(cells, 200, 32, 1) == (256, 64, 1)
    with pytest.raises(ValueError, match="no grid cell covers p1024_o64 at c=1"):
        cell_intervals.nearest(cells, 1024, 64, 1)


def test_deterministic_cells_give_zero_width_intervals(tmp_path, capsys) -> None:
    grid = {
        (128, 64, 1): [(400.0, 20.0, 370.0, 64)] * 8,
        (512, 128, 1): [(900.0, 80.0, 800.0, 128)] * 8,
        (128, 64, 4): [(600.0, 40.0, 540.0, 64)] * 8,
        (512, 128, 4): [(1500.0, 160.0, 1300.0, 128)] * 8,
    }
    slow = {k: [(2 * s, 8 * p, 1.2 * d, t) for s, p, d, t in v] for k, v in grid.items()}
    fast_dir = _grid_run(tmp_path / "fast", grid)
    slow_dir = _grid_run(tmp_path / "slow", slow)
    profile = tmp_path / "trace_mix.json"
    profile.write_text(
        json.dumps({"length_dist": {"buckets": ["p128_o64", "p512_o128"], "weights": [3, 1]}})
    )
    out = tmp_path / "cells.json"
    argv = ["--fast", str(fast_dir), "--slow", str(slow_dir), "--profile", str(profile)]
    assert cell_intervals.main([*argv, "--draws", "50", "--out", str(out)]) == 0

    report = json.loads(out.read_text())
    cap = report["capability"]
    assert cap["fast"]["tok_s_of_service"] == 160.0
    assert cap["fast"]["ci95"] == [160.0, 160.0]
    assert cap["slow"]["tok_s_of_service"] == 80.0
    assert cap["fast_over_slow"] == {"value": 2.0, "ci95": [2.0, 2.0]}
    by_c = report["profiles"][0]["by_concurrency"]
    assert set(by_c) == {"1", "4"}
    for c in by_c.values():
        assert c["R_service"]["value"] == 2.0 and c["R_service"]["ci95"] == [2.0, 2.0]
        assert c["R_prefill"]["value"] == 8.0 and c["R_prefill"]["ci95"] == [8.0, 8.0]
        assert c["R_decode"]["ci95"][0] == c["R_decode"]["ci95"][1] == 1.2
    assert json.loads(capsys.readouterr().out) == report
    assert cell_intervals.main([*argv, "--draws", "10"]) == 0


# ------------------------------------------------------------------------ restart_effect


def test_offset_s_parses_minutes_seconds_millis_micros() -> None:
    assert restart_effect.offset_s("1.02.003.004") == pytest.approx(62.003004)
    assert restart_effect.offset_s("0.00.000.001") == pytest.approx(1e-6)


@pytest.mark.parametrize("stamp", ["1.02.003", "a.b.c.d", "1.02.003.004.005", ""])
def test_offset_s_refuses_a_malformed_stamp(stamp) -> None:
    with pytest.raises(ValueError):
        restart_effect.offset_s(stamp)


def _engine_log(path, last_offset: str, mtime: float) -> None:
    path.write_text(
        "build: 10569 (1a2b3c4)\n"
        "0.00.012.000 I main: loading model\n"
        f"{last_offset} I srv  update_slots: all slots idle\n"
        "trailing text without a stamp\n"
    )
    os.utime(path, (mtime, mtime))


def test_engine_spans_start_at_mtime_minus_the_last_offset(tmp_path) -> None:
    _engine_log(tmp_path / "llama-server-b.log", "10.00.000.000", 20_000.0)
    _engine_log(tmp_path / "llama-server-a.log", "1.00.500.000", 5_000.0)
    (tmp_path / "llama-server-empty.log").write_text("no stamps here\n")
    (tmp_path / "other.log").write_text("0.00.001.000 I x\n")
    assert restart_effect.engine_spans(tmp_path) == [
        (5_000.0 - 60.5, 5_000.0, "llama-server-a.log"),
        (20_000.0 - 600.0, 20_000.0, "llama-server-b.log"),
    ]


def _runset(root, name: str, started: list[float], service: list[float], node: str = "rtx3050"):
    rs = root / name
    rs.mkdir(parents=True)
    rows = []
    for k, (t, s) in enumerate(zip(started, service, strict=True)):
        rid = f"{name}_jsq_s0_p_r{k + 1}"
        (rs / rid).mkdir()
        (rs / rid / "manifest.json").write_text(
            json.dumps({"run_id": rid, "started_unix": t, "policy": "jsq", "lambda": 2.0})
        )
        for i in range(4):
            rows.append(
                {
                    "run_id": rid,
                    "is_warmup": i == 0,
                    "status": "ok",
                    "chosen_node": node,
                    "service_ms": s,
                }
            )
        rows.append(
            {
                "run_id": rid,
                "is_warmup": False,
                "status": "ok",
                "chosen_node": "other",
                "service_ms": 1.0,
            }
        )
    pd.DataFrame(rows).to_parquet(rs / "runset.parquet")
    return rs


def _restart_report(tmp_path, service_for_hour, capsys, draws: int = 200) -> dict:
    logs = tmp_path / "logs"
    logs.mkdir()
    start = 1_800_000_000.0
    _engine_log(logs / "llama-server-1.log", "600.00.000.000", start + 36_000.0)  # up for 10 h
    hours = [0.5, 1.5, 3.0, 4.5, 6.0, 8.0]
    started = [start + h * 3600 for h in hours] + [start + 99 * 3600]  # the last is outside
    service = [service_for_hour(h) for h in hours] + [1.0]
    rs = _runset(tmp_path, "set", started, service)
    out = tmp_path / "restart.json"
    argv = [str(rs), "--engine-logs", str(logs), "--node", "rtx3050", "--draws", str(draws)]
    assert restart_effect.main([*argv, "--out", str(out)]) == 0
    capsys.readouterr()
    return json.loads(out.read_text())


def test_restart_effect_recovers_a_known_slope(tmp_path, capsys) -> None:
    slope = 0.02  # log service per hour
    report = _restart_report(tmp_path, lambda h: 400.0 * math.exp(slope * h), capsys)
    assert report["runs_placed"] == 6
    assert report["slope_log_service_per_hour"] == pytest.approx(slope, abs=1e-5)
    assert report["percent_per_hour"] == pytest.approx(100 * (math.exp(slope) - 1), abs=0.01)
    lo, hi = report["slope_ci95"]
    assert lo <= slope <= hi
    assert report["hours_since_restart_range"] == [0.5, 8.0]


def test_restart_effect_is_flat_on_flat_data(tmp_path, capsys) -> None:
    report = _restart_report(tmp_path, lambda h: 400.0, capsys)
    assert report["slope_log_service_per_hour"] == 0.0
    assert report["slope_ci95"] == [0.0, 0.0]


def test_restart_effect_with_no_run_inside_an_engine_lifetime_says_so(tmp_path, capsys) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    _engine_log(logs / "llama-server-1.log", "1.00.000.000", 1000.0)
    rs = _runset(tmp_path, "set", [5_000.0], [400.0])
    assert restart_effect.main([str(rs), "--engine-logs", str(logs), "--node", "rtx3050"]) == 1
    assert "no run falls inside a logged engine lifetime" in capsys.readouterr().out


def test_restart_effect_prints_without_an_out_file(tmp_path, capsys) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    _engine_log(logs / "llama-server-1.log", "600.00.000.000", 1_000_036_000.0)
    rs = _runset(tmp_path, "set", [1_000_003_600.0, 1_000_007_200.0], [400.0, 404.0])
    assert (
        restart_effect.main(
            [str(rs), "--engine-logs", str(logs), "--node", "rtx3050", "--draws", "20"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["engines"] == 1


# ----------------------------------------------------------------------- make_length_mix


@pytest.mark.parametrize(
    ("grid", "median", "sigma"),
    [(make_length_mix.OUTPUT_GRID, 32, 0.9), (make_length_mix.PROMPT_GRID, 160, 0.8)],
)
def test_lognormal_weights_sum_to_one_and_the_ends_take_the_tails(grid, median, sigma) -> None:
    w = make_length_mix.lognormal_weights(grid, median, sigma)
    assert w.sum() == pytest.approx(1.0)
    mu = math.log(median)

    def cdf(x):
        return 0.5 * (1 + math.erf((math.log(x) - mu) / (sigma * math.sqrt(2))))

    assert w[0] == pytest.approx(cdf(math.sqrt(grid[0] * grid[1])))
    assert w[-1] == pytest.approx(1 - cdf(math.sqrt(grid[-2] * grid[-1])))


@pytest.mark.parametrize(
    ("grid", "median", "sigma"),
    [(make_length_mix.OUTPUT_GRID, 32, 0.9), (make_length_mix.PROMPT_GRID, 160, 0.8)],
)
def test_the_densest_interior_cell_is_the_one_holding_the_median(grid, median, sigma) -> None:
    """Mass per unit of log length, not raw mass: the grid is not evenly spaced in log, so
    a wide cell next to the median (16, spanning 11.3 to 19.6) can carry more raw mass than
    the median's own narrower cell (32, spanning 27.7 to 39.2) and still be right."""
    w = make_length_mix.lognormal_weights(grid, median, sigma)
    edges = [math.sqrt(a * b) for a, b in itertools.pairwise(grid)]
    density = {
        grid[i + 1]: w[i + 1] / math.log(edges[i + 1] / edges[i]) for i in range(len(edges) - 1)
    }
    holding = next(grid[i + 1] for i in range(len(edges) - 1) if edges[i] <= median < edges[i + 1])
    assert max(density, key=density.get) == holding


def test_the_written_config_stays_inside_the_admissible_envelope(tmp_path, capsys) -> None:
    base = CONFIGS / "trace_anchor_1b.json"
    arrival = tmp_path / "arrival.json"
    arrival.write_text(
        json.dumps(
            {
                "process": "mmpp",
                "lambda_base": 0.5,
                "burst_lambda": 2.0,
                "quiet_mean_s": 30,
                "burst_mean_s": 10,
            }
        )
    )
    out = tmp_path / "trace_mix.json"
    argv = [
        "--base", str(base), "--prompt-median", "160", "--prompt-sigma", "0.9",
        "--output-median", "32", "--output-sigma", "0.9", "--arrival", str(arrival), "--out", str(out),
    ]  # fmt: skip
    assert make_length_mix.main(argv) == 0
    cfg = json.loads(out.read_text())
    limits = json.loads(base.read_text())["admissible"]
    assert limits == {"max_prompt": 512, "max_output": 128, "timeout_ceiling_ms": 60000}
    for bucket in cfg["length_dist"]["buckets"]:
        p, o = (int(x) for x in bucket[1:].split("_o"))
        assert p <= limits["max_prompt"] and o <= limits["max_output"]
    assert all(w >= 0.002 for w in cfg["length_dist"]["weights"])
    assert sum(cfg["length_dist"]["weights"]) == pytest.approx(1.0, abs=0.02)
    assert cfg["arrival"]["process"] == "mmpp"
    assert "Chosen parameters, not fitted" in cfg["_comment"]
    assert "buckets, mean output" in capsys.readouterr().out


def test_the_committed_heavy_tail_config_is_what_the_script_writes(tmp_path, capsys) -> None:
    """trace_heavytail_1b.json says it was written by this script from the anchor config at
    400 requests, prompt (160, 0.9), output (32, 0.9). Regenerating it gives the same mix."""
    base = {**json.loads((CONFIGS / "trace_anchor_1b.json").read_text()), "n_requests": 400}
    base_path = tmp_path / "trace_anchor_1b.json"
    base_path.write_text(json.dumps(base))
    out = tmp_path / "mix.json"
    argv = [
        "--base", str(base_path), "--prompt-median", "160", "--prompt-sigma", "0.9",
        "--output-median", "32", "--output-sigma", "0.9", "--out", str(out),
    ]  # fmt: skip
    assert make_length_mix.main(argv) == 0
    committed = json.loads((CONFIGS / "trace_heavytail_1b.json").read_text())
    assert json.loads(out.read_text())["length_dist"] == committed["length_dist"]
