# Two-Person Split & Interface Contract
### Distributed LLM inference scheduling measurement study — 6-week window

**Status:** proposal, to be frozen at end of Week 1 alongside the requirements spec.

> ### ⚠ Partly superseded by the single-engine decision
>
> The [requirements spec](scheduling-requirements-spec.pdf) is the authority, and it now
> carries this as **F-9 / F-9a / F-9b** plus threat **R9**: every *pool* node runs
> llama.cpp with GGUF, GPU and CPU alike, and the mixed vLLM/llama.cpp pool is withdrawn.
> vLLM is retained as one measured condition (F-9b), not as a pool member.
>
> The body of this document is **not** rewritten. The passages the spec now contradicts
> are marked inline with `[superseded]`.
>
> Affected here: the "Owns" row of §0's table, the two-adapter worker in §8.1, the
> engine-specific F-18 note in §5, and — most consequentially — **the whole Python
> argument in §10**, which no longer holds. See the note there.

---

## 0. Where the seam goes, and why

The spec (§10) assigns three members: A = worker/runtime/calibration, B = scheduler/policies, C = harness/simulator. Collapsing to two people, the naive move is to merge B and C — scheduler plus simulator to one person, workers plus harness to the other. That is the correct merge, but for a non-obvious reason.

**The binding constraint is F-21:** the discrete-event simulator must execute *the same policy implementations* as the live scheduler — shared code, not a reimplementation. If the live scheduler and the DES are owned by different people, they will drift. Not maliciously; through ordinary divergence in tie-breaking, in how "queue depth" is defined at the moment of decision, in whether an in-flight request counts before or after admission. That drift invalidates F-23 validation silently, because both systems will still run and still produce plausible numbers.

So: **one person owns the policy code and both of its hosts.** Everything else is arranged around that.

| | **Divyansh Shukla (A) — Data Plane & Measurement** | **Aditya Gupta (B) — Control Plane & Simulation** |
|---|---|---|
| Owns | `[superseded]` Worker wrapper (**llama.cpp only**; capability throttling F-9a; engine-gap probe F-9b), heartbeat emitter, calibration campaign, non-stationarity measurement, trace generator, replay client, log join pipeline, figures | Scheduler core, five policy implementations, admission filter, node state store, staleness injection, discrete-event simulator, F-23 validation |
| Spec requirements | F-9, F-10, F-11 (worker side), F-13, F-15, F-16, F-17, F-18, F-19, F-20 | F-1, F-2, F-3, F-4, F-5, F-6, F-7, F-8, F-11 (scheduler side), F-12, F-14, F-21, F-22, F-23, F-24 |
| Owns MPR | MPR-1 (τ and variance envelope — Week 2, hardware only) | MPR-3 (H2/H3 sweeps in validated simulator) |
| Load profile | Front-heavy: Weeks 1–3 | Back-heavy: Weeks 3–5 |

MPR-2 (the 2×2 decomposition on real hardware) is jointly owned and is the first point where both halves must be working simultaneously.

**Why A also gets the harness rather than B:** the trace generator, the prompt materializer, and the replay client all need to agree exactly on how a `content_seed` becomes a token sequence. Splitting the generator from the replayer across two people creates a second drift surface for no benefit. And A already owns the tokenizer and vocabulary through the worker work.

**Why A also gets figures:** by Week 5 A's engine work is frozen (feature freeze is end of Week 3) while B is running sweeps. Analysis load naturally moves to A. Hypothesis-specific figures in Week 5–6 are joint.

---

## 1. The integration contract — six frozen artifacts

A and B interact through exactly six things. Nothing else crosses the seam. Freeze all six at end of Week 1; changes after that require the same re-scoping ritual as the requirements spec.

| # | Artifact | Direction | Format |
|---|---|---|---|
| C-1 | `scheduling.proto` | bidirectional | protobuf3 / gRPC |
| C-2 | Trace file | A → A (B reads for DES) | JSONL + header record |
| C-3 | Cost model snapshot | A → B | JSON |
| C-4 | Log record schemas | A and B → pipeline | JSONL, one schema per emitter |
| C-5 | Joined record schema | pipeline → figures | Parquet (or CSV) |
| C-6 | Run manifest | run launcher → everything | JSON |

