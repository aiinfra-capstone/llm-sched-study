"""My fake scheduler, exercised rather than merely linted.

The Week-1 gate says the replay client runs open-loop against `fixtures/fake_scheduler`.
CI lints that file but never runs it, which means the one fixture standing in for
Aditya's half until it lands is the least-tested Python in my half.

Two modes, and the distinction between them is the point:

  * `--worker` forwards `Execute` to real workers. This is the mode that matches the
    fixture contract, and it is what proves my worker's ingress before a scheduler exists.
  * `--loopback` delivers from an analytic service-time model with no worker at all. It is
    a crutch for exercising the client solo. Its timings mean nothing and nothing may be
    calibrated against it — so the test that matters most here is the one asserting the
    loopback node is labelled as such in every record it produces.

The whole directory is deleted after Week 3. This file goes with it; nothing in the
harness suite imports it.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import grpc
import pytest
from conftest import REPO_ROOT, assert_conforms

from dataplane.harness import gen_trace, replay
from dataplane.proto import sched_grpc, sched_pb2

FIXTURE = REPO_ROOT / "fixtures" / "fake_scheduler" / "serve.py"

CONFIG = {
    "gen_seed": 101,
    "n_requests": 8,
    "duration_s": 10,
    "arrival": {"process": "poisson", "lambda_base": 40.0},
    "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
    "priority_mix": {"0": 1.0},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 2000},
    "vocab_size": 1000,
}


@pytest.fixture(scope="module")
def serve():
    """Import the fixture by path — it is a script, not a package."""
    if not FIXTURE.exists():
        pytest.skip("fixtures/fake_scheduler removed (expected after Week 3)")
    spec = importlib.util.spec_from_file_location("fake_scheduler_serve", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Worker(sched_grpc.WorkerServicer):
    """A fake worker: accepts Execute, answers the client directly (F-11)."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self.executed: list[str] = []
        self.decision_seqs: list[int] = []

    async def Execute(self, request, context):
        self.executed.append(request.req_id)
        self.decision_seqs.append(request.decision_seq)
        asyncio.create_task(self._deliver(request))
        return sched_pb2.ExecuteAck(req_id=request.req_id, queued=True)

    async def _deliver(self, request) -> None:
        async with grpc.aio.insecure_channel(request.client_endpoint) as ch:
            await sched_grpc.ClientStub(ch).Deliver(
                sched_pb2.ResponseDelivery(
                    run_id=request.run_id,
                    req_id=request.req_id,
                    node_id=self.node_id,
                    output_tokens=request.output_len,
                    status="ok",
                    worker_service_ns=1_000_000,
                    worker_queue_wait_ns=0,
                )
            )


