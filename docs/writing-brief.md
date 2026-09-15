# Writing brief

For the writing team. What the study now is, what we can claim and on what evidence, what we
contrast against, and every figure and table with its provenance. Snapshot of
**2026-09-15**, after all four hardware campaigns on the first pair finished (132 valid runs).
Sections marked **pending** need work that has not run yet; nothing in them should be written
into the paper yet. Section 10 lays out what stands between these results and a journal submission.

Numbers here come from committed code and the run sets named beside them. If a number in the
draft disagrees with a run set's `summary.json`, the run set wins.

---

## 1. The study in one paragraph

Small labs and edge deployments serve language models on whatever machines they own, and
those machines differ by an order of magnitude or more. The textbook fix is calibration:
measure each node's speed and weight routing by it. We ask whether that measurement buys
anything a scheduler cannot already read from live queue depth, and we answer with a
two-by-two factorial of routing policies (queue-blind or queue-aware, hardware-blind or
hardware-aware) on real consumer GPUs serving the same pinned engine, model and
quantisation, backed by a discrete-event simulator that runs the same policy code. The
elevation is that "how much faster" is not one number: prefill is compute-bound and decode
is memory-bandwidth-bound, so the heterogeneity a scheduler faces depends on the workload's
prompt-to-output mix, and so, we expect, does the value of calibration.

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
   SSD. The planned 4 GB CPU-only machine was not used, and the RTX 4070 desktop the same SSD was built for was not available for these runs.
2. **The prediction's direction flipped with the pair, and we say so.** The elevation was
   written against a CPU and partially offloaded GPU pair, where decode differed more than
   prefill. Our GPU pair is the opposite: prefill R about 10x, decode R 1.2x. The prediction
   is therefore about direction relative to the pair, not "decode-heavy work always needs
   calibration".
3. **Capability was redefined, and the old definition would have biased H1.** Both vehicles
   seeded each node's capability with decode tokens per second at the lowest cost-model
   cell. Decode hides prefill, so on this pool the capability-aware policies saw a 1.18x
   pool that is 1.57x on service time and 1.76x on the anchor trace. Capability is now
   output tokens per second of total service time (decode tok/s times the decode share of
   service time), shared by the live scheduler and the simulator
   (`com.sched.core.Capability`). Every hardware run in this brief used the new definition.
   Reporting the sensitivity is planned (S3 in `docs/checkpoint.md`).
4. **Engine context size is pinned.** llama-server sizes its context to fit VRAM when none
   is given, which gave the two nodes 13,824 and 30,720 tokens per slot from one command. All
   nodes now run `-c 55296` (13,824 per slot).
5. **Load points are set against the pool, not one node.** The specification's load band
   (1.035 to 1.305 req/s) belongs to the 1650 Ti alone. The anchor campaign runs at 1.3, 2.4
   and 3.2 req/s.
6. **Threshold T sits between the two classes** (130 tok/s against 103.9 and 163.6), so
   Threshold serves the 3050 alone.
7. **The network is 2.4 GHz Wi-Fi** hosted by the harness laptop, with client power saving
   off (RTT about 4 ms). A cable was not available.
8. **Engines were restarted between runs.** llama-server keeps an 8 GiB host-side prompt cache
   by default (`--cache-ram 8192`), which filled host memory over the shape campaigns. Both
   engines were restarted with the identical command at run boundaries (about 12 times), never
   inside a run, so the validity rule on engine restarts holds. The cache never produced a hit
   (every prompt was evaluated at full length), so it did not change any measured latency.
   Future runs set `--cache-ram 0`.

## 4. Setup and method (for the methods section)