C-1 and C-3 are the only true *runtime* couplings. C-2 and C-4–C-6 are file formats, which means they can be validated offline with fixture files — and should be, before either side is finished. **Both people should be able to run against fixtures by end of Week 1.** B builds a fake worker that heartbeats scripted state; A builds a fake scheduler that round-robins blindly. Neither waits on the other.

---

## 2. C-1 — Wire schemas

```protobuf
syntax = "proto3";
package sched.v1;

// ---------- Client → Scheduler ----------
// The scheduler IS in the request path (payload is small; LAN).
// The scheduler is NOT in the response path (F-11).
message DispatchRequest {
  string   run_id            = 1;
  string   req_id            = 2;   // from the trace, e.g. "r000417"
  repeated uint32 prompt_token_ids = 3;
  uint32   output_len        = 4;   // forced; ignore_eos on the worker
  uint32   priority          = 5;   // 0 = interactive, 1 = background
  string   bucket_id         = 6;   // e.g. "p512_o128"
  string   client_endpoint   = 7;   // where the worker returns the response
  uint64   client_send_mono_ns = 8; // A's clock. Informational only; never subtracted cross-host.
}

message DispatchAck {
  string req_id       = 1;
  string chosen_node  = 2;
  bool   accepted     = 3;
  string reject_reason = 4;  // "no_admissible_node" | "run_not_started" | ""
}

// ---------- Scheduler → Worker ----------
message ExecuteRequest {
  string   run_id            = 1;
  string   req_id            = 2;
  repeated uint32 prompt_token_ids = 3;
  uint32   output_len        = 4;
  uint32   priority          = 5;
  string   bucket_id         = 6;
  string   client_endpoint   = 7;
  uint32   decision_seq      = 8;  // joins to the scheduler's decision record
}

message ExecuteAck { string req_id = 1; bool queued = 2; }

// ---------- Worker → Client (direct; F-11) ----------
message ResponseDelivery {
  string run_id            = 1;
  string req_id            = 2;
  string node_id           = 3;
  uint32 output_tokens     = 4;
  string status            = 5;   // "ok" | "timeout" | "oom" | "engine_error"
  uint64 worker_service_ns = 6;   // worker-local duration
  uint64 worker_queue_wait_ns = 7;// worker-local duration
}

// ---------- Worker → Scheduler ----------
message Heartbeat {
  string run_id             = 1;
  string node_id            = 2;
  uint64 seq                = 3;   // monotonic per node; gap detection
  uint32 queue_depth        = 4;   // admitted, not yet started
  uint32 inflight_count     = 5;   // in the engine's running batch
  double recent_tokens_per_s = 6;  // EWMA over a stated window
  double kv_occupancy_frac  = 7;   // -1.0 if the engine does not expose it
  uint64 worker_mono_ns     = 8;
  string engine_state       = 9;   // "ready" | "warming" | "degraded"
}

message Completion {
  string run_id   = 1;
  string node_id  = 2;
  string req_id   = 3;
  string status   = 4;
  uint64 service_ns = 5;
}

// ---------- Run control ----------
message BeginRun { string run_id = 1; string config_hash = 2; string trace_sha256 = 3; }
message EndRun   { string run_id = 1; }

service Scheduler {
  rpc Dispatch     (DispatchRequest)     returns (DispatchAck);
  rpc StreamHeartbeat (stream Heartbeat) returns (stream BeginRun);
  rpc ReportCompletion (Completion)      returns (ExecuteAck);
}
service Worker {
  rpc Execute  (ExecuteRequest) returns (ExecuteAck);
  rpc Begin    (BeginRun)       returns (ExecuteAck);
  rpc End      (EndRun)         returns (ExecuteAck);
}
service Client {
  rpc Deliver  (ResponseDelivery) returns (ExecuteAck);
}
```

**Three design notes worth defending in the report:**

1. **`Completion` exists separately from `Heartbeat`** because the scheduler is off the response path and would otherwise learn about completions only at the next heartbeat tick. That lag is a *second, uncontrolled* staleness source sitting alongside the one you inject deliberately for H3. `Completion` shrinks it to one LAN RTT. Measure the residual anyway and report it as a floor on achievable estimate freshness.

2. **`client_endpoint` travels with the request** so the worker can return directly without the scheduler proxying. This is what makes F-11 cheap.

