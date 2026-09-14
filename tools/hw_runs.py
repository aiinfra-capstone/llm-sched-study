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
reason: a cold engine's first requests belong under the lightest load), and the policy
order is shuffled per point from a fixed seed. Throughput drifts over minutes on these
nodes, so running the policies in the same order every time would put a drift effect
inside the policy comparison.

Usage:
  uv run --project dataplane python tools/hw_runs.py dataplane/configs/hw_mpr2_lan.json --dry-run
  uv run --project dataplane python tools/hw_runs.py dataplane/configs/hw_mpr2_lan.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dataplane.harness import gen_trace, launch
from dataplane.harness import manifest as manifest_mod
from dataplane.harness import replay as replay_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"
POLICIES = ("round_robin", "jsq", "static_weighted", "wjsq", "threshold")
READY_LINE = "Live Control Plane active"
RESOLVED_LINE = "Resolving snapshot"


@dataclass
class Point:
    name: str
    rate_scale: float


@dataclass
class Campaign:
    tag: str
    trace: Path
    trace_sha256: str
    out_root: Path
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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Campaign:
        return cls(
            tag=d["tag"],
            trace=_repo_path(d["trace"]),
            trace_sha256=d["trace_sha256"],
            out_root=_repo_path(d["out_root"]),
            nodes=d["nodes"],
            workers=d["workers"],
            cost_model_snapshots=d["cost_model_snapshots"],
            policies=list(d.get("policies", POLICIES)),
            points=[Point(p["name"], float(p["rate_scale"])) for p in d["points"]],
            staleness_s=[float(s) for s in d.get("staleness_s", [0.0])],
            threshold_t=float(d.get("threshold_t", 50.0)),
            repeats=int(d.get("repeats", 1)),
            warmup_s=float(d.get("warmup_s", 0.0)),
            settle_s=float(d.get("settle_s", 20.0)),
            scheduler_port=int(d.get("scheduler_port", 50051)),
            scheduler_host=d.get("scheduler_host", "127.0.0.1"),
            bind=d.get("bind", "0.0.0.0:50071"),
            advertise=d.get("advertise"),
        )


@dataclass
class Run:
    run_id: str
    policy: str
    point: Point
    staleness_s: float
    repeat: int
    sequence_no: int


def _repo_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def snapshot_index(root: Path = SNAPSHOT_ROOT) -> dict[str, dict[str, Any]]:
    index = {}
    for p in sorted(root.glob("*/*.json")):
        snap = json.loads(p.read_text(encoding="utf-8"))
        index[snap["snapshot_id"]] = snap
    return index


def check_campaign(c: Campaign, index: dict[str, dict[str, Any]], allow_colocation: bool) -> None:
    """Every refusal that would otherwise surface as a bad run hours into the campaign."""
    launch.build_nodes(c.nodes, allow_colocation=allow_colocation)
    unknown = sorted(set(c.policies) - set(POLICIES))
    if unknown:
        raise ValueError(f"unknown policies {unknown}; expected some of {list(POLICIES)}")
    if c.repeats < 1:
        raise ValueError(f"repeats must be at least 1, got {c.repeats}")
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
        for point in sorted(c.points, key=lambda p: p.rate_scale):
            for staleness in c.staleness_s:
                order = list(c.policies)
                random.Random(f"{c.tag}|{rep}|{point.name}|{staleness}").shuffle(order)
                for policy in order:
                    runs.append(
                        Run(
                            run_id=f"{c.tag}_{policy}_s{staleness:g}_{point.name}_r{rep}",
                            policy=policy,
                            point=point,
                            staleness_s=staleness,
                            repeat=rep,
                            sequence_no=len(runs),
                        )
                    )
    return runs


def pre_run_manifest(c: Campaign, run: Run, header: dict[str, Any]) -> dict[str, Any]:
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
    }
    if run.policy == "threshold":
        config["threshold_t"] = c.threshold_t
    return manifest_mod.build(
        run_id=run.run_id,
        config=config,
        trace_path=c.trace.relative_to(REPO_ROOT) if c.trace.is_relative_to(REPO_ROOT) else c.trace,
        trace_sha256=c.trace_sha256,
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


def run_one(c: Campaign, run: Run, header: dict[str, Any]) -> bool:
    run_dir = c.out_root / run.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    pre = pre_run_manifest(c, run, header)
    pre_path = run_dir / "manifest.pre.json"
    pre_path.write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")

    scheduler = Scheduler(c, pre_path, run_dir)
    scheduler.start()
    try:
        result = asyncio.run(
            replay_mod.replay(
                trace_path=c.trace,
                scheduler_endpoint=f"{c.scheduler_host}:{c.scheduler_port}",
                run_id=run.run_id,
                expect_sha256=c.trace_sha256,
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

    man = replay_mod.post_run_manifest(
        run_id=run.run_id,
        result=result,
        trace_path=pre["trace_path"],
        trace_sha256=c.trace_sha256,
        rate_scale=run.point.rate_scale,
        warmup_s=c.warmup_s,
        nodes=c.nodes,
        policy=run.policy,
        pre=pre,
        clock_sync=c.clock_sync,
    )
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
    try:
        check_campaign(c, snapshot_index(), args.allow_colocation)
        header, _ = gen_trace.load(c.trace, expect_sha256=c.trace_sha256)
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2

    runs = plan(c)
    minutes = sum(header["duration_s"] / r.point.rate_scale + c.settle_s for r in runs) / 60
    print(f"{len(runs)} runs, about {minutes:.0f} min of replay and settling, into {c.out_root}")
    if c.clock_sync is None and len({n["host"] for n in c.nodes}) > 1:
        print("  no --clock-sync: every manifest will record that nobody measured the clocks")
    if args.dry_run:
        for r in runs:
            print(f"  {r.sequence_no:3d}  {r.run_id}")
        return 0

    failed: list[str] = []
    for r in runs:
        run_dir = c.out_root / r.run_id
        if (run_dir / "manifest.json").is_file() and (
            run_dir / f"client_{r.run_id}.jsonl"
        ).is_file():
            print(f"[{r.sequence_no + 1}/{len(runs)}] {r.run_id}  already done, skipped")
            continue
        print(f"[{r.sequence_no + 1}/{len(runs)}] {r.run_id}", flush=True)
        try:
            ok = run_one(c, r, header)
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"  failed: {exc}")
            ok = False
        if not ok:
            failed.append(r.run_id)
            if not args.keep_going:
                break

    print(f"\n{len(failed)} run(s) not usable: {failed}" if failed else "\nall runs valid")
    print(f"next: uv run --project dataplane runset {c.out_root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