| | |
|---|---|
| Engine | llama.cpp tag b10569, commit 5a32f7b, one patch (a 500 on `/completion` when generation ends mid-character); recorded as `b10569+p1+cuda13.2` |
| Model | Llama-3.2-1B-Instruct, GGUF Q4_K_M, identical bytes on every node (SHA-256 checked) |
| Engine settings | `-ngl 99 --threads 6 --parallel 4 -c 55296` on both nodes |
| Nodes | GTX 1650 Ti 4 GB (Ryzen 5 4600H) and RTX 3050 6 GB Laptop GPU (i5-13450HX, 85 W cap, performance profile), both on AC |
| Harness | scheduler (Java, gRPC) and open-loop replay client on the 1650 Ti laptop |
| Admissible envelope | prompt ≤ 512, output ≤ 128 tokens |
| Clock discipline | chrony on both hosts, measured into every manifest; worst pair offset about 2 ms, rate difference ≤ 0.13 ppm; no cross-host duration is ever computed |
| Validity | a run is discarded if any request's send lag exceeds its bound, any request is dropped, the engine restarts, or two pool nodes share a host. Every run in the statistics is valid; the three invalid runs (RoundRobin, summarisation, 3.2 req/s) are reported as saturation in 5.6 and excluded from every table. |
| Anchor trace | 200 requests, Poisson at a base 0.9 req/s scaled per point, buckets p128/o64 (50%), p256/o64 (30%), p512/o128 (20%), mean prompt-to-output ratio 3.0 |
| Shape traces | 600 requests each, identical arrivals and priorities, lengths only differ; ratios 13.76 (summarisation), 2.00 (balanced), 0.50 (generation). Their arrivals differ from the anchor trace's, so the three shape traces compare cleanly with each other and only loosely with the anchor |
| Repeats and order | 3 repeats per policy and point; points run lightest first; policy order shuffled per point from a fixed seed so throughput drift cannot align with the policy comparison |
| Warmup | the first 10 s of each run excluded |
| Statistics | 95% bootstrap intervals, 2000 draws, resampling requests within each run so repeats remain repeats |
| Simulator | discrete-event, same policy classes as the live scheduler, parameterised from the C-3 cost models, validated against hardware (F-23, ±25% on p50 and p95) |

**Calibration.** Each node class is calibrated on a 24-cell grid (three prompt buckets, two
output buckets, concurrency 1 to 4, 8 samples per cell) plus a 300 s sustained segment
sliced into 5 s windows, producing a time-ordered series of cost-model snapshots. Prefill and
decode time are recorded per request, so every cell carries the phase split.

## 5. Results we can write now

### 5.1 Throughput nonstationarity (MPR-1)

- The CPU 8B class has τ = 69.5 s (fit r² 0.989). Neither GPU class shows measurable drift:
  τ is censored at the 5 s resolution floor, which is itself a result about what the
  instrument can see (a node whose τ is shorter than about five service times cannot show
  its own drift).
- The RTX 3050 held its sustained throughput with a coefficient of variation of 0.009 across
  60 windows.

### 5.2 Heterogeneity depends on the workload (the elevation's premise)

Table 1. RTX 3050 over GTX 1650 Ti, concurrency 1, from the committed cost models
(`runs/exp/phase_ratio_3050_over_1650ti.json`, figure `phase_ratio.png`).

| Workload | Prompt:output | R on service time | R on prefill | R on decode |
|---|---:|---:|---:|---:|
| generation | 0.50 | 1.39 | 8.64 | 1.19 |
| balanced | 2.00 | 1.58 | 9.79 | 1.20 |
| anchor | 3.00 | 1.76 | 9.43 | 1.19 |
| summarisation | 13.76 | 2.59 | 11.12 | 1.20 |

The same two machines look 1.39x or 2.59x apart depending only on what is asked of them.
Decode, which dominates generation-heavy work, is nearly homogeneous across these cards
(similar memory bandwidth); prefill differs by an order of magnitude (compute).

### 5.3 The cost model predicts the live pool

`costcheck` over the 45 anchor runs: 24 exercised cells, 7,905 requests, request-weighted
absolute error 17.4% against the ±25% tolerance (7.3% on medians). Three cells fall outside,
worst 29%, all at concurrency 1 or 2 where a table of means cannot carry the right tail.

