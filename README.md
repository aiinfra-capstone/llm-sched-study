# Scheduling LLM Inference Under Uncalibrated Heterogeneity

A six-week measurement study of how to route language model inference across a pool of
mismatched consumer machines. The scheduler here is an **instrument**, not a product. We
built it to produce measurements, at the lowest fidelity that still supports the claims.

---

## The problem

Four machines of the kind a small lab already owns. A desktop with a discrete GPU, a laptop
with a weaker one, an older GPU box, a CPU-only machine. All serving one model together.

A request arrives. Where does it go?

Two things make this harder than ordinary load balancing.

**The machines differ enormously.** Datacenter hardware generations differ by 2 to 5×.
Consumer machines differ by 10 to 100×. A request that takes 2 seconds on the fast node can
take 3 minutes on the slow one, or blow past any sane timeout entirely. That is a
categorical failure, not a latency tail.

**Nobody knows how fast each node really is.** It can be measured, but the measurement
decays. Serving throughput is non-stationary under sustained load: it drifts with thermal
state, memory pressure, and whatever else happens to be batched alongside. The scheduler is
always routing on an estimate that is already slightly wrong.

The obvious fix is calibration. Measure each node's tokens per second, weight the routing by
it. This study asks whether that is worth doing at all.

> **Research question.** Given a router that already sees live queue depth, how much does
> hardware calibration add, and what does that depend on: how wrong the estimate is, how
> loaded the pool is, how variable the request sizes are, and how stale the queue signal is?

The wording is locked, with the reasoning and the deviations from the frozen specification,
in [`docs/research-plan.md`](docs/research-plan.md).

There is a real reason to doubt it. **Queue depth is already a proxy for speed.** Slow nodes
accumulate queue, so a scheduler that simply joins the shortest queue is implicitly
hardware-aware. Calibration may be paying for something the queue reports for free.

## What we are testing

Each claim is falsifiable, and each has a negative result worth reporting.

| | Claim | Why it matters |
|---|---|---|
| **H1** | Calibration's benefit to a queue-aware router is smaller than its benefit to a queue-blind one. The primary statistic is the interaction on log mean latency, `log(WJSQ/JSQ) − log(StaticWeighted/RoundRobin)`, at steady-state points; positive means calibration buys a smaller fraction once the policy sees queue depth. | If it holds, the honest headline contradicts the intuition that motivates hardware-aware schedulers. If it fails, calibration carries independent signal and we have to say where it comes from. |
| **H2** | The advantage of hardware-aware routing is **non-monotonic** in the heterogeneity ratio *R*, converging on `Threshold(T)`. Simulator only, and gated on a simulator validated against the held-out hardware runs. | As *R* grows the best policy converges to thresholding, which a one-line static rule achieves without any calibration machinery. |
| **Elevation** | The value of calibration moves with the workload's prompt-to-output mix, in the direction set by which phase the machines differ on. | It is what makes this a question about language-model serving rather than a queueing question with language models attached. |
| **H3** | ~~Routing quality degrades as estimate age approaches τ.~~ **Out of this paper.** The simulator has no drift process, capability never ages, and τ is unresolved on every class we own. Section 5 of the research plan says what would bring it back. | The absence is itself reportable: on this hardware, drift is below what five service times can resolve. |

The policies form a **2×2 factorial, not a ladder.** A ladder confounds hardware-knowledge
with queue-knowledge and cannot decompose the gain:

| | Queue-blind | Queue-aware |
|---|---|---|
| **Hardware-blind** | `RoundRobin` | `JSQ` |
| **Hardware-aware** | `StaticWeighted` | `WJSQ` |

Plus four controls that turn "does calibration help" into "how much of it is worth knowing":
`Threshold(T)`, round-robin over nodes above a calibrated cutoff; `JSQFastFirst`, which
breaks JSQ's ties toward the fastest node and so carries the ranking without the magnitude;
`StaticWeightedWRR`, the deterministic form of weighted routing; and `ECT`, which prices each
request from the full cost model.

## The vocabulary that matters

