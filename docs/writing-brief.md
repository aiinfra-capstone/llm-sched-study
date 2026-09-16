# Writing brief

For the writing team. What the study now is, what we can claim and on what evidence, what we
contrast against, and every figure and table with its provenance. Snapshot of
**2026-09-15, evening**, after all four hardware campaigns on the first pair finished (132
valid runs) and after an internal audit of the design and the analysis. The audit changed the
statistics, the headline and several claims; section 3 lists what changed and why, and every
number in section 5 was re-derived under the new rules. Sections marked **pending** need work
that has not run yet; nothing in them should be written into the paper yet.

Numbers here come from committed code and the run sets named beside them. If a number in the
draft disagrees with a run set's `summary.json`, the run set wins. The previous summaries are
kept beside each run set as `summary_iid_v1.{json,md}`; do not quote them.

---

## 1. The study in one paragraph

Small labs and edge deployments serve language models on whatever machines they own, and
those machines differ by an order of magnitude or more. The textbook fix is calibration:
measure each node's speed and weight routing by it. We ask whether that measurement buys
anything a scheduler cannot already read from live queue depth, and we answer with a
two-by-two factorial of routing policies (queue-blind or queue-aware, hardware-blind or
hardware-aware) on real consumer GPUs serving the same pinned engine, model and
quantisation, backed by a discrete-event simulator that runs the same policy code. The
elevation is that "how much faster" is not one number: the two phases of a request do not
slow down by the same factor from one machine to the next, so the heterogeneity a scheduler
faces depends on the workload's prompt-to-output mix, and so, we expected, does the value of
calibration.

## 2. Research question and hypotheses (final wording)

> In a pool of consumer machines whose per-node throughput is heterogeneous, non-stationary
> and known only through stale estimates, does explicit hardware calibration improve
> scheduling beyond what live queue depth already reveals; and how does the answer move with
> the prompt-to-output ratio of the workload, given that prefill and decode do not degrade
> at the same rate across machines?

| | Claim |
|---|---|
| **H1** | Calibration is largely redundant given queue-awareness, and more so as load rises. The interaction `(WJSQ - JSQ) - (StaticWeighted - RoundRobin)` is positive. |
| **H2** | The advantage of hardware-aware routing is non-monotonic in the heterogeneity ratio R: it rises, peaks and falls toward zero, where Threshold(T) matches it. |
| **H3** | Routing quality degrades as a node's estimate age approaches the autocorrelation time τ of its throughput. |
| **Elevation** | The value of calibration moves with the workload's prompt-to-output ratio, in the direction set by which phase the pool's machines differ on. |

Two definitions in this table differ from the frozen specification PDF, and the paper has to
say so (section 3, item 12).

The policies:

| | Queue-blind | Queue-aware |
|---|---|---|
| **Hardware-blind** | RoundRobin | JSQ |
| **Hardware-aware** | StaticWeighted | WJSQ, score `(pending + 1) / capability` |

Plus Threshold(T): round-robin over nodes whose capability is at least T.

## 3. What changed since the specification

Write these into the method section; several are findings in their own right.

1. **The second machine is a laptop GPU, not a CPU box.** The pool is a GTX 1650 Ti 4 GB
   laptop and an RTX 3050 6 GB laptop (Dell, i5-13450HX), the second booted from an external
   SSD. The planned 4 GB CPU-only machine was not used, and the RTX 4070 desktop the same SSD
   was built for was not available for these runs.
2. **The prediction's direction was reset after the pair was chosen, and we say so.** The
   elevation was written against a CPU and partially offloaded GPU pair, where decode
   differed more than prefill, and `scope.md` predicted that queue depth fails on
   decode-heavy work. Our GPU pair is the opposite (prefill R about 10x, decode R 1.2x at one
   slot), and the prediction was restated as "direction relative to the pair" after that was
   known. A reviewer will see this as a post hoc change; state it as one.
3. **Capability was redefined, and it is still a one-slot number.** Both vehicles used to
   seed capability with decode tok/s at the lowest cost-model cell, which made this pool look
   1.18x apart. It is now output tokens per second of total service time at the same cell
   (`com.sched.core.Capability`), 1.57x apart. That cell is the least heterogeneous number
   the snapshots contain: at four slots the same cell is 3.35x apart, and R on service for
   the four workloads at four slots is 2.2 to 4.4 (Table 1). The policies were therefore given
   a scalar that understates the heterogeneity they operated under by about 2x, and the same
   1.57 weighting on every workload. Every hardware run in this brief used it.
4. **Engine context size is pinned.** llama-server sizes its context to fit VRAM when none
   is given, which gave the two nodes 13,824 and 30,720 tokens per slot from one command. All
   nodes now run `-c 55296` (13,824 per slot). The 1650 Ti's cost model was calibrated on
   2026-08-31 before this pin, under driver 580.173.02; the campaigns ran on 580.178.04.
5. **Load points were set against the pool, and chosen so RoundRobin would fail.** The anchor
   campaign's config says "round_robin alone crosses the 1650 Ti's onset at heavy". Measured
   against the pool's four-slot capacity on each workload, the same req/s is a different load
   on each shape (Table 8). Future campaigns set load as a fraction of capacity
   (`tools/pool_load.py`).
6. **Threshold T sits between the two classes** (130 tok/s against 103.9 and 163.6), so
   Threshold serves the 3050 alone.