### 5.4 H1 on real heterogeneous hardware (anchor trace)

Run set `runs/exp/mpr2_1650ti_3050`, 45 runs, all valid; tables in `summary.md` beside it.

Table 2. Mean end-to-end latency in ms [95% interval].

| Policy | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| RoundRobin | 916 [860, 976] | 1516 [1398, 1627] | 2949 [2717, 3191] |
| StaticWeighted | 788 [752, 826] | 871 [833, 914] | 996 [944, 1049] |
| JSQ | 734 [703, 767] | 873 [829, 921] | 1011 [956, 1068] |
| WJSQ | 632 [609, 657] | 761 [727, 797] | 832 [794, 872] |
| Threshold (3050 only) | 655 [633, 679] | 755 [731, 780] | 851 [822, 881] |

Table 3. p95 end-to-end latency in ms [95% interval].

| Policy | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| RoundRobin | 2689 [2379, 2962] | 4443 [3939, 4893] | 7396 [7072, 7611] |
| StaticWeighted | 1654 [1520, 1718] | 1859 [1656, 2104] | 2120 [1918, 2339] |
| JSQ | 1632 [1600, 1692] | 1916 [1700, 2079] | 2294 [2035, 2634] |
| WJSQ | 1119 [952, 1608] | 1616 [1605, 1626] | 1615 [1488, 1877] |
| Threshold (3050 only) | 1244 [1145, 1346] | 1385 [1293, 1455] | 1538 [1441, 1598] |

Table 4. The H1 interaction, ms [95% interval]. Positive means calibration buys less once the
policy sees queue depth.

| Statistic | 1.3 req/s | 2.4 req/s | 3.2 req/s |
|---|---|---|---|
| mean | +27 [-53, +108] | +533 [+397, +665] | +1775 [+1525, +2037] |
| p95 | +522 [+105, +1032] | +2285 [+1768, +2752] | +4597 [+4000, +5005] |

What the tables support:

1. **H1 holds and strengthens with load.** On the mean, the interaction is indistinguishable
   from zero at 1.3 req/s and positive with intervals excluding zero at 2.4 and 3.2 req/s.
   Calibration saves RoundRobin 128, 645 and 1,953 ms on the mean; it saves JSQ 102, 112 and
   178 ms.
2. **Redundant is not useless.** WJSQ beats JSQ at every point, with non-overlapping
   intervals on the mean (about 12 to 18%). The honest phrasing is that queue depth recovers
   most of what calibration offers, not all of it.
3. **Threshold is competitive already at R about 1.6 to 2.5.** Sending everything to the
   3050 gives the best or equal-best tail at 2.4 and 3.2 req/s: the slow node adds little
   capacity and a long tail. That is the behaviour H2 predicts at high R arriving earlier
   than expected, and it needs a load point where the 3050 alone saturates before it can be
   called a crossover.
4. **Where work goes.** RoundRobin sends 50% to the 3050, JSQ 57 to 63%, StaticWeighted 67
   to 68%, WJSQ 72 to 73%, Threshold 100% (`routing_share_anchor.png`).

Do not write: that calibration is unnecessary; that Threshold is best in general; anything
about H2's shape from this table alone (5.6 adds the workload axis).

### 5.5 Simulator validity

F-23 on the single-node 1650 Ti pool: p50 error -7.8% to -19.4% across 0.72 to 1.98 req/s,
worst of eight p50 and p95 comparisons 21.7%, all within ±25%. Validation on the two-node
pool, per policy, is **pending** (P4).

### 5.6 Workload shapes on hardware

Run sets `runs/exp/phase_{generation,balanced,summarisation}_1650ti_3050`, 30 runs each at 2.4
and 3.2 req/s (5 policies, 3 repeats, 570 measured requests per run). 87 of 90 runs are valid.
The three invalid runs are RoundRobin on summarisation at 3.2 req/s, all three repeats: the
1650 Ti could not clear its half of the prompt-heavy work, requests timed out (522 of 600
completed), and the run fails validity. That is saturation, and it is reported as such.

