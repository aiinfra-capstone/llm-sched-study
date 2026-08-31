"""F-17 — what the replay client does when the run goes wrong.

`test_replay_open_loop.py` covers the happy path and the open-loop guarantee. This file
covers the rest of the state space, because the client's real job is not to succeed: it is
to produce exactly one honest record per request and then say, from measurements, whether
the run is analysable at all.

Every failure here has to end as a record, never as an exception that loses a request.
A missing record is indistinguishable downstream from a request that was never sent, and
that difference is the whole of `validity.dropped_requests`.

The scheduler is inline rather than `fixtures/fake_scheduler`, for the reason given in
`test_replay_open_loop.py`: the fixture is deleted after Week 3 and this suite must
outlive it.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import grpc
import pytest
from conftest import assert_conforms

from dataplane.harness import gen_trace, replay
from dataplane.proto import sched_grpc, sched_pb2

CONFIG = {
    "gen_seed": 31,
    "n_requests": 6,
    "duration_s": 30,
    "arrival": {"process": "poisson", "lambda_base": 40.0},
    "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
    "priority_mix": {"0": 1.0},
    # 400 ms keeps the timeout tests fast. On a real run this is the F-13 ceiling.
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 400},
    "vocab_size": 1000,
}


class _Scheduler(sched_grpc.SchedulerServicer):
    """A scriptable stand-in. Each knob corresponds to one real failure of a real pool."""

    def __init__(
        self,
        *,
        service_s: float = 0.0,
        deliver: bool = True,
        status: str = "ok",
        deliver_before_ack: bool = False,
        deliver_twice: bool = False,
        deliver_unknown_req: bool = False,
    ) -> None:
        self.service_s = service_s
        self.deliver = deliver
        self.status = status
        self.deliver_before_ack = deliver_before_ack
        self.deliver_twice = deliver_twice
        self.deliver_unknown_req = deliver_unknown_req
        self.seen: list[str] = []
        self.endpoints: set[str] = set()

    async def Dispatch(self, request, context):
        self.seen.append(request.req_id)
        self.endpoints.add(request.client_endpoint)
        if self.deliver and self.deliver_before_ack:
            await self._deliver(request)
        elif self.deliver:
            asyncio.create_task(self._deliver(request))
        return sched_pb2.DispatchAck(req_id=request.req_id, chosen_node="n1", accepted=True)

    async def _send(self, request, req_id: str) -> None:
        async with grpc.aio.insecure_channel(request.client_endpoint) as ch:
            await sched_grpc.ClientStub(ch).Deliver(
                sched_pb2.ResponseDelivery(
                    run_id=request.run_id,
                    req_id=req_id,
                    node_id="n1",
                    output_tokens=request.output_len,
                    status=self.status,
                    worker_service_ns=int(self.service_s * 1e9),
                    worker_queue_wait_ns=0,
                )
            )

    async def _deliver(self, request) -> None:
        if self.service_s:
            await asyncio.sleep(self.service_s)
        if self.deliver_unknown_req:
            await self._send(request, "r999999")
        await self._send(request, request.req_id)
        if self.deliver_twice:
            await self._send(request, request.req_id)


async def _run(trace: Path, sha: str, servicer: _Scheduler | None, **kw):
    """Replay against `servicer`, or against a dead endpoint when it is None."""
    if servicer is None:
        with socket.socket() as s:  # a port nothing is listening on
            s.bind(("127.0.0.1", 0))
            dead = f"127.0.0.1:{s.getsockname()[1]}"
        return await replay.replay(
            trace_path=trace,
            scheduler_endpoint=dead,
            run_id="run_test",
            expect_sha256=sha,
            bind="127.0.0.1:0",
            advertise_host="127.0.0.1",
            **kw,
        )

    server = grpc.aio.server()
    sched_grpc.add_SchedulerServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        return await replay.replay(
            trace_path=trace,
            scheduler_endpoint=f"127.0.0.1:{port}",
            run_id="run_test",
            expect_sha256=sha,
            bind="127.0.0.1:0",
            advertise_host="127.0.0.1",
            **kw,
        )
    finally:
        await server.stop(grace=1.0)


@pytest.fixture
def trace(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "t.jsonl"
    return path, gen_trace.generate(CONFIG, path)


# --------------------------------------------------------------------------------------
# One record per request, whatever happens
# --------------------------------------------------------------------------------------


def test_every_request_produces_exactly_one_record(trace) -> None:
    """A lost record and a lost request look identical downstream. They must not be."""
    path, sha = trace
    _, body = gen_trace.load(path)
    result = asyncio.run(_run(path, sha, _Scheduler()))
    assert [r["req_id"] for r in result.records] == [r["req_id"] for r in body]


def test_records_stay_in_trace_order(trace) -> None:
    """Requests complete out of order on a heterogeneous pool. The log is written in trace
    order anyway, so a diff between two runs of the same trace is readable."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(service_s=0.02)))
    assert result.records == sorted(result.records, key=lambda r: r["intended_offset_s"])


