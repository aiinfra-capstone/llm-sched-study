# Research plan

**Locked 2026-09-16.** This document fixes what the study asks, what it claims, and what it
refuses. Every other document in `docs/` serves it. The rule for changing it is in section 8,
and the bar is deliberately high: the aim has moved twice already, once by choice and once by
correction, and a third move would cost more than any result it could buy.

---

## 1. The question

> Given a router that already sees live queue depth, how much does hardware calibration add,
> and what does that depend on: how wrong the estimate is, how loaded the pool is, how
> variable the request sizes are, and how stale the queue signal is?

The measured premise underneath it, which is what makes this a question about language-model
serving rather than about servers in general:

> A node's speed relative to another's is not one number. Prefill and decode do not scale
> together across machines, and neither does batching efficiency, so the heterogeneity a
> scheduler faces moves with the shape of the request and with how many requests are in
> flight.

## 2. Why this wording, and what it replaces

The frozen specification asks whether calibration helps "in a pool of consumer machines whose
per-node throughput is heterogeneous, non-stationary, and known only through stale estimates".
Three conditions, and our instrument delivers them unequally.

| Condition | What we can deliver |
|---|---|
| Heterogeneous | Yes. 1.4x to 2.6x at one slot and 2.2x to 4.4x at four on the first pair, wider in the simulator |
| Known only through stale estimates | Yes, for the queue signal, through the staleness veil. The capability estimate itself is static in both vehicles |
| Non-stationary | No. Both GPU classes decorrelate faster than the measurement floor, and τ is unresolved even on the CPU class |

Keeping "non-stationary" in the question would promise evidence the hardware cannot produce.
That absence is itself reportable: on consumer GPUs serving a 1B model under this workload,
throughput drift is below what five service times can resolve, which tells anyone repeating
this what their hardware has to be able to do before the question is askable at all.

The second change is that the old question is a yes or no, and the honest answer on our
evidence is "it depends, and here is on what". Turning the dependence into the axis is what
the pending campaigns are built around.

## 3. The claims ladder

Numbered, so that a result can be pointed at a claim and a claim can be pointed at its
evidence. Nothing outside this list goes in the paper as a contribution.

| # | Claim | Evidence it needs | Status |
|---|---|---|---|
| **K1** | The heterogeneity a scheduler faces depends on the workload and on concurrency: 1.4x to 2.6x at one slot, 2.2x to 4.4x at four, on one pair of consumer GPUs with one pinned engine | Committed cost models with phase splits and intervals, plus the operating ratios the live runs recorded | Held. `tools/cell_intervals.py`, held-out shape runs agree |
| **K2** | A curve for the value of calibration: latency against how wrong the believed capability ratio is, from a homogeneous belief through the true operating ratio and beyond, with an ordinal-only control and a per-request cost-model policy on the same axis | The ablation campaign in both vehicles | Pending. Configured, unrun |
| **K3** | While the queue-blind router is comfortably stable, calibration removes about the same fraction of latency with or without queue depth. A positive interaction appears as that router approaches its stability limit | The seeded anchor campaign, at three utilisations, with arrivals and routing draws resampled | Candidate. Five of nine points on the first pair, conditional on one arrival path |
| **K4** | The residual value of calibration to a queue-aware router is larger, as a fraction, on the workload where the machines differ most | The matched-load shape campaign | Candidate. 0.889 to 0.769 on the first pair, load not matched |
| **K5** | Whether K3 and K4 survive outside the regime most favourable to queue depth: stale queue counts, and request sizes that are variable and unknown at dispatch | The staleness and heavy-tailed campaigns | Pending. Configured, unrun |
| **K6** | Throughput drift on consumer GPU nodes serving a 1B model is below what this workload can resolve, and the resolution floor is about five service times | The calibration records and the 80-minute CPU 1B segment | Partly held, stated as a bound with its interval |

K2 is the contribution. K1 is the premise that makes it specific to this domain. K3 to K5 are
the conditions under which the curve means anything. K6 is what replaces the old MPR-1.

## 4. Hypotheses

| | Standing | Wording we test |
|---|---|---|
| **H1** | In | Calibration's benefit to a queue-aware router is smaller than its benefit to a queue-blind one. The primary statistic is the interaction on log mean latency; positive means calibration buys a smaller fraction once the policy sees queue depth |
| **H1b** | Rejected on the first pair, reported as a null | The spec's second clause, that the queue-aware benefit shrinks as load rises. In milliseconds it grows on three of four workloads and is flat on the fourth; as a ratio no workload shows a change distinguishable from none |
| **H2** | In, simulator only | The advantage of hardware-aware routing is non-monotonic in R and converges to Threshold(T). It is drawn both as the specification states it (WJSQ minus JSQ) and as the figures compute it (best aware minus best blind), each beside the gap to Threshold |
| **H3** | **Out of paper one** | Routing quality degrades as estimate age approaches τ. Section 5 says why and what would bring it back |
| **Elevation** | In, as K1 and K4 | The value of calibration moves with the workload's prompt-to-output ratio, in the direction set by which phase the machines differ on |

H2 is reported only if the simulator passes the contrast criterion in the analysis plan and
the sweep sets load as a fraction of pool capacity. Without both it is drawn as an
illustration and labelled as one.

## 5. H3 is out, and the reason is measurement, not effort

H3 asks about the age of a throughput estimate against the autocorrelation time of that
throughput. Four things stand between us and an answer, and all four are structural.

1. The simulator draws service times from a static lookup table, so no quantity in it has an
   autocorrelation time.