3. **All timestamps on the wire are labelled with their originating host and are never subtracted across hosts.** They exist for gap detection and debugging. Every duration in the analysis comes from a single machine's monotonic clock.

---

## 3. C-2 — Trace file

JSONL. First line is a header record; every subsequent line is one request.

```json
{"record":"header","trace_schema":1,"gen_seed":20260421,"n_requests":4000,
 "duration_s":600,"arrival":{"process":"mmpp","lambda_base":3.5,"burst_lambda":14.0,
 "burst_mean_s":8.0,"quiet_mean_s":45.0},
 "length_dist":{"buckets":["p128_o64","p512_o128","p2048_o256"],"weights":[0.5,0.35,0.15]},
 "priority_mix":{"0":0.7,"1":0.3},
 "admissible":{"max_prompt":2048,"max_output":256,"timeout_ceiling_ms":60000},
 "vocab_size":128000,"reserved_ids_excluded":true,"generator_git_sha":"a3f1c09"}
{"record":"req","req_id":"r000001","arrival_offset_s":0.0000,"prompt_len":512,
 "output_len":128,"bucket_id":"p512_o128","priority":0,"content_seed":91827364}
{"record":"req","req_id":"r000002","arrival_offset_s":0.2841, ...}
```

**Token IDs are not stored** — only `content_seed`. The materializer turns `(content_seed, prompt_len, vocab_size)` into an exact token sequence deterministically. This keeps traces small and diffable, and means the trace file is genuinely portable to the simulator, which doesn't need token content at all.

The trace's SHA-256 is the identity used everywhere downstream. Regenerating a trace with the same seed and parameters must produce a byte-identical file — that's a Week-1 test, not an assumption. Float formatting is the usual culprit; fix `arrival_offset_s` at 4 decimal places.

---

## 4. C-3 — Cost model snapshot (the most load-bearing artifact)

A produces these; B's scheduler and B's DES both consume them. This is where the two halves of the project actually meet.

```json
{
  "cost_model_schema": 1,
  "snapshot_id": "cm_rtx3090_awq_20260428T1412Z",
  "node_class": "rtx3090_awq_llama3_8b",
  "measured_at_unix": 1777392720,
  "calibration_run_ids": ["cal_0031","cal_0032"],
  "form": "lookup_table",
  "entries": [
    {"prompt_bucket":[257,1024],"output_bucket":[65,192],"concurrency":1,
     "service_ms_mean":1840.0,"service_ms_p50":1795.0,"service_ms_p95":2210.0,
     "tokens_per_s":69.6,"n_samples":40},
    {"prompt_bucket":[257,1024],"output_bucket":[65,192],"concurrency":4,
     "service_ms_mean":3120.0,"service_ms_p50":3040.0,"service_ms_p95":4015.0,
     "tokens_per_s":41.0,"n_samples":40}
  ],
  "stochastic": {"model":"lognormal_multiplier","sigma":0.113,
                 "autocorr_time_s":42.0,"fit_r2":0.87},
  "admissibility": {"max_prompt":2048,"max_output":256,"timeout_ceiling_ms":60000},
  "provenance": {"engine":"vllm","engine_version":"0.6.x","quant":"awq",
                 "gpu":"RTX 3090","driver":"550.x","prefix_caching":false}
}
```

**Two requirements on A that follow from H3 and are easy to miss:**

- A must emit a **time-ordered series of snapshots** during the calibration campaign, not one final fitted model. Staleness injection (F-8) means B serves the scheduler a snapshot from *s* seconds ago. If A emits only one snapshot, B has to synthesize aging by perturbing parameters — which makes H3 a study of B's perturbation model rather than of real drift. Real snapshot history makes H3 an empirical result.
- The `stochastic.autocorr_time_s` field is MPR-1's headline number and is what F-22 uses to give the DES realistic variance. It's a deliverable, not a diagnostic.

**On `form`:** F-7 permits a lookup table *or* a ≤6-parameter regression. Pick one and commit at end of Week 2 — supporting both doubles B's interpolation logic for no research gain.

---

## 5. C-4 — Log record schemas

Three emitters, three files, appended locally, never written over the network. Joined offline on `req_id`.

### `client_{run_id}.jsonl` — Divyansh Shukla (A)

