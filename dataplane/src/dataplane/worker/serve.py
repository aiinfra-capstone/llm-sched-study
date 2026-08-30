"""F-9 / F-10 / F-11 — the live worker: the gRPC face of one llama-server.

Week 1 gave me an *adapter* — something that can drive `llama-server` and describe what
came back. This is the thing that puts that adapter on the network, and it is what turns
"I calibrated a node" into "a node served a replayed trace". Week 3 needs it because a
validation anchor (F-23) is by definition a live-hardware run, and until something answers
`Execute` there is no such thing.

Three decisions in here are measurement decisions rather than plumbing decisions.

**The wrapper owns admission, not the engine.** llama.cpp will happily accept more
requests than it has slots and queue them internally, and if I let it, the wait would land
inside `service_ns` where nothing can separate it from compute. So the wrapper holds a
semaphore of exactly `--parallel` permits: `queue_wait_ns` is time spent waiting for a
permit, measured here, and `service_ns` is the engine's own span with no queueing in it.
That is what makes C-4's two duration columns mean two different things, and it is why the
DES can model a node as a fixed-capacity server without approximating anything (the slot
count *is* `SimNode.batch_capacity`).

**`Execute` returns before the work is done.** It answers `queued=true` and hands the
request to a task. A worker that blocked until the completion came back would apply
backpressure to the scheduler, and backpressure at the scheduler silently converts an
open-loop experiment into a closed-loop one — which is the single failure mode the replay
client's send-lag guard exists to catch. The guard watches the client; this is the other
end of the same rule.

**The response goes to the client, not back up through the scheduler (F-11).** The
`client_endpoint` rides on the request for exactly this reason. The scheduler is in the
request path because the payload is small; it is not in the response path because the
response is not.

One cost I am paying on purpose: `/slots` is read once per request, after the permit is
acquired and before the completion is posted. It costs a loopback round trip that lands in
neither `queue_wait_ns` nor `service_ns` — a small unattributed gap — and it buys a
`kv_occupancy_at_admission` that is a reading rather than an interpolation from the last
heartbeat. A per-request field filled in from a value up to a heartbeat interval old would
be indistinguishable from a fresh one in the log, which is the kind of quiet lie this study
cannot afford.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc

from dataplane.proto import sched_grpc, sched_pb2
from dataplane.worker.adapter import LiveState, ServiceResult
from dataplane.worker.heartbeat import DEFAULT_INTERVAL_S, HeartbeatEmitter
from dataplane.worker.llamacpp import LlamaCppAdapter
from dataplane.worker.log import WorkerLog

__all__ = ["WorkerConfig", "WorkerService", "main", "run_worker"]

# Consecutive `/slots` probes that must agree before a busy-slot excess is called a leak.
# Three at the default one-second cadence: long enough that the moment between a response
# being returned and its slot being released cannot trigger it, short enough that a leak is
# reported while the run it is corrupting is still going.
_LEAK_CONFIRMATIONS = 3


@dataclass
class WorkerConfig:
    """Everything about this node that ends up in the C-6 node block, plus where to log.

    `slots` is `--parallel` and nothing else. It is passed in rather than read from
    `/props` because it is the experimental condition under F-9a: the manifest claims a
    value, and the wrapper's admission control has to be the same number the manifest
    claims, or the recorded condition is not the one that ran.
    """

    node_id: str
    engine_endpoint: str
    slots: int = 4
    timeout_ceiling_ms: int = 60_000
    engine_version: str = "unknown"
    scheduler_endpoint: str | None = None
    log_dir: Path = Path("runs")
    heartbeat_interval_s: float = DEFAULT_INTERVAL_S

    def __post_init__(self) -> None:
        if self.slots < 1:
            raise ValueError(
                f"slots must be >= 1 — it is llama.cpp's --parallel and the wrapper's "
                f"admission width; got {self.slots}"
            )
        self.log_dir = Path(self.log_dir)


@dataclass
class _Counters:
    """What the wrapper knows about itself without asking the engine.

    Queue depth is *not* observable from `/slots`: llama.cpp admits exactly `--parallel`
    requests and cannot see the ones this wrapper is holding back. So the number the
    scheduler needs most under load is the one only this process has.
    """

    queued: int = 0
    inflight: int = 0
    served: int = 0
    failed: int = 0
    leaked_slots: int = 0


class WorkerService(sched_grpc.WorkerServicer):
    """One node. Answers `Execute`, delivers to the client, heartbeats to the scheduler."""

    def __init__(
        self,
        config: WorkerConfig,
        adapter: LlamaCppAdapter,
        *,
        channel_factory: Any = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.counters = _Counters()
        self.records: list[dict[str, Any]] = []
        self.run_id = ""
        self._log: WorkerLog | None = None
        self._sem = asyncio.Semaphore(config.slots)
        self._tasks: set[asyncio.Task] = set()
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._channel_factory = channel_factory or grpc.aio.insecure_channel
        self._emitter = HeartbeatEmitter(
            run_id="", node_id=config.node_id, interval_s=config.heartbeat_interval_s
        )
        self._leak_streak = 0

    # ---------------------------------------------------------------- run control

    def begin(self, run_id: str) -> None:
        """Open the C-4 log for a run. Idempotent for the run already open.

        Called from `Begin`, from a `BeginRun` arriving down the heartbeat stream, and —
        deliberately — from `Execute` itself when a request shows up for a run nobody
        announced. Refusing unannounced work would lose the head of a trace to a race
        between the scheduler's broadcast and the client's first arrival, and the head of
        the trace is the warmup I was going to discard anyway *only if I can see it*. The
        run_id is in the log's filename, so an unexpected one is visible rather than
        silent.
        """
        if run_id == self.run_id and self._log is not None:
            return
        if self._log is not None:
            self._log.close()
        self.run_id = run_id
        self._log = WorkerLog(self.config.log_dir, node_id=self.config.node_id, run_id=run_id)
        # Re-pointed, never replaced: `heartbeat_forever` holds a reference to this object
        # for the life of the process, so a fresh emitter here would keep beating the run
        # that just ended while the queue depth the request path updates went to an object
        # nobody was reading.
        self._emitter.start_run(run_id)

    async def drain(self, timeout_s: float = 120.0) -> None:
        """Let every admitted request finish before the log closes.

        A worker that exits with requests in flight produces a C-4 file that is missing
        exactly the slowest requests in the run — which is the tail the whole study is
        about. Timing out here is reported, not swallowed: an un-drained task is a real
        anomaly and the run should be looked at.
        """
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout_s)
        for ch in self._channels.values():
            await ch.close()
        self._channels.clear()
        if self._log is not None:
            self._log.close()
            self._log = None

    # ---------------------------------------------------------------- Worker service

    async def Begin(self, request, context):  # gRPC servicer name
        self.begin(request.run_id)
        return sched_pb2.ExecuteAck(req_id=request.run_id, queued=True)

    async def End(self, request, context):  # gRPC servicer name
        await self.drain()
        return sched_pb2.ExecuteAck(req_id=request.run_id, queued=True)

    async def Execute(self, request, context):  # gRPC servicer name
        """Admit and acknowledge. The work happens in a task; see the module docstring."""
        self.begin(request.run_id)
        task = asyncio.create_task(self.serve_one(request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return sched_pb2.ExecuteAck(req_id=request.req_id, queued=True)

    # ---------------------------------------------------------------- the request path

    async def serve_one(self, request) -> dict[str, Any]:
        """One request, end to end on this node. Returns the C-4 record it wrote."""
        admitted_ns = time.monotonic_ns()
        self.counters.queued += 1
        self._emitter.set_queue_depth(self.counters.queued)

        async with self._sem:
            self.counters.queued -= 1
            self._emitter.set_queue_depth(self.counters.queued)
            # Stamped before the /slots read, so the probe's cost is not billed to the
            # queue. It is not billed to service either — the adapter stamps its own span.
            queue_wait_ns = time.monotonic_ns() - admitted_ns
            state = await self.adapter.live_state()
            self.counters.inflight += 1
            try:
                result = await self.adapter.complete(
                    list(request.prompt_token_ids), request.output_len
                )
            finally:
                self.counters.inflight -= 1

        self._emitter.observe_completion(result.decode_tokens_per_s)
        self.counters.served += 1
        if result.status != "ok":
            self.counters.failed += 1

        # Delivered first. The C-4 write is a local flush of microseconds, but it is still
        # work between the engine finishing and the client hearing about it, and e2e
        # latency is the dependent variable — I would rather pay that cost in the log than
        # in the measurement.
        await self._deliver(request, result, queue_wait_ns)
        record = self._write(request.req_id, result, queue_wait_ns, state)
        await self._report(request, result)
        return record

    def _write(
        self, req_id: str, result: ServiceResult, queue_wait_ns: int, state: LiveState
    ) -> dict[str, Any]:
        assert self._log is not None  # begin() runs before any request path
        record = self._log.log(
            req_id=req_id,
            result=result,
            queue_wait_ns=queue_wait_ns,
            state_at_admission=state,
        )
        self.records.append(record)
        return record

    async def _deliver(self, request, result: ServiceResult, queue_wait_ns: int) -> None:
        """F-11 — worker to client, directly. A client that vanished is its own log's problem."""
        delivery = sched_pb2.ResponseDelivery(
            run_id=request.run_id,
            req_id=request.req_id,
            node_id=self.config.node_id,
            output_tokens=result.output_tokens,
            status=result.status,
            worker_service_ns=result.service_ns,
            worker_queue_wait_ns=queue_wait_ns,
        )
        try:
            await sched_grpc.ClientStub(self._channel(request.client_endpoint)).Deliver(delivery)
        except grpc.aio.AioRpcError:
            # The client records this as a timeout on its own clock, which is the only
            # clock allowed to measure it. Raising here would kill the task and lose the
            # C-4 record for a request that actually ran.
            pass

    async def _report(self, request, result: ServiceResult) -> None:
        """C-1 `Completion` — so the scheduler learns about a finish now, not at the next beat.

        This is invariant 1 of the proto: without it, completion news arrives only on the
        heartbeat tick, which would add a second uncontrolled staleness source alongside
        the one H3 injects on purpose.
        """
        if not self.config.scheduler_endpoint:
            return
        try:
            await sched_grpc.SchedulerStub(
                self._channel(self.config.scheduler_endpoint)
            ).ReportCompletion(
                sched_pb2.Completion(
                    run_id=request.run_id,
                    node_id=self.config.node_id,
                    req_id=request.req_id,
                    status=result.status,
                    service_ns=result.service_ns,
                )
            )
        except grpc.aio.AioRpcError:
            pass

    def _channel(self, endpoint: str) -> grpc.aio.Channel:
        if endpoint not in self._channels:
            self._channels[endpoint] = self._channel_factory(endpoint)
        return self._channels[endpoint]

    # ---------------------------------------------------------------- F-10 heartbeats

    async def probe(self) -> LiveState:
        """The engine's view, checked against the wrapper's own books before it is believed.

        **The invariant: the engine cannot be busier than I made it.** This wrapper is the
        only thing that posts to this `llama-server`, and it counts exactly what it has in
        flight. So `/slots` reporting more busy slots than `counters.inflight` means the
        engine is holding a slot for work nobody is waiting on — the slot leaked.

        That is not hypothetical. On the pinned build, the HTTP 500 raised when generated
        output ends mid-character (see the README) cancels the task **without releasing its
        slot**: one anchor campaign logged 304 `launch_slot_` against 301 `release`, and
        `/slots` then showed three of four slots permanently `is_processing` at 0% GPU
        utilization. The node does not fail at that point, which is what makes it dangerous
        — it silently loses a quarter of its capacity per leak and the run keeps producing
        plausible, wrong numbers until the last slot goes and everything times out.

        Confirmed over `_LEAK_CONFIRMATIONS` consecutive probes rather than on one reading,
        because a slot is genuinely busy for a moment after its response is returned and
        before the engine releases it, and a single sample would call that a leak.

        A confirmed leak makes the node report `degraded` — which is what C-1's
        `engine_state` is for, and what lets the scheduler route around a node whose
        capacity is no longer what the manifest says it is.
        """
        state = await self.adapter.live_state()
        if state.state != "ready":
            return state
        excess = state.inflight - self.counters.inflight
        self._leak_streak = self._leak_streak + 1 if excess > 0 else 0
        if self._leak_streak < _LEAK_CONFIRMATIONS:
            return state
        self.counters.leaked_slots = excess
        return LiveState(
            inflight=state.inflight,
            slots_total=state.slots_total,
            kv_frac=state.kv_frac,
            queue_depth=state.queue_depth,
            recent_tok_s=state.recent_tok_s,
            state="degraded",
        )

    async def heartbeat_forever(
        self, *, stop: asyncio.Event, reconnect_s: float | None = 2.0
    ) -> None:
        """Beat to the scheduler until `stop`, reconnecting if the stream drops.

        The beat cadence and the transport are separate tasks on purpose. If they were one
        loop, a scheduler that went away would stop the clock as well as the stream, and
        the `seq` numbers would resume contiguously afterwards — hiding the gap from the
        only mechanism that can detect it. Here the emitter keeps counting into a queue
        whether or not anyone is listening, so a reconnect shows up as the hole in `seq`
        that it is.
        """
        if not self.config.scheduler_endpoint:
            return
        outbox: asyncio.Queue = asyncio.Queue()
        beats = asyncio.create_task(self._emitter.run(self.probe, outbox.put_nowait, stop=stop))
        try:
            while not stop.is_set():
                try:
                    await self._stream(outbox, stop)
                except grpc.aio.AioRpcError:
                    if reconnect_s is None:
                        return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=reconnect_s)
                    except TimeoutError:
                        pass
        finally:
            beats.cancel()

    async def _stream(self, outbox: asyncio.Queue, stop: asyncio.Event) -> None:
        """One `StreamHeartbeat` call: drain the outbox up, take `BeginRun` down."""

        async def outgoing():
            # Polled with a timeout rather than blocked on `get()`, so that setting `stop`
            # while the node is idle ends the stream within one interval instead of
            # leaving a half-open call for gRPC to time out on at shutdown.
            while not stop.is_set():
                try:
                    hb = await asyncio.wait_for(outbox.get(), self.config.heartbeat_interval_s)
                except TimeoutError:
                    continue
                yield sched_pb2.Heartbeat(**hb.to_dict())

        stub = sched_grpc.SchedulerStub(self._channel(self.config.scheduler_endpoint))
        call = stub.StreamHeartbeat(outgoing())
        async for begin in call:
            self.begin(begin.run_id)


