"""The worker wrapper's two side-effects: the C-4 log and the F-10 heartbeat.

Both are things the scheduler and the pipeline believe without being able to check, so the
tests are about the ways they could quietly lie: a log that records only successes, a
heartbeat that repeats a stale occupancy, a tok/s average that counts an unmeasurable
request as zero.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from dataplane.worker.adapter import LiveState, ServiceResult
from dataplane.worker.heartbeat import HeartbeatEmitter, heartbeat_payload
from dataplane.worker.log import WorkerLog, build_record


def _result(status="ok", **over):
    return ServiceResult(
        status=status,
        service_ns=over.pop("service_ns", 2_000_000_000),
        prompt_tokens=over.pop("prompt_tokens", 512),
        output_tokens=over.pop("output_tokens", 128),
        prefill_ns=over.pop("prefill_ns", 210_000_000),
        decode_ns=over.pop("decode_ns", 1_790_000_000),
        **over,
    )


def test_a_failed_request_still_gets_a_record(tmp_path) -> None:
    """A worker that logs only its successes produces a file whose failure rate is zero by
    construction, and the join then drops those requests — so a policy that routes badly
    enough to cause timeouts would look *better* than one that does not."""
    with WorkerLog(tmp_path, node_id="n2", run_id="run_0142") as log:
        log.log(
            req_id="r1",
            result=_result(),
            queue_wait_ns=88_000_000,
            state_at_admission=LiveState(inflight=3, slots_total=4, kv_frac=0.75),
        )
        log.log(
            req_id="r2",
            result=ServiceResult(
                status="timeout", service_ns=60_000_000_000, prompt_tokens=2048, output_tokens=0
            ),
            queue_wait_ns=0,
            state_at_admission=LiveState.unavailable(),
        )
        assert log.n_written == 2

    lines = [
        json.loads(x) for x in (tmp_path / "worker_n2_run_0142.jsonl").read_text().splitlines()
    ]
    assert [r["status"] for r in lines] == ["ok", "timeout"]
    assert "prefill_ns" in lines[0] and "prefill_ns" not in lines[1]
    assert lines[1]["kv_occupancy_at_admission"] == -1.0


def test_build_record_takes_a_result_or_the_fields_flat() -> None:
    """The pool worker holds a `ServiceResult`; the F-9b vLLM probe does not and is not a
    llama.cpp client at all. Both emit C-4, so both shapes are accepted."""
    from_obj = build_record(
        run_id="r",
        req_id="q",
        node_id="n1",
        queue_wait_ns=5,
        result=_result(),
        state_at_admission=LiveState(inflight=2, slots_total=4, kv_frac=0.5),
    )
    flat = build_record(
        run_id="r",
        req_id="q",
        node_id="n1",
        queue_wait_ns=5,
        service_ns=2_000_000_000,
        prompt_tokens=512,
        output_tokens=128,
        prefill_ns=210_000_000,
        decode_ns=1_790_000_000,
        # The object path derives batch from inflight as inflight + 1, so the flat path
        # has to state 3 against the same inflight of 2 for the two shapes to agree.
        batch_size_at_admission=3,
        inflight_at_admission=2,
        status="ok",
        kv_occupancy_at_admission=0.5,
        engine="vllm",
    )
    assert from_obj["engine"] == "llamacpp" and flat["engine"] == "vllm"
    assert {k: v for k, v in from_obj.items() if k != "engine"} == {
        k: v for k, v in flat.items() if k != "engine"
    }


def test_an_explicit_field_beats_the_object_it_was_defaulted_from() -> None:
    record = build_record(
        run_id="r",
        req_id="q",
        node_id="n1",
        queue_wait_ns=0,
        result=_result(status="ok"),
        status="oom",
        output_tokens=0,
    )
    assert record["status"] == "oom" and record["output_tokens"] == 0


def test_a_record_with_neither_result_nor_fields_is_all_defaults() -> None:
    record = build_record(run_id="r", req_id="q", node_id="n1", queue_wait_ns=0)
    assert record["service_ns"] == 0 and record["status"] == "ok"
    assert record["kv_occupancy_at_admission"] == -1.0
    assert "prefill_ns" not in record


def test_closing_twice_is_harmless(tmp_path) -> None:
    log = WorkerLog(tmp_path, node_id="n1", run_id="r1")
    log.close()
    log.close()
    assert log.path.exists()


def test_heartbeat_sequence_is_gapless_and_carries_the_five_values() -> None:
    """`validity.heartbeat_gaps` counts breaks in this sequence, so it is never reused."""
    emitter = HeartbeatEmitter(run_id="run_0142", node_id="n1")
    beats = [emitter.next() for _ in range(3)]

    assert [b.seq for b in beats] == [1, 2, 3]
    d = beats[0].to_dict()
    assert set(d) >= {
        "queue_depth",
        "inflight_count",
        "recent_tokens_per_s",
        "kv_occupancy_frac",
        "engine_state",
    }
    assert d["engine_state"] == "ready"
    assert emitter.emitted == beats


def test_an_unmeasurable_rate_is_ignored_not_counted_as_zero() -> None:
    """A `partial` backend reports no decode time. A node that cannot state its rate is not
    a node running at zero tok/s, and telling the scheduler otherwise makes it look like
    the slowest machine in the pool."""
    emitter = HeartbeatEmitter(run_id="r", node_id="n1")
    emitter.observe_completion(None)
    assert emitter.next().recent_tokens_per_s == 0.0

    emitter.observe_completion(100.0)
    assert emitter.next().recent_tokens_per_s == pytest.approx(100.0)  # first sample seeds it
    emitter.observe_completion(50.0)
    # EWMA, not a replacement: one slow request must not make a healthy node look broken.
    assert 50.0 < emitter.next().recent_tokens_per_s < 100.0


def test_queue_depth_is_the_wrappers_to_report() -> None:
    """llama.cpp admits exactly `--parallel`; anything beyond that queues in the wrapper,
    and `/slots` cannot see it."""
    emitter = HeartbeatEmitter(run_id="r", node_id="n1")
    emitter.set_queue_depth(7)
    beat = emitter.next(LiveState(inflight=4, slots_total=4, kv_frac=1.0))
    assert (beat.queue_depth, beat.inflight_count, beat.kv_occupancy_frac) == (7, 4, 1.0)


def test_a_payload_carries_the_state_it_was_built_from() -> None:
    """`worker_mono_ns` is stamped here and rides along for gap detection only. Nothing
    downstream may subtract it from a stamp taken on another host."""
    hb = heartbeat_payload(
        run_id="run_0142",
        node_id="n3",
        seq=9,
        state=LiveState(inflight=2, slots_total=4, kv_frac=0.5, queue_depth=1, state="warming"),
        recent_tokens_per_s=61.25,
    )
    assert (hb.seq, hb.node_id, hb.engine_state) == (9, "n3", "warming")
    assert (hb.queue_depth, hb.inflight_count, hb.kv_occupancy_frac) == (1, 2, 0.5)
    assert hb.recent_tokens_per_s == pytest.approx(61.25)
    assert hb.worker_mono_ns > 0


def test_the_emitter_keeps_its_cadence_and_stops_when_told() -> None:
    """Driven off an advancing deadline, so a slow `/slots` read does not make the stream
    drift late — which would look like estimate age growing for no reason in Aditya's logs,
    and would be indistinguishable from the staleness he injects on purpose."""

    async def go() -> list[int]:
        emitter = HeartbeatEmitter(run_id="r", node_id="n1", interval_s=0.01)
        stop = asyncio.Event()
        seen: list[int] = []

        async def probe() -> LiveState:
            await asyncio.sleep(0)
            return LiveState(inflight=1, slots_total=4, kv_frac=0.25)

        def sink(hb) -> None:
            seen.append(hb.seq)
            if len(seen) == 4:
                stop.set()

        await asyncio.wait_for(emitter.run(probe, sink, stop=stop), timeout=5)
        return seen

    assert asyncio.run(go()) == [1, 2, 3, 4]


def test_a_probe_slower_than_the_interval_does_not_stall_the_stream() -> None:
    """The deadline advances by a fixed step, so a slow `/slots` read makes the next beat
    immediate rather than pushing the whole stream late — late beats would show up in
    Aditya's logs as estimate age growing for no reason."""

    async def go() -> list[int]:
        emitter = HeartbeatEmitter(run_id="r", node_id="n1", interval_s=0.001)
        stop = asyncio.Event()
        seen: list[int] = []

        async def slow_probe() -> LiveState:
            await asyncio.sleep(0.005)  # five intervals
            return LiveState(inflight=0, slots_total=4, kv_frac=0.0)

        def sink(hb) -> None:
            seen.append(hb.seq)
            if len(seen) == 3:
                stop.set()

        await asyncio.wait_for(emitter.run(slow_probe, sink, stop=stop), timeout=5)
        return seen

    assert asyncio.run(go()) == [1, 2, 3]
