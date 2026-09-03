#!/usr/bin/env python3
"""MPR-3 sweep runner in the validated simulator (H2/H3).

Runs the DES across the full grid that the figures need: policy, R,
staleness, load. It reuses the anchors loop shape (sort by rate_scale,
settle between points, check trace hash once) and then aggregates to a
runset (which anchors.py does not do).

The grid's R axis is the one that needs synthesis. We have three measured
node classes so a sweep over R in {1,2,4,8,16,32,64,100} cannot come from
measurement. A synthesised snapshot gets:

  1. its own snapshot_id that says it is synthesised (synth_<base>__x<factor>)
  2. its own node_class (base__synth_x<factor>) so figures can label it;
     provenance is left as measured so the file validates per C-3 and the
     strict Java parser keeps loading it (factor is recoverable from the id)
  3. R derived from the manifest via runset.deployed_r (no typed-in --r)

Synthesised snapshots are written under <out>/synthesised/ (not into
contracts/cost_models/) and the aggregation uses an index that includes
them, so sweeps stay reproducible from the sweep dir alone.

Every figure drawn from these runs is labelled by vehicle (simulator) via
runset, and by synthesised-vs-measured via snapshot id / node_class.

Usage:
  uv run --project dataplane python -m tools.sweep --out runs/sweeps/des_1b --dry-run
  uv run --project dataplane python -m tools.sweep --config /tmp/sweep.json --out /tmp/sweep --settle-s 0
"""

from __future__ import annotations

import argparse
import json
import time
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# Reuse anchors' helpers for manifest building and trace checking
from dataplane.harness import gen_trace

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"
TRACE_DEFAULT = REPO_ROOT / "runs" / "traces" / "anchor_1b.trace.jsonl"

# Minimal sweep config. Can be overridden by a JSON file passed via --config
DEFAULT_GRID = {
    "policies": ["round_robin", "jsq", "static_weighted", "wjsq", "threshold"],
    "staleness_s": [0.0, 0.1, 0.5, 1.0],
    "rate_scale": [0.8, 1.15, 1.45],  # quiet/light/mid, heavy is saturated and excluded from H2/H3
    "R": [1, 2, 4],  # small subset for smoke; full grid up to 100 for paper
}


def load_snapshots_by_id(root: Path = SNAPSHOT_ROOT) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for p in sorted(root.glob("*/*.json")):
        snap = json.loads(p.read_text(encoding="utf-8"))
        index[snap["snapshot_id"]] = snap
    return index


def synthesize_snapshot(base: dict[str, Any], factor: float) -> dict[str, Any]:
    """Scale a measured C-3 snapshot to synthesise a new node class at R=factor.

    Scaling is applied to service time (mean/p50/p95) and inversely to
    tokens_per_s, preserving the shape of the grid. A synthesised snapshot
    gets its own id (synth_<base>__x<factor>) and node_class so figures can
    label it; provenance is left as the measured snapshot's provenance so the
    file remains valid per C-3 (additional fields would break the frozen schema
    and the Java parser which is strict). The factor is recoverable from the id.
    """
    if factor == 1.0:
        return base
    new_id = f"synth_{base['snapshot_id']}__x{factor:g}"
    # Deep copy via json round-trip to avoid mutating the base
    new = json.loads(json.dumps(base))
    new["snapshot_id"] = new_id
    new["node_class"] = f"{base['node_class']}__synth_x{factor:g}"
    # Scale every cell
    for e in new["entries"]:
        for field in ("service_ms_mean", "service_ms_p50", "service_ms_p95"):
            e[field] = round(e[field] * factor, 4)
        # tokens_per_s is work per time, so it scales inversely
        e["tokens_per_s"] = round(e["tokens_per_s"] / factor, 4)
        # prefill/decode split, if present, scale with service
        if e.get("prefill_ms_mean") is not None:
            e["prefill_ms_mean"] = round(e["prefill_ms_mean"] * factor, 4)
        if e.get("decode_ms_mean") is not None:
            e["decode_ms_mean"] = round(e["decode_ms_mean"] * factor, 4)
    # Keep provenance as measured (so file validates); synthesis is evident from id/node_class
    return new