7. **The network is 2.4 GHz Wi-Fi** hosted by the harness laptop, with client power saving
   off (RTT about 4 ms). The transport residual differs by node: 5.4 to 7.2 ms on the 1650 Ti,
   which shares a host with the harness, and 8.7 to 15.5 ms on the 3050 over Wi-Fi. The
   simulator's 5.86 ms was measured on the single-node loopback setup.
8. **Engines were restarted between runs, and the restarts were not logged per run.**
   llama-server keeps an 8 GiB host-side prompt cache by default, which filled host memory
   over the shape campaigns and put the 1650 Ti host, which also runs the harness, into swap.
   Both engines were restarted with the identical command at run boundaries, about 12 times.
   No prompt was served from the cache. On the 3050, reconstructing engine start times from
   its logs places 118 of 132 runs, and its service time moved +0.05% per hour since restart
   [-0.19, +0.32] over 0 to 4.5 h (`runs/exp/restart_effect_rtx3050.json`). The 1650 Ti's
   engine logs were not kept, so the node that swapped is unchecked. `engine_restarts: 0`
   in these manifests cannot detect a restart between runs and proves nothing. Future runs set
   `--cache-ram 0`, and the driver records each engine's command line and start time.
9. **Every repeat replayed one arrival sequence with one scheduler seed.** No campaign
   manifest set `config.seed`, so the live scheduler used its default of 42 in every run, and
   every repeat replayed the same trace. StaticWeighted and Threshold routed identically in
   all three repeats, and JSQ and WJSQ drew the same tie-breaks. Run-to-run spread is small
   (JSQ on balanced at 2.4 req/s: 1176.9, 1175.3 and 1178.3 ms). So the repeats sample
   hardware jitter, and every interval in section 5 is conditional on one Poisson sample path
   and one routing random stream. The three shape traces also share one arrival stream.
10. **StaticWeighted sent more work to the fast node than its weights say.** Its nominal
    share to the 3050 is 163.6 / 267.5 = 61.2%. The measured share is 66 to 68% in every run
    of every campaign, because Java `Random(42)` happens to draw that way over the first 600
    decisions. The 1650 Ti got about 13% less traffic than the policy specifies, which helps
    StaticWeighted and enlarges the queue-blind calibration gain.
11. **Statistics were changed after the audit, before re-deriving anything.** The first
    version resampled requests independently within runs and resampled policies
    independently. Latencies inside a run are autocorrelated (lag-1 up to 0.73 for Threshold
    at a steady point), and policies share arrivals. The rules now (docstring of
    `tools/campaign_summary.py`): repeats resampled jointly across policies, then a circular
    block bootstrap over arrival positions shared by every policy and repeat, block length
    from the detrended autocorrelation time; a cell is transient when the last third of its
    arrivals averages at least 10% above the first third and that ratio's interval excludes 1;
    no contrast is computed at a point where a cell it uses is transient; the primary H1
    statistic is the interaction on log mean latency, with the millisecond version secondary.
12. **Two definitions deviate from the frozen specification.** The spec's formal H1 statement
    is `(WJSQ - JSQ) < (StaticWeighted - RoundRobin)`, which with both brackets negative when
    calibration helps reads as the opposite of redundancy. The README was corrected in
    ffb2c4c; the PDF was not. The spec's H2 observable is WJSQ - JSQ converging to Threshold;
    the figures compute best hardware-aware minus best hardware-blind. The H2 figure now draws
    both, side by side, with the gap to Threshold(T) on each, so the deviation is visible in
    the figure rather than only in prose. Both still go in the paper as deviations with the
    reason, and in the spec record: the PDF is the authority and it has not been changed.
13. **One response path could lose responses silently.** In
    `phase_summarisation_1650ti_3050_jsq_s0_heavy_r3`, three requests on the 1650 Ti were
    served in 2.2 to 3.3 s and the client recorded 60 s timeouts. The worker's cached channel
    to the client was in reconnect backoff from the previous run, and a call made then fails
    without trying. They fell inside warmup, so the run stayed valid. The worker now waits for
    the channel and counts undelivered responses, and the campaign driver counts failures over
    the whole run. The run is kept in the tables; its measured window has no failure.

## 4. Setup and method (for the methods section)

| | |
|---|---|
| Engine | llama.cpp tag b10569, commit 5a32f7b, one patch (a 500 on `/completion` when generation ends mid-character); recorded as `b10569+p1+cuda13.2`. The 1650 Ti binary reports build 1: same commit and patch by our record, CMake flags unknown |
| Model | Llama-3.2-1B-Instruct, GGUF Q4_K_M, identical bytes on every node (SHA-256 checked at install; not recorded per run in these manifests) |
| Engine settings | `-ngl 99 --threads 6 --parallel 4 -c 55296` on both nodes |
| Nodes | GTX 1650 Ti 4 GB (Ryzen 5 4600H) and RTX 3050 6 GB Laptop GPU (i5-13450HX, 85 W cap, performance profile), both on AC |
| Harness | scheduler (Java, gRPC) and open-loop replay client on the 1650 Ti laptop, beside that node's `--threads 6` engine |
| Admissible envelope | prompt ≤ 512, output ≤ 128 tokens. The 1650 Ti snapshot declares 2048/256 but only 512/128 was sampled |
| Clock discipline | chrony on both hosts, measured into every manifest; worst pair offset about 2 ms, rate difference ≤ 0.13 ppm; no cross-host duration is ever computed |
| Validity | a run is discarded if any request's send lag exceeds its bound, any request in the measurement window is dropped, the engine restarts inside the run, or two pool nodes share a host. The three invalid runs (RoundRobin, summarisation, 3.2 req/s) are reported as saturation. Validity is a separate test from steady state (section 3 item 11): a valid run can still be transient |
| Anchor trace | 200 requests, Poisson at a base 0.9 req/s scaled per point, buckets p128/o64 (50%), p256/o64 (30%), p512/o128 (20%), mean prompt-to-output ratio 3.0. `gen_seed` 20260830. This trace was also used to place the cost-model grid, tune the simulator for F-23 and choose the load points, so anchor results are development results |
| Shape traces | 600 requests each, identical arrivals and priorities, lengths only differ; ratios 13.76 (summarisation), 2.00 (balanced), 0.50 (generation). They played no part in building the simulator or the cost model, so they are the held-out set for its validation. Their arrivals differ from the anchor trace's |
| Order | 3 repeats per policy and point; points run lightest first; policy order shuffled per point from a fixed seed. Each shape ran as its own campaign, in sequence, over about 11 hours |
| Output length | forced exactly (`ignore_eos`, `n_predict`); prompts are random tokens; prefix caching off |
| Queue information | at staleness 0 the policies read the scheduler's own dispatch and completion counters, with one dispatcher |
| Warmup | the first 10 s of each run excluded |
| Statistics | section 3 item 11; 2000 draws |
| Simulator | discrete-event, same policy classes as the live scheduler, parameterised from the C-3 cost models. Validated on the single-node pool only (F-23), in sample |

