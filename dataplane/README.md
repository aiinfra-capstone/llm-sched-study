# Data Plane & Measurement — Divyansh Shukla (A)

This half of the repository holds the worker wrapper (llama.cpp), capability throttling, the
calibration campaign, the non-stationarity measurement, the trace generator and replay
client, the admissible-set and load-band determinations, the log join pipeline, and the
figures. It is the measurement instrument; the control plane holds the scheduler, the
policies and the simulator that we measure *with* it.

**Requirements owned:** F-9, F-9a, F-9b, F-10, F-11 (worker side), F-13, F-15 – F-20, F-23
(the hardware half).
**MPR owned:** MPR-1 — τ and the variance envelope, hardware only.
**Load profile:** front-heavy, Weeks 1–3.

```bash
uv sync --all-groups
uv run pytest
```

The [root README](../README.md#terms-used-throughout) defines the serving and queueing
vocabulary the whole study uses — tokens, prefill and decode, KV cache, `-ngl`, slots,
percentiles, τ, and the heterogeneity ratio *R*. A handful of terms are specific to this
half and are worth having up front:

| Term | What it means here |
|---|---|
| **Run** | One replay of one trace against one pool, producing a manifest and three log files. The unit of measurement. |
| **Run set** | Many joined runs concatenated into one table. The unit of *analysis*: every hypothesis compares across runs, so no figure is drawn from a single one. |
| **Vehicle** | Whether a run came from real hardware or from the simulator. F-24 requires simulated figures to say so, and this field is what says it. |
| **C-3 snapshot** | One calibration measurement of one node class at one moment: a table of service times and throughputs. A *series* of them is what lets the control plane age an estimate realistically. |
| **Anchor** | A hardware run kept specifically so the simulator can be checked against it (F-23), at a known offered rate on a known trace. |
| **Admissible set** | The `(prompt, output)` length range every node in the pool can serve inside a stated timeout. Requests outside it fail categorically rather than slowly, so the study is defined over the range where latency is still a meaningful measurement. |
| **The cliff** | What happens outside that range — timeouts and out-of-memory rather than a long tail. Characterized separately (F-15) instead of being averaged into the tail statistics. |
| **Send lag** | How late the load generator was in firing a request relative to the trace's schedule. The check that the load offered was the load intended. |
| **Warmup** | The opening span of a run, discarded before analysis. Computed from the trace's intended offsets rather than wall-clock, so hardware and simulator discard exactly the same requests. |

## Where things stand

Everything below is built and has been run on hardware. These are the three results the rest
of the study leans on:

| | |
|---|---|
| **MPR-1** | τ = **69.5 s** on `cpu_ngl0_p4_q4km_llama3_8b`, *r²* = 0.989. Both GPU classes show no decay at any timescale their service time can resolve. A single calibrated tok/s figure on the CPU node understates its own standard error by **1.36×**. |
| **Synthesizable *R*** | **2.00×** configured, **1.00×** deployable — the second number is one physical host, and it is what MPR-2 waits on. |
| **Admissible set** | 8B pool: `prompt ≤ 128, output ≤ 64`, limited by the CPU class. 1B pool: `prompt ≤ 512, output ≤ 128`. |

Plus four F-23 validation anchors on one trace, and a load band of **1.03–1.30 req/s**.

The analysis layer behind those numbers is built too. `runset` assembles many joined runs
into the one frame the figures read, deriving *R* from each run's own cost-model snapshots
instead of taking it from the command line, and `figures` renders §5.5 with F-24's stamp
applied from the manifest. Both run on the committed anchor set today. The one figure that
cannot yet draw is `validation`, because F-23 needs the simulator's half.

We audited this half against the frozen specification rather than against the previous
week's notes, requirement by requirement. Four things had drifted and are now closed:
F-23's criterion is p50 **and** p95 against a **stated ±25% tolerance** derived from the
anchors' own resolution; §5.4's queue-wait, per-node-utilization and routing-error-rate
variables are computed rather than only carried in the schema; and MPR-2 has an estimator
that reports H1's 2×2 as a range across *R*, which is the form §7 asks for. One item is
raised rather than closed: C-5 carries no identity for the node that *served* a request,
only the one the scheduler chose, so per-node utilization is empty for runs driven by the
fixture scheduler. Adding `served_by` would fix it and is a joint decision, because the six
contract artifacts froze at the end of Week 1.

Then the seam opened. The control plane's C-3 parser was fixed on 2026-08-31, the 50
committed snapshots became readable, and the first thing we did with that was point
`costcheck` at the four anchors — which found that **the cost model those anchors were
served by missed its own hardware by a request-weighted 127%**. No simulator parameterised
from that table could have passed F-23, and the failure would have looked like the
simulator's. The 1B node class has been recalibrated on a grid that lands on the trace's own
lengths at every concurrency the pool can reach, and now reads **20.8%** — inside the F-23
tolerance. The story, including what it says about batching on this card, is under
[`costcheck.py`](#costcheckpy--does-the-cost-model-predict-its-own-hardware-f-7).

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
`src/dataplane`. We do not treat that as a vanity number. This half of the repo is an instrument, and a
line of the harness that no test executes is a line whose behaviour would be an assumption
underneath a reported measurement. If a new line is unreachable from a test, the honest
options are to test it or to delete it.

The generated protobuf stubs are excluded — they are protoc output, regenerated on import
and gitignored (§12.2). What they must actually *do* is asserted in `test_proto_stubs.py`.

| | |
|---|---|
| `test_forward_w[1-5]_*.py` | Written in Week 1 against work scheduled for later weeks, and skipped until that work lands; `-rs` prints every skip reason, so the skip list in a CI log is the remaining backlog of this half. **That list is now empty** — the Week-5 figure suite was the last to un-skip. These files are the specification of each week's deliverable, which is why they are written before the code and not changed to accommodate it. |
| `-m perf` | `test_replay_load.py` measures send-lag under sustained load. That is a property of the machine rather than of the code, so a shared runner cannot assert a threshold on it without either flaking or being set so loose it proves nothing. Run it on the load host: `uv run pytest -m perf -s`. |
| `-m integration` | Runs by default, but launches real subprocesses and binds real sockets, so it is slower than the rest. |

Because the coverage gate is on by default, running a subset needs `--no-cov`:

```bash
uv run pytest tests/test_manifest.py --no-cov
```

**The suite must not run on the load host while a campaign is measuring.** Doing so once
cost a four-point anchor set: 99 timeouts and a 3.7-second client send lag at an offered
rate well under capacity. The `perf` marker exists for that reason, but the rule covers the whole
suite during a run, not only the tests labelled as load measurements.

`test_golden_trace.py` is the one test that can notice the generator itself changing.
Everything else about determinism compares this build against itself — generate twice, get
the same bytes — which stays true even if the output is wrong, because both sides move
together. A reference computed once and written down is the only check on a claim about
*later*, and "the trace regenerates byte-for-byte from (config, seed)" is entirely a claim
about later. The gap surfaced under mutation testing: swapping `rng_length` and
`rng_content` in `generate` changed every byte of every trace the harness produces, and the
whole suite still passed.

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
has slots and queue them internally, and left alone that wait lands inside `service_ns`
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

### The engine-gap measurement — F-9b — scoped, and scheduled into Week 6

Run once, on the strongest node, at one operating point, against an identical replayed
trace: vLLM (AWQ, same model class) against llama.cpp on that same machine, reported as a
stated bound on external validity (threat R9). Marked `role: "engine_gap_probe"` in the
manifest, and the pipeline keeps it out of every policy comparison — `join.py` drops probe
rows *before* the join rather than filtering them in each figure, so it can never be averaged
into a policy comparison by accident.

**The model question is settled.** `Meta-Llama-3-8B-Instruct` in 4-bit AWQ is roughly 5.7 GB
of weights before any KV cache and the strongest node here is a GTX 1650 Ti with 4096 MiB.
Turing is supported; the memory is not. We run it at `Llama-3.2-3B-Instruct` AWQ instead,
about 2.2 GB, with the model substitution captioned everywhere the number appears.

**It runs in Week 6, and the deferral is a decision rather than a slip.** F-9b bounds a
threat; it feeds no hypothesis, no figure waits on it, and nothing downstream changes shape
depending on what it says. Weeks 4 and 5 are the ones with a dependency chain — parameterise
the simulator, run the sweeps — and both want the same single GPU that F-9b would occupy. So
it sits next to the threats-to-validity section that consumes it. The cost of being wrong
about that is Week 6 having the least slack of any week; the fallback is reporting R9 as an
argument rather than a number, which is worse and is the thing to avoid.

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
that averaged in the 60 s ceiling would report the harness's own `--timeout` setting as the node's
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
a copy of the run directories. On the committed 8B snapshots they read **2.00** and **2.07**.
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

One thing it deliberately does not check: **bandwidth**, because a five-node pool at the
measured load band runs at about **0.08 Mbit/s** — a `Dispatch` averages 754 bytes, a
`Deliver` is 33, a heartbeat 47 — and even a 30 ms hop is roughly 2% of the fastest
end-to-end latency measured here.

It does now read the local **clock discipline**, and reports it without failing on it. A
preflight sees one machine, so an undisciplined clock here is a reason to fix this host
before a two-machine run, not evidence about the pool; failing the check would conflate
"the LAN is misconfigured" with "chronyd is not installed on the box we ran this from". The
teeth live where a reading of every host exists, in `clocksync --combine` and again in the
join summary.

### `clocksync.py`

Measures how far apart the machines' clocks are, at LAN setup, and folds the readings into
the C-6 `clock_sync` block. Run `--measure` on each host and `--combine` once.

The rule that no duration crosses a host boundary has not changed, so the **offset** we
record corrects nothing: it cancels exactly in `transport_residual_ms`, which is a
difference of single-host durations. We record it as evidence that the clocks were being
disciplined. It could not correct the two on-wire timestamps either, because
`client_send_mono_ns` and `worker_mono_ns` are `CLOCK_MONOTONIC` reads whose zero is each
machine's boot, and an NTP offset says nothing about the gap between two boot times.

What does touch a number is **rate**. A worker clock ticking r ppm fast inflates every
worker-local duration by r ppm relative to the client's `e2e_ms`, multiplicatively, so it
does not cancel. `join` divides it out for each node whose host was measured, leaves the
scheduler alone (C-6 does not record its host, and a ppm correction on tens of microseconds
is picoseconds), and prints the size of the correction against the longest service time in
the set. With no `clock_sync` block nothing is scaled at all, so every run joined before
this existed joins to the same numbers.

A host with no time daemon comes back marked unsynchronised, never as offset zero. Zero
would read as agreement, and agreement is the one claim nobody made.

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

# The fixture scheduler, from the repo root. The control plane's LiveSchedulerApp replaces
# it; --loopback answers from an analytic service-time model and is NOT a worker — its
# timings mean nothing and no calibration may be run against it.
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

# Analysis. Neither needs a node up: both are pure functions of the files a run left behind.
uv run runset     runs/anchors --out runs/anchors/runset.parquet
uv run figures    runs/anchors/runset.parquet --out figures/
```

`--nodes` is the launcher's C-6 node block. Without it the client writes `validity.json`
alone instead of a manifest: under F-9a the per-node `engine_config` *is* the experimental
condition, and a harness that invents one emits a manifest that lies about what ran.

C-1 stubs are generated from `contracts/scheduling.proto` on first import of
`dataplane.proto` and are never committed — the proto is the contract, the stubs are a build
artifact, and a committed copy is a second thing that can disagree with it.

## `pipeline/` — F-19, §5.5

A **pure function** of `(manifest, three log files)` → one Parquet file. No network, no
engine, runnable on a laptop. That is what lets the control plane hand over a directory of *simulator*
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

### `runset.py` — the analysis unit is a set, not a run

`join.py` turns one run directory into one C-5 record set. Nothing this study asks is answered
by a single run — H1 compares four policies, H2 sweeps *R*, H3 sweeps staleness — so what a
figure opens is a concatenation of joined runs. Three things only become visible at that
level, and all three are why this file exists rather than a shell loop over `pipeline`:

***R* was being typed in by hand.** `pipeline --r 2.0` is defensible for one run and is a
loaded gun across a twenty-run sweep: *R* is H2's independent variable, and a mistyped
value does not fail, it relabels a point on the x-axis. `deployed_r()` derives it from the
run's own manifest instead — `cost_model_snapshots` already names which C-3 snapshot each
node was serving under, so the ratio is recomputed from those snapshots. The division is
delegated to `r_range.synthesizable`, which refuses to compare throughputs measured at
different cells; a second implementation of that division would be a second chance to make
the p256-against-p64 mistake that cost this study 20% of its ratio once already.

A pool of one node class reads **exactly 1.0** — not unknown, not NaN. That is the honest
number for a single-host pool and it is why the deployable *R* here is 1.00x while the
synthesizable range tops out at 2.00x. On the four committed anchor runs it derives 1.00x
without being told.

**`vehicle` never reached the frame.** F-24 is enforced by the figure reading the
manifest, and a figure that reads a *set* has no single manifest left to read. So the
label rides in the rows. It is attached here rather than in `join.py` because C-5's column
list is frozen and Aditya's simulator emits against it — `vehicle` is a property of how a
set was assembled, not a field of a joined record.

**One bad run must not cost the other nineteen.** `join` refuses an invalid run loudly,
which is right when someone asked for that one run. Assembling a directory, the same
strictness stops the whole analysis over one drifted run. So a refusal becomes an
*exclusion carrying its reason*, printed before anything is plotted. Only the deliberate
refusals are caught — an invalid manifest, a missing or mismatched trace, an uncommitted
cost model. Anything else propagates, because a set that silently drops a run on a
`KeyError` silently drops a run on a bug in this code.

```
uv run runset runs/anchors --out runs/anchors/runset.parquet
# 4 run(s), 800 rows / vehicle(s): hardware / R: 1.00x
# and it says so: every run is R = 1.00x, so this set cannot speak to H2
```

### `costcheck.py` — does the cost model predict its own hardware? (F-7)

F-23 asks whether the simulator reproduces hardware within a stated tolerance. When that
comparison fails there are two suspects and they sit on opposite sides of the seam: the
simulator's queueing and policy logic, or the C-3 table it was parameterised from. Nothing
in the F-23 figure separates them, so a failure there starts an argument rather than a
diagnosis.

This settles the upstream half without a simulator. It takes a run that already happened,
takes the snapshot the manifest says each node was deployed under, and asks what
`predict_service_ms` would have said about every request that ran. If the cost model does
not predict the hardware it was measured on, no DES parameterised from it can pass F-23, and
the DES is not where to look.

The first thing it was pointed at was the four committed anchors, and the answer was a
request-weighted **127% error**, eleven of twelve cells outside tolerance, worst 353%. Two
causes, neither of them a bug:

**The grid was calibrated at lengths the study does not run.** The Week-2 config sampled
prompt 64 and output 32; the anchor trace runs 128–512 and 64–128. Both fall inside the same
buckets, so nothing was out of range and nothing raised — but decode time is linear in output
tokens, so a cell measured at 32 tokens under-predicts a request of 64 by roughly half.
`campaign.py`'s own docstring warns about this in as many words.

**The concurrency axis had holes where the anchors live.** The grid measured 1 and 4 slots.
The anchors spend 28% of the quiet point and 18% of the light point at 2 slots, and
nearest-measured-concurrency then prices a two-slot request at the one-slot rate, because 2
is nearer to 1 than to 4.

Recalibrating on a grid whose representative lengths are the trace's own, at every
concurrency the pool can reach, is what closed it: **127% → 27.5% → 20.8%**, the last step
coming from a prompt-bucket edge added at 256 so one bucket no longer averaged a fourfold
prefill range. On medians the error is 9.9%, and the four-slot cells — most of the requests,
and where the anchors sit under load — land within 2%. That is a limitation as well as a fix:
the model is calibrated for the traces this study replays, and a length between two bucket
representatives would be priced at its bucket's, not its own.

**It reports the mean and the median side by side, and the gap is information.** The
prediction is a mean measured with concurrency *held fixed*; a live cell is a mean over
requests labelled by concurrency *at admission*, and a request admitted alone onto a busy
node does not stay alone. Those keep the low-concurrency label and carry a high-concurrency
service time, landing entirely in the upper tail — one cell's median sits 12% above
prediction while its mean sits 70% above. The mean stays the verdict, because a mean is what
the model predicts; the median is printed beside it so the difference between "the model is
wrong" and "the label is a proxy" is visible rather than arguable.

```
uv run costcheck runs/anchors
uv run costcheck runs/anchors --snapshot <a candidate C-3 file>   # would recalibrating help?
```

`--snapshot` scores one candidate table against runs that predate it. The runs cannot be
replayed, so pricing the old run's requests with the new model is the only honest way to ask
whether a recalibration was worth it.

## `figures/` — F-19's last stage, F-24's enforcement point

Consume Parquet only, never raw logs. Not tidiness: every exclusion the analysis rests on
— warmup discarded from the trace offset rather than wall-clock, failures kept as rows but
out of the latency statistics, the engine-gap probe dropped as a non-member — is applied
once, in the pipeline. A figure script reaching back to the logs would be a second place
those rules live, which over six weeks means a second place they diverge.

**Only simulated figures carry the stamp.** This was written the other way first — stamp
everything, so that a missing stamp reads as a bug rather than as a claim of hardware
provenance. The Week-1 forward test says otherwise, and it is right: a label on every
figure is a label nobody reads, and F-24 asks for a mark that means something. So the
stamp goes on when a simulator was involved, stays off when one was not, mixed provenance
stamps as simulated, and a manifest with no `vehicle` is **refused** rather than defaulted
— defaulting to `hardware` is the one failure that puts a simulated number into the report
wearing a measurement's clothes.

The hypothesis estimators live here as arithmetic, testable without a plot attached,
because in each case the sign convention or the axis choice *is* the claim:

| | what it computes | the trap it exists to avoid |
|---|---|---|
| `h1_interaction` | `(WJSQ − JSQ) − (StaticWeighted − RoundRobin)` | both brackets are negative when calibration helps, so reversing the subtraction gives the same magnitude and the opposite headline. A missing cell is refused — three policies are a ranking, not a 2×2. |
| `h2_advantage_curve` | best hardware-aware against best hardware-blind, per *R* | a sweep at one *R* is refused: one point cannot be non-monotonic, and reporting it is how MPR-2's *range* becomes a figure. An *R* with only one side of the 2×2 is undefined, not zero. |
| `h3_axis` | `staleness_s / τ`, as a named Series | raw seconds would make the result a property of the heartbeat interval that happened to be configured rather than of the process. Returning only the axis stops a caller plotting `staleness_s` by habit and believing they divided. |
| `mpr2_interaction_range` | H1's 2×2 evaluated at every *R*, returned as an interval | §7 words MPR-2 as the decomposition "across the synthesized heterogeneity range … reported as a range rather than a single figure". A single interaction term is the ingredient, not the deliverable. `sign_consistent` names the case where the interval straddles zero, which is a publishable negative result rather than a mean that happens to sit near zero. |

§5.4 names four dependent variables, not one, and until the audit only the first was
computed here. `by_offered_load` now returns end-to-end p50/p95/p99, queue-wait p50/p95 and
the routing-error rate per run, and `per_node_utilization` is a separate function.

Two details in those are load-bearing. The routing-error rate returns `None` rather than
`0.0` when no request in a set carries a scheduler decision: a fixture-driven run observed
nothing about routing, and reporting zero would claim routing was perfect — the opposite
claim, so the two must not share a value. And **"materially sooner"** needs a threshold or
every floating-point difference counts as an error; a dispatch is counted when the best
admissible alternative was estimated to save at least 10% of the request's actual service
time, relative rather than absolute because 200 ms decides a 1 s request and is noise in a
20 s one.

Queue wait turned out to locate the queueing onset more sharply than the tail test does.
Median queue wait is **0.01 ms** at the quiet and light anchors, **731 ms** at mid and
**11.8 s** at heavy, and the load band's upper edge sits exactly at that transition — a
third independent confirmation alongside the latency-drift and throughput-shortfall tests.

`latency_vs_load` and `throughput_vs_load` read §5.5 off a run set, and their percentiles
agree with `load_band.json` **exactly** at all four anchor points. A test pins the two
estimators equal, because two definitions of p95 in one repository disagree by a few
milliseconds everywhere and the disagreement is invisible and permanent. That test earned
its keep immediately: `percentile` had been written taking a percent while `loadband` takes
a fraction, which fails silently toward returning the minimum.

`validation` is the **Week-4 joint gate** — hardware against simulator on an identical
trace, F-23. The requirement is more specific than a plot: agreement in p50 **and** p95,
within a *stated* tolerance, across at least three operating points, with the tolerance and
the observed error both reported. `validation_error` returns all of that per matched point
and the figure writes the verdict into its own title, so a reader does not have to consult
a separate table to see whether the gate passed.

**The tolerance is ±25%, set by the anchors rather than chosen.** Bootstrapping each
committed anchor run — resampling its own requests 4,000 times to see how far its
percentiles move under a different draw — gives 95% intervals on its own p50 and p95 of
**±25.9%** and **±24.7%**, at 180–196 measured requests. That is the resolution of the
instrument the simulator is being compared against, so anything tighter would not be a
stricter test but an unfalsifiable one. Improving it is a matter of run length rather than
analysis: halving the interval takes roughly four times the requests per anchor. Each
reported error carries its anchor's interval beside it, so the limiting side of the
comparison stays visible.

The figure refuses rather than drawing half of itself. One vehicle validates nothing; two
vehicles on different traces produce a gap that is part simulator error and part workload
difference with no way to separate them; and fewer than three matched operating points can
be fitted exactly by a simulator with two free parameters, which is the failure F-23 exists
to prevent. It is skipped by the default render on a hardware-only set, so the normal case
does not fail while the simulator's half is still to come.

```
uv run figures runs/anchors/runset.parquet --out figures/
```

## Why the harness and the figures sit on this side of the seam

The trace generator, the prompt materializer and the replay client all have to agree exactly
on how a `content_seed` becomes a token sequence. Splitting the generator from the replayer
across two people creates a second drift surface for no benefit, and this half already owns the
tokenizer and vocabulary through the worker work.

Figures, because by Week 5 the engine work is frozen — feature freeze is end of Week 3 —
while the control plane is running sweeps, so analysis load naturally moves here. The
hypothesis-specific figures in Weeks 5–6 are joint.
