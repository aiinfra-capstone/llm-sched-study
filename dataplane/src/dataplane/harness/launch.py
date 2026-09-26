"""F-9a / F-20 — the launcher: turn a pool description into a C-6 node block, or refuse.

The launcher is the only thing that knows what the pool physically *is*, so it is the only
thing that can check the one invariant the harness cannot recover from afterwards:

    **One logical node per physical host.**

F-9a is explicit that per-node throttling must be applied to distinct physical machines,
because two logical nodes on one host contend for PCIe, memory bandwidth and cache — which
reintroduces as contention the exact confound that throttling exists to remove. A run that
violates it still produces plausible numbers, which is what makes it dangerous: the slow
node looks slow, the fast node looks fast, and the ratio between them is partly an
artifact of them fighting each other. There is no way to detect that from the results, so
it is detected here, before the run.

`colocated_nodes` is counted rather than merely asserted because C-6's validity block
wants the number, and because the honest thing for a deliberately co-located smoke run is
to record it and mark the run invalid — not to crash the launcher and lose the smoke test.
`build()` refuses; `validity_for()` reports. Callers pick.

The node block is also where `engine_config` enters the record. Under F-9a those three
numbers — `-ngl`, `--threads`, `--parallel` — *are* the experimental condition, so they are
recorded per run rather than per machine: the same box is a different node class on
Tuesday if it was started with a different `-ngl`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from dataplane.harness.manifest import Validity

__all__ = [
    "ENGINE_PS",
    "NodeSpec",
    "build_nodes",
    "colocated_count",
    "engine_processes",
    "engine_verdict",
    "on_host",
    "validity_for",
]

# One line per llama-server: pid, start time, command line. A restart changes the first two.
ENGINE_PS = "ps -C llama-server -o pid=,lstart=,args="

_REQUIRED_ENGINE_CONFIG = ("ngl", "threads", "parallel")


class NodeSpec(dict):
    """A C-6 node block. A dict subclass so it serializes with no adapter."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NodeSpec:
        missing = [
            k
            for k in (
                "node_id",
                "host",
                "engine",
                "engine_version",
                "model",
                "quant",
                "gpu",
                "engine_config",
                "prefix_caching",
                "max_batch",
            )
            if k not in d
        ]
        if missing:
            raise ValueError(f"node {d.get('node_id', '?')!r} is missing C-6 fields: {missing}")
        ec = d["engine_config"]
        missing_ec = [k for k in _REQUIRED_ENGINE_CONFIG if k not in ec]
        if missing_ec:
            raise ValueError(
                f"node {d['node_id']!r} engine_config is missing {missing_ec} — under F-9a "
                "these ARE the experimental condition and cannot be defaulted"
            )
        if d["engine"] == "vllm" and d.get("role") != "engine_gap_probe":
            # F-9b: vLLM is a measured condition, never a pool member. Letting it into the
            # pool would put an engine effect inside R, which no hypothesis can decompose.
            raise ValueError(
                f"node {d['node_id']!r} runs vllm but is not role='engine_gap_probe'; under "
                "F-9 every pool node runs llama.cpp"
            )
        return cls({"role": "pool", **d})


def colocated_count(nodes: list[dict[str, Any]]) -> int:
    """How many pool nodes share a host with another pool node.

    Counted over pool members only: the F-9b probe is allowed to sit on the same box as a
    pool node because it never runs at the same time as one — it is a separate measured
    condition on an identical replayed trace, not a member of the pool.
    """
    hosts: dict[str, int] = {}
    for n in nodes:
        if n.get("role", "pool") == "pool":
            hosts[n["host"]] = hosts.get(n["host"], 0) + 1
    return sum(count for count in hosts.values() if count > 1)


