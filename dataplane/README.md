# Data Plane & Measurement — Person A

Worker wrapper (vLLM + llama.cpp), heartbeat emitter, calibration campaign,
non-stationarity measurement, trace generator, replay client, log join pipeline,
figures.

**Requirements owned:** F-9, F-10, F-11 (worker side), F-13, F-15 – F-20.
**MPR owned:** MPR-1 — τ and the variance envelope, Week 2, hardware only.
**Load profile:** front-heavy, Weeks 1–3.

```bash
uv sync --all-extras
uv run pytest
```

---

## `worker/` — F-9, F-10, F-11

```
gRPC ingress (Execute)
      ↓
Admission wrapper        — timeout ceiling, records queue-entry stamp
      ↓
Engine adapter           — one interface, two implementations
      ↓                    submit(prompt_token_ids, output_len) → (n_tokens, timings)
   [vLLM adapter]  [llama.cpp adapter]
      ↓
Response sender          — direct gRPC to client_endpoint          (F-11)
      ↓
Local JSONL logger       — append-only, fsync at run end only

Telemetry sampler (independent loop)
      → Heartbeat stream to scheduler
      → Completion RPC on each finish
```

The engine adapter interface is the only thing that must be identical across the two
runtimes:

```
submit(prompt_token_ids, output_len, req_id) -> ServiceResult
probe() -> LiveState{queue_depth, inflight, recent_tok_s, kv_frac, state}
```

Everything runtime-specific — `ignore_eos`, `min_tokens`/`max_tokens`,
`enable_prefix_caching=False`, llama.cpp slot reuse disabled — is set **once at engine
construction** and recorded in the manifest's node block (C-6), so the settings that
eliminate your confounds are auditable from the results.

**Prefill/decode split without streaming (F-18):** vLLM exposes per-request
arrival/first-scheduled/first-token/finished times on its output object; llama.cpp's
server returns a `timings` block with prompt and predicted milliseconds. Both are
worker-local engine clocks. If either turns out not to expose it on your pinned
version, log `service_ns` only and record `f18_status: "partial"` in the manifest
**rather than faking the split**.

> **Python version.** The worker is the one component pinned to Python (§10) — vLLM is
> a Python library. That means *vLLM's* supported Python matrix pins the worker
> environment, not the other way round. `pyproject.toml` allows `>=3.11,<3.14`;
> confirm the actual pin against the vLLM version you install in Week 1 and record it
> in the manifest's node block. The harness and pipeline have no such constraint.

## `harness/` — F-16, F-17, F-20

### `gen_trace.py`

```
config → SeedSequence(seed).spawn(3)
           ├── rng_arrival  → MMPP / Poisson offsets
           ├── rng_length   → bucket draws + priority draws
           └── rng_content  → per-request content_seed
        → sort by offset → assign req_ids → write header + records → sha256
```

Single-threaded, no I/O in the sampling loop, no wall-clock reads. **Separate streams**
so that changing the length distribution does not shift the arrival process underneath
you.

Regenerating a trace with the same seed and parameters must produce a **byte-identical
file**. That is a Week-1 test, not an assumption. Float formatting is the usual culprit:
fix `arrival_offset_s` at 4 decimal places.

### `replay.py`

```
load trace + verify sha256
      ↓
materialize prompts up front (all of them, before t0)   ← keeps the timing loop allocation-free
      ↓
t0 = monotonic()
for each req:
    sleep_until(t0 + arrival_offset_s)
    assert send_lag < threshold
    spawn task → Dispatch → await Deliver → log
      ↓
drain in-flight, flush log
```

**Open-loop:** the loop never waits on a response before firing the next request.
Pre-materialising every prompt matters more than it sounds — tokenising inside the
timing loop is the most common cause of send-lag drift under high λ.

`send_lag_ms` is the open-loop guard. Assert per request; if *any* request in the
measurement window exceeds the threshold (suggest 50 ms), the run is marked **invalid in
the manifest** rather than analysed.

> **Language note.** The replay client is the one A-side component where Python may not
> hold. Python asyncio is adequate below roughly 50 req/s; above that, GIL contention
> shows up as send-lag violations. Go is a legitimate choice here and the seam
> (gRPC + JSONL) fully supports it. Decide from measured send-lag, not in advance.

## `pipeline/` — F-19

A **pure function** of `(manifest, three log files)` → one Parquet file. No network, no
engine, runnable on a laptop.

This is why B can hand you a directory of *simulator* logs and the pipeline processes
them with zero changes. Do not let an engine import, a gRPC stub, or a hostname leak in
here.

## `figures/`

Consume Parquet only, never raw logs.

**F-24: make the figure scripts read `manifest.vehicle` and stamp simulated plots
automatically.** Labelling by hand fails exactly once, in the final report.

## Why A also owns the harness and the figures

The trace generator, the prompt materializer, and the replay client all need to agree
exactly on how a `content_seed` becomes a token sequence. Splitting the generator from
the replayer across two people creates a second drift surface for no benefit — and A
already owns the tokenizer and vocabulary through the worker work.

Figures, because by Week 5 A's engine work is frozen (feature freeze is end of Week 3)
while B is running sweeps, so analysis load naturally moves to A. Hypothesis-specific
figures in Weeks 5–6 are joint.
