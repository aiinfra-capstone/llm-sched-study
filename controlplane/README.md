# Control Plane & Simulation — Person B

Scheduler core, the five policy implementations, admission filter, node state store,
staleness injection, the discrete-event simulator, and F-23 validation.

**Requirements owned:** F-1 – F-8, F-11 (scheduler side), F-12, F-14, F-21 – F-24.
**MPR owned:** MPR-3 (H2/H3 sweeps in the validated simulator).

This directory is deliberately unopinionated about language. See "Language" below.

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

- **`Clock`** — `now_ns()`. Live reads the monotonic clock; sim returns event-queue
  time. Policies never call the system clock directly. This is the single change that
  makes shared policy code possible.
- **`StateStore`** — the scheduler's belief about each node: queue depth, in-flight,
  capability estimate, and the timestamp each was learned. Updated by heartbeat and
  completion in the live path; by simulated events in the DES.
- **`StalenessVeil`** — a **wrapper, not a flag**. It serves the policy a view of the
  state store as it existed `s` seconds ago, drawn from snapshot history. Making it a
  layer rather than a parameter inside each policy means no policy can accidentally
  read fresh state. F-8 says staleness is a first-class feature; this is what
  first-class looks like structurally.
- **`AdmissionFilter`** — applies F-14 *outside* the policy, so every policy including
  RoundRobin and Threshold(T) inherits admissibility identically and no policy is
  scored on infinite-latency outcomes.
- **The five policies** are pure functions with no state of their own except an
  injected `rng` for tie-breaks. RoundRobin's counter is the one exception — keep it in
  an explicit policy-state object passed in, so a replayed trace produces an identical
  dispatch sequence.
- **`ServiceSampler`** (DES only) — reads C-3 and returns a service time for
  `(node, prompt_len, output_len, concurrency)` plus the fitted lognormal multiplier
  with the measured autocorrelation. The only component here with no live-path
  counterpart.

## The 2×2 (F-1: all five selectable from one config value)

|  | Queue-blind | Queue-aware |
|---|---|---|
| **Hardware-blind** | `round_robin` | `jsq` |
| **Hardware-aware** | `static_weighted` | `wjsq` |

Plus `threshold` — round-robin over nodes above calibrated cutoff T; ignores queue and
fine-grained capability. This is the degenerate baseline H2 predicts WJSQ converges to
at high R.

The policy set is a **factorial design, not a ladder**. A ladder confounds
hardware-knowledge with queue-knowledge and cannot decompose the gain.

## Language

Free — any gRPC-capable language. **Two constraints, both from F-21:**

1. The policies must be in the same language as the DES.
2. The DES must be in the same language as the scheduler.

Which collapses to: **pick one language for this whole directory, and pick it once.**

> **The one thing that must not happen:** writing the scheduler in one language and the
> DES in another, then "keeping the policies in sync." That is F-21 violated in spirit
> while satisfied on paper, and it is the failure mode most likely to survive undetected
> until the validation numbers look strange in Week 4.

If you choose something other than Python, add your build/test invocation to
`.github/workflows/ci.yml` — the `controlplane` job is a placeholder until you do.

## What A needs from you, and when

| When | What |
|---|---|
| End of Week 1 | A fake worker that heartbeats scripted state, so A's replay client has something to talk to. `Clock` and `StateStore` interfaces. RoundRobin only. |
| Week 3 | All five policies live from one config value. |
| Week 4 | Simulator logs in C-4 format, in a directory. A's pipeline processes them with **zero changes** — that is the whole point of the C-4/C-5 split. |

## Validation (F-23)

Mechanically simple once the above holds: run the same trace through both vehicles at
three operating points, join both output Parquets, compare p50 and p95.
**Report observed error, not just pass/fail.**