```json
{"run_id":"run_0142","req_id":"r000417","intended_offset_s":37.4210,
 "actual_send_offset_s":37.4232,"send_lag_ms":2.2,
 "e2e_duration_ns":2140883000,"status":"ok","output_tokens":128,
 "responding_node":"n2","chosen_node_from_ack":"n2","dispatch_ack_ns":1420000}
```

`send_lag_ms` is the open-loop guard. Assert per request; if any request in the measurement window exceeds the threshold (suggest 50 ms), the run is marked invalid in the manifest rather than analysed.

### `scheduler_{run_id}.jsonl` — Aditya Gupta (B)

Two record types, discriminated by `type`.

```json
{"type":"decision","run_id":"run_0142","req_id":"r000417","decision_seq":417,
 "policy":"wjsq","staleness_param_s":0.0,"decide_duration_ns":184000,
 "chosen_node":"n2","tie_break_draw":0.4471,
 "candidates":[
   {"node_id":"n1","queue_depth":3,"inflight":2,"capability_tok_s":69.6,
    "estimate_age_ms":412,"admissible":true,"score":0.0431},
   {"node_id":"n2","queue_depth":1,"inflight":1,"capability_tok_s":58.2,
    "estimate_age_ms":388,"admissible":true,"score":0.0344},
   {"node_id":"n3","queue_depth":0,"inflight":0,"capability_tok_s":4.1,
    "estimate_age_ms":501,"admissible":false,"score":null}]}
{"type":"completion_observed","run_id":"run_0142","req_id":"r000417",
 "node_id":"n2","source":"completion_rpc","observed_lag_ns":1900000}
```

The `candidates` array is F-3 in full. It's what lets you compute routing-error rate post hoc — for each dispatch, whether an alternative node would have finished materially sooner — and, critically, whether the error came from a *bad policy* or a *stale estimate*. Without `estimate_age_ms` per candidate, H3 is unanalysable.

### `worker_{node_id}_{run_id}.jsonl` — Divyansh Shukla (A)

```json
{"run_id":"run_0142","req_id":"r000417","node_id":"n2","engine":"vllm",
 "queue_wait_ns":88000000,"prefill_ns":210000000,"decode_ns":1790000000,
 "service_ns":2000000000,"prompt_tokens":512,"output_tokens":128,
 "batch_size_at_admission":3,"inflight_at_admission":2,
 "kv_occupancy_at_admission":0.41,"status":"ok"}
```

`[superseded]` Prefill/decode split is obtainable without streaming: llama.cpp's server returns a timings block with prompt and predicted milliseconds. With a uniform pool this is now **one code path**, not two engine-specific ones. Treat it as a worker-local engine clock. If your pinned llama.cpp build turns out not to expose it, log `service_ns` only and record F-18 as partially satisfied in the manifest rather than faking the split.

---

## 6. C-5 — Joined record, and the honest handling of F-18

The pipeline (A) emits one row per request:

```
run_id, req_id, policy, lambda, staleness_s, R, node_count,     # from manifest
bucket_id, prompt_len, output_len, priority,                     # from trace
intended_offset_s, send_lag_ms, e2e_ms, status,                  # from client
chosen_node, decide_us, chosen_queue_depth, chosen_est_age_ms,
  best_alt_node, best_alt_est_service_ms, routing_error_ms,      # from scheduler
queue_wait_ms, prefill_ms, decode_ms, service_ms,                # from worker
transport_residual_ms,                                           # derived
is_warmup                                                        # derived
```

**`transport_residual_ms = e2e_ms − queue_wait_ms − service_ms − decide_us/1000`.**

This is the important admission. F-18 asks for `transport_in` and `transport_out` as separate stages. Without clock synchronisation across machines you cannot measure them separately — you can only measure the *sum of everything not accounted for by single-host durations*. Report it as one residual, state why, and note that on a LAN it is small enough that its decomposition doesn't affect any hypothesis. Do not install PTP to fix this; it is a week you don't have, for a number you don't need.

`is_warmup` is computed by `intended_offset_s < manifest.warmup_s`, from the trace, not from wall-clock — so warmup discard is identical in hardware and simulator runs.

---

## 7. C-6 — Run manifest

One JSON per run, written by the launcher before the run and finalised after.

