"""The llama.cpp adapter (F-9, F-10, F-18).

Every test here runs against `httpx.MockTransport` rather than a server, because what
needs pinning down is how the adapter *classifies* what a server says: a timeout is not
an engine error, an OOM is not a generic failure, and an absent `timings` block is not a
zero-millisecond prefill. Those are decisions in my code, and a live server would only
ever exercise the branch it happened to take that day.

The one claim a mock cannot support is that the pinned build emits `timings` at all. That
was verified against b10569+cuda13.2 directly and is recorded per run as `f18_status`;
here I only assert the adapter reads the block when it is present and refuses to invent
it when it is not.

No `pytest-asyncio` in the lockfile and no reason to add one for this: each test drives
its coroutine through `asyncio.run`, which is also closer to how the campaign calls it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from dataplane.worker.adapter import LiveState, ServiceResult, f18_status
from dataplane.worker.llamacpp import LlamaCppAdapter

_TIMINGS = {
    "prompt_n": 64,
    "prompt_ms": 243.288,
    "predicted_n": 16,
    "predicted_ms": 109.847,
    "cache_n": 0,
}


def _adapter(handler, timeout_ms: int = 5000) -> LlamaCppAdapter:
    return LlamaCppAdapter(
        "http://node/",
        node_id="n1",
        timeout_ceiling_ms=timeout_ms,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _ok_body(**over: Any) -> dict[str, Any]:
    return {
        "tokens_evaluated": 64,
        "tokens_predicted": 16,
        "id_slot": 2,
        "timings": dict(_TIMINGS),
    } | over


def _complete(handler, prompt=(1, 2, 3), output_len=16) -> ServiceResult:
    async def go() -> ServiceResult:
        a = _adapter(handler)
        try:
            return await a.complete(list(prompt), output_len)
        finally:
            await a.aclose()

    return asyncio.run(go())


def test_completion_reads_both_halves_of_the_f18_split() -> None:
    c = _complete(lambda r: httpx.Response(200, json=_ok_body()))

    assert c.status == "ok"
    assert c.prefill_ns == 243_288_000
    assert c.decode_ns == 109_847_000
    assert (c.prompt_tokens, c.output_tokens, c.slot_id) == (64, 16, 2)
    assert f18_status(c) == "full"


def test_absent_timings_gives_partial_rather_than_zero_prefill() -> None:
    """A backend that omits `timings` must degrade to `partial`, never to zeros.

    Zeros are indistinguishable from a real measurement of an instantaneous prefill, and
    would flow into the cost model as if they were data.
    """
    c = _complete(lambda r: httpx.Response(200, json={"tokens_predicted": 8}))

    assert c.prefill_ns is None and c.decode_ns is None
    assert c.residual_ns is None
    assert c.decode_tokens_per_s is None
    assert f18_status(c) == "partial"


def test_residual_is_service_minus_the_two_accounted_stages() -> None:
    c = _complete(lambda r: httpx.Response(200, json=_ok_body()))
    assert c.residual_ns == c.service_ns - (c.prefill_ns + c.decode_ns)


def test_decode_throughput_ignores_prefill() -> None:
    """tok/s must not move when prompt length does — R is a ratio of these numbers."""
    c = ServiceResult(
        status="ok", service_ns=10**9, prompt_tokens=2048, output_tokens=100, decode_ns=10**9
    )
    assert c.decode_tokens_per_s == pytest.approx(100.0)

    no_split = ServiceResult(status="ok", service_ns=1, prompt_tokens=1, output_tokens=1)
    zero_decode = ServiceResult(
        status="ok", service_ns=1, prompt_tokens=1, output_tokens=1, decode_ns=0
    )
    no_tokens = ServiceResult(
        status="ok", service_ns=1, prompt_tokens=1, output_tokens=0, decode_ns=10**9
    )
    assert no_split.decode_tokens_per_s is None
    assert zero_decode.decode_tokens_per_s is None
    assert no_tokens.decode_tokens_per_s == 0.0


def test_timeout_is_its_own_status_not_an_engine_error() -> None:
    """F-13's timeout ceiling is a study parameter; conflating it with failure loses it."""

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("past the ceiling", request=request)

    c = _complete(slow)
    assert c.status == "timeout" and c.output_tokens == 0


