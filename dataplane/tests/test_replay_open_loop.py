"""F-17 — the replay client must stay open-loop, and must say so when it didn't.

These run against an inline scheduler rather than `fixtures/fake_scheduler`, on purpose:
the fixture gets deleted after Week 3 (a fake worker that survives into the measurement
weeks is one somebody eventually calibrates against by accident), and a test suite that
dies with it is a test suite that stops guarding the harness exactly when the harness
starts producing real numbers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import grpc
import pytest

from dataplane.harness import gen_trace, replay
from dataplane.proto import sched_grpc, sched_pb2

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"

CONFIG = {
    "gen_seed": 7,
    "n_requests": 12,
    "duration_s": 30,
    "arrival": {"process": "poisson", "lambda_base": 20.0},
    "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
    "priority_mix": {"0": 1.0},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 5000},
    "vocab_size": 1000,
}


class _Scheduler(sched_grpc.SchedulerServicer):
    """Accepts, then delivers after `service_s`. Set `accept=False` to reject everything."""

    def __init__(self, service_s: float = 0.0, accept: bool = True) -> None:
        self.service_s = service_s
        self.accept = accept
        self.concurrent = 0
        self.peak_concurrent = 0

    async def Dispatch(self, request, context):
        if not self.accept:
            return sched_pb2.DispatchAck(
                req_id=request.req_id,
                chosen_node="",
                accepted=False,
                reject_reason="no_admissible_node",
            )
        asyncio.create_task(self._deliver(request))
        return sched_pb2.DispatchAck(req_id=request.req_id, chosen_node="n1", accepted=True)

    async def _deliver(self, request) -> None:
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.service_s)
            async with grpc.aio.insecure_channel(request.client_endpoint) as ch:
                await sched_grpc.ClientStub(ch).Deliver(
                    sched_pb2.ResponseDelivery(
                        run_id=request.run_id,
                        req_id=request.req_id,
                        node_id="n1",
                        output_tokens=request.output_len,
                        status="ok",
                        worker_service_ns=int(self.service_s * 1e9),
                        worker_queue_wait_ns=0,
                    )
                )
        finally:
            self.concurrent -= 1


async def _run(trace: Path, sha: str, servicer: _Scheduler, **kw):
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


def test_records_conform_to_c4(trace: tuple[Path, str]) -> None:
    """Every record the client writes must validate against log_client.schema.json.

    If A's client and B's scheduler both emit records that pass their schemas, the two
    halves join when they meet for real. That is the whole bet of the fixture-first plan.
    """
    jsonschema = pytest.importorskip("jsonschema")
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(service_s=0.01)))

    validator = jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / "schemas" / "log_client.schema.json").read_text())
    )
    for record in result.records:
        errors = list(validator.iter_errors(record))
        assert not errors, f"{record['req_id']}: {[e.message for e in errors]}"

    assert all(r["status"] == "ok" for r in result.records)
    assert result.validity.valid


def test_client_is_open_loop(trace: tuple[Path, str]) -> None:
    """Requests overlap in flight; the loop never waits for a response to fire the next.

    A closed-loop client silently converts a slow pool into a lower arrival rate — every
    queueing result it produces would then be a measurement of itself.
    """
    path, sha = trace
    servicer = _Scheduler(service_s=0.25)  # ~5x the mean interarrival at lambda=20
    result = asyncio.run(_run(path, sha, servicer))

    assert servicer.peak_concurrent > 1, "requests were serialized — the client is closed-loop"
    assert all(r["status"] == "ok" for r in result.records)


def test_send_lag_is_recorded_against_intended_offset(trace: tuple[Path, str]) -> None:
    """`send_lag_ms` is measured, per request, against the trace's own offset."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(service_s=0.0)))

    for r in result.records:
        assert r["send_lag_ms"] >= 0.0
        assert r["actual_send_offset_s"] >= r["intended_offset_s"]
    assert result.validity.max_send_lag_ms == max(r["send_lag_ms"] for r in result.records)


def test_rejected_dispatch_invalidates_the_run(trace: tuple[Path, str]) -> None:
    """A run that lost requests is marked invalid rather than analysed."""
    path, sha = trace
    result = asyncio.run(_run(path, sha, _Scheduler(accept=False)))

    assert result.validity.dropped_requests == len(result.records)
    assert not result.validity.valid
    assert result.validity.reasons()


def test_trace_hash_mismatch_refuses_to_start(trace: tuple[Path, str]) -> None:
    """The trace's SHA-256 is its identity. A mismatch is a stop, not a warning."""
    path, _ = trace
    with pytest.raises(ValueError, match="sha256 mismatch"):
        asyncio.run(_run(path, "0" * 64, _Scheduler()))
