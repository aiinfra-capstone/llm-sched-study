"""F-17 — the open-loop replay client.

    load trace + verify sha256
          |
    materialize prompts up front (all of them, before t0)   <- keeps the timing loop allocation-free
          |
    t0 = monotonic()
    for each req:
        sleep_until(t0 + arrival_offset_s)
        record send_lag                                     <- the open-loop guard
        spawn task -> Dispatch -> await Deliver -> log
          |
    drain in-flight, flush log, emit the C-6 validity block

**Open-loop** means the loop never waits on a response before firing the next request. A
closed-loop client silently converts a slow pool into a lower arrival rate, which makes
every queueing result it produces a measurement of itself.

`send_lag_ms` is how we know that held. It is asserted per request, and if any request in
the measurement window exceeds the threshold the run is marked **invalid in the manifest**
rather than analysed. A run that failed to generate its stated load is not a data point
about scheduling.

Two clock rules, from §7:
  * `e2e_duration_ns` is measured entirely on this host's monotonic clock — send stamp to
    delivery-receipt stamp, both taken here.
  * `client_send_mono_ns` goes on the wire for gap detection only. Nothing downstream may
    subtract it from a worker stamp.

> **Language note.** This is the one A-side component where Python may not hold. asyncio
> is adequate below roughly 50 req/s; above that, GIL contention shows up as send-lag
> violations — which this client will honestly report as an invalid run rather than hide.
> Decide from measured send-lag, not in advance; the seam (gRPC + JSONL) supports Go.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import grpc

from dataplane.harness import gen_trace, launch
from dataplane.harness import manifest as manifest_mod
from dataplane.harness.prompts import materialize_all
from dataplane.proto import sched_grpc, sched_pb2

__all__ = ["ReplayResult", "check_pre_run", "find_pre_run", "post_run_manifest", "replay"]


@dataclass
class ReplayResult:
    records: list[dict[str, Any]]
    validity: manifest_mod.Validity
    header: dict[str, Any]


class _DeliverySink(sched_grpc.ClientServicer):
    """Receives worker -> client responses directly (F-11); the scheduler is not in this path.

    A future is registered *before* the Dispatch RPC goes out, never after: on a fast pool
    the delivery can beat the DispatchAck back, and a sink that registers late loses that
    response and reports a phantom timeout.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future] = {}

    def expect(self, req_id: str) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[req_id] = fut
        return fut

    def forget(self, req_id: str) -> None:
        self._waiters.pop(req_id, None)

    async def Deliver(self, request, context):
        recv_ns = time.monotonic_ns()  # stamped first: everything after is our own overhead
        fut = self._waiters.pop(request.req_id, None)
        if fut is not None and not fut.done():
            fut.set_result((request, recv_ns))
        return sched_pb2.ExecuteAck(req_id=request.req_id, queued=True)