```json
{"run_id":"run_0142","started_unix":1777400000,"vehicle":"hardware",
 "config_hash":"9c1e...","config": { ...verbatim config... },
 "trace_path":"traces/t_lam3.5_s20260421.jsonl","trace_sha256":"f4a9...",
 "policy":"wjsq","lambda":3.5,"staleness_s":0.0,"warmup_s":60,"duration_s":600,
 "cost_model_snapshots":{"n1":"cm_rtx3090_awq_20260428T1412Z","n2":"cm_...","n3":"cm_..."},
 "nodes":[{"node_id":"n1","host":"box-a","engine":"vllm","engine_version":"0.6.x",
           "model":"llama3-8b","quant":"awq","gpu":"RTX 3090","driver":"550.x",
           "prefix_caching":false,"max_batch":8}],
 "git_shas":{"worker":"a3f1c09","scheduler":"77bd120","harness":"a3f1c09","sim":"77bd120"},
 "validity":{"max_send_lag_ms":3.9,"send_lag_violations":0,"dropped_requests":0,
             "heartbeat_gaps":0,"engine_restarts":0,"valid":true}}
```

`vehicle` is `"hardware"` or `"simulator"`, and F-24 requires every simulated figure to be labelled. Make the figure scripts read this field and stamp the plot automatically — labelling by hand fails exactly once, in the final report.

---

## 8. Internal architecture — Divyansh Shukla (A)

### 8.1 Worker

```
gRPC ingress (Execute)
      ↓
Admission wrapper        — timeout ceiling, records queue-entry stamp
      ↓
Engine adapter           — one interface  [superseded: one POOL implementation]
      ↓                    submit(prompt_token_ids, output_len) → (n_tokens, timings)
   [llama.cpp adapter]     (vLLM adapter retained for the F-9b probe only)
      ↓
Response sender          — direct gRPC to client_endpoint
      ↓
Local JSONL logger       — append-only, fsync at run end only

Telemetry sampler (independent loop)
      → Heartbeat stream to scheduler
      → Completion RPC on each finish
```

`[superseded]` The adapter interface is retained even though one implementation now serves the whole pool — the F-9b probe is its second implementation, and writing the probe against the same interface is what makes the engine-gap number a comparison rather than an anecdote:

```
submit(prompt_token_ids, output_len, req_id) -> ServiceResult
probe() -> LiveState{queue_depth, inflight, recent_tok_s, kv_frac, state}
```

Everything runtime-specific — `ignore_eos`, `min_tokens`/`max_tokens`, `enable_prefix_caching=False`, llama.cpp slot reuse disabled — is set once at engine construction and recorded in the manifest's node block, so the settings that eliminate your confounds are auditable from the results.

### 8.2 Trace generator (`gen_trace.py`)

```
config → SeedSequence(seed).spawn(3)
           ├── rng_arrival  → MMPP / Poisson offsets
           ├── rng_length   → bucket draws + priority draws
           └── rng_content  → per-request content_seed
        → sort by offset → assign req_ids → write header + records → sha256
```

Single-threaded, no I/O in the sampling loop, no wall-clock reads. Separate streams so that changing the length distribution does not shift the arrival process underneath you.

### 8.3 Replay client (`replay.py`)

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

Open-loop: the loop never waits on a response before firing the next request. Pre-materialising every prompt matters more than it sounds — tokenising inside the timing loop is the most common cause of send-lag drift under high λ.

### 8.4 Results pipeline

Pure function of `(manifest, three log files)` → one Parquet file. No network, no engine, runnable on a laptop. Then figure scripts consume Parquet only. Keeping these two stages separate means B can hand A a directory of simulator logs and A's pipeline processes them with zero changes.

---

## 9. Internal architecture — Aditya Gupta (B)

The whole design goal here is that **`choose()` cannot tell whether it is running in the live scheduler or the DES.**

```
                 ┌──────────────── shared core ────────────────┐
                 │                                             │
  live path      │   Clock (interface)                         │   sim path
  ─────────      │   StateStore (interface)                    │   ────────
  gRPC ingress   │   StalenessVeil  ── wraps StateStore,       │   event queue
        ↓        │                     serves views aged by s  │        ↓
  AdmissionFilter│         ↓                                   │   AdmissionFilter
        ↓        │   choose(request, node_view, now, rng)      │        ↓
  Forwarder      │         ↓                                   │   ServiceSampler
  (Execute RPC)  │   DecisionLogger (emits C-4 records)        │   (cost model + noise)
        ↓        │                                             │        ↓
  Heartbeat/     └─────────────────────────────────────────────┘   state mutation
  Completion                                                        events
  consumers → StateStore
```

