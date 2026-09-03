"""Report the heterogeneity ratio separately for prefill and for decode.

The study has been treating R as one number per pair of machines. It is not one number.
Prefill is compute-bound and decode is memory-bandwidth-bound, and a machine does not lose
those two capabilities at the same rate, so the heterogeneity a scheduler actually faces
depends on the shape of the request as well as on the pair of nodes.

This reads the committed C-3 snapshots and computes R three ways per shared grid cell:
on total service time, on prefill alone, and on decode alone. Given one or more trace
configs it also reports the R each workload profile *sees*, weighted by that profile's
bucket mix, which is the number the phase-asymmetry result turns on.

WHAT THIS CAN AND CANNOT SAY.

It needs `prefill_ms_mean` and `decode_ms_mean`, which snapshots fitted before those fields
existed do not carry. Cells without them are skipped and counted rather than estimated.

The concurrency column is not a free axis. Our calibration grid holds `c` requests in flight
by firing them together, so at c > 1 the measured prefill carries contention between
simultaneous prefills that a Poisson workload does not produce. R_prefill at c = 1 is the
honest number; the higher rows say as much about the harness as about the hardware. The
default is therefore concurrency 1, and asking for more prints the warning with the table.

F-9 holds engine, model and quantisation constant across a pool, so a ratio between two
classes running different models is a statement about the models rather than the machines.
The snapshot does not record the model, only `node_class`, so this compares the trailing
part of that string by convention and warns rather than refusing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

Cell = tuple[tuple[int, int], tuple[int, int], int]


def newest_by_class(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    """One snapshot per node class: the most recently measured."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            snap = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if "node_class" not in snap or "entries" not in snap:
            continue
        cur = out.get(snap["node_class"])
        if cur is None or snap["measured_at_unix"] > cur["measured_at_unix"]:
            out[snap["node_class"]] = snap
    return out