def test_a_pool_that_never_answers_is_a_timeout_not_a_hang(trace, schema) -> None:
    """The categorical failure mode of a 100x-heterogeneous pool: the slow node exceeds
    any reasonable ceiling. That is a `timeout` status and an invalid run, not an
    exception and not a silently short log."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(deliver=False)))

    assert all(r["status"] == "timeout" for r in result.records)
    assert result.validity.dropped_requests == len(result.records)
    assert not result.validity.valid
    assert_conforms(schema("log_client"), result.records, "client")


def test_an_unreachable_scheduler_is_an_engine_error(trace, schema) -> None:
    """Nothing was dispatched, so nothing can be attributed to a policy. The run still
    produces a full, conformant log saying so."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, None))

    assert all(r["status"] == "engine_error" for r in result.records)
    assert all(r["chosen_node_from_ack"] == "" for r in result.records)
    assert not result.validity.valid
    assert_conforms(schema("log_client"), result.records, "client")


def test_a_worker_reported_failure_is_carried_verbatim(trace) -> None:
    """`oom` is a real outcome on a 4 GB card at the top of the admissible envelope. It is
    the worker's word, recorded, not reinterpreted by the client."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(status="oom")))

    assert all(r["status"] == "oom" for r in result.records)
    assert result.validity.dropped_requests == len(result.records)


def test_a_timed_out_request_still_reports_a_duration(trace) -> None:
    """`e2e_duration_ns` is measured from the send stamp either way, so a timeout is a
    point on the latency distribution rather than a hole in it."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(deliver=False)))
    for r in result.records:
        assert r["e2e_duration_ns"] > 0


# --------------------------------------------------------------------------------------
# The delivery path (F-11): worker answers the client directly
# --------------------------------------------------------------------------------------


def test_a_delivery_that_beats_the_ack_is_not_lost(trace) -> None:
    """On a fast pool the response can arrive before the DispatchAck comes back. The sink
    registers its future BEFORE the Dispatch RPC goes out for exactly this reason; a sink
    that registered afterwards would report a phantom timeout on the fastest requests —
    which would bias the tail of the very distribution the study is measuring."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(deliver_before_ack=True)))

    assert all(r["status"] == "ok" for r in result.records)
    assert result.validity.valid


def test_a_duplicate_delivery_is_ignored(trace) -> None:
    """A worker retry must not produce a second record or crash the sink."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(deliver_twice=True)))

    assert len(result.records) == len({r["req_id"] for r in result.records})
    assert all(r["status"] == "ok" for r in result.records)