**Calibration.** Each node class is calibrated on a 24-cell grid (three prompt buckets, two
output buckets, concurrency 1 to 4, 8 samples per cell) plus a 300 s sustained segment
sliced into 5 s windows. At concurrency 4 the 8 samples are two synchronised batches, and
capability comes from one cell measured once on one day.

## 5. Results we can write now

### 5.1 Throughput nonstationarity (MPR-1)

- **τ is not resolved on any class.** The CPU 8B class's point estimate is 69.5 s, and its own
  calibration record says `tau_resolved: false`: 31 windows of 48 s, a segment 21 τ long.
  `tools/tau_interval.py` gives lag-1 correlation 0.48 [0.03, 0.60], a block-bootstrap τ
  interval of 31 to 91 s, and a fit that finds no decay beyond one window in 66% of draws.
  Removing a linear trend (throughput fell 2.3% across the segment) gives 61.8 s. Write it as
  an estimate of the order of a minute, with its interval, on a class that is not in any pool.
- Both GPU classes are censored at the 5 s floor. The RTX 3050 held sustained throughput with
  a coefficient of variation of 0.009 across 60 windows.
- MPR-1 should not be marked done on this evidence. An 80-minute sustained segment on the 1B
  CPU class is configured (`calibration_1b_cpu.json`).

### 5.2 Heterogeneity depends on the workload and on concurrency

Table 1. RTX 3050 over GTX 1650 Ti. Cost-model columns from `tools/cell_intervals.py`
(`runs/exp/cell_intervals_3050_over_1650ti.json`, intervals resample calibration batches);
operating columns are slow over fast on the phase times the steady cells of the live runs
actually recorded at 2.4 req/s.

| Workload | Prompt:output | R service, 1 slot | R service, 4 slots | R prefill, 1 slot | R decode, 1 slot | R decode, 4 slots | Operating R service | Operating R decode |
|---|---:|---|---|---:|---|---|---:|---:|
| generation | 0.50 | 1.39 [1.39, 1.40] | 2.17 [1.82, 2.57] | 8.64 | 1.19 | 1.69 [1.44, 1.98] | 1.51 | 1.44 |
| balanced | 2.00 | 1.58 [1.57, 1.58] | 2.57 [2.14, 3.07] | 9.79 | 1.20 | 1.70 [1.44, 1.99] | 1.80 | 1.52 |
| anchor | 3.00 | 1.79 [1.79, 1.80] | 3.02 [2.67, 3.39] | 9.91 | 1.19 | 1.70 [1.54, 1.87] | 2.24 | 1.79 |
| summarisation | 13.76 | 2.59 [2.57, 2.60] | 4.41 [3.56, 5.41] | 11.12 | 1.20 | 1.71 [1.45, 2.01] | 4.57 | 2.98 |

What it supports: the same two machines look 1.39x or 2.59x apart at one slot depending only
on the workload, and 2.2x to 4.4x at four slots. What it does not support: that decode is
nearly homogeneous on this pair. That holds at one slot only. The 3050 gains about 2.85x
aggregate decode throughput from batching and the 1650 Ti about 1.9x, so under load the pair
differs on decode too, in a way that looks like batching efficiency. Drop the
memory-bandwidth explanation. The one-slot intervals are narrow because the engine is nearly
deterministic at fixed load; they carry no day-to-day variation, since each class was
calibrated once.

Separately, the 1650 Ti's prefill (about 735 tok/s on this model) has not been checked
against published numbers for the card, and its build flags are unknown. Until
`tools/engine_bench.sh` has been run on both nodes before and after a rebuild, the 10x prefill
R cannot be called a pure hardware property.

### 5.3 The cost model predicts the live pool

`costcheck` over the 45 anchor runs: 24 exercised cells, 7,905 requests, request-weighted
absolute error 17.4% against the ±25% tolerance (7.3% on medians). Three cells fall outside,
worst 29%, all at concurrency 1 or 2. The grid was placed on the anchor trace's lengths, so
this is an in-sample check.

### 5.4 H1 on the anchor trace

Run set `runs/exp/mpr2_1650ti_3050`, 45 runs, all valid; tables in `summary.md` beside it.
No cell on this trace is transient under the rule.

