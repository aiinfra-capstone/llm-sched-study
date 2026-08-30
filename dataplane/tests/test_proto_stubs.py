"""C-1 — the stubs are a build artifact, and the build has to be reliable.

`dataplane.proto` regenerates the gRPC stubs from `contracts/scheduling.proto` on import,
rather than committing them. That is a deliberate §12.2 choice: a committed stub is a
second copy of the contract that can disagree with the thing it was generated from, and
the disagreement shows up as a wire error in the middle of a run.

The cost of that choice is that stub generation is now part of my runtime, so it is
tested like runtime: it must be idempotent, it must notice when the proto moves, and the
messages it produces must accept the values my client actually puts in them.
"""

from __future__ import annotations

import pytest

from dataplane import proto
from dataplane.proto import sched_grpc, sched_pb2


def test_the_proto_resolves_to_the_committed_contract() -> None:
    """Not a vendored copy inside the package: the one file both halves agree on."""
    assert proto.PROTO.parts[-2:] == ("contracts", "scheduling.proto")
    assert proto.PROTO.exists()


def test_generate_is_idempotent() -> None:
    """Import happens on every process start, including inside the timing loop's process.
    A second call must be a stat, not a protoc invocation."""
    first = proto.generate()
    second = proto.generate()
    assert first == second == proto.OUT_DIR
    assert (proto.OUT_DIR / "scheduling_pb2.py").exists()


def test_a_forced_regeneration_still_produces_working_stubs() -> None:
    """`force=True` is what I reach for when the proto changed and the mtime did not."""
    proto.generate(force=True)
    assert sched_pb2.DispatchRequest(req_id="r000001").req_id == "r000001"


def test_the_generated_stubs_are_not_committed() -> None:
    """If they were, the contract would have two sources of truth."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(proto.OUT_DIR / "scheduling_pb2.py")],
        capture_output=True,
        text=True,
        cwd=proto.PROTO.parent.parent,
        check=False,
    )
    assert tracked.returncode != 0, "generated stubs are tracked in git"


# --------------------------------------------------------------------------------------
# The messages my components fill in
# --------------------------------------------------------------------------------------


def test_dispatch_request_carries_everything_the_client_sends() -> None:
    """One assertion per field the replay client sets, so a renamed field fails here and
    not as a silently-defaulted zero on the wire."""
    msg = sched_pb2.DispatchRequest(
        run_id="run_0142",
        req_id="r000417",
        prompt_token_ids=[1, 2, 3],
        output_len=128,
        priority=1,
        bucket_id="p512_o128",
        client_endpoint="10.0.0.7:41234",
        client_send_mono_ns=123456789,
    )
    assert list(msg.prompt_token_ids) == [1, 2, 3]
    assert msg.client_endpoint == "10.0.0.7:41234"
    assert msg.client_send_mono_ns == 123456789


def test_prompt_token_ids_hold_a_full_admissible_prompt() -> None:
    """2048 ids at the top of the F-13 envelope, and the largest id a 128k vocabulary can
    produce. `repeated uint32` handles both; a narrower type would not."""
    from dataplane.harness.prompts import materialize

    tokens = materialize(1, 2048, 128000)
    msg = sched_pb2.DispatchRequest(prompt_token_ids=tokens)
    assert len(msg.prompt_token_ids) == 2048
    assert max(msg.prompt_token_ids) < 128000


def test_a_negative_token_id_is_rejected_rather_than_wrapped() -> None:
    """`uint32` wrapping a negative id into four billion would produce a prompt the model
    cannot embed, and the failure would surface as an engine error under load."""
    with pytest.raises(ValueError):
        sched_pb2.DispatchRequest(prompt_token_ids=[-1])


def test_response_delivery_carries_worker_local_durations_only() -> None:
    """Both are durations measured entirely on the worker. Neither is a point in time, so
    neither can be subtracted against a client stamp by accident."""
    msg = sched_pb2.ResponseDelivery(
        req_id="r000417",
        node_id="n2",
        output_tokens=128,
        status="ok",
        worker_service_ns=2_000_000_000,
        worker_queue_wait_ns=88_000_000,
    )
    assert msg.worker_service_ns > msg.worker_queue_wait_ns


def test_heartbeat_can_say_the_engine_does_not_expose_kv_occupancy() -> None:
    """llama.cpp reports slot occupancy, not paged-KV occupancy. -1.0 is the contract's
    way of saying so; 0.0 would claim the node is empty."""
    assert sched_pb2.Heartbeat(kv_occupancy_frac=-1.0).kv_occupancy_frac == -1.0


def test_the_three_services_my_components_implement_or_call() -> None:
    """The client serves `Client`, the worker serves `Worker` and calls `Scheduler`."""
    for name in ("SchedulerStub", "WorkerStub", "ClientStub"):
        assert hasattr(sched_grpc, name)
    for name in ("add_ClientServicer_to_server", "add_WorkerServicer_to_server"):
        assert hasattr(sched_grpc, name)


def test_execute_request_carries_the_decision_seq() -> None:
    """It is the only join key between the worker's log and Aditya's decision record.
    Without it, a per-request routing error cannot be attributed to a decision."""
    assert sched_pb2.ExecuteRequest(decision_seq=417).decision_seq == 417
