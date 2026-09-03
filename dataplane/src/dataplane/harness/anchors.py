"""F-23 — the validation anchors: the hardware runs the simulator is judged against.

Week 4 parameterizes a discrete-event simulator from Week-2 measurements and then has to
answer one question: does it agree with the machine? F-23 fixes the form of the answer —
p50 and p95 end-to-end latency within a stated tolerance, at **three or more operating
points**, on **identical replayed traces**. This module produces the hardware half of that
comparison, and the two emphasised phrases are the whole design.

**Three points, not one.** A simulator tuned at a single load is not validated, it is
fitted. The interesting failure is a service-time model that is right when the node is
idle and wrong when it is saturated — a single anchor at either end cannot see it, and a
single anchor in the middle sees neither.

**One trace across all three.** The points differ by `rate_scale`: the same trace file,
replayed with its arrival timeline compressed. Three separately seeded traces would change
the length draw and the burst structure along with the rate, and a disagreement between
vehicles could then be a workload difference rather than a simulator error. Sharing the
trace makes `trace_sha256` identical across the anchor set, which is exactly what
`admissible.load_anchors` checks before it will hand the set to anything.

The operating points are chosen relative to the pool's measured capacity, not in the
abstract: below the knee, near it, and past it. That is the span §5.5's load band lives
in, so the same three runs that validate the simulator also locate the band — which is why
Week 3's exit criterion names both.

What this module does **not** do is bring the pool up. `llama-server`, the worker wrapper
and the scheduler are three processes with three lifetimes, and a campaign runner that
owned them would hide an engine restart inside a Python traceback. They are started by the
operator (README), and this connects to them; `engine_restarts` in the validity block is
counted, not caused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dataplane.calibration.admissible import ANCHOR_ROOT
from dataplane.harness import gen_trace
from dataplane.harness import manifest as manifest_mod
from dataplane.harness import replay as replay_mod

__all__ = ["AnchorConfig", "AnchorPoint", "AnchorResult", "main", "run_anchors"]

# F-23 says "at least 3". Three is the floor, not the target, and the floor is asserted
# here as well as in the test so a campaign that lost a run to a send-lag violation says
# so at the end instead of quietly writing two.
MIN_ANCHORS = 3


@dataclass(frozen=True)
class AnchorPoint:
    """One operating point: a name for the figure legend and the rate that produces it."""

    name: str
    rate_scale: float

    def __post_init__(self) -> None:
        if self.rate_scale <= 0:
            raise ValueError(f"{self.name}: rate_scale must be > 0, got {self.rate_scale}")


@dataclass
class AnchorConfig:
    """Everything one anchor campaign needs. Loaded verbatim from a JSON file (F-20)."""

    trace: Path
    trace_sha256: str
    scheduler: str
    nodes: list[dict[str, Any]]
    points: list[AnchorPoint]
    policy: str = "round_robin"
    warmup_s: float = 0.0
    bind: str = "127.0.0.1:0"
    advertise: str | None = None
    settle_s: float = 15.0
    out_root: Path = ANCHOR_ROOT
    cost_model_snapshots: dict[str, str] = field(default_factory=dict)
    # Set by the CLI from --clock-sync, not read from the campaign config: it is a
    # measurement of the pool taken just before the run, not a property of the run's design.
    clock_sync: dict[str, Any] | None = None
    tag: str = "anchor"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AnchorConfig:
        points = [AnchorPoint(p["name"], float(p["rate_scale"])) for p in d["points"]]
        if len(points) < MIN_ANCHORS:
            raise ValueError(
                f"F-23 needs at least {MIN_ANCHORS} operating points; this config lists "
                f"{len(points)}. Two points cannot distinguish a simulator that is right "
                "from one that was fitted at a single load"
            )
        names = [p.name for p in points]
        if len(set(names)) != len(names):
            raise ValueError(f"operating point names must be unique for the run ids: {names}")
        return cls(
            trace=Path(d["trace"]),
            trace_sha256=d["trace_sha256"],
            scheduler=d["scheduler"],
            nodes=d["nodes"],
            points=points,
            policy=d.get("policy", "round_robin"),
            warmup_s=float(d.get("warmup_s", 0.0)),
            bind=d.get("bind", "127.0.0.1:0"),
            advertise=d.get("advertise"),
            settle_s=float(d.get("settle_s", 15.0)),
            out_root=Path(d.get("out_root", ANCHOR_ROOT)),
            cost_model_snapshots=d.get("cost_model_snapshots", {}),
            tag=d.get("tag", "anchor"),
        )


@dataclass
class AnchorResult:
    """One completed anchor: where it was written, and whether it may be used as one."""

    run_id: str
    point: AnchorPoint
    manifest: dict[str, Any]
    run_dir: Path
    n_records: int
    n_ok: int

    @property
    def valid(self) -> bool:
        return bool(self.manifest["validity"]["valid"])

    def summary(self) -> str:
        v = self.manifest["validity"]
        return (
            f"{self.point.name:>10}  x{self.point.rate_scale:<5.2f} "
            f"lambda={self.manifest['lambda']:.2f}/s  {self.n_ok}/{self.n_records} ok  "
            f"max send lag {v['max_send_lag_ms']:.1f} ms  "
            f"{'VALID' if self.valid else 'INVALID: ' + '; '.join(_reasons(v))}"
        )


def _reasons(validity: dict[str, Any]) -> list[str]:
    """Restate a serialized validity block's failures without rebuilding the dataclass."""
    out = []
    if validity["send_lag_violations"]:
        out.append(f"{validity['send_lag_violations']} send-lag violation(s)")
    if validity["dropped_requests"]:
        out.append(f"{validity['dropped_requests']} request(s) never returned")
    if validity["colocated_nodes"]:
        out.append(f"{validity['colocated_nodes']} co-located node(s)")
    if validity["engine_restarts"]:
        out.append(f"{validity['engine_restarts']} engine restart(s)")
    return out