Table 2. Mean end-to-end latency in ms [95% interval].

| Policy | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| RoundRobin | 916 [814, 1023] | 1516 [1330, 1713] | 2949 [2656, 3243] |
| StaticWeighted | 788 [710, 874] | 871 [777, 967] | 996 [872, 1132] |
| JSQ | 734 [678, 794] | 873 [795, 959] | 1011 [902, 1134] |
| WJSQ | 632 [594, 672] | 761 [691, 834] | 832 [759, 916] |
| Threshold (3050 only) | 655 [597, 712] | 755 [698, 808] | 851 [775, 931] |

Table 3. p95 end-to-end latency in ms [95% interval]. Percentile bootstrap intervals on p95
are unreliable where the p95 lands on a cluster of near-identical latencies; treat p95 as
secondary.

| Policy | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| RoundRobin | 2689 [1830, 3173] | 4443 [3730, 5228] | 7396 [6723, 8532] |
| StaticWeighted | 1654 [1307, 2441] | 1859 [1584, 2212] | 2120 [1749, 2521] |
| JSQ | 1632 [1376, 1718] | 1916 [1621, 2237] | 2294 [1815, 2971] |
| WJSQ | 1119 [900, 1611] | 1616 [1500, 1761] | 1615 [1354, 2043] |
| Threshold (3050 only) | 1244 [1006, 1522] | 1385 [1236, 1492] | 1538 [1331, 1655] |

Table 4 is folded into Table 7.

What the tables support:

1. **The interaction is zero where RoundRobin's slow node is lightly loaded.** At 1.3 req/s
   the log interaction is +0.002 [-0.117, +0.116]: StaticWeighted over RoundRobin is 0.860 and
   WJSQ over JSQ is 0.862. Calibration removes the same 14% with or without queue depth.
2. **It is positive, on the ratio scale too, as RoundRobin's slow node fills.** At 2.4 req/s
   (1650 Ti under RoundRobin at 0.73 of its four-slot capacity) it is +0.417 [+0.241, +0.582];
   at 3.2 (0.97) +0.891 [+0.704, +1.088]. The 3.2 point passes the steady-state rule only
   because a 200-request run is too short to show the climb; offered load on that node is
   within 3% of its capacity. What grows with load is RoundRobin's latency, not how
   calibration and queue depth combine for a router that is stable.
3. **Redundant is not useless.** WJSQ beats JSQ at every point, by 102 to 178 ms on the mean,
   intervals excluding zero (WJSQ/JSQ 0.82 to 0.87).
4. **Where work goes.** RoundRobin sends 50% to the 3050, JSQ 57 to 63%, StaticWeighted 67 to
   68% (nominal 61%, section 3 item 10), WJSQ 72 to 73%, Threshold 100%.

Do not write: that H1 "strengthens with load"; that calibration is unnecessary; that Threshold
is best in general.

### 5.5 Simulator validity

F-23 on the single-node 1650 Ti pool: p50 error -16.5% to -19.4% at the three steady-state
anchors and -7.8% at 1.98 req/s, which is transient and whose percentiles are not quoted.
The fixes that brought it inside ±25% were chosen while watching this error, so it is in
sample. A simulator can pass ±25% on every policy and still get a 10 to 18% contrast wrong.
Validation on the two-node pool is **pending**, and its criterion is being changed to the
contrasts: policy ranking, the sign and rough size of JSQ - WJSQ, and the interaction, within
the hardware intervals, on the shape runs as held-out data.

### 5.6 Workload shapes on hardware

Run sets `runs/exp/phase_{generation,balanced,summarisation}_1650ti_3050`, 30 runs each at 2.4
and 3.2 req/s (5 policies, 3 repeats, about 570 measured requests per run). 87 of 90 runs are
valid; the three invalid runs are RoundRobin on summarisation at 3.2 req/s, saturated. **T**
marks a transient cell.

Table 5. Mean end-to-end latency in ms [95% interval].

| Policy | generation 2.4 | generation 3.2 | balanced 2.4 | balanced 3.2 | summarisation 2.4 | summarisation 3.2 |
|---|---|---|---|---|---|---|
| RoundRobin | 1403 [1292, 1509] | 2713 [2273, 3162] T | 1859 [1547, 2214] | 6448 [5453, 7473] T | 6792 [5513, 8262] T | saturated |
| StaticWeighted | 1258 [1178, 1357] | 1464 [1353, 1589] | 1290 [1158, 1456] | 1576 [1421, 1745] T | 1308 [1063, 1599] | 3030 [2233, 3886] T |
| JSQ | 1268 [1198, 1346] | 1423 [1356, 1486] | 1177 [1115, 1242] | 1348 [1289, 1407] | 729 [682, 782] | 854 [792, 920] |
| WJSQ | 1128 [1068, 1189] | 1281 [1224, 1337] | 1019 [965, 1074] | 1138 [1090, 1189] | 560 [528, 594] | 630 [583, 683] |
| Threshold (3050 only) | 1282 [1182, 1407] | 3476 [2738, 4350] T | 1038 [976, 1110] | 1615 [1388, 1854] T | 440 [419, 463] | 503 [473, 536] |

Table 6. p95 end-to-end latency in ms [95% interval].

