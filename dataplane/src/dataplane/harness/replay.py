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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc

from dataplane.harness import gen_trace
from dataplane.harness import manifest as manifest_mod
from dataplane.harness.prompts import materialize_all
from dataplane.proto import sched_grpc, sched_pb2

__all__ = ["ReplayResult", "replay"]


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
        "intended_offset_s": rec["arrival_offset_s"],
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


async def replay(
    *,
    trace_path: str | Path,
    scheduler_endpoint: str,
    run_id: str,
    expect_sha256: str | None = None,
    bind: str = "0.0.0.0:0",
    advertise_host: str | None = None,
    warmup_s: float = 0.0,
    send_lag_threshold_ms: float = manifest_mod.SEND_LAG_THRESHOLD_MS,
) -> ReplayResult:
    """Replay a trace open-loop against a scheduler and return the C-4 records + validity."""
    header, body = gen_trace.load(trace_path, expect_sha256=expect_sha256)
    timeout_s = header["admissible"]["timeout_ceiling_ms"] / 1000.0

    # Everything expensive happens here, before t0. Tokenising inside the timing loop is
    # the most common cause of send-lag drift at high lambda.
    prompts = materialize_all(body, header["vocab_size"])

    sink = _DeliverySink()
    server = grpc.aio.server()
    sched_grpc.add_ClientServicer_to_server(sink, server)
    port = server.add_insecure_port(bind)
    await server.start()

    host = advertise_host or bind.rsplit(":", 1)[0]
    if host in ("0.0.0.0", ""):
        raise ValueError(
            "cannot advertise 0.0.0.0 to a worker on another host; pass --advertise <this host's LAN address>"
        )
    client_endpoint = f"{host}:{port}"

    try:
        async with grpc.aio.insecure_channel(scheduler_endpoint) as channel:
            stub = sched_grpc.SchedulerStub(channel)
            tasks: list[asyncio.Task] = []

            t0 = time.monotonic_ns()
            for rec in body:
                target_ns = t0 + round(rec["arrival_offset_s"] * 1e9)
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
        "--bind", default="0.0.0.0:0", help="where this client listens for Deliver (F-11)"
    )
    ap.add_argument(
        "--advertise", help="host the worker should send responses to (this host's LAN address)"
    )
    ap.add_argument("--out", type=Path, default=Path("runs"), help="run directory root")
    ap.add_argument("--warmup-s", type=float, default=0.0)
    ap.add_argument(
        "--policy",
        default="round_robin",
        help="recorded in the manifest; the client does not choose",
    )
    ap.add_argument(
        "--nodes",
        type=Path,
        help="JSON array of C-6 node blocks from the launcher. Without it the client writes "
        "its validity block alone — under F-9a the node block is the experimental "
        "condition and the harness must not invent one.",
    )
    ap.add_argument(
        "--threshold-ms",
        type=float,
        default=manifest_mod.SEND_LAG_THRESHOLD_MS,
        help="send-lag ceiling; any breach in the measurement window invalidates the run",
    )
    args = ap.parse_args()

    result = asyncio.run(
        replay(
            trace_path=args.trace,
            scheduler_endpoint=args.scheduler,
            run_id=args.run_id,
            expect_sha256=args.sha256,
            bind=args.bind,
            advertise_host=args.advertise,
            warmup_s=args.warmup_s,
            send_lag_threshold_ms=args.threshold_ms,
        )
    )

    run_dir = args.out / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"client_{args.run_id}.jsonl"
    log_path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in result.records)
    )

    if args.nodes is not None:
        config = {
            "duration_s": result.header["duration_s"],
            "warmup_s": args.warmup_s,
            "arrival": result.header["arrival"],
            "length_dist": result.header["length_dist"],
            "gen_seed": result.header["gen_seed"],
        }
        man = manifest_mod.build(
            run_id=args.run_id,
            config=config,
            trace_path=args.trace,
            trace_sha256=args.sha256 or "",
            validity=result.validity,
            policy=args.policy,
            nodes=json.loads(args.nodes.read_text()),
        )
        (run_dir / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    else:
        # The launcher assembles the manifest; the client contributes the half it measured.
        (run_dir / "validity.json").write_text(
            json.dumps(result.validity.to_dict(), indent=2) + "\n"
        )

    ok = sum(1 for r in result.records if r["status"] == "ok")
    print(f"{log_path}  {ok}/{len(result.records)} ok")
    print(
        f"max send lag {result.validity.max_send_lag_ms:.2f} ms  "
        f"violations {result.validity.send_lag_violations}"
    )

    if result.validity.valid:
        print("run VALID")
        return 0
    print("run INVALID — not analysable:")
    for reason in result.validity.reasons():
        print(f"  - {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
