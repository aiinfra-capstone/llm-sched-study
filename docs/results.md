# Results

Every measurement that currently stands, with its provenance, and every claim we have
withdrawn. Snapshot of **2026-09-16**. This is what the write-up draws from.

**The run set wins.** Each run set's `summary.json` is the source of truth. A number here that
disagrees with it is wrong, and the superseded `summary_iid_v1.*` files are never cited.
Statistics follow `analysis-plan.md`; anything computed under the earlier rules has been
re-derived.

---

## 1. Provenance

| Evidence | Vehicle | Set | Standing |
|---|---|---|---|
| Anchor campaign, 45 runs | hardware | `runs/exp/mpr2_1650ti_3050` | Development data. The trace placed the cost-model grid, tuned the simulator and chose the load points |
| Three shape campaigns, 87 valid of 90 | hardware | `runs/exp/phase_{generation,balanced,summarisation}_1650ti_3050` | Held out from cost-model and simulator development |
| Calibration snapshots | hardware | `contracts/cost_models/` | Each 1B class calibrated once, on the 24-cell grid |
| Anchors and load band | hardware | `runs/anchors` | Single node, used for F-23 |
| F-23 validation | both | `runs/anchors` | Single node, in sample |
| Sweeps | simulator | none for the paper | `runs/sweeps/capacity_probe_1b` is a historical probe |

All four hardware campaigns replayed one trace per campaign with scheduler seed 42 in every
repeat, so their intervals sample hardware jitter and are conditional on one arrival path and
one routing random stream (`arrivals_independent: false`).

## 2. K1: heterogeneity depends on the workload and on concurrency

RTX 3050 over GTX 1650 Ti. Cost-model columns from `tools/cell_intervals.py`
(`runs/exp/cell_intervals_3050_over_1650ti.json`, intervals resample calibration batches).
Operating columns are the ratios the steady cells of the live runs recorded at 2.4 req/s.

| Workload | Prompt:output | R service, 1 slot | R service, 4 slots | R prefill, 1 slot | R decode, 1 slot | R decode, 4 slots | Operating R service | Operating R decode |
|---|---:|---|---|---:|---|---|---:|---:|
| generation | 0.50 | 1.39 [1.39, 1.40] | 2.17 [1.82, 2.57] | 8.64 | 1.19 | 1.69 [1.44, 1.98] | 1.51 | 1.44 |
| balanced | 2.00 | 1.58 [1.57, 1.58] | 2.57 [2.14, 3.07] | 9.79 | 1.20 | 1.70 [1.44, 1.99] | 1.80 | 1.52 |
| anchor | 3.00 | 1.79 [1.79, 1.80] | 3.02 [2.67, 3.39] | 9.91 | 1.19 | 1.70 [1.54, 1.87] | 2.24 | 1.79 |
| summarisation | 13.76 | 2.59 [2.57, 2.60] | 4.41 [3.56, 5.41] | 11.12 | 1.20 | 1.71 [1.45, 2.01] | 4.57 | 2.98 |

Supported: the same two machines look 1.39x or 2.59x apart at one slot depending only on the
workload, and 2.2x to 4.4x at four slots.

Not supported: that decode is nearly homogeneous on this pair. That holds at one slot only.
The 3050 gains about 2.85x aggregate decode throughput from batching and the 1650 Ti about
1.9x, so under load the pair differs on decode as well, in a way that looks like batching
efficiency. The memory-bandwidth explanation is dropped.

The one-slot intervals are narrow because the engine is close to deterministic at fixed load.
They carry no day-to-day variation, since each class was calibrated once.

Every number in this section comes from the snapshots the first pair ran on, which predate
the context pin, the campaign driver and the 1650 Ti rebuild. G2 replaces the 1650 Ti's
snapshot, and when it does, this table, the capability figures below it and the utilisation
targets in every campaign config are re-derived from the new one. Whether the ratios move is
itself the answer to the open question above.

**The 1650 Ti's prefill is the card, not the build.** Measured on 2026-09-16 and written up in
section 10. What remains is the rebuild for provenance and the same bench on the 3050.

