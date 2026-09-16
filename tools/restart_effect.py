#!/usr/bin/env python3
"""Does a node get slower as its engine's host prompt cache fills? A check on logged runs.

The shape campaigns ran with llama-server's default `--cache-ram 8192`, which grew host
memory until both engines were restarted by hand, about 12 times. No prompt was served from
the cache, so no prefill was skipped, but a filling cache and swap can still cost time. The
brief said no latency changed; this is the measurement behind that sentence.

Engine start times come from the engine logs: a log's modification time is the wall clock of
its last line, and every line carries its offset from engine start (`M.SS.mmm.uuu`), so
start = mtime - last offset. Each run is placed on the engine that was up when it started
(`started_unix` in its manifest).

The response is a node's own worker-side service time, the one thing a slower engine would
change, taken relative to the mean of the same cell (run set, policy, point) so that policy
and load drop out. A slope of log relative service time on hours since restart, with a
bootstrap over runs, says whether service drifted as the engine aged.

Only the RTX 3050's engine logs were kept, so only that node is checked.

Usage:
  uv run --project dataplane python tools/restart_effect.py \\
      --engine-logs runs/node_rtx3050/tmp_logs --node rtx3050 \\
      runs/exp/mpr2_1650ti_3050 runs/exp/phase_*_1650ti_3050 --out runs/exp/restart_effect_rtx3050.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def offset_s(stamp: str) -> float:
    minutes, seconds, ms, us = (int(x) for x in stamp.split("."))
    return minutes * 60 + seconds + ms / 1e3 + us / 1e6


def engine_spans(log_dir: Path) -> list[tuple[float, float, str]]:
    spans = []
    for p in sorted(log_dir.glob("llama-server*.log")):
        last = None
        with p.open(errors="replace") as fh:
            for line in fh:
                head = line.split(" ", 1)[0]
                if head.count(".") == 3:
                    last = head
        if last is None:
            continue
        end = p.stat().st_mtime
        spans.append((end - offset_s(last), end, p.name))
    return sorted(spans)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runsets", nargs="+", type=Path)
    ap.add_argument("--engine-logs", type=Path, required=True)
    ap.add_argument("--node", required=True)
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    spans = engine_spans(args.engine_logs)
    rows = []
    for rs in args.runsets:
        frame = pd.read_parquet(rs / "runset.parquet")
        frame = frame[(~frame["is_warmup"]) & (frame["status"] == "ok")]
        frame = frame[frame["chosen_node"] == args.node]
        for run_id, g in frame.groupby("run_id"):
            man = json.loads((rs / run_id / "manifest.json").read_text())
            t = man["started_unix"]
            up = [s for s in spans if s[0] <= t <= s[1]]
            if not up:
                continue
            start, _, name = up[-1]
            rows.append(
                {
                    "runset": rs.name,
                    "run_id": run_id,
                    "policy": man["policy"],
                    "lambda": man["lambda"],
                    "engine_log": name,
                    "hours_since_restart": (t - start) / 3600,
                    "service_ms": float(g["service_ms"].mean()),
                    "n": len(g),
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        print("no run falls inside a logged engine lifetime")
        return 1
    df["rel"] = df["service_ms"] / df.groupby(["runset", "policy", "lambda"])[
        "service_ms"
    ].transform("mean")
    x = df["hours_since_restart"].to_numpy()
    y = np.log(df["rel"].to_numpy())
    slope = float(np.polyfit(x, y, 1)[0])
    rng = np.random.default_rng(args.seed)
    boots = []
    for _ in range(args.draws):
        i = rng.integers(0, len(df), len(df))
        if np.ptp(x[i]) > 0:
            boots.append(np.polyfit(x[i], y[i], 1)[0])
    lo, hi = np.percentile(boots, [2.5, 97.5])
    report = {
        "node": args.node,
        "engines": [{"log": n, "start_unix": round(s), "end_unix": round(e)} for s, e, n in spans],
        "runs_placed": len(df),
        "hours_since_restart_range": [round(float(x.min()), 2), round(float(x.max()), 2)],
        "slope_log_service_per_hour": round(slope, 5),
        "slope_ci95": [round(float(lo), 5), round(float(hi), 5)],
        "percent_per_hour": round(100 * (np.exp(slope) - 1), 2),
        "percent_per_hour_ci95": [
            round(100 * (np.exp(lo) - 1), 2),
            round(100 * (np.exp(hi) - 1), 2),
        ],
    }
    print(json.dumps(report | {"engines": len(spans)}, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
