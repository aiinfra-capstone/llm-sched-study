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
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

# Reuse anchors' helpers for manifest building and trace checking
from dataplane.harness import gen_trace

# `python -m tools.sweep` puts the repository root on the path rather than this directory,
# so the sibling tools are not importable by name without saying where they are.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ensure_trace as trace_check
import pool_load

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"
TRACE_DEFAULT = REPO_ROOT / "runs" / "traces" / "anchor_1b.trace.jsonl"

# Minimal sweep config. Can be overridden by a JSON file passed via --config
DEFAULT_GRID = {
    "policies": ["round_robin", "jsq", "static_weighted", "wjsq", "threshold", "ect"],
    "staleness_s": [0.0, 0.1, 0.5, 1.0],
    "rate_scale": [0.8, 1.15, 1.45],  # quiet/light/mid, heavy is saturated and excluded from H2/H3
    # The same sweep at fixed load rather than at fixed rate. A pool holding a slower node
    # has less capacity, so one rate_scale across the R axis walks up the utilisation curve
    # as R grows, and a policy difference read off it is part R and part load. A utilisation
    # point resolves its own rate per R from the synthesised pool's capacity, so every R is
    # compared at the same distance from saturation. E4.1 runs both: fixed lambda answers
    # "what happens to this pool as its slow node gets slower", fixed utilisation answers
    # "what does heterogeneity cost at a load the pool can carry".
    "pool_utilisation": [],
    "R": [1, 2, 4],  # small subset for smoke; full grid up to 100 for paper
    # R_decode / R_prefill of the synthesised node. 1.0 scales both phases alike, which is
    # every sweep before this axis existed. Above 1 the slow node loses decode faster than
    # prefill (a CPU against a GPU); below 1 it loses prefill faster (an older GPU).
    "phase_skew": [1.0],
}


# The ECT mode a sweep gives its ECT points when neither the sweep config nor the base
# manifest names one. The scheduler refuses an ECT run with no mode, so a sweep states it.
DEFAULT_ECT_MODE = "known"
ECT_MODES = ("known", "unknown")
# Config keys that only ECT reads. Other policies do not carry them.
_ECT_KEYS = ("ect_mode", "ect_prior_output_len")


def load_snapshots_by_id(root: Path = SNAPSHOT_ROOT) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for p in sorted(root.glob("*/*.json")):
        snap = json.loads(p.read_text(encoding="utf-8"))
        index[snap["snapshot_id"]] = snap
    return index


def synthesize_snapshot(
    base: dict[str, Any], factor: float, phase_skew: float = 1.0
) -> dict[str, Any]:
    """Scale a measured C-3 snapshot to synthesise a new node class at R=factor.

    With `phase_skew` 1.0 every service-time field is multiplied by `factor` and
    tokens_per_s divided by it, which preserves the shape of the grid. tokens_per_s is decode
    tok/s, so under a skew it is divided by the decode factor.

    Prefill and decode do not slow down at the same rate across real machines, and that is
    the elevation's claim, so a single factor cannot synthesise the pools it is about. With
    `phase_skew` s, prefill is scaled by factor / sqrt(s) and decode by factor * sqrt(s):
    their ratio is s and their geometric mean is still `factor`. Each cell's service time
    then scales by what its own prefill and decode split implies, so a prompt-heavy cell and
    a generation-heavy cell of the same node slow down by different amounts, as they do on
    hardware. A cell without the split cannot be skewed, and refuses rather than falling
    back to a uniform factor that would contradict the id it is written under.

    A synthesised snapshot gets its own id and node_class so figures can label it:
    synth_<base>__x<factor>, with _skew<s> appended when s is not 1, so every sweep written
    before the skew axis keeps its ids. Provenance is left as the measured snapshot's so the
    file remains valid per C-3 (additional fields would break the frozen schema and the
    strict Java parser). Both parameters are recoverable from the id.

    Every sweep point gets two nodes, including R=1 where the second node is
    an unscaled copy with its own id. A one node R=1 point next to two node
    R>1 points would read pool size as an effect of R, so homogeneity means
    two identical nodes, not one node.
    """
    if phase_skew <= 0:
        raise ValueError(
            f"phase_skew is a ratio of two slowdowns and must be > 0, got {phase_skew}"
        )
    suffix = "" if phase_skew == 1.0 else f"_skew{phase_skew:g}"
    new_id = f"synth_{base['snapshot_id']}__x{factor:g}{suffix}"
    # Deep copy via json round-trip to avoid mutating the base
    new = json.loads(json.dumps(base))
    new["snapshot_id"] = new_id
    new["node_class"] = f"{base['node_class']}__synth_x{factor:g}{suffix}"
    prefill_factor = factor / phase_skew**0.5
    decode_factor = factor * phase_skew**0.5
    for e in new["entries"]:
        prefill = e.get("prefill_ms_mean")
        decode = e.get("decode_ms_mean")
        if phase_skew == 1.0:
            cell_factor = factor
        elif prefill is None or decode is None or prefill + decode <= 0:
            raise ValueError(
                f"{base['snapshot_id']} has a cell with no prefill/decode split "
                f"({e['prompt_bucket']}, {e['output_bucket']}, c={e['concurrency']}), so "
                f"phase_skew {phase_skew:g} cannot be synthesised from it"
            )
        else:
            cell_factor = (prefill * prefill_factor + decode * decode_factor) / (prefill + decode)
        for f in ("service_ms_mean", "service_ms_p50", "service_ms_p95"):
            e[f] = round(e[f] * cell_factor, 4)
        # tokens_per_s is decode tok/s (C-3), so it answers to the decode factor alone. Dividing
        # by the cell's blended factor made a skewed node's decode rate move with its prefill.
        e["tokens_per_s"] = round(e["tokens_per_s"] / decode_factor, 4)
        if prefill is not None:
            e["prefill_ms_mean"] = round(prefill * prefill_factor, 4)
        if decode is not None:
            e["decode_ms_mean"] = round(decode * decode_factor, 4)
    # Keep provenance as measured (so file validates); synthesis is evident from id/node_class
    return new