Table 5. Mean end-to-end latency in ms [95% interval]. Lowest mean per column in bold.

| Policy | generation 2.4 | generation 3.2 | balanced 2.4 | balanced 3.2 | summarisation 2.4 | summarisation 3.2 |
|---|---|---|---|---|---|---|
| RoundRobin | 1403 [1374, 1433] | 2713 [2600, 2826] | 1859 [1786, 1929] | 6448 [6082, 6836] | 6792 [6383, 7188] | saturated |
| StaticWeighted | 1258 [1236, 1280] | 1464 [1431, 1497] | 1290 [1255, 1329] | 1576 [1522, 1631] | 1308 [1230, 1390] | 3030 [2806, 3246] |
| JSQ | 1268 [1247, 1290] | 1423 [1399, 1447] | 1177 [1152, 1202] | 1348 [1315, 1381] | 729 [699, 759] | 854 [815, 893] |
| WJSQ | **1128 [1112, 1143]** | **1281 [1264, 1298]** | **1019 [1003, 1036]** | **1138 [1118, 1159]** | 560 [543, 579] | 630 [607, 653] |
| Threshold (3050 only) | 1282 [1266, 1299] | 3476 [3361, 3590] | 1038 [1025, 1051] | 1615 [1573, 1657] | **440 [433, 446]** | **503 [495, 510]** |

Table 6. p95 end-to-end latency in ms [95% interval].

| Policy | generation 2.4 | generation 3.2 | balanced 2.4 | balanced 3.2 | summarisation 2.4 | summarisation 3.2 |
|---|---|---|---|---|---|---|
| RoundRobin | 2482 [2374, 2570] | 8366 [8095, 8581] | 5127 [4900, 5668] | 21315 [21094, 21782] | 22960 [22769, 23238] | saturated |
| StaticWeighted | 2129 [2050, 2198] | 2796 [2670, 3138] | 2892 [2715, 3205] | 3994 [3781, 4253] | 4751 [4578, 5342] | 14018 [13696, 14462] |
| JSQ | 2098 [2066, 2120] | 2256 [2228, 2291] | 2272 [2227, 2321] | 2629 [2551, 2735] | 2241 [1914, 2385] | 2749 [2582, 2922] |
| WJSQ | 1766 [1699, 1824] | 1988 [1953, 2021] | 1851 [1747, 1929] | 2037 [1985, 2120] | 1370 [1207, 1608] | 1814 [1695, 1861] |
| Threshold (3050 only) | 2012 [1926, 2056] | 7917 [7775, 8055] | 1567 [1512, 1625] | 3784 [3675, 3924] | 663 [651, 675] | 768 [737, 798] |

Table 7. What calibration buys on the mean, ms [95% interval], and the H1 interaction. Rows
ordered by R on service time. The anchor row uses a different arrival sequence (see section 4).

| Workload | R service | Load | Queue-blind gain (RR - SW) | Queue-aware gain (JSQ - WJSQ) | Queue-aware gain, % of JSQ | H1 interaction |
|---|---:|---|---|---|---:|---|
| generation | 1.39 | 2.4 | 146 [110, 182] | 141 [115, 168] | 11% | +5 [-43, +50] |
| balanced | 1.58 | 2.4 | 568 [486, 645] | 158 [128, 186] | 13% | +411 [+322, +497] |
| anchor | 1.76 | 2.4 | 645 [517, 763] | 112 [57, 170] | 13% | +533 [+397, +665] |
| summarisation | 2.59 | 2.4 | 5484 [5060, 5891] | 168 [132, 205] | 23% | +5315 [+4884, +5722] |
| generation | 1.39 | 3.2 | 1249 [1129, 1369] | 142 [112, 172] | 10% | +1107 [+985, +1230] |
| balanced | 1.58 | 3.2 | 4872 [4497, 5263] | 210 [170, 249] | 16% | +4662 [+4285, +5053] |
| anchor | 1.76 | 3.2 | 1953 [1718, 2200] | 178 [110, 252] | 18% | +1775 [+1525, +2037] |
| summarisation | 2.59 | 3.2 | RoundRobin saturated | 224 (no interval) | 26% | undefined |

