# Analysis plan

**Frozen 2026-09-16, before any of the five configured campaigns runs.** It states how every
number is computed and what each possible outcome licenses us to say. Amendments go in
section 9, with a date and the measurement that forced them. An analysis choice made after
seeing a result, and not recorded there, is a result we do not report.

The implementation of everything in sections 2 to 5 is `tools/campaign_summary.py`, and its
module docstring is the executable copy of these rules. If the two ever disagree, the code is
what ran, and one of the two is a bug.

---

## 1. What a number is attached to

| Term | Definition |
|---|---|
| Run | One policy, one operating point, one staleness value, one repeat, one trace replay, one scheduler process |
| Cell | One policy at one point, pooling that point's repeats |
| Point | One operating point in one run set, holding every policy |
| Measured request | A request whose intended arrival is at or past the warmup boundary (10 s) |
| Latency rows | Measured requests with status `ok` |
| Failure | Any request in the run, warmup included, that is not delivered `ok`. A response lost in warmup is still a lost response |

## 2. Two gates, tested separately

**Validity** is a property of the run and comes from the manifest. A run is invalid if any
measured request's send lag exceeds its bound, any measured request is dropped, the engine
changed or died inside the run, two pool nodes shared a host, or the engine could not be
checked. Invalid runs are named in the summary, excluded from every statistic, and reported
as saturation where that is what they are.

**Steady state** is a property of a cell. A cell is transient when the mean latency of the
last third of its measured arrivals is at least 10% above the first third's and the 95%
interval of that ratio excludes 1. Transient cells are reported with their numbers and a
`T` mark, and are excluded from every contrast. No interaction and no calibration gain is
computed at a point where any cell it uses is transient.

A valid run can be transient. A mean taken off a filling queue scales with how long the run
lasted, so it is not comparable with anything.

## 3. Resampling

Every interval is a 95% percentile bootstrap with 2000 draws. Each draw does two things:

1. **Repeats are resampled with replacement, jointly across policies.** Repeat *k* of every
   policy at a point shares an arrival sequence and, from the seeded campaigns on, a
   scheduler seed. The pairing is what makes a contrast a contrast.
2. **Arrival positions are resampled with a circular block bootstrap, and the same positions
   are applied to every policy and repeat.** Block length is
   `max(ceil(n^(1/3)), ceil(2 * tau_int))`, where `tau_int` is the largest integrated
   autocorrelation time (Sokal window, in requests) of the detrended latency series over the
   cells at that point, capped at n/5. Detrended, so a climbing cell does not set the block
   length for the stable ones.

When every repeat replayed one trace with one scheduler seed, step 1 samples hardware jitter
only. The summary records this as `arrivals_independent: false`, and every interval from such
a set is conditional on one arrival path and one routing random stream. The first pair's four
campaigns are all in that state and say so.

## 4. Statistics

| Hypothesis or campaign | Primary | Secondary |
|---|---|---|
| H1 | Interaction on log mean end-to-end latency, `log(wjsq/jsq) - log(static_weighted/round_robin)`, at steady-state points | The same in ms; the same on p50, p95, p99 |
| Calibration gain, per router | Ratio of calibrated to uncalibrated mean latency (`sw/rr`, `wjsq/jsq`) | The difference in ms |
| Value of calibration (K2) | Mean end-to-end latency per arm, as a ratio to the JSQ arm at the same point | p95 ratio; SLO attainment at 2x |
| Shapes at matched load (K4) | `wjsq/jsq` per shape, at matched slow-node utilisation | `sw/rr` where defined; the ms difference |
| Staleness (K5) | `wjsq/jsq` and the interaction at each staleness value | Share routed to the fast node |
| Heavy-tailed and bursty (K5) | `wjsq/jsq` against the uniform-length trace at the same utilisation | SLO attainment at 2x and 5x, TPOT |
| Simulator validation | The contrasts in section 6 | Absolute p50 and p95 error |

The millisecond version of any contrast is never primary. A difference in milliseconds grows
with the latency it sits on, so a queue-blind policy near its stability limit inflates it
without anything changing about how calibration and queue depth combine.

**Serving metrics.** TTFT is worker-side, queue wait plus prefill. TPOT is decode time over
output tokens minus one. Delivery is not streamed, so neither includes the network. SLO
attainment is the share of measured requests, failures counted as misses, within 2x and 5x of
what the fastest node in the pool takes for that request's bucket at concurrency 1 in its cost
model.

**Load is reported as utilisation, not req/s**, in two forms: the pool's summed four-slot
capacity on that workload, and the load the cell put on the slow node. Both come from
`tools/pool_load.py`, from the same cost-model cells the campaign driver used to set the rate.
Where the two disagree with the rate that was actually achieved, the achieved figure is
reported beside it.

