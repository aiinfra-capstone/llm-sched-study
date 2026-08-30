"""Week 1, still open — the worker wrapper and its engine adapter.

Written before the code, and skipped until it lands. That is the same pattern
`test_trace_determinism.py` used while `gen_trace` was still a plan, and it is worth
repeating here for a specific reason: the worker is where F-18 either holds or quietly
stops holding, and the difference between "the split is measured" and "the split is
plausible" is not visible in a figure.

The module layout these tests assume, from §8.1 of the split doc:

    dataplane.worker.adapter   — ServiceResult, LiveState, the interface both engines meet
    dataplane.worker.llamacpp  — the pool implementation; parse_timings / build_request
    dataplane.worker.log       — C-4 worker records

`parse_timings` and `build_request` are named as pure functions on purpose. Everything
interesting about this adapter is a translation — an engine's JSON into my contract, and
my contract into an engine's request body — and a translation that can only be tested
through a live HTTP server is a translation that will be tested once, by hand, on a node.

If the implementation lands under different names, these tests are what say what the names
have to mean. Rewrite the call, keep the assertion.
"""

from __future__ import annotations

import pytest
from conftest import pending

pytestmark = pytest.mark.forward

adapter = pending(
    "dataplane.worker.adapter", "ServiceResult", week="Week 1", deliverable="worker wrapper"
)
llamacpp = pending(
    "dataplane.worker.llamacpp", "parse_timings", week="Week 1", deliverable="llama.cpp adapter"
)

# A real b10569 /completion response, trimmed to the block F-18 depends on. The CUDA
# build was confirmed to emit both halves; this is the shape it emits.
FULL_TIMINGS = {
    "content": "",
    "tokens_predicted": 128,
    "timings": {
        "prompt_n": 512,
        "prompt_ms": 210.0,
        "predicted_n": 128,
        "predicted_ms": 1790.0,
    },
}

# What a backend that does not expose the block returns. Vulkan was unbuilt at the
# freeze, so this path is not hypothetical.
NO_TIMINGS = {"content": "", "tokens_predicted": 128}


# --------------------------------------------------------------------------------------
# The interface both engines meet
# --------------------------------------------------------------------------------------


def test_service_result_carries_the_fields_the_worker_log_needs() -> None:
    """Every C-4 worker field that is not observable from outside the engine comes from
    here. A missing one means the log gets a zero instead of a measurement."""
    fields = set(adapter.ServiceResult.__dataclass_fields__)
    assert {"output_tokens", "service_ns", "prefill_ns", "decode_ns", "status"} <= fields


def test_live_state_carries_what_a_heartbeat_reports() -> None:
    """§8.1: probe() -> LiveState{queue_depth, inflight, recent_tok_s, kv_frac, state}.
    These are the five values a policy's node view is built from."""
    fields = set(adapter.LiveState.__dataclass_fields__)
    assert {"queue_depth", "inflight", "recent_tok_s", "kv_frac", "state"} <= fields


# --------------------------------------------------------------------------------------
# F-18 — the prefill/decode split, measured or honestly absent
# --------------------------------------------------------------------------------------


def test_a_timings_block_becomes_a_real_split() -> None:
    """Confirmed on the pinned CUDA build: `prompt_ms`/`prompt_n` is prefill and
    `predicted_ms`/`predicted_n` is decode, from one code path for the whole pool."""
    result = llamacpp.parse_timings(FULL_TIMINGS)
    assert result.prefill_ns == 210_000_000
    assert result.decode_ns == 1_790_000_000
    assert result.f18_status == "full"


def test_a_missing_timings_block_is_partial_not_zero() -> None:
    """The one rule that matters here. `service_ns` alone and `f18_status: "partial"` is
    an honest gap; a zero prefill is a number somebody will plot and believe."""
    result = llamacpp.parse_timings(NO_TIMINGS)
    assert result.prefill_ns is None
    assert result.decode_ns is None
    assert result.f18_status == "partial"


def test_the_split_never_exceeds_the_service_time_it_decomposes() -> None:
    """Both halves come from the engine's own clock, and their sum is bounded by the
    worker-local duration that contains them. A violation means two different clocks."""
    result = llamacpp.parse_timings(FULL_TIMINGS, service_ns=2_100_000_000)
    assert result.prefill_ns + result.decode_ns <= result.service_ns


def test_a_partial_timings_block_is_treated_as_absent() -> None:
    """Half a split is not a split. A block with prefill and no decode must degrade to
    `partial` rather than reporting a prefill against an invented decode."""
    half = {"timings": {"prompt_n": 512, "prompt_ms": 210.0}}
    result = llamacpp.parse_timings(half)
    assert result.f18_status == "partial"
    assert result.prefill_ns is None


