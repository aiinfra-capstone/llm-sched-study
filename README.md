# Scheduling LLM Inference Under Uncalibrated Heterogeneity

A **6-week measurement study**. The scheduler this repo contains is an *instrument,
not a deliverable* — it exists to produce measurements.

> **Research question.** In a pool of consumer machines whose per-node throughput is
> heterogeneous, non-stationary, and known only through stale estimates, does explicit
> hardware calibration improve scheduling beyond what live queue depth already reveals —
> and over what range of heterogeneity does that advantage hold?

Full statement of scope, hypotheses (H1–H3), requirements (F-1 – F-24), non-goals, and
the MPR ladder: [`docs/requirements-spec.pdf`](docs/requirements-spec.pdf). **Frozen.**

---

## Layout

```
contracts/       C-1 .. C-6 — the six frozen artifacts. Nothing else crosses the seam.
dataplane/       Person A — worker wrapper, harness, pipeline, figures.       (Python)
controlplane/    Person B — scheduler, five policies, DES.        (B's language choice)
fixtures/        Fake scheduler (A) and fake worker (B). Throwaway, load-bearing.
docs/            Spec, split & interface contract, freeze checklist.
```

`contracts/` is the interface. A and B interact through exactly six things, and
**nothing else crosses the seam**:

| # | Artifact | Direction | Format |
|---|---|---|---|
| C-1 | [`scheduling.proto`](contracts/scheduling.proto) | bidirectional | protobuf3 / gRPC |
| C-2 | [Trace file](contracts/schemas/trace.schema.json) | A → A (B reads for DES) | JSONL + header record |
| C-3 | [Cost model snapshot](contracts/schemas/cost_model.schema.json) | A → B | JSON |
| C-4 | Log record schemas ([client](contracts/schemas/log_client.schema.json) · [scheduler](contracts/schemas/log_scheduler.schema.json) · [worker](contracts/schemas/log_worker.schema.json)) | A and B → pipeline | JSONL |
| C-5 | [Joined record](contracts/schemas/joined_record.schema.json) | pipeline → figures | Parquet |
| C-6 | [Run manifest](contracts/schemas/manifest.schema.json) | launcher → everything | JSON |

C-1 and C-3 are the only true *runtime* couplings. The rest are file formats, so they
are validated offline against fixtures — see `uv run contracts/check.py`, which CI runs
on every PR.

---

## Ownership

| | **Person A — Data Plane & Measurement** | **Person B — Control Plane & Simulation** |
|---|---|---|
| Owns | Worker wrapper (vLLM + llama.cpp), heartbeat emitter, calibration campaign, non-stationarity measurement, trace generator, replay client, log join pipeline, figures | Scheduler core, five policy implementations, admission filter, node state store, staleness injection, discrete-event simulator, F-23 validation |
| Requirements | F-9, F-10, F-11 (worker side), F-13, F-15 – F-20 | F-1 – F-8, F-11 (scheduler side), F-12, F-14, F-21 – F-24 |
| Owns MPR | MPR-1 (τ and variance envelope — Week 2, hardware only) | MPR-3 (H2/H3 sweeps in validated simulator) |
| Load profile | Front-heavy: Weeks 1–3 | Back-heavy: Weeks 3–5 |

MPR-2 is jointly owned and is the first point where both halves must work simultaneously.

**Why the seam is here:** the binding constraint is **F-21** — the discrete-event
simulator must execute *the same policy implementations* as the live scheduler, shared
code rather than a reimplementation. If the live scheduler and the DES were owned by
different people they would drift: not maliciously, but through ordinary divergence in
tie-breaking, in how "queue depth" is defined at the moment of decision, in whether an
in-flight request counts before or after admission. That drift invalidates F-23
validation *silently*, because both systems still run and still produce plausible
numbers. So one person owns the policy code and both of its hosts, and everything else
is arranged around that.

Ownership marks primary responsibility, not exclusive access. Both members can run the
full stack.

---

## Getting started

```bash
git clone <this repo> && cd <repo>

# Verify the contracts — needs nothing but uv.
uv run contracts/check.py

# Person A
cd dataplane && uv sync --all-extras && uv run pytest
```

**Person B:** `controlplane/` is yours and unopinionated — see
[`controlplane/README.md`](controlplane/README.md) for the two constraints that bind
your language choice (there are only two, and they both come from F-21).

### Fixture-first — this is what buys the parallelism

Neither person should ever be blocked waiting for the other's component to be real.
By end of Week 1 both halves run against fixtures: **B builds a fake worker that
heartbeats scripted state; A builds a fake scheduler that round-robins blindly.** They
are throwaway, cost about half a day each, and are the difference between two people
working in parallel and two people working in sequence on a timeline with no slack.

---

## Status

| | |
|---|---|
| Requirements spec | **Frozen.** Changes to §3 or §4 require explicit re-scoping against §6. |
| Interface contract (C-1 – C-6) | Freezes **end of Week 1** — see [`docs/week1-freeze-checklist.md`](docs/week1-freeze-checklist.md) |
| Feature freeze | **End of Week 3.** No new system capability after this point. |
| Slack | None. If a week is lost, §7 of the spec defines what survives. |

## Reading order for a new contributor

1. [`docs/requirements-spec.pdf`](docs/requirements-spec.pdf) — what is being measured and why.
2. [`docs/two-person-split-and-interface-contract.md`](docs/two-person-split-and-interface-contract.md) — where the seam is, and the six artifacts across it.
3. [`docs/week1-freeze-checklist.md`](docs/week1-freeze-checklist.md) — what must be true before the contract freezes.
4. Your half's README.
