# Control plane and simulation (Aditya Gupta, B)

The scheduler core, the eight policies, the admission filter, the node state store,
staleness injection, the discrete-event simulator, and F-23 validation.

**Requirements owned:** F-1 to F-8, F-11 (scheduler side), F-12, F-14, F-21 to F-24.
**MPR owned:** MPR-3 (H2 sweeps in the validated simulator).

Everything here is Java 17 built with Maven. CI runs `mvn clean test` in this directory
(`.github/workflows/ci.yml`, job `controlplane`). Locally:

```
cd controlplane && mvn -q test
```

---

## The design goal

> **`choose()` cannot tell whether it is running in the live scheduler or the DES.**

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

- **`Clock`**: `now_ns()`. Live reads the monotonic clock; the simulator returns
  event-queue time. Policies never call the system clock directly, which is what lets
  one policy class serve both vehicles.
- **`StateStore`**: the scheduler's belief about each node: queue depth, in-flight,
  capability estimate, and when each was learned. Heartbeats and completions update it
  in the live path; simulated events update it in the DES.
- **`StalenessVeil`**: a wrapper, not a flag. It serves the policy the state store as it
  was `s` seconds ago, from snapshot history. Because it is a layer and not a parameter
  inside each policy, no policy can read fresh state by accident.
- **`AdmissionFilter`**: applies F-14 outside the policy, so every policy, RoundRobin and
  Threshold(T) included, inherits admissibility identically and none is scored on
  requests no node could serve.
- **Policies** hold no state of their own except an injected `rng` for tie-breaks.
  RoundRobin's counter and StaticWeightedWRR's credits are the exceptions, and both are
  deterministic, so a replayed trace gives an identical dispatch sequence.
- **`ServiceSampler`** (DES only) reads C-3 and returns a service time for
  `(node, prompt_len, output_len, concurrency)`. The mean comes from the same lookup ECT
  uses (`CostModelSnapshot.meanServiceMs`). It is the only component here with no
  live-path counterpart.

### The simulator's noise model

The simulator's noise is i.i.d.: one lognormal multiplier per request,
`exp(σZ - σ²/2)` with `Z` standard normal, so its mean is 1, and σ from the snapshot's
`stochastic.sigma`. Draws are independent across requests, and there is no
autocorrelation. `stochastic.autocorr_time_s` is a measurement reported for
characterisation (K6), and the simulator does not read it. K6 found no drift the
instrument could resolve on any class in the pool, so an i.i.d. model is what we
measured. `--deterministic` sets the multiplier to 1, which is what F-20 parity uses.

### Node model

`SimNode.batch_capacity` is llama.cpp's slot model exactly: a fixed number of parallel
sequences, each holding a fixed KV share. The simulator reads it from
`manifest.nodes[].engine_config.parallel`, so any F-23 error we see is a real
difference and not modelling slack.

## The policies (F-1: all eight selectable from one config value)

The four corners of the 2×2:

|  | Queue-blind | Queue-aware |
|---|---|---|
| **Hardware-blind** | `round_robin` | `jsq` |
| **Hardware-aware** | `static_weighted` | `wjsq` |

And four more:

- `threshold`: round-robin over the nodes above a calibrated cutoff T. It ignores queue
  and fine-grained capability, and is the baseline H2 predicts WJSQ converges to at
  high R.
- `jsq_fastfirst`: JSQ with ties broken towards the more capable node.
- `static_weighted_wrr`: `static_weighted` served by smooth weighted round-robin, so the
  long-run share is the policy's share and not a property of one random stream.
- `ect`: earliest predicted completion from the full C-3 cost model (P6). Its mode
  (`ect_mode`: `known` or `unknown`) comes from the run's config, and so does the
  output-length prior in `unknown` mode (`output_len_prior`). Neither is defaulted.

The 2×2 is a factorial design, not a ladder. A ladder confounds hardware knowledge with
queue knowledge and cannot decompose the gain.

## What each vehicle writes

Both vehicles write C-4 logs (`scheduler_`, `worker_`, `client_` JSONL) that the data
plane's pipeline reads with no changes.

- `completion_observed.source` is `completion_rpc` in the live scheduler and
  `sim_completion` in the simulator. `observed_lag_ns` is measured live (from the
  completion RPC reaching the scheduler to the state update it causes) and is 0 in the
  simulator, where the update happens in the same event.
- The live scheduler counts `Heartbeat.seq` per node and writes one `heartbeat_summary`
  record per node at shutdown.
- `SimApp` writes a C-6 manifest with `vehicle: simulator`, validity computed from the
  run (drops in the measurement window), `heartbeat_gaps: null` listed under
  `unmeasured`, and the shas of the checkout that ran it. It refuses a manifest with no
  `config.seed` and a trace whose SHA-256 or `trace_schema` does not match, and exits
  non-zero without writing a manifest if the write fails.

## Language

F-21 has two constraints: the policies are in the same language as the DES, and the DES
is in the same language as the scheduler. So this whole directory is one language, and it
is Java. Writing the scheduler in one language and the DES in another, then keeping the
policies in sync by hand, would satisfy F-21 on paper and break it in practice.

## Validation (F-23)

We run the same trace through both vehicles at three operating points, join both
outputs, and compare p50 and p95. We report the observed error, not only pass or fail.
