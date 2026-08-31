# Data Plane & Measurement — Divyansh Shukla (A)

The worker wrapper (llama.cpp), capability throttling, the calibration campaign, the
non-stationarity measurement, the trace generator and replay client, the admissible-set and
load-band determinations, the log join pipeline, and the figures.

**Requirements owned:** F-9, F-9a, F-9b, F-10, F-11 (worker side), F-13, F-15 – F-20, F-23
(the hardware half).
**MPR owned:** MPR-1 — τ and the variance envelope, hardware only.
**Load profile:** front-heavy, Weeks 1–3.

```bash
uv sync --all-groups
uv run pytest
```

## Where things stand

Everything below is built and has been run on hardware. The three results the rest of the
study leans on:

| | |
|---|---|
| **MPR-1** | τ = **69.5 s** on `cpu_ngl0_p4_q4km_llama3_8b`, *r²* = 0.989. Both GPU classes show no decay at any timescale their service time can resolve. A single calibrated tok/s figure on the CPU node understates its own standard error by **1.36×**. |
| **Synthesizable *R*** | **2.00×** configured, **1.00×** deployable — the second number is one physical host, and it is what MPR-2 waits on. |
| **Admissible set** | 8B pool: `prompt ≤ 128, output ≤ 64`, limited by the CPU class. 1B pool: `prompt ≤ 512, output ≤ 128`. |

Plus four F-23 validation anchors on one trace, and a load band of **1.03–1.30 req/s**.

## The engine is pinned *and patched*

`engine_version` is `b10569+p1+cuda13.2`. The `+p1` is
[`patches/llamacpp-b10569-skip-chat-parse-on-completion.patch`](../patches), without which
`llama-server` returns HTTP 500 on about 1 request in 100 and discards a completed
generation. `server_task_result_cmpl_final::update()` runs the chat PEG parser
unconditionally, but `/completion` renders through `to_json_non_oaicompat()`, which never
reads the parsed message — so the parse is dead work that can throw, and forced output
lengths (`n_predict` + `ignore_eos`) reach it by ending mid-UTF-8-character.

Upgrading does not fix it: the lenient-parsing fix for the same failure is already in
b10569 and only covers *partial* parses, and master still calls it unconditionally. So the
pin moved sideways rather than forward. `llama-server --version` cannot know the tree was
modified, which is exactly why the marker is in `engine_version` and the binary hash is in
the manifest.

## Tests

`uv run pytest` is what CI runs, and it enforces **100% statement and branch coverage** of
`src/dataplane`. That is not a vanity number. This half of the repo is an instrument, and a
line of the harness no test executes is a line whose behaviour I would be assuming when I
report a measurement taken through it. If a new line is unreachable from a test, the honest
options are to test it or to delete it.

The generated protobuf stubs are excluded — they are protoc output, regenerated on import
and gitignored (§12.2). What they must actually *do* is asserted in `test_proto_stubs.py`.

| | |
|---|---|
| `test_forward_w[1-5]_*.py` | Written against work scheduled for a later week. They **skip** until it lands, and `-rs` prints every skip reason — so the skip list in a CI log is the remaining backlog of my half. Only the Week 5–6 figure scripts still skip. |
| `-m perf` | `test_replay_load.py` measures send-lag under sustained load. That is a property of the machine rather than of the code, so a shared runner cannot assert a threshold on it without either flaking or being set so loose it proves nothing. Run it on the load host: `uv run pytest -m perf -s`. |
| `-m integration` | Runs by default, but launches real subprocesses and binds real sockets, so it is slower than the rest. |

Because the coverage gate is on by default, running a subset needs `--no-cov`:

```bash
uv run pytest tests/test_manifest.py --no-cov
```

**Do not run the suite on the load host while a campaign is measuring.** I did, and it cost
me a four-point anchor set: 99 timeouts and a 3.7-second client send lag at an offered rate
well under capacity. The `perf` marker exists for that reason, but the rule covers the whole
suite during a run, not only the tests labelled as load measurements.

`test_golden_trace.py` is the one test that can notice the generator itself changing.
Everything else about determinism compares this build against itself — generate twice, get
the same bytes — which stays true even if the output is wrong, because both sides move
together. A reference computed once and written down is the only check on a claim about
*later*, and "the trace regenerates byte-for-byte from (config, seed)" is entirely a claim
about later. I found the gap by mutation testing: swapping `rng_length` and `rng_content` in
`generate` changed every byte of every trace the harness produces, and the whole suite still
passed.

