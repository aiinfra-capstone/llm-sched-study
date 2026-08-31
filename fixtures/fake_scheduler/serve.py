"""The fake scheduler (A's fixture) — round-robins blindly, so the replay client is not
blocked on a real control plane.

No policy, no state store, no estimates. It accepts `Dispatch`, answers `DispatchAck`,
and forwards `Execute` to whichever worker is next in the rotation.

**It deliberately writes no scheduler log.** A C-4 decision record requires a full
`candidates` array with `queue_depth`, `capability_tok_s`, and `estimate_age_ms` per
candidate (F-3) — and a fixture with no state store cannot fill those in honestly.
Fabricating them would put plausible-looking numbers into the join pipeline, which is
strictly worse than a gap. That record is Aditya's to emit from the real scheduler.

Two modes:

  --worker HOST:PORT [...]   forward Execute to real (or fake) workers. This is the mode
                             that matches the fixture contract.

  --loopback                 no workers at all: this process delivers the response itself
                             from an analytic service-time model. A crutch for exercising
                             the replay client solo — it is NOT a worker, its timings mean
                             nothing, and no calibration may be run against it. Delete
                             this whole directory after Week 3.

Run:
    uv run --project dataplane python fixtures/fake_scheduler/serve.py --loopback
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import time

import grpc
from dataplane.proto import sched_grpc, sched_pb2


class FakeScheduler(sched_grpc.SchedulerServicer):
    def __init__(
        self, workers: list[str], loopback: bool, prefill_ms: float, per_token_ms: float
    ) -> None:
        self._workers = workers
        self._rotation = itertools.cycle(workers) if workers else None
        self._loopback = loopback
        self._prefill_ms = prefill_ms
        self._per_token_ms = per_token_ms
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._seq = itertools.count(1)
        self.dispatched = 0

    def _channel(self, endpoint: str) -> grpc.aio.Channel:
        if endpoint not in self._channels:
            self._channels[endpoint] = grpc.aio.insecure_channel(endpoint)
        return self._channels[endpoint]

    async def Dispatch(self, request, context):
        self.dispatched += 1

        if self._loopback:
            asyncio.create_task(self._deliver_later(request))
            return sched_pb2.DispatchAck(
                req_id=request.req_id, chosen_node="loopback", accepted=True
            )

        node = next(self._rotation)
        try:
            await sched_grpc.WorkerStub(self._channel(node)).Execute(
                sched_pb2.ExecuteRequest(
                    run_id=request.run_id,
                    req_id=request.req_id,
                    prompt_token_ids=request.prompt_token_ids,
                    output_len=request.output_len,
                    priority=request.priority,
                    bucket_id=request.bucket_id,
                    client_endpoint=request.client_endpoint,
                    decision_seq=next(self._seq),
                )
            )
        except grpc.aio.AioRpcError:
            return sched_pb2.DispatchAck(
                req_id=request.req_id,
                chosen_node=node,
                accepted=False,
                reject_reason="no_admissible_node",
            )
        return sched_pb2.DispatchAck(req_id=request.req_id, chosen_node=node, accepted=True)

    async def _deliver_later(self, request) -> None:
        """Loopback only: an analytic service time, then a direct delivery to the client (F-11)."""
        service_ms = self._prefill_ms + self._per_token_ms * request.output_len
        started = time.monotonic_ns()
        await asyncio.sleep(service_ms / 1000.0)
        try:
            await sched_grpc.ClientStub(self._channel(request.client_endpoint)).Deliver(
                sched_pb2.ResponseDelivery(
                    run_id=request.run_id,
                    req_id=request.req_id,
                    node_id="loopback",
                    output_tokens=request.output_len,
                    status="ok",
                    worker_service_ns=time.monotonic_ns() - started,
                    worker_queue_wait_ns=0,
                )
            )
        except grpc.aio.AioRpcError:
            pass  # the client went away; its own log will record the timeout

    async def StreamHeartbeat(self, request_iterator, context):
        """Accept and discard. A fixture that has no state store has nothing to update."""
        async for _ in request_iterator:
            pass
        return
        yield  # pragma: no cover - makes this an async generator for grpc.aio

    async def ReportCompletion(self, request, context):
        return sched_pb2.ExecuteAck(req_id=request.req_id, queued=True)


async def serve(args: argparse.Namespace) -> None:
    servicer = FakeScheduler(args.worker, args.loopback, args.prefill_ms, args.per_token_ms)
    server = grpc.aio.server()
    sched_grpc.add_SchedulerServicer_to_server(servicer, server)
    port = server.add_insecure_port(args.bind)
    await server.start()

    where = "loopback (NOT a worker)" if args.loopback else ", ".join(args.worker)
    print(f"fake scheduler on :{port} -> {where}", flush=True)
    await server.wait_for_termination()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fake scheduler fixture — blind round-robin, no policy"
    )
    ap.add_argument("--bind", default="0.0.0.0:50051")
    ap.add_argument("--worker", action="append", default=[], help="worker endpoint; repeatable")
    ap.add_argument(
        "--loopback", action="store_true", help="deliver responses from an analytic model"
    )
    ap.add_argument(
        "--prefill-ms", type=float, default=40.0, help="loopback service model: fixed cost"
    )
    ap.add_argument(
        "--per-token-ms", type=float, default=8.0, help="loopback service model: per output token"
    )
    args = ap.parse_args()

    if not args.worker and not args.loopback:
        ap.error("give at least one --worker, or --loopback to run without one")
    if args.worker and args.loopback:
        ap.error("--loopback replaces workers; do not pass both")

    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
