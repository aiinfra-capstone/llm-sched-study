#!/usr/bin/env python3
"""Promote a finished calibration into the contracts, and point every config at it.

After a node class is recalibrated, five things have to happen before any campaign can run,
and doing them by hand on 2026-09-17 took an hour and one near miss:

1. **Backfill the phase split.** `calibrate` writes snapshots whose entries carry service time
   only; the per-request prefill and decode timings are in `observations.jsonl`, and
   `tools/backfill_phase_split.py` summarises them onto the snapshots. Without it, phase R
   and the phase-aware parts of the simulator have nothing to read.
2. **Validate against C-3, and against the hardware it claims to describe.** A snapshot that
   fails the schema, for instance one whose provenance lacks `driver`, is refused rather than
   promoted. So is one that is inverted in concurrency, where a bucket's service time falls
   as more requests share the engine.
3. **Copy the series into `contracts/cost_models/<node_class>/`.**
4. **Repoint the configs.** The scheduler serves the newest snapshot in a class, and
   `tools/hw_runs.py` refuses any campaign whose config names an older one. Every config in
   `dataplane/configs/` that names the class's previous newest snapshot is rewritten to name
   the new one. The edit replaces the id string only, so a config's layout and comments stay
   as they were. Manifests of runs already done keep the snapshot that served them.
5. **Dry-run every campaign**, so a config that still does not check is found now.

It also answers the question a recalibration is for: did the node change? It prints the
capability and every cell's service and prefill time against the snapshot it replaces.

Usage:
  uv run --project dataplane python tools/promote_calibration.py \\
      runs/calibration/llama32-1b-anchorgrid/cal_rtx3050_ngl99_p4_q4km_llama32_1b_<ts> [--dry-run]
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pool_load

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"
CONFIGS = REPO_ROOT / "dataplane" / "configs"
TOOLS = REPO_ROOT / "tools"


def newest_in_class(index: dict[str, dict[str, Any]], node_class: str) -> dict[str, Any] | None:
    snaps = [s for s in index.values() if s["node_class"] == node_class]
    return max(snaps, key=lambda s: s["measured_at_unix"]) if snaps else None


def has_phase_split(snap: dict[str, Any]) -> bool:
    return all(e.get("prefill_ms_mean") is not None for e in snap["entries"])


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO_ROOT)


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Capability and every shared cell, old against new, as printable lines."""

    def key(e: dict[str, Any]) -> tuple:
        return (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"])

    out = [
        (
            f"capability  {pool_load.capability(old):.3f} -> {pool_load.capability(new):.3f} "
            "output tok/s of service"
        ),
        (
            f"{'prompt':>12} {'output':>10} {'c':>2} {'service':>17} {'change':>7} "
            f"{'prefill':>15} {'change':>7}"
        ),
    ]
    olds = {key(e): e for e in old["entries"]}
    for e in sorted(new["entries"], key=key):
        o = olds.get(key(e))
        if o is None:
            continue
        ds = 100 * (e["service_ms_mean"] - o["service_ms_mean"]) / o["service_ms_mean"]
        pre = ""
        if o.get("prefill_ms_mean") and e.get("prefill_ms_mean"):
            dp = 100 * (e["prefill_ms_mean"] - o["prefill_ms_mean"]) / o["prefill_ms_mean"]
            pre = f"{o['prefill_ms_mean']:6.0f} -> {e['prefill_ms_mean']:6.0f} {dp:+6.1f}%"
        out.append(
            f"{e['prompt_bucket']!s:>12} {e['output_bucket']!s:>10} {e['concurrency']:>2} "
            f"{o['service_ms_mean']:6.0f} -> {e['service_ms_mean']:6.0f} {ds:+6.1f}% {pre}"
        )
    return out


def inversions(snap: dict[str, Any], tolerance: float = 0.02) -> list[str]:
    """Cells whose service time falls as concurrency rises, which hardware does not do.

    Sharing an engine with more requests cannot make a request faster, so a bucket whose
    mean drops from one concurrency to the next is a measurement artefact rather than a
    property of the node. The 2026-09-14 RTX 3050 grid was inverted at concurrency 3 in
    all six of its buckets, by 15 to 40%, and nothing refused it: it reached the contracts,
    parameterised the simulator, and is why the simulator failed the contrast criterion on
    every held-out shape (`results.md` section 8). The tolerance absorbs sampling noise,
    not a step.
    """
    by_bucket: dict[tuple, dict[int, float]] = {}
    for e in snap["entries"]:
        key = (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]))
        by_bucket.setdefault(key, {})[e["concurrency"]] = e["service_ms_mean"]
    out = []
    for (pb, ob), row in sorted(by_bucket.items()):
        for lo, hi in itertools.pairwise(sorted(row)):
            if row[hi] < row[lo] * (1 - tolerance):
                out.append(
                    f"prompt {list(pb)} output {list(ob)}: c={lo} {row[lo]:.0f} ms is slower "
                    f"than c={hi} {row[hi]:.0f} ms, by {100 * (row[lo] / row[hi] - 1):.0f}%"
                )
    return out


