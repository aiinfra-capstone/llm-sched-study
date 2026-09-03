"""Summarise the per-request cost of everything that is not the engine, and record it in C-6.

The C-3 cost model is fitted on the worker's own `service_ns`. A client's `e2e_duration_ns`
also pays for the gRPC hop in, the decision, the dispatch to the worker, and the F-11 direct
return. C-5 already derives that difference per request as `transport_residual_ms`, so there
is nothing new to measure here: this reads the joined runset, pools the warmed-up ok rows,
and writes the summary into the run manifests as `transport_overhead`.

Two decisions worth stating, because both are places the number could be quietly wrong.

POOLING ACROSS OPERATING POINTS IS DELIBERATE. The quantity is a property of the wiring, not
of the load, and the anchors say so: the per-point means sit between 5.37 ms and 6.84 ms
while service time over the same range moves by a factor of ten. Fitting it per operating
point would let load-dependent noise into a term that should not have any, and a simulator
reading it back would then be reproducing the load curve it is supposed to be predicting.

THE SUMMARY IS A MEAN AND A SPREAD, NOT A FIT TO ANY TARGET. Nothing in this file looks at
F-23 error. If the simulator is still short after applying it, that gap belongs to the cost
model or to the queueing, and it should be reported as such rather than absorbed here.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

SCHEMA_KEYS = ("mean_ms", "sd_ms", "n_samples", "source", "measured_from")


def summarise(runset_path: pathlib.Path) -> dict:
    """One `transport_overhead` block from one joined runset."""
    import pandas as pd

    df = pd.read_parquet(runset_path)
    for col in ("transport_residual_ms", "is_warmup", "status", "run_id"):
        if col not in df.columns:
            raise SystemExit(f"{runset_path} has no column {col!r}; is this a C-5 runset?")

    kept = df[(~df["is_warmup"]) & (df["status"] == "ok")]
    if kept.empty:
        raise SystemExit(f"{runset_path} has no warmed-up ok rows to summarise")

    residual = kept["transport_residual_ms"].astype(float)
    if (residual < 0).any():
        # A negative residual means e2e came out below the single-host durations inside it,
        # which is a joining or clock problem, not a small network. Refuse rather than
        # average it away.
        n_neg = int((residual < 0).sum())
        raise SystemExit(f"{n_neg} rows have negative transport_residual_ms; fix the join first")

    runs = sorted(kept["run_id"].unique().tolist())
    return {
        "mean_ms": round(float(residual.mean()), 4),
        "sd_ms": round(float(residual.std(ddof=0)), 4),
        "n_samples": int(residual.size),
        "source": (
            "C-5 transport_residual_ms, pooled over the warmed-up ok rows of "
            f"{len(runs)} run(s) in {runset_path.as_posix()}"
        ),
        "measured_from": runs,
    }


def apply_to(manifest_path: pathlib.Path, block: dict, *, dry_run: bool) -> str:
    manifest = json.loads(manifest_path.read_text())
    was = manifest.get("transport_overhead")
    manifest["transport_overhead"] = block
    if not dry_run:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    verb = "would set" if dry_run else "set"
    if was is None:
        return f"{verb} transport_overhead on {manifest_path}"
    return f"{verb} transport_overhead on {manifest_path} (was {was.get('mean_ms')} ms)"


def validate(block: dict) -> None:
    """Check the block against C-6 before it goes anywhere near a manifest."""
    import jsonschema

    schema = json.loads(pathlib.Path("contracts/schemas/manifest.schema.json").read_text())
    sub = schema["properties"]["transport_overhead"]
    jsonschema.validate(block, sub)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runset", required=True, type=pathlib.Path, help="C-5 runset.parquet")
    ap.add_argument(
        "--apply",
        nargs="*",
        default=[],
        type=pathlib.Path,
        help="run directories whose manifest.json should carry the block",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    block = summarise(args.runset)
    validate(block)

    print(
        f"transport overhead: {block['mean_ms']} +/- {block['sd_ms']} ms "
        f"over n={block['n_samples']} from {len(block['measured_from'])} run(s)"
    )

    missing = []
    for d in args.apply:
        manifest_path = d / "manifest.json" if d.is_dir() else d
        if not manifest_path.exists():
            missing.append(str(manifest_path))
            continue
        print("  " + apply_to(manifest_path, block, dry_run=args.dry_run))

    if missing:
        print("no manifest at: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
