#!/usr/bin/env python3
"""MPR-2 hardware runs: one live scheduler per run, and every log in that run's directory.

The anchors driver replays one trace at several operating points against a scheduler that
is already up. That worked against the fixture scheduler, which writes no log. The live
scheduler takes its run_id, policy, staleness and log file from the manifest it starts
with, so a single scheduler process cannot serve a second run: its decisions land under
the first run's name and the join finds none for the rest. This driver starts a fresh
scheduler for every run instead.

Per run, in order:

    write <run_dir>/manifest.pre.json         the scheduler's input
    start LiveSchedulerApp                    --log-dir <run_dir>, one --worker per node
    replay the trace open-loop                client log into <run_dir>
    settle                                    let the tail finish and report completion
    stop the scheduler                        its shutdown hook closes the decision log
    pull worker_<node>_<run_id>.jsonl         from each node's --log-dir, over rsync
    write <run_dir>/manifest.json             the post-run record runset reads

`manifest.pre.json` is never discovered by `runset`, so a run that dies halfway leaves
nothing that can be mistaken for a data point. A run directory that already holds a
`manifest.json` and a client log is skipped, so a campaign that stopped can be restarted
with the same command.

Run order: within each repeat, operating points go slowest first (the anchors driver's
reason: a cold engine's first requests belong under the lightest load), and the order of
(workload, policy) pairs is shuffled per point from a fixed seed. Throughput drifts over
minutes on these nodes, so running the policies, or the workload shapes, in the same order
every time would put a drift effect inside the comparison.

Replication: a campaign names a trace config rather than a trace, and one seed per repeat.
Repeat k generates its own trace from seed k and starts the scheduler with its own seed, so
repeats sample different arrival sequences and different routing random draws. Every policy
in repeat k replays the same trace with the same scheduler seed, which keeps the policies
paired inside a repeat. The first pair's campaigns replayed one trace with the scheduler's
default seed in every repeat; the repeats of those campaigns only sample hardware jitter.
A config that still names a single `trace` runs that way and says so.

Several workloads can share one campaign (`workloads`), each writing to its own run set.
They share the per-repeat seed, so their arrivals stay paired, and they interleave within
each point, so a shape comparison is not also a comparison of hours of the night.

Engine restarts: before and after every run the driver reads each node's llama-server
process id, start time and command line (over ssh for a remote node). A change counts as
an engine restart and invalidates the run. The command line is recorded in the manifest,
which is how `--cache-ram 0` and `-c` become part of the record rather than of a runbook.

Usage:
  uv run --project dataplane python tools/hw_runs.py dataplane/configs/hw_mpr2_lan_3050.json --dry-run
  uv run --project dataplane python tools/hw_runs.py dataplane/configs/hw_mpr2_lan_3050.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pool_load
from dataplane.harness import gen_trace, launch
from dataplane.harness import manifest as manifest_mod
from dataplane.harness import replay as replay_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"
POLICIES = (
    "round_robin",
    "jsq",
    "jsq_fastfirst",
    "static_weighted",
    "static_weighted_wrr",
    "wjsq",
    "threshold",
    "ect",
)
LOAD_TARGETS = ("lambda_rps", "pool_utilisation", "slow_node_utilisation")
# What a capability arm may change: the numbers the calibrated policies are given, and
# nothing else. Keys the control plane reads (com.sched.core.Capability, Policies.ECT).
ARM_KEYS = (
    "capability_override",
    "capability_concurrency",
    "capability_mode",
    "ect_mode",
    "ect_prior_output_len",
    "threshold_t",
)
READY_LINE = "Live Control Plane active"
RESOLVED_LINE = "Resolving snapshot"


class EngineUnreadable(RuntimeError):
    """A pool node's engine could not be read before a run.

    Stops the campaign whatever `--keep-going` says. `--keep-going` is for a run that came
    out unusable; this is the pool being unreachable, and every run after it would be one
    more directory nobody can vouch for.
    """


@dataclass
class Point:
    """A load point: a fixed rate_scale, or a target resolved per workload from the cost models.

    `target` is one of `{"lambda_rps": x}`, `{"pool_utilisation": u}` or
    `{"slow_node_utilisation": u}` (see tools/pool_load.py). A utilisation target gives each
    workload its own rate, which is how the shape comparison is run at matched load.
    """

    name: str
    rate_scale: float | None = None
    target: dict[str, float] | None = None
    note: str = ""

    @property
    def order(self) -> float:
        if self.rate_scale is not None:
            return self.rate_scale
        assert self.target is not None
        return float(next(iter(self.target.values())))


@dataclass
class Arm:
    """One setting of what the calibrated policies are told, run as its own set of runs.

    The value-of-calibration curve is a sweep over the capability ratio WJSQ is given, and
    a ratio is a property of a run rather than of a campaign, so it is an axis here. An arm
    with an empty name is the campaign's own setting and leaves run ids unchanged.

    `policies` narrows the arm to the policies the setting can reach: JSQ ignores capability
    entirely, so re-running it under seven ratios buys nothing but machine time.
    """

    name: str
    config: dict[str, Any] = field(default_factory=dict)
    policies: list[str] | None = None


@dataclass
class Workload:
    """One trace family and the run set it writes to.

    Either `trace_config` (a C-2 trace config; one trace is generated per repeat seed) or a
    fixed `trace` with its `trace_sha256` (every repeat replays the same file).
    """

    name: str
    out_root: Path
    trace_config: dict[str, Any] | None = None
    trace_config_path: Path | None = None
    trace: Path | None = None
    trace_sha256: str | None = None


@dataclass
class Campaign:
    tag: str
    workloads: list[Workload]
    nodes: list[dict[str, Any]]
    workers: dict[str, dict[str, str]]
    cost_model_snapshots: dict[str, str]
    policies: list[str]
    points: list[Point]
    staleness_s: list[float] = field(default_factory=lambda: [0.0])
    threshold_t: float = 50.0
    repeats: int = 1
    warmup_s: float = 0.0
    settle_s: float = 20.0
    scheduler_port: int = 50051
    scheduler_host: str = "127.0.0.1"
    bind: str = "0.0.0.0:50071"
    advertise: str | None = None
    clock_sync: dict[str, Any] | None = None
    capability_mode: str = "service"
    capability_override: dict[str, float] | None = None
    capability_concurrency: int | None = None
    ect_mode: str | None = None
    ect_prior_output_len: int | None = None
    arms: list[Arm] = field(default_factory=lambda: [Arm("")])
    repeat_seeds: list[int] | None = None
    scheduler_seeds: list[int] | None = None
    check_engine_restarts: bool = True
    engine_provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def trace(self) -> Path | None:
        return self.workloads[0].trace if len(self.workloads) == 1 else None

    @property
    def out_root(self) -> Path:
        return self.workloads[0].out_root

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Campaign:
        if "workloads" in d:
            workloads = [_workload(w) for w in d["workloads"]]
        else:
            workloads = [_workload({"name": "", **d})]
        return cls(
            tag=d["tag"],
            workloads=workloads,
            nodes=d["nodes"],
            workers=d["workers"],
            cost_model_snapshots=d["cost_model_snapshots"],
            policies=list(d.get("policies", POLICIES)),
            points=[_point(p) for p in d["points"]],
            staleness_s=[float(s) for s in d.get("staleness_s", [0.0])],
            threshold_t=float(d.get("threshold_t", 50.0)),
            repeats=int(d.get("repeats", 1)),
            warmup_s=float(d.get("warmup_s", 0.0)),
            settle_s=float(d.get("settle_s", 20.0)),
            scheduler_port=int(d.get("scheduler_port", 50051)),
            scheduler_host=d.get("scheduler_host", "127.0.0.1"),
            bind=d.get("bind", "0.0.0.0:50071"),
            advertise=d.get("advertise"),
            capability_mode=str(d.get("capability_mode", "service")),
            capability_override=d.get("capability_override"),
            capability_concurrency=d.get("capability_concurrency"),
            ect_mode=d.get("ect_mode"),
            ect_prior_output_len=d.get("ect_prior_output_len"),
            arms=[_arm(a) for a in d.get("capability_arms", [{}])],
            repeat_seeds=[int(x) for x in d["repeat_seeds"]] if "repeat_seeds" in d else None,
            scheduler_seeds=(
                [int(x) for x in d["scheduler_seeds"]] if "scheduler_seeds" in d else None
            ),
            check_engine_restarts=bool(d.get("check_engine_restarts", True)),
        )


def _arm(a: dict[str, Any]) -> Arm:
    """One capability arm, refusing a key no policy will read.

    The filter here used to drop anything outside `ARM_KEYS` on its way in, which meant a
    misspelled key never reached the refusal in `check_campaign`: the arm ran as the
    baseline, under its own name, and the ablation would have reported a ratio arm that
    was never applied as if it had been measured.
    """
    unknown = sorted(set(a) - {*ARM_KEYS, "name", "policies"})
    if unknown:
        raise ValueError(
            f"capability arm {a.get('name', '')!r} sets {', '.join(unknown)}, which no "
            f"policy reads. An arm may set {', '.join(ARM_KEYS)}"
        )
    return Arm(
        name=a.get("name", ""),
        config={k: v for k, v in a.items() if k in ARM_KEYS},
        policies=list(a["policies"]) if "policies" in a else None,
    )


def _point(d: dict[str, Any]) -> Point:
    target = {k: float(d[k]) for k in LOAD_TARGETS if k in d}
    settings = len(target) + int("rate_scale" in d)
    if settings != 1:
        raise ValueError(
            f"point {d.get('name')!r} needs exactly one of rate_scale, "
            f"{', '.join(LOAD_TARGETS)}; it sets {settings}"
        )
    if "rate_scale" in d:
        return Point(d["name"], rate_scale=float(d["rate_scale"]))
    return Point(d["name"], target=target)


def _workload(d: dict[str, Any]) -> Workload:
    if ("trace_config" in d) == ("trace" in d):
        raise ValueError(
            f"workload {d.get('name', '')!r} needs exactly one of trace_config and trace: a "
            "config generates one trace per repeat seed, a trace is one file every repeat "
            "replays"
        )
    if "trace_config" in d:
        cfg_path = _repo_path(d["trace_config"])
        return Workload(
            name=d.get("name", ""),
            out_root=_repo_path(d["out_root"]),
            trace_config=json.loads(cfg_path.read_text(encoding="utf-8")),
            trace_config_path=cfg_path,
        )
    return Workload(
        name=d.get("name", ""),
        out_root=_repo_path(d["out_root"]),
        trace=_repo_path(d["trace"]),
        trace_sha256=d["trace_sha256"],
    )


@dataclass
class Run:
    run_id: str
    policy: str
    arm: Arm
    point: Point
    staleness_s: float
    repeat: int
    sequence_no: int
    workload: Workload | None = None
    gen_seed: int | None = None
    scheduler_seed: int | None = None


@dataclass
class TraceFile:
    path: Path
    sha256: str
    header: dict[str, Any]


def _repo_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def snapshot_index(root: Path = SNAPSHOT_ROOT) -> dict[str, dict[str, Any]]:
    index = {}
    for p in sorted(root.glob("*/*.json")):
        snap = json.loads(p.read_text(encoding="utf-8"))
        index[snap["snapshot_id"]] = snap
    return index


PLACEHOLDER_SNAPSHOT = "REPLACE-WITH-THE-"


def check_campaign(
    c: Campaign,
    index: dict[str, dict[str, Any]],
    allow_colocation: bool,
    allow_placeholder: bool = False,
) -> None:
    """Every refusal that would otherwise surface as a bad run hours into the campaign.

    `allow_placeholder` is for `--dry-run` only. A campaign for a node class nobody has
    calibrated yet names its snapshot as `REPLACE-WITH-THE-...`, which is a note to a
    future self rather than a mistake; the plan is still worth printing and costing. A real
    run refuses, because a scheduler cannot admit a node without a C-3 snapshot.
    """
    launch.build_nodes(c.nodes, allow_colocation=allow_colocation)
    unknown = sorted(set(c.policies) - set(POLICIES))
    if unknown:
        raise ValueError(f"unknown policies {unknown}; expected some of {list(POLICIES)}")
    if c.repeats < 1:
        raise ValueError(f"repeats must be at least 1, got {c.repeats}")
    seeded = [w for w in c.workloads if w.trace_config is not None]
    if seeded and c.repeat_seeds is None:
        raise ValueError("a workload names a trace_config, so repeat_seeds is required")
    if c.repeat_seeds is not None:
        if len(c.repeat_seeds) != c.repeats:
            raise ValueError(
                f"repeat_seeds has {len(c.repeat_seeds)} seeds for {c.repeats} repeats"
            )
        if len(set(c.repeat_seeds)) != len(c.repeat_seeds):
            raise ValueError("repeat_seeds repeats a seed, so two repeats would share arrivals")
    if c.scheduler_seeds is not None:
        if len(c.scheduler_seeds) != c.repeats:
            raise ValueError(
                f"scheduler_seeds has {len(c.scheduler_seeds)} seeds for {c.repeats} repeats"
            )
        if any(not -(2**31) <= x < 2**31 for x in c.scheduler_seeds):
            raise ValueError("scheduler_seeds must fit a Java int")
    for arm in c.arms:
        unknown_keys = sorted(set(arm.config) - set(ARM_KEYS))
        if unknown_keys:
            raise ValueError(
                f"capability arm {arm.name!r} sets {unknown_keys}, which no policy reads; "
                f"an arm may set {list(ARM_KEYS)}"
            )
        unknown_policies = sorted(set(arm.policies or []) - set(c.policies))
        if unknown_policies:
            raise ValueError(
                f"capability arm {arm.name!r} names {unknown_policies}, which the campaign "
                "does not run"
            )
    arm_names = [a.name for a in c.arms]
    if len(set(arm_names)) != len(arm_names):
        raise ValueError(f"capability arm names must be distinct, got {arm_names}")
    if len(c.arms) > 1 and any(not n for n in arm_names):
        raise ValueError("every arm in a multi-arm campaign needs a name")

    names = [w.name for w in c.workloads]
    if len(set(names)) != len(names):
        raise ValueError(f"workload names must be distinct, got {names}")
    if len(c.workloads) > 1 and any(not n for n in names):
        raise ValueError("every workload in a multi-workload campaign needs a name")
    if not c.advertise:
        raise ValueError("advertise is required: the LAN address workers deliver responses to")

    pool = [n for n in c.nodes if n.get("role", "pool") == "pool"]
    for node in pool:
        node_id = node["node_id"]
        if node_id not in c.workers or "endpoint" not in c.workers[node_id]:
            raise ValueError(f"no worker endpoint for pool node {node_id!r}")
        if "logs" not in c.workers[node_id]:
            raise ValueError(f"no worker log location for pool node {node_id!r}")
        snap_id = c.cost_model_snapshots.get(node_id)
        if snap_id is None:
            raise ValueError(
                f"no C-3 snapshot for {node_id!r}; the scheduler never admits a node without one"
            )
        if snap_id.startswith(PLACEHOLDER_SNAPSHOT):
            if not allow_placeholder:
                raise ValueError(
                    f"{node_id!r} still names the placeholder snapshot {snap_id}: calibrate "
                    f"{node_id!r} and put the snapshot id here before running this campaign"
                )
            print(f"  {node_id}: names a placeholder snapshot, so this campaign cannot run yet")
            continue
        if snap_id not in index:
            raise ValueError(f"snapshot {snap_id} for {node_id!r} is not under {SNAPSHOT_ROOT}")
        node_class = index[snap_id]["node_class"]
        ngl = node["engine_config"]["ngl"]
        if f"_ngl{ngl}_" not in f"_{node_class}_":
            raise ValueError(f"{node_id!r} runs ngl {ngl} but {snap_id} is for {node_class}")
        # The scheduler quietly serves the newest snapshot in a class, whatever the manifest
        # names. Naming an older one would leave a record claiming a model that did not run.
        newest = max(
            (s for s in index.values() if s["node_class"] == node_class),
            key=lambda s: s["measured_at_unix"],
        )
        if newest["snapshot_id"] != snap_id:
            raise ValueError(
                f"{node_id!r} names {snap_id}, but the scheduler would serve the newest snapshot "
                f"in {node_class}, {newest['snapshot_id']}; name that one"
            )


def plan(c: Campaign) -> list[Run]:
    runs: list[Run] = []
    for rep in range(1, c.repeats + 1):
        gen_seed = c.repeat_seeds[rep - 1] if c.repeat_seeds is not None else None
        if c.scheduler_seeds is not None:
            sched_seed = c.scheduler_seeds[rep - 1]
        elif gen_seed is not None:
            sched_seed = gen_seed % (2**31)
        else:
            sched_seed = None
        for point in sorted(c.points, key=lambda p: p.order):
            for staleness in c.staleness_s:
                order = [
                    (w, a, p)
                    for w in c.workloads
                    for a in c.arms
                    for p in (a.policies if a.policies is not None else c.policies)
                ]
                if len(c.workloads) == 1 and len(c.arms) == 1:
                    # The same shuffle as before workloads and arms existed, so a
                    # single-trace campaign that stopped resumes with the same run order.
                    names = list(c.policies)
                    random.Random(f"{c.tag}|{rep}|{point.name}|{staleness}").shuffle(names)
                    order = [(c.workloads[0], c.arms[0], p) for p in names]
                else:
                    random.Random(f"{c.tag}|{rep}|{point.name}|{staleness}").shuffle(order)
                for workload, arm, policy in order:
                    resolved = resolve_point(c, point, workload)
                    prefix = f"{c.tag}_{workload.name}" if workload.name else c.tag
                    suffix = f"_{arm.name}" if arm.name else ""
                    runs.append(
                        Run(
                            run_id=f"{prefix}_{policy}{suffix}_s{staleness:g}_{point.name}_r{rep}",
                            policy=policy,
                            arm=arm,
                            point=resolved,
                            staleness_s=staleness,
                            repeat=rep,
                            sequence_no=len(runs),
                            workload=workload,
                            gen_seed=gen_seed,
                            scheduler_seed=sched_seed,
                        )
                    )
    return runs


def resolve_point(c: Campaign, point: Point, workload: Workload) -> Point:
    """The point with its rate_scale filled in for this workload."""
    if point.rate_scale is not None:
        return point
    assert point.target is not None
    if workload.trace_config is not None:
        length_dist, base_rate = (
            workload.trace_config["length_dist"],
            pool_load.mean_rate(workload.trace_config["arrival"]),
        )
    else:
        assert workload.trace is not None
        header, _ = gen_trace.load(workload.trace, expect_sha256=workload.trace_sha256)
        length_dist, base_rate = header["length_dist"], pool_load.mean_rate(header["arrival"])
    capacity = pool_load.pool_capacity(
        c.nodes, c.cost_model_snapshots, length_dist, pool_load.snapshot_index(SNAPSHOT_ROOT)
    )
    rate, note = pool_load.rate_for(point.target, capacity)
    return Point(point.name, rate_scale=rate / base_rate, target=point.target, note=note)


def trace_for(workload: Workload, gen_seed: int | None) -> TraceFile:
    """The trace a run replays: generated from the workload's config and the repeat's seed.

    Generated once, then verified against what the generator writes now. The comparison is on
    the request stream and not on the file's hash, because the header carries the generator's
    own commit: a trace's sha256 moves with every commit to this repository even when not one
    arrival or length has changed. A body that differs is the real failure, and it refuses.

    A file already on disk keeps its identity, so a campaign that stopped resumes with the
    same `trace_sha256` in every manifest instead of splitting a run set across two.
    """
    if workload.trace_config is None:
        assert workload.trace is not None and workload.trace_sha256 is not None
        header, _ = gen_trace.load(workload.trace, expect_sha256=workload.trace_sha256)
        return TraceFile(workload.trace, workload.trace_sha256, header)
    if gen_seed is None:
        raise ValueError(f"workload {workload.name!r} needs a repeat seed")
    assert workload.trace_config_path is not None
    stem = workload.trace_config_path.stem.removeprefix("trace_")
    path = REPO_ROOT / "runs" / "traces" / f"{stem}_g{gen_seed}.trace.jsonl"
    config = {**workload.trace_config, "gen_seed": gen_seed}
    fresh = path.with_suffix(".check")
    sha = gen_trace.generate(config, fresh)
    if path.is_file():
        _, on_disk = gen_trace.load(path)
        _, regenerated = gen_trace.load(fresh)
        fresh.unlink()
        if on_disk != regenerated:
            raise ValueError(
                f"{path} does not match what its config and seed generate now; move it "
                "aside rather than replaying a trace nobody can regenerate"
            )
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        fresh.replace(path)
    header, _ = gen_trace.load(path, expect_sha256=sha)
    return TraceFile(path, sha, header)


ENGINE_PS = "ps -C llama-server -o pid=,lstart=,args="


def _on_node(
    c: Campaign, node_id: str, command: str, timeout_s: float = 20, attempts: int = 2
) -> str | None:
    """Run a shell command on a node's host: over ssh when its log location is remote.

    Returns the command's output, or None when it could not be run at all. The two are
    different findings and the caller treats them differently: a `ps` that ran and printed
    nothing says the engine is gone, which is a restart, while an ssh that never connected
    says nothing about the engine at all.

    Retried once by default, because one refused connection is not evidence either way.
    """
    host, sep, _ = c.workers[node_id].get("logs", "").partition(":")
    cmd = (
        ["ssh", "-o", "BatchMode=yes", host, command]
        if sep and "/" not in host
        else ["sh", "-c", command]
    )
    for attempt in range(attempts):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode in (0, 1):
            # `ps -C` exits 1 when it matched no process, which is an answer.
            return result.stdout
        if attempt + 1 < attempts:
            time.sleep(1.0)
    return None


def engine_provenance(c: Campaign) -> dict[str, dict[str, Any]]:
    """Per node: the running llama-server's command line, version, library and weights hashes.

    The README says a manifest carries the SHA-256 of the engine's shared libraries and of
    the weights, and until now no campaign manifest did. They are read once per campaign
    from the process that is actually serving, not from the install directory a runbook
    names. Hashing a 1B GGUF takes a few seconds; that is why this is not done per run.
    """
    out: dict[str, dict[str, Any]] = {}
    for node_id in c.workers:
        answer = _on_node(c, node_id, ENGINE_PS)
        line = " ".join((answer or "").split())
        tokens = line.split()
        if len(tokens) < 7:
            out[node_id] = {"error": "no llama-server process found"}
            continue
        args = tokens[6:]  # pid, then five lstart tokens
        binary = args[0]
        model = args[args.index("-m") + 1] if "-m" in args else ""
        bindir = binary.rsplit("/", 1)[0]
        hashes = (
            _on_node(
                c,
                node_id,
                f"sha256sum {bindir}/libllama.so {bindir}/libggml*.so {model} 2>/dev/null",
                timeout_s=120,
            )
            or ""
        )
        version = _on_node(c, node_id, f"{binary} --version 2>&1 | head -2") or ""
        out[node_id] = {
            "command": " ".join(args),
            "version": " ".join(version.split()),
            "sha256": {
                path.rsplit("/", 1)[-1]: digest
                for digest, path in (h.split(None, 1) for h in hashes.splitlines() if h.strip())
            },
            # Both spellings llama-server accepts, so a node started with `--cache-ram=0`
            # is not reported as running the 8 GiB default.
            "cache_ram_0": any(
                a == "--cache-ram=0" or (a == "--cache-ram" and nxt == "0")
                for a, nxt in zip(args, [*args[1:], ""], strict=True)
            ),
        }
    return out


def engine_processes(c: Campaign) -> dict[str, str | None]:
    """node_id -> `pid start args` of its llama-server.

    An empty string means the read worked and found no llama-server. None means the read
    itself failed, so nothing was learned.
    """
    out: dict[str, str | None] = {}
    for node_id in c.workers:
        answer = _on_node(c, node_id, ENGINE_PS)
        out[node_id] = None if answer is None else " ".join(answer.split())
    return out


def pre_run_manifest(
    c: Campaign, run: Run, header: dict[str, Any], trace: TraceFile | None = None
) -> dict[str, Any]:
    if trace is None:
        w = c.workloads[0]
        assert w.trace is not None and w.trace_sha256 is not None
        trace = TraceFile(w.trace, w.trace_sha256, header)
    config = {
        "duration_s": header["duration_s"] / run.point.rate_scale,
        "warmup_s": c.warmup_s,
        "rate_scale": run.point.rate_scale,
        "operating_point": run.point.name,
        "lambda": round(float(header["arrival"].get("lambda_base", 0.0)) * run.point.rate_scale, 6),
        "arrival": header["arrival"],
        "length_dist": header["length_dist"],
        "gen_seed": header["gen_seed"],
        "staleness_s": run.staleness_s,
        "repeat": run.repeat,
        "sequence_no": run.sequence_no,
        "campaign": c.tag,
        # S3: recorded so the run set can tell service-rate and decode-only apart.
        "capability_mode": c.capability_mode,
    }
    # What the calibrated policies are told about the nodes, so a capability-ratio arm and a
    # baseline arm are distinguishable in the run set rather than only in the campaign file.
    if c.capability_override is not None:
        config["capability_override"] = c.capability_override
    if c.capability_concurrency is not None:
        config["capability_concurrency"] = c.capability_concurrency
    if c.ect_mode is not None:
        config["ect_mode"] = c.ect_mode
    if c.ect_prior_output_len is not None:
        config["ect_prior_output_len"] = c.ect_prior_output_len
    if run.arm.name:
        config["capability_arm"] = run.arm.name
    if run.policy == "threshold":
        config["threshold_t"] = c.threshold_t
    # The arm last, so its setting wins over the campaign's. Writing the campaign's cutoff
    # after it, as this once did, meant an arm's threshold_t never reached the scheduler.
    # A cutoff is only meaningful to Threshold, so other policies in the arm do not carry it.
    config.update(
        {k: v for k, v in run.arm.config.items() if k != "threshold_t" or run.policy == "threshold"}
    )
    if run.point.target is not None:
        config["load_target"] = run.point.target
    config["mean_lambda"] = round(pool_load.mean_rate(header["arrival"]) * run.point.rate_scale, 6)
    if run.scheduler_seed is not None:
        # LiveSchedulerApp reads config.seed for its tie-break and weighted-draw stream.
        config["seed"] = run.scheduler_seed
    if run.workload is not None and run.workload.name:
        config["workload"] = run.workload.name
    return manifest_mod.build(
        run_id=run.run_id,
        config=config,
        trace_path=(
            trace.path.relative_to(REPO_ROOT)
            if trace.path.is_relative_to(REPO_ROOT)
            else trace.path
        ),
        trace_sha256=trace.sha256,
        validity=manifest_mod.Validity(),
        nodes=c.nodes,
        vehicle="hardware",
        policy=run.policy,
        cost_model_snapshots={
            n["node_id"]: c.cost_model_snapshots[n["node_id"]]
            for n in c.nodes
            if n.get("role", "pool") == "pool"
        },
        clock_sync=c.clock_sync,
    )


class Scheduler:
    """LiveSchedulerApp in its own process group, with its console copied to a file."""

    def __init__(self, c: Campaign, pre_path: Path, run_dir: Path) -> None:
        mvn = shutil.which("mvn")
        if mvn is None:
            raise RuntimeError("mvn not found on PATH; the live scheduler runs through Maven")
        exec_args = [
            str(pre_path),
            "--port",
            str(c.scheduler_port),
            "--cost-models",
            str(SNAPSHOT_ROOT),
            "--log-dir",
            str(run_dir),
        ]
        for node_id, w in c.workers.items():
            exec_args += ["--worker", f"{node_id}={w['endpoint']}"]
        self.cmd = [
            mvn,
            "-q",
            "-f",
            str(REPO_ROOT / "controlplane" / "pom.xml"),
            "exec:java",
            "-Dexec.mainClass=com.sched.live.LiveSchedulerApp",
            "-Dexec.args=" + " ".join(exec_args),
        ]
        self.console = run_dir / "scheduler_console.txt"
        self.ready = threading.Event()
        self.resolved_other_snapshot = False
        self.proc: subprocess.Popen[str] | None = None

    def start(self, timeout_s: float = 300.0) -> None:
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=REPO_ROOT / "controlplane",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        deadline = time.monotonic() + timeout_s
        while not self.ready.wait(0.5):
            if self.proc.poll() is not None:
                raise RuntimeError(f"the scheduler exited before it was ready; see {self.console}")
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError(
                    f"the scheduler was not ready in {timeout_s:g} s; see {self.console}"
                )
        if self.resolved_other_snapshot:
            self.stop()
            raise RuntimeError(
                f"the scheduler served a different snapshot than the manifest names; see {self.console}"
            )

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        with self.console.open("w", encoding="utf-8") as fh:
            for line in self.proc.stdout:
                fh.write(line)
                fh.flush()
                if RESOLVED_LINE in line:
                    self.resolved_other_snapshot = True
                if READY_LINE in line:
                    self.ready.set()

    def stop(self, timeout_s: float = 30.0) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        # SIGTERM runs the JVM's shutdown hook, which is what closes the decision log.
        os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()


def pull_worker_log(node_id: str, logs: str, run_id: str, run_dir: Path) -> Path | None:
    """Copy one node's worker log for this run into the run directory.

    `logs` is the worker's --log-dir: a local path, or `user@host:path` for a remote node.
    """
    name = f"worker_{node_id}_{run_id}.jsonl"
    dst = run_dir / name
    host, sep, remote = logs.partition(":")
    if sep and "/" not in host:
        result = subprocess.run(
            ["rsync", "-a", "--timeout=30", f"{host}:{remote.rstrip('/')}/{name}", str(dst)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"    could not pull {name} from {host}: {result.stderr.strip()}")
            return None
        return dst
    src = _repo_path(logs) / name
    if not src.is_file():
        print(f"    no {src}")
        return None
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return dst


def run_one(c: Campaign, run: Run, header: dict[str, Any], trace: TraceFile | None = None) -> bool:
    workload = run.workload or c.workloads[0]
    if trace is None:
        trace = trace_for(workload, run.gen_seed)
    header = trace.header
    engines_before = engine_processes(c) if c.check_engine_restarts else {}
    unreadable = sorted(n for n, line in engines_before.items() if not line)
    if unreadable:
        # Refused before anything is written, and whether the read failed or simply found no
        # llama-server. Both mean the same thing here: there is no engine this run can be
        # shown to have used, and a half-written run directory is exactly what
        # `manifest.pre.json` exists to avoid.
        raise EngineUnreadable(
            f"could not read a running engine on {unreadable}; not starting the run"
        )
    run_dir = workload.out_root / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    pre = pre_run_manifest(c, run, header, trace)
    if not c.check_engine_restarts:
        # Said in words, because a manifest that simply carried `engine_restarts: 0` would
        # read as "the engines stayed up" when what happened is that nobody looked.
        pre["config"]["engine_check"] = "disabled"
        pre["config_hash"] = manifest_mod.config_hash(pre["config"])
    if engines_before or c.engine_provenance:
        if engines_before:
            pre["config"]["engine_processes"] = engines_before
        if c.engine_provenance:
            pre["config"]["engine_provenance"] = c.engine_provenance
        pre["config_hash"] = manifest_mod.config_hash(pre["config"])
        for node_id, line in engines_before.items():
            if line and "--cache-ram 0" not in line and "--cache-ram=0" not in line:
                # ruff: keep the message on one line for the console
                print(f"    {node_id}: llama-server runs without --cache-ram 0")
    pre_path = run_dir / "manifest.pre.json"
    pre_path.write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")

    scheduler = Scheduler(c, pre_path, run_dir)
    scheduler.start()
    try:
        result = asyncio.run(
            replay_mod.replay(
                trace_path=trace.path,
                scheduler_endpoint=f"{c.scheduler_host}:{c.scheduler_port}",
                run_id=run.run_id,
                expect_sha256=trace.sha256,
                bind=c.bind,
                advertise_host=c.advertise,
                warmup_s=c.warmup_s,
                rate_scale=run.point.rate_scale,
            )
        )
        replay_mod.write_run(run_dir, run.run_id, result.records)
        # Requests the client gave up on can still be finishing on a node. Waiting here
        # lets them report completion to the scheduler and reach the worker log before
        # either is collected.
        time.sleep(c.settle_s)
    finally:
        scheduler.stop()

    worker_counts = {}
    for node_id, w in c.workers.items():
        path = pull_worker_log(node_id, w["logs"], run.run_id, run_dir)
        worker_counts[node_id] = (
            sum(1 for line in path.read_text().splitlines() if line.strip()) if path else 0
        )

    # replay() counts failures inside the measurement window only. A response lost during
    # warmup is still a lost response, and on the first pair three of them (a summarisation
    # run, jsq, heavy, r3) left the run marked valid, so a campaign counts the whole run.
    lost = sum(1 for r in result.records if r["status"] != "ok")
    if lost > result.validity.dropped_requests:
        print(f"    {lost - result.validity.dropped_requests} request(s) failed during warmup")
        result.validity = replace(result.validity, dropped_requests=lost)

    engine_check: dict[str, dict[str, str]] = {}
    if c.check_engine_restarts:
        engines_after = engine_processes(c)
        for node_id, before in engines_before.items():
            after = engines_after.get(node_id)
            if after is None or not before:
                # A read that failed after its retry taught nothing, and neither does a run
                # whose engine was already missing before it. Not a restart, and not a clean
                # run either.
                verdict = "unknown"
            elif before == after:
                verdict = "same"
            elif not after:
                # `ps` ran and found no llama-server at all: the engine that served this
                # run is gone, which is a restart by the time anyone reads the record.
                verdict = "died"
            else:
                verdict = "changed"
            # `before` and `after` are kept as they were read, null included: a read that
            # failed is not an engine that printed nothing, and the record says which.
            engine_check[node_id] = {"before": before, "after": after, "verdict": verdict}
        restarted = sorted(
            n for n, e in engine_check.items() if e["verdict"] in ("changed", "died")
        )
        unchecked = sorted(n for n, e in engine_check.items() if e["verdict"] == "unknown")
        for node_id in unchecked:
            print(f"    {node_id}: the engine process could not be read; the run is unchecked")
        if restarted:
            print(f"    engine restarted during the run on {restarted}")
        if restarted or unchecked:
            result.validity = replace(
                result.validity,
                engine_restarts=result.validity.engine_restarts + len(restarted),
                engine_unchecked=result.validity.engine_unchecked + len(unchecked),
            )

    man = replay_mod.post_run_manifest(
        run_id=run.run_id,
        result=result,
        trace_path=pre["trace_path"],
        trace_sha256=trace.sha256,
        rate_scale=run.point.rate_scale,
        warmup_s=c.warmup_s,
        nodes=c.nodes,
        policy=run.policy,
        pre=pre,
        clock_sync=c.clock_sync,
    )
    man["config"]["engine_check"] = engine_check if c.check_engine_restarts else "disabled"
    man["config_hash"] = manifest_mod.config_hash(man["config"])
    (run_dir / "manifest.json").write_text(json.dumps(man, indent=2) + "\n", encoding="utf-8")

    dispatched: dict[str, int] = {}
    for r in result.records:
        if r["chosen_node_from_ack"]:
            dispatched[r["chosen_node_from_ack"]] = dispatched.get(r["chosen_node_from_ack"], 0) + 1
    ok = sum(1 for r in result.records if r["status"] == "ok")
    v = man["validity"]
    print(
        f"  {ok}/{len(result.records)} ok  max send lag {v['max_send_lag_ms']:.1f} ms  "
        f"{'VALID' if v['valid'] else 'INVALID'}"
    )
    complete = True
    for node_id, n in worker_counts.items():
        sent = dispatched.get(node_id, 0)
        flag = "" if n == sent else "  MISMATCH"
        complete = complete and n == sent
        print(f"    {node_id}: dispatched {sent}, worker log {n}{flag}")
    if not complete:
        print("    the worker log does not account for every dispatch; the join will be partial")
    return bool(v["valid"]) and complete


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MPR-2 hardware runs against the live scheduler")
    ap.add_argument("config", type=Path)
    ap.add_argument("--clock-sync", type=Path, help="clock_sync.json from `clocksync --combine`")
    ap.add_argument("--dry-run", action="store_true", help="check the config and print the plan")
    ap.add_argument(
        "--allow-colocation",
        action="store_true",
        help="run a pool with two nodes on one host; every run stays marked invalid (F-9a)",
    )
    ap.add_argument(
        "--keep-going", action="store_true", help="continue after a run fails to complete"
    )
    args = ap.parse_args(argv)

    c = Campaign.from_dict(json.loads(args.config.read_text(encoding="utf-8")))
    if args.clock_sync is not None:
        c.clock_sync = json.loads(args.clock_sync.read_text(encoding="utf-8"))
    runs = plan(c)
    try:
        check_campaign(c, snapshot_index(), args.allow_colocation, allow_placeholder=args.dry_run)
        traces = {
            (r.workload.name, r.gen_seed): trace_for(r.workload, r.gen_seed)
            for r in runs
            if r.workload is not None
        }
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2

    minutes = (
        sum(
            traces[(r.workload.name, r.gen_seed)].header["duration_s"] / r.point.rate_scale
            + c.settle_s
            for r in runs
            if r.workload is not None
        )
        / 60
    )
    roots = ", ".join(str(w.out_root) for w in c.workloads)
    print(f"{len(runs)} runs, about {minutes:.0f} min of replay and settling, into {roots}")
    if c.repeat_seeds is None and c.repeats > 1:
        print(
            "  every repeat replays one trace with one scheduler seed: repeats sample hardware "
            "jitter only, not arrivals or routing draws. Set repeat_seeds and trace_config."
        )
    if c.clock_sync is None and len({n["host"] for n in c.nodes}) > 1:
        print("  no --clock-sync: every manifest will record that nobody measured the clocks")
    if args.dry_run:
        for r in runs:
            lam = (
                pool_load.mean_rate(traces[(r.workload.name, r.gen_seed)].header["arrival"])
                * r.point.rate_scale
            )
            seeds = f"  gen {r.gen_seed} sched {r.scheduler_seed}" if r.gen_seed else ""
            print(f"  {r.sequence_no:3d}  {r.run_id}  {lam:.3f} req/s{seeds}")
        return 0

    if c.check_engine_restarts:
        c.engine_provenance = engine_provenance(c)
        for node_id, prov in c.engine_provenance.items():
            print(f"  {node_id}: {prov.get('version') or prov.get('error')}")
            if prov.get("sha256") is not None and not prov["cache_ram_0"]:
                print(f"  {node_id}: llama-server is not running with --cache-ram 0")

    failed: list[str] = []
    for r in runs:
        assert r.workload is not None
        run_dir = r.workload.out_root / r.run_id
        if (run_dir / "manifest.json").is_file() and (
            run_dir / f"client_{r.run_id}.jsonl"
        ).is_file():
            print(f"[{r.sequence_no + 1}/{len(runs)}] {r.run_id}  already done, skipped")
            continue
        print(f"[{r.sequence_no + 1}/{len(runs)}] {r.run_id}", flush=True)
        trace = traces[(r.workload.name, r.gen_seed)]
        try:
            ok = run_one(c, r, trace.header, trace)
        except EngineUnreadable as exc:
            print(f"  stopping: {exc}")
            failed.append(r.run_id)
            break
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"  failed: {exc}")
            ok = False
        if not ok:
            failed.append(r.run_id)
            if not args.keep_going:
                break

    print(f"\n{len(failed)} run(s) not usable: {failed}" if failed else "\nall runs valid")
    for w in c.workloads:
        print(f"next: uv run --project dataplane runset {w.out_root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
