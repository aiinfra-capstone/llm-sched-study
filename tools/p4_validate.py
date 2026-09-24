#!/usr/bin/env python3
"""P4: simulator against hardware on the two-node pool, per policy.

Takes a hardware run folder (manifest, trace, the cost model snapshots the
manifest names) and runs SimApp with the same policy, lambda, staleness and
seed, then compares simulated and measured p50/p95 with tools/f23_compare.py
in --hardware-run-dir mode (warmup + nearest-rank kept identical).

Run data is not in git (runs/ and *.parquet are ignored). Point this at an
archive of runs/exp/ + runs/worker_logs/ (e.g. the drive copy) — it is read
only, never committed. Sim outputs go under --out (default: temp dir outside
the repo unless given).

Two criteria. The absolute one, each run within ±25% on p50 and p95, is reported
and decides nothing on its own. With --contrasts, the simulated runs are joined
into a run set, summarised by tools/campaign_summary.py exactly as the hardware
was, and compared on the contrasts analysis-plan 6.6 requires (ranking, WJSQ over
JSQ, the H1 interaction) by tools/contrast_check.py. That verdict is the one that
decides whether any simulator figure is citable. Run it against the three shape
run sets, which played no part in building the simulator.

Each run replays the trace its own manifest names, so the shape campaigns and the
seeded campaigns, whose repeats each have their own trace, need no --trace.

Usage:
  uv run --project dataplane python tools/p4_validate.py \
    --hardware-root runs/exp/phase_balanced_1650ti_3050 \
    --out /tmp/p4_balanced --contrasts
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"


def find_mvn() -> str:
    found = shutil.which("mvn")
    if found is None:
        raise RuntimeError("mvn not found on PATH; install Maven to run SimApp")
    return found


def run_one_sim(
    trace: Path, hw_manifest: Path, out_dir: Path, cost_models: Path
) -> tuple[bool, str]:
    """Run SimApp for one hardware manifest. Returns (ok, log tail).

    `mvn exec:java -Dexec.args=...` splits on spaces, so a manifest under a path
    with spaces ("drive files/...") arrives truncated. Stage it to a space-free
    copy beside the sim output; content is identical, only the filename changes.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = out_dir / "hw_manifest.json"
    try:
        if str(hw_manifest) != str(staged):
            shutil.copyfile(hw_manifest, staged)
    except OSError as e:
        return False, f"could not stage {hw_manifest}: {e}"
    manifest_arg = staged
    cmd = [
        find_mvn(),
        "-q",
        "-f",
        str(REPO_ROOT / "controlplane" / "pom.xml"),
        "exec:java",
        "-Dexec.mainClass=com.sched.sim.SimApp",
        f"-Dexec.args={trace} {manifest_arg} {out_dir} --deterministic --cost-models {cost_models}",
    ]
    r = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT / "controlplane"),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    tail = (r.stdout[-1500:] + "\n" + r.stderr[-1500:]).strip()
    if (
        r.returncode != 0
        or "Error during simulation" in r.stdout
        or "Error during simulation" in r.stderr
    ):
        return False, tail
    if not list(out_dir.glob("scheduler_*.jsonl")):
        return False, f"no scheduler log in {out_dir}\n{tail[-800:]}"
    if not list(out_dir.glob("client_*.jsonl")):
        return False, f"no client log in {out_dir}\n{tail[-800:]}"
    return True, tail