def build_manifest(
    config: AnchorConfig,
    point: AnchorPoint,
    result: replay_mod.ReplayResult,
    *,
    run_id: str,
    started_unix: int,
) -> dict[str, Any]:
    """A C-6 manifest for one anchor, with the operating point recorded as `lambda`.

    `lambda` is the trace's base rate multiplied by the compression factor, because that
    is the load the pool actually saw. The unscaled rate stays visible in `config.arrival`,
    so the figure caption can say "the same trace at 1.0x / 1.6x / 2.4x" and the number in
    the manifest still means requests per second.
    """
    header = result.header
    validity = manifest_mod.Validity(
        max_send_lag_ms=result.validity.max_send_lag_ms,
        send_lag_violations=result.validity.send_lag_violations,
        dropped_requests=result.validity.dropped_requests,
        heartbeat_gaps=result.validity.heartbeat_gaps,
        engine_restarts=result.validity.engine_restarts,
        colocated_nodes=_colocated(config.nodes),
        clock_unsynced_hosts=manifest_mod.unsynced_hosts(config.clock_sync),
    )
    run_config = {
        "duration_s": header["duration_s"] / point.rate_scale,
        "warmup_s": config.warmup_s,
        "rate_scale": point.rate_scale,
        "operating_point": point.name,
        "arrival": header["arrival"],
        "length_dist": header["length_dist"],
        "gen_seed": header["gen_seed"],
        # Rounded because it is a label on a figure axis, not a measurement: the product of
        # a config rate and a scale factor lands on 0.7200000000000001 often enough to be
        # worth not putting in an artifact people read.
        "lambda": round(float(header["arrival"].get("lambda_base", 0.0)) * point.rate_scale, 6),
    }
    return manifest_mod.build(
        run_id=run_id,
        config=run_config,
        trace_path=config.trace,
        trace_sha256=config.trace_sha256,
        validity=validity,
        nodes=config.nodes,
        vehicle="hardware",
        policy=config.policy,
        cost_model_snapshots=config.cost_model_snapshots,
        clock_sync=config.clock_sync,
        started_unix=started_unix,
    )