def configs_naming(snapshot_id: str) -> list[Path]:
    """Configs whose cost_model_snapshots name this id, confirmed by parsing, not by grep."""
    hits = []
    for path in sorted(CONFIGS.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        # Some configs are a bare list of node blocks, and name no snapshot at all.
        if isinstance(d, dict) and snapshot_id in (d.get("cost_model_snapshots") or {}).values():
            hits.append(path)
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path, help="a finished calibration run directory")
    ap.add_argument("--dry-run", action="store_true", help="report, and change nothing")
    args = ap.parse_args(argv)

    campaign = json.loads((args.run_dir / "campaign.json").read_text(encoding="utf-8"))
    node_class = campaign["node_class"]
    series = sorted((args.run_dir / "snapshots").glob("*.json"))
    if not series:
        print(f"refusing: {args.run_dir} has no snapshots")
        return 2

    # 1. Phase split.
    newest_path = series[-1]
    if not has_phase_split(json.loads(newest_path.read_text(encoding="utf-8"))):
        print("backfilling the phase split from observations.jsonl")
        if not args.dry_run:
            r = run(
                [
                    sys.executable,
                    str(TOOLS / "backfill_phase_split.py"),
                    "--observations",
                    str(args.run_dir / "observations.jsonl"),
                    "--snapshots",
                    str(args.run_dir / "snapshots" / "*.json"),
                ]
            )
            if r.returncode != 0:
                print(f"refusing: backfill failed\n{r.stdout}{r.stderr}")
                return 2
            if not has_phase_split(json.loads(newest_path.read_text(encoding="utf-8"))):
                print("refusing: the backfill ran but the newest snapshot still has no split")
                return 2

    # 2. C-3.
    r = run(
        [
            sys.executable,
            str(REPO_ROOT / "contracts" / "check.py"),
            "--validate",
            *(str(p) for p in series),
            "--schema",
            "cost_model.schema.json",
        ]
    )
    if r.returncode != 0:
        print(f"refusing: the snapshots do not satisfy C-3\n{r.stdout[-2000:]}")
        return 2
    print(f"{len(series)} snapshot(s) satisfy C-3")

    new = json.loads(newest_path.read_text(encoding="utf-8"))

    # 2b. Is the grid self-consistent?
    bad = inversions(new)
    if bad:
        print(f"refusing: {newest_path.name} is inverted in concurrency")
        for line in bad:
            print(f"  {line}")
        print(
            "  A request cannot be served faster by sharing the engine with more requests. "
            "Recalibrate this class rather than promoting a grid that says it can."
        )
        return 2
    index = pool_load.snapshot_index(SNAPSHOT_ROOT)
    old = newest_in_class(index, node_class)
    if old is not None and old["snapshot_id"] == new["snapshot_id"]:
        print(f"{new['snapshot_id']} is already the newest in {node_class}; nothing to promote")
        return 0
    if old is not None and old["measured_at_unix"] >= new["measured_at_unix"]:
        print(
            f"refusing: {old['snapshot_id']} in the contracts is newer than this run's "
            f"{new['snapshot_id']}, so promoting would not change what the scheduler serves"
        )
        return 2

    # The answer to "did the node change".
    if old is not None:
        print(f"\n{node_class}: {old['snapshot_id']}\n  -> {new['snapshot_id']}")
        for line in compare(old, new):
            print("  " + line)
        print()

    # 3. Promote.
    dest = SNAPSHOT_ROOT / node_class
    for p in series:
        target = dest / p.name
        if target.exists() and target.read_bytes() != p.read_bytes():
            print(f"refusing: {target} exists with different content")
            return 2
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        for p in series:
            shutil.copy2(p, dest / p.name)
    print(f"{'would promote' if args.dry_run else 'promoted'} {len(series)} snapshot(s) to {dest}")

    # 4. Repoint.
    repointed = configs_naming(old["snapshot_id"]) if old is not None else []
    for path in repointed:
        text = path.read_text(encoding="utf-8")
        if not args.dry_run:
            path.write_text(text.replace(old["snapshot_id"], new["snapshot_id"]), encoding="utf-8")
        print(f"  {'would repoint' if args.dry_run else 'repointed'} {path.relative_to(REPO_ROOT)}")
    if args.dry_run:
        return 0

    # 5. Every campaign still checks.
    failed = []
    for path in sorted(CONFIGS.glob("hw_*.json")):
        r = run(
            [
                "uv",
                "run",
                "--project",
                "dataplane",
                "python",
                str(TOOLS / "hw_runs.py"),
                str(path),
                "--dry-run",
            ]
        )
        if r.returncode != 0:
            failed.append((path.name, (r.stdout + r.stderr).strip().splitlines()[-1:]))
    if failed:
        print("campaigns that no longer dry-run:")
        for name, tail in failed:
            print(f"  {name}: {tail}")
        return 1
    print("every hw_*.json dry-runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