| Policy | generation 2.4 | generation 3.2 | balanced 2.4 | balanced 3.2 | summarisation 2.4 | summarisation 3.2 |
|---|---|---|---|---|---|---|
| RoundRobin | 2482 [2219, 2924] | 8366 [6799, 9217] T | 5127 [3579, 6491] | 21315 [20327, 23151] T | 22960 [22059, 24005] T | saturated |
| StaticWeighted | 2129 [1930, 2708] | 2796 [2273, 3787] | 2892 [2399, 4012] | 3994 [3408, 5312] T | 4751 [3964, 7183] | 14018 [11506, 15184] T |
| JSQ | 2098 [1988, 2179] | 2256 [2176, 2397] | 2272 [2168, 2393] | 2629 [2480, 2815] | 2241 [1852, 2522] | 2749 [2481, 3151] |
| WJSQ | 1766 [1575, 1875] | 1988 [1880, 2057] | 1851 [1572, 1983] | 2037 [1928, 2177] | 1370 [1012, 1693] | 1814 [1603, 2074] |
| Threshold (3050 only) | 2012 [1709, 2365] | 7917 [6851, 8427] T | 1567 [1368, 1825] | 3784 [2804, 4120] T | 663 [635, 719] | 768 [696, 891] |

Table 7. The H1 interaction on mean latency, every point on the first pair.

| Workload | Load | Pool utilisation | 1650 Ti under RoundRobin, offered / capacity | Interaction, log (primary) | Interaction, ms | Status |
|---|---|---:|---:|---|---|---|
| generation | 2.4 | 0.43 | 0.73 | -0.008 [-0.098, +0.069] | +5 [-109, +103] | defined |
| generation | 3.2 | 0.57 | 0.99 | | | RoundRobin transient |
| balanced | 2.4 | 0.49 | 0.93 | +0.221 [+0.089, +0.346] | +411 [+175, +669] | defined, and RoundRobin's climb is borderline (1.23, interval [0.99, 1.51]) |
| balanced | 3.2 | 0.65 | 1.26 | | | RoundRobin and StaticWeighted transient |
| anchor | 1.3 | 0.18 | 0.41 | +0.002 [-0.117, +0.116] | +27 [-69, +122] | defined |
| anchor | 2.4 | 0.34 | 0.73 | +0.417 [+0.241, +0.582] | +533 [+325, +742] | defined |
| anchor | 3.2 | 0.45 | 0.97 | +0.891 [+0.704, +1.088] | +1775 [+1450, +2118] | defined |
| summarisation | 2.4 | 0.36 | 1.06 | | | RoundRobin transient |
| summarisation | 3.2 | 0.49 | | | | RoundRobin saturated |

The previous brief quoted +5315 ms (summarisation 2.4) and +4662 ms (balanced 3.2). Both came
from RoundRobin cells whose latency climbed by about 9 s from the first third of the run to
the last, and whose means scale with run length. They are withdrawn.

Table 8. What calibration buys each router, on the mean. Ratio is calibrated over
uncalibrated latency. n/d: a cell of that router is transient or saturated. The last column
is the load the JSQ cell put on the 1650 Ti.

| Workload | Load | Queue-blind: SW / RR | Queue-blind, ms | Queue-aware: WJSQ / JSQ | Queue-aware, ms | 1650 Ti under JSQ, offered / capacity |
|---|---|---|---|---|---|---:|
| generation | 2.4 | 0.896 [0.852, 0.945] | 146 [72, 211] | 0.889 [0.850, 0.926] | 141 [92, 195] | 0.62 |
| generation | 3.2 | n/d | n/d | 0.900 [0.868, 0.931] | 142 [96, 191] | 0.78 |
| balanced | 2.4 | 0.694 [0.621, 0.786] | 568 [339, 811] | 0.866 [0.834, 0.896] | 158 [119, 200] | 0.72 |
| balanced | 3.2 | n/d | n/d | 0.844 [0.820, 0.869] | 210 [172, 248] | 0.89 |
| anchor | 1.3 | 0.860 [0.771, 0.957] | 128 [37, 226] | 0.862 [0.815, 0.907] | 102 [64, 145] | 0.36 |
| anchor | 2.4 | 0.575 [0.499, 0.670] | 645 [452, 842] | 0.872 [0.816, 0.924] | 112 [63, 166] | 0.58 |
| anchor | 3.2 | 0.338 [0.286, 0.398] | 1953 [1626, 2278] | 0.824 [0.759, 0.891] | 178 [103, 267] | 0.72 |
| summarisation | 2.4 | n/d | n/d | 0.769 [0.731, 0.808] | 168 [134, 205] | 0.66 |
| summarisation | 3.2 | n/d | n/d | 0.738 [0.691, 0.791] | 224 [172, 277] | 0.80 |

Table 9. The queue-aware gain against load, lightest to heaviest point. H1's second clause
predicts it shrinks.

| Workload | Loads (req/s) | JSQ - WJSQ, ms | Change, ms | WJSQ / JSQ | Change in ratio |
|---|---|---|---|---|---|
| anchor | 1.3 to 3.2 | 102 / 112 / 178 | +77 [-8, +172] | 0.862 / 0.872 / 0.824 | x0.956 [0.867, 1.051] |
| generation | 2.4 to 3.2 | 141 / 142 | +1 [-70, +70] | 0.889 / 0.900 | x1.013 [0.959, 1.073] |
| balanced | 2.4 to 3.2 | 158 / 210 | +52 [-5, +107] | 0.866 / 0.844 | x0.975 [0.929, 1.024] |
| summarisation | 2.4 to 3.2 | 168 / 224 | +56 [-8, +118] | 0.769 / 0.738 | x0.959 [0.884, 1.041] |

What the tables support:

1. **H1's "more so as load rises" clause is not supported.** In milliseconds the queue-aware
   gain grows with load on three of four workloads and is flat on generation; as a ratio no
   workload shows a change distinguishable from none. Nowhere does it shrink. Say this
   plainly in the paper.
2. **The interaction is defined at five of nine points, and it is zero at the two where
   RoundRobin's slow node is least loaded.** Ordered by the load RoundRobin puts on the
   1650 Ti: 0.41 (anchor 1.3) zero, 0.73 (generation 2.4) zero, 0.73 (anchor 2.4) +0.417,
   0.93 (balanced 2.4) +0.221, 0.97 (anchor 3.2) +0.891. The two points at 0.73 disagree,
   and they differ in R (2.2 against 3.0 at four slots) as well as in arrivals, so neither
   the load nor R can be separated as the cause on these runs. Write H1 as: calibration
   removes about the same fraction of latency with or without queue depth while the
   queue-blind router is comfortably stable, and a positive interaction appears as that
   router approaches its stability limit. Balanced at 2.4 req/s only just passes the
   steady-state gate, so treat it as the weakest of the three positive points.
3. **The elevation cannot be stated for queue-blind routing.** The previous "146 to 5484 ms,
   tracking R" rested on transient RoundRobin cells, and the three shapes at one req/s put
   RoundRobin's 1650 Ti at 0.73, 0.93 and 1.06 of its capacity. Load and R moved together.
   Among the shapes only generation and balanced at 2.4 req/s have a defined queue-blind
   gain, 146 ms and 568 ms, and their slow-node loads are 0.73 and 0.93. Two points that
   differ in both R and load are not a trend in R.
4. **The queue-aware relative gain does rise with R, and this is the candidate result.** WJSQ
   over JSQ is 0.889 on generation, 0.866 on balanced and 0.769 on summarisation at 2.4 req/s,
   and the generation and summarisation intervals do not overlap; at 3.2 req/s 0.900, 0.844
   and 0.738. The JSQ cells put the 1650 Ti at similar load on generation and summarisation
   (0.62 and 0.66 at 2.4 req/s), so this is less confounded with load than the queue-blind
   result, but not free of it. Two limits: every interval is conditional on one arrival path
   and one routing stream (section 3 item 9), and part of the relative change is a denominator
   effect (JSQ is 729 ms on summarisation and 1268 ms on generation). The matched-load
   campaign (P2b in `docs/checkpoint.md`) is what decides it.
5. **The best policy moves with the workload.** WJSQ has the lowest mean on generation and on
   balanced at both loads (on balanced at 2.4 req/s it ties Threshold, which has the better
   tail). On summarisation Threshold is best at both loads, on the mean (440 against 560 ms)
   and on p95 (663 against 1370 ms).
6. **Threshold holds only while the fast node alone has room.** Threshold is transient on
   generation and balanced at 3.2 req/s, and steady on summarisation at 3.2 req/s at 503 ms.
   This is consistent with H2's shape along the workload axis, and it is confounded with load
   headroom on a two-node pool. Leave the claim to the simulator sweep.
7. **Where work goes.** WJSQ sends 67 to 68% to the 3050 on generation, 70 to 71% on balanced
   and 77 to 78% on summarisation; JSQ 57 to 60%, 61 to 65% and 69 to 72%. StaticWeighted sends
   66% everywhere.

Do not write: that the value of calibration "tracks R" for queue-blind routing; that H1 holds
"on every shape"; that H1 strengthens with load; that Threshold beats WJSQ in general; H2 from
these runs.

### 5.7 Serving metrics

`summary.md` in each run set now carries worker-side TTFT (queue wait plus prefill), TPOT
(decode time over output tokens minus one) and SLO attainment at 2x and 5x the fastest node's
one-slot time for each bucket. Delivery is not streamed, so neither number includes the
network. Two things to write:

1. **E2E SLO attainment at 2x ranks the policies much as the mean does**, with WJSQ highest or
   tied on every workload except summarisation, where Threshold is at 98 to 100% against
   WJSQ's 77 to 78%.
2. **TTFT attainment at 2x is mostly the share routed to the 3050.** The 1650 Ti's prefill
   alone is 9 to 11x the 3050's, so almost any request placed there misses a TTFT target set
   by the fast node. JSQ and WJSQ sit at 56 to 75%, close to their shares; Threshold at 70 to
   98% where it is steady. A TTFT SLO on this pair is a routing-share metric, which is worth
   saying because it is where a policy that wins on mean latency can lose.

`queue_wait_ms` is no longer a dependent variable: with four slots, contention shows up as
longer service time. `routing_error_ms` is not reported: it scores each decision with WJSQ's
own formula on the view WJSQ saw, so WJSQ's rate is 0 by construction and Threshold's is
highest (0.60 on summarisation at 2.4 req/s) in the cell where it has the lowest latency.

## 6. What we contrast against

### 6.1 Inside the study

- **RoundRobin** is the hardware-blind, queue-blind floor.
- **JSQ** is the claim that queue depth is already a speed signal.
- **StaticWeighted** is calibration without queue information.
- **WJSQ** is both. The 2x2 is what lets us separate the two sources of gain, which a ranked
  list of policies cannot.
- **Threshold(T)** is the one-line static rule H2 says hardware-awareness collapses into.
- **`JSQFastFirst`** breaks JSQ's ties toward the fastest node instead of at random. JSQ ties
  in 30 to 53% of decisions on these runs, so this is the ranking bit of calibration without
  the magnitude, and WJSQ minus JSQ has to beat it to be about calibration at all.
- **`StaticWeightedWRR`** is StaticWeighted as deterministic weighted round-robin, so its
  routing share is the policy's share rather than one random stream's.