async def _fire(
    *,
    rec: dict[str, Any],
    prompt: list[int],
    target_ns: int,
    t0: int,
    intended_offset_s: float,
    run_id: str,
    stub: sched_grpc.SchedulerStub,
    sink: _DeliverySink,
    client_endpoint: str,
    timeout_s: float,
) -> dict[str, Any]:
    """One request, end to end. Returns exactly one C-4 record — success or failure."""
    req_id = rec["req_id"]
    fut = sink.expect(req_id)

    send_ns = time.monotonic_ns()
    record: dict[str, Any] = {
        "run_id": run_id,
        "req_id": req_id,
        "intended_offset_s": round(intended_offset_s, 6),
        "actual_send_offset_s": round((send_ns - t0) / 1e9, 6),
        "send_lag_ms": round((send_ns - target_ns) / 1e6, 3),
        "e2e_duration_ns": 0,
        "status": "engine_error",
        "output_tokens": 0,
        "responding_node": "",
        "chosen_node_from_ack": "",
        "dispatch_ack_ns": 0,
    }

    try:
        ack = await stub.Dispatch(
            sched_pb2.DispatchRequest(
                run_id=run_id,
                req_id=req_id,
                prompt_token_ids=prompt,
                output_len=rec["output_len"],
                priority=rec["priority"],  # passthrough label (§5.4); nothing branches on it
                bucket_id=rec["bucket_id"],
                client_endpoint=client_endpoint,
                client_send_mono_ns=send_ns,  # gap detection only, never subtracted cross-host
            ),
            timeout=timeout_s,
        )
    except grpc.aio.AioRpcError as exc:
        sink.forget(req_id)
        record["e2e_duration_ns"] = time.monotonic_ns() - send_ns
        record["status"] = (
            "timeout" if exc.code() is grpc.StatusCode.DEADLINE_EXCEEDED else "engine_error"
        )
        return record

    record["dispatch_ack_ns"] = time.monotonic_ns() - send_ns
    record["chosen_node_from_ack"] = ack.chosen_node

    if not ack.accepted:
        sink.forget(req_id)
        record["e2e_duration_ns"] = time.monotonic_ns() - send_ns
        return record  # reject_reason lives in the scheduler's own log

    try:
        delivery, recv_ns = await asyncio.wait_for(fut, timeout=timeout_s)
    except TimeoutError:
        sink.forget(req_id)
        record["e2e_duration_ns"] = time.monotonic_ns() - send_ns
        record["status"] = "timeout"
        return record

    record["e2e_duration_ns"] = recv_ns - send_ns  # both stamps from this host's monotonic clock
    record["status"] = delivery.status or "ok"
    record["output_tokens"] = delivery.output_tokens
    record["responding_node"] = delivery.node_id
    return record


# Config keys this replay measures itself. Every other config key in the pre-run manifest
# describes the scheduler's condition and is carried into the post-run record.
_MEASURED_CONFIG = ("duration_s", "warmup_s", "rate_scale", "lambda", "arrival", "length_dist")


def find_pre_run(run_dir: Path) -> Path | None:
    """The manifest the scheduler was started with, if the run directory holds one.

    `manifest.pre.json` first, because that name is never mistaken for a finished run by
    `runset.discover`. A `manifest.json` already in the directory is the older README
    layout, where the pre-run manifest sat at the path the post-run one is written to.
    """
    for name in ("manifest.pre.json", "manifest.json"):
        if (run_dir / name).is_file():
            return run_dir / name
    return None


def check_pre_run(
    pre: dict[str, Any], *, run_id: str, sha256: str | None, policy: str | None
) -> None:
    """Refuse a replay whose client would disagree with the scheduler it is talking to.

    Checked before the run rather than after it. A different run_id splits the logs
    across two names and the join finds no decisions; a different trace or policy produces
    a record that describes a run nobody did.
    """
    if pre.get("run_id") != run_id:
        raise ValueError(
            f"the pre-run manifest is for run {pre.get('run_id')!r} but --run-id is {run_id!r}; "
            "the scheduler names its log after the manifest, so the join would find no decisions"
        )
    if sha256 and pre.get("trace_sha256") and pre["trace_sha256"] != sha256:
        raise ValueError(
            f"the pre-run manifest names trace {pre['trace_sha256'][:12]} but --sha256 is "
            f"{sha256[:12]}"
        )
    if policy and pre.get("policy") and pre["policy"] != policy:
        raise ValueError(
            f"the scheduler was started with policy {pre['policy']!r} but --policy says {policy!r}"
        )
    if not pre.get("cost_model_snapshots"):
        raise ValueError(
            "the pre-run manifest names no cost model snapshots; the scheduler admits no node "
            "without one, and runset cannot derive R for the run"
        )


