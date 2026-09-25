#!/usr/bin/env python3
"""Re-fit a finished calibration from its own samples, by today's rules.

The samples a campaign collected do not go stale; the rules for turning them into a cost
model do. A cell is now fitted only from the samples served at the concurrency it claims,
and every campaign taken before that rule existed was fitted from all of them, the draining
end of each cell included. Re-running those campaigns would cost engine time the samples
already paid for, and would measure the machine on a different day, which is the one thing
a rule change must not introduce.

This runs `dataplane.calibration.refit`, which puts the logged samples back through the
campaign's own fit. The output is a new run directory, stamped when it was fitted and
naming its source as `refit_of`. The source run is not touched: its snapshots are what some
campaign was actually served, and rewriting them would make that record untrue.

It promotes nothing. Read what moved, then promote the new run the usual way, which is what
validates it against C-3, checks it for inversions and repoints the configs:

  uv run --project dataplane python tools/refit_calibration.py \\
      runs/calibration/cal_gtx1650ti_..._1758000000
  uv run --project dataplane python tools/promote_calibration.py runs/calibration/<new_run>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dataplane.calibration import refit
from promote_calibration import compare


def newest_snapshot(run_dir: Path) -> dict[str, Any] | None:
    """The last snapshot of a run's series, which is the one a promotion would serve."""
    series = sorted((run_dir / "snapshots").glob("*.json"))
    return json.loads(series[-1].read_text(encoding="utf-8")) if series else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path, help="a finished calibration run directory")
    ap.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="where the refitted run is written; by default beside the run it came from, "
        "which is where every other run of that campaign already is",
    )
    args = ap.parse_args(argv)

    if not (args.run_dir / "campaign.json").is_file():
        print(f"refusing: {args.run_dir} has no campaign.json, so it is not a finished run")
        return 2
    if not (args.run_dir / "observations.jsonl").is_file():
        print(f"refusing: {args.run_dir} has no observations.jsonl, so there is nothing to refit")
        return 2

    old = newest_snapshot(args.run_dir)
    out_dir = refit.refit_run(args.run_dir, args.out_root or args.run_dir.parent)
    report = json.loads((out_dir / "campaign.json").read_text(encoding="utf-8"))
    new = newest_snapshot(out_dir)

    print(f"{report['refit_of']}\n  -> {report['run_id']}  ({out_dir})")
    print(
        f"  {report['n_grid_samples']} grid + {report['n_sustained_samples']} sustained samples, "
        f"{report['n_snapshots']} snapshot(s)"
    )
    thin = {
        cell: kept
        for cell, kept in report["grid_samples_at_stated_concurrency"].items()
        if kept.split("/")[0] != kept.split("/")[1]
    }
    if thin:
        print("  cells fitted from fewer samples than they hold, the draining end dropped:")
        for cell, kept in sorted(thin.items()):
            print(f"    {cell}: {kept}")
    # Without this line a refit where MPR-1 did not land reads only "no snapshot to compare
    # against" below, and never says why. Promotion refuses a run with no snapshots anyway.
    if report["stationarity"] is None:
        print(f"  MPR-1 not established on the refit: {report['stationarity_error']}")
    if old is None or new is None:
        print("  no snapshot to compare against; nothing else to say")
        return 0
    # A fit writes service time alone, so the first line compare() returns, which is the
    # capability, is dropped: the phase split is backfilled from the same observations at
    # promotion, and comparing a refit's capability against a promoted snapshot's would
    # report a node that changed when only the pipeline stage did.
    for line in compare(old, new)[1:]:
        print("  " + line)
    print(
        "  capability and prefill are not compared: a fit writes service time alone, and "
        "promotion backfills the split from the same observations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