- **`ECT`** prices each request from the full cost model, in a mode that reads the request's
  output length and a mode that knows only a prior. It is the strongest calibrated baseline,
  and the one that makes "queue depth recovers most of it" a claim against something real.
- All three are implemented in both vehicles (92976e9), together with capability overrides.
  **Pending:** the runs. `hw_calibration_ablation_3050.json` runs them against WJSQ at
  capability ratios 1.0, 1.2, 1.57, 2.5, 3.35, 5 and 100, plus the decode-only definition
  and the four-slot reference cell, in one campaign of about 1.5 h.

### 6.2 Related work, and how we differ

Summaries below were checked against each paper's published abstract. Read the paper before
citing anything beyond them.

| Work | What it does | How we differ |
|---|---|---|
| Splitwise (Patel et al., ISCA 2024) | Splits prompt computation and token generation onto different machines, since the phases have different compute and memory demands | Uses phase asymmetry to place phases on datacenter GPUs with fast interconnect; we route whole requests on consumer machines with no such interconnect and ask what phase asymmetry does to the value of calibration |
| DistServe (Zhong et al., OSDI 2024) | Disaggregates prefill and decode onto different GPUs to remove interference, co-optimising resources per phase for TTFT and TPOT | Same distinction as Splitwise; their heterogeneity is in the workload's SLOs, ours is in the hardware |
| HexGen-2 (Jiang, Yan and Yuan, ICLR 2025) | Separates prefill and decode across heterogeneous GPUs, allocating them by constrained optimisation with graph partitioning and max-flow | Disaggregation plus hardware heterogeneity; we study whole-request routing and what information it needs, not phase placement |
| Mélange (Griggs et al., arXiv 2404.14527) | Picks the cheapest GPU mix for a service; finds no GPU type is most cost-efficient across all request sizes | Offline allocation that assumes profiles are worth having. We measure online whether a profile adds anything over queue depth. Their request-size finding is consistent with our phase-dependent R |
| Helix (Mei et al., ASPLOS 2025) | Max-flow and MILP formulation for placement and request scheduling over heterogeneous GPUs and networks | Optimises with a performance model; we question when the model earns its keep |
| HexGen (Jiang et al., ICML 2024) | Asymmetric tensor and pipeline partitioning across heterogeneous GPUs with a constrained-optimisation scheduler | Model partitioning across GPUs; our nodes each hold a whole model |
| Llumnix (Sun et al., OSDI 2024) | Runtime rescheduling across instances with live migration of request state | Reactive rebalancing within homogeneous-model fleets; we study the dispatch decision and its information |
| NexusSched (Zhang et al., arXiv 2509.23384) | Argues queue length and average latency are lagging signals under heterogeneity; routes on a predictive per-step performance model | The closest contrary position. We test the premise with a factorial rather than assume it, and find queue depth recovers most of the gain on our pool |
| vLLM Router, llm-d | Production routers weighing prefix-cache hits, KV state and live load | Built around cache locality across replicas; whether hardware calibration adds to live load is not the question they report on |
| RouteBalance (Da and Kalyvianaki, arXiv 2606.17949) | Joins model selection and load balancing across heterogeneous model instances | Heterogeneity of models and quality; ours is one model on different hardware |
| Solyx AI Grid (Bernhard and Katla, arXiv 2606.15050) | Weighted routing on GPU telemetry, application metrics and network signals across datacenters | Multi-signal production routing against round-robin; no decomposition of which signal pays |
| Mitzenmacher, TPDS 2000 | Load balancing on stale queue information; small amounts of old information help, herd behaviour when stale | The classical basis for H3; we measure staleness against the real τ of LLM nodes |
| Speed-aware JSQ (arXiv 2203.01721; Stochastic Systems) | SA-JSQ is asymptotically delay-optimal for heterogeneous servers | Asymptotic, scalar speeds, large N. Our speeds are phase-dependent, non-stationary, and N is small |
| Luo and Zubeldia (arXiv 2510.14284) | Stability and heavy-traffic optimality of JSQ, JSED and power-of-d in heterogeneous systems; stability need not be monotone in arrival rate | Theory for the same policy families; we supply measured service distributions |
| Lin et al. (arXiv 2602.02987) | Stochastic control for prefill-decode contention with gate-and-route policies at scale | Contention inside a GPU cluster; we take contention as part of each node's measured cost |
| Usami et al. (arXiv 2606.17104) | Accelerator advantages differ by phase: GPUs lead prefill, GroqRack leads decode TPOT | Independent evidence that "faster" depends on phase; they characterise, we route |
| Tummalapalli et al. (arXiv 2603.23640) | Sustained-load behaviour of a 1.5B model on edge devices, including thermal loss of throughput | Supports MPR-1's framing that a single tok/s figure decays; they do not route |
| Petals (Borzunov et al., ACL 2023 demo) | Collaborative inference over consumer GPUs by splitting a model into blocks | Consumer hardware, but model splitting rather than whole-request routing |


### 6.3 What we can claim as new

Stated narrowly, and only in the form the evidence supports:

1. A factorial decomposition of what hardware calibration buys beyond queue depth, measured on
   real heterogeneous consumer LLM nodes with a pinned engine, with steady-state gating and
   intervals paired by arrival. In the work reviewed above, calibration is assumed
   (placement and routing systems) or modelled asymptotically (queueing theory); we did not
   find it measured this way.
