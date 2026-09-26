#!/usr/bin/env python3
"""Intervals on the cost-model numbers the study leans on: capability and phase R.

Each C-3 cell is a mean of 8 samples, and at concurrency 4 those 8 are two synchronised
batches. Capability, which three of the five policies route on, comes from one cell measured
once. Phase R, the elevation's premise, is a ratio of two such cells. Neither has carried an
interval. This bootstraps them from the calibration observations behind the two snapshots.

Resampling is by batch: consecutive grid samples of one cell at concurrency c are grouped c
at a time, in the order the calibration logged them, and whole groups are drawn. At c = 1
that is the ordinary bootstrap.

A batch is counted at concurrency c only if it ran at c. The cost model fits only samples
served at the concurrency their cell claims (`cost_model.steady_samples`), and the same rule
is applied here: a batch holding any sample whose logged `occupancy_mean` fell below
c - OCCUPANCY_TOLERANCE is dropped whole, and the report says how many batches and samples
each cell lost. A sample logged before occupancy was recorded is kept, as the fit keeps it.

It reports, per node class: capability (output tok/s of service at the lowest cell and
c = 1, as com.sched.core.Capability) and, for each trace profile, its mean prompt-to-output
ratio and R on service, prefill and decode at every concurrency the grid covers, weighted by
the profile's bucket mix. The
concurrency the live runs operated at is not 1: under load the slots are mostly busy, so
the c = 4 rows are the ones that describe the pool the policies faced.

Usage:
  uv run --project dataplane python tools/cell_intervals.py \\
      --fast runs/calibration/llama32-1b-anchorgrid/cal_rtx3050_ngl99_p4_q4km_llama32_1b_1789415783 \\
      --slow runs/calibration/llama32-1b-anchorgrid/cal_gtx1650ti_ngl99_p4_q4km_llama32_1b_1788190342 \\
      --profile dataplane/configs/trace_anchor_1b.json ... --out runs/exp/cell_intervals.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from dataplane.calibration.cost_model import OCCUPANCY_TOLERANCE
from pool_load import bucket_mix

PHASES = ("service", "prefill", "decode")


Key = tuple[int, int, int]


def grid(run_dir: Path) -> tuple[dict[Key, np.ndarray], dict[Key, dict[str, int]]]:
    """(cells, removed).

    cells: (prompt_len, output_len, c) -> array (groups, c, 4) of service, prefill, decode,
    tokens, holding only the batches that ran at c. removed: the same keys -> how many
    batches, and the samples in them, the occupancy filter dropped.
    """
    cells: dict[Key, list] = {}
    steady: dict[Key, list[bool]] = {}
    for line in (run_dir / "observations.jsonl").read_text().splitlines():
        o = json.loads(line)
        if o.get("segment") != "grid" or o.get("status") != "ok":
            continue
        key = (o["prompt_len"], o["output_len"], o["concurrency"])
        cells.setdefault(key, []).append(
            [o["service_ns"] / 1e6, o["prefill_ns"] / 1e6, o["decode_ns"] / 1e6, o["output_tokens"]]
        )
        occupancy = o.get("occupancy_mean")
        steady.setdefault(key, []).append(
            occupancy is None or occupancy >= o["concurrency"] - OCCUPANCY_TOLERANCE
        )
    out: dict[Key, np.ndarray] = {}
    removed: dict[Key, dict[str, int]] = {}
    for (p, o, c), rows in cells.items():
        k = len(rows) // c
        batches = np.asarray(rows)[: k * c].reshape(k, c, 4)
        kept = np.asarray(steady[(p, o, c)][: k * c], dtype=bool).reshape(k, c).all(axis=1)
        out[(p, o, c)] = batches[kept]
        dropped = int((~kept).sum())
        removed[(p, o, c)] = {"batches": dropped, "samples": dropped * c}
    return out, removed


def draw_means(cell: np.ndarray, rng: np.random.Generator, draws: int) -> np.ndarray:
    """(draws, 4) bootstrap means, resampling whole batches."""
    k = cell.shape[0]
    idx = rng.integers(0, k, size=(draws, k))
    return cell[idx].reshape(draws, -1, 4).mean(axis=1)


def nearest(cells: dict, p: int, o: int, c: int) -> tuple[int, int, int]:
    """The grid cell whose prompt and output lengths bracket the bucket, as C-3 lookup does."""
    candidates = [k for k in cells if k[2] == c and k[0] >= p and k[1] >= o]
    if not candidates:
        raise ValueError(f"no grid cell covers p{p}_o{o} at c={c}")
    return min(candidates, key=lambda k: (k[0], k[1]))


def ci(a: np.ndarray) -> list[float]:
    return [round(float(x), 3) for x in np.percentile(a, [2.5, 97.5])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fast", type=Path, required=True)
    ap.add_argument("--slow", type=Path, required=True)
    ap.add_argument("--profile", type=Path, action="append", default=[])
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)

    g, removed = {}, {}
    for side, path in (("fast", args.fast), ("slow", args.slow)):
        g[side], removed[side] = grid(path)
    boot = {
        side: {k: draw_means(v, rng, args.draws) for k, v in cells.items()}
        for side, cells in g.items()
    }
    point = {
        side: {k: v.reshape(-1, 4).mean(axis=0) for k, v in cells.items()}
        for side, cells in g.items()
    }

    report: dict = {
        "fast": str(args.fast),
        "slow": str(args.slow),
        "draws": args.draws,
        # Only cells that lost something, named as p<prompt>_o<output>_c<concurrency>.
        "removed_by_occupancy": {
            side: {f"p{p}_o{o}_c{c}": n for (p, o, c), n in sorted(cells.items()) if n["batches"]}
            for side, cells in removed.items()
        },
        "capability": {},
    }
    for side in ("fast", "slow"):
        lo_p = min(k[0] for k in g[side])
        lo_o = min(k[1] for k in g[side])
        key = (lo_p, lo_o, 1)
        m, b = point[side][key], boot[side][key]
        cap = m[3] / (m[0] / 1000)
        cap_b = b[:, 3] / (b[:, 0] / 1000)
        report["capability"][side] = {
            "cell": list(key),
            "samples": int(g[side][key].shape[0]),
            "tok_s_of_service": round(float(cap), 2),
            "ci95": ci(cap_b),
        }
        report["capability"][side + "_boot"] = cap_b
    ratio_b = report["capability"].pop("fast_boot") / report["capability"].pop("slow_boot")
    report["capability"]["fast_over_slow"] = {
        "value": round(
            report["capability"]["fast"]["tok_s_of_service"]
            / report["capability"]["slow"]["tok_s_of_service"],
            3,
        ),
        "ci95": ci(ratio_b),
    }

    concurrencies = sorted({k[2] for k in g["fast"]} & {k[2] for k in g["slow"]})
    report["profiles"] = []
    for path in args.profile:
        mix = bucket_mix(json.loads(path.read_text())["length_dist"])
        # The profile's mean prompt-to-output ratio, weighted by its bucket mix, as
        # phase_ratio.py reports it, so a K1 row can say which workload shape it is.
        mean_rho = sum(w * p / o for _, p, o, w in mix)
        entry: dict = {"profile": path.stem, "mean_rho": mean_rho, "by_concurrency": {}}
        for c in concurrencies:
            vals, draws = {}, {}
            for side in ("fast", "slow"):
                acc = np.zeros(3)
                acc_b = np.zeros((args.draws, 3))
                for _, p, o, w in mix:
                    key = nearest(g[side], p, o, c)
                    acc += w * point[side][key][:3]
                    acc_b += w * boot[side][key][:, :3]
                vals[side], draws[side] = acc, acc_b
            entry["by_concurrency"][str(c)] = {
                f"R_{ph}": {
                    "value": round(float(vals["slow"][i] / vals["fast"][i]), 3),
                    "ci95": ci(draws["slow"][:, i] / draws["fast"][:, i]),
                }
                for i, ph in enumerate(PHASES)
            }
        report["profiles"].append(entry)

    print(json.dumps(report, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
