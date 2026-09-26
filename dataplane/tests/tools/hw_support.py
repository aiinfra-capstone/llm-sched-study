"""A fake pool for driving `hw_runs.run_one` and `hw_runs.main` without a scheduler or a node.

Only the process boundaries are faked: the Maven scheduler, the replay client, the rsync of
worker logs, and `subprocess.run` for the ssh and `sh -c` calls that read each node's
llama-server. Everything between them (the pre-run manifest, the validity counts, the
post-run manifest) is the real code.

The engine reads are scripted per node and per phase. "before" is every read made before
the scheduler starts, "after" every read made after it stops. Each phase holds a list of
outcomes consumed in order, the last one repeating:

    ("line", "<pid> <lstart x5> <args>")   ssh worked and ps printed a process
    ("none",)                              ssh worked and ps found no llama-server (exit 1)
    ("ssh",)                               ssh itself failed (exit 255)
    ("timeout",)                           the call timed out
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hw_runs
from support import campaign_dict, current_snapshots

from dataplane.harness import gen_trace
from dataplane.harness import manifest as manifest_mod
from dataplane.harness import replay as replay_mod

HOSTS = {"gtx1650ti": "u@gtx", "rtx3050": "u@rtx"}
LINE = (
    "4242 Mon Sep 15 10:00:00 2026 /opt/llama/bin/llama-server -m /m/1b.gguf -ngl 99 --cache-ram 0"
)
OTHER_PID = (
    "5151 Mon Sep 15 10:07:00 2026 /opt/llama/bin/llama-server -m /m/1b.gguf -ngl 99 --cache-ram 0"
)


@dataclass
class Pool:
    tmp: Path
    real_run: Any
    phase: str = "before"
    script: dict[tuple[str, str], list[tuple]] = field(default_factory=dict)
    ssh_calls: dict[tuple[str, str], int] = field(default_factory=dict)
    schedulers: list[Path] = field(default_factory=list)
    replays: list[str] = field(default_factory=list)
    records_ok: int = 6
    records: list[dict[str, Any]] | None = None
    validity: manifest_mod.Validity = field(default_factory=manifest_mod.Validity)
    worker_lines: dict[str, int] | None = None
    # The order of the Maven steps main() takes: "compile", then "exec:java" per run.
    maven: list[str] = field(default_factory=list)
    compile_fails: bool = False

    # ------------------------------------------------------------------ engine reads

    def engines(self, before: tuple | list, after: tuple | list | None = None, node=None) -> None:
        """Script the ps reads. `before`/`after` are one outcome or a list of outcomes."""
        for n in [node] if node else list(HOSTS):
            for phase, outcome in (("before", before), ("after", after if after else before)):
                self.script[(n, phase)] = list(outcome) if isinstance(outcome, list) else [outcome]

    def _engine(self, node: str, cmd: list[str], timeout: float | None):
        key = (node, "before" if self.phase == "before" else "after")
        self.ssh_calls[key] = self.ssh_calls.get(key, 0) + 1
        queue = self.script.get(key, [("line", LINE)])
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        kind = outcome[0]
        if kind == "line":
            return subprocess.CompletedProcess(cmd, 0, stdout=outcome[1] + "\n", stderr="")
        if kind == "none":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if kind == "ssh":
            return subprocess.CompletedProcess(
                cmd, 255, stdout="", stderr=f"ssh: connect to host {node}: Connection refused"
            )
        raise subprocess.TimeoutExpired(cmd, timeout or 0)

    def run(self, cmd, *args, **kwargs):
        if not isinstance(cmd, list) or cmd[0] not in ("ssh", "sh"):
            return self.real_run(cmd, *args, **kwargs)
        command = cmd[-1]
        node = next((n for n, h in HOSTS.items() if h in cmd), None)
        if node is not None and command == hw_runs.ENGINE_PS:
            return self._engine(node, cmd, kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # ------------------------------------------------------------ scheduler and replay

    def scheduler_class(self):
        pool = self

        class FakeScheduler:
            def __init__(self, c, pre_path, run_dir) -> None:
                self.pre_path = pre_path

            def start(self, timeout_s: float = 300.0) -> None:
                pool.maven.append("exec:java")
                pool.schedulers.append(self.pre_path)
                pool.phase = "during"

            def stop(self, timeout_s: float = 30.0) -> None:
                pool.phase = "after"

        return FakeScheduler

    def make_records(self, run_id: str) -> list[dict[str, Any]]:
        if self.records is not None:
            return [dict(r, run_id=run_id) for r in self.records]
        nodes = list(HOSTS)
        return [
            {
                "run_id": run_id,
                "req_id": f"r{i:06d}",
                "status": "ok",
                "chosen_node_from_ack": nodes[i % 2],
            }
            for i in range(self.records_ok)
        ]

    async def replay(self, **kw):
        self.replays.append(kw["run_id"])
        return replay_mod.ReplayResult(
            records=self.make_records(kw["run_id"]),
            validity=self.validity,
            header=gen_trace.load(kw["trace_path"])[0],
        )

    def compile_scheduler(self) -> None:
        self.maven.append("compile")
        if self.compile_fails:
            raise RuntimeError("mvn -q compile failed: COMPILATION ERROR")

    def pull_worker_log(self, node_id, logs, run_id, run_dir):
        path = run_dir / f"worker_{node_id}_{run_id}.jsonl"
        if self.worker_lines is not None:
            n = self.worker_lines.get(node_id, 0)
        else:
            n = sum(1 for r in self.make_records(run_id) if r["chosen_node_from_ack"] == node_id)
        path.write_text("{}\n" * n)
        return path


def isolate_cost_models(monkeypatch, tmp_path: Path) -> Path:
    """A cost-model directory holding only the snapshots the committed campaign names.

    `main()` refuses a campaign whose snapshot is not the newest in its class, which is right,
    and it reads the class from the repository. A test that drives `main()` for some other
    reason should not start failing the night a node is recalibrated, so it gets a directory
    where the named snapshots are the only ones, and therefore the newest.
    """
    wanted = set(current_snapshots().values())
    root = tmp_path / "cost_models"
    for path in sorted(hw_runs.SNAPSHOT_ROOT.glob("*/*.json")):
        snap = json.loads(path.read_text(encoding="utf-8"))
        if snap.get("snapshot_id") in wanted:
            (root / path.parent.name).mkdir(parents=True, exist_ok=True)
            (root / path.parent.name / path.name).write_text(path.read_text(encoding="utf-8"))
    real_index = hw_runs.snapshot_index
    monkeypatch.setattr(hw_runs, "SNAPSHOT_ROOT", root)
    monkeypatch.setattr(hw_runs, "snapshot_index", lambda root_=root: real_index(root_))
    return root


def install(monkeypatch, tmp_path: Path) -> Pool:
    isolate_cost_models(monkeypatch, tmp_path)
    pool = Pool(tmp=tmp_path, real_run=subprocess.run)
    monkeypatch.setattr(hw_runs.subprocess, "run", pool.run)
    monkeypatch.setattr(hw_runs, "Scheduler", pool.scheduler_class())
    monkeypatch.setattr(hw_runs, "compile_scheduler", pool.compile_scheduler)
    # The checkout the suite runs in may well have edits in it; a test that is not about the
    # dirty-tree refusal runs as though from a clean one.
    clean = dict.fromkeys(manifest_mod.COMPONENTS, False)
    monkeypatch.setattr(manifest_mod, "git_dirty", lambda root=None: clean)
    monkeypatch.setattr(hw_runs.replay_mod, "replay", pool.replay)
    monkeypatch.setattr(hw_runs, "pull_worker_log", pool.pull_worker_log)
    monkeypatch.setattr(hw_runs.time, "sleep", lambda s: None)
    monkeypatch.setattr(hw_runs, "REPO_ROOT", tmp_path / "repo")
    return pool


def remote_campaign(tmp_path: Path, **over: Any) -> dict[str, Any]:
    d = campaign_dict(tmp_path, **over)
    d["workers"] = {
        n: {"endpoint": f"10.0.0.{i}:50061", "logs": f"{HOSTS[n]}:runs/worker_logs"}
        for i, n in enumerate(HOSTS, start=1)
    }
    d.setdefault("settle_s", 0.0)
    return d


def first_run(c: hw_runs.Campaign) -> tuple[hw_runs.Run, hw_runs.TraceFile]:
    run = hw_runs.plan(c)[0]
    return run, hw_runs.trace_for(run.workload, run.gen_seed)


def manifest_of(c: hw_runs.Campaign, run: hw_runs.Run) -> dict[str, Any] | None:
    path = run.workload.out_root / run.run_id / "manifest.json"
    return json.loads(path.read_text()) if path.is_file() else None