Capability as the scheduler seeds it: 103.947 and 163.607 output tok/s of service, 1.57x. That
is the least heterogeneous number the snapshots contain.

## 3. K3 and H1 on the anchor trace

45 runs, all valid, no transient cell. Mean end-to-end latency in ms [95% interval].

| Policy | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| RoundRobin | 916 [814, 1023] | 1516 [1330, 1713] | 2949 [2656, 3243] |
| StaticWeighted | 788 [710, 874] | 871 [777, 967] | 996 [872, 1132] |
| JSQ | 734 [678, 794] | 873 [795, 959] | 1011 [902, 1134] |
| WJSQ | 632 [594, 672] | 761 [691, 834] | 832 [759, 916] |
| Threshold (3050 only) | 655 [597, 712] | 755 [698, 808] | 851 [775, 931] |

p95 in ms [95% interval]. Secondary: percentile bootstrap intervals are unreliable where the
p95 lands on a cluster of near-identical latencies.

| Policy | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| RoundRobin | 2689 [1830, 3173] | 4443 [3730, 5228] | 7396 [6723, 8532] |
| StaticWeighted | 1654 [1307, 2441] | 1859 [1584, 2212] | 2120 [1749, 2521] |
| JSQ | 1632 [1376, 1718] | 1916 [1621, 2237] | 2294 [1815, 2971] |
| WJSQ | 1119 [900, 1611] | 1616 [1500, 1761] | 1615 [1354, 2043] |
| Threshold (3050 only) | 1244 [1006, 1522] | 1385 [1236, 1492] | 1538 [1331, 1655] |

Routing shares: RoundRobin 50% to the 3050, JSQ 57 to 63%, StaticWeighted 67 to 68% against a
nominal 61%, WJSQ 72 to 73%, Threshold 100%.

## 4. The H1 interaction, every point on the first pair

Primary statistic, the interaction on log mean latency. A blank row is a point where a cell of
the 2x2 is transient or saturated, so no contrast is computed.

| Workload | Load | Pool utilisation | 1650 Ti under RoundRobin, offered / capacity | Interaction, log | Interaction, ms | Status |
|---|---|---:|---:|---|---|---|
| generation | 2.4 | 0.43 | 0.73 | -0.008 [-0.098, +0.069] | +5 [-109, +103] | defined |
| generation | 3.2 | 0.57 | 0.99 | | | RoundRobin transient |
| balanced | 2.4 | 0.49 | 0.93 | +0.221 [+0.089, +0.346] | +411 [+175, +669] | defined, RoundRobin's climb borderline (1.23, interval reaching 1.51) |
| balanced | 3.2 | 0.65 | 1.26 | | | RoundRobin and StaticWeighted transient |
| anchor | 1.3 | 0.18 | 0.41 | +0.002 [-0.117, +0.116] | +27 [-69, +122] | defined |
| anchor | 2.4 | 0.34 | 0.73 | +0.417 [+0.241, +0.582] | +533 [+325, +742] | defined |
| anchor | 3.2 | 0.45 | 0.97 | +0.891 [+0.704, +1.088] | +1775 [+1450, +2118] | defined |
| summarisation | 2.4 | 0.36 | 1.06 | | | RoundRobin transient |
| summarisation | 3.2 | 0.49 | | | | RoundRobin saturated |

Ordered by the load RoundRobin puts on the slow node: 0.41 zero, 0.73 zero, 0.73 +0.417,
0.93 +0.221, 0.97 +0.891. The two points at 0.73 disagree, and they differ in R as well as in
arrivals, so neither load nor R can be separated as the cause on these runs.

**What this licenses.** Calibration removes about the same fraction of latency with or without
queue depth while the queue-blind router is comfortably stable, and a positive interaction
appears as that router approaches its stability limit. Balanced at 2.4 req/s only just passes
the steady-state gate and is the weakest of the three positive points.

## 5. What calibration buys each router