`test_properties.py` is Hypothesis, for the invariants stated as universals in the source —
most importantly the three RNG streams: changing the length mix must not move the arrivals
or the prompt content, or an *R*-sweep cannot attribute anything to *R*. A hand-written
example checking two length distributions is not evidence for a claim about all of them.

---

## `worker/` — F-9, F-9a, F-9b, F-10, F-11

> **F-9: one runtime.** llama.cpp with GGUF on every pool node, GPU and CPU alike. Engine
> and quantization are held constant because otherwise they are confounded with hardware in
> the definition of *R*, and neither the 2×2 decomposition nor the *R*-sweep can separate an
> engine effect from a hardware effect. vLLM survives as **one measured condition** (F-9b),
> not as a pool member.

```
gRPC Execute  →  admission semaphore of exactly --parallel permits   ← queue_wait_ns
                        ↓
                 llama.cpp adapter — HTTP to a local llama-server    ← service_ns
                        ↓
                 Deliver, direct to client_endpoint                  (F-11)
                        ↓
                 append-only C-4 JSONL, flushed per record

heartbeat loop (independent) → Heartbeat stream to scheduler         (F-10)
                             → Completion RPC on each finish
```

`serve.py` is that, and three of its decisions are measurement decisions rather than
plumbing.

**The wrapper owns admission, not the engine.** llama.cpp will accept more requests than it
has slots and queue them internally, and if I let it, that wait lands inside `service_ns`
where nothing can separate it from compute. The semaphore is what makes C-4's two duration
columns mean two different things — and it is why the simulator can model a node as a
fixed-capacity server without approximating anything: the slot count *is*
`SimNode.batch_capacity`.

**`Execute` returns before the work is done.** Blocking would apply backpressure to the
scheduler, and backpressure at the scheduler silently converts an open-loop experiment into
a closed-loop one. The replay client's send-lag guard watches one end of that rule; this is
the other.

**`/slots` is read once per request**, after the permit is acquired and before the
completion is posted. It costs a loopback round trip that lands in neither `queue_wait_ns`
nor `service_ns`, and it buys a `kv_occupancy_at_admission` that is a reading rather than a
value copied from a heartbeat up to a second old. A stale per-request field is
indistinguishable from a fresh one once it is in the log.