def ensure_trace(
    trace_path: Path,
    trace_config: Path,
    anchors_dir: Path | None = None,
    *,
    base_manifest: dict[str, Any] | None = None,
) -> str:
    """Return the sha256 of a trace the base manifest (or the anchors) replayed.

    A missing trace is regenerated at the generator commit those manifests recorded, and
    kept only when its hash matches. A trace that does not match raises
    `ensure_trace.TraceMismatch`: a sweep over a workload the base never ran would compare
    the simulator against a different arrival process. With no manifest to check against,
    a trace on disk is hashed as it stands.
    """
    if base_manifest is not None:
        manifests = [base_manifest]
    elif anchors_dir is not None and anchors_dir.exists():
        manifests = [json.loads(p.read_text()) for p in sorted(anchors_dir.glob("*/manifest.json"))]
    else:
        manifests = []
    if trace_path.exists():
        if not manifests:
            return hashlib.sha256(trace_path.read_bytes()).hexdigest()
        sha = trace_check.verify_present(trace_path, manifests)
        print(f"trace present and matches its manifests: {trace_path} {sha[:12]}")
        return sha
    config = json.loads(trace_config.read_text())
    sha = trace_check.ensure(config, trace_path, manifests)
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
    trace_path: Path | None = None,
    phase_skew: float = 1.0,
    base_rate: float | None = None,
    ect_mode: str | None = None,
) -> dict[str, Any]:
    """Build a C-6 manifest for one sweep point, reusing anchors' manifest builder shape.

    `base_rate` is the trace's long-run arrival rate (`gen_trace.mean_rate`), so `lambda`
    is the rate the point offers. Without it the base config's `lambda_base` is used,
    which is only right for a Poisson trace.

    An ECT point carries `ect_mode`: the one passed, else the base config's. With neither
    it is refused, since SimApp refuses an ECT run with no mode. Other policies carry no
    ECT keys, whatever the base run was. main() checks the mode's value before any point.
    """
    config = dict(base_manifest.get("config", {}))
    new_config = dict(config)
    if policy == "ect":
        mode = ect_mode if ect_mode is not None else config.get("ect_mode")
        if mode is None:
            raise ValueError(
                "an ECT sweep point needs an ect_mode: name one in the sweep config, or "
                "sweep from a base manifest that has one"
            )
        new_config["ect_mode"] = mode
    else:
        for key in _ECT_KEYS:
            new_config.pop(key, None)
    new_config["staleness_s"] = staleness_s
    new_config["policy"] = policy
    new_config["R_target"] = R
    new_config["phase_skew"] = phase_skew
    new_config["rate_scale"] = rate_scale
    new_manifest = dict(base_manifest)
    new_manifest["policy"] = policy
    new_manifest["staleness_s"] = staleness_s
    if base_rate is None:
        arrival = base_manifest.get("config", {}).get("arrival", {})
        base_rate = float(arrival.get("lambda_base", 0.9)) if isinstance(arrival, dict) else 0.9
    new_manifest["lambda"] = round(base_rate * rate_scale, 6)
    if synthesized_snapshots:
        new_manifest["cost_model_snapshots"] = synthesized_snapshots
    new_manifest["config"] = new_config
    if trace_sha256:
        new_manifest["trace_sha256"] = trace_sha256
        # The path has to name the file the sha was taken from. Hardcoding TRACE_DEFAULT
        # here while hashing args.trace meant any run with --trace wrote a manifest naming
        # one trace beside another's sha256, and join refuses that pair outright ("the
        # workload is not the one this run replayed"), excluding the whole sweep. Invisible
        # on the default path, fatal the first time W5 sweeps the workload-shape profiles.
        new_manifest["trace_path"] = str(trace_path if trace_path is not None else TRACE_DEFAULT)
    return new_manifest