Ratio is calibrated over uncalibrated mean latency. `n/d` where a cell of that router is
transient or saturated.

| Workload | Load | Queue-blind SW/RR | ms | Queue-aware WJSQ/JSQ | ms | 1650 Ti under JSQ, offered / capacity |
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

**K4, candidate.** The queue-aware ratio falls as R rises: 0.889 on generation, 0.866 on
balanced and 0.769 on summarisation at 2.4 req/s, with the generation and summarisation
intervals not overlapping; 0.900, 0.844 and 0.738 at 3.2. The JSQ cells put the slow node at
similar load on generation and summarisation (0.62 and 0.66), so this is less confounded with
load than the queue-blind result without being free of it. Part of the relative change is a
denominator effect, since JSQ is 729 ms on summarisation against 1268 ms on generation. The
matched-load campaign decides it.

**H1's second clause is not supported.** The queue-aware gain against load, lightest to
heaviest:

| Workload | JSQ - WJSQ, ms | Change | WJSQ/JSQ | Change in ratio |
|---|---|---|---|---|
| anchor | 102 / 112 / 178 | +77 [-8, +172] | 0.862 / 0.872 / 0.824 | x0.956 [0.867, 1.051] |
| generation | 141 / 142 | +1 [-70, +70] | 0.889 / 0.900 | x1.013 [0.959, 1.073] |
| balanced | 158 / 210 | +52 [-5, +107] | 0.866 / 0.844 | x0.975 [0.929, 1.024] |
| summarisation | 168 / 224 | +56 [-8, +118] | 0.769 / 0.738 | x0.959 [0.884, 1.041] |

In milliseconds it grows on three of four workloads and is flat on the fourth. As a ratio no
workload shows a change distinguishable from none. Nowhere does it shrink.

## 6. Workload shapes on hardware

Mean end-to-end latency in ms [95% interval]. **T** marks a transient cell.

| Policy | gen 2.4 | gen 3.2 | bal 2.4 | bal 3.2 | summ 2.4 | summ 3.2 |
|---|---|---|---|---|---|---|
| RoundRobin | 1403 [1292, 1509] | 2713 [2273, 3162] T | 1859 [1547, 2214] | 6448 [5453, 7473] T | 6792 [5513, 8262] T | saturated |
| StaticWeighted | 1258 [1178, 1357] | 1464 [1353, 1589] | 1290 [1158, 1456] | 1576 [1421, 1745] T | 1308 [1063, 1599] | 3030 [2233, 3886] T |
| JSQ | 1268 [1198, 1346] | 1423 [1356, 1486] | 1177 [1115, 1242] | 1348 [1289, 1407] | 729 [682, 782] | 854 [792, 920] |
| WJSQ | 1128 [1068, 1189] | 1281 [1224, 1337] | 1019 [965, 1074] | 1138 [1090, 1189] | 560 [528, 594] | 630 [583, 683] |
| Threshold | 1282 [1182, 1407] | 3476 [2738, 4350] T | 1038 [976, 1110] | 1615 [1388, 1854] T | 440 [419, 463] | 503 [473, 536] |

p95 in ms [95% interval], secondary.

| Policy | gen 2.4 | gen 3.2 | bal 2.4 | bal 3.2 | summ 2.4 | summ 3.2 |
|---|---|---|---|---|---|---|
| RoundRobin | 2482 [2219, 2924] | 8366 [6799, 9217] T | 5127 [3579, 6491] | 21315 [20327, 23151] T | 22960 [22059, 24005] T | saturated |
| StaticWeighted | 2129 [1930, 2708] | 2796 [2273, 3787] | 2892 [2399, 4012] | 3994 [3408, 5312] T | 4751 [3964, 7183] | 14018 [11506, 15184] T |
| JSQ | 2098 [1988, 2179] | 2256 [2176, 2397] | 2272 [2168, 2393] | 2629 [2480, 2815] | 2241 [1852, 2522] | 2749 [2481, 3151] |
| WJSQ | 1766 [1575, 1875] | 1988 [1880, 2057] | 1851 [1572, 1983] | 2037 [1928, 2177] | 1370 [1012, 1693] | 1814 [1603, 2074] |
| Threshold | 2012 [1709, 2365] | 7917 [6851, 8427] T | 1567 [1368, 1825] | 3784 [2804, 4120] T | 663 [635, 719] | 768 [696, 891] |