async def _serve(servicer, add) -> tuple[grpc.aio.Server, str]:
    server = grpc.aio.server()
    add(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, f"127.0.0.1:{port}"


@pytest.fixture
def trace(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "t.jsonl"
    return path, gen_trace.generate(CONFIG, path)


# The production threshold is 50 ms, and it is calibrated for the thing it guards: a
# dedicated host driving a LAN pool at around 1 req/s, where the timing loop is idle
# between sends. This file is a different animal. It runs CONFIG at 40 req/s with the
# client, the scheduler and up to two workers sharing one event loop in one process, which
# `replay`'s own docstring calls the edge of what asyncio holds. On a two-core CI runner
# with a noisy neighbour that has produced 59 to 64 ms of lag and failed the run as a bad
# load generator, when nothing about the load generator was wrong.
#
# Loosening it here costs no coverage, because this is not where open-loop discipline is
# guarded. `test_a_client_that_falls_behind_reports_it_rather_than_absorbing_it` drives 400
# requests into one second and asserts the client reports the lag instead of absorbing it,
# and `test_send_lag_breach_invalidates_the_run` pins the threshold logic on synthetic
# values. Neither depends on how fast the machine is. What this file asserts is that the
# fake scheduler distributes and delivers, and the rest of `validity` stays strict: a
# dropped request or a colocation breach still fails, at any speed.
FIXTURE_SEND_LAG_MS = 250.0


async def _replay_against(scheduler_servicer, trace_path: Path, sha: str):
    server, endpoint = await _serve(scheduler_servicer, sched_grpc.add_SchedulerServicer_to_server)
    try:
        return await replay.replay(
            trace_path=trace_path,
            scheduler_endpoint=endpoint,
            run_id="run_fixture",
            expect_sha256=sha,
            bind="127.0.0.1:0",
            advertise_host="127.0.0.1",
            send_lag_threshold_ms=FIXTURE_SEND_LAG_MS,
        )
    finally:
        await server.stop(grace=1.0)


def test_loopback_completes_a_replay(serve, trace, schema) -> None:
    """The Week-1 gate: a seeded trace replays end to end with no worker in existence."""
    path, sha = trace

    async def go():
        scheduler = serve.FakeScheduler([], loopback=True, prefill_ms=1.0, per_token_ms=0.01)
        return await _replay_against(scheduler, path, sha), scheduler

    result, scheduler = asyncio.run(go())

    assert result.validity.valid
    assert scheduler.dispatched == CONFIG["n_requests"]
    assert_conforms(schema("log_client"), result.records, "client")


def test_loopback_labels_itself_in_every_record(serve, trace) -> None:
    """Its timings are analytic and mean nothing. The node id says so in the log, so a
    loopback run can never be mistaken for a calibration run after the fact."""
    path, sha = trace

    async def go():
        return await _replay_against(
            serve.FakeScheduler([], loopback=True, prefill_ms=1.0, per_token_ms=0.01), path, sha
        )

    result = asyncio.run(go())
    assert {r["responding_node"] for r in result.records} == {"loopback"}
    assert {r["chosen_node_from_ack"] for r in result.records} == {"loopback"}


def test_forwarding_mode_round_robins_across_workers(serve, trace) -> None:
    """Blind round-robin is the whole policy. It is not a baseline for anything — the real
    RoundRobin is Aditya's — but an even split proves the rotation is wired to the
    dispatch path rather than to a fixed first entry."""
    path, sha = trace

    async def go():
        workers = [_Worker("w1"), _Worker("w2")]
        servers = []
        endpoints = []
        for w in workers:
            server, endpoint = await _serve(w, sched_grpc.add_WorkerServicer_to_server)
            servers.append(server)
            endpoints.append(endpoint)
        try:
            result = await _replay_against(
                serve.FakeScheduler(endpoints, loopback=False, prefill_ms=0.0, per_token_ms=0.0),
                path,
                sha,
            )
        finally:
            for server in servers:
                await server.stop(grace=1.0)
        return result, workers

    result, workers = asyncio.run(go())

    assert result.validity.valid
    assert all(r["status"] == "ok" for r in result.records)
    counts = sorted(len(w.executed) for w in workers)
    assert counts == [CONFIG["n_requests"] // 2, CONFIG["n_requests"] // 2]


def test_forwarding_mode_stamps_a_monotonic_decision_seq(serve, trace) -> None:
    """`decision_seq` is the join key between a dispatch and the record that explains it.
    Even a fixture with no decision to explain has to keep it unique and increasing, or my
    pipeline's join is tested against something the real scheduler will not produce."""
    path, sha = trace

    async def go():
        worker = _Worker("w1")
        server, endpoint = await _serve(worker, sched_grpc.add_WorkerServicer_to_server)
        try:
            await _replay_against(
                serve.FakeScheduler([endpoint], loopback=False, prefill_ms=0.0, per_token_ms=0.0),
                path,
                sha,
            )
        finally:
            await server.stop(grace=1.0)
        return worker

    worker = asyncio.run(go())
    seqs = sorted(worker.decision_seqs)
    assert seqs == list(range(1, CONFIG["n_requests"] + 1))


def test_an_unreachable_worker_becomes_an_explicit_rejection(serve, trace) -> None:
    """Not a hang and not a silent drop: the ack says `accepted=False`, the client counts
    the request as lost, and the run is marked invalid."""
    path, sha = trace

    async def go():
        return await _replay_against(
            serve.FakeScheduler(["127.0.0.1:1"], loopback=False, prefill_ms=0.0, per_token_ms=0.0),
            path,
            sha,
        )

    result = asyncio.run(go())
    assert result.validity.dropped_requests == CONFIG["n_requests"]
    assert not result.validity.valid


def test_the_fixture_writes_no_scheduler_log(serve) -> None:
    """Deliberate. A C-4 decision record needs a full `candidates` array with queue depth,
    capability and estimate age per candidate (F-3), and a fixture with no state store
    cannot fill those in honestly. Plausible-looking fabricated numbers in the join
    pipeline would be strictly worse than a gap."""
    source = FIXTURE.read_text()
    writes = [op for op in ("open(", ".write_text(", ".write(", "json.dump") if op in source]
    assert not writes, f"the fixture has grown a log writer: {writes}"
