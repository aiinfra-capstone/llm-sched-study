"""F-9 — the llama.cpp engine adapter.

This is the only place in my half that knows what an inference engine looks like. Under
F-9 there is exactly one engine in the pool, so there is exactly one of these, and the
prefill/decode split comes from one code path for every node rather than from two
engine-specific ones that agree by luck.

`llama-server` is an HTTP binary, so the "adapter" is a client. That is deliberate: the
worker wrapper does not link the engine, does not reimplement its batching (F-5), and
does not pull CUDA wheels into this package. It starts a server, talks to it, and records
what it says.

Three measurement rules are enforced here rather than trusted downstream:

1. **The engine's clock and mine are the same clock.** The wrapper runs on the same host
   as the `llama-server` it talks to, so `service_ns` (my monotonic span) and the engine's
   `timings` block are commensurable. Nothing here subtracts a stamp taken on another
   machine.
2. **The split is reported, never reconstructed.** `prefill_ns` and `decode_ns` come from
   `timings.prompt_ms` and `timings.predicted_ms` or they are absent. If a backend does
   not emit the block I log `service_ns` alone and the run is stamped
   `f18_status="partial"` — an engine that cannot tell me where the time went is a fact
   about the engine, not a licence to divide `service_ns` by a guess.
3. **The residual is kept, not hidden.** `service_ns - (prefill_ns + decode_ns)` is
   queueing inside the engine plus HTTP framing. C-4 has no field for it, so it does not
   go in the worker log — but the calibration observation keeps it, because a residual
   that quietly grows with concurrency is the batching effect F-4 is about.

What the pinned build actually exposes, verified against b10569+cuda13.2 rather than
assumed:

    /completion  timings.prompt_ms / .prompt_n       -> prefill      (F-18)
                 timings.predicted_ms / .predicted_n -> decode       (F-18)
                 timings.cache_n                     -> prefix reuse, must stay 0
    /slots       exactly `--parallel` entries, each with is_processing
                                                     -> LiveState.kv_frac (F-10)
    /props       the model and generation settings the server is actually running

`/slots` reports **slot occupancy, not paged-KV occupancy** — llama.cpp does not expose
the latter. C-4 names the field `kv_occupancy_at_admission` and says to report the
fraction of `--parallel` slots in use, which is what this returns. The name is the
contract's; the meaning is written down here so nobody reads that column as a KV-cache
figure in Week 6.
"""

from __future__ import annotations

import time
from typing import Any, Self

import httpx

from dataplane.worker.adapter import LiveState, ServiceResult

__all__ = ["LlamaCppAdapter", "build_request", "kv_frac", "parse_timings"]

# `cache_prompt: false` on every request. Prefix caching would make service time depend on
# what the *previous* request looked like, which turns a cost model keyed on
# (prompt_len, output_len, concurrency) into one keyed on trace order — and the same trace
# replayed under two policies visits the nodes in different orders. The manifest records
# `prefix_caching: false`; this is where that claim is actually made true.
_CACHE_PROMPT = False