Observations that stand: the best policy moves with the workload (WJSQ on generation and
balanced, Threshold on summarisation at both loads); Threshold holds only while the fast node
alone has room, and is transient on generation and balanced at 3.2 req/s. The second is
consistent with H2's shape along the workload axis and is confounded with load headroom on a
two-node pool, so the claim is left to the simulator.

Routing shares: WJSQ sends 67 to 68% to the 3050 on generation, 70 to 71% on balanced, 77 to
78% on summarisation; JSQ 57 to 60%, 61 to 65%, 69 to 72%; StaticWeighted 66% everywhere.

## 7. Serving metrics

- **E2E SLO attainment at 2x** ranks the policies much as the mean does, with WJSQ highest or
  tied on every workload except summarisation, where Threshold reaches 98 to 100% against
  WJSQ's 77 to 78%.
- **TTFT attainment at 2x is close to the share routed to the fast node.** The 1650 Ti's
  prefill alone is 9 to 11x the 3050's, so almost any request placed there misses a TTFT
  target set by the fast node. JSQ and WJSQ sit at 56 to 75%; Threshold at 70 to 98% where it
  is steady. A TTFT SLO on this pair is a routing-share metric, which is where a policy that
  wins on mean latency can lose.

TTFT is worker-side and excludes the network, because delivery is not streamed.

## 8. The cost model, and the simulator

**Cost model.** `costcheck` over the 45 anchor runs: 24 exercised cells, 7,905 requests,
request-weighted absolute error 17.4% against the ±25% tolerance, 7.3% on medians. Three cells
fall outside, worst 29%, all at concurrency 1 or 2. The grid was placed on the anchor trace's
lengths, so this is in sample.

**Simulator.** F-23 on the single-node 1650 Ti pool: p50 error -16.5% to -19.4% at the three
steady anchors and -7.8% at 1.98 req/s, which is transient and whose percentiles are not
quoted. The fixes that brought it inside ±25% were chosen while watching this error, so it is
in sample. Two-node validation on contrasts, on the held-out shape runs, is pending and is the
gate on every simulator claim.

How the error moved, for the record:

| Stage | quiet | light | mid | heavy |
|---|---|---|---|---|
| With a hardcoded 5% multiplier | -17.2% | -12.0% | -2.8% | +16.3% |
| Multiplier removed, measured transport added | -23.8% | -21.3% | -18.3% | -7.1% |
| Prefill held invariant under batch change | -18.3% | -19.4% | -16.5% | -7.8% |

Determinism: 200 of 200 decisions identical across two simulator runs of one manifest.

## 9. K6: drift, and what the instrument can see

- **τ is not resolved on any class.** The CPU 8B point estimate is 69.5 s, and its own
  calibration record says `tau_resolved: false`: 31 windows of 48 s over a segment 21 τ long.
  `tools/tau_interval.py` gives lag-1 correlation 0.48 [0.03, 0.60], a block-bootstrap τ
  interval of 31 to 91 s, and no decay beyond one window in 66% of draws. Removing a linear
  trend, throughput fell 2.3% across the segment, gives 61.8 s. Report it as an estimate of
  the order of a minute, with its interval, on a class that is in no pool.
- **Both GPU classes are censored at the 5 s floor.** The resolution rule is that a node whose
  τ is shorter than about five service times cannot show its own drift, which is the reusable
  half of this result.
- The RTX 3050 held sustained throughput with a coefficient of variation of 0.009 across 60
  windows.
- **Restart and memory pressure.** On the 3050, engine start times recovered from its logs
  place 118 of 132 runs, and service time moved +0.05% per hour since restart [-0.19, +0.32]
  over 0 to 4.5 h (`runs/exp/restart_effect_rtx3050.json`). The 1650 Ti's engine logs were not
  kept, and that is the host that went into swap, so it is unchecked.