def post_run_manifest(
    *,
    run_id: str,
    result: ReplayResult,
    trace_path: str | Path,
    trace_sha256: str,
    rate_scale: float,
    warmup_s: float,
    nodes: list[dict[str, Any]],
    policy: str,
    pre: dict[str, Any] | None = None,
    clock_sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The C-6 record of a finished replay: what the client measured over what the scheduler ran.

    The measured fields (duration, rate, arrival, validity) come from this replay. Everything
    that describes the scheduler's condition comes from the pre-run manifest when there is
    one: the snapshots, staleness, Threshold(T) cutoff, any borrowed snapshot, the transport
    term. `staleness_s` is taken from the top level because that is the field the scheduler
    reads.
    """
    header = result.header
    validity = replace(
        result.validity,
        colocated_nodes=launch.colocated_count(nodes),
        clock_unsynced_hosts=manifest_mod.unsynced_hosts(clock_sync),
    )
    config = {k: v for k, v in (pre or {}).get("config", {}).items() if k not in _MEASURED_CONFIG}
    config.update(
        {
            "duration_s": header["duration_s"] / rate_scale,
            "warmup_s": warmup_s,
            "rate_scale": rate_scale,
            "lambda": round(float(header["arrival"].get("lambda_base", 0.0)) * rate_scale, 6),
            "arrival": header["arrival"],
            "length_dist": header["length_dist"],
            "gen_seed": header["gen_seed"],
        }
    )
    if pre is not None:
        config["staleness_s"] = float(pre.get("staleness_s", 0.0))
    man = manifest_mod.build(
        run_id=run_id,
        config=config,
        trace_path=trace_path,
        trace_sha256=trace_sha256,
        validity=validity,
        policy=policy,
        nodes=nodes,
        clock_sync=clock_sync,
        vehicle=(pre or {}).get("vehicle", "hardware"),
        cost_model_snapshots=(pre or {}).get("cost_model_snapshots"),
        f18_status=(pre or {}).get("f18_status"),
    )
    if pre is not None and "transport_overhead" in pre:
        man["transport_overhead"] = pre["transport_overhead"]
    return man


def write_run(run_dir: Path, run_id: str, records: list[dict[str, Any]]) -> Path:
    """Write the client's C-4 log into the run directory and return its path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"client_{run_id}.jsonl"
    log_path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return log_path


async def replay(
    *,
    trace_path: str | Path,
    scheduler_endpoint: str,
    run_id: str,
    expect_sha256: str | None = None,
    bind: str = "0.0.0.0:0",
    advertise_host: str | None = None,
    warmup_s: float = 0.0,
    rate_scale: float = 1.0,
    send_lag_threshold_ms: float = manifest_mod.SEND_LAG_THRESHOLD_MS,
) -> ReplayResult:
    """Replay a trace open-loop against a scheduler and return the C-4 records + validity.

    `rate_scale` compresses the arrival timeline: every `arrival_offset_s` is divided by
    it, so 2.0 replays the same trace at twice the offered load. This is how F-23's three
    operating points are reached **without three traces**. The alternative — one seeded
    trace per lambda — would vary the length draw and the burst structure along with the
    rate, and the difference between two operating points would then be partly a
    difference in workload. Here the request sequence is byte-identical across points and
    the only thing that changes is when they arrive, which is what "operating point"
    is supposed to mean.

    It also keeps `trace_sha256` constant across the anchor set, which is the property
    `admissible.load_anchors` refuses to run without: comparing a simulator to hardware on
    two different traces cannot distinguish a simulator error from a workload difference.
    """
    if rate_scale <= 0:
        raise ValueError(
            f"rate_scale is a multiplier on offered load and must be > 0, got {rate_scale}"
        )
    header, body = gen_trace.load(trace_path, expect_sha256=expect_sha256)
    timeout_s = header["admissible"]["timeout_ceiling_ms"] / 1000.0

    # Everything expensive happens here, before t0. Tokenising inside the timing loop is
    # the most common cause of send-lag drift at high lambda.
    prompts = materialize_all(body, header["vocab_size"])

    # Resolved before anything is bound: raising after `server.start()` would leave a
    # started server with nothing to stop it, and the process hangs on exit instead of
    # printing the reason it refused to run.
    host = advertise_host or bind.rsplit(":", 1)[0]
    if host in ("0.0.0.0", ""):
        raise ValueError(
            "cannot advertise 0.0.0.0 to a worker on another host; "
            "pass --advertise <this host's LAN address>"
        )

    sink = _DeliverySink()
    server = grpc.aio.server()
    sched_grpc.add_ClientServicer_to_server(sink, server)
    port = server.add_insecure_port(bind)
    await server.start()
    client_endpoint = f"{host}:{port}"

    try:
        async with grpc.aio.insecure_channel(scheduler_endpoint) as channel:
            stub = sched_grpc.SchedulerStub(channel)
            tasks: list[asyncio.Task] = []

            t0 = time.monotonic_ns()
            for rec in body:
                intended_offset_s = rec["arrival_offset_s"] / rate_scale
                target_ns = t0 + round(intended_offset_s * 1e9)
                delay_s = (target_ns - time.monotonic_ns()) / 1e9
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                tasks.append(
                    asyncio.create_task(
                        _fire(
                            rec=rec,
                            prompt=prompts[rec["req_id"]],
                            target_ns=target_ns,
                            t0=t0,
                            intended_offset_s=intended_offset_s,
                            run_id=run_id,
                            stub=stub,
                            sink=sink,
                            client_endpoint=client_endpoint,
                            timeout_s=timeout_s,
                        )
                    )
                )
            records = list(await asyncio.gather(*tasks))  # drain in-flight
    finally:
        await server.stop(grace=1.0)

    # The measurement window excludes warmup: a cold engine's send-lag is not evidence
    # about the load generator, and the warmup requests are not analysed either.
    windowed = [r for r in records if r["intended_offset_s"] >= warmup_s]
    validity = manifest_mod.Validity(
        max_send_lag_ms=max((r["send_lag_ms"] for r in windowed), default=0.0),
        send_lag_violations=sum(1 for r in windowed if r["send_lag_ms"] > send_lag_threshold_ms),
        # Every request that did not come back `ok`, whatever went wrong: no dispatch, no
        # delivery, or a delivery carrying `timeout` / `oom` / `engine_error`.
        #
        # I loosened this to "no response at all" for a while, because the pinned engine
        # returned HTTP 500 on about 1% of requests and under the strict rule almost no run
        # could ever be valid. That was treating the symptom. The 500 was a bug in
        # llama.cpp's `/completion` path (`patches/`), and with it fixed the strict rule is
        # the right one: a replay runs *inside* the admissible set, where by construction
        # nothing should fail. A failure there means the run is not measuring what it
        # claims, and that is exactly what `valid` is for.
        #
        # The F-15 cliff is characterized from the calibration campaign's observations,
        # which deliberately probe *outside* the admissible set. It does not need replay
        # runs to be allowed to fail.
        dropped_requests=sum(1 for r in windowed if r["status"] != "ok"),
    )
    return ReplayResult(records=records, validity=validity, header=header)


def main() -> int:
    ap = argparse.ArgumentParser(description="F-17 — replay a seeded trace open-loop")
    ap.add_argument("trace", type=Path)
    ap.add_argument(
        "--scheduler", required=True, help="host:port of the scheduler's Dispatch service"
    )
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--sha256", help="expected trace hash; the run refuses to start without a match"
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        help="the pre-run manifest the scheduler was started with. Defaults to "
        "<out>/<run-id>/manifest.pre.json, then manifest.json. Its snapshots, staleness and "
        "policy are carried into the post-run manifest.",
    )
    ap.add_argument(
        "--clock-sync",
        type=Path,
        help="clock_sync.json from `clocksync --combine`, recorded into the manifest",
    )
    ap.add_argument(
        "--bind", default="0.0.0.0:0", help="where this client listens for Deliver (F-11)"
    )
    ap.add_argument(
        "--advertise", help="host the worker should send responses to (this host's LAN address)"
    )
    ap.add_argument("--out", type=Path, default=Path("runs"), help="run directory root")
    ap.add_argument("--warmup-s", type=float, default=0.0)
    ap.add_argument(
        "--rate-scale",
        type=float,
        default=1.0,
        help="compress the arrival timeline by this factor; 2.0 replays the same trace at "
        "twice the offered load, and the trace hash stays the same (F-23)",
    )
    ap.add_argument(
        "--policy",
        help="recorded in the manifest; the client does not choose. Taken from the pre-run "
        "manifest when there is one, and refused if it disagrees.",
    )
    ap.add_argument(
        "--nodes",
        type=Path,
        help="JSON array of C-6 node blocks from the launcher. Taken from the pre-run "
        "manifest when there is one. Without either, the client writes its validity block "
        "alone: under F-9a the node block is the experimental condition and the harness "
        "must not invent one.",
    )
    ap.add_argument(
        "--threshold-ms",
        type=float,
        default=manifest_mod.SEND_LAG_THRESHOLD_MS,
        help="send-lag ceiling; any breach in the measurement window invalidates the run",
    )
    args = ap.parse_args()

    run_dir = args.out / args.run_id
    pre_path = args.manifest or find_pre_run(run_dir)
    if args.manifest is not None and not args.manifest.is_file():
        ap.error(f"--manifest {args.manifest} does not exist")
    pre = json.loads(pre_path.read_text()) if pre_path is not None else None

    nodes = json.loads(args.nodes.read_text()) if args.nodes is not None else None
    # Everything below is checked before the run, not after it: discovering at the end of a
    # ten-minute replay that the record cannot be written costs the run.
    if pre is not None:
        try:
            check_pre_run(pre, run_id=args.run_id, sha256=args.sha256, policy=args.policy)
        except ValueError as exc:
            ap.error(f"{pre_path}: {exc}")
        if nodes is not None and nodes != pre["nodes"]:
            ap.error(f"--nodes differs from the node block in {pre_path}; pass one of them")
        nodes = pre["nodes"]
        print(f"pre-run manifest {pre_path}")
    sha256 = args.sha256 or (pre or {}).get("trace_sha256")
    # A C-6 manifest must carry the trace's sha256 (the schema pins it to 64 hex characters).
    if nodes is not None and not sha256:
        ap.error("--nodes writes a manifest, which must carry the trace hash; pass --sha256")

    result = asyncio.run(
        replay(
            trace_path=args.trace,
            scheduler_endpoint=args.scheduler,
            run_id=args.run_id,
            expect_sha256=sha256,
            bind=args.bind,
            advertise_host=args.advertise,
            warmup_s=args.warmup_s,
            rate_scale=args.rate_scale,
            send_lag_threshold_ms=args.threshold_ms,
        )
    )

    log_path = write_run(run_dir, args.run_id, result.records)

    # `replay()` measures the load generator and nothing else, so the pool-level counters
    # are filled in here, where the node block and the clock measurement have been read.
    # The count reuses the launcher's rule rather than a second copy of it.
    clock_sync = json.loads(args.clock_sync.read_text()) if args.clock_sync else None
    if nodes is not None:
        man = post_run_manifest(
            run_id=args.run_id,
            result=result,
            trace_path=args.trace,
            trace_sha256=sha256,
            rate_scale=args.rate_scale,
            warmup_s=args.warmup_s,
            nodes=nodes,
            policy=(pre or {}).get("policy") or args.policy or "round_robin",
            pre=pre,
            clock_sync=clock_sync,
        )
        (run_dir / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
        validity = manifest_mod.Validity(
            **{k: v for k, v in man["validity"].items() if k != "valid"}
        )
    else:
        # The launcher assembles the manifest; the client contributes the half it measured.
        validity = result.validity
        (run_dir / "validity.json").write_text(json.dumps(validity.to_dict(), indent=2) + "\n")

    ok = sum(1 for r in result.records if r["status"] == "ok")
    print(f"{log_path}  {ok}/{len(result.records)} ok")
    print(
        f"max send lag {result.validity.max_send_lag_ms:.2f} ms  "
        f"violations {result.validity.send_lag_violations}"
    )

    # The exit code is what a sweep script reads, so it has to agree with the manifest it
    # sits next to.
    if validity.valid:
        print("run VALID")
        return 0
    print("run INVALID — not analysable:")
    for reason in validity.reasons():
        print(f"  - {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