def phase_cells(snap: dict[str, Any]) -> dict[Cell, dict[str, Any]]:
    """Cells that carry a phase split, keyed by (prompt bucket, output bucket, concurrency)."""
    cells = {}
    for e in snap["entries"]:
        if e.get("prefill_ms_mean") is None or e.get("decode_ms_mean") is None:
            continue
        key = (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"])
        cells[key] = e
    return cells


def model_of(node_class: str) -> str | None:
    """The model half of `<hw>_ngl<N>_p<P>_q<quant>_<model>`, or None if it does not fit."""
    parts = node_class.split("_")
    return "_".join(parts[4:]) if len(parts) > 4 else None


def ratios(fast: dict[str, Any], slow: dict[str, Any]) -> dict[str, float]:
    """Slow over fast, so a ratio above 1 means the slow node is that many times slower."""
    return {
        "R_service": slow["service_ms_mean"] / fast["service_ms_mean"],
        "R_prefill": slow["prefill_ms_mean"] / fast["prefill_ms_mean"],
        "R_decode": slow["decode_ms_mean"] / fast["decode_ms_mean"],
    }


def locate(p: int, o: int, c: int, cells: dict[Cell, Any]) -> Cell | None:
    for pb, ob, cc in cells:
        if cc == c and pb[0] <= p <= pb[1] and ob[0] <= o <= ob[1]:
            return (pb, ob, cc)
    return None


def profile_buckets(config_path: pathlib.Path) -> tuple[list[tuple[int, int]], list[float]]:
    """(prompt, output) pairs and weights from a C-2 trace config's length_dist."""
    cfg = json.loads(config_path.read_text())
    ld = cfg["length_dist"]
    pairs = []
    for b in ld["buckets"]:
        prompt, output = b[1:].split("_o")
        pairs.append((int(prompt), int(output)))
    total = float(sum(ld["weights"]))
    return pairs, [w / total for w in ld["weights"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--fast", required=True, help="node_class of the faster node")
    ap.add_argument("--slow", required=True, help="node_class of the slower node")
    ap.add_argument(
        "--cost-models", type=pathlib.Path, default=pathlib.Path("contracts/cost_models")
    )
    ap.add_argument("--concurrency", type=int, default=1, help="default 1; see the note above")
    ap.add_argument(
        "--profile",
        action="append",
        default=[],
        type=pathlib.Path,
        help="C-2 trace config; repeatable",
    )
    ap.add_argument("--out", type=pathlib.Path, help="write the report as JSON")
    args = ap.parse_args(argv)

    snaps = newest_by_class(args.cost_models)
    missing = [n for n in (args.fast, args.slow) if n not in snaps]
    if missing:
        print(f"no snapshot for {', '.join(missing)}", file=sys.stderr)
        print(f"known classes: {', '.join(sorted(snaps))}", file=sys.stderr)
        return 1

    fast_snap, slow_snap = snaps[args.fast], snaps[args.slow]
    mf, ms = model_of(args.fast), model_of(args.slow)
    if mf and ms and mf != ms:
        print(
            f"WARNING: {args.fast} runs {mf} and {args.slow} runs {ms}. F-9 holds the model "
            f"constant across a pool, so this ratio mixes the models with the machines.\n"
        )

    fast_cells, slow_cells = phase_cells(fast_snap), phase_cells(slow_snap)
    for name, snap, cells in (
        (args.fast, fast_snap, fast_cells),
        (args.slow, slow_snap, slow_cells),
    ):
        skipped = len(snap["entries"]) - len(cells)
        if skipped:
            print(f"note: {name} has {skipped} cell(s) with no phase split; they are not compared")

    shared = sorted(set(fast_cells) & set(slow_cells))
    at_c = [k for k in shared if k[2] == args.concurrency]
    if not at_c:
        have = sorted({k[2] for k in shared})
        print(
            f"no shared cell at concurrency {args.concurrency}; shared concurrencies: {have}",
            file=sys.stderr,
        )
        return 1
    if args.concurrency != 1:
        print(
            "WARNING: at concurrency above 1 the calibration grid fires requests together, so "
            "R_prefill carries harness contention a Poisson workload does not produce.\n"
        )

    report: dict[str, Any] = {
        "fast": {"node_class": args.fast, "snapshot_id": fast_snap["snapshot_id"]},
        "slow": {"node_class": args.slow, "snapshot_id": slow_snap["snapshot_id"]},
        "concurrency": args.concurrency,
        "cells": [],
        "profiles": [],
    }

    print(f"\n{args.slow}\n  over {args.fast}   at concurrency {args.concurrency}\n")
    print(
        f"{'prompt':>12} {'output':>10} {'R_service':>10} {'R_prefill':>10} {'R_decode':>9} {'dec/pre':>8}"
    )
    for key in at_c:
        r = ratios(fast_cells[key], slow_cells[key])
        spread = r["R_decode"] / r["R_prefill"] if r["R_prefill"] else float("nan")
        print(
            f"{key[0]!s:>12} {key[1]!s:>10} {r['R_service']:10.2f} {r['R_prefill']:10.2f} "
            f"{r['R_decode']:9.2f} {spread:8.2f}"
        )
        report["cells"].append(
            {
                "prompt_bucket": list(key[0]),
                "output_bucket": list(key[1]),
                **r,
                "decode_over_prefill": spread,
            }
        )

    for path in args.profile:
        pairs, weights = profile_buckets(path)
        acc = {"R_service": 0.0, "R_prefill": 0.0, "R_decode": 0.0}
        rho = 0.0
        unpriced = []
        for (p, o), w in zip(pairs, weights):
            key = locate(p, o, args.concurrency, fast_cells)
            if key is None or key not in slow_cells:
                unpriced.append(f"p{p}_o{o}")
                continue
            r = ratios(fast_cells[key], slow_cells[key])
            for k in acc:
                acc[k] += w * r[k]
            rho += w * (p / o)
        entry = {"profile": path.stem, "mean_rho": rho, **acc, "unpriced_buckets": unpriced}
        report["profiles"].append(entry)

    if report["profiles"]:
        print("\nwhat each workload profile sees\n")
        print(
            f"{'profile':>28} {'mean rho':>9} {'R_service':>10} {'R_prefill':>10} {'R_decode':>9}"
        )
        for e in report["profiles"]:
            print(
                f"{e['profile']:>28} {e['mean_rho']:9.2f} {e['R_service']:10.2f} "
                f"{e['R_prefill']:10.2f} {e['R_decode']:9.2f}"
            )
            if e["unpriced_buckets"]:
                print(f"{'':>28} no shared cell for: {', '.join(e['unpriced_buckets'])}")

    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
