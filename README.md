# Scheduling LLM Inference Under Uncalibrated Heterogeneity

A six-week **measurement study** of how to route LLM inference requests across a pool of
mismatched consumer machines. The scheduler in this repository is an *instrument*, not a
product: it exists to produce measurements, and it is built to the minimum fidelity that
supports the hypotheses below.

---

## The problem

Suppose you have four machines you already own — a desktop with a discrete GPU, a laptop
with a weaker one, an older GPU box, and a CPU-only machine — and you want them to serve
one LLM together. A request arrives. Where should it go?

Two things make this harder than ordinary load balancing:

**The machines differ enormously.** In a datacenter, hardware generations differ by
roughly 2–5×. Consumer machines differ by **10–100×**. A request that takes 2 seconds on
the fast node may take 3 minutes on the slow one — or exceed any reasonable timeout
entirely, which is a *categorical* failure rather than a latency tail.

**You do not reliably know how fast each machine is.** You can measure it, but the
measurement decays: LLM serving throughput is non-stationary under sustained load, drifting
with thermal state, memory pressure, and batch composition. So the scheduler is always
routing on an estimate that is somewhat out of date.

The obvious fix is calibration — measure each node's tokens/second and weight your routing
by it. This study asks whether that is actually worth doing:

> **Research question.** In a pool of consumer machines whose per-node throughput is
> heterogeneous, non-stationary, and known only through stale estimates, does explicit
> hardware calibration improve scheduling **beyond what live queue depth already reveals**
> — and over what range of heterogeneity does that advantage hold?

The reason to doubt it: **queue depth is already a partial proxy for speed.** Slow nodes
accumulate queue, so a scheduler that simply joins the shortest queue is *implicitly*
hardware-aware. Calibration may be measuring something the queue already tells you for free.

## What is being tested

Three hypotheses, each falsifiable, each with a negative result that is worth reporting.

| | Claim | Why it matters |
|---|---|---|
| **H1** | Calibration is largely **redundant** given queue-awareness, and more so as load rises. Formally, the interaction term is negative: `(WJSQ − JSQ) < (StaticWeighted − RoundRobin)`. | If true, the honest headline contradicts the intuition motivating hardware-aware schedulers. If false, calibration carries independent signal and the mechanism needs identifying. |
| **H2** | The advantage of hardware-aware routing is **non-monotonic** in the heterogeneity ratio *R*: it rises, peaks, then declines toward zero. | As *R* → ∞ the best policy converges to *thresholding* — never dispatch below a cutoff — which is behaviourally round-robin over the strong subset, achievable by a one-line static rule with no calibration machinery. So hardware-awareness has a **sweet spot**, and is matched by something trivial outside it. |
| **H3** | Routing quality degrades as the age of a node's estimate approaches the **autocorrelation time τ** of that node's actual throughput. | This turns the non-stationarity of LLM throughput from a threat to the method into the independent variable — and it gives an empirical basis for choosing a heartbeat frequency, which is otherwise arbitrary. |

The policies form a **2×2 factorial, not a ladder** — a ladder confounds hardware-knowledge
with queue-knowledge and cannot decompose the gain:

| | Queue-blind | Queue-aware |
|---|---|---|
| **Hardware-blind** | `RoundRobin` | `JSQ` (join-shortest-queue) |
| **Hardware-aware** | `StaticWeighted` (calibration weights, ignores live queue) | `WJSQ` (capability-weighted queue depth) |

Plus `Threshold(T)` — round-robin over nodes above a calibrated cutoff — as the degenerate
baseline H2 predicts `WJSQ` converges to at high *R*.

## How it is measured

![Deployment view](assets/fig03_deployment.png)

Four machines on a LAN, **all running the same inference runtime** (llama.cpp + GGUF).
Holding engine and quantization constant is deliberate: it makes the heterogeneity ratio
*R* a property of hardware and configuration alone. A mixed-engine pool would confound *R*
with an engine effect that none of the three hypotheses can decompose. Per-node capability
is then set by configuration — GPU offload (`-ngl`), threads, slot count — which also makes
*R* **tunable on real hardware** rather than reachable only in simulation.

Two vehicles, and the split between them is the core methodological commitment:

- **Hardware** measures what the simulator must not assume: cost-model parameters, the
  autocorrelation time τ and variance envelope, validation anchors, and the range of *R*
  that real machines can actually span.
- **A discrete-event simulator** provides breadth — node counts to 12, *R* to 100×,
  controlled estimate staleness — none of which four machines can reach.

The simulator **executes the same policy code as the live scheduler** (not a
reimplementation), is parameterized from measured hardware data, and is validated against
live runs on identical replayed traces before any simulated result is believed. Every
simulated figure is labelled as such.

Three disciplines run through the whole design, and are worth stating because they are the
sort of thing that quietly invalidates measurement studies:

1. **No cross-host clock subtraction.** Host clocks are not synchronised. Every duration in
   the analysis comes from a single machine's monotonic clock; whatever is left over is
   reported as one honest *residual*, not decomposed into invented stages.
2. **Open-loop load generation.** The replay client never waits for a response before
   firing the next request, and asserts its own send-lag per request. A run whose timing
   drifted is marked invalid rather than analysed.
3. **Byte-identical trace regeneration.** A trace is reproducible from `(config, seed)` and
   identified by its SHA-256, so the same workload can be replayed across every policy and
   across the hardware/simulator boundary.

## What it produces even if things go wrong

The window has no slack, so the result ladder is defined in advance and strictly ordered:

