"""The one claim in my half that no other test touches: does Python hold at rate?

`replay.py`'s own language note says asyncio is adequate below roughly 50 req/s, and that
above that GIL contention shows up as send-lag violations — and that the decision to move
this component to Go should be made *from measured send-lag, not in advance*.

Nothing measured it. This does.

It is marked `perf` and deselected by default, because a shared CI runner cannot assert a
threshold on this without either flaking or being set so loose it proves nothing. Send-lag
is a property of the machine. Run it where the number means something:

    uv run pytest -m perf -s

`-s` matters: the table it prints is the deliverable, not the assertion. The assertion
only says the load host can do what the harness claims at the rate the study will use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest

from dataplane.harness import gen_trace, replay
from dataplane.harness.manifest import SEND_LAG_THRESHOLD_MS
from dataplane.proto import sched_grpc, sched_pb2

pytestmark = pytest.mark.perf

# The rates worth knowing about: the study's own lambda, twice it, the stated boundary,
# and one past it so the table shows where the client actually breaks rather than only
# that it survived.
RATES = [10.0, 25.0, 50.0, 100.0]
WINDOW_S = 6.0


class _Sink(sched_grpc.SchedulerServicer):
    """Zero service time. The pool is not under test here — the load generator is."""

    async def Dispatch(self, request, context):
        asyncio.create_task(self._deliver(request))
        return sched_pb2.DispatchAck(req_id=request.req_id, chosen_node="n1", accepted=True)

    async def _deliver(self, request) -> None:
        async with grpc.aio.insecure_channel(request.client_endpoint) as ch:
            await sched_grpc.ClientStub(ch).Deliver(
                sched_pb2.ResponseDelivery(
                    run_id=request.run_id,
                    req_id=request.req_id,
                    node_id="n1",
                    output_tokens=request.output_len,
                    status="ok",
                )
            )


def _trace(rate: float, tmp_path: Path) -> tuple[Path, str]:
    config = {
        "gen_seed": 20260421,
        "n_requests": int(rate * WINDOW_S * 2),
        "duration_s": WINDOW_S,
        "arrival": {"process": "poisson", "lambda_base": rate},
        "length_dist": {"buckets": ["p512_o128"], "weights": [1.0]},
        "priority_mix": {"0": 1.0},
        "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 10000},
        "vocab_size": 128000,
    }
    path = tmp_path / f"t{rate:.0f}.jsonl"
    return path, gen_trace.generate(config, path)


async def _measure(path: Path, sha: str):
    server = grpc.aio.server()
    sched_grpc.add_SchedulerServicer_to_server(_Sink(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        return await replay.replay(
            trace_path=path,
            scheduler_endpoint=f"127.0.0.1:{port}",
            run_id="run_perf",
            expect_sha256=sha,
            bind="127.0.0.1:0",
            advertise_host="127.0.0.1",
        )
    finally:
        await server.stop(grace=1.0)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def test_send_lag_against_arrival_rate(tmp_path: Path) -> None:
    """Prints the table, then asserts only what the harness actually claims.

    Prompts are p512 and materialized before t0, so this measures the timing loop and the
    gRPC path — not tokenisation, which is precisely the thing pre-materialisation was
    supposed to remove from the loop.
    """
    print(f"\n  {'rate/s':>7}  {'n':>5}  {'p50 ms':>7}  {'p99 ms':>7}  {'max ms':>7}  breaches")
    print(f"  {'-' * 7}  {'-' * 5}  {'-' * 7}  {'-' * 7}  {'-' * 7}  --------")

    results = {}
    for rate in RATES:
        path, sha = _trace(rate, tmp_path)
        result = asyncio.run(_measure(path, sha))
        lags = [r["send_lag_ms"] for r in result.records]
        results[rate] = result
        print(
            f"  {rate:7.0f}  {len(lags):5d}  {_percentile(lags, 0.50):7.2f}  "
            f"{_percentile(lags, 0.99):7.2f}  {max(lags):7.2f}  "
            f"{result.validity.send_lag_violations}"
        )

    at_50 = results[50.0]
    assert at_50.validity.send_lag_violations == 0, (
        f"the client breached the {SEND_LAG_THRESHOLD_MS:g} ms threshold at 50 req/s "
        f"(max {at_50.validity.max_send_lag_ms:.1f} ms) — this is the measurement the "
        "language note asks for, and it says Python does not hold on this host"
    )


def test_no_request_is_lost_under_load(tmp_path: Path) -> None:
    """Send-lag is the guard the manifest reports. Losing a record entirely would not even
    register as a violation — the run would just be short, and short in a way that
    correlates with load."""
    path, sha = _trace(100.0, tmp_path)
    _, body = gen_trace.load(path)
    result = asyncio.run(_measure(path, sha))
    assert len(result.records) == len(body)


def test_pre_materialisation_keeps_long_prompts_out_of_the_loop(tmp_path: Path) -> None:
    """The same rate at the top of the admissible envelope. If send-lag rises with prompt
    length, something is allocating inside the timing loop after all."""
    short, sha_short = _trace(50.0, tmp_path)
    result_short = asyncio.run(_measure(short, sha_short))

    config = {
        "gen_seed": 20260421,
        "n_requests": int(50.0 * WINDOW_S * 2),
        "duration_s": WINDOW_S,
        "arrival": {"process": "poisson", "lambda_base": 50.0},
        "length_dist": {"buckets": ["p2048_o256"], "weights": [1.0]},
        "priority_mix": {"0": 1.0},
        "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 10000},
        "vocab_size": 128000,
    }
    long_path = tmp_path / "long.jsonl"
    sha_long = gen_trace.generate(config, long_path)
    result_long = asyncio.run(_measure(long_path, sha_long))

    print(
        f"\n  p512 max {result_short.validity.max_send_lag_ms:.2f} ms  ->  "
        f"p2048 max {result_long.validity.max_send_lag_ms:.2f} ms"
    )
    assert result_long.validity.send_lag_violations == 0