async def run_worker(
    config: WorkerConfig,
    *,
    bind: str = "0.0.0.0:50061",
    stop: asyncio.Event | None = None,
    wait_for_engine_s: float = 600.0,
    engine_poll_s: float = 1.0,
) -> int:
    """Bring up one node: wait for the engine, serve, heartbeat, drain on the way out.

    The engine wait is a poll on `/health` rather than a sleep. Loading the 8B GGUF is
    minutes on a cold page cache and seconds when warm, so any constant would be either a
    wasted wait or a race — and a worker that announced itself before the model was loaded
    would take dispatches it cannot serve.
    """
    stop = stop or asyncio.Event()
    adapter = LlamaCppAdapter(
        config.engine_endpoint,
        node_id=config.node_id,
        timeout_ceiling_ms=config.timeout_ceiling_ms,
        engine_version=config.engine_version,
    )
    deadline = time.monotonic() + wait_for_engine_s
    while not await adapter.health():
        if time.monotonic() > deadline:
            await adapter.aclose()
            print(f"engine at {config.engine_endpoint} never became healthy", flush=True)
            return 1
        await asyncio.sleep(engine_poll_s)

    service = WorkerService(config, adapter)
    server = grpc.aio.server()
    sched_grpc.add_WorkerServicer_to_server(service, server)
    port = server.add_insecure_port(bind)
    await server.start()
    hb = asyncio.create_task(service.heartbeat_forever(stop=stop))
    print(
        f"worker {config.node_id} on :{port} -> {config.engine_endpoint} "
        f"({config.slots} slots, {config.engine_version})",
        flush=True,
    )
    try:
        await stop.wait()
    finally:
        hb.cancel()
        await service.drain()
        await server.stop(grace=2.0)
        await adapter.aclose()
    print(
        f"worker {config.node_id}: {service.counters.served} served, "
        f"{service.counters.failed} failed"
        + (
            f", {service.counters.leaked_slots} ENGINE SLOT(S) LEAKED — this node was not "
            "running at the --parallel the manifest claims"
            if service.counters.leaked_slots
            else ""
        ),
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="F-9/F-10/F-11 — run one llama.cpp pool node")
    ap.add_argument("--node-id", required=True)
    ap.add_argument(
        "--engine", required=True, help="llama-server base URL, e.g. http://127.0.0.1:18080"
    )
    ap.add_argument("--bind", default="0.0.0.0:50061", help="where the scheduler sends Execute")
    ap.add_argument("--scheduler", help="host:port for heartbeats and completions (F-10)")
    ap.add_argument(
        "--slots",
        type=int,
        default=4,
        help="llama.cpp --parallel; the wrapper admits exactly this many at a time",
    )
    ap.add_argument("--timeout-ms", type=int, default=60_000)
    ap.add_argument("--engine-version", default="unknown")
    ap.add_argument("--log-dir", type=Path, default=Path("runs"))
    ap.add_argument("--heartbeat-s", type=float, default=DEFAULT_INTERVAL_S)
    args = ap.parse_args(argv)

    config = WorkerConfig(
        node_id=args.node_id,
        engine_endpoint=args.engine,
        slots=args.slots,
        timeout_ceiling_ms=args.timeout_ms,
        engine_version=args.engine_version,
        scheduler_endpoint=args.scheduler,
        log_dir=args.log_dir,
        heartbeat_interval_s=args.heartbeat_s,
    )
    try:
        return asyncio.run(run_worker(config, bind=args.bind))
    except KeyboardInterrupt:  # pragma: no cover - operator action
        return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
