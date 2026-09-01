"""C-4 — the worker log: `worker_{node_id}_{run_id}.jsonl`.

One line per request, every duration on this worker's own monotonic clock. The pipeline
joins these to the client and scheduler logs on `req_id` (C-5), so the only thing that has
to be true here is that a record is written for **every** request the worker admitted,
including the ones that failed.

That last point is the one worth being deliberate about. A worker that logs only its
successes produces a file where the failure rate is zero by construction, and the join
then silently drops those requests — so a policy that routes badly enough to cause
timeouts would look *better* than one that does not. Failures are the F-15 cliff signal;
they are the data.

`kv_occupancy_at_admission` is slot occupancy, not paged-KV occupancy — see `LiveState`.
The optional split fields are omitted rather than zeroed when the backend is `partial`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dataplane.worker.adapter import LiveState, ServiceResult

__all__ = ["WorkerLog", "build_record"]


def build_record(
    *,
    run_id: str,
    req_id: str,
    node_id: str,
    queue_wait_ns: int,
    result: ServiceResult | None = None,
    state_at_admission: LiveState | None = None,
    service_ns: int | None = None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    batch_size_at_admission: int | None = None,
    inflight_at_admission: int | None = None,
    status: str | None = None,
    prefill_ns: int | None = None,
    decode_ns: int | None = None,
    kv_occupancy_at_admission: float | None = None,
    engine: str = "llamacpp",
) -> dict[str, Any]:
    """One C-4 record. Optional fields are omitted, never faked.

    Two ways in, because two callers need different things. The pool worker holds a
    `ServiceResult` and a `LiveState` and passes those. The F-9b vLLM probe emits C-4
    records too and is not a llama.cpp client at all, so it passes the fields flat. Where
    both are given the explicit field wins — the objects are defaults, not overrides.

    `batch_size_at_admission` counts this request and `inflight_at_admission` does not, so
    under llama.cpp the first is the second plus one. That off-by-one is the point of
    keeping two fields: `inflight` is what the scheduler could have observed before it
    dispatched, and `batch_size` is the batch that actually resulted, which is what indexes
    C-3's `concurrency` axis. The cost model is calibrated at concurrency 1 and 4, so a
    request served alone has to record 1 and not 0 or it indexes nothing.

    A `ServiceResult.error` is deliberately not written: C-4 is `additionalProperties:
    false`, and the diagnostic text belongs where it is useful — on the result object and
    in the calibration observations — not in the artifact the pipeline reads.
    """

    def pick(explicit, from_result, default=None):
        if explicit is not None:
            return explicit
        if result is not None:
            return from_result(result)
        return default

    inflight = inflight_at_admission
    if inflight is None and state_at_admission is not None:
        inflight = state_at_admission.inflight
    inflight = 0 if inflight is None else inflight

    kv = kv_occupancy_at_admission
    if kv is None and state_at_admission is not None:
        kv = state_at_admission.kv_frac
    kv = -1.0 if kv is None else kv

    record: dict[str, Any] = {
        "run_id": run_id,
        "req_id": req_id,
        "node_id": node_id,
        "engine": engine,
        "queue_wait_ns": queue_wait_ns,
        "service_ns": pick(service_ns, lambda r: r.service_ns, 0),
        "prompt_tokens": pick(prompt_tokens, lambda r: r.prompt_tokens, 0),
        "output_tokens": pick(output_tokens, lambda r: r.output_tokens, 0),
        "batch_size_at_admission": (
            batch_size_at_admission if batch_size_at_admission is not None else inflight + 1
        ),
        "inflight_at_admission": inflight,
        "status": pick(status, lambda r: r.status, "ok"),
    }
    prefill = pick(prefill_ns, lambda r: r.prefill_ns)
    decode = pick(decode_ns, lambda r: r.decode_ns)
    if prefill is not None:
        record["prefill_ns"] = prefill
    if decode is not None:
        record["decode_ns"] = decode
    record["kv_occupancy_at_admission"] = kv
    return record


class WorkerLog:
    """Append-only C-4 writer, one file per (node, run).

    Line-buffered and flushed per record. A worker that buffers its log loses the tail of
    a run exactly when the run is interesting — the OOM, the timeout, the moment the
    engine fell over — so the cost of a flush per request is paid deliberately. It is
    microseconds against seconds of service time, and it is not inside the measured
    interval: `service_ns` is stamped by the adapter before this is ever called.
    """

    def __init__(self, directory: str | Path, *, node_id: str, run_id: str) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.path = Path(directory) / f"worker_{node_id}_{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self.n_written = 0

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()
        self.n_written += 1

    def log(
        self,
        *,
        req_id: str,
        result: ServiceResult,
        queue_wait_ns: int,
        state_at_admission: LiveState,
        engine: str = "llamacpp",
    ) -> dict[str, Any]:
        """Build and write one record. Returns it, so a caller can assert on it."""
        record = build_record(
            run_id=self.run_id,
            req_id=req_id,
            node_id=self.node_id,
            queue_wait_ns=queue_wait_ns,
            result=result,
            state_at_admission=state_at_admission,
            engine=engine,
        )
        self.write(record)
        return record

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