## 5. What we do not report

- `routing_error_ms` and any rate derived from it. It scores each decision with WJSQ's own
  formula on the view the policy saw, so WJSQ's rate is zero by construction and Threshold's
  is highest in cells where Threshold has the lowest latency.
- `queue_wait_ms` as a dependent variable. With four slots, contention appears as longer
  service time.
- Percentiles of transient cells beside percentiles of steady ones.
- p95 and p99 as primary anywhere. Percentile bootstrap intervals are unreliable where the
  percentile lands on a cluster of near-identical latencies, which happens on these traces.

**Multiplicity.** One primary statistic per hypothesis and per campaign, listed in section 4.
Everything else is secondary and is written as an observation rather than as a test. We do not
count a secondary interval excluding zero as a finding on its own.

## 6. Decision rules

Written before the runs. Each row says what the campaign is allowed to conclude. "Separated"
means the paired 95% interval of the difference excludes zero.

### 6.1 Value of calibration, `hw_calibration_ablation_3050.json` (K2)

Arms: `jsq`, `jsq_fastfirst`, `static_weighted_wrr`, `wjsq` at believed ratios 1.0, 1.2, 1.57,
2.5, 3.35, 5 and 100, `threshold`, `ect` in both modes. One load point, three seeded repeats.
The curve is reported in full whatever its shape.

| Outcome | Conclusion licensed |
|---|---|
| `wjsq(1.57)/jsq_fastfirst` interval entirely below 1 - d | Calibrated magnitude adds more than d beyond the ranking bit. Headline A |
| The interval inside [1 - d, 1 + d] | The ranking is what pays, and the magnitude adds or costs no more than d. Headline B |
| The interval entirely above 1 + d | Calibrated magnitude costs latency against the ranking alone. Reported as reversed, and neither headline is used as written |
| The interval crosses 1 - d or 1 + d | Inconclusive. Neither headline is licensed, and the paper reports the interval against the margin |
| `ect` beats the best `wjsq` arm, separated | The scalar was the limit rather than calibration itself, and the paper says which policy earns the profile |
| The curve's minimum sits at a believed ratio far from the operating one | We report how wrong an estimate can be before it stops paying, which is the quantitative form of K2 |
| Every arm within one interval of `jsq` | Calibration buys nothing measurable on this pool at this load, reported as a null with its interval |

The ratio is `wjsq` over `jsq_fastfirst` on mean end-to-end latency in the baseline arm, with
its paired 95% interval, and the equivalence margin d is 0.05. A separated gain smaller than d
is Headline B, not A: A claims the router gains X% from calibrated magnitude, and a gain inside
the margin is what B's "adds no more than X%" covers. `tools/campaign_summary.py` computes the
outcome as `headline_6_1` at the ablation's point.

### 6.2 Seeded anchor, `hw_seeded_anchor_3050.json` (K3)

Three utilisation points, five repeats with independent arrivals and scheduler seeds.

| Outcome | Conclusion licensed |
|---|---|
| Interaction includes zero at 0.2 and 0.3 and excludes it at 0.4 | K3 as written: the interaction appears as the queue-blind router approaches its limit |
| Interaction excludes zero at every point | H1's interaction holds while the queue-blind router is stable, and K3 is reworded to say so |
| Interaction includes zero everywhere | With arrival and routing randomness included, the interaction is not detectable on this pool. Reported as a null, and K3 leaves the ladder |
| StaticWeightedWRR differs from StaticWeighted by more than its interval | The first pair's queue-blind bracket was a property of one random stream, and only the WRR numbers are quoted from then on |

### 6.3 Staleness, `hw_staleness_h1_3050.json` (K5)

| Outcome | Conclusion licensed |
|---|---|
| `wjsq/jsq` falls as staleness rises, separated between 0 and 5 s | Queue depth substitutes for calibration only while it is fresh, and we state the timescale |
| `wjsq/jsq` flat across 0, 1 and 5 s | The substitution is robust to queue staleness at these timescales, which strengthens the null and bounds it to the range tested |
| Interaction changes sign | Reported as measured, with the mechanism left open |

### 6.4 Heavy-tailed and bursty, `hw_heavytail_3050.json` (K5)

Compared against the seeded anchor at the same pool utilisation, with `tools/compare_sets.py`.
A campaign that runs several workloads into one run set names them `runset.parquet#workload`,
and the workload is part of what a point is, so Poisson and MMPP at one utilisation stay
apart even though they share a mean rate. Sets are different runs, so a difference between
two sets' ratios is taken on independent draws and is separated when its interval excludes
zero.