def test_transport_failure_is_an_engine_error() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _complete(refused).status == "engine_error"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("ggml_backend_cuda_buffer_type_alloc_buffer: out of memory", "oom"),
        ("failed to allocate KV cache", "oom"),
        ("invalid request payload", "engine_error"),
    ],
)
def test_oom_is_separated_from_generic_failure(body: str, expected: str) -> None:
    """F-15 makes the cliff a standalone observation, so OOM cannot hide inside 'error'."""
    assert _complete(lambda r: httpx.Response(500, text=body)).status == expected


def test_health_is_true_only_on_200() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("not up yet", request=request)

    async def go() -> tuple[bool, bool, bool]:
        return (
            await _adapter(lambda r: httpx.Response(200, json={"status": "ok"})).health(),
            await _adapter(lambda r: httpx.Response(503, json={})).health(),
            await _adapter(refused).health(),
        )

    assert asyncio.run(go()) == (True, False, False)


def test_props_surfaces_what_the_server_says_it_is_running() -> None:
    props = asyncio.run(
        _adapter(lambda r: httpx.Response(200, json={"model_path": "/m/llama.gguf"})).props()
    )
    assert props["model_path"].endswith("llama.gguf")


def test_live_state_reports_slot_occupancy() -> None:
    slots = [{"is_processing": True}] * 2 + [{"is_processing": False}] * 2
    state = asyncio.run(_adapter(lambda r: httpx.Response(200, json=slots)).live_state())
    assert state == LiveState(inflight=2, slots_total=4, kv_frac=0.5)


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(501, text="slots disabled"),
        lambda r: httpx.Response(200, json={}),
        lambda r: httpx.Response(200, json=[]),
    ],
)
def test_live_state_says_unavailable_rather_than_guessing(handler) -> None:
    """C-4 wants -1.0 for 'not exposed'. A 0.0 would read as a genuinely idle node."""
    assert asyncio.run(_adapter(handler).live_state()).kv_frac == -1.0


def test_live_state_survives_a_dead_socket() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone", request=request)

    assert asyncio.run(_adapter(down).live_state()) == LiveState.unavailable()


def test_worker_record_omits_the_split_it_does_not_have() -> None:
    async def go() -> tuple[dict[str, Any], dict[str, Any]]:
        a = _adapter(lambda r: httpx.Response(200, json=_ok_body()))
        try:
            full = a.worker_record(
                run_id="run_1",
                req_id="r1",
                completion=await a.complete([1], 16),
                queue_wait_ns=5,
                state_at_admission=LiveState(inflight=3, slots_total=4, kv_frac=0.75),
            )
            partial = a.worker_record(
                run_id="run_1",
                req_id="r2",
                completion=ServiceResult(
                    status="ok", service_ns=1, prompt_tokens=1, output_tokens=1
                ),
                queue_wait_ns=0,
                state_at_admission=LiveState.unavailable(),
            )
            return full, partial
        finally:
            await a.aclose()

    full, partial = asyncio.run(go())

    assert full["prefill_ns"] == 243_288_000 and full["decode_ns"] == 109_847_000
    # Off by one by design: inflight is what the scheduler could have seen before it
    # dispatched, batch counts this request too and is what indexes C-3's concurrency.
    assert full["inflight_at_admission"] == 3 and full["batch_size_at_admission"] == 4
    assert full["kv_occupancy_at_admission"] == 0.75
    assert full["engine"] == "llamacpp" and full["node_id"] == "n1"

    assert "prefill_ns" not in partial and "decode_ns" not in partial
    assert partial["kv_occupancy_at_admission"] == -1.0


def test_adapter_closes_only_the_client_it_created() -> None:
    """A borrowed client outlives the adapter; an owned one does not."""

    async def go() -> tuple[bool, bool]:
        borrowed = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
        async with LlamaCppAdapter(
            "http://n/", node_id="n", timeout_ceiling_ms=1, client=borrowed
        ) as a:
            assert a.endpoint == "http://n"
        still_open = not borrowed.is_closed
        await borrowed.aclose()

        owned = LlamaCppAdapter("http://n", node_id="n", timeout_ceiling_ms=1)
        await owned.aclose()
        return still_open, owned._client.is_closed

    assert asyncio.run(go()) == (True, True)
