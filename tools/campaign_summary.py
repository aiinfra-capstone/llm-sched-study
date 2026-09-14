#!/usr/bin/env python3
"""Per-point policy tables and the H1 interaction for one run set, with bootstrap intervals.

`figures` draws H1 pooled over every load point in a set and without an interval. A paper
needs the table underneath: each policy at each point, and the interaction at each point,
with an interval that says whether the difference is larger than the run-to-run noise.

Rows are the measured requests of each run: warmup excluded, status ok. The bootstrap
resamples requests with replacement within each run, so a policy's three repeats stay three
repeats and a noisy repeat widens the interval instead of being averaged away. Every policy
is resampled independently, which is right because the repeats of different policies are
different runs.

The H1 interaction is `(wjsq - jsq) - (static_weighted - round_robin)` on the chosen
statistic. Both brackets are negative when calibration helps. A positive interaction means
calibration buys less once the policy already sees queue depth, which is H1's "redundant".

Usage:
  uv run --project dataplane python tools/campaign_summary.py runs/exp/<tag>/runset.parquet \\
      --out runs/exp/<tag>/summary
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

STATS = {
    "mean": np.mean,
    "p50": lambda a: np.percentile(a, 50),
    "p95": lambda a: np.percentile(a, 95),
    "p99": lambda a: np.percentile(a, 99),
}
H1_POLICIES = ("round_robin", "static_weighted", "jsq", "wjsq")


def measured(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[(~frame["is_warmup"]) & (frame["status"] == "ok")]


def resample(rows: pd.DataFrame, rng: np.random.Generator) -> np.ndarray:
    """One bootstrap draw of e2e latencies, stratified by run."""
    parts = []
    for _, run in rows.groupby("run_id"):
        e2e = run["e2e_ms"].to_numpy()
        parts.append(e2e[rng.integers(0, len(e2e), len(e2e))])
    return np.concatenate(parts)


def interval(draws: np.ndarray) -> tuple[float, float]:
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return float(lo), float(hi)


def summarise(frame: pd.DataFrame, n_boot: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    rows = measured(frame)
    fast = rows.groupby("chosen_node")["service_ms"].mean().idxmin()
    points = []
    for lam, at_point in sorted(rows.groupby("lambda"), key=lambda kv: kv[0]):
        by_policy = {p: g for p, g in at_point.groupby("policy")}
        draws = {p: {s: np.empty(n_boot) for s in STATS} for p in by_policy}
        for i in range(n_boot):
            for p, g in by_policy.items():
                sample = resample(g, rng)
                for s, fn in STATS.items():
                    draws[p][s][i] = fn(sample)
        policies = {}
        for p, g in sorted(by_policy.items()):
            e2e = g["e2e_ms"].to_numpy()
            policies[p] = {
                "runs": int(g["run_id"].nunique()),
                "requests": len(g),
                "share_to_fast_node": round(float((g["chosen_node"] == fast).mean()), 3),
                "queue_wait_ms_mean": round(float(g["queue_wait_ms"].mean()), 1),
                "routing_error_rate": round(float((g["routing_error_ms"].fillna(0) > 0).mean()), 3),
                **{
                    s: {
                        "value": round(float(fn(e2e)), 1),
                        "ci95": [round(x, 1) for x in interval(draws[p][s])],
                    }
                    for s, fn in STATS.items()
                },
            }
        h1 = {}
        if all(p in by_policy for p in H1_POLICIES):
            for s, fn in STATS.items():
                v = {p: fn(by_policy[p]["e2e_ms"].to_numpy()) for p in H1_POLICIES}
                point_value = (v["wjsq"] - v["jsq"]) - (v["static_weighted"] - v["round_robin"])
                d = draws
                boot = (d["wjsq"][s] - d["jsq"][s]) - (
                    d["static_weighted"][s] - d["round_robin"][s]
                )
                lo, hi = interval(boot)
                h1[s] = {
                    "calibration_gain_queue_blind": round(
                        float(v["round_robin"] - v["static_weighted"]), 1
                    ),
                    "calibration_gain_queue_aware": round(float(v["jsq"] - v["wjsq"]), 1),
                    "interaction": round(float(point_value), 1),
                    "ci95": [round(lo, 1), round(hi, 1)],
                    "excludes_zero": bool(lo > 0 or hi < 0),
                }
        points.append({"lambda_rps": round(float(lam), 3), "policies": policies, "h1": h1})
    first = frame.iloc[0]
    return {
        "run_ids": sorted(frame["run_id"].unique().tolist()),
        "vehicle": sorted(frame["vehicle"].unique().tolist()),
        "R_headline": round(float(first["R"]), 3),
        "trace_sha256": first["trace_sha256"],
        "fast_node": fast,
        "bootstrap": {"draws": n_boot, "seed": seed, "strata": "run"},
        "points": points,
    }


def markdown(summary: dict) -> str:
    out = [
        (
            f"Fast node: `{summary['fast_node']}`. Bootstrap: {summary['bootstrap']['draws']} draws, "
            "resampling requests within each run. Intervals are 95%."
        ),
        "",
    ]
    for pt in summary["points"]:
        out += [
            f"### {pt['lambda_rps']} req/s",
            "",
            "| Policy | Requests | To fast node | p50 ms | p95 ms | p99 ms | Mean ms | Queue wait ms | Routing error rate |",
            "|---|---:|---:|---|---|---|---|---:|---:|",
        ]
        for p, r in pt["policies"].items():

            def cell(s: str, r: dict = r) -> str:
                return f"{r[s]['value']:.0f} [{r[s]['ci95'][0]:.0f}, {r[s]['ci95'][1]:.0f}]"

            out.append(
                f"| {p} | {r['requests']} | {r['share_to_fast_node']:.0%} | {cell('p50')} | {cell('p95')} | "
                f"{cell('p99')} | {cell('mean')} | {r['queue_wait_ms_mean']:.1f} | {r['routing_error_rate']:.0%} |"
            )
        if pt["h1"]:
            out += [
                "",
                "| H1 on | Calibration gain, queue-blind | Calibration gain, queue-aware | Interaction | 95% CI | Excludes 0 |",
                "|---|---:|---:|---:|---|---|",
            ]
            for s, h in pt["h1"].items():
                out.append(
                    f"| {s} | {h['calibration_gain_queue_blind']:.0f} | {h['calibration_gain_queue_aware']:.0f} | "
                    f"{h['interaction']:+.0f} | [{h['ci95'][0]:+.0f}, {h['ci95'][1]:+.0f}] | {'yes' if h['excludes_zero'] else 'no'} |"
                )
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runset", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="writes <out>.json and <out>.md")
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260915)
    args = ap.parse_args(argv)
    summary = summarise(pd.read_parquet(args.runset), args.draws, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    args.out.with_suffix(".md").write_text(markdown(summary) + "\n")
    print(markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
