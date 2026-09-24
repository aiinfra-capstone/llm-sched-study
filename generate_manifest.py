#!/usr/bin/env python3
"""Generate a 2-node C-6 manifest (GPU node + CPU node) for a live scheduler demonstration.

Usage:
    uv run --project dataplane generate_manifest.py <policy> [<run_name>] --snapshot amd4600h=<id>
    uv run --project dataplane generate_manifest.py --all --snapshot amd4600h=<id>
    uv run --project dataplane generate_manifest.py jsq demonstrate_jsq --borrow-gpu-snapshot

The node blocks, trace and load come from a committed quiet anchor run. Everything that
describes *this* run (started_unix, config_hash, git_shas, the co-location count) is built
fresh by `dataplane.harness.manifest.build`, the same builder the replay client uses.

The CPU node needs its own C-3 snapshot. The scheduler takes a node's admissibility bounds
and its capability from the snapshot the manifest names, and a node with no snapshot is
never admissible. Handing it the GPU node's snapshot makes the scheduler admit and weight
the CPU node as if it were the GPU, while the manifest records a calibration that never
happened. So that is refused unless --borrow-gpu-snapshot says so explicitly, and then the
borrowing is written into `config`, where it also moves `config_hash`.

The validity block counts co-location from the node blocks. The send-lag, drop and restart
counters are only known after the run; the replay client's validity.json is the record of
those.
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys

from dataplane.harness import launch
from dataplane.harness import manifest as manifest_mod
from jsonschema import Draft202012Validator

VALID_POLICIES = ("round_robin", "jsq", "static_weighted", "wjsq", "threshold")
GPU_NODE_ID = "gtx1650ti"
CPU_NODE_ID = "amd4600h"
CPU_ENGINE_CONFIG = {"ngl": 0, "threads": 6, "parallel": 4}


def find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """The nearest ancestor holding contracts/, so the script works from any location."""
    for d in (start, *start.parents):
        if (d / "contracts" / "schemas" / "manifest.schema.json").is_file():
            return d
    raise FileNotFoundError(f"no repo root (a directory with contracts/) above {start}")


def get_base_manifest(repo_root: pathlib.Path) -> dict:
    """Load the newest committed quiet 1B anchor manifest as the template."""
    candidates = sorted(repo_root.glob("runs/anchors/anchor1b_quiet_*/manifest.json"))
    if not candidates:
        raise FileNotFoundError(
            "no runs/anchors/anchor1b_quiet_*/manifest.json. The template has to be a 1B quiet "
            "anchor; another anchor would carry a different model or load."
        )
    return json.loads(candidates[-1].read_text())


def snapshot_index(repo_root: pathlib.Path) -> dict[str, str]:
    """snapshot_id -> node_class for every committed C-3 snapshot."""
    index = {}
    for p in (repo_root / "contracts" / "cost_models").glob("*/*.json"):
        d = json.loads(p.read_text())
        index[d["snapshot_id"]] = d["node_class"]
    return index


def check_snapshot_matches_node(node_class: str, node: dict) -> None:
    """Refuse a snapshot whose node class names a different -ngl than the node runs."""
    ngl = node["engine_config"]["ngl"]
    if f"_ngl{ngl}_" not in f"_{node_class}_":
        raise ValueError(
            f"node {node['node_id']} runs ngl {ngl}, but the snapshot is for node class "
            f"{node_class}"
        )


def parse_snapshot_args(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        node_id, sep, snap = pair.partition("=")
        if not sep or not node_id or not snap:
            raise ValueError(f"--snapshot takes NODE=SNAPSHOT_ID, got {pair!r}")
        out[node_id] = snap
    return out


def build_manifest_for_policy(
    base: dict,
    policy: str,
    run_name: str,
    snapshots: dict[str, str],
    known_snapshots: dict[str, str],
    staleness_s: float = 0.0,
    threshold_t: float = 50.0,
    borrow_gpu_snapshot: bool = False,
) -> dict:
    """Construct a 2-node manifest (GPU + CPU) for the given policy and run name."""
    if policy not in VALID_POLICIES:
        raise ValueError(f"Invalid policy '{policy}'. Must be one of: {', '.join(VALID_POLICIES)}")

    pool = [n for n in base["nodes"] if n.get("role") == "pool"]
    if len(pool) != 1 or pool[0]["node_id"] != GPU_NODE_ID:
        raise ValueError(f"the template must have exactly one pool node, {GPU_NODE_ID}")
    gpu_node = copy.deepcopy(pool[0])
    cpu_node = copy.deepcopy(gpu_node)
    cpu_node["node_id"] = CPU_NODE_ID
    cpu_node["gpu"] = "none"
    cpu_node["engine_config"] = dict(CPU_ENGINE_CONFIG)
    nodes = [gpu_node, cpu_node]

    unknown = set(snapshots) - {GPU_NODE_ID, CPU_NODE_ID}
    if unknown:
        raise ValueError(f"--snapshot names nodes this pool does not have: {sorted(unknown)}")

    config = copy.deepcopy(base["config"])
    config.pop("threshold_t", None)
    config.pop("borrowed_snapshots", None)
    config["staleness_s"] = staleness_s
    if policy == "threshold":
        config["threshold_t"] = threshold_t

    chosen = {GPU_NODE_ID: base["cost_model_snapshots"][GPU_NODE_ID], **snapshots}
    if CPU_NODE_ID not in chosen:
        if not borrow_gpu_snapshot:
            raise ValueError(
                f"no C-3 snapshot for {CPU_NODE_ID}. Pass --snapshot {CPU_NODE_ID}=<id> once the "
                "CPU 1B class is calibrated, or --borrow-gpu-snapshot to run a demo that "
                "records the borrowing"
            )
        chosen[CPU_NODE_ID] = chosen[GPU_NODE_ID]
        config["borrowed_snapshots"] = {CPU_NODE_ID: chosen[GPU_NODE_ID]}

    for node in nodes:
        snap = chosen[node["node_id"]]
        if snap not in known_snapshots:
            raise ValueError(f"snapshot {snap} is not under contracts/cost_models/")
        if node["node_id"] in config.get("borrowed_snapshots", {}):
            continue
        check_snapshot_matches_node(known_snapshots[snap], node)

    return manifest_mod.build(
        run_id=run_name,
        config=config,
        trace_path=base["trace_path"],
        trace_sha256=base["trace_sha256"],
        validity=manifest_mod.Validity(colocated_nodes=launch.colocated_count(nodes)),
        nodes=nodes,
        vehicle=base.get("vehicle", "hardware"),
        policy=policy,
        cost_model_snapshots=chosen,
    )


def validate_schema(m: dict, repo_root: pathlib.Path) -> list[str]:
    """Every C-6 schema violation in the manifest, as readable lines. Empty means valid."""
    schema = json.loads((repo_root / "contracts/schemas/manifest.schema.json").read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(m), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]


def generate_single(
    repo_root: pathlib.Path,
    policy: str,
    run_name: str,
    snapshots: dict[str, str],
    out_file: pathlib.Path | None = None,
    staleness_s: float = 0.0,
    threshold_t: float = 50.0,
    borrow_gpu_snapshot: bool = False,
) -> pathlib.Path:
    m = build_manifest_for_policy(
        base=get_base_manifest(repo_root),
        policy=policy,
        run_name=run_name,
        snapshots=snapshots,
        known_snapshots=snapshot_index(repo_root),
        staleness_s=staleness_s,
        threshold_t=threshold_t,
        borrow_gpu_snapshot=borrow_gpu_snapshot,
    )
    problems = validate_schema(m, repo_root)
    if problems:
        raise ValueError("manifest does not conform to C-6:\n  " + "\n  ".join(problems))

    if out_file is None:
        out_file = repo_root / "runs" / run_name / "manifest.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(m, indent=2) + "\n")
    print(f"[OK] Manifest generated for policy '{policy}' -> {out_file}")
    if m["validity"]["colocated_nodes"]:
        print(
            f"     both nodes report host '{m['nodes'][0]['host']}': colocated_nodes="
            f"{m['validity']['colocated_nodes']}, so this run is a demonstration, not a "
            "measurement"
        )
    if "borrowed_snapshots" in m["config"]:
        print(f"     {CPU_NODE_ID} borrows the GPU snapshot; admission and weights treat it as GPU")
    return out_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 2-node (GPU + CPU) manifest for live scheduler demonstration."
    )
    policies = ", ".join(VALID_POLICIES)
    parser.add_argument("pos_policy", nargs="?", help=f"Policy name ({policies})")
    parser.add_argument("pos_run_name", nargs="?", help="Run name / run_id")
    parser.add_argument("-p", "--policy", dest="flag_policy", help="Policy name")
    parser.add_argument("-r", "--run-name", "--run_name", dest="flag_run_name", help="Run id")
    parser.add_argument(
        "-o", "--out", type=pathlib.Path, help="output file (default runs/<run_name>/manifest.json)"
    )
    parser.add_argument("-s", "--staleness-s", type=float, default=0.0, help="default 0.0")
    parser.add_argument(
        "-t", "--threshold-t", type=float, default=50.0, help="Threshold(T) cutoff, default 50.0"
    )
    parser.add_argument(
        "--snapshot",
        action="append",
        default=[],
        metavar="NODE=SNAPSHOT_ID",
        help=f"C-3 snapshot for a node; required for {CPU_NODE_ID} unless borrowing",
    )
    parser.add_argument(
        "--borrow-gpu-snapshot",
        action="store_true",
        help=f"give {CPU_NODE_ID} the GPU snapshot and record that in config (demo only)",
    )
    parser.add_argument(
        "--all", action="store_true", help="all 5 policies, run names demonstrate_<policy>"
    )
    args = parser.parse_args(argv)
    repo_root = find_repo_root(pathlib.Path(__file__).resolve().parent)

    try:
        snapshots = parse_snapshot_args(args.snapshot)
        common = {
            "snapshots": snapshots,
            "staleness_s": args.staleness_s,
            "threshold_t": args.threshold_t,
            "borrow_gpu_snapshot": args.borrow_gpu_snapshot,
        }
        if args.all:
            if args.out or args.flag_policy or args.pos_policy:
                parser.error("--all writes runs/demonstrate_<policy>/; drop --out and the policy")
            for pol in VALID_POLICIES:
                generate_single(repo_root, pol, f"demonstrate_{pol}", **common)
            print("\nAll 5 demonstration manifests generated.")
            return 0

        policy = args.flag_policy or args.pos_policy
        if not policy:
            parser.error(f"Policy is required (one of: {policies}) or use --all")
        if policy not in VALID_POLICIES:
            parser.error(f"Invalid policy '{policy}'. Must be one of: {policies}")
        run_name = args.flag_run_name or args.pos_run_name or f"demonstrate_{policy}"
        generate_single(repo_root, policy, run_name, out_file=args.out, **common)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