| Term | What it means here |
|---|---|
| **Prefill / decode** | The two phases of one request. Prefill reads the whole prompt at once, decode emits output tokens one at a time. They scale differently across machines and across batch sizes, so we measure them separately rather than assuming which resource binds either one. |
| **Heterogeneity ratio *R*** | Fastest node's throughput over slowest, within one pool. The study's primary independent variable. |
| **`-ngl` / `--parallel`** | How many model layers sit on the GPU, and how many requests the engine serves at once. Lowering `-ngl` genuinely slows a node down, which is how we manufacture heterogeneity on hardware we already own. |
| **Autocorrelation time τ** | How long a node's throughput stays correlated with itself. Informally, how long a speed measurement stays useful. |
| **Open-loop load** | The generator sends on a fixed schedule and never waits for a response. Closed-loop would let a slow pool throttle its own workload, silently changing the experiment. |
| **Node class** | One machine under one configuration. The same box at two `-ngl` settings is two node classes, calibrated separately. |
| **Cost model** | A calibrated table predicting service time from prompt length, output length, and concurrency. What a hardware-aware policy consults. |

---

## How it is measured

![Deployment view](assets/fig03_deployment.png)

Machines on a LAN, **all running the same inference runtime**. Holding the engine and the
quantization constant is what makes *R* a property of hardware and configuration alone. A
mixed-engine pool would fold an engine effect into *R*, and none of the three hypotheses can
pull it back out.

We run on two vehicles, and the split between them is the core methodological commitment.

**Hardware** measures what the simulator must not assume: cost-model parameters, τ and the
variance envelope, validation anchors, and the range of *R* real machines can span.

**A discrete-event simulator** provides breadth: pools of one fast node and *k* slow ones,
*R* synthesised beyond what we own, and controlled staleness on the queue signal. Two laptops
reach none of that, and every synthesised figure is labelled as synthesised.

The simulator runs the *same policy code* as the live scheduler, not a reimplementation. It
is parameterized from measured hardware and validated against live runs on identical
replayed traces before any simulated result is believed. Every simulated figure is stamped.

### Three disciplines

Each one guards against something that quietly invalidates measurement studies.

**No duration crosses a host boundary.** Every duration comes from one machine's monotonic
clock. What is left over is reported as a single honest residual, never decomposed into
invented stages. From the two-machine setup onward the assumption behind this is measured
rather than asserted: `clocksync` records each host's clock discipline into the manifest,
and the pipeline divides out the only thing a differing clock can actually corrupt, which is
rate, not offset.

**Open-loop load generation.** The client never waits for a response before firing the next
request, and asserts its own send-lag per request. A run whose timing drifted is marked
invalid rather than analysed.

**Byte-identical trace regeneration.** A trace is reproducible from `(config, seed)` and
identified by its SHA-256, so the same workload replays across every policy and across the
hardware/simulator boundary.

### The pinned engine

*R* is a hardware property only if the engine underneath does not move.