## 10. Supporting measurements

Carried forward because later decisions rest on them.

**The slow node's engine, benched (G1, first half).** `tools/engine_bench.sh` on the 1650 Ti,
2026-09-16, `runs/bench/gtx1650ti_before_rebuild.json`. The engine serving every campaign
reports `0.2.0-dev (build 1, commit 5a32f7b)`, which is the pinned commit with the build
number missing.

| Build | pp512 tok/s | tg128 tok/s |
|---|---:|---:|
| As installed (CUDA Release, `sm_75`, native) | 754.6 ± 1.8 | 149.8 ± 0.5 |
| Same commit, `GGML_CUDA_FORCE_MMQ=ON` | 748.6 ± 1.6 | 148.6 ± 0.2 |

Three things that together answer the open question in section 2.

1. **The flags were never unknown.** The build tree that produced the installed engine is on
   disk and configured: CUDA on, `CMAKE_CUDA_ARCHITECTURES=75`, Release, native. Only
   `LLAMA_BUILD_NUMBER` is absent, which is the whole of why the server says build 1.
2. **The kernel path llama.cpp itself suggests changes nothing.** The runtime prints a hint
   that a card without tensor cores should be built with forced MMQ. Built that way, prefill
   moves by 0.8%, which is the wrong direction and inside the run-to-run spread. The hint's
   other half, `CMAKE_CUDA_ARCHITECTURES=61-virtual`, cannot be tested here: CUDA 13.2 has
   dropped `compute_61`.
3. **The card is at its power limit while doing it.** Sampled under a sustained prefill load:
   1545 MHz of a 2100 MHz maximum, 48.6 W against a 50 W limit, 65 °C, 98% utilisation.

Published figures agree on the ratio rather than the absolute. The llama.cpp CUDA scoreboard
has a GTX 1660, Turing without tensor cores like the 1650 Ti, at pp512 149 tok/s, and an RTX
3050 at 1147 tok/s on the same build and model, a 7.7x prompt-processing ratio between the two
classes. Ours is 9 to 10x on a different model and quantisation, between a laptop 1650 Ti at
50 W and a laptop 3050. Same order, same direction.

**What this leaves open.** The rebuild itself, which is provenance rather than performance: it
stamps the build number so a manifest can prove its engine. And the same bench on the 3050,
which needs that laptop back.

**Transport.** C-5 derives `transport_residual_ms` per request. Over 759 warmed-up successful
rows of the four anchors: mean 5.86 ms, sd 2.66, p50 5.16, and flat in load (5.37 ms quiet
against 5.63 ms heavy while service time moves by a factor of ten). So it is additive in
milliseconds and a multiplicative correction is the wrong shape. On the two-node pool it
differs by node: 5.4 to 7.2 ms to the co-located node, 8.7 to 15.5 ms over Wi-Fi.

**Prefill is flat in concurrency under Poisson arrivals, and not under synchronised ones.**
From the anchor worker logs, mean ms by prompt bucket and batch size at admission:

| Bucket | batch 1 | batch 2 | batch 3 | batch 4 |
|---|---|---|---|---|
| 1-128 prefill | 179.4 | 267.2 | 280.4 | 189.0 |
| 257-512 prefill | 701.7 | 750.5 | 756.1 | 706.9 |
| 1-128 decode | 822.0 | 993.8 | 1073.0 | 1587.7 |

The calibration grid holds `c` requests in flight by firing them together, and there the same
128-token prompt goes 174.2 ms at one slot to 726.6 ms at four. Both are correct and they
answer different questions: a Poisson process almost never starts two prompts at once, so a new
request's prefill overlaps its neighbours' decode. It matters because `service_ms_mean` above
one slot inherits the synchronised figure.