class LlamaCppAdapter:
    """An async client for one `llama-server`, plus the C-4 record it produces.

    One adapter is one node. It holds no queue of its own: llama.cpp's `--parallel` slots
    *are* the queue, which is exactly why F-9a makes slot count the experimental knob and
    why the DES can model a node as a fixed-capacity server without approximating
    anything.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        node_id: str,
        timeout_ceiling_ms: int,
        engine_version: str = "unknown",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.node_id = node_id
        self.timeout_ceiling_ms = timeout_ceiling_ms
        self.engine_version = engine_version
        self._client = client or httpx.AsyncClient(timeout=None)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def health(self) -> bool:
        """True once the model is loaded. Poll this instead of sleeping a fixed time.

        Model load is minutes for the 8B on a cold page cache and seconds when warm, so
        any constant here is either a wasted wait or a race.
        """
        try:
            r = await self._client.get(f"{self.endpoint}/health", timeout=5.0)
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    async def props(self) -> dict[str, Any]:
        """`/props` — what the server says it is running, for provenance."""
        r = await self._client.get(f"{self.endpoint}/props", timeout=10.0)
        r.raise_for_status()
        return r.json()

    async def live_state(self) -> LiveState:
        """F-10. Slot occupancy now, or the explicit 'unavailable' when `--no-slots`."""
        try:
            r = await self._client.get(f"{self.endpoint}/slots", timeout=5.0)
        except httpx.HTTPError:
            return LiveState.unavailable()
        if r.status_code != 200:
            return LiveState.unavailable()
        slots = r.json()
        if not isinstance(slots, list) or not slots:
            return LiveState.unavailable()
        busy = sum(1 for s in slots if s.get("is_processing"))
        return LiveState(
            inflight=busy,
            slots_total=len(slots),
            kv_frac=kv_frac(slots),
            # llama.cpp admits exactly `--parallel` requests at a time, so anything beyond
            # that is queued in the wrapper, not in the engine. /slots alone cannot see it.
            queue_depth=0,
            state="ready",
        )

    async def complete(self, prompt_tokens: list[int], output_len: int) -> ServiceResult:
        """One forced-length completion. Never raises for an engine failure — classifies it.

        `ignore_eos` plus `n_predict` makes output length an *independent variable* rather
        than something the model chooses. Without it a cost model keyed on `output_len`
        would be fitting the model's stopping behaviour, and the same trace would produce
        different service times on different models — which would confound the model-set
        replication axis with the scheduling result.

        The prompt goes on the wire as token ids, not text. The materializer already
        settled what `(content_seed, prompt_len)` means; re-tokenising a string here would
        add a second definition of prompt length that drifts from the trace's.
        """
        body = build_request(prompt_tokens, output_len)
        started = time.monotonic_ns()
        try:
            r = await self._client.post(
                f"{self.endpoint}/completion",
                json=body,
                timeout=self.timeout_ceiling_ms / 1000.0,
            )
        except httpx.TimeoutException as exc:
            return ServiceResult(
                status="timeout",
                service_ns=time.monotonic_ns() - started,
                prompt_tokens=len(prompt_tokens),
                output_tokens=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        except httpx.HTTPError as exc:
            return ServiceResult(
                status="engine_error",
                service_ns=time.monotonic_ns() - started,
                prompt_tokens=len(prompt_tokens),
                output_tokens=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        service_ns = time.monotonic_ns() - started

        if r.status_code != 200:
            return ServiceResult(
                status=_classify_error(r),
                service_ns=service_ns,
                prompt_tokens=len(prompt_tokens),
                output_tokens=0,
                error=f"HTTP {r.status_code}: {r.text[:200]}",
            )
        return parse_timings(r.json(), service_ns=service_ns, prompt_len=len(prompt_tokens))

    def worker_record(
        self,
        *,
        run_id: str,
        req_id: str,
        completion: ServiceResult,
        queue_wait_ns: int,
        state_at_admission: LiveState,
    ) -> dict[str, Any]:
        """One C-4 worker log record. Optional fields are omitted, never faked.

        `batch_size_at_admission` and `inflight_at_admission` are the same number under
        llama.cpp and that is not an oversight: a slot is a batch member, so the size of
        the batch this request joined *is* the number of slots busy when it was admitted.
        C-4 keeps them separate because vLLM's F-9b probe distinguishes them, and a
        schema that only fits the pool engine would have to change to carry the probe.
        """
        rec: dict[str, Any] = {
            "run_id": run_id,
            "req_id": req_id,
            "node_id": self.node_id,
            "engine": "llamacpp",
            "queue_wait_ns": queue_wait_ns,
            "service_ns": completion.service_ns,
            "prompt_tokens": completion.prompt_tokens,
            "output_tokens": completion.output_tokens,
            "batch_size_at_admission": state_at_admission.inflight,
            "inflight_at_admission": state_at_admission.inflight,
            "status": completion.status,
        }
        if completion.prefill_ns is not None:
            rec["prefill_ns"] = completion.prefill_ns
        if completion.decode_ns is not None:
            rec["decode_ns"] = completion.decode_ns
        rec["kv_occupancy_at_admission"] = state_at_admission.kv_frac
        return rec


def _classify_error(response: httpx.Response) -> str:
    """Map a non-200 onto C-4's four-value status enum.

    OOM is separated from a generic engine error because F-15 makes the cliff a
    *reportable observation*: on a CPU or low-VRAM node a long-context request fails
    categorically rather than slowly, and folding that into `engine_error` would lose the
    one signal that tells the admissible-set search where the edge is.
    """
    text = ""
    try:
        text = response.text.lower()
    except (UnicodeDecodeError, httpx.HTTPError):  # pragma: no cover - defensive
        text = ""
    if "out of memory" in text or "oom" in text or "kv cache" in text:
        return "oom"
    return "engine_error"


def parse_timings(
    payload: dict[str, Any], *, service_ns: int = 0, prompt_len: int = 0
) -> ServiceResult:
    """Turn a `/completion` response into a `ServiceResult`. The single F-18 decision point.

    Accepts either the whole response or a bare `timings` block, because the caller that
    matters is a backend probe asking "does this build tell me where the time went", and it
    should not have to know which shape it is holding.

    **Half a split is not a split.** A block carrying `prompt_ms` but no `predicted_ms`
    degrades to `partial` with both halves `None`, rather than reporting a prefill and
    leaving decode to be inferred — because `service_ns - prefill_ns` is not decode, it is
    decode plus in-engine queueing, and writing it into the `decode_ns` column would put a
    queueing artifact into the one number the cost model is built on.
    """
    timings = payload.get("timings", payload) or {}
    prefill_ns = decode_ns = None
    if "prompt_ms" in timings and "predicted_ms" in timings:
        prefill_ns = round(float(timings["prompt_ms"]) * 1e6)
        decode_ns = round(float(timings["predicted_ms"]) * 1e6)
    return ServiceResult(
        status="ok",
        service_ns=service_ns,
        prompt_tokens=int(payload.get("tokens_evaluated", prompt_len)),
        output_tokens=int(payload.get("tokens_predicted", 0)),
        prefill_ns=prefill_ns,
        decode_ns=decode_ns,
        cached_tokens=int(timings.get("cache_n", 0)),
        slot_id=int(payload.get("id_slot", -1)),
    )


def build_request(prompt_token_ids: list[int], output_len: int) -> dict[str, Any]:
    """The `/completion` body for one calibrated request. Four settings, four reasons.

    `n_predict` + `ignore_eos` **force** the output length. The trace fixes `output_len`,
    and a request that stopped at an EOS would measure a different request than the one
    the trace describes — worse, it would measure the *model's* stopping behaviour, so the
    same trace would produce different service times on Mistral than on Llama and the
    model-set replication axis would be confounded with the scheduling result.

    The prompt goes as **token ids**, not text. The materializer already settled what
    `(content_seed, prompt_len)` means; re-tokenising a string here would create a second
    definition of prompt length that drifts from the trace's.

    `cache_prompt: false` because prefix caching makes service time depend on what the
    *previous* request looked like — turning a cost model keyed on
    (prompt_len, output_len, concurrency) into one keyed on trace order, when the same
    trace under two policies visits the nodes in different orders. The manifest claims
    `prefix_caching: false`; this is where that claim is made true.

    `temperature: 0` because service time should vary with load and thermal state, not
    with how many tokens the sampler happened to reject.
    """
    # The parameter carries C-1's name (`prompt_token_ids`), not C-4's (`prompt_tokens`).
    # They are different contracts about different things — one is the request going out,
    # the other is the count that came back — and collapsing the names would invite the
    # two to be confused exactly where a prompt is truncated.
    return {
        "prompt": prompt_token_ids,
        "n_predict": output_len,
        "ignore_eos": True,
        "cache_prompt": _CACHE_PROMPT,
        "temperature": 0.0,
    }


def kv_frac(slots: list[dict[str, Any]]) -> float:
    """Fraction of `--parallel` slots busy, or -1.0 when the engine exposes nothing.

    -1.0 rather than 0.0 is the whole point: 0.0 is a real reading meaning "idle", and a
    scheduler told a saturated node is idle will pile onto it.
    """
    if not slots:
        return -1.0
    return sum(1 for s in slots if s.get("is_processing")) / len(slots)