| | |
|---|---|
| Source | [`ggml-org/llama.cpp`](https://github.com/ggml-org/llama.cpp) tag `b10569`, commit `5a32f7b`, plus one patch in [`patches/`](patches) |
| Model | `Llama-3.2-1B-Instruct` GGUF, `Q4_K_M`, the same bytes on every node |
| Backends | CUDA and Vulkan, both built from that one commit |
| Recorded as | `engine_version: b10569+p1+cuda13.2` |

The patch stops `llama-server` throwing a 500 on the `/completion` path when generation ends
mid-character, which it did on roughly 1 request in 100. `llama-server --version` cannot
report a patched tree, which is exactly why the manifest carries `+p1` and the SHA-256 of
the engine's shared libraries. A pin a running node cannot prove is not a pin.

---

## What we have found

**No node class has a resolved τ yet.** On the CPU 8B class the point estimate is τ = 69.5 s
(*r²* = 0.989, integrated 89.0 s, 1/e crossing 72.1 s), but the calibration's own record
marks it `tau_resolved: false`: 31 windows of 48 s, a segment only 21 τ long, and a fit that
rests on two or three lags. `tools/tau_interval.py` puts a block-bootstrap interval on it:
lag-1 correlation 0.48 [0.03, 0.60], τ 31 to 91 s, and censored at one window in 66% of
draws. Removing a linear trend (throughput fell 2.3% across the segment) moves the point to
61.8 s. So the number is an estimate of an upper-bound kind, not a measurement, and the
80-minute segment in `calibration_1b_cpu.json` is how we intend to resolve it. On both GPU
classes τ is censored at the 5 s floor. The instrument limit is still the reusable half: a
node whose τ is shorter than about five service times cannot show its own drift.

**Batching buys some throughput, and a different amount on each card.** An earlier version
of this section said aggregate decode throughput was constant in concurrency on the 1650 Ti.
The committed cost models used by every hardware run say otherwise. On the 1650 Ti snapshot
per-request decode goes from about 140 to 147 tok/s at one slot to 67 to 70 at four in five
of six cells, which is about 1.9x aggregate (1.4x in the one cell measured with 120 samples).
On the RTX 3050 it goes from about 170 to 120, which is about 2.85x. The two cards differ
in how much batching helps, so their gap at four slots is wider than at one: R on service
time for the anchor trace's mix is 1.79 at one slot and 3.0 at four
(`tools/cell_intervals.py`). The capability the policies route on comes from the one-slot
cell, which is the least heterogeneous number the snapshots contain.

**Prefill is flat in concurrency, but only under arrivals that are not synchronised.** The
anchors measure it at 179 / 267 / 280 / 189 ms across batch sizes 1 to 4 for prompts under
128 tokens, and 702 / 751 / 756 / 707 ms for 257 to 512, while decode over the same rows
roughly doubles. The reason is that a Poisson process almost never starts two prompts at
once, so a new request's prefill overlaps its neighbours' decode rather than their prefill.
Our own calibration grid does synchronise them, because it holds *c* requests in flight by
firing them together, and there the same 128-token prompt goes 174 ms at one slot to 727 ms
at four. Both numbers are real and they answer different questions. It matters because
`service_ms_mean` at *c* > 1 inherits the synchronised figure, so the cost model carries
contention that a trace-driven run never produces. We have recorded that caveat on the C-3
field rather than refitting around it. It is a fact about attribution rather than about
total cost: reconstructing service time from the split comes out 20 to 40% below the anchors,
where the table as it stands is accurate to within 1.7 to 7.4% at four slots.

**The deployed cost model missed its own hardware by 127%.** We found it with `costcheck`, a
diagnostic that asks whether the model a run was served by would have predicted that run.
Two causes, both about grid placement rather than model form: bucket representatives were
calibrated at prompt 64 / output 32 while the traces ran 128 to 512 / 64 to 128, and the
concurrency grid held only {1, 4} while the anchors spent a quarter of their time at 2 slots.
Recalibrating onto the trace's own lengths brought it to **21.3%** request-weighted and
**11.8%** on medians, inside the ±25% tolerance the validation criterion asserts. What
remains is concentrated rather than spread: the model is accurate at four slots (2 to 6%
low) and under-predicts by 50 to 63% at one and two, where the mean is dragged by a right
tail a table of means cannot carry. That is the shape of the error, and it is worth saying
because a scale correction cannot fix a shape.

**The heterogeneity ratio is not one number.** Prefill is compute-bound and decode is
memory-bandwidth-bound, and a machine does not lose those two capabilities at the same rate,
so how heterogeneous a pool looks depends on the shape of the request. On the only
same-model pair we currently own, CPU against a partially offloaded GPU, R is 1.75x on
service time but 1.46x on prefill and 1.83x on decode, and the gap between the two phases
widens with concurrency. That is what makes this a question about language-model serving
rather than a queueing question with language models attached, and chasing it is the whole
of the phase question ([`docs/research-plan.md`](docs/research-plan.md), K1). The magnitude
here is small because that GPU node is a
partial offload and because both classes were calibrated at a single grid cell, so the
prompt-to-output ratio never varied. Varying it is the experiment.

**The admissible envelope is `prompt ≤ 512, output ≤ 128`**, with the load band at
**1.03 to 1.30 req/s** on a one-node pool.

**The simulator agrees with the hardware at the single-node anchors, in sample.** F-23 asks
for three or more operating points inside ±25% on p50 and p95. The p50 error is −16.5% to
−19.4% at the three steady-state anchors (0.72 to 1.305 req/s) and −7.8% at 1.98 req/s. That
last point is transient queue filling, so its percentiles are not quoted beside the others,
and the "worst of eight comparisons, 21.7%" figure we used to give included its p95. Two
limits apply to all of it. The fixes below were chosen while watching this same error, so
the check is in sample. And it is a check on absolute latency per policy, which a simulator
can pass while getting a 10 to 18% difference between two policies wrong; the two-node
validation is being redefined to test those contrasts on the shape runs, which played no
part in building the simulator.
Getting there took recollecting the anchors against the recalibrated cost model rather than
the superseded one they were first served by: parameterised from the old table the same
simulator ran −58.7% to −93.8%, which was a statement about our calibration grid and not
about the simulator.

Nothing in that figure is fitted to the tolerance. An earlier version of the simulator
reached it partly through a hardcoded 5% inflation of every service time, which we removed.
What stands in its place is measured: the client pays 5.86 ± 2.66 ms per request that the
cost model cannot contain, because the cost model is fitted on the engine's own span while
the client also pays for the gRPC hop, the decision, the dispatch, and the direct return.
That number is flat from quiet to heavy, which is why it is added in milliseconds rather
than applied as a percentage, and it travels on the run manifest so a different environment
supplies its own. The remaining error is uniformly negative, and we know where it lives: the
cost model carries one global σ = 0.1225, and at fixed concurrency the engine turns out to
be close to deterministic: σ = 0.003 at one slot, and 0.0035 across the second half of a
632-sample sustained segment at four, with p95/p50 of 1.01. The spread the anchors show is
concurrency changing *during* a request rather than service-time noise, which the simulator
already models as a queueing effect. So the residual is a queueing question and not a
service-model one.

The first real heterogeneous pair (GTX 1650 Ti and RTX 3050 laptops) is measured, audited and
re-derived. What those runs do and do not support, and what we withdrew, is in
[`docs/results.md`](docs/results.md).

---

## Setting up the experiment

Six stages, in order. Each one refuses to proceed on something the previous leaves broken,
which is deliberate: every failure below is silent if it is discovered during a run instead
of before one.

Paths are relative to the repo root for `tools/`, and to `dataplane/` for everything run
through `uv`.

### 1. Machines

Every machine gets surveyed before it is assigned anything. The survey reads only. It needs
no root, installs nothing, and runs on a bare install.

```bash
./tools/survey.sh --json survey-$(hostname).json
```

It answers the three questions that decide a machine's role. Which models fit, computed at
the study's actual shape rather than from weights alone. What `-ngl` and `--threads` range
the machine can produce, which is where *R* comes from. And whether it has the RAM to host
the scheduler and the client instead of an engine.

Roles follow from the answers, not from preference:

| Role | What it runs | Needs |
|---|---|---|
| **Pool node** | one engine, one worker wrapper | a GPU or enough cores, and room for the model |
| **Harness host** | the scheduler and the replay client | about 1.5 GiB of RAM, and no engine |

Nothing is ever both. The client has to fire on a fixed schedule, and an engine will take
the CPU it needs. A late send marks the whole run invalid.

### 2. Nodes

Same engine, same weights, same everything, on every pool node.

```bash
./tools/pool-install.sh --role node --backend cuda --reference 10.42.0.1
./tools/pool-install.sh --role harness --reference 10.42.0.1
```

It prints a plan and waits, because two of these machines belong to other people. It detects
the CUDA architecture from the card rather than assuming ours, refuses to build if the patch
does not apply cleanly, and ends by printing what the node can *prove* about itself: the
build number and commit the server reports, the SHA-256 of the engine's shared libraries,
and the hash of the weights. Those three go into the manifest.

### 3. LAN

The network this study needs is not the one people assume. The worker returns the response
**directly to the client**, so traffic has to flow worker to client, and that is the
direction consumer wireless breaks.

**Not a phone hotspot.** Most of them isolate connected devices from each other, which kills
the reply path silently: the dispatch succeeds, the worker serves the request, the client
records a timeout, and the run reads as a saturated pool.

```bash
./tools/lan-up.sh --ap   --ssid poolnet --pass '<secret>'          # harness host
./tools/lan-up.sh --join --ssid poolnet --pass '<secret>' --ip 10.42.0.11
./tools/lan-up.sh --open node                                      # tcp/50061
./tools/lan-up.sh --verify 10.42.0.1:50051 10.42.0.1:50071
```

Addresses are static because the manifest records where each node was, and an address that
changes between runs makes two runs incomparable. The engine's own port is never opened.

Then the two checks that test what a TCP connect cannot:

```bash
uv run preflight --serve 0.0.0.0:50071        # harness host
uv run preflight --probe <harness>:50071      # from each pool node
uv run clocksync --measure --out clocks/$(hostname).json
uv run clocksync --combine clocks/*.json --reference <harness> --out clock_sync.json
```

`preflight` speaks gRPC, so a port that accepts TCP but not HTTP/2 fails there rather than at
the first request of a run. `clocksync` records each host's clock discipline into the
manifest. The offset it records is subtracted from nothing. The number that matters is the
rate, because Linux slews the monotonic clock along with the system clock, and that is what
makes two machines' durations comparable at all.

### 4. Workers

The engine listens on loopback only. The worker wrapper is the only thing that talks to it,
and the only thing exposed to the LAN.

```bash
~/opt/llama.cpp/b10569-cuda/bin/llama-server \
  --host 127.0.0.1 --port 18080 \
  -m ~/models/gguf/Llama-3.2-1B-Instruct-Q4_K_M.gguf \
  -ngl 99 --threads 6 --parallel 4 -c 55296 --cache-ram 0

uv run worker --node-id gtx1650ti --engine http://127.0.0.1:18080 \
  --bind 0.0.0.0:50061 --scheduler 10.42.0.1:50051 --slots 4 \
  --engine-version b10569+p1+cuda13.2 --log-dir runs/exp/<run_id>
```

`--cache-ram 0` turns off llama-server's host-side prompt cache, which is 8 GiB by default
and filled host memory during the first pair's shape campaigns. `tools/hw_runs.py` reads each
engine's command line before every run, records it in the manifest, and warns when this flag
is missing.

`-c 55296` is 13824 tokens per slot. Without it llama-server sizes the context to fit the
card's VRAM, so the same command gave the 4 GB 1650 Ti 13824 tokens per slot and the 6 GB
3050 30720, an engine difference that no config recorded.

`--scheduler` is not optional any more. The worker heartbeats to it and reports every
completion to it, and the scheduler now uses both: completions arrive on their own RPC
rather than at the next heartbeat tick, which is the second staleness source C-1's first
invariant exists to keep out of H3.

**For a `tools/hw_runs.py` campaign, every worker logs to `~/Documents/capstone/runs/worker_logs`**
(`--log-dir ~/Documents/capstone/runs/worker_logs`), on whichever host it runs. That is
where `hw_mpr2_lan_3050.json` looks for each node's log, and the driver copies it into the run
directory after every run.

For a run by hand, all three processes must log into the same run directory. `replay --out runs/exp`
creates `runs/exp/<run_id>/` and writes the client log there, so the worker and the
scheduler have to be pointed at that same path. Splitting them is not loud: the join finds
the manifest, finds no client log, and writes an empty frame rather than refusing.

**This is where *R* is set.** A second node running the same engine at a lower `-ngl` is
genuinely slower, and that is how we produce a second *R* point without a second machine.
The settings are part of the experimental condition, so they are recorded per run rather
than per machine.

### 5. Scheduler

Control plane only. It picks a node and steps out of the way, so it never sits in the
response path.

The scheduler reads a C-6 manifest to learn the pool, the policy, and which C-3 snapshot
prices each node. Admissibility and capability both come from those snapshots, so a node
whose snapshot is missing gets neither.

Capability is output tokens per second of service at the snapshot's lowest prompt and output
bucket at one slot, computed as decode tok/s times the decode share of service time
(`com.sched.core.Capability`, shared by the live scheduler and the simulator). We used to
seed it with decode tok/s alone. That hides prefill, which is where our two GPU laptops
differ most: decode is 1.18x apart and prefill about 10x, so the capability-aware policies
saw a nearly homogeneous pool. A snapshot without the prefill/decode split still falls back
to decode tok/s, and the scheduler prints that when it happens.

```bash
cd controlplane
mvn -q exec:java -Dexec.mainClass=com.sched.live.LiveSchedulerApp \
  -Dexec.args="../runs/exp/<run_id>/manifest.pre.json --port 50051 \
    --cost-models ../contracts/cost_models --log-dir ../runs/exp/<run_id> \
    --worker gtx1650ti=10.42.0.1:50061 --worker rtx4070=10.42.0.11:50061"
```

`--worker <node_id>=<host:port>` maps the node ids in the manifest to endpoints. The ids
have to match, because `DispatchAck.chosen_node` carries the node id and that is what C-5
joins on.

The manifest is a pre-run input here, not the post-run C-6 record, which is why it is named
`manifest.pre.json`: `runset` only reads `manifest.json`, so a run that dies halfway leaves
nothing that looks like a data point. The scheduler takes its run id, policy, staleness and
log file from it at startup, so **one scheduler process serves exactly one run**. The
scheduler also serves the newest snapshot in each node class whatever the manifest names,
so the manifest has to name the newest one or it records a model that did not run.

For a campaign we do not do any of this by hand. `tools/hw_runs.py` writes the pre-run
manifest, starts a fresh scheduler per run, replays, stops the scheduler, pulls each node's
worker log back over rsync, and writes the post-run manifest:

```bash
uv run --project dataplane python tools/hw_runs.py dataplane/configs/hw_mpr2_lan_3050.json --dry-run
uv run --project dataplane python tools/hw_runs.py dataplane/configs/hw_mpr2_lan_3050.json \
  --clock-sync clock_sync.json
```

It refuses a snapshot that is not the newest in its class, a pool node with no worker
endpoint or log location, and a co-located pool. It runs the operating points slowest first
and shuffles the policy order at each point from a fixed seed, so throughput drift over the
campaign does not line up with the policy comparison. A campaign that stops can be restarted
with the same command; finished runs are skipped.

`fixtures/fake_scheduler/serve.py` still exists and still round-robins blindly without
writing a decision record. It is a Week-1 unblocking device, not a vehicle for a result:
every run it drives has `chosen_node` and `routing_error_ms` null, so it cannot produce
the 2×2 decomposition. Do not use it for anything being measured.

### 6. Run

One trace, generated once from a config and a seed. It prints its own SHA-256, and the
replay refuses to start unless the file still hashes to it.

```bash
uv run gen-trace configs/trace_anchor_1b.json -o runs/traces/t_lam1.2.jsonl
```

Then one replay per policy, and the whole set again per *R* point; `tools/hw_runs.py` does
this loop. By hand, `--advertise` is the LAN address the worker sends the response back to,
which is the F-11 path and the thing nothing else exercises.

```bash
uv run replay runs/traces/t_lam1.2.jsonl \
  --scheduler 10.42.0.1:50051 --run-id jsq_r1 \
  --sha256 <printed by gen-trace> \
  --bind 0.0.0.0:50071 --advertise 10.42.0.1 --out runs/exp
```

The replay reads `runs/exp/jsq_r1/manifest.pre.json` (or `--manifest`) and takes the node
block, policy, snapshots and staleness from it, so the post-run `manifest.json` describes
the scheduler that actually ran. It refuses before the first request if the run id, trace
hash or `--policy` disagrees with that manifest. Each worker writes its log on its own
host, so `worker_<node_id>_<run_id>.jsonl` has to be copied into the run directory before
the pipeline runs.

Then the pipeline, which is a pure function of the manifest and the three log files. No
network, no engine, and it runs on a laptop.

```bash
uv run pipeline  runs/exp/jsq_r1 --trace runs/traces/t_lam1.2.jsonl
uv run costcheck runs/exp                                     # before blaming the simulator
uv run runset    runs/exp --out runs/exp/runset.parquet
uv run figures   runs/exp/runset.parquet --out figures/ --tau-s 69.5
```

Nothing changes between runs except the policy and the `-ngl` setting. That is the entire
reason the results are comparable.

`figures` draws what the set can support and skips the rest, so a hardware-only set at one
*R* renders the load characterisation and nothing else. That characterisation is all four
of the study's dependent variables: latency percentiles, queue wait, per-node utilization,
and the routing share. The full set adds the hypothesis figures: H1's interaction plot, H2's
advantage curve against *R* drawn both ways, the interaction across the *R* range, and H3
against estimate age. `--tau-s` is the measured autocorrelation
time from the C-3 snapshot for that node class. Without it the H3 figure is skipped rather
than drawn against a guess, because age over τ is the only axis H3 is a claim about, and
substituting the heartbeat interval would turn a property of the process into a property
of a setting.

**Read the manifest before reading any figure.** Four fields decide whether a run is a data
point at all:

| Field | Must be |
|---|---|
| `validity.colocated_nodes` | `0`. One logical node per physical machine, or the contention confound is back. |
| `validity.send_lag_violations` | `0`. Anything else means the client was not open-loop for the whole window. |
| `clock_sync.ok` | `true`, with no host unsynchronised. |
| `nodes[].engine_version` | identical across the pool. |

---

## What it produces even if things go wrong

The claims are fixed in advance and ordered, so that a result can be pointed at a claim and a
claim at its evidence. The full ladder, with the evidence each one needs, is section 3 of
[`docs/research-plan.md`](docs/research-plan.md).

| | | Standing |
|---|---|---|
| **K1** | The heterogeneity a scheduler faces depends on the workload and on concurrency: 1.4x to 2.6x at one slot, 2.2x to 4.4x at four, on one pair of consumer GPUs under one pinned engine. | Held |
| **K2** | The value of calibration as a curve: latency against how wrong the believed capability ratio is, with an ordinal-only control and a per-request cost-model policy on the same axis. This is the contribution. | Configured, unrun |
| **K3 to K5** | What that curve depends on: the load the queue-blind router is under, the workload's phase mix, stale queue counts, and request sizes that are variable and unknown at dispatch. | Candidate, and two campaigns away |
| **K6** | Throughput drift on consumer GPU nodes serving a 1B model is below what this workload can resolve, and the floor is about five service times. | Held as a bound, with its interval |

K1 and K6 need no scheduler comparison, which is why they are the two that have landed.

---

## Repository layout

```
contracts/     The interface between the two halves. Six frozen artifacts, plus the
               committed cost-model snapshot series the simulator reads.
dataplane/     Workers, calibration campaign, harness, results pipeline.   (Python)
controlplane/  Scheduler, the eight policies, discrete-event simulator.    (Java)
tools/         Machine survey, LAN bring-up, pool install; the cross-seam CI checks;
               and the analyses that are not console entry points.
fixtures/      Fake scheduler and fake worker, so neither half blocks on the other.
docs/          Research, analysis, experiment, design and test plans; results; the frozen
               spec PDF and the UML figure set.
patches/       Changes to the pinned engine, with the reasoning that justifies them.
runs/          Measurement output. Only manifests and determinations are versioned.
```

![Component view](assets/fig02_component.png)

### The contract

The two halves interact through exactly six artifacts and **nothing else crosses the seam**.
All six are validated in CI on every pull request.

| # | Artifact | Direction |
|---|---|---|
| C-1 | [`scheduling.proto`](contracts/scheduling.proto) | bidirectional |
| C-2 | [Trace file](contracts/schemas/trace.schema.json) | harness to harness and simulator |
| C-3 | [Cost model snapshot](contracts/schemas/cost_model.schema.json) | data plane to control plane |
| C-4 | Log records ([client](contracts/schemas/log_client.schema.json), [scheduler](contracts/schemas/log_scheduler.schema.json), [worker](contracts/schemas/log_worker.schema.json)) | both to pipeline |
| C-5 | [Joined record](contracts/schemas/joined_record.schema.json) | pipeline to figures |
| C-6 | [Run manifest](contracts/schemas/manifest.schema.json) | launcher to everything |

Only C-1 and C-3 are runtime couplings. The rest are file formats, so both halves develop
against fixtures without waiting on each other.

**Why the seam sits there.** The binding constraint is that the simulator must run the same
policy implementations as the live scheduler. Split those across two people and they drift,
not maliciously but through ordinary divergence over tie-breaking, or over whether an
in-flight request counts before or after admission. That drift invalidates validation
*silently*, because both systems still run and still produce plausible numbers. So one
person owns the policy code and both of its hosts, and everything else is arranged around
that.

## Getting started

Requires [`uv`](https://docs.astral.sh/uv/). No GPU needed to run the checks.

```bash
uv run contracts/check.py                          # the six artifacts
cd dataplane && uv sync --all-groups && uv run pytest
```

`check.py` also validates an arbitrary file against the contract its name implies, which
is how either side checks output before pushing it rather than after the other side's
pipeline rejects it:

```bash
uv run contracts/check.py --validate runs/exp/jsq_r1/scheduler_jsq_r1.jsonl
```

## Documentation

The doc set was rewritten on 2026-09-16 and supersedes everything before it. The earlier
planning, scope and results documents are in the git history and are not authoritative.

| | |
|---|---|
| [Doc index](docs/README.md) | What each document settles, and when it changes. |
| [Research plan](docs/research-plan.md) | The question, the claims ladder, which hypotheses are in and out, the scope, and every deviation from the frozen specification. |
| [Analysis plan](docs/analysis-plan.md) | How every number is computed, and what each campaign outcome licenses us to say. Frozen before the campaigns run. |
| [Experiment plan](docs/experiment-plan.md) | What we run, in what order, what gates each block, and what we refuse to run. |
| [System design](docs/system-design.md) | The instrument: the seam, the contracts, one run end to end, the policies, the invariants, and the known limits. |
| [Test plan](docs/test-plan.md) | What the suite has to guarantee, and what passing means. |
| [Results](docs/results.md) | Every measurement that stands, with provenance, and every claim withdrawn. |
| [Requirements specification](docs/base_scope/scheduling-requirements-spec.pdf) | The frozen scope, hypotheses and F-numbers. Never edited; differences are listed in the research plan. |
| [UML figure set](docs/uml/FIGURES.md) | Twelve figures with captions and the requirements each discharges. |

## Project status

The instrument is built and frozen, the first pair has been measured, and that measurement has
been audited and re-derived. Five campaigns are configured and unrun.

| | |
|---|---|
| Instrument | Worker, harness, calibration, pipeline, figures, live scheduler, simulator sharing the policy code. Contract frozen, feature set frozen |
| Measured | 132 valid runs on one pair of laptop GPUs: the anchor trace and three workload shapes, five policies, three repeats, up to three load points |
| Re-derived | All 132 runs under the rules in [`docs/analysis-plan.md`](docs/analysis-plan.md): paired block bootstrap, a steady-state gate on every cell, log-scale interaction as the primary statistic |
| Standing | K1 and K6. K3 is a candidate on one arrival path, K2 and K5 are unrun, and H3 is out of this paper |
| Next | Two gates on this laptop (engine rebuild and bench, recalibration), then two nights on the pool. About 17 hours of machine time |

We audited the finished first pair as a reviewer would, and it changed the headline. What the
audit withdrew and why is section 11 of [`docs/results.md`](docs/results.md); the three open
control-plane items are in [`docs/experiment-plan.md`](docs/experiment-plan.md).

## Team

| | | |
|---|---|---|
| **Divyansh Shukla** (A) | [@divyanshuklai](https://github.com/divyanshuklai) | Data plane and measurement. Workers, calibration, harness, pipeline, figures. |
| **Aditya Gupta** (B) | [@adityaxgupta](https://github.com/adityaxgupta) | Control plane and simulation. Scheduler, policies, staleness injection, simulator, validation. |

Ownership marks primary responsibility, not exclusive access. Both of us can run the full
stack.