| | | Depends on |
|---|---|---|
| **MPR-1** | A characterization of throughput non-stationarity in consumer LLM serving nodes — τ, the variance envelope, and the implication that any single calibrated tok/s figure is a moving average over a non-stationary process. | Nothing. Week 2, hardware only. |
| **MPR-2** | The H1 2×2 decomposition on real hardware, across the synthesized *R* range, plus the load-band characterization. | Week 3–4. |
| **MPR-3** | H2 and H3 — the non-monotonic advantage curve and its shift under staleness, in the validated simulator. | Weeks 5–6. |

MPR-1 stands alone as a measurement contribution and needs no scheduler comparison at all.

---

## Repository layout

```
contracts/       The interface between the two halves — six frozen artifacts.
dataplane/       Workers, measurement harness, results pipeline, figures.   (Python)
controlplane/    Scheduler, the five policies, discrete-event simulator.
fixtures/        Fake scheduler and fake worker, so neither half blocks on the other.
docs/            Spec, decision records, UML figure set.
assets/          Images used by this README.
```

The whole system is two processes and a client, and the seam between them is narrow by
design:

![Component view](assets/fig02_component.png)

The scheduler is **control-plane only** — it chooses a node and steps out of the way.
Responses travel from worker straight back to the client, so the scheduler never sits in
the response data path.

### The contract

The two halves interact through exactly six artifacts and **nothing else crosses the
seam**. All six are validated in CI on every pull request:

| # | Artifact | Direction | Format |
|---|---|---|---|
| C-1 | [`scheduling.proto`](contracts/scheduling.proto) | bidirectional | protobuf3 / gRPC |
| C-2 | [Trace file](contracts/schemas/trace.schema.json) | harness → harness, simulator | JSONL + header |
| C-3 | [Cost model snapshot](contracts/schemas/cost_model.schema.json) | data plane → control plane | JSON |
| C-4 | Log records ([client](contracts/schemas/log_client.schema.json) · [scheduler](contracts/schemas/log_scheduler.schema.json) · [worker](contracts/schemas/log_worker.schema.json)) | both → pipeline | JSONL |
| C-5 | [Joined record](contracts/schemas/joined_record.schema.json) | pipeline → figures | Parquet |
| C-6 | [Run manifest](contracts/schemas/manifest.schema.json) | launcher → everything | JSON |

Only C-1 and C-3 are runtime couplings. The rest are file formats, so both halves can be
developed against fixtures without either waiting on the other.

**Why the seam sits where it does.** The binding constraint is that the simulator must run
*the same policy implementations* as the live scheduler. Split those across two people and
they drift — not maliciously, but through ordinary divergence in tie-breaking, or in
whether an in-flight request counts before or after admission. That drift invalidates
validation *silently*, because both systems still run and still produce plausible numbers.
So one person owns the policy code and both of its hosts, and everything else is arranged
around that.

## Getting started

Requires [`uv`](https://docs.astral.sh/uv/). Nothing else — no GPU needed to run the checks.

```bash
git clone git@github.com:aiinfra-capstone/llm-sched-study.git
cd llm-sched-study

# Validate the six contract artifacts (schemas + examples + proto compile)
uv run contracts/check.py

# Data plane
cd dataplane && uv sync --all-groups && uv run pytest
```

Rebuild the UML figures (needs `java` and `graphviz`):

```bash
docs/uml/render.sh
```

## Documentation

| | |
|---|---|
| [Requirements specification](docs/requirements-spec.md) | Scope, hypotheses, F-1 – F-24, non-goals, threats to validity, the MPR ladder. **Start here.** Readable inline; [PDF](docs/scheduling-requirements-spec_sc001.pdf) for print. |
| [SCOPE-CHANGE-001](docs/SCOPE-CHANGE-001.md) | Decision record: why the pool moved to a single inference engine. Integrated into the spec. |
| [SCOPE-CHANGE-002](docs/SCOPE-CHANGE-002.md) | Decision record: why `priority` is a passthrough label, and the priority metric withdrawn. |
| [Split & interface contract](docs/two-person-split-and-interface-contract.md) | Where the seam is, the six artifacts across it, and the failure modes to watch for. |
| [Week-1 freeze checklist](docs/week1-freeze-checklist.md) | What must be true before the contract freezes. |
| [UML figure set](docs/uml/FIGURES.md) | Twelve figures with draft captions and the requirements each discharges. |

## Project status

Six weeks, no slack. Feature freeze at end of Week 3; the interface contract freezes at end
of Week 1.

| Week | Focus |
|---|---|
| 1 | Worker wrapper, heartbeat, thin client, measurement harness. One query routed and measured end to end. |
| 2 | Calibration campaign; τ and the variance envelope; synthesizable *R* range. **MPR-1.** |
| 3 | Multi-node pool; all five policies behind one config value; load band identified. **Feature freeze.** |
| 4 | Discrete-event simulator sharing policy code; validated against hardware. |
| 5 | Sweeps: *R* × load × staleness × policy. Hypotheses tested. |
| 6 | Analysis, threats to validity, literature positioning, writeup. |

## Team

| | | |
|---|---|---|
| **Divyansh Shukla** (A) | [@divyanshuklai](https://github.com/divyanshuklai) | Data plane & measurement — workers, calibration, harness, pipeline, figures |
| **Aditya Gupta** (B) | [@adityaxgupta](https://github.com/adityaxgupta) | Control plane & simulation — scheduler, policies, staleness injection, simulator, validation |

Ownership marks primary responsibility, not exclusive access; both members can run the full
stack. The specification assigns three roles (A/B/C); B and C are merged here, for the
reason given under *Why the seam sits where it does*.
