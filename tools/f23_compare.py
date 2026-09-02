#!/usr/bin/env python3
"""Compare one simulated anchor against its hardware measurement (F-23).

Exit codes are three-valued on purpose. 0 is within tolerance, 2 is a real comparison that
came out outside tolerance, and 1 is a failure to compare at all. The caller counts 0 and 2
towards the three-point minimum and counts 1 towards neither, so a broken run can never be
mistaken for a passing one nor quietly reduce the number of points F-23 rests on.

The hardware baseline is `runs/anchors/load_band.json`. That file is committed, it is what
`figures/plots.py` draws the hardware curve from, and it was produced by
`pipeline/loadband.py` from the anchor logs. Reading it here rather than hardcoding numbers
from an issue thread keeps one hardware p50 in the repo instead of two that can drift apart.

Both vehicles are reduced with the same two conventions, because F-23 is a comparison and a
comparison of two differently-computed percentiles measures the conventions:

  warmup    `intended_offset_s >= warmup_s`, the trace's own timeline rather than either
            run's wall clock, so both vehicles discard the same requests (§12.2)
  percentile nearest-rank, matching `loadband._percentile`, so a quoted p95 is a latency
            some request actually had rather than one interpolated between two
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank, byte-for-byte the rule `pipeline/loadband.py` used on the hardware."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def sim_latencies_ms(sim_dir: Path, warmup_s: float) -> list[float]:
    """End-to-end latencies from the simulator's client log, warmup excluded, ok only."""
    logs = sorted(glob.glob(str(sim_dir / "client_*.jsonl")))
    if not logs:
        raise FileNotFoundError(f"no client log in {sim_dir}")
    out = []
    with open(logs[0]) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if float(rec.get("intended_offset_s", 0.0)) < warmup_s:
                continue
            if rec.get("status") != "ok":
                continue
            out.append(rec["e2e_duration_ns"] / 1e6)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="F-23 comparison for one operating point")
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--sim-dir", type=Path, required=True)
    ap.add_argument("--load-band", type=Path, required=True)
    ap.add_argument("--tolerance", type=float, default=25.0)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    name = manifest.get("config", {}).get("operating_point")
    if not name:
        print(f"FAIL: {args.manifest} has no config.operating_point to match against the band")
        return 1

    band = json.loads(args.load_band.read_text())
    point = next((p for p in band.get("points", []) if p.get("name") == name), None)
    if point is None:
        print(f"FAIL: no point named {name!r} in {args.load_band}")
        return 1

    try:
        sim = sim_latencies_ms(args.sim_dir, float(manifest.get("warmup_s", 0.0)))
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"FAIL: could not read the simulated run: {exc}")
        return 1
    if not sim:
        print("FAIL: no simulated requests survived the warmup filter")
        return 1

    hw_p50, hw_p95 = float(point["p50_ms"]), float(point["p95_ms"])
    sim_p50, sim_p95 = percentile(sim, 0.50), percentile(sim, 0.95)
    err_p50 = (sim_p50 - hw_p50) / hw_p50 * 100.0
    err_p95 = (sim_p95 - hw_p95) / hw_p95 * 100.0

    note = " (saturated)" if point.get("saturated") else ""
    print(f"Operating point: {name}{note}  lambda={point['lambda_rps']} rps")
    print(f"  Hardware: p50={hw_p50:9.1f} ms  p95={hw_p95:9.1f} ms  (n={point['n']})")
    print(f"  Sim:      p50={sim_p50:9.1f} ms  p95={sim_p95:9.1f} ms  (n={len(sim)})")
    print(
        f"  Error:    p50={err_p50:+7.1f}%   p95={err_p95:+7.1f}%   tolerance +/-{args.tolerance:.0f}%"
    )

    if abs(err_p50) > args.tolerance or abs(err_p95) > args.tolerance:
        print(f"  FAIL: {name} outside tolerance")
        return 2
    print(f"  PASS: {name} within tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