def find_mvn() -> str:
    """Locate the maven binary on PATH so the runner works on Linux and Windows alike."""
    found = shutil.which("mvn")
    if found is None:
        raise RuntimeError("mvn not found on PATH; install Maven to run DES sweeps")
    return found


def overlay_dir(
    snapshots: list[dict[str, Any]],
    cost_models_dir: Path,
    cache: dict[str, Path] | None = None,
) -> Path:
    """A cost-model directory holding the measured snapshots plus these synthesised ones.

    SimApp walks a single directory, so a synthesised snapshot can only reach it through a
    copy of the real tree with the extra files dropped in. Built once per set of
    synthesised ids and cached, because the sweep reuses the same synthesised snapshot for
    every policy, staleness and load at one R: rebuilding it per point copied every
    committed snapshot again each time, which over the full grid is tens of thousands of
    file copies to produce the same directory over and over.

    The caller owns cleanup, since the cache outlives any one run.
    """
    key = "|".join(sorted(snap["snapshot_id"] for snap in snapshots))
    if cache is not None and key in cache:
        return cache[key]
    target = Path(tempfile.mkdtemp(prefix="sweep_costmodels_"))
    for src in cost_models_dir.rglob("*.json"):
        dst = target / src.relative_to(cost_models_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    for snap in snapshots:
        (target / f"{snap['snapshot_id']}.json").write_text(
            json.dumps(snap, indent=2), encoding="utf-8"
        )
    if cache is not None:
        cache[key] = target
    return target


def run_one_des(
    trace: Path,
    manifest: dict[str, Any],
    out_dir: Path,
    cost_models_dir: Path = SNAPSHOT_ROOT,
    extra_snapshots: list[dict[str, Any]] | None = None,
    overlay_cache: dict[str, Path] | None = None,
    *,
    deterministic: bool = True,
) -> Path:
    """Run SimApp for one manifest (DES, not hardware) and return the run dir.

    Deterministic by default, for F-20 parity. `deterministic=False` lets SimApp draw its
    i.i.d. lognormal service noise from the snapshot's `stochastic.sigma`.
    """
    # Write manifest to temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(manifest, tf, indent=2)
        manifest_path = Path(tf.name)
    # If we have synthesised snapshots, write them to a temp cost_models dir that overlays the real one.
    # SimApp walks a single dir, so the temp dir holds copies of the real files plus the synthesised ones.
    if extra_snapshots:
        cost_models_arg = overlay_dir(extra_snapshots, cost_models_dir, overlay_cache)
    else:
        cost_models_arg = cost_models_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    # Plain argument list with no shell, so this runs the same on Linux and Windows.
    noise = " --deterministic" if deterministic else ""
    cmd = [
        find_mvn(),
        "-q",
        "-f",
        str(REPO_ROOT / "controlplane" / "pom.xml"),
        "exec:java",
        "-Dexec.mainClass=com.sched.sim.SimApp",
        f"-Dexec.args={trace} {manifest_path} {out_dir}{noise} --cost-models {cost_models_arg}",
    ]
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT / "controlplane"),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    manifest_path.unlink(missing_ok=True)
    if (
        result.returncode != 0
        or "Error during simulation" in result.stdout
        or "Error during simulation" in result.stderr
    ):
        raise RuntimeError(
            f"SimApp failed for {manifest.get('run_id')}: {result.stdout[-1000:]} {result.stderr[-1000:]}"
        )
    # Verify output
    if not list(out_dir.glob("scheduler_*.jsonl")):
        raise RuntimeError(f"No scheduler log in {out_dir}, SimApp output: {result.stdout[-500:]}")
    return out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MPR-3 DES sweep runner (policy, R, staleness, load)")
    ap.add_argument("--config", type=Path, help="sweep config JSON (if omitted, uses defaults)")
    ap.add_argument(
        "--out",
        type=Path,
        required=True,
        help="output root for sweep runs (will contain many run dirs + runset.parquet)",
    )
    ap.add_argument("--trace", type=Path, default=TRACE_DEFAULT, help="trace to replay")
    ap.add_argument(
        "--trace-config",
        type=Path,
        default=REPO_ROOT / "dataplane" / "configs" / "trace_anchor_1b.json",
        help="C-2 config to regenerate trace if missing",
    )
    ap.add_argument(
        "--anchors",
        type=Path,
        default=REPO_ROOT / "runs" / "anchors",
        help="anchors dir for trace hash check",
    )
    ap.add_argument("--cost-models", type=Path, default=SNAPSHOT_ROOT, help="cost models dir")
    ap.add_argument("--dry-run", action="store_true", help="print what would be run, don't execute")
    ap.add_argument(
        "--settle-s",
        type=float,
        default=0.0,
        help="sleep between points; 0 by default because each point is a separate "
        "SimApp process with no engine state to drain, unlike the anchors loop this "
        "borrowed the shape from",
    )
    ap.add_argument(
        "--keep-going",
        action="store_true",
        help="run remaining points after a failure instead of stopping at the first one",
    )
    ap.add_argument(
        "--stochastic",
        action="store_true",
        help="let SimApp draw its lognormal service noise; the default is --deterministic",
    )
    ap.add_argument(
        "--k-slow",
        type=int,
        default=1,
        help="number of slow nodes in pool (1 fast + k slow, N >= 2)",
    )
    args = ap.parse_args(argv)

    # Load sweep grid. The config is read once here and again per point, for k_slow, so
    # it is bound on both paths: without one, every lookup falls back to the flag.
    sweep_cfg: dict[str, Any] = {}
    grid = DEFAULT_GRID
    if args.config and args.config.exists():
        sweep_cfg = json.loads(args.config.read_text())
        # Merged over the default rather than replacing it, so a config that overrides
        # one axis ("just sweep R further") keeps the other three instead of raising
        # KeyError on the first axis it does not mention.
        grid = {**DEFAULT_GRID, **sweep_cfg.get("grid", {})}

    # The base is a measured run: the config's base_manifest, else the first anchor. With
    # neither there is no pool to sweep, and a made-up base would name a snapshot, a host
    # and a trace hash that no run ever had.
    base_manifest_path = sweep_cfg.get("base_manifest")
    if base_manifest_path:
        base_manifest = json.loads(Path(base_manifest_path).read_text())
    else:
        anchor_manifests = (
            sorted(Path(args.anchors).glob("*/manifest.json"))
            if Path(args.anchors).exists()
            else []
        )
        if not anchor_manifests:
            print(
                f"refusing: no base manifest in the config and no anchor manifests under "
                f"{args.anchors}"
            )
            return 1
        base_manifest = json.loads(anchor_manifests[0].read_text())
    if "cost_model_snapshots" in sweep_cfg:
        base_manifest["cost_model_snapshots"] = sweep_cfg["cost_model_snapshots"]

    # The mode every ECT point runs in: the sweep config's, else the base run's, else the
    # default. Checked here, before any point runs, rather than failing every ECT point.
    base_config = base_manifest.get("config", {})
    ect_mode = sweep_cfg.get("ect_mode") or base_config.get("ect_mode") or DEFAULT_ECT_MODE
    ect_prior = sweep_cfg.get("ect_prior_output_len", base_config.get("ect_prior_output_len"))
    if "ect" in grid["policies"]:
        if ect_mode not in ECT_MODES:
            print(f"refusing: ect_mode must be one of {list(ECT_MODES)}, got {ect_mode!r}")
            return 1
        if ect_mode == "unknown" and not (
            isinstance(ect_prior, int) and not isinstance(ect_prior, bool) and ect_prior > 0
        ):
            print(
                "refusing: ect_mode unknown needs a positive integer ect_prior_output_len, "
                f"got {ect_prior!r}"
            )
            return 1

    print(
        f"Sweep grid: policies={grid['policies']} R={grid['R']} phase_skew={grid['phase_skew']} "
        f"staleness={grid['staleness_s']} rate_scale={grid['rate_scale']} "
        f"pool_utilisation={grid.get('pool_utilisation') or []}"
    )
    total = (
        len(grid["policies"])
        * len(grid["R"])
        * len(grid["phase_skew"])
        * len(grid["staleness_s"])
        * (len(grid["rate_scale"]) + len(grid.get("pool_utilisation") or []))
    )
    print(f"Total points: {total} -> {args.out}")

    if not args.trace.exists():
        print(f"Trace missing at {args.trace}, regenerating from {args.trace_config}")
    try:
        trace_sha256 = ensure_trace(args.trace, args.trace_config, base_manifest=base_manifest)
    except ValueError as e:  # TraceMismatch, or manifests that record no generator sha
        print(f"refusing: {e}")
        return 1

    if args.dry_run:
        print("Dry run, not executing")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    index = load_snapshots_by_id(args.cost_models)
    print(f"Using trace {args.trace} sha {trace_sha256[:12]}")

    # Flat grid sorted by rate_scale so the slowest load runs first. Anchors.py
    # does this because a cold engine pays a first request cost; the DES has no
    # engine but the same order keeps sweep output comparable with anchor output.
    # A load point is either a rate_scale, applied as it stands, or a pool utilisation,
    # which becomes a rate_scale once the pool for this R is known.
    loads = [("rate_scale", float(v)) for v in sorted(grid["rate_scale"])] + [
        ("pool_utilisation", float(v)) for v in sorted(grid.get("pool_utilisation") or [])
    ]
    points = [
        (R, skew, load, staleness, policy)
        for R in sorted(grid["R"])
        for skew in sorted(grid["phase_skew"])
        for load in loads
        for staleness in sorted(grid["staleness_s"])
        for policy in grid["policies"]
    ]

    # A utilisation point needs the trace's length mix, to price a request against the
    # pool's cost models. Every point needs the trace's long-run arrival rate: it turns a
    # target into a scale, and a scale into the `lambda` the manifest reports.
    header, _ = gen_trace.load(args.trace, expect_sha256=trace_sha256)
    length_dist: dict[str, Any] = header["length_dist"]
    base_rate = gen_trace.mean_rate(header["arrival"])

    run_dirs: list[Path] = []
    failures: list[str] = []
    # One overlay per set of synthesised snapshots, reused across every policy, staleness
    # and load at the same R, and removed together at the end.
    overlay_cache: dict[str, Path] = {}
    last_load = loads[-1] if loads else None
    for point_no, (R, skew, load, staleness, policy) in enumerate(points):
        load_kind, load_value = load
        rate = load_value if load_kind == "rate_scale" else None
        # Every point runs a two node pool. At R=1 the second node is an
        # unscaled copy with its own id; a one node R=1 pool next to two node
        # R>1 pools would confound pool size with heterogeneity.
        cost_snaps = dict(base_manifest["cost_model_snapshots"])
        base_snap_id = next(iter(cost_snaps.values()))
        base_snap = index.get(base_snap_id)
        if base_snap is None:
            print(f"  failed: base snapshot {base_snap_id} not in {args.cost_models}")
            failures.append(
                f"R={R} skew={skew} policy={policy} staleness={staleness} {load_kind}="
                f"{load_value:g}: missing base snapshot"
            )
            if not args.keep_going:
                break
            continue
        try:
            synth = synthesize_snapshot(base_snap, float(R), float(skew))
        except ValueError as e:
            print(f"  failed: {e}")
            failures.append(f"R={R} skew={skew}: {e}")
            if not args.keep_going:
                break
            continue
        # Persist synthesised snapshot under the sweep out dir (not contracts/),
        # so runset can find it via a custom index without polluting measured data
        synth_dir = args.out / "synthesised"
        synth_dir.mkdir(parents=True, exist_ok=True)
        synth_path = synth_dir / f"{synth['snapshot_id']}.json"
        if not synth_path.exists():
            synth_path.write_text(json.dumps(synth, indent=2), encoding="utf-8")
            print(f"  wrote synthesised {synth['snapshot_id']} -> {synth_path}")
        index[synth["snapshot_id"]] = synth
        extra_for_this_run = [synth]
        k_slow = int(sweep_cfg.get("k_slow", getattr(args, "k_slow", 1)))
        nodes = list(base_manifest["nodes"])
        skew_tag = "" if skew == 1.0 else f"_skew{skew:g}"
        for i in range(1, k_slow + 1):
            slow_node_id = f"slow_{R:g}x{skew_tag}" if k_slow == 1 else f"slow_{R:g}x{skew_tag}_{i}"
            cost_snaps[slow_node_id] = synth["snapshot_id"]
            if base_manifest["nodes"]:
                slow_node = dict(base_manifest["nodes"][0])
                slow_node["node_id"] = slow_node_id
                slow_node["host"] = (
                    f"slow-{R:g}x{skew_tag}-{i}" if k_slow > 1 else f"slow-{R:g}x{skew_tag}"
                )
                slow_node["gpu"] = "synthesised"
                nodes.append(slow_node)
        if rate is None:
            # Resolved against this R's pool, not the measured one: the slow node here is
            # synthesised, and its capacity is what decides the rate this point offers.
            capacity = pool_load.pool_capacity(nodes, cost_snaps, length_dist, index)
            target_rps, note = pool_load.rate_for({load_kind: load_value}, capacity)
            rate = target_rps / base_rate
            print(
                f"  {load_kind} {load_value:g} at R={R:g} -> {target_rps:.3f} req/s "
                f"(rate_scale {rate:.4f}, {note})"
            )
        manifest = build_sweep_manifest(
            base_manifest,
            policy,
            staleness,
            rate,
            R,
            cost_snaps,
            trace_sha256,
            args.trace,
            phase_skew=float(skew),
            base_rate=base_rate,
            ect_mode=ect_mode,
        )
        if policy == "ect" and ect_prior is not None:
            manifest["config"]["ect_prior_output_len"] = ect_prior
        manifest["config"]["load_target"] = {load_kind: load_value}
        manifest["config"]["operating_point"] = (
            f"r{load_value:g}" if load_kind == "rate_scale" else f"u{load_value:g}"
        )
        manifest["nodes"] = nodes
        load_tag = f"r{load_value:g}" if load_kind == "rate_scale" else f"u{load_value:g}"
        manifest["run_id"] = (
            f"sweep_R{R:g}{skew_tag}_{policy}_s{staleness}_{load_tag}_{point_no:04d}"
        )

        run_dir = args.out / manifest["run_id"]
        print(
            f"Running {manifest['run_id']}  R={R} skew={skew} policy={policy} "
            f"staleness={staleness} {load_kind}={load_value:g} -> {run_dir}"
        )
        try:
            run_one_des(
                trace=args.trace,
                manifest=manifest,
                out_dir=run_dir,
                cost_models_dir=args.cost_models,
                extra_snapshots=extra_for_this_run,
                overlay_cache=overlay_cache,
                deterministic=not args.stochastic,
            )
            run_dirs.append(run_dir)
        except RuntimeError as e:
            print(f"  failed: {e}")
            failures.append(f"{manifest['run_id']}: {e}")
            if not args.keep_going:
                break
            continue
        if args.settle_s and load != last_load:
            time.sleep(args.settle_s)

    for overlay in overlay_cache.values():
        shutil.rmtree(overlay, ignore_errors=True)

    print(f"\n{len(run_dirs)}/{total} sweep points completed, written under {args.out}")
    if failures and not args.keep_going:
        print(
            f"Stopped at the first failure ({len(failures)} failed). Rerun with --keep-going to run the rest."
        )
        return 1
    if len(run_dirs) < total:
        print(f"Only {len(run_dirs)} of {total} points produced output, failing.")
        return 1

    # Aggregate to runset.parquet with an index that includes synthesised snapshots
    try:
        from dataplane.pipeline import runset

        # Rebuild index to include synthesised snapshots written under <out>/synthesised/
        full_index = dict(index)
        synth_glob = (
            sorted((args.out / "synthesised").glob("*.json"))
            if (args.out / "synthesised").exists()
            else []
        )
        for sp in synth_glob:
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
                full_index[s["snapshot_id"]] = s
            except (OSError, json.JSONDecodeError, KeyError) as e:
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
    except (ValueError, OSError) as e:
        print(f"Aggregation failed: {e}")
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