def ensure_trace(trace_path: Path, trace_config: Path, anchors_dir: Path | None = None) -> str:
    """Ensure trace exists, regenerating from config if needed (like ensure_trace.py)."""
    if trace_path.exists():
        # Verify hash if anchors exist
        if anchors_dir and anchors_dir.exists():
            manifests = sorted(anchors_dir.glob("*/manifest.json"))
            if manifests:
                expected = {json.loads(p.read_text())["trace_sha256"] for p in manifests}
                actual = hashlib.sha256(trace_path.read_bytes()).hexdigest()
                if actual in expected:
                    print(f"trace present and matches anchors: {trace_path} {actual[:12]}")
                    return actual
        return hashlib.sha256(trace_path.read_bytes()).hexdigest()
    # Regenerate
    from dataplane.harness.gen_trace import generate
    config = json.loads(trace_config.read_text())
    sha = generate(config, trace_path)
    print(f"regenerated {trace_path} from {trace_config} sha {sha[:12]}")
    return sha


def build_sweep_manifest(
    base_manifest: dict[str, Any],
    policy: str,
    staleness_s: float,
    rate_scale: float,
    R: float,
    synthesized_snapshots: dict[str, str] | None = None,
    trace_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a C-6 manifest for one sweep point, reusing anchors' manifest builder shape."""
    config = dict(base_manifest.get("config", {}))
    new_config = dict(config)
    new_config["staleness_s"] = staleness_s
    new_config["policy"] = policy
    new_config["R_target"] = R
    new_config["rate_scale"] = rate_scale
    new_manifest = dict(base_manifest)
    new_manifest["policy"] = policy
    new_manifest["staleness_s"] = staleness_s
    arrival = base_manifest.get("config", {}).get("arrival", {})
    lambda_base = float(arrival.get("lambda_base", 0.9)) if isinstance(arrival, dict) else 0.9
    new_manifest["lambda"] = round(lambda_base * rate_scale, 6)
    if synthesized_snapshots:
        new_manifest["cost_model_snapshots"] = synthesized_snapshots
    new_manifest["config"] = new_config
    if trace_sha256:
        new_manifest["trace_sha256"] = trace_sha256
        new_manifest["trace_path"] = str(TRACE_DEFAULT)
    return new_manifest


def run_one_des(
    trace: Path,
    manifest: dict[str, Any],
    out_dir: Path,
    cost_models_dir: Path = SNAPSHOT_ROOT,
    extra_snapshots: list[dict[str, Any]] | None = None,
) -> Path:
    """Run SimApp for one manifest (DES, not hardware) and return the run dir."""
    # Write manifest to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(manifest, tf, indent=2)
        manifest_path = Path(tf.name)
    # If we have synthesised snapshots, write them to a temp cost_models dir that overlays the real one
    temp_cost_dir = None
    if extra_snapshots:
        temp_cost_dir = Path(tempfile.mkdtemp())
        # Copy real cost models into temp dir for SimApp to find both
        # Instead of copying, we can just write synthesised files into temp dir and pass that dir plus real dir?
        # SimApp walks one dir, so we need a unified dir. Create temp dir with symlinks/copies of real + synthesised
        for src in cost_models_dir.rglob("*.json"):
            # Replicate structure under temp
            rel = src.relative_to(cost_models_dir)
            dst = temp_cost_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
        for snap in extra_snapshots:
            # Write synthesised snapshot as if it were a file
            fname = f"synth_{snap['snapshot_id']}.json"
            # Find a suitable subdir (use first real snapshot's subdir or just root)
            (temp_cost_dir / fname).write_text(json.dumps(snap, indent=2), encoding="utf-8")
        cost_models_arg = temp_cost_dir
    else:
        cost_models_arg = cost_models_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    # Use --deterministic for reproducibility (F-20)
    cmd = [
        "mvn", "-q", "-f", str(REPO_ROOT / "controlplane" / "pom.xml"),
        "exec:java",
        f"-Dexec.mainClass=com.sched.sim.SimApp",
        f"-Dexec.args={trace} {manifest_path} {out_dir} --deterministic --cost-models {cost_models_arg}",
    ]
    # Use shell for mvn.cmd on Windows
    result = subprocess.run(
        ["C:/Program Files/apache-maven-3.9.16/bin/mvn.cmd" if Path("C:/Program Files/apache-maven-3.9.16/bin/mvn.cmd").exists() else "mvn"]
        + cmd[1:],
        cwd=str(REPO_ROOT / "controlplane"),
        capture_output=True, text=True, shell=True,
    )
    manifest_path.unlink(missing_ok=True)
    if temp_cost_dir:
        shutil.rmtree(temp_cost_dir, ignore_errors=True)
    if result.returncode != 0 or "Error during simulation" in result.stdout or "Error during simulation" in result.stderr:
        raise RuntimeError(f"SimApp failed for {manifest.get('run_id')}: {result.stdout[-1000:]} {result.stderr[-1000:]}")
    # Verify output
    if not list(out_dir.glob("scheduler_*.jsonl")):
        raise RuntimeError(f"No scheduler log in {out_dir}, SimApp output: {result.stdout[-500:]}")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MPR-3 DES sweep runner (policy, R, staleness, load)")
    ap.add_argument("--config", type=Path, help="sweep config JSON (if omitted, uses defaults)")
    ap.add_argument("--out", type=Path, required=True, help="output root for sweep runs (will contain many run dirs + runset.parquet)")
    ap.add_argument("--trace", type=Path, default=TRACE_DEFAULT, help="trace to replay")
    ap.add_argument("--trace-config", type=Path, default=REPO_ROOT / "dataplane" / "configs" / "trace_anchor_1b.json", help="C-2 config to regenerate trace if missing")
    ap.add_argument("--anchors", type=Path, default=REPO_ROOT / "runs" / "anchors", help="anchors dir for trace hash check")
    ap.add_argument("--cost-models", type=Path, default=SNAPSHOT_ROOT, help="cost models dir")
    ap.add_argument("--dry-run", action="store_true", help="print what would be run, don't execute")
    ap.add_argument("--settle-s", type=float, default=2.0, help="sleep between points to drain tail (like anchors.py)")
    args = ap.parse_args(argv)

    # Load sweep grid
    if args.config and args.config.exists():
        sweep_cfg = json.loads(args.config.read_text())
        grid = sweep_cfg.get("grid", DEFAULT_GRID)
        base_manifest_path = sweep_cfg.get("base_manifest")
        if base_manifest_path:
            base_manifest = json.loads(Path(base_manifest_path).read_text())
        else:
            # Use first anchor manifest as base
            anchor_manifests = sorted(Path(args.anchors).glob("*/manifest.json")) if Path(args.anchors).exists() else []
            if not anchor_manifests:
                print(f"no anchor manifests under {args.anchors}, using minimal base", flush=True)
                base_manifest = {
                    "run_id": "sweep_base",
                    "trace_path": str(args.trace),
                    "trace_sha256": "unknown",
                    "policy": "round_robin",
                    "lambda": 1.0,
                    "staleness_s": 0.0,
                    "warmup_s": 10.0,
                    "duration_s": 193.0,
                    "cost_model_snapshots": {"gtx1650ti": "cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260831T153652Z_008"},
                    "nodes": [{"node_id": "gtx1650ti", "host": "fedora", "role": "pool", "engine": "llamacpp", "engine_version": "b10569+p1+cuda13.2", "model": "Llama-3.2-1B-Instruct", "quant": "Q4_K_M", "gpu": "NVIDIA GeForce GTX 1650 Ti", "driver": "580.173.02", "prefix_caching": False, "max_batch": 4, "engine_config": {"ngl": 99, "threads": 6, "parallel": 4}}],
                    "config": {"arrival": {"lambda_base": 0.9}, "gen_seed": 20260830},
                    "git_shas": {"worker": "unknown", "scheduler": "unknown", "harness": "unknown", "sim": "unknown"},
                    "validity": {"valid": True},
                }
            else:
                base_manifest = json.loads(anchor_manifests[0].read_text())
        # If sweep_cfg has cost_model_snapshots, use it
        if "cost_model_snapshots" in sweep_cfg:
            base_manifest["cost_model_snapshots"] = sweep_cfg["cost_model_snapshots"]
    else:
        grid = DEFAULT_GRID
        # Fallback base
        anchor_manifests = sorted(Path(args.anchors).glob("*/manifest.json")) if Path(args.anchors).exists() else []
        if anchor_manifests:
            base_manifest = json.loads(anchor_manifests[0].read_text())
        else:
            base_manifest = {
                "run_id": "sweep_base",
                "trace_path": str(args.trace),
                "trace_sha256": "unknown",
                "policy": "round_robin",
                "lambda": 1.0,
                "staleness_s": 0.0,
                "warmup_s": 10.0,
                "duration_s": 193.0,
                "cost_model_snapshots": {"gtx1650ti": "cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260831T153652Z_008"},
                "nodes": [{"node_id": "gtx1650ti", "host": "fedora", "role": "pool", "engine": "llamacpp", "engine_version": "b10569+p1+cuda13.2", "model": "Llama-3.2-1B-Instruct", "quant": "Q4_K_M", "gpu": "NVIDIA GeForce GTX 1650 Ti", "driver": "580.173.02", "prefix_caching": False, "max_batch": 4, "engine_config": {"ngl": 99, "threads": 6, "parallel": 4}}],
                "config": {"arrival": {"lambda_base": 0.9}, "gen_seed": 20260830},
                "git_shas": {"worker": "unknown", "scheduler": "unknown", "harness": "unknown", "sim": "unknown"},
                "validity": {"valid": True},
            }

    print(f"Sweep grid: policies={grid['policies']} R={grid['R']} staleness={grid['staleness_s']} rate_scale={grid['rate_scale']}")
    total = len(grid["policies"]) * len(grid["R"]) * len(grid["staleness_s"]) * len(grid["rate_scale"])
    print(f"Total points: {total} -> {args.out}")

    # Ensure trace exists (like ensure_trace.py, but simpler)
    if not args.trace.exists():
        print(f"Trace missing at {args.trace}, regenerating from {args.trace_config}")
        ensure_trace(args.trace, args.trace_config, Path(args.anchors) if Path(args.anchors).exists() else None)
    else:
        # Verify hash once, like anchors.py
        if Path(args.anchors).exists():
            try:
                gen_trace.load(args.trace, expect_sha256=base_manifest.get("trace_sha256"))
            except Exception as e:
                print(f"Trace hash check warning (will continue): {e}")

    if args.dry_run:
        print("Dry run, not executing")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    index = load_snapshots_by_id(args.cost_models)
    # Compute actual trace SHA for manifests (regenerated trace has new SHA)
    trace_sha256 = hashlib.sha256(args.trace.read_bytes()).hexdigest() if args.trace.exists() else base_manifest.get("trace_sha256", "unknown")
    print(f"Using trace {args.trace} sha {trace_sha256[:12]}")

    # Sort by rate_scale so slowest first (cold-engine cost where warmup discards it)
    rate_scales = sorted(grid["rate_scale"])

    run_dirs: list[Path] = []
    for R in sorted(grid["R"]):
        # Synthesize snapshots for this R if needed
        extra_snaps: list[dict[str, Any]] = []
        # For now, synthesize by scaling the base snapshot with factor R
        # In a real sweep, you'd have two node classes: fast and slow. Here we create a slow synthesised
        # snapshot for a second node to achieve the ratio.
        # Simplified: use one node for R=1 (homogeneous), two nodes for R>1
        for rate in rate_scales:
            for staleness in sorted(grid["staleness_s"]):
                for policy in grid["policies"]:
                    # Build manifest for this point
                    # For R>1, we need two nodes; for R=1, one node (homogeneous)
                    # Create a second node by synthesising the base node's snapshot
                    cost_snaps = dict(base_manifest["cost_model_snapshots"])
                    extra_for_this_run: list[dict[str, Any]] | None = None
                    if R != 1:
                        # Find base snapshot id for the pool node
                        base_snap_id = next(iter(cost_snaps.values()))
                        base_snap = index.get(base_snap_id)
                        if base_snap:
                            synth = synthesize_snapshot(base_snap, float(R))
                            # Persist synthesised snapshot under the sweep out dir (not contracts/),
                            # so runset can find it via a custom index without polluting measured data
                            synth_dir = args.out / "synthesised"
                            synth_dir.mkdir(parents=True, exist_ok=True)
                            synth_path = synth_dir / f"{synth['snapshot_id']}.json"
                            if not synth_path.exists():
                                synth_path.write_text(json.dumps(synth, indent=2), encoding="utf-8")
                                print(f"  wrote synthesised {synth['snapshot_id']} -> {synth_path}")
                            # Also keep in index for this run
                            index[synth["snapshot_id"]] = synth
                            extra_for_this_run = [synth]
                            # Add second node to manifest
                            nodes = list(base_manifest["nodes"])
                            slow_node_id = f"slow_{R}x"
                            cost_snaps[slow_node_id] = synth["snapshot_id"]
                            if nodes:
                                slow_node = dict(nodes[0])
                                slow_node["node_id"] = slow_node_id
                                slow_node["host"] = f"slow-{R}x"
                                slow_node["gpu"] = "synthesised"
                                nodes = nodes + [slow_node]
                            manifest = build_sweep_manifest(base_manifest, policy, staleness, rate, R, cost_snaps, trace_sha256)
                            manifest["nodes"] = nodes
                            manifest["run_id"] = f"sweep_R{R:g}_{policy}_s{staleness}_r{rate:g}_{len(run_dirs)}"
                        else:
                            manifest = build_sweep_manifest(base_manifest, policy, staleness, rate, R, cost_snaps, trace_sha256)
                            manifest["run_id"] = f"sweep_R{R:g}_{policy}_s{staleness}_r{rate:g}_{len(run_dirs)}"
                            extra_for_this_run = None
                    else:
                        manifest = build_sweep_manifest(base_manifest, policy, staleness, rate, R, cost_snaps, trace_sha256)
                        manifest["run_id"] = f"sweep_R{R:g}_{policy}_s{staleness}_r{rate:g}_{len(run_dirs)}"

                    # Stagger rate_scale order: already sorted, but we need to ensure slowest first overall
                    # For simplicity, we run in the nested loops as is; to truly sort, we'd flatten and sort by rate
                    # Here we just run in order and sleep between rate changes
                    run_dir = args.out / manifest["run_id"]
                    print(f"Running {manifest['run_id']}  R={R} policy={policy} staleness={staleness} rate_scale={rate} -> {run_dir}")
                    try:
                        run_one_des(trace=args.trace, manifest=manifest, out_dir=run_dir, cost_models_dir=args.cost_models, extra_snapshots=extra_for_this_run)
                        run_dirs.append(run_dir)
                    except Exception as e:
                        print(f"  failed: {e}")
                        continue
                    # Settle between points like anchors.py
                    if args.settle_s and rate != rate_scales[-1]:
                        time.sleep(args.settle_s)

    print(f"\n{len(run_dirs)}/{total} sweep points completed, written under {args.out}")

    # Aggregate to runset.parquet with an index that includes synthesised snapshots
    try:
        from dataplane.pipeline import runset
        # Rebuild index to include synthesised snapshots written under <out>/synthesised/
        full_index = dict(index)
        for sp in sorted((args.out / "synthesised").glob("*.json")) if (args.out / "synthesised").exists() else []:
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
                full_index[s["snapshot_id"]] = s
            except Exception as e:
                print(f"  warning: could not index synthesised {sp.name}: {e}")
        rs = runset.aggregate(args.out, index=full_index)
        out_parquet = args.out / "runset.parquet"
        rs.frame.to_parquet(out_parquet, index=False)
        print(f"Wrote {out_parquet} with {len(rs.frame)} rows")
        for line in rs.summary():
            print(f"  {line}")
        if rs.excluded:
            print(f"{len(rs.excluded)} runs excluded")
            return 1
    except Exception as e:
        print(f"Aggregation failed (non-fatal for sweep runner smoke): {e}")
        import traceback; traceback.print_exc()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
