"""F-9 / F-10 / F-11 — the live worker, and the four things it must not get wrong.

1. **Queue wait is measured by the wrapper.** If the engine is allowed to hold the backlog,
   the wait lands inside `service_ns` and nothing downstream can separate waiting from
   computing — which would make C-4's two duration columns one column with two names.
2. **`Execute` returns before the work does.** Blocking would apply backpressure to the
   scheduler, which silently converts an open-loop experiment into a closed-loop one.
3. **The response goes straight to the client (F-11).** The scheduler is in the request
   path and not in the response path, on purpose.
4. **A failure is still a record.** The cliff is made of the requests that did not succeed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import grpc
import pytest

from dataplane.proto import sched_grpc, sched_pb2
from dataplane.worker import serve
from dataplane.worker.adapter import LiveState, ServiceResult


class _FakeAdapter:
    """A llama-server that does what the test says, at the speed the test says."""

    def __init__(self, *, service_s=0.02, status="ok", slots_busy=1, slots_total=4, healthy=True):
        self.service_s = service_s
        self.status = status
        self.slots_busy = slots_busy
        self.slots_total = slots_total
        self.healthy = healthy
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.peak_concurrent = 0
        self._active = 0

    async def health(self) -> bool:
        return self.healthy

    async def live_state(self) -> LiveState:
        return LiveState(
            inflight=self.slots_busy,
            slots_total=self.slots_total,
            kv_frac=self.slots_busy / self.slots_total,
            state="ready",
        )

    async def complete(self, prompt_tokens, output_len) -> ServiceResult:
        self.calls.append((len(prompt_tokens), output_len))
        self._active += 1
        self.peak_concurrent = max(self.peak_concurrent, self._active)
        try:
            await asyncio.sleep(self.service_s)
        finally:
            self._active -= 1
        ok = self.status == "ok"
        return ServiceResult(
            status=self.status,
            service_ns=int(self.service_s * 1e9),
            prompt_tokens=len(prompt_tokens),
            output_tokens=output_len if ok else 0,
            prefill_ns=int(self.service_s * 1e9 * 0.2) if ok else None,
            decode_ns=int(self.service_s * 1e9 * 0.8) if ok else None,
        )

    async def aclose(self) -> None:
        self.closed = True


class _Sink(sched_grpc.ClientServicer):
    """The replay client's end of F-11."""

    def __init__(self) -> None:
        self.delivered: list = []

    async def Deliver(self, request, context):
        self.delivered.append(request)
        return sched_pb2.ExecuteAck(req_id=request.req_id, queued=True)


class _Scheduler(sched_grpc.SchedulerServicer):
    def __init__(self, begin_run_id: str | None = None) -> None:
        self.completions: list = []
        self.heartbeats: list = []
        self.begin_run_id = begin_run_id

    async def Dispatch(self, request, context):  # pragma: no cover - not on this path
        return sched_pb2.DispatchAck(req_id=request.req_id, accepted=False)

    async def ReportCompletion(self, request, context):
        self.completions.append(request)
        return sched_pb2.ExecuteAck(req_id=request.req_id, queued=True)

    async def StreamHeartbeat(self, request_iterator, context):
        if self.begin_run_id:
            yield sched_pb2.BeginRun(run_id=self.begin_run_id)
        async for hb in request_iterator:
            self.heartbeats.append(hb)