def test_a_delivery_for_an_unknown_request_is_ignored(trace) -> None:
    """A response from a previous run reaching this client's port must not be attributed
    to this run. `run_id` is on the wire; an unmatched `req_id` is simply dropped."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(deliver_unknown_req=True)))

    assert "r999999" not in {r["req_id"] for r in result.records}
    assert all(r["status"] == "ok" for r in result.records)


def test_the_client_advertises_a_reachable_endpoint(trace) -> None:
    """The worker dials this string from another host. It has to be the LAN address."""
    path, sha = trace
    servicer = _Scheduler()
    asyncio.run(_run(path, sha, servicer))
    assert all(e.startswith("127.0.0.1:") for e in servicer.endpoints)


@pytest.mark.parametrize(
    "bind",
    [
        pytest.param("0.0.0.0:0", id="explicit-wildcard"),
        # `--bind :50051` is the ordinary way to say "every interface", and rsplit leaves
        # an empty host rather than "0.0.0.0". Same unroutable endpoint, different string:
        # a guard that only knows the spelled-out form lets this one through.
        pytest.param(":0", id="bare-colon"),
    ],
)
def test_advertising_a_wildcard_bind_is_refused(trace, bind: str) -> None:
    """No form of "every interface" is an address a worker on another host can dial.

    Failing at startup beats a whole run of phantom timeouts.
    """
    path, sha = trace
    with pytest.raises(ValueError, match="cannot advertise"):
        asyncio.run(
            replay.replay(
                trace_path=path,
                scheduler_endpoint="127.0.0.1:1",
                run_id="run_test",
                expect_sha256=sha,
                bind=bind,
            )
        )


# --------------------------------------------------------------------------------------
# Clock discipline and the measurement window
# --------------------------------------------------------------------------------------


def test_the_client_record_contains_no_worker_stamp(trace) -> None:
    """Watch-list failure mode 3. Every duration in this record is send-to-receive on this
    host's monotonic clock. The worker's own durations arrive on the wire and go into the
    WORKER's log, where they can be joined but never subtracted against these."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(service_s=0.01)))
    for r in result.records:
        assert not [k for k in r if k.startswith("worker_")]


def test_e2e_covers_at_least_the_service_time(trace) -> None:
    """A sanity floor on the one duration the study is built from."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(service_s=0.05)))
    for r in result.records:
        assert r["e2e_duration_ns"] >= 0.04 * 1e9


def test_dispatch_ack_is_timed_separately_from_the_response(trace) -> None:
    """`dispatch_ack_ns` isolates the scheduler's contribution to the request path from
    the worker's. Both are measured here, on one clock, so the split is honest."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(service_s=0.05)))
    for r in result.records:
        assert 0 < r["dispatch_ack_ns"] < r["e2e_duration_ns"]


def test_both_node_attributions_are_recorded(trace) -> None:
    """The ack says where the scheduler sent it; the delivery says who answered. Keeping
    both is what makes a mismatch detectable after the fact instead of invisible."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler()))
    for r in result.records:
        assert r["chosen_node_from_ack"] == "n1"
        assert r["responding_node"] == "n1"


def test_warmup_requests_are_logged_but_not_judged(trace) -> None:
    """A cold engine's send-lag is not evidence about the load generator, and warmup
    requests are not analysed either. They still have to appear in the log — the join
    marks them `is_warmup` from `intended_offset_s`, in both vehicles, so both must have
    the rows to mark."""
    path, sha = trace
    _, body = gen_trace.load(path)
    cutoff = body[len(body) // 2]["arrival_offset_s"]

    result = asyncio.run(_run(path, sha, _Scheduler(deliver=False), warmup_s=cutoff))

    assert len(result.records) == len(body)
    outside_warmup = sum(1 for r in body if r["arrival_offset_s"] >= cutoff)
    assert result.validity.dropped_requests == outside_warmup


def test_a_warmup_that_covers_the_whole_trace_leaves_nothing_to_judge(trace) -> None:
    """A degenerate but reachable config. It must not divide by zero or claim validity
    from an empty window by accident — with nothing measured, nothing is violated."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(deliver=False), warmup_s=10_000))
    assert result.validity.max_send_lag_ms == 0.0
    assert result.validity.valid


def test_a_tightened_threshold_can_fail_an_otherwise_clean_run(trace) -> None:
    """The threshold is a parameter, not a constant, so I can re-judge a recorded run
    against a stricter bar without re-running it."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(), send_lag_threshold_ms=-1.0))
    assert result.validity.send_lag_violations == len(result.records)
    assert "open-loop" in " ".join(result.validity.reasons())


def test_the_header_is_returned_for_the_manifest(trace) -> None:
    """The launcher builds the C-6 config block from what was actually replayed, not from
    the config file it thinks it passed."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler()))
    assert result.header["gen_seed"] == CONFIG["gen_seed"]
    assert result.header["arrival"] == CONFIG["arrival"]
