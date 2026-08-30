"""The branches that only run when something is already wrong.

These are the lines a coverage report finds and a feature test never does: a fallback when
git is missing, a protoc invocation that fails, a client that has fallen behind its own
schedule. Each one exists because I decided in advance how the harness should degrade, and
a decision that is never exercised is a decision I only think I made.

The whole file is white-box on purpose. It reaches for internal functions and patches
module globals, because that is the only way to reach a failure that the outside world
cannot be made to produce on demand.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from dataplane import proto
from dataplane.harness import gen_trace
from dataplane.harness.manifest import Validity, git_shas

# --------------------------------------------------------------------------------------
# Provenance degrades, it does not fail
# --------------------------------------------------------------------------------------


def test_a_trace_still_generates_where_git_is_absent(trace_config, tmp_path, monkeypatch) -> None:
    """F-20 wants the generator's commit in the header. An unversioned checkout — a
    tarball on a lab machine — should still be able to make a trace; it just cannot claim
    provenance for it. Failing here would make the harness need git to do arithmetic."""

    def no_git(*_args, **_kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", no_git)

    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(), path)
    assert json.loads(path.read_text().splitlines()[0])["generator_git_sha"] == "unknown"


def test_a_git_call_that_fails_is_not_an_exception(trace_config, tmp_path, monkeypatch) -> None:
    """The other way it goes wrong: git exists, and the directory is not a repository."""

    def failing_git(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(subprocess, "run", failing_git)

    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(), path)
    assert json.loads(path.read_text().splitlines()[0])["generator_git_sha"] == "unknown"


def test_a_git_call_that_hangs_is_bounded(monkeypatch) -> None:
    """The subprocess carries a 5 s timeout. A hung git — an unreachable network remote in
    a filter — must not stall trace generation indefinitely."""

    def timing_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(subprocess, "run", timing_out)
    assert git_shas()["harness"] == "unknown"


def test_an_empty_git_answer_is_treated_as_unknown(monkeypatch) -> None:
    """`rev-parse` succeeding with no output would otherwise put an empty string into the
    manifest, which passes the schema and says nothing."""

    class Empty:
        stdout = "   \n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Empty())
    assert git_shas()["worker"] == "unknown"


# --------------------------------------------------------------------------------------
# The stub generator
# --------------------------------------------------------------------------------------


def test_a_failing_protoc_raises_rather_than_leaving_half_a_package(monkeypatch) -> None:
    """The stubs are regenerated on import. A silent failure would leave the previous
    generation in place and the process would run happily against a stale contract — which
    is exactly the drift not committing them was supposed to prevent."""
    from grpc_tools import protoc as protoc_mod

    monkeypatch.setattr(protoc_mod, "main", lambda *_a, **_k: 1)
    with pytest.raises(RuntimeError, match="protoc failed"):
        proto.generate(force=True)


def test_an_installed_wheel_says_why_it_cannot_generate(monkeypatch, tmp_path) -> None:
    """A wheel has no `contracts/` directory. The message has to say that, because the
    alternative is an ImportError from a package that looks broken rather than misused."""
    monkeypatch.setattr(proto, "__file__", str(tmp_path / "proto.py"))
    with pytest.raises(FileNotFoundError, match="source checkout"):
        proto._find_proto()


def test_stub_regeneration_is_driven_by_the_proto_mtime() -> None:
    """The freshness check is what makes import cheap. If it compared nothing, every
    process start would shell out to protoc — including the one running the timing loop."""
    proto.generate()
    pb2 = proto.OUT_DIR / "scheduling_pb2.py"
    assert pb2.stat().st_mtime >= proto.PROTO.stat().st_mtime


# --------------------------------------------------------------------------------------
# Every validity reason, in words
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("validity", "phrase"),
    [
        (Validity(send_lag_violations=1, max_send_lag_ms=99.0), "open-loop"),
        (Validity(dropped_requests=4), "never returned a response"),
        (Validity(colocated_nodes=2), "colocated"),
        (Validity(engine_restarts=1), "engine restart"),
    ],
)
def test_each_rejection_explains_itself(validity: Validity, phrase: str) -> None:
    """These strings are the console output at the end of a failed run, and they are the
    only thing between me and re-running a broken configuration overnight."""
    assert phrase in " ".join(validity.reasons())
    assert not validity.valid


def test_several_failures_are_all_reported_not_just_the_first() -> None:
    """A run that drifted AND lost requests has two problems, and fixing one of them is
    not enough."""
    reasons = Validity(send_lag_violations=1, dropped_requests=2, engine_restarts=1).reasons()
    assert len(reasons) == 3


# --------------------------------------------------------------------------------------
# The timing loop when it is already late
# --------------------------------------------------------------------------------------


def test_a_client_that_falls_behind_reports_it_rather_than_absorbing_it(tmp_path) -> None:
    """The branch that runs when the next request was due in the past.

    The loop must not sleep a negative duration and must not quietly reschedule: it fires
    immediately and records the lag it accrued. That honesty is the entire basis for
    `validity.send_lag_violations` — a client that silently caught up would convert a
    failed load generator into a clean-looking run at a lower arrival rate.
    """
    import asyncio

    import grpc

    from dataplane.harness import replay
    from dataplane.proto import sched_grpc, sched_pb2

    # 400 requests inside one second: the loop cannot keep up, by construction.
    config = {
        "gen_seed": 5,
        "n_requests": 400,
        "duration_s": 1.0,
        "arrival": {"process": "poisson", "lambda_base": 2000.0},
        "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
        "priority_mix": {"0": 1.0},
        "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 5000},
        "vocab_size": 1000,
    }
    path = tmp_path / "t.jsonl"
    sha = gen_trace.generate(config, path)

    class _Fast(sched_grpc.SchedulerServicer):
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

    async def go():
        server = grpc.aio.server()
        sched_grpc.add_SchedulerServicer_to_server(_Fast(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            return await replay.replay(
                trace_path=path,
                scheduler_endpoint=f"127.0.0.1:{port}",
                run_id="run_late",
                expect_sha256=sha,
                bind="127.0.0.1:0",
                advertise_host="127.0.0.1",
            )
        finally:
            await server.stop(grace=1.0)

    result = asyncio.run(go())

    assert result.validity.max_send_lag_ms > 0
    assert all(r["send_lag_ms"] >= 0 for r in result.records)
    assert len(result.records) == 400


def test_a_trace_path_that_does_not_exist_fails_before_anything_is_bound(tmp_path) -> None:
    """The load is verified first. A run that dies after opening a listening socket leaves
    the port held by a process that is already exiting."""
    import asyncio

    from dataplane.harness import replay

    with pytest.raises(FileNotFoundError):
        asyncio.run(
            replay.replay(
                trace_path=tmp_path / "nope.jsonl",
                scheduler_endpoint="127.0.0.1:1",
                run_id="run_missing",
                bind="127.0.0.1:0",
                advertise_host="127.0.0.1",
            )
        )