Reconstructing service time from the split was tested and rejected: it lands 20 to 40% below
the anchors, where the table as it stands is accurate to within 1.7 to 7.4% at four slots. The
flat prefill is a fact about attribution rather than about total cost.

**The engine is close to deterministic at fixed concurrency.** The 300 s sustained segment,
632 samples at four slots, gives log-sd 0.087 overall and 0.0035 across its second half, with
p95/p50 of 1.01; the first half's 0.104 is the thermal ramp. Dispersion keyed on
`batch_size_at_admission` (0.403, 0.368, 0.363, 0.228) is concurrency changing during a
request, not service-time noise: mean service at admission-batch 1 is 1016.6 ms against a true
fixed-concurrency value of 615.7 ms. So C-3's global sigma of 0.1225 is at or above the honest
value, and the residual F-23 error is a queueing question.

**Token budget.** VRAM is not the constraint: 1477 MiB of 4096 at 16384 context and 1993 MiB
at 32768, matching the KV arithmetic. Time is: prefill is close to linear out to 4096 tokens
(1.37 to 1.65 ms per token), but a (2048, 256) request averages 5.15 s at one slot and 17.15 s
at four, and saturated node throughput falls from 2.55 req/s at (128, 64) to 0.23 at
(2048, 256). Widening the envelope costs a recalibration and lengthens every run.

**Single-node load band** (`runs/anchors/load_band.json`): quiet 0.72 offered and 0.74
achieved, light 1.035 and 1.06, mid 1.305 and 1.34, all steady; heavy 1.98 offered and 1.60
achieved with latency still climbing by 11.7 s across the window, flagged `saturated` and
`short`. Heavy bounds saturation from above and its percentiles are not quoted as steady-state
figures.

## 11. Withdrawn

Listed so that nothing re-enters from an old figure or an old summary.

| Withdrawn | Why |
|---|---|
| "Queue-blind calibration gain rises 146 to 5484 ms with R" | The large values came from RoundRobin cells whose latency climbed about 9 s from the first third of the run to the last. Their means scale with run length |
| "+4662 ms interaction on balanced at 3.2 req/s" | Same, two transient cells |
| "H1 holds and strengthens with load" | The interaction grows only as RoundRobin approaches its stability limit, and is zero on the ratio scale where that router is comfortably stable |
| "Calibration is redundant, more so as load rises" | The second clause is contradicted in both milliseconds and ratio |
| "τ = 69.5 s, resolved" | The calibration record says `tau_resolved: false`, and the block-bootstrap interval is 31 to 91 s |
| "Batching buys nothing on this card" | Contradicted by the committed cost models: aggregate decode throughput rises about 1.9x on the 1650 Ti and 2.85x on the 3050 |
| "Decode is nearly homogeneous across these cards" | True at one slot only. At four slots decode R is about 1.7 |
| "The choice of capability number moved the pool from 1.18x to 1.57x" as a contribution | A methods note. The value-of-calibration curve replaces it |
| Any routing-error rate | The metric scores decisions with WJSQ's own formula on the view the policy saw |
| `figures/shapes_1650ti_3050/phase_advantage.png` | Pools load points unequally across shapes |

## 12. Threats that remain open

| Threat | Where it bites | Closed by |
|---|---|---|
| One arrival path and one routing stream | Every interval on the first pair | Seeded anchor campaign |
| Load moves with R across the shapes | K4 | Matched-load campaign |
| Capability is a one-slot scalar, about half the operating ratio | The calibrated policies look weaker than they could | Ablation campaign, and `ect` |
| The workload favours queue depth: known, near-constant lengths, Poisson arrivals, exact queue counts, one dispatcher | K3 and K5 | Heavy-tailed and staleness campaigns |
| 1650 Ti build flags unknown; its cost model predates the context pin and the driver | K1 | Engine bench and rebuild, then recalibration |
| The harness shares a 6-core host with the slow node | K1 and every latency on that node | A third host, one anchor rerun |
| Two nodes | H1's generality, Threshold's meaning | Simulator with 1 fast plus k slow |
| Simulator validated single-node, in sample, on absolute latency | Every simulator claim, including H2 | The contrast criterion on the held-out shape runs |
| Anchor trace is development data | Anchor results | Reported as development results; shapes and heavy-tailed traces are held out |
| Shape is also campaign order and time of night | K4 | Interleaved shapes in the matched-load campaign |
| The spec PDF disagrees on H1's sign and H2's observable | A reviewer given the spec | Recorded as deviations in `research-plan.md` section 7 |