# --------------------------------------------------------------------------------------
# The request the worker actually sends
# --------------------------------------------------------------------------------------


def test_the_output_length_is_forced() -> None:
    """The trace fixes `output_len`. A request that stops at an EOS measures a different
    workload than the one in the manifest, and the difference is node-dependent — which
    would land squarely on the heterogeneity axis the study is measuring."""
    body = llamacpp.build_request(prompt_token_ids=[1, 2, 3], output_len=64)
    assert body["n_predict"] == 64
    assert body["ignore_eos"] is True


def test_prompts_go_as_token_ids_not_text() -> None:
    """The materializer produces ids. Re-tokenising text would make prompt_len a claim
    rather than a fact, and prefill cost is measured against prompt_len."""
    body = llamacpp.build_request(prompt_token_ids=[7, 8, 9], output_len=8)
    assert body["prompt"] == [7, 8, 9]


def test_prefix_caching_is_disabled() -> None:
    """Recorded in the manifest node block as `prefix_caching: false`. With it on, the
    second request of a bucket is cheaper than the first, and service time stops being a
    function of length alone — which is the assumption the whole cost model rests on."""
    body = llamacpp.build_request(prompt_token_ids=[1], output_len=1)
    assert body.get("cache_prompt") is False


def test_sampling_is_deterministic() -> None:
    """Service time should vary with load and thermal state, not with how many tokens the
    sampler happened to reject. Greedy decoding removes one source of variance from a
    measurement whose entire subject is variance."""
    body = llamacpp.build_request(prompt_token_ids=[1], output_len=1)
    assert body.get("temperature") == 0


# --------------------------------------------------------------------------------------
# probe() — what the heartbeat reports
# --------------------------------------------------------------------------------------


def test_kv_frac_is_slot_occupancy_on_llama_cpp() -> None:
    """`/slots` returns exactly `--parallel` entries with `is_processing`. llama.cpp
    exposes slot occupancy, not paged-KV occupancy, and the worker log schema says so."""
    slots = [{"is_processing": True}, {"is_processing": True}, {"is_processing": False}]
    assert llamacpp.kv_frac(slots) == pytest.approx(2 / 3)


def test_an_engine_that_exposes_nothing_reports_the_sentinel() -> None:
    """-1.0 means "not exposed" on the wire. 0.0 would tell the scheduler the node is
    idle, and a queue-aware policy would then pile work onto it."""
    assert llamacpp.kv_frac([]) == -1.0


def test_engine_state_is_one_of_the_three_contract_values() -> None:
    """ "warming" is not cosmetic: a cold node reporting "ready" gets routed to during the
    exact window its throughput estimate is least true."""
    assert set(adapter.ENGINE_STATES) == {"ready", "warming", "degraded"}


# --------------------------------------------------------------------------------------
# What the worker writes, and what it must never compute
# --------------------------------------------------------------------------------------


def test_the_worker_record_conforms_to_c4(schema) -> None:
    log = pytest.importorskip("dataplane.worker.log", reason="Week 1: worker log not implemented")
    record = log.build_record(
        run_id="run_0142",
        req_id="r000417",
        node_id="n2",
        result=llamacpp.parse_timings(FULL_TIMINGS, service_ns=2_000_000_000),
        queue_wait_ns=88_000_000,
        prompt_tokens=512,
        batch_size_at_admission=3,
        inflight_at_admission=2,
        kv_occupancy_at_admission=0.41,
    )
    errors = list(schema("log_worker").iter_errors(record))
    assert not errors, [e.message for e in errors]


def test_the_worker_record_carries_no_client_stamp() -> None:
    """Watch-list failure mode 3. `client_send_mono_ns` arrives on the wire for gap
    detection and must never enter a duration. The worker's queue wait is measured from
    its own admission stamp, on its own monotonic clock."""
    source = pytest.importorskip("dataplane.worker.log").__file__
    with open(source) as fh:
        assert "client_send_mono_ns" not in fh.read()


def test_heartbeat_sequence_numbers_are_gapless_per_node() -> None:
    """`validity.heartbeat_gaps` counts breaks in this sequence, and H3 is about what a
    stale estimate costs. A resequenced counter would hide the very thing being measured."""
    emitter = pytest.importorskip(
        "dataplane.worker.heartbeat", reason="Week 1: heartbeat emitter not implemented"
    )
    beat = emitter.HeartbeatEmitter(node_id="n1", run_id="run_0142")
    assert [beat.next().seq for _ in range(5)] == [1, 2, 3, 4, 5]