On p95 the interaction is +23 [-113, +174] for generation at 2.4 req/s and excludes zero
everywhere else it is defined (+1815 to +17339 ms); full tables in each set's `summary.md`.

What the tables support:

1. **H1 holds on every shape, and its size tracks R.** The interaction is positive wherever it
   is defined. Among the shape campaigns its one null is generation at 2.4 req/s, the lowest R at the lighter load, where
   queue-blind calibration gains no more than queue-aware calibration does (146 against 141 ms)
   because there is little heterogeneity left to exploit. On summarisation at 2.4 req/s it is
   +5315 ms.
2. **The elevation holds for queue-blind routing.** At 2.4 req/s, what calibration saves
   RoundRobin rises with R on service time: 146, 568, 645 and 5484 ms across R 1.39, 1.58,
   1.76 and 2.59. At 3.2 req/s the same order holds across the three shape traces, which share
   arrivals (1249 ms, 4872 ms, then saturation).
3. **For queue-aware routing the absolute gain barely moves, the relative gain doubles.** JSQ
   to WJSQ saves 112 to 168 ms at 2.4 req/s on every workload, with overlapping intervals. As a
   share of JSQ's latency that is 11% on generation and 23% on summarisation (10% and 26% at 3.2
   req/s). Write it this way: queue depth recovers most of what calibration offers everywhere,
   and the residual matters more, relatively, on the workload where the machines differ most.
4. **The best policy moves with the workload.** WJSQ has the lowest mean on generation at both
   loads and on balanced at both loads (at 2.4 req/s on balanced it ties Threshold, intervals
   overlap, and Threshold has the better tail). On summarisation Threshold is best at both
   loads, on the mean (440 against 560 ms) and far more on the tail (p95 663 against 1370 ms).
5. **Threshold wins only while the fast node alone has room.** Threshold collapses on
   generation at 3.2 req/s (mean 3476 ms, p95 7917 ms), where the 3050 by itself saturates
   (R 1.39: little spare speed). On summarisation, R 2.59, the 3050 alone absorbs 3.2 req/s at
   503 ms. This is the H2 shape (hardware-awareness collapses into a static cut at high R)
   appearing along the workload axis. It is confounded with load headroom on a two-node pool, so
   state it as consistent with H2 and leave the claim to the simulator sweep.
6. **Where work goes.** WJSQ sends 67 to 68% to the 3050 on generation, 70 to 71% on balanced
   and 77 to 78% on summarisation; JSQ 57 to 60%, 61 to 65% and 69 to 72%. Queue depth alone
   shifts work toward the fast node more as R grows, which is why JSQ closes most of the gap
   (`routing_share_<shape>.png`).

Do not write: that calibration's value "increases with R" without saying queue-blind or
queue-aware; that Threshold beats WJSQ in general; H2 from these runs alone.

## 6. What we contrast against

### 6.1 Inside the study

- **RoundRobin** is the hardware-blind, queue-blind floor.
- **JSQ** is the claim that queue depth is already a speed signal.
- **StaticWeighted** is calibration without queue information.
- **WJSQ** is both. The 2x2 is what lets us separate the two sources of gain, which a ranked
  list of policies cannot.
- **Threshold(T)** is the one-line static rule H2 says hardware-awareness collapses into.

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
   real heterogeneous consumer LLM nodes with a pinned engine and validated alongside a
   simulator running the same policy code. In the work reviewed above, calibration is assumed
   (placement and routing systems) or modelled asymptotically (queueing theory); we did not
   find it measured this way.