2. The capability a policy reads in the simulator comes from the same snapshot that generates
   the service times, so calibration error is zero by construction and the mechanism H3 names
   has nothing to act on.
3. The staleness veil ages queue counts. Capability never ages. The timescale that governs
   stale queue counts is the service time, not τ.
4. τ is unresolved on every class we own, so the axis has no unit.

Fixing all four is several days of control-plane work plus an 80-minute calibration, and it
would produce one figure. We are not spending paper one's remaining time on it.

**What we report instead.** K6, as a characterisation with a bound and an interval, and the
staleness campaign as a robustness test of H1 rather than as an H3 measurement. The
difference is written into the campaign's own configuration comment so it cannot be
misread later.

**What would bring H3 back.** A drift process in the simulator, an online capability estimate
that ages, counterfactual regret as the quality metric, and a τ measured on a class that is in
a pool. That is `M12` in the experiment plan, and it belongs to a second paper or to a later
revision of this one.

## 6. Scope

**In.** Whole-request routing across nodes that each hold the whole model. One engine, one
model, one quantisation, pinned. Policies that differ only in what they know: nothing, queue
depth, capability, both, a cutoff, a ranking, a per-request cost model. Two vehicles, hardware
and a discrete-event simulator running the same policy classes. Consumer machines.

**Out, and not to be revisited inside this paper.**

| Refused | Why |
|---|---|
| Prefix caching and KV affinity | Service time would depend on trace order, which destroys a cost model keyed on (prompt, output, concurrency). It is a rebuild of the apparatus, not a feature |
| Prefill and decode disaggregation | Splitwise, DistServe and HexGen-2 own that question, and it needs interconnect we do not have |
| A new scheduler as a contribution | The scheduler is an instrument. We measure what information a router needs, and do not propose one |
| Model sharding, pipeline parallelism, peer-to-peer scheduling, learned policies | Out at any timeline we have |
| An offline oracle for regret on hardware | The counterfactual is only computable in the simulator, and it arrives with H3 or not at all |
| A wider token envelope | Measured as affordable and declined: it costs a recalibration and lengthens every run, and the shape result is available inside the current envelope |

## 7. Deviations from the frozen specification

The specification PDF in `base_scope/` is the authority for the original scope and is not
edited. Every difference between it and this plan is listed here, and the paper states them in
its method section.

| # | Specification | Here | Reason |
|---|---|---|---|
| 1 | H1 formally as `(WJSQ - JSQ) < (StaticWeighted - RoundRobin)` | The opposite sign | With both brackets negative when calibration helps, the spec's inequality states the opposite of the sentence beside it. The sentence is unambiguous, so we follow the sentence |
| 2 | H2 observable is WJSQ minus JSQ converging to Threshold | Figures compute best hardware-aware minus best hardware-blind | Both are drawn, side by side, with the gap to Threshold on each |
| 3 | R of 10 to 100x | 1.4x to 4.4x measured; larger values only in the simulator, by scaling a measured snapshot | Two laptops. The synthetic range is labelled as synthetic in every figure |
| 4 | Non-stationary throughput as a condition | Reported as unresolvable on this hardware (K6) | Section 5 |
| 5 | Controlled staleness across the study | Staleness on the queue signal only, at 0, 1 and 5 s, as a robustness test | Section 5 |
| 6 | N up to 12 | N = 2 on hardware, N = 1 fast plus k slow in the simulator | Two machines |
| 7 | Four machines, one CPU-only | Two laptop GPUs, one of which also hosts the harness | Availability |
| 8 | Priority as a passthrough label | Generated in traces, read by nothing | Unchanged from SCOPE-CHANGE-002, stated so nobody looks for a priority result |

## 8. How this document changes

It changes for one reason: a measurement contradicts a premise stated in it. Not because a
result is disappointing, not because a reviewer suggests a better paper, and not because a
new capability becomes available.

The procedure, in order:

1. Write the contradicting measurement into `results.md` with its provenance.
2. Amend this document, with the date and the measurement that forced it.
3. Amend `analysis-plan.md` only if the primary statistic has to change, in its amendment log.
4. Re-derive anything downstream, and say in `results.md` what moved.

An amendment that is not traceable to a measurement in `results.md` is not a valid amendment.

## 9. What each venue needs

| Target | Needs | Where we are |
|---|---|---|
| Capstone checkpoint, October | K1, K3, K6, the instrument, and the audit record | Held today |
| Workshop or a characterisation paper | K1, K2, K3, K6, and the seeded intervals | Two campaigns away |
| Small to mid-tier journal | The above plus K4 and K5, and a simulator that passes the contrast criterion | Five campaigns and one control-plane item away |
| A stronger submission | The above plus a second pair, N greater than two on hardware, and either a phase-flipping pair or H3 rebuilt | Needs machines we do not have |

## 10. What would make this unpublishable

Recorded so that the risks stay visible rather than becoming surprises.

1. The 1650 Ti rebuild moves its prefill speed materially and no machines are available to
   re-run the first pair. K1 would then rest on a binary we cannot characterise.
2. The value-of-calibration curve is flat between the ordinal control and every calibrated
   arm, and the heavy-tailed and staleness campaigns leave it flat. The result is then a
   clean negative with no positive quantity beside it, which caps the venue.
3. The simulator fails the contrast criterion and cannot be repaired in time. H2 and every
   claim about R beyond 4.4x leave the paper.

None of the three removes the capstone deliverable, and the first two still leave a paper.