| Outcome | Conclusion licensed |
|---|---|
| `wjsq/jsq` materially lower under heavy tails, separated | The first pair's small residual was a property of near-constant, known request sizes, and the paper says the redundancy does not survive realistic size variation |
| `wjsq/jsq` unchanged | Redundancy holds when sizes are variable and unknown at dispatch, which is the strongest version of the null we can measure |
| MMPP differs from Poisson at the same utilisation | Burstiness is reported as its own axis, with the tie rate beside it |

### 6.5 Shapes at matched load, `hw_shapes_matched_3050.json` (K4)

`tools/compare_sets.py --ordered`, over the three workloads of the one run set. Monotone means
the three ratios fall in the order named; the extremes are separated when the difference
between the first and the last excludes zero.

| Outcome | Conclusion licensed |
|---|---|
| Ordering generation, balanced, summarisation holds with the extremes separated, at matched slow-node utilisation | K4 stands as a shape result, and the elevation is a measured finding |
| Ordering collapses or reverses | The first pair's shape trend was a load effect. K4 leaves the ladder, and the phase content of the paper reduces to K1, which still stands |
| Ordering holds but the extremes are not separated | Reported as a trend consistent with K4 and not established by it, with the interval quoted |

### 6.6 Simulator validation on contrasts (gates H2)

Run through `tools/p4_validate.py --contrasts` against the three held-out shape run sets, per
policy and point, on steady cells only. It replays each hardware run, summarises the replays
with `campaign_summary.py` exactly as the hardware was summarised, and applies the three tests
below in `tools/contrast_check.py`, which exits non-zero when any of them misses. A ranking
miss between two policies whose hardware intervals overlap is still a miss, and is listed
separately so a reader can see whether the simulator disagreed with the hardware or with its
noise.

Passes when all three hold:

1. The policy ranking on mean latency matches the hardware's at every point.
2. `wjsq/jsq` from the simulator lies inside the hardware's 95% interval at every point.
3. The H1 interaction from the simulator lies inside the hardware's 95% interval wherever the
   hardware's is defined.

Absolute p50 and p95 within ±25% is reported, and is not sufficient on its own: a simulator
can pass ±25% on every policy and still get a 10 to 18% contrast wrong.

If it fails, every simulator figure is labelled illustrative, H2 leaves the claims ladder, and
the paper states the failure and what it means.

A failure on contrasts is a failure of the numbers the simulator produces, not evidence that
it routes by a different rule. Those are separate questions, and the second one is answered
by `tools/parity_check.py`, which pairs each hardware run with its own replay and compares
the decisions both vehicles made on the same state and the same tie-break draw. It is
reported beside 6.6 so that a contrast miss is read as a cost-model or queueing difference
rather than as policy drift.

## 7. The two headline sentences

Both written now, so that the outcome of 6.1 changes one sentence and nothing else about the
paper's structure. X and Y are filled from the runs.

**Headline A, if calibrated magnitude pays.**

> On a pool of two consumer GPUs serving one pinned engine, a router that already sees live
> queue depth still gains X% of its mean latency from calibrated capability, and the gain is
> largest on the workload where the machines differ most. The gain persists under stale queue
> counts and variable request sizes, and an estimate wrong by more than Y stops paying.

**Headline B, if the ranking is what pays.**

> On a pool of two consumer GPUs serving one pinned engine, nearly everything hardware
> calibration buys a queue-aware router is recovered by knowing only which node is faster.
> Calibrated magnitude adds no more than X% of mean latency, and a per-request cost model adds
> no more than Y%, across believed ratios from homogeneous to 100x, stale queue counts, and
> request sizes that are variable and unknown at dispatch.

## 8. Reporting rules that apply to every campaign

1. The run set's `summary.json` is the source of truth. A number in prose that disagrees with
   it is wrong.
2. Every table says whether its intervals include arrival randomness (`arrivals_independent`).
3. Every transient or invalid cell is shown and marked, never dropped silently.
4. Anchor-trace results are development results and are labelled as such. The three shape
   traces and the heavy-tailed traces are held out from simulator and cost-model development.
5. Withdrawn numbers stay listed in `results.md` so that they are not re-quoted from an old
   figure.
6. Superseded summaries keep the `summary_iid_v1.*` name and are never cited.

## 9. Amendment log

| Date | Change | Measurement that forced it |
|---|---|---|
| 2026-09-16 | Plan frozen | None. Initial |
| 2026-09-24 | 6.1 gains an equivalence margin of 0.05 and four outcomes (A, B, reversed, inconclusive) in place of "separated" and "not separated" | None; made before the ablation runs. Audit item C2: "not separated" read an interval that includes 1 as evidence of no effect, so with three repeats B would have won whenever the campaign was too small to tell. 0.05 is under half the smallest queue-aware calibration gain the first pair measured (WJSQ/JSQ 0.738 to 0.900) and wide enough for three seeded repeats to land inside |