2. Evidence on one pair of machines that the heterogeneity a scheduler faces is
   workload-dependent (1.39x to 2.59x), that the value of calibration to a queue-blind router
   moves with it by more than an order of magnitude (146 to 5484 ms at the same load), and
   that a queue-aware router recovers all but a roughly constant 110 to 225 ms of it.
3. A methodological finding: the choice of capability number alone moved the pool from 1.18x
   to 1.57x as the policies saw it, so any study reporting "hardware-aware routing helps"
   should report which number it weighted by.

Do not claim: that phase asymmetry is new (Splitwise, DistServe); that we propose a new
scheduler; generality beyond 1B models, four slots and two to three nodes.

## 7. Figures and tables

| # | File | Shows | Source | Status |
|---|---|---|---|---|
| F1 | `figures/paper/phase_ratio.png` | R on service, prefill and decode for each workload shape | cost models, `phase_ratio_3050_over_1650ti.json` | ready |
| F2 | `figures/paper/latency_by_policy_anchor.png` | p50, p95, p99 per policy against load, with intervals | `mpr2_1650ti_3050/summary.json` | ready |
| F3 | `figures/paper/h1_interaction.png` | H1 interaction on mean and p95 against load, one line per workload, with intervals | all four `summary.json` | ready |
| F4 | `figures/paper/routing_share_anchor.png` | share of requests sent to the fast node per policy | same | ready |
| F5 | `figures/paper/calibration_gain_by_shape.png` | calibration gain, queue-blind and queue-aware, per workload shape at 2.4 and 3.2 req/s | all four `summary.json` | ready; the lead figure for the elevation |
| F5b | `figures/paper/latency_by_policy_{generation,balanced,summarisation}.png`, `routing_share_{…}.png` | as F2 and F4, per shape trace | shape `summary.json` | ready |
| F6 | `figures/mpr2_1650ti_3050/latency_vs_load.png`, `throughput_vs_load.png`, `queue_wait_vs_load.png` | load characterisation pooled across policies | `runset.parquet` | usable as supplementary |
| F7 | validation figure | F-23 on one node | anchors | exists; two-node version **pending** |
| T1 | section 5.2 | phase R by workload | F1's source | ready |
| T2 to T4 | section 5.4 | mean, p95, H1 interaction, anchor | `summary.md` | ready |
| T5 to T7 | section 5.6 | mean, p95, calibration gain by shape | shape `summary.md` | ready |

`figures/mpr2_1650ti_3050/node_utilization.png` is re-rendered with the multi-run fix and
usable as supplementary. Do not use `figures/shapes_1650ti_3050/phase_advantage.png`: it pools
every load point per shape, the anchor adds 1.3 req/s that the others lack and summarisation has
no RoundRobin at 3.2, so its zig-zag is an artefact of pooling. F5 is the correct version.

## 8. Threats and limitations to state

- **Scale.** 1B model, four slots, two nodes, consumer GPUs. Datacenter engines with
  continuous batching may behave differently.
- **One real pair so far.** Four workload R values on one pair of machines; H2 needs the
  simulator sweep and more pairs.
- **Two load points per shape.** The shape campaigns ran at 2.4 and 3.2 req/s only.
- **Anchor and shape traces differ in arrivals,** so the anchor row compares loosely with the
  three shape traces.
- **Engine restarts between runs** (section 3 item 8). Never inside a run; no prompt-cache hits.
- **Harness shares a host with the slow node.** Acceptable for GPU nodes with full offload;
  stated.
- **Wi-Fi.** RTT about 4 ms, transport overhead measured at 5.86 ± 2.66 ms per request on the
  single-node setup.
- **Short runs.** 200 requests per run on the anchor trace (600 on the shape traces); anchor p99
  intervals are wide.
- **Three repeats.** Intervals resample requests within runs; five repeats at the deciding
  points are planned (P5).