**Component notes:**

- **`Clock`** — `now_ns()`. Live implementation reads the monotonic clock; sim implementation returns the event-queue time. Policies never call the system clock directly. This is the single change that makes shared policy code possible.
- **`StateStore`** — the scheduler's belief about each node: queue depth, in-flight, capability estimate, and the timestamp each was learned. Updated by heartbeat and completion in the live path; by simulated events in the DES.
- **`StalenessVeil`** — a wrapper, not a flag. It serves the policy a view of the state store as it existed `s` seconds ago, drawn from the snapshot history. Making it a layer rather than a parameter inside each policy means no policy can accidentally read fresh state. F-8 says staleness is a first-class feature; this is what first-class looks like structurally.
- **`AdmissionFilter`** — applies F-14 *outside* the policy, so every policy including RoundRobin and Threshold(T) inherits the admissibility constraint identically and no policy is scored on infinite-latency outcomes.
- **The five policies** are pure functions with no state of their own except an injected `rng` for tie-breaks. RoundRobin's counter is the one exception; keep it in an explicit policy-state object passed in, so a replayed trace produces identical dispatch sequences.
- **`ServiceSampler`** (DES only) — reads C-3 and returns a service time for `(node, prompt_len, output_len, concurrency)` plus the fitted lognormal multiplier with the measured autocorrelation. This is the only component in B's half with no live-path counterpart.

**Validation harness (F-23)** is B's, and is mechanically simple once the above holds: run the same trace through both vehicles at three operating points, join both output Parquets, compare p50 and p95. Report observed error, not just pass/fail.

---

## 10. Structural independence — what actually pins the platform

| Component | Owner | Language pinned? | By what |
|---|---|---|---|
| Worker wrapper | A | ~~**Yes — Python**~~ → **No** `[superseded]` | The original reason was that vLLM is a Python library. F-9 (single engine) removes vLLM from the pool, so the pool worker wraps `llama-server` — an HTTP binary — and is language-free. |
| F-9b engine-gap probe | A | **Yes — Python**, weakly | vLLM as a library pins this one-off measurement. Avoidable by driving its OpenAI-compatible server over HTTP; the extra hop does not matter here, because the probe is a throughput-ratio measurement and not a pool member whose `service_ns` feeds a policy comparison. |
| Trace generator | A | No | Pure computation + file write. Must produce byte-identical output to whatever the determinism test expects. |
| Replay client | A | **No — and this matters** | Needs precise `sleep_until` and thousands of concurrent in-flight requests. Python asyncio is adequate below roughly 50 req/s; above that, GIL contention shows up as send-lag violations. Go is a legitimate choice here and the seam (gRPC + JSONL) fully supports it. |
| Results pipeline / figures | A | No | pandas + matplotlib is the path of least resistance, but nothing forces it. |
| Scheduler core | B | No | Any gRPC-capable language. |
| Policies | B | **Must equal the DES's language** | F-21. Internal to B, so B is free to choose — but B must choose *once*. |
| DES | B | **Must equal the scheduler's language** | Same constraint from the other side. |

**Net position:** `[superseded]` **A is no longer pinned to Python anywhere in the measurement path.** The single remaining Python pull is the one-off F-9b probe, and even that is avoidable over HTTP. Everything else on both sides was already free, provided the six contract artifacts hold. B could reasonably build the entire control plane and simulator in Go or Rust and never touch Python; A could now build the *whole* data plane in Go if send-lag under high λ argues for it (see the replay-client row above, which was already the strongest reason to).

**The one thing that must not happen:** B writing the scheduler in one language and the DES in another, then "keeping the policies in sync." That is F-21 violated in spirit while satisfied on paper, and it is the failure mode most likely to survive undetected until the validation numbers look strange in Week 4.

---

## 11. Handoff points and fixture-first sequencing

