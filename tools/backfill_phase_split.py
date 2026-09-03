"""Add the measured prefill/decode split to C-3 entries that were fitted without it.

The cost model records `service_ms_mean` per cell and stops there, which is enough to draw
a service time and not enough to reason about what happens to a request already in flight.
Prefill and decode do not answer to concurrency the same way: decode is memory-bound and
falls close to 1/c, while prompt evaluation under staggered arrivals overlaps other
requests' decode rather than their prefill and stays flat. A simulator that rescales a
running request when the batch changes needs to know which part of the remaining time is
which, or it stretches the prompt evaluation too.

Nothing here is re-measured. `prefill_ns` and `decode_ns` were recorded per observation at
calibration time and kept in `observations.jsonl`; this only summarises them onto the
snapshots that were built before the fields existed.

TWO THINGS THAT WOULD OTHERWISE BE WRONG.

The split is written as a share of each snapshot's own `service_ms_mean`, not as a raw mean.
The sustained cell drifts across a snapshot series while the grid cells do not, so a raw
mean taken over the whole run would disagree with the service time sitting next to it in
the same entry. The measured ratio is stable where the absolute number is not.

`prefill + decode` is deliberately left short of `service`. The engine's own timings do not
account for the whole span, and that residual is the adapter's `residual_ns`. Distributing
it across the two phases would invent an attribution that the backend never reported.
"""

from __future__ import annotations

import argparse
import glob
import json
import pathlib


def cell_ratios(obs_path: pathlib.Path) -> dict[tuple[int, int, int], tuple[float, float]]:
    """(prompt_len, output_len, concurrency) -> (prefill share, decode share)."""
    totals: dict[tuple[int, int, int], list[float]] = {}
    for line in obs_path.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        if o.get("status") != "ok":
            continue
        if o.get("prefill_ns") is None or o.get("decode_ns") is None:
            continue
        key = (o["prompt_len"], o["output_len"], o["concurrency"])
        acc = totals.setdefault(key, [0.0, 0.0, 0.0])
        acc[0] += o["prefill_ns"]
        acc[1] += o["decode_ns"]
        acc[2] += o["service_ns"]
    return {k: (v[0] / v[2], v[1] / v[2]) for k, v in totals.items() if v[2] > 0}


def in_bucket(value: int, bucket: list[int]) -> bool:
    return bucket[0] <= value <= bucket[1]


def backfill(snapshot_path: pathlib.Path, ratios: dict, *, dry_run: bool) -> tuple[int, int]:
    snap = json.loads(snapshot_path.read_text())
    done = skipped = 0
    for entry in snap["entries"]:
        matched = [
            r
            for (p, o, c), r in ratios.items()
            if in_bucket(p, entry["prompt_bucket"])
            and in_bucket(o, entry["output_bucket"])
            and c == entry["concurrency"]
        ]
        if not matched:
            skipped += 1
            continue
        pre = sum(m[0] for m in matched) / len(matched)
        dec = sum(m[1] for m in matched) / len(matched)
        entry["prefill_ms_mean"] = round(pre * entry["service_ms_mean"], 3)
        entry["decode_ms_mean"] = round(dec * entry["service_ms_mean"], 3)
        done += 1
    if not dry_run and done:
        snapshot_path.write_text(json.dumps(snap, indent=2) + "\n")
    return done, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--observations", required=True, type=pathlib.Path)
    ap.add_argument("--snapshots", required=True, help="glob of snapshot json files")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ratios = cell_ratios(args.observations)
    if not ratios:
        print(f"{args.observations} carries no prefill/decode timings; nothing to backfill")
        return 1
    print(f"{len(ratios)} calibration cell(s) with a phase split")

    paths = sorted(pathlib.Path(p) for p in glob.glob(args.snapshots))
    if not paths:
        print(f"no snapshots matched {args.snapshots}")
        return 1
    for path in paths:
        done, skipped = backfill(path, ratios, dry_run=args.dry_run)
        note = "would write" if args.dry_run else "wrote"
        print(f"  {note} {done} entr(ies), {skipped} unmatched -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