async def _start(servicer, adder):
    server = grpc.aio.server()
    adder(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, f"127.0.0.1:{port}"


def _config(tmp_path, **over):
    return serve.WorkerConfig(
        node_id=over.pop("node_id", "n1"),
        engine_endpoint="http://127.0.0.1:1",
        slots=over.pop("slots", 4),
        log_dir=tmp_path,
        heartbeat_interval_s=over.pop("heartbeat_interval_s", 0.05),
        **over,
    )


def _request(req_id="r0001", *, run_id="run_a", client_endpoint="", output_len=8):
    return sched_pb2.ExecuteRequest(
        run_id=run_id,
        req_id=req_id,
        prompt_token_ids=[1, 2, 3, 4],
        output_len=output_len,
        priority=0,
        bucket_id="p128_o64",
        client_endpoint=client_endpoint,
        decision_seq=1,
    )


def test_a_slot_count_below_one_is_not_a_node() -> None:
    with pytest.raises(ValueError, match="admission width"):
        serve.WorkerConfig(node_id="n1", engine_endpoint="http://x", slots=0)
    assert isinstance(serve.WorkerConfig("n1", "http://x", log_dir="runs").log_dir, Path)


def test_the_wrapper_holds_the_backlog_so_queue_wait_is_not_inside_service(tmp_path) -> None:
    """With one slot, the second request waits in the wrapper. If the engine held it
    instead, that wait would be indistinguishable from compute in `service_ns`."""

    async def go():
        adapter = _FakeAdapter(service_s=0.15)
        svc = serve.WorkerService(_config(tmp_path, slots=1), adapter)
        svc.begin("run_a")
        records = await asyncio.gather(
            svc.serve_one(_request("r0001")), svc.serve_one(_request("r0002"))
        )
        await svc.drain()
        return adapter, records

    adapter, records = asyncio.run(go())
    assert adapter.peak_concurrent == 1
    waits = sorted(r["queue_wait_ns"] for r in records)
    assert waits[0] < 10_000_000  # the first request waited for nothing
    assert waits[1] > 100_000_000  # the second waited for the first
    assert all(r["service_ns"] < 200_000_000 for r in records)


def test_the_engine_is_never_given_more_than_its_slot_count(tmp_path) -> None:
    async def go():
        adapter = _FakeAdapter(service_s=0.05)
        svc = serve.WorkerService(_config(tmp_path, slots=2), adapter)
        svc.begin("run_a")
        await asyncio.gather(*(svc.serve_one(_request(f"r{i:04d}")) for i in range(6)))
        await svc.drain()
        return adapter

    assert asyncio.run(go()).peak_concurrent == 2


def test_execute_acknowledges_before_the_completion_comes_back(tmp_path) -> None:
    """Blocking here would apply backpressure to the scheduler, and backpressure at the
    scheduler is how an open-loop experiment quietly becomes a closed-loop one."""

    async def go():
        adapter = _FakeAdapter(service_s=0.3)
        svc = serve.WorkerService(_config(tmp_path), adapter)
        started = asyncio.get_running_loop().time()
        ack = await svc.Execute(_request(), None)
        acked = asyncio.get_running_loop().time() - started
        await svc.drain()
        return ack, acked, svc

    ack, acked, svc = asyncio.run(go())
    assert ack.queued and ack.req_id == "r0001"
    assert acked < 0.1
    assert len(svc.records) == 1


def test_the_response_goes_to_the_client_and_the_completion_to_the_scheduler(tmp_path) -> None:
    """F-11 plus proto invariant 1: the client hears about the result directly, and the
    scheduler hears about the finish now rather than at the next heartbeat tick."""

    async def go():
        sink = _Sink()
        sched = _Scheduler()
        client_server, client_ep = await _start(sink, sched_grpc.add_ClientServicer_to_server)
        sched_server, sched_ep = await _start(sched, sched_grpc.add_SchedulerServicer_to_server)
        svc = serve.WorkerService(_config(tmp_path, scheduler_endpoint=sched_ep), _FakeAdapter())
        svc.begin("run_a")
        await svc.serve_one(_request(client_endpoint=client_ep))
        await svc.drain()
        await client_server.stop(None)
        await sched_server.stop(None)
        return sink, sched

    sink, sched = asyncio.run(go())
    assert [d.req_id for d in sink.delivered] == ["r0001"]
    assert sink.delivered[0].node_id == "n1"
    assert sink.delivered[0].output_tokens == 8
    assert [c.req_id for c in sched.completions] == ["r0001"]


def test_a_client_that_went_away_does_not_cost_the_worker_its_record(tmp_path) -> None:
    """The client records that as a timeout on its own clock — the only clock allowed to
    measure it. Losing the C-4 record for a request that actually ran would be worse."""

    async def go():
        svc = serve.WorkerService(
            _config(tmp_path, scheduler_endpoint="127.0.0.1:1"), _FakeAdapter()
        )
        svc.begin("run_a")
        record = await svc.serve_one(_request(client_endpoint="127.0.0.1:1"))
        await svc.drain()
        return record, svc

    record, svc = asyncio.run(go())
    assert record["status"] == "ok"
    assert len(svc.records) == 1


def test_a_failed_request_is_still_written_and_still_delivered(tmp_path) -> None:
    async def go():
        sink = _Sink()
        server, endpoint = await _start(sink, sched_grpc.add_ClientServicer_to_server)
        svc = serve.WorkerService(_config(tmp_path), _FakeAdapter(status="timeout"))
        svc.begin("run_a")
        await svc.serve_one(_request(client_endpoint=endpoint))
        await svc.drain()
        await server.stop(None)
        return sink, svc

    sink, svc = asyncio.run(go())
    assert svc.counters.failed == 1
    assert sink.delivered[0].status == "timeout"
    written = json.loads((tmp_path / "worker_n1_run_a.jsonl").read_text().strip())
    assert written["status"] == "timeout"
    assert "prefill_ns" not in written


def test_a_second_run_gets_its_own_log_and_the_first_is_closed(tmp_path) -> None:
    async def go():
        svc = serve.WorkerService(_config(tmp_path), _FakeAdapter())
        await svc.Begin(sched_pb2.BeginRun(run_id="run_a"), None)
        await svc.serve_one(_request("r0001", run_id="run_a"))
        svc.begin("run_a")  # idempotent for the run already open
        await svc.Begin(sched_pb2.BeginRun(run_id="run_b"), None)
        await svc.serve_one(_request("r0002", run_id="run_b"))
        await svc.End(sched_pb2.EndRun(run_id="run_b"), None)
        return svc

    svc = asyncio.run(go())
    assert svc.run_id == "run_b"
    assert len((tmp_path / "worker_n1_run_a.jsonl").read_text().splitlines()) == 1
    assert len((tmp_path / "worker_n1_run_b.jsonl").read_text().splitlines()) == 1


def test_an_unannounced_run_opens_its_own_log_rather_than_losing_the_head_of_the_trace(
    tmp_path,
) -> None:
    """The scheduler's broadcast and the client's first arrival race. Refusing the work
    would lose whichever requests won."""

    async def go():
        svc = serve.WorkerService(_config(tmp_path), _FakeAdapter())
        await svc.Execute(_request(run_id="unannounced"), None)
        await svc.drain()
        return svc

    asyncio.run(go())
    assert (tmp_path / "worker_n1_unannounced.jsonl").exists()


def test_heartbeats_carry_the_wrappers_queue_depth_which_slots_cannot_see(tmp_path) -> None:
    """llama.cpp admits exactly `--parallel`; the requests this wrapper is holding back are
    invisible to `/slots`, and they are the number the scheduler needs most under load."""

    async def go():
        sched = _Scheduler(begin_run_id="run_from_scheduler")
        server, endpoint = await _start(sched, sched_grpc.add_SchedulerServicer_to_server)
        adapter = _FakeAdapter(service_s=0.4, slots_busy=1)
        svc = serve.WorkerService(_config(tmp_path, slots=1, scheduler_endpoint=endpoint), adapter)
        svc.begin("run_a")
        stop = asyncio.Event()
        hb = asyncio.create_task(svc.heartbeat_forever(stop=stop))
        work = asyncio.gather(*(svc.serve_one(_request(f"r{i:04d}")) for i in range(4)))
        await asyncio.sleep(0.25)
        depths = [h.queue_depth for h in sched.heartbeats]
        await work
        stop.set()
        await hb
        await svc.drain()
        await server.stop(None)
        return sched, depths, svc

    sched, depths, svc = asyncio.run(go())
    assert sched.heartbeats, "the scheduler heard nothing"
    assert max(depths) >= 1
    assert [h.seq for h in sched.heartbeats] == sorted({h.seq for h in sched.heartbeats})
    assert sched.heartbeats[0].node_id == "n1"
    # BeginRun arrived down the same stream and switched the run over.
    assert svc.run_id == "run_from_scheduler"


def test_a_second_run_keeps_beating_from_the_same_emitter(tmp_path) -> None:
    """The heartbeat task holds one emitter for the life of the process. If `begin` built a
    fresh one per run, the stream would keep reporting the run that ended while the queue
    depth the request path updates went to an object nobody was reading — and `seq` would
    restart, which the scheduler reads as the largest gap of the run."""

    async def go():
        svc = serve.WorkerService(_config(tmp_path), _FakeAdapter())
        svc.begin("run_a")
        first = svc._emitter
        first.observe_completion(50.0)
        svc.begin("run_b")
        assert svc._emitter is first
        assert first.run_id == "run_b"
        beat = svc._emitter.next(await svc.probe())
        return beat

    beat = asyncio.run(go())
    assert beat.run_id == "run_b"
    assert beat.seq == 1  # per-node monotonic, and this node has beaten once
    assert beat.recent_tokens_per_s == 0.0  # run_a's rate is not run_b's


def test_an_engine_busier_than_the_wrapper_made_it_is_a_leaked_slot(tmp_path) -> None:
    """This wrapper is the only thing posting to this engine, so `/slots` reporting more busy
    slots than the wrapper has in flight means the engine is holding a slot for work nobody
    is waiting on. It costs a quarter of the node's capacity per leak and nothing else
    notices — the run keeps producing plausible numbers until the last slot goes."""

    async def go():
        adapter = _FakeAdapter(slots_busy=0)
        svc = serve.WorkerService(_config(tmp_path), adapter)
        assert (await svc.probe()).state == "ready"

        adapter.slots_busy = 2  # two slots busy, nothing in flight here
        for _ in range(serve._LEAK_CONFIRMATIONS - 1):
            # Not on one reading: a slot is genuinely busy for a moment after its response
            # is returned and before the engine releases it.
            assert (await svc.probe()).state == "ready"
        confirmed = await svc.probe()
        assert confirmed.state == "degraded"
        assert svc.counters.leaked_slots == 2

        adapter.slots_busy = 0
        assert (await svc.probe()).state == "ready"

        adapter.healthy = False
        return svc

    svc = asyncio.run(go())
    assert svc.counters.leaked_slots == 2


def test_a_degraded_engine_is_reported_as_it_is_not_re_examined(tmp_path) -> None:
    """`/slots` unreadable means the node reports degraded with kv_frac -1.0. There is no
    inflight count to compare against, so the leak check has nothing to say and does not
    overwrite the state the adapter already decided."""

    async def go():
        adapter = _FakeAdapter()

        async def _unavailable():
            return LiveState.unavailable()

        svc = serve.WorkerService(_config(tmp_path), adapter)
        adapter.live_state = _unavailable
        return await svc.probe()

    state = asyncio.run(go())
    assert state.state == "degraded" and state.kv_frac == -1.0


def test_a_node_with_no_scheduler_beats_to_nobody(tmp_path) -> None:
    async def go():
        svc = serve.WorkerService(_config(tmp_path), _FakeAdapter())
        stop = asyncio.Event()
        await svc.heartbeat_forever(stop=stop)  # returns immediately
        assert (await svc.probe()).slots_total == 4

    asyncio.run(go())


def test_a_scheduler_that_is_not_there_is_retried_and_then_given_up_on(tmp_path) -> None:
    """The beat cadence keeps counting while the transport is down, so the reconnect shows
    up as the hole in `seq` that gap detection is looking for."""

    async def go():
        svc = serve.WorkerService(
            _config(tmp_path, scheduler_endpoint="127.0.0.1:1"), _FakeAdapter()
        )
        stop = asyncio.Event()
        retrying = asyncio.create_task(svc.heartbeat_forever(stop=stop, reconnect_s=0.02))
        await asyncio.sleep(0.15)
        stop.set()
        await retrying
        await svc.heartbeat_forever(stop=asyncio.Event(), reconnect_s=None)
        await svc.drain()

    asyncio.run(go())


def test_run_worker_waits_for_the_model_instead_of_sleeping_a_constant(
    tmp_path, monkeypatch
) -> None:
    """Loading the 8B GGUF is minutes cold and seconds warm, so any fixed wait is either
    wasted time or a race with a node that is not ready."""
    adapter = _FakeAdapter(healthy=False)
    polls = []

    async def _health():
        polls.append(1)
        return len(polls) > 1  # not ready on the first poll, ready on the second

    monkeypatch.setattr(serve, "LlamaCppAdapter", lambda *a, **k: adapter)
    monkeypatch.setattr(adapter, "health", _health)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(
            serve.run_worker(_config(tmp_path), bind="127.0.0.1:0", stop=stop, engine_poll_s=0.01)
        )
        await asyncio.sleep(0.2)
        stop.set()
        return await task

    assert asyncio.run(go()) == 0
    assert len(polls) >= 2


def test_run_worker_refuses_to_announce_a_node_whose_engine_never_loaded(tmp_path) -> None:
    """A worker that announced itself before the model was loaded would take dispatches it
    cannot serve."""
    config = _config(tmp_path)
    assert asyncio.run(serve.run_worker(config, bind="127.0.0.1:0", wait_for_engine_s=0.0)) == 1


def test_run_worker_serves_execute_over_a_real_socket_and_drains_on_the_way_out(
    tmp_path, monkeypatch
) -> None:
    adapter = _FakeAdapter(service_s=0.05)
    monkeypatch.setattr(serve, "LlamaCppAdapter", lambda *a, **k: adapter)

    async def go():
        sink = _Sink()
        client_server, client_ep = await _start(sink, sched_grpc.add_ClientServicer_to_server)
        stop = asyncio.Event()
        config = _config(tmp_path)
        task = asyncio.create_task(serve.run_worker(config, bind="127.0.0.1:50077", stop=stop))
        await asyncio.sleep(0.2)
        async with grpc.aio.insecure_channel("127.0.0.1:50077") as ch:
            await sched_grpc.WorkerStub(ch).Execute(_request(client_endpoint=client_ep))
            await asyncio.sleep(0.3)
        stop.set()
        rc = await task
        await client_server.stop(None)
        return rc, sink

    rc, sink = asyncio.run(go())
    assert rc == 0
    assert [d.req_id for d in sink.delivered] == ["r0001"]
    assert adapter.closed
    assert (tmp_path / "worker_n1_run_a.jsonl").exists()


def test_cli_passes_the_experimental_condition_through_to_the_node(tmp_path, monkeypatch) -> None:
    """`--slots` is `--parallel` is `SimNode.batch_capacity`. If the CLI dropped it the
    manifest would claim a condition the wrapper did not run."""
    seen = {}

    async def _fake_run(config, *, bind, **kw):
        seen["config"] = config
        seen["bind"] = bind
        return 0

    monkeypatch.setattr(serve, "run_worker", _fake_run)
    rc = serve.main(
        [
            "--node-id",
            "gtx1650ti",
            "--engine",
            "http://127.0.0.1:18080",
            "--bind",
            "0.0.0.0:50061",
            "--scheduler",
            "127.0.0.1:50051",
            "--slots",
            "4",
            "--engine-version",
            "b10569+cuda13.2",
            "--log-dir",
            str(tmp_path),
            "--heartbeat-s",
            "0.5",
        ]
    )
    assert rc == 0
    assert seen["config"].slots == 4
    assert seen["config"].node_id == "gtx1650ti"
    assert seen["config"].scheduler_endpoint == "127.0.0.1:50051"
    assert seen["config"].heartbeat_interval_s == 0.5
    assert seen["bind"] == "0.0.0.0:50061"


def test_a_stop_driven_cancel_is_a_clean_exit_and_an_external_one_is_not(tmp_path) -> None:
    """`outgoing()` returns when `stop` is set, which half-closes the stream and makes
    grpc.aio cancel the call — so a normal shutdown surfaces as CancelledError inside the
    loop. Swallowing every cancel would hide a real one; `stop` is what tells them apart."""

    async def go():
        svc = serve.WorkerService(
            _config(tmp_path, scheduler_endpoint="127.0.0.1:1"), _FakeAdapter()
        )

        async def _cancelled(outbox, stop):
            # Exactly the live sequence: the stream is up, `stop` fires, `outgoing()`
            # returns, grpc cancels the call underneath us.
            stop.set()
            raise asyncio.CancelledError

        async def _cancelled_from_outside(outbox, stop):
            raise asyncio.CancelledError

        svc._stream = _cancelled

        # Shutting down on purpose: the loop exits without raising.
        await svc.heartbeat_forever(stop=asyncio.Event())

        svc._stream = _cancelled_from_outside

        # Cancelled from outside with no stop pending: it still means what it means.
        with pytest.raises(asyncio.CancelledError):
            await svc.heartbeat_forever(stop=asyncio.Event())
        await svc.drain()

    asyncio.run(go())