- **Capability definition.** Changed before any reported run; sensitivity to be reported.
- **Engine self-report.** The 1650 Ti binary reports build 1 instead of 10569. Same commit and
  patch; rebuild before submission.
- **τ is unresolved on GPU classes,** which limits what H3 can claim on hardware.

## 9. Pending, and where it lands in this brief

| Item | Lands in | Expected |
|---|---|---|
| Second and third real pairs | 5.4, 5.6, H2 discussion | needs CPU calibrations and a third machine for the harness |
| Two-node F-23 per policy | 5.5 | needs a driver (P4) |
| Simulator sweeps for H2 and H3 | new 5.7 and 5.8; settles 5.6 item 5 | after P4 |
| Per-request cost-model policy | 6.1, 6.3 | control plane, about a day |
| Capability sensitivity | 3 item 3 | about 1 h of runs |
| Five repeats at the deciding points | 5.4, 5.6 | generation 2.4, balanced 2.4, anchor 1.3 |

## 10. Path to a journal

Where we stand: a clean, interval-backed result on one real pair of machines, with the
elevation shown on hardware. That is enough for a workshop or an IISWC-style
characterisation paper. A mid-tier journal needs the result on more than one pair, a
simulator we have validated on the heterogeneous pool, and a calibrated policy strong enough
that "queue depth recovers most of it" is not a claim against a strawman.

| Target | Needs | Where we are |
|---|---|---|
| Workshop, or an IISWC-style characterisation paper | P1, P2, P5, P8 | P1 and P2 done; P5 has intervals, five repeats pending; P8 drafted from abstracts |
| Mid-tier journal (for example Journal of Supercomputing, Future Generation Computer Systems, Cluster Computing) | the above plus P3, P4, P6, P7 | not started |
| A noticeably stronger submission | the above plus S2 (a pair where the faster node flips by phase) and S3 (capability sensitivity) | not started; S2 needs a machine we do not have |

What each remaining must-have changes in the paper (IDs match `docs/checkpoint.md`):

| ID | Work | Owner | Cost | What it changes in the paper |
|---|---|---|---|---|
| P3 | Two more real pairs: CPU calibrations on both laptops, then 1650 Ti CPU + 3050 GPU and one more | Divyansh calibrates, both run | 8 to 12 h machine | 5.4 and 5.6 become a range across R instead of one pair; H1 and the elevation either hold or visibly change |
| P4 | Replay each hardware run through the simulator and compare per policy (F-23 on the two-node pool) | Aditya | about a day of code, minutes of simulator time | 5.5 extends from one node to the heterogeneous pool; every simulator result becomes citable |
| P6 | A policy that prices each request from the full cost model, rerun as an extra arm on P1 and P3 | Aditya writes, both run | about a day of code, 4 h machine | 6.1 gains the strongest calibrated baseline; 6.3 claim 1 is earned against it |
| P7 | Simulator sweeps over R 1 to 100, phase skew and staleness | Aditya | half a day to configure, hours of simulator time | new 5.7 (H2) and 5.8 (H3); settles whether 5.6 item 5 is H2 |
| P5 | Five repeats at the deciding points (generation 2.4, balanced 2.4, anchor 1.3 req/s) | Divyansh | about 3 h machine | narrows the three intervals that sit near zero or overlap |
| P8 | Read the related work in full, not only abstracts | writing team | 2 to 3 days | 6.2 and 6.3 become citable |

Order: P4 and P6 need no hardware and start now, alongside the P3 calibrations. P3's
campaigns follow, with P6's arm included so it does not need a separate rerun. P7 runs once
P4 passes. P5 repeats come last, once we know which points decide the result. About 18 to 22
hours of machine time remain, plus the control-plane code.

What the writing team can draft now: the method (sections 3 and 4), results 5.1 to 5.6 as
single-pair results, and related work from 6.2. Hold the discussion and conclusion until P3
and P7, since both can change the headline. If the journal items slip, sections 1 to 8 as
they stand are the workshop paper.