def _colocated(nodes: list[dict[str, Any]]) -> int:
    """F-9a, counted here rather than imported, so an anchor manifest carries the number.

    The launcher already refuses to *start* a co-located pool; this is the record that the
    pool which actually ran was not one.
    """
    hosts: dict[str, int] = {}
    for n in nodes:
        if n.get("role", "pool") == "pool":
            hosts[n["host"]] = hosts.get(n["host"], 0) + 1
    return sum(c for c in hosts.values() if c > 1)


async def run_anchors(config: AnchorConfig, *, sleep=asyncio.sleep) -> list[AnchorResult]:
    """Replay the trace once per operating point, slowest first, writing one run each.

    Slowest first because the engine is coldest at the start: a run that begins with a
    freshly loaded model pays a first-request cost that is real but is not a property of
    the load level, and putting the lightest load first keeps that cost where the warmup
    window already discards it. `settle_s` between runs lets the previous point's tail
    drain, so the next point does not begin with the last one's queue.
    """
    # The hash is checked once here, before the first replay, rather than three times
    # inside them: three runs is tens of minutes, and discovering the trace is not the
    # trace the manifest will claim is worth ten seconds at the start.
    gen_trace.load(config.trace, expect_sha256=config.trace_sha256)

    results: list[AnchorResult] = []
    for i, point in enumerate(sorted(config.points, key=lambda p: p.rate_scale)):
        if i:
            await sleep(config.settle_s)
        started_unix = int(time.time())
        run_id = f"{config.tag}_{point.name}_{started_unix}"
        result = await replay_mod.replay(
            trace_path=config.trace,
            scheduler_endpoint=config.scheduler,
            run_id=run_id,
            expect_sha256=config.trace_sha256,
            bind=config.bind,
            advertise_host=config.advertise,
            warmup_s=config.warmup_s,
            rate_scale=point.rate_scale,
        )
        manifest = build_manifest(config, point, result, run_id=run_id, started_unix=started_unix)
        run_dir = config.out_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"client_{run_id}.jsonl").write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in result.records)
        )
        (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        anchor = AnchorResult(
            run_id=run_id,
            point=point,
            manifest=manifest,
            run_dir=run_dir,
            n_records=len(result.records),
            n_ok=sum(1 for r in result.records if r["status"] == "ok"),
        )
        results.append(anchor)
        print(anchor.summary(), flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="F-23 — replay one trace at 3+ operating points and write the anchors"
    )
    ap.add_argument("config", type=Path, help="anchor campaign config (JSON)")
    ap.add_argument("--out-root", type=Path, help="override where the anchor runs are written")
    ap.add_argument(
        "--clock-sync",
        type=Path,
        help="clock_sync.json from `clocksync --combine`, recorded into every manifest. "
        "Omit on a single-host pool: absence means nobody measured, which is the honest "
        "record, and a zeroed block would read as though the clocks had agreed.",
    )
    args = ap.parse_args(argv)

    config = AnchorConfig.from_dict(json.loads(args.config.read_text()))
    if args.out_root is not None:
        config.out_root = args.out_root
    if args.clock_sync is not None:
        config.clock_sync = json.loads(args.clock_sync.read_text())

    results = asyncio.run(run_anchors(config))
    valid = [r for r in results if r.valid]
    print(f"\n{len(valid)}/{len(results)} anchors valid, written under {config.out_root}")
    if len(valid) < MIN_ANCHORS:
        # Non-zero rather than a warning: an anchor set below the floor cannot validate
        # anything, and the next thing to run is Week 4's comparison, which would
        # otherwise start against a set it has no way to know is short.
        print(
            f"F-23 needs {MIN_ANCHORS} valid anchors on one trace; the invalid runs are "
            "still on disk to be looked at, but they are not anchors",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
