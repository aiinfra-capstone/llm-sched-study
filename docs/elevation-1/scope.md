# Elevation 1: scope

## Why the base research question was not enough

The question the base scope asks is whether explicit hardware calibration improves
scheduling beyond what live queue depth already reveals, and over what range of
heterogeneity that advantage holds.

The problem with it is not the phrasing. It is that nothing in it is about language models.
Substitute video transcoding chunks or database queries for the requests and the question is
unchanged, which puts it against thirty years of queueing literature where the fact that
queue depth acts as an implicit speed estimator has been understood since the early 2000s.
Our own setup made this worse in three specific ways: every request starts from a cold cache
because `cache_prompt` is false, the two phases of inference are collapsed into one scalar
service time, and the admissible envelope of 512 prompt and 128 output tokens is small
enough to avoid memory pressure entirely.

## The elevation: R is not a scalar

Prefill is compute-bound and decode is memory-bandwidth-bound, and machines do not lose
those two capabilities at the same rate. A CPU is closer to a GPU on arithmetic than it is
on memory bandwidth, so the heterogeneity a scheduler faces depends on the shape of the
request, not only on the pair of machines.

We are not proposing this from theory. It is already visible in our committed cost models,
on the same model and quantisation, once the phase split was backfilled into C-3:

```
prompt   output  c | R_service  R_prefill  R_decode
(1,128)  (1,64)  1 |     1.75       1.46       1.83
(1,128)  (1,64)  4 |     1.64       1.18       1.92
```

Decode is 1.25x to 1.6x more heterogeneous than prefill, in the predicted direction. The
magnitude is small only because the faster node in that pair is a partial offload at
`ngl 20` rather than a real GPU, and because those two classes were calibrated at a single
grid cell, so the prompt-to-output ratio never varied. Varying it is the experiment.

**The revised question**, which contains the base one rather than replacing it:

> In a pool of consumer machines whose per-node throughput is heterogeneous, non-stationary
> and known only through stale estimates, does explicit hardware calibration improve
> scheduling beyond what live queue depth already reveals; and how does the answer move with
> the prompt-to-output ratio of the workload, given that prefill and decode do not degrade
> at the same rate across machines?

The predicted finding is that queue depth substitutes well for calibration on prompt-heavy
work, where R is small, and fails on decode-heavy work, where R is large and misrouting a
generation-heavy request causes head-of-line blocking that a queue counter cannot anticipate.
That is a result queueing theory cannot produce without phase decomposition, and it is
actionable: it tells an operator when a profiler is worth writing.

## What the elevation adds

1. **A second physical machine.** Deployable R has been 1.00x against a specification asking
   for 10x to 100x. A CPU-only box running the same Llama-3.2-1B at `ngl 0` gives a
   same-model GPU-versus-CPU pair, which is both the largest R available to us and the
   cleanest comparison for the phase result.
2. **The scheduler actually dispatching.** See below; this was not known to be missing.
3. **Parameter sweeps.** `runs/sweeps` is empty and no sweep runner exists.
4. **Workload-shape profiles.** Three trace profiles at prompt-to-output ratios of roughly
   16, 2 and 0.5, all inside the existing admissible envelope and all inside cells the cost
   model has already calibrated.
5. **A stochastic component that matches the hardware.** One global sigma of 0.1225 is
   replaced by a fit per concurrency.
6. **Honest run lengths.** The saturated operating point is a transient observation and our
   own load-band tool already says so.

## The gap nobody had noticed

`SchedulerGrpcService.dispatch()` runs the staleness veil, the admission filter and the
policy, writes the decision record, returns a `DispatchAck` carrying the chosen node, and
stops. It never calls `Worker.Execute`, and no node-to-endpoint map exists anywhere in the
control plane.

Every hardware run so far has gone through the fixture scheduler, which does forward
`Execute` but selects with a blind rotation and writes no scheduler log. That is why every
joined record we hold has `chosen_node`, `routing_error_ms` and the other decision-derived
columns null.

MPR-2 is the four-policy decomposition on real hardware. A blind rotation cannot produce it.
So MPR-2 was never blocked only on a second machine, and cabling one would have produced a
two-node pool able to run exactly one policy. Tracked as issue #14.

It needs no contract change. `DispatchRequest` already carries every field `ExecuteRequest`
needs, and the endpoint map can come from command-line arguments the way the fixture's
already does. C-1 stays frozen.

## What we refuse, and why

**KV-cache affinity and prefix caching.** This is the most tempting addition, because
locality-versus-load routing under heterogeneity is the central trade-off in modern serving.
We are not taking it. `cache_prompt: false` is a deliberate confound control: with prefix
caching on, service time depends on what ran before, which turns a cost model keyed on
`(prompt_len, output_len, concurrency)` into one keyed on trace order and destroys C-3's
identity. That is a rebuild of the measurement apparatus, not a feature.

**Raising the token cap to 2048 prompt and 256 output.** We measured this as affordable
rather than assuming it was not: VRAM peaks at 1993 MiB of 4096 with 8192 tokens per slot,
and prefill stays close to linear out to 4096 tokens. We are still not doing it now. It
costs a recalibration and drops saturated node throughput from 2.55 to 0.23 requests per
second, which lengthens every run in the sweep, and the phase-asymmetry result is available
for free inside the current envelope. The measurements are recorded in
[`evidence.md`](evidence.md) so the decision can be revisited with numbers rather than
re-litigated.

**Model sharding, pipeline parallelism, peer-to-peer scheduling, learned schedulers.** Out
of scope at any timeline we have.

**Regret against an offline oracle.** Held in reserve. `routing_error_ms` already exists but
is estimate-based rather than a true counterfactual, so a real oracle is a refinement we
take only if the sweeps land early.

## What we are not reframing

The scrutiny pass graded the study against IEEE TPDS and INFOCOM acceptance and recommended
repositioning the paper. October is a checkpoint with the paper written afterwards, so we
are optimising for a complete and honest result set rather than for venue novelty. The
positioning advice is worth keeping for later; it is not worth spending September on.

Two of its criticisms we also reject on the evidence. It claims our service-time residuals
are bimodal rather than log-normal, and they are not: log-residual skew runs 0.30 to 0.61
and excess kurtosis minus 0.66 to plus 0.32. The distributional form is right and only the
scale is wrong. It also cites our own claim that prefill is flat in concurrency as though it
were unconditional, and the truth is more interesting than either version; see
[`evidence.md`](evidence.md).
