# Data Plane & Measurement — Divyansh Shukla (A)

Worker wrapper (llama.cpp), capability throttling, the cost-model calibration
campaign, the F-9b engine-gap measurement, heartbeat emitter, non-stationarity
measurement, trace generator, replay client, log join pipeline, figures.

**Requirements owned:** F-9, F-9a, F-9b, F-10, F-11 (worker side), F-13, F-15 – F-20.
**MPR owned:** MPR-1 — τ and the variance envelope, Week 2, hardware only.
**Load profile:** front-heavy, Weeks 1–3.

```bash
uv sync --all-groups
uv run pytest
```

---

## `worker/` — F-9, F-9a, F-9b, F-10, F-11

> **Amended by [SCOPE-CHANGE-001](../docs/SCOPE-CHANGE-001.md).** One runtime — llama.cpp
> with GGUF — on every pool node, GPU and CPU alike. The mixed vLLM/llama.cpp pool in the
> frozen F-9 is withdrawn, because engine and quantization were confounded with hardware
> in the definition of *R*. vLLM survives as **one measured condition** (F-9b), not as a
> pool member.

```
gRPC ingress (Execute)
      ↓
Admission wrapper        — timeout ceiling, records queue-entry stamp
      ↓
llama.cpp adapter        — HTTP to a local llama-server
      ↓                    submit(prompt_token_ids, output_len) → (n_tokens, timings)
      ↓
Response sender          — direct gRPC to client_endpoint          (F-11)
      ↓
Local JSONL logger       — append-only, fsync at run end only

Telemetry sampler (independent loop)
      → Heartbeat stream to scheduler
      → Completion RPC on each finish
```

The adapter interface stays as it was — one interface, now one *pool* implementation:

```
submit(prompt_token_ids, output_len, req_id) -> ServiceResult
probe() -> LiveState{queue_depth, inflight, recent_tok_s, kv_frac, state}
```

Keep the interface even though only one implementation serves the pool. The F-9b probe
is its second implementation, and writing that probe against the same interface is what
makes the engine-gap number a comparison rather than an anecdote.

Everything runtime-specific — `ignore_eos`, `min_tokens`/`max_tokens`, slot reuse
disabled — is set **once at engine construction** and recorded in the manifest's node
block (C-6), so the settings that eliminate your confounds are auditable from the
results.

### Capability throttling — F-9a

Per-node capability is now **configuration, not hardware class**. Three knobs, all
recorded in `manifest.nodes[].engine_config`:

| Knob | llama.cpp flag | Effect |
|---|---|---|
| GPU offload fraction | `-ngl` | The primary throttle. `0` = CPU-only. |
| Thread count | `--threads` | Secondary; dominates once `-ngl` is 0. |
| Slot count | `--parallel` | The fixed batch capacity. Also `SimNode.batch_capacity`. |

This is what makes *R* **tunable on physical hardware** rather than reachable only in
simulation — the same GPU at `-ngl 99` and `-ngl 40` is two node classes. Week 2 sweeps
these per machine to establish the synthesizable *R* range, which is then **reported as
a range**, not a single figure (§7, MPR-2).

> **Distinct machines only.** Throttling must be applied to separate physical hosts.
> Two logical nodes on one box contend for PCIe, memory bandwidth, and cache — which
> reintroduces as contention exactly the confound this change removes. The manifest
> carries `validity.colocated_nodes`, and it must be `0`. Have the launcher assert it;
> this is the kind of thing that gets violated at 2am in Week 3 because one machine was
> free.

### The engine-gap measurement — F-9b

Run **once**, on the strongest node, at one operating point, against an identical
replayed trace: vLLM (AWQ, same model class) versus llama.cpp on that same machine.
Report the observed throughput ratio as a **stated bound on external validity** (threat
R9).

Mark it `role: "engine_gap_probe"` in the manifest — see
`contracts/examples/manifest.engine_gap.sample.json`. The pipeline must keep it out of
every policy comparison: it is a measured condition, and it appears in no figure other
than the engine-gap result. The cost of a single non-production engine is thereby
*measured* rather than assumed away, which is the entire reason the change is defensible.

### Prefill/decode split without streaming (F-18)

llama.cpp's server returns a `timings` block with `prompt_ms` and `predicted_ms` — so
the split now comes from **one code path for the whole pool** rather than two
engine-specific ones. Treat them as worker-local engine clocks. If your pinned
llama.cpp build does not expose it, log `service_ns` only and record
`f18_status: "partial"` in the manifest **rather than faking the split**.

> **Python is no longer forced here.** §10 of the split doc pins the worker to Python
> because vLLM is a library, and notes that `llama-server` — an HTTP binary — only
> follows that choice because it sits beside the vLLM adapter. SCOPE-CHANGE-001 removes
> the vLLM adapter from the pool, so that argument has nothing left holding it up: the
> pool worker now wraps an HTTP binary and is language-free like the rest of your half.
> What remains pinned is the **F-9b probe alone**, and only if you drive vLLM as a
> library rather than through its OpenAI-compatible server. Python is still the path of
> least resistance and `pyproject.toml` allows `>=3.11,<3.14` — but it is now a
> preference, not a constraint, and the constraint is worth knowing you have shed.

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