def compare_one(
    hw_manifest: Path, hw_dir: Path, sim_dir: Path, tolerance: float
) -> tuple[int, str]:
    """Run f23_compare in P4 mode. Returns (exit code, output)."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools" / "f23_compare.py"),
        "--manifest",
        str(hw_manifest),
        "--sim-dir",
        str(sim_dir),
        "--hardware-run-dir",
        str(hw_dir),
        "--tolerance",
        str(tolerance),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out


def contrasts(hw_root: Path, out_root: Path) -> int:
    """Join the simulated runs, summarise them like the hardware, and apply 6.6."""
    hw_summary = hw_root / "summary.json"
    if not hw_summary.is_file():
        print(f"no {hw_summary}; run tools/campaign_summary.py on the hardware run set first")
        return 1
    steps = [
        ["uv", "run", "--project", "dataplane", "runset", str(out_root)],
        [
            "uv",
            "run",
            "--project",
            "dataplane",
            "python",
            str(REPO_ROOT / "tools" / "campaign_summary.py"),
            str(out_root / "runset.parquet"),
            "--out",
            str(out_root / "summary"),
        ],
    ]
    for cmd in steps:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO_ROOT)
        if r.returncode != 0:
            print(f"P4 contrasts FAILED at {' '.join(cmd[4:6])}:\n{(r.stdout + r.stderr)[-1500:]}")
            return 1
    r = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "contrast_check.py"),
            "--hardware",
            str(hw_summary),
            "--simulator",
            str(out_root / "summary.json"),
            "--out",
            str(out_root / "contrast_check"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(r.stdout)
    return r.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P4: replay hardware manifests through SimApp and compare"
    )
    ap.add_argument(
        "--hardware-root",
        type=Path,
        required=True,
        help="directory containing hardware run dirs (each with manifest.json)",
    )
    ap.add_argument(
        "--trace",
        type=Path,
        default=None,
        help="replay this trace for every run; default is the trace each manifest names",
    )
    ap.add_argument(
        "--contrasts",
        action="store_true",
        help="after the replays, check analysis-plan 6.6 against <hardware-root>/summary.json",
    )
    ap.add_argument("--cost-models", type=Path, default=SNAPSHOT_ROOT)
    ap.add_argument(
        "--out", type=Path, default=None, help="where SimApp outputs go (default: temp dir)"
    )
    ap.add_argument("--tolerance", type=float, default=25.0)
    ap.add_argument(
        "--keep-going", action="store_true", help="compare remaining runs after a SimApp failure"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    hw_root: Path = args.hardware_root
    # SimApp runs with cwd=controlplane, and -Dexec.args splits on spaces, so every
    # path SimApp sees must be absolute and space-free. The hardware root itself may
    # contain spaces (it is only read via Python), but trace / cost-models / out must not.
    fixed_trace: Path | None = None
    if args.trace is not None:
        fixed_trace = args.trace if args.trace.is_absolute() else (REPO_ROOT / args.trace)
    cost_models: Path = (
        args.cost_models if args.cost_models.is_absolute() else (REPO_ROOT / args.cost_models)
    )
    if not hw_root.exists():
        print(f"hardware root not found: {hw_root}")
        return 1
    # Support both: --hardware-root = campaign dir (with run subdirs),
    # or a single run dir (with manifest.json directly).
    if (hw_root / "manifest.json").exists():
        run_dirs = [hw_root]
    else:
        run_dirs = sorted(
            p for p in hw_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()
        )
    if not run_dirs:
        print(f"no runs with manifest.json under {hw_root}")
        return 1
    print(f"P4: {len(run_dirs)} hardware runs under {hw_root}")
    print(f"  trace: {fixed_trace if fixed_trace else 'the one each manifest names'}")
    print(f"  cost models: {cost_models}")

    if args.dry_run:
        for d in run_dirs[:5]:
            print(f"  would replay {d.name}")
        if len(run_dirs) > 5:
            print(f"  ... and {len(run_dirs) - 5} more")
        return 0

    out_root = args.out or Path(tempfile.mkdtemp(prefix="p4_sims_"))
    if not out_root.is_absolute():
        out_root = REPO_ROOT / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"  sim out: {out_root}")

    results: list[dict] = []
    failures: list[str] = []
    for run_dir in run_dirs:
        hw_manifest = run_dir / "manifest.json"
        try:
            man = json.loads(hw_manifest.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"\n=== {run_dir.name} ===\n  FAIL: could not read manifest: {e}")
            failures.append(f"{run_dir.name}: bad manifest")
            if not args.keep_going:
                break
            continue
        run_id = man.get("run_id", run_dir.name)
        trace = fixed_trace or (REPO_ROOT / man["trace_path"])
        if not trace.exists():
            print(f"\n=== {run_id} ===\n  FAIL: trace not found: {trace}")
            failures.append(f"{run_id}: trace not found")
            if not args.keep_going:
                break
            continue
        policy = man.get("policy", "?")
        point = man.get("config", {}).get("operating_point", "?")
        sim_dir = out_root / f"{run_id}_sim"
        print(f"\n=== {run_id}  policy={policy} point={point} ===")
        # Skip finished sims so a stopped campaign restarts cheaply.
        if list(sim_dir.glob("client_*.jsonl")) and (sim_dir / "manifest.json").exists():
            print(f"  sim exists, reusing {sim_dir}")
        else:
            ok, tail = run_one_sim(trace, hw_manifest, sim_dir, cost_models)
            if not ok:
                print(f"  FAIL: SimApp failed:\n  {tail[-1200:]}")
                failures.append(f"{run_id}: SimApp failed")
                results.append(
                    {
                        "run_id": run_id,
                        "policy": policy,
                        "point": point,
                        "rc": 1,
                        "err_p50": None,
                        "err_p95": None,
                    }
                )
                if not args.keep_going:
                    break
                continue
            print(f"  sim ok -> {sim_dir.name}")
        rc, out = compare_one(hw_manifest, run_dir, sim_dir, args.tolerance)
        print("  " + out.replace("\n", "\n  "))
        # Parse errors for the summary table.
        err_p50 = err_p95 = None
        for line in out.splitlines():
            if "Error:" in line and "p50=" in line:
                try:
                    # "  Error:    p50=  +1.2%   p95=  -3.4% ..."
                    parts = line.replace("%", " ").split()
                    # parts: ['Error:', 'p50=', '+1.2', 'p95=', '-3.4', ...]
                    i50 = parts.index("p50=") + 1
                    i95 = parts.index("p95=") + 1
                    err_p50 = float(parts[i50])
                    err_p95 = float(parts[i95])
                except (ValueError, IndexError):
                    err_p50 = err_p95 = None
        if rc == 2 and err_p50 is None:
            # f23_compare exits 2 when a run is outside tolerance, but Python also exits 2
            # when it cannot open the script and argparse exits 2 on a usage error. An exit
            # of 2 that printed no comparison is a failure to compare, which counts towards
            # neither pass nor miss.
            rc = 1
            print("  FAIL: nothing was compared, so this run is an error and not a miss")
        results.append(
            {
                "run_id": run_id,
                "policy": policy,
                "point": point,
                "rc": rc,
                "err_p50": err_p50,
                "err_p95": err_p95,
            }
        )
        if rc == 1:
            failures.append(f"{run_id}: comparison failed")
            if not args.keep_going:
                break

    # Summary per policy x point.
    print(f"\n--- P4 summary (sim vs hardware, tolerance ±{args.tolerance:.0f}%) ---")
    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        by_cell[(str(r["policy"]), str(r["point"]))].append(r)
    npass = nfail = nerr = 0
    for (pol, pt), rows in sorted(by_cell.items()):
        passed = sum(1 for x in rows if x["rc"] == 0)
        outside = sum(1 for x in rows if x["rc"] == 2)
        err = sum(1 for x in rows if x["rc"] == 1)
        npass += passed
        nfail += outside
        nerr += err
        errs = [(x["run_id"], x["err_p50"], x["err_p95"]) for x in rows if x["rc"] == 2]
        print(f"  {pol:15s} {pt!s:8s}  pass {passed}/{len(rows)}  outside {outside}  error {err}")
        for rid, e50, e95 in errs:
            print(f"      MISS {rid}: p50={e50:+.1f}% p95={e95:+.1f}%")
    print(
        f"\n{len(results)}/{len(run_dirs)} runs compared: {npass} pass, {nfail} outside tolerance, {nerr} errors"
    )
    if failures and not args.keep_going:
        print(f"Stopped early ({len(failures)} failures). Rerun with --keep-going to see the rest.")
    if args.contrasts and not nerr:
        return contrasts(hw_root, out_root)
    if nerr:
        print("P4 FAILED: comparison errors (see above) — not citable until they are fixed.")
        return 1
    if nfail:
        print("P4 MIXED: some runs outside ±25% — explain the misses before citing the simulator.")
        return 2
    print("P4 PASSED: every run within tolerance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