| Week | A delivers | B delivers | Joint gate |
|---|---|---|---|
| 1 | `scheduling.proto` frozen; trace generator with byte-identical determinism test; replay client against a **fake scheduler**; log schemas + fixture files | Scheduler skeleton against a **fake worker** that heartbeats scripted state; `Clock` and `StateStore` interfaces; RoundRobin only | End-to-end single request, real worker, real scheduler. Harness replays a seeded trace and emits joined records. |
| 2 | Calibration campaign; **time-ordered C-3 snapshots**; τ and variance envelope | Remaining four policies against fixture cost models; StalenessVeil | **MPR-1 achieved.** C-3 frozen. |
| 3 | Admissible-set determination; validation-anchor runs at 3+ operating points | All five policies live from one config value; admission filter | Load band identified. **Feature freeze.** |
| 4 | Pipeline hardened; figure scripts | DES parameterised from Week-2 snapshots; F-23 validation | Simulator agrees within stated tolerance. |
| 5 | Figures for H1/H2/H3 | R × load × staleness × policy sweeps | Hypotheses tested. |
| 6 | Threats to validity, limitations | Literature check (R-1), positioning | Report. |

The fixture-first pattern in Week 1 is what buys the parallelism. Neither person should ever be blocked waiting for the other's component to be real — the fake worker and fake scheduler are throwaway, cost half a day each, and are the difference between two people working in parallel and two people working in sequence on a timeline with no slack.

---

## 12. Failure modes at the seam — watch for these

1. **Silent policy drift** between live scheduler and DES. Mitigation: the cross-environment determinism test (build step L) — same trace, same seed, deterministic service times injected into the DES, assert the *dispatch sequence* is identical. Run it in CI, not by hand.
2. **Cost model schema evolution mid-project.** A learns something in Week 2 and wants another field. That breaks B's DES. Mitigation: `cost_model_schema` version field, and B's loader rejects unknown versions loudly rather than defaulting.
3. **Clock discipline erosion.** Someone will, at some point, compute a duration by subtracting a worker timestamp from a client timestamp because it's convenient. Mitigation: the joined schema has no cross-host subtraction in it, and `transport_residual_ms` is named "residual" precisely so nobody mistakes it for a measurement.
4. **Heartbeat gaps treated as zero load.** If a node stops heartbeating, a naive StateStore shows stale-but-plausible state and the policy keeps routing to it. Mitigation: `estimate_age_ms` is already in the decision record — add an explicit staleness ceiling above which a node is treated as unavailable, and log when it fires.
5. **Warmup discarded differently in the two vehicles.** Mitigation: `is_warmup` computed from the trace's `intended_offset_s` in both, never from run wall-clock.

---

## 13. One inconsistency to resolve before freezing

> **RESOLVED — passthrough label only**, the first of the two options below. `priority`
> is generated and carried through the trace and logs, no policy reads it, and the
> priority metric is withdrawn from the spec's §5.4 dependent variables. The reasoning
> is kept below; the decision is made and the spec reflects it.

`priority` appears in the trace schema (F-16 requires a configurable priority mix) and in the dependent variables (§5.4: high-priority p99 under low-priority load). But **none of the five policies in the 2×2 design is priority-aware**, and priority tiers were dropped from the 6-week scope relative to the earlier pitch.

Two coherent resolutions:

- **Carry priority as a passthrough label only.** It travels in the trace and the logs, no policy reads it, and §5.4's priority metric is dropped from the dependent variables. Cleanest, and costs nothing.
- **Add a priority dimension to the request space** and report the high-priority p99 metric as a descriptive observation under policies that are priority-blind — i.e. "here is what happens to interactive requests when nothing protects them." That's a legitimate small finding and requires no new policy.

~~Pick one at freeze.~~ **Picked:** the first. The failure case named here — leaving `priority` in the schema, never acting on it, and having an examiner ask what it's for — is precisely what the resolution forecloses, by removing the *metric* while keeping the *label*. The spec's §5.4 now says so explicitly.

There is a third option, considered and deferred to §9 rather than rejected outright:
correlate priority with request length (interactive → short, background → long). That is
the only variant in which the metric becomes informative, because short requests then
queue behind long ones and head-of-line blocking becomes measurable and genuinely
policy-dependent. It was declined on cost against a timeline with no slack, not on merit.