The worker also **watches for slot leaks**. It is the only thing posting to its engine, so
`/slots` reporting more busy slots than it has in flight means the engine is holding a slot
for work nobody is waiting on; confirmed over three probes, the node reports `degraded`
(C-1's `engine_state`, used for what it is for). One campaign logged 304 slot launches
against 301 releases and ended with three of four slots stuck busy at 0% GPU. A node does
not fail at that point — it quietly loses a quarter of its capacity per leak and keeps
producing plausible numbers.

The adapter interface is one interface with one *pool* implementation:

```
complete(prompt_token_ids, output_len) -> ServiceResult
live_state()                           -> LiveState{inflight, slots_total, kv_frac, state}
```

Keeping it an interface even with one pool implementation is deliberate: the F-9b probe is
its second implementation, and writing that probe against the same interface is what makes
the engine-gap number a comparison rather than an anecdote.

Everything runtime-specific — `ignore_eos`, `n_predict`, `cache_prompt: false`, temperature
0 — is set per request in `build_request` and recorded in the manifest's node block (C-6),
so the settings that eliminate the confounds are auditable from the results.

### Capability throttling — F-9a

Per-node capability is **configuration, not hardware class**. Three knobs, all recorded in
`manifest.nodes[].engine_config`:

| Knob | llama.cpp flag | Effect |
|---|---|---|
| GPU offload fraction | `-ngl` | The primary throttle. `0` = CPU-only. |
| Thread count | `--threads` | Secondary; dominates once `-ngl` is 0. |
| Slot count | `--parallel` | The fixed batch capacity. Also `SimNode.batch_capacity`. |

This is what makes *R* tunable on physical hardware rather than reachable only in
simulation. Measured: `-ngl 20` gives 7.49 decode tok/s and `-ngl 0` gives 3.74 on the same
`Meta-Llama-3-8B-Instruct`, so **configuration alone reaches 2.00×**.

> **Distinct machines only.** Throttling must be applied to separate physical hosts. Two
> logical nodes on one box contend for PCIe, memory bandwidth and cache — which reintroduces
> as contention exactly the confound this removes. `launch.py` refuses to build such a pool,
> `preflight` reports it, and the manifest carries `validity.colocated_nodes`, which must be
> `0`. **Deployable *R* is therefore 1.00×** on one host: that is not a configuration
> problem, it is a second machine.

### The engine-gap measurement — F-9b — blocked

Run once, on the strongest node, at one operating point, against an identical replayed
trace: vLLM (AWQ, same model class) against llama.cpp on that same machine, reported as a
stated bound on external validity (threat R9). Marked `role: "engine_gap_probe"` in the
manifest, and the pipeline keeps it out of every policy comparison.

**It has not run.** `Meta-Llama-3-8B-Instruct` in 4-bit AWQ is roughly 5.7 GB of weights
before any KV cache, and the strongest node here is a GTX 1650 Ti with 4096 MiB. Turing is
supported; the memory is not there. Three ways out — run it at 3B and name the model in the
caption, run it on a machine with more VRAM, or report it as not measured — and it is a
scoping call for both of us rather than an engineering one.

### Prefill/decode split (F-18) — resolved, `full` on both backends

`/completion` returns a `timings` block with `prompt_ms` and `predicted_ms`, so the split
comes from **one code path for the whole pool** rather than two engine-specific ones that
agree by luck. Verified against both pinned builds — CUDA and Vulkan — so the `partial`
fallback is not needed by either, and `engine_version` continues to carry the backend as a
free string rather than C-6 needing a `backend` field.

**Half a split is not a split.** A `timings` block carrying `prompt_ms` but no
`predicted_ms` degrades to `partial` with both halves `None`, rather than reporting a
prefill and inferring decode: `service_ns - prefill_ns` is decode *plus in-engine queueing*,
and writing it into the `decode_ns` column would put a queueing artifact into the one number
the cost model is built on.

---

## `calibration/` — F-6, F-9a, F-13, F-15, MPR-1

```
campaign.py      grid pass + sustained segment + rolling snapshot series
cost_model.py    the C-3 lookup table, sigma, and the snapshot artifact
stationarity.py  tau, the variance envelope, and the floors that bound both
r_range.py       how far apart F-9a can actually push two node classes
admissible.py    the (prompt, output) range every node can serve — and the cliff outside it
```

**C-3 `form` is `lookup_table`**, a module constant rather than a runtime option. F-7
allowed that or a ≤6-parameter regression, and supporting both doubles the interpolation
logic on the other side of the seam for no research gain. The deciding reason: service time
against concurrency is flat while slots are free and then bends sharply once `--parallel`
saturates, and that knee — which is what F-4 is about — is exactly what a six-parameter form
cannot hold.

Failures are counted, never fitted. A timeout is a censored observation, and a cell mean
that averaged in the 60 s ceiling would report my own `--timeout` setting as the node's
speed. Those counts are what F-15's cliff is computed from, because a fitted table excludes
failures by construction and therefore cannot say where the cliff is.

### The one thing to understand about τ

`bursts_per_window` is `samples_per_window / batch_size`, and at saturation
`samples_per_window = (batch_size / service) × window`, so batch size cancels:

> **`bursts_per_window == window_s / service_s`** — the five-burst floor means
> **`window ≥ 5 × service`**, and τ is only visible if **τ > 5 × service**. Concurrency does
> not enter it.

A node that takes seconds per request cannot see a correlation time of seconds. That is
arithmetic, not impatience, and it is why the Week-2 cells could not have worked however
long they ran: they put the window *above* τ on every node. It is also the reason the CPU
class is the one that produced a number — 9.6 s per request against a 70 s drift — while the
1B class, at 0.81 s per request, shows nothing above 3.5 s.

`resolution_floor_s` is the larger of the median request duration and the window,
`cadence_limited` is true below five slot turnovers per window, and `tau_resolved` is false
unless τ clears both by 2×. A run that cannot support a τ says so and keeps its samples
rather than throwing and losing ten minutes of GPU time.

### *R* has two entry points, and they are not the same number

`synthesizable_range(classes)` divides `headline_tokens_per_s` — the median per-request
decode rate over the sustained segment — and drives `uv run r-range`.
`synthesizable(snapshots)` divides the fitted table's `tokens_per_s` at a cell every class
shares, and works from `contracts/cost_models/` so R is recomputable across the seam without
a copy of my run directories. On the committed 8B snapshots they read **2.00** and **2.07**.
Quote one, say which, and do not average them into a third number nothing measured.

`synthesizable` refuses when the classes share no calibrated cell, and that check is a
lesson rather than a nicety: decode rate falls as the KV context grows, so a class measured
at p256 looks slower than the same class at p64. When the fast node happened to be
calibrated at p256 and the slow one at p64, R read **1.66× instead of 2.00×** — a 20% error
that nothing downstream could have separated back out.

### The admissible set is drawn on p95, at every concurrency

A bucket whose mean fits under the ceiling but whose p95 does not will time out one request
in twenty, and those timeouts land in exactly the tail statistics the study is about. And a
bucket that fits at concurrency 1 but blows the ceiling at `--parallel 4` is not admissible,
because under load the scheduler will absolutely put four requests on that node — that is
what the load band *is*.

It also reports what it could not measure. A bucket is named by its **ceiling** and sampled
in its **interior**, so `prompt ≤ 128` resting on samples that reach 64 is a claim about 128
that nothing tested. The envelope still reports the ceiling — that is what C-2's header and
the trace generator consume — but `evidence` reports the measurement, and the gap is printed
rather than left to be noticed in Week 6.

---

## `harness/` — F-16, F-17, F-20, F-23

### `gen_trace.py`

```
config → SeedSequence(seed).spawn(3)
           ├── rng_arrival  → MMPP / Poisson offsets
           ├── rng_length   → bucket draws + priority draws
           └── rng_content  → per-request content_seed
        → sort by offset → assign req_ids → write header + records → sha256
```

Single-threaded, no I/O in the sampling loop, no wall-clock reads. **Separate streams** so
that changing the length distribution does not shift the arrival process underneath the
result. Regenerating with the same seed and parameters produces a **byte-identical file** —
a test, not an assumption. Float formatting is the usual culprit, so `arrival_offset_s` is
fixed at 4 decimal places.

### `replay.py`

Open-loop: the loop never waits on a response before firing the next request. Every prompt
is materialized before t0, because tokenising inside the timing loop is the most common
cause of send-lag drift under high λ. `send_lag_ms` is asserted per request, and any breach
in the measurement window marks the run **invalid in the manifest** rather than analysed — a
run that failed to generate its stated load is not a data point about scheduling.

`rate_scale` compresses the arrival timeline, which is how F-23's operating points are
reached **without three traces**: the request sequence is byte-identical across points and
only the arrival times change. Three separately seeded traces would vary the length draw and
the burst structure along with the rate, and a vehicle disagreement could then be a workload
difference rather than a simulator error.

> **Language note.** This is the one A-side component where Python may not hold. asyncio is
> adequate below roughly 50 req/s; above that, GIL contention shows up as send-lag
> violations — which this client reports honestly as an invalid run rather than hiding. The
> `perf` tests measure it, they pass on this host, and the seam (gRPC + JSONL) supports Go
> if it ever stops holding.

### `anchors.py` — F-23

One trace, replayed at 3+ compressions, each writing a C-6 manifest. Four points rather than
three, because "at least 3" is a floor and a sweep that loses a run to a send-lag violation
should still have an anchor set. It does **not** bring the pool up: an engine, a wrapper and
a scheduler have three lifetimes, and a campaign runner that owned all three would hide an
engine restart inside a Python traceback — `engine_restarts` is a validity field precisely
because it is a thing to be *counted*.

### `preflight.py` — before the pool spans two machines

Under F-11 the worker returns responses **directly to the client**, so the client's port has
to be reachable *inbound* from every worker host, and nothing about bringing the scheduler
up exercises that direction. Blocked, it is not loud: the dispatch succeeds, the worker
serves the request, the record says `timeout`, and a firewall is indistinguishable from a
saturated pool. Hence `--serve` on the client and `--probe` from each worker.

Two things it deliberately does not check. **Bandwidth**, because a five-node pool at the
measured load band runs at about **0.08 Mbit/s** — a `Dispatch` averages 754 bytes, a
`Deliver` is 33, a heartbeat 47 — and even a 30 ms hop is roughly 2% of the fastest
end-to-end latency measured here. And **clock synchronisation**, because no duration in this
study is computed by subtracting stamps taken on different hosts, and heartbeat gaps are
found through `Heartbeat.seq` rather than through time. NTP is not needed, and that is a
property the design paid for deliberately.

### Running it

```bash
uv sync --all-groups

# F-16 — a trace is a pure function of (config, seed). The printed sha256 is its identity.
uv run gen-trace configs/smoke.json -o traces/smoke.jsonl

# --model sets vocab_size and reserved_ids_excluded from the tokenizer table in
# gen_trace.MODELS. It picks which RUN SET a trace belongs to, not a per-node knob: the
# model is held constant inside a pool, exactly like the engine and the quant.
uv run gen-trace configs/smoke.json --model mistral-7b-v03 -o traces/smoke-mistral.jsonl

# One live node (F-9/F-10/F-11), against an already-running llama-server.
uv run worker --node-id gtx1650ti --engine http://127.0.0.1:18080 \
  --bind 0.0.0.0:50061 --scheduler <scheduler>:50051 --slots 4 \
  --engine-version b10569+p1+cuda13.2 --log-dir runs/worker

# The fake scheduler, from the repo root. Aditya's LiveSchedulerApp replaces it; --loopback
# answers from an analytic service-time model and is NOT a worker — its timings mean
# nothing and no calibration may be run against it.
uv run --project dataplane python fixtures/fake_scheduler/serve.py --worker 127.0.0.1:50061

# F-17 — open-loop replay. Exits non-zero and says why when the run is invalid.
uv run replay traces/smoke.jsonl \
  --scheduler 127.0.0.1:50051 --run-id run_0001 --sha256 <printed above> \
  --advertise <this host's LAN address> --nodes nodes.json

# The determinations, each writing the artifact it prints.
uv run calibrate  --config configs/calibration_1b_dense.json --out runs/calibration/llama32-1b
uv run r-range    runs/calibration/llama3-8b
uv run admissible runs/calibration/llama3-8b --out runs/admissible/llama3-8b.json
uv run anchors    configs/anchors_1b.json
uv run load-band  runs/anchors --out runs/anchors/load_band.json
uv run preflight  configs/preflight_lan.json --out runs/preflight.json
```

`--nodes` is the launcher's C-6 node block. Without it the client writes `validity.json`
alone instead of a manifest: under F-9a the per-node `engine_config` *is* the experimental
condition, and a harness that invents one emits a manifest that lies about what ran.

C-1 stubs are generated from `contracts/scheduling.proto` on first import of
`dataplane.proto` and are never committed — the proto is the contract, the stubs are a build
artifact, and a committed copy is a second thing that can disagree with it.

## `pipeline/` — F-19, §5.5

A **pure function** of `(manifest, three log files)` → one Parquet file. No network, no
engine, runnable on a laptop. That is what lets Aditya hand me a directory of *simulator*
logs and have the pipeline process them unchanged, so no engine import, gRPC stub or
hostname may leak in here.

`loadband.py` identifies the band where dispatch policy can still change p99. Onset is
**tail against tail** — a point's p99 against the reference point's p99, on an identical
trace — because comparing a tail to a median fires on the length spread alone: the lightest
run had p50 1367 ms and p99 4106 ms with no queue anywhere. Saturation is read twice, as a
latency trend *and* as a throughput shortfall against the offered rate, because the trend
test alone called a point stable that was visibly retiring 1.63 req/s against 1.80 offered.

`policy_separable` stays `false` in the output until the pool has two hosts. With one node
there is no placement to get wrong, so these runs bound the band from physics but cannot
demonstrate the thing the band is *defined* by.

## `figures/`

Consume Parquet only, never raw logs. Weeks 5–6, and the one part of my half not yet built.

**F-24: the figure scripts read `manifest.vehicle` and stamp simulated plots
automatically.** Labelling by hand fails exactly once, in the final report.

## Why I own the harness and the figures too

The trace generator, the prompt materializer and the replay client all have to agree exactly
on how a `content_seed` becomes a token sequence. Splitting the generator from the replayer
across two people creates a second drift surface for no benefit, and I already own the
tokenizer and vocabulary through the worker work.

Figures, because by Week 5 my engine work is frozen — feature freeze is end of Week 3 —
while Aditya is running sweeps, so analysis load naturally moves to me. The
hypothesis-specific figures in Weeks 5–6 are joint.
