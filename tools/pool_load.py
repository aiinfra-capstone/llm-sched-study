"""Pool capacity and utilisation from the C-3 cost models, shared by the campaign tools.

A load point given as req/s means different things on different workloads: the same 2.4
req/s put the 1650 Ti at 68% of its four-slot capacity under RoundRobin on the generation
trace and at 98% on summarisation. Comparing workload shapes at one req/s therefore moves
load and heterogeneity together. These helpers turn a utilisation target into a rate, and
a rate into utilisation, from the same cost-model cells, so the campaign driver and the
summary use one definition.

Capacity of a node on a workload is `slots / mean service time at full slots`, with the
mean weighted by the workload's bucket mix. That is what the node retires when every slot
is busy. It is a property of the node and the workload, not of any policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"


def snapshot_index(root: Path = SNAPSHOT_ROOT) -> dict[str, dict[str, Any]]:
    index = {}
    for p in sorted(root.glob("*/*.json")):
        snap = json.loads(p.read_text(encoding="utf-8"))
        if "snapshot_id" in snap:
            index[snap["snapshot_id"]] = snap
    return index


def locate(entries: list[dict], prompt: int, output: int, concurrency: int) -> dict | None:
    for e in entries:
        pb, ob = e["prompt_bucket"], e["output_bucket"]
        if (
            e["concurrency"] == concurrency
            and pb[0] <= prompt <= pb[1]
            and ob[0] <= output <= ob[1]
        ):
            return e
    return None


def bucket_mix(length_dist: dict) -> list[tuple[str, int, int, float]]:
    """(bucket_id, prompt, output, weight) with weights summing to one."""
    total = float(sum(length_dist["weights"]))
    out = []
    for b, w in zip(length_dist["buckets"], length_dist["weights"], strict=True):
        p, o = b[1:].split("_o")
        out.append((b, int(p), int(o), w / total))
    return out


def capability(snap: dict) -> float:
    """Output tokens per second of service at the lowest cell, as com.sched.core.Capability."""
    lo_p = min(e["prompt_bucket"][0] for e in snap["entries"])
    lo_o = min(e["output_bucket"][0] for e in snap["entries"])
    cell = next(
        e
        for e in snap["entries"]
        if e["prompt_bucket"][0] == lo_p and e["output_bucket"][0] == lo_o and e["concurrency"] == 1
    )
    if cell.get("decode_ms_mean") and cell.get("service_ms_mean"):
        return cell["tokens_per_s"] * cell["decode_ms_mean"] / cell["service_ms_mean"]
    return cell["tokens_per_s"]


def mean_service_ms(snap: dict, length_dist: dict, concurrency: int) -> float:
    total = 0.0
    for bucket, p, o, w in bucket_mix(length_dist):
        cell = locate(snap["entries"], p, o, concurrency)
        if cell is None:
            raise ValueError(f"{snap['snapshot_id']} has no cell for {bucket} at c={concurrency}")
        total += w * cell["service_ms_mean"]
    return total


def pool_capacity(
    nodes: list[dict], snapshots: dict[str, str], length_dist: dict, index: dict[str, dict]
) -> dict[str, dict[str, float]]:
    """node_id -> slots, mean service at one and at full slots, and capacity in req/s."""
    out = {}
    for node in nodes:
        if node.get("role", "pool") != "pool":
            continue
        nid = node["node_id"]
        snap = index[snapshots[nid]]
        slots = int(node.get("max_batch") or node["engine_config"].get("parallel", 1))
        s1 = mean_service_ms(snap, length_dist, 1)
        sfull = mean_service_ms(snap, length_dist, slots)
        out[nid] = {
            "slots": slots,
            "capability_tok_s": capability(snap),
            "service_ms_c1": s1,
            "service_ms_full": sfull,
            "capacity_rps": slots / (sfull / 1000.0),
        }
    return out


def mean_rate(arrival: dict) -> float:
    """Long-run arrival rate of a C-2 arrival block, before any rate_scale.

    For a two-state MMPP that is the dwell-weighted mean of the two rates, which is what a
    utilisation target has to be set against; `lambda_base` alone is the quiet rate.
    """
    if arrival["process"] == "poisson":
        return float(arrival["lambda_base"])
    if arrival["process"] == "mmpp":
        q, b = float(arrival["quiet_mean_s"]), float(arrival["burst_mean_s"])
        return (float(arrival["lambda_base"]) * q + float(arrival["burst_lambda"]) * b) / (q + b)
    raise ValueError(f"unknown arrival process {arrival['process']!r}")


def rate_for(target: dict[str, float], capacity: dict[str, dict[str, float]]) -> tuple[float, str]:
    """Offered rate in req/s for one load target, and a note saying how it was set.

    `lambda_rps`: that rate. `pool_utilisation`: that fraction of the summed capacity.
    `slow_node_utilisation`: the rate at which an even split (RoundRobin's share) puts the
    slowest node at that fraction of its own capacity.
    """
    caps = {n: c["capacity_rps"] for n, c in capacity.items()}
    if "lambda_rps" in target:
        return float(target["lambda_rps"]), "fixed rate"
    if "pool_utilisation" in target:
        u = float(target["pool_utilisation"])
        return u * sum(caps.values()), f"pool utilisation {u:g}"
    if "slow_node_utilisation" in target:
        u = float(target["slow_node_utilisation"])
        return u * min(caps.values()) * len(
            caps
        ), f"slow-node utilisation {u:g} under an even split"
    raise ValueError(
        f"a load target needs lambda_rps, pool_utilisation or slow_node_utilisation: {target}"
    )