2. On one pair of machines: while the queue-blind router is stable, calibration removes about
   the same fraction of latency with or without queue depth (interaction zero on the ratio
   scale), and a queue-aware router keeps a residual gain from calibration of 10 to 26% that
   is larger, as a fraction, on the workload where the machines differ most. Pending the
   seeded and load-matched campaigns.
3. The heterogeneity a scheduler faces depends on the workload and on concurrency: 1.4x to
   2.6x at one slot and 2.2x to 4.4x at four, on one pair of GPUs.

The previous claim 3 ("the choice of capability number moved the pool from 1.18x to 1.57x")
is a methods note, not a contribution. The capability-ratio sweep (pending) is what would
replace it: a curve of the value of calibration against how wrong the estimate is.

Do not claim: that phase asymmetry is new (Splitwise, DistServe); that we propose a new
scheduler; that queue depth makes calibration redundant in general. The workload is the
regime most favourable to JSQ (near-constant, exactly known output lengths, Poisson
arrivals, exact queue counts from one dispatcher, two nodes), and nothing here says the
result carries outside it; the heavy-tailed, bursty and stale-queue campaigns are what test
that. No generality beyond 1B models, four slots and two nodes.

## 7. Figures and tables

Paper figures are drawn by `tools/paper_figures.py` from the re-derived `summary.json` files.

| # | File | Shows | Status |
|---|---|---|---|
| F1 | `figures/paper/phase_ratio.png` | R on service, prefill and decode per workload, one slot | ready; add the four-slot column from Table 1 before use |
| F2 | `figures/paper/latency_by_policy_anchor.png` | p50, p95, p99 per policy against load, with intervals | ready (re-rendered) |
| F3 | `figures/paper/h1_interaction.png` | H1 interaction on log mean (primary) and in ms, steady points only; lists the points not drawn | ready (redrawn) |
| F4 | `figures/paper/routing_share_anchor.png` | share of requests to the fast node per policy | ready |
| F5 | `figures/paper/calibration_gain_by_shape.png` | calibrated over uncalibrated latency and ms saved, per router and shape; n/d where a cell is transient | ready (redrawn); caption must say load is not matched across shapes |
| F5b | `figures/paper/latency_by_policy_{generation,balanced,summarisation}.png`, `routing_share_{…}.png` | as F2 and F4 per shape | ready; mark transient cells in the caption |
| F6 | `figures/mpr2_1650ti_3050/latency_vs_load.png` and siblings | load characterisation pooled across policies | supplementary |
| F7 | validation figure | F-23 on one node, in sample | exists; the two-node driver is `tools/p4_validate.py`, and the contrast verdict on the held-out shape runs is **pending** |
| F8 | `h2_advantage.png` | H2 both ways: best-aware minus best-blind, and the specification's WJSQ minus JSQ, each beside the gap to Threshold(T) | code ready, **pending** a simulator sweep |
| F9 | `h3_staleness.png` | routing quality against estimate age in units of τ | **blocked**: it draws counterfactual regret as soon as a run set carries it, and until then falls back to the circular routing-error rate and says so on the figure |
| T1 | section 5.2 | phase R by workload at one and four slots, with intervals | ready |
| T2, T3 | section 5.4 | anchor mean and p95 | ready |
| T5 to T9 | section 5.6 | shape mean and p95, H1 by point, calibration gain by router, load trend | ready |

Do not use `figures/shapes_1650ti_3050/phase_advantage.png` (pools load points unequally) or
anything read from `summary_iid_v1.*`.

## 8. Threats and limitations to state

- **One arrival path and one routing random stream.** Every repeat replayed one trace with
  scheduler seed 42 (section 3 item 9). The intervals do not include arrival or routing
  randomness. Seeded reruns are planned.
- **The workload favours JSQ.** Output lengths forced and within about 10% per shape, Poisson
  arrivals, exact queue counts at staleness 0 from one dispatcher, random-token prompts,
  prefix caching off.
- **Two nodes.** With N = 2, Threshold is "send everything to the 3050" and JSQ's choice is
  shaped by ties. The spec promises N up to 12.
- **Load and R move together across the shapes** at a fixed req/s (Table 7 and 8).
- **Capability is a one-slot scalar** that understates operating R by about 2x, fixed across
  workloads.
- **StaticWeighted's routing is one lucky draw** (66 to 68% to the fast node against 61%).
- **Engine and host.** The 1650 Ti binary reports build 1 with unknown flags; its cost model
  predates the context pin and the campaign driver; it shares a 6-core host with the
  scheduler and the replay client; its engine restarts and swap are unchecked.
- **Anchor trace is development data** (grid placement, simulator tuning, load points).
- **Simulator validation is single-node, in sample and on absolute latency.**
- **τ is unresolved on every class.**
- **Scale and realism.** 1B model, four slots, consumer GPUs, synthetic prompts, a 512/128
  envelope.
- **Multiplicity.** Dozens of interval statements across statistics, points and shapes. One
  primary statistic per hypothesis (section 3 item 11); everything else is secondary.
- **Transport is asymmetric** (5 to 7 ms on the co-located node, 9 to 16 ms on the 3050).
- **The shape campaigns ran one after another over 11 hours**, so shape is also campaign
  order and time of night.

## 9. Pending

The plan, with owners, costs and what each item changes, is section 4 of
`docs/checkpoint.md`. Control-plane items are tracked in the GitHub issue linked there. What
the writing team can draft now: the method (sections 3 and 4, including the deviations), 5.2,
5.3 and 5.7, and related work. Hold the H1 and elevation discussion until the seeded and
load-matched campaigns are in; both can change the headline again.