def build_nodes(specs: list[dict[str, Any]], *, allow_colocation: bool = False) -> list[NodeSpec]:
    """Validate and normalize a pool description. Refuses co-location by default.

    `allow_colocation` exists for smoke runs on one machine, and it does not make the run
    valid — it makes it *runnable*. `validity_for` still counts the co-located nodes and
    the manifest still marks the run invalid, so nothing co-located can be analysed by
    accident.
    """
    nodes = [NodeSpec.from_dict(s) for s in specs]
    if not nodes:
        raise ValueError("a pool needs at least one node")

    duplicates = {
        n["node_id"] for n in nodes if [x["node_id"] for x in nodes].count(n["node_id"]) > 1
    }
    if duplicates:
        raise ValueError(f"duplicate node_id(s): {sorted(duplicates)}")

    pool = [n for n in nodes if n.get("role", "pool") == "pool"]
    engines = {n["engine"] for n in pool}
    models = {n["model"] for n in pool}
    quants = {n["quant"] for n in pool}
    if len(engines) > 1 or len(models) > 1 or len(quants) > 1:
        # F-9: engine, model and quantization are held constant across the pool so that R
        # is a property of hardware and engine_config alone.
        raise ValueError(
            f"pool is not homogeneous — engines={sorted(engines)}, models={sorted(models)}, "
            f"quants={sorted(quants)}. F-9 holds all three constant so that R is a property "
            "of hardware and engine_config alone"
        )

    colocated = colocated_count(nodes)
    if colocated and not allow_colocation:
        by_host: dict[str, list[str]] = {}
        for n in pool:
            by_host.setdefault(n["host"], []).append(n["node_id"])
        shared = {h: ids for h, ids in by_host.items() if len(ids) > 1}
        raise ValueError(
            f"{colocated} co-located pool node(s): {shared}. F-9a requires one logical node "
            "per physical host — co-located nodes contend for PCIe, memory bandwidth and "
            "cache, which reintroduces as contention the confound throttling removes. Pass "
            "allow_colocation=True only for a smoke run; it stays marked invalid"
        )
    return nodes


def validity_for(nodes: list[dict[str, Any]], **counts: Any) -> Validity:
    """A C-6 validity block carrying the co-location count the launcher measured."""
    return Validity(colocated_nodes=colocated_count(nodes), **counts)


def on_host(
    ssh: str | None,
    command: str,
    *,
    timeout_s: float = 20,
    attempts: int = 2,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str | None:
    """Run a shell command on a host: over ssh when `ssh` names one, else locally.

    Returns the command's output, or None when it could not be run at all. The two are
    different findings and the caller treats them differently: a `ps` that ran and printed
    nothing says the engine is gone, which is a restart, while an ssh that never connected
    says nothing about the engine at all.

    Retried once by default, because one refused connection is not evidence either way.
    `run` is looked up at call time so a test can replace `subprocess.run`.
    """
    run = run or subprocess.run
    cmd = ["ssh", "-o", "BatchMode=yes", ssh, command] if ssh else ["sh", "-c", command]
    for attempt in range(attempts):
        try:
            result = run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode in (0, 1):
            # `ps -C` exits 1 when it matched no process, which is an answer.
            return result.stdout
        if attempt + 1 < attempts:
            time.sleep(1.0)
    return None


def engine_processes(
    nodes: list[dict[str, Any]],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, str | None]:
    """node_id -> `pid start args` of its llama-server.

    Each node is read on the host its `ssh` key names, or locally without one. An empty
    string means the read worked and found no llama-server. None means the read itself
    failed, so nothing was learned.
    """
    out: dict[str, str | None] = {}
    for node in nodes:
        answer = on_host(node.get("ssh"), ENGINE_PS, run=run)
        out[node["node_id"]] = None if answer is None else " ".join(answer.split())
    return out


def engine_verdict(
    before: str | None, after: str | None
) -> Literal["same", "changed", "died", "unknown"]:
    """What one node's engine did across a run, from a read before and a read after.

    A read that failed teaches nothing, and neither does a run whose engine was already
    missing before it: that is `unknown`, which is neither a restart nor a clean run. `ps`
    that ran after the run and found no llama-server means the engine that served it is
    gone, which is a restart by the time anyone reads the record.
    """
    if after is None or not before:
        return "unknown"
    if before == after:
        return "same"
    if not after:
        return "died"
    return "changed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a pool description into a C-6 node block")
    ap.add_argument("pool", type=Path, help="JSON array of node specs")
    ap.add_argument("--out", type=Path, help="write the normalized node block here")
    ap.add_argument(
        "--allow-colocation",
        action="store_true",
        help="permit two logical nodes on one host; the run stays marked invalid (F-9a)",
    )
    args = ap.parse_args(argv)

    nodes = build_nodes(json.loads(args.pool.read_text()), allow_colocation=args.allow_colocation)
    colocated = colocated_count(nodes)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(nodes, indent=2) + "\n")
    hosts = sorted({n["host"] for n in nodes})
    print(f"{len(nodes)} node(s) across {len(hosts)} host(s): {', '.join(hosts)}")
    for n in nodes:
        print(f"  {n['node_id']:12} {n['host']:12} {n['role']:18} {n['engine_config']}")
    if colocated:
        print(f"  WARNING: {colocated} co-located node(s) — this run cannot be analysed (F-9a)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