## 13. Related work

Checked against each paper's published abstract. Read the paper before citing anything beyond
these lines.

| Work | What it does | How we differ |
|---|---|---|
| Splitwise (Patel et al., ISCA 2024) | Splits prompt computation and token generation onto different machines | Phase placement on datacenter GPUs with fast interconnect; we route whole requests on consumer machines and ask what phase asymmetry does to the value of calibration |
| DistServe (Zhong et al., OSDI 2024) | Disaggregates prefill and decode to remove interference, per-phase resources for TTFT and TPOT | Their heterogeneity is in the workload's SLOs, ours is in the hardware |
| HexGen-2 (Jiang, Yan and Yuan, ICLR 2025) | Separates the phases across heterogeneous GPUs by constrained optimisation | Disaggregation plus hardware heterogeneity; we study whole-request routing and what information it needs |
| Mélange (Griggs et al., arXiv 2404.14527) | Picks the cheapest GPU mix; no GPU type is most cost-efficient across all request sizes | Offline allocation that assumes profiles are worth having. Their request-size finding is consistent with our K1 |
| Helix (Mei et al., ASPLOS 2025) | Max-flow and MILP placement and scheduling over heterogeneous GPUs and networks | Optimises with a performance model; we ask when the model earns its keep |
| HexGen (Jiang et al., ICML 2024) | Asymmetric tensor and pipeline partitioning across heterogeneous GPUs | Model partitioning; our nodes each hold a whole model |
| Llumnix (Sun et al., OSDI 2024) | Runtime rescheduling with live migration of request state | Reactive rebalancing; we study the dispatch decision and its information |
| NexusSched (Zhang et al., arXiv 2509.23384) | Argues queue length and average latency are lagging signals, routes on a predictive model | The closest contrary position. We test the premise with a factorial rather than assume it |
| vLLM Router, llm-d | Production routers weighing prefix-cache hits, KV state and live load | Built around cache locality; whether hardware calibration adds to live load is not what they report |
| RouteBalance (Da and Kalyvianaki, arXiv 2606.17949) | Model selection joined with load balancing across heterogeneous model instances | Heterogeneity of models and quality; ours is one model on different hardware |
| Solyx AI Grid (Bernhard and Katla, arXiv 2606.15050) | Weighted routing on telemetry across datacenters | Multi-signal production routing; no decomposition of which signal pays |
| Mitzenmacher, TPDS 2000 | Load balancing on stale queue information; herd behaviour when stale | The classical basis for the staleness axis |
| Speed-aware JSQ (arXiv 2203.01721) | SA-JSQ is asymptotically delay-optimal for heterogeneous servers | Asymptotic, scalar speeds, large N. Ours are phase-dependent and N is small |
| Luo and Zubeldia (arXiv 2510.14284) | Stability and heavy-traffic optimality of JSQ, JSED and power-of-d in heterogeneous systems | Theory for the same policy families; we supply measured service distributions |
| Lin et al. (arXiv 2602.02987) | Stochastic control for prefill-decode contention at scale | Contention inside a cluster; we take it as part of each node's measured cost |
| Usami et al. (arXiv 2606.17104) | Accelerator advantages differ by phase | Independent evidence for K1; they characterise, we route |
| Tummalapalli et al. (arXiv 2603.23640) | Sustained-load behaviour of a 1.5B model on edge devices, with thermal loss | Supports the framing of K6; they do not route |
| Petals (Borzunov et al., ACL 2023 demo) | Collaborative inference over consumer GPUs by splitting a model into blocks | Consumer hardware, model splitting rather than whole-request routing |
