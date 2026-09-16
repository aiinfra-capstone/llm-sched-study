# Checkpoint

What we have measured, what our code can still measure, and what stands between that and a
paper. We update this after every campaign. Status as of **2026-09-15, evening**, after P2 and the
audit of the first pair's design and analysis (issue #21 for the control-plane half).

`[x]` done, `[~]` running or partial, `[ ]` not started. Owners: **D** Divyansh (data plane),
**A** Aditya (control plane), **J** joint.

---

## 1. Where the study stands

| Result | Needs | Status |
|---|---|---|
| **MPR-1** throughput nonstationarity | τ and the variance envelope on real nodes | `[~]` not resolved on any class. CPU 8B point estimate 69.5 s, record says `tau_resolved: false`, block-bootstrap interval 31 to 91 s, lag-1 0.48 [0.03, 0.60]; censored at ≤ 5 s on every 1B GPU class. An 80-minute CPU 1B segment is configured |
| **MPR-2** H1 2x2 on real hardware | two-host pool, 2x2 at several load points, a range across R, repeats that sample arrivals | `[~]` first pair: 132 valid runs, but every repeat replayed one trace with seed 42. Re-analysed with paired block bootstrap and steady-state gating: H1 interaction defined at 5 of 9 points, zero at the 2 where RoundRobin's slow node is least loaded (0.41 and 0.73 of its capacity) and positive at the 3 where it is at 0.73 to 0.97. "Shrinks with load" not supported |
| **MPR-3** H2 and H3 in a validated simulator | F-23 on a heterogeneous pool judged on contrasts, R and staleness sweeps with load normalised, N > 2, a drift process and a non-circular quality metric | `[~]` the simulator gained separate random streams, pools above two nodes, per-node transport, ECT and two deterministic baselines (92976e9). Still open: the two-node validation has a driver (`tools/p4_validate.py`) but has not been run against the shape sets; sweeps still set load as `rate_scale` rather than as utilisation; and the simulator has no drift process, no online capability estimate and no regret metric, so H3 cannot be measured |
| **Elevation 1** R depends on the workload's phase mix | phase-split cost models on a real pair, the shapes through the 2x2 at matched load | `[~]` R moves with the workload (1.4 to 2.6x at one slot, 2.2 to 4.4x at four). The queue-blind gain "146 to 5484 ms" is withdrawn (transient cells, load moving with R). Candidate: WJSQ/JSQ 0.889 on generation to 0.769 on summarisation at 2.4 req/s. Needs the matched-load campaign |
| **Mid-tier journal** | the above, plus section 4 | `[ ]` |

---

## 2. Data we have

### Cost models (C-3, committed under `contracts/cost_models/`)

| Node class | Host | Snapshots | Headline tok/s | τ | Phase split |
|---|---|---:|---:|---|---|
| `cpu_ngl0_p4_q4km_llama3_8b` | fedora | 23 | | 69.5 s, resolved | `[x]` |
| `gtx1650ti_ngl20_p4_q4km_llama3_8b` | fedora | 18 | | ≤ 32 s | `[x]` |
| `gtx1650ti_ngl99_p4_q4km_llama32_1b` | fedora | 9 + 9 | 50.8 | ≤ 5.0 s | `[x]` |
| `rtx3050_ngl99_p4_q4km_llama32_1b` | rtx3050 | 9 | 124.5 | ≤ 5.0 s | `[x]` |

The two 8B classes were calibrated at a single grid point, so no workload profile can be
priced from them. Every 1B class above shares the 24-cell anchor grid.

### Heterogeneity of the first real pair (`tools/phase_ratio.py`, concurrency 1)

RTX 3050 over GTX 1650 Ti:

| Profile | Prompt:output | R service | R prefill | R decode |
|---|---:|---:|---:|---:|
| summarisation | 13.76 | 2.59 | 11.12 | 1.20 |
| anchor | 3.00 | 1.76 | 9.43 | 1.19 |
| balanced | 2.00 | 1.58 | 9.79 | 1.20 |
| generation | 0.50 | 1.39 | 8.64 | 1.19 |

Capability as the scheduler seeds it (`com.sched.core.Capability`): 103.9 and 163.6 output
tok/s of service, 1.57x.

### Single-node characterisation (GTX 1650 Ti, 1B)

- `[x]` Admissible envelope: prompt ≤ 512, output ≤ 128 (`runs/admissible/llama32-1b.json`)
- `[x]` Load band 1.035 to 1.305 req/s, `policy_separable: false` (`runs/anchors/load_band.json`)
- `[x]` F-23 anchors at 0.72, 1.035, 1.305 and 1.98 req/s; simulator within ±25% on p50 and p95 at all four
- `[x]` Transport overhead 5.86 ± 2.66 ms per request
- `[x]` Determinism: 200 of 200 decisions identical across two simulator runs

### Hardware campaigns

| Campaign | Pool | Trace | Points (req/s) | Policies x repeats | Staleness | Status |
|---|---|---|---|---|---|---|
| `mpr2_1650ti_3050` | 1650 Ti + 3050, Wi-Fi | anchor | 1.3, 2.4, 3.2 | 5 x 3 | 0 | `[x]` 45 of 45 valid, `summary.md` |
| `phase_generation_1650ti_3050` | same | generation | 2.4, 3.2 | 5 x 3 | 0 | `[x]` 30 of 30 valid, `summary.md` |
| `phase_balanced_1650ti_3050` | same | balanced | 2.4, 3.2 | 5 x 3 | 0 | `[x]` 30 of 30 valid, `summary.md` |
| `phase_summarisation_1650ti_3050` | same | summarisation | 2.4, 3.2 | 5 x 3 | 0 | `[x]` 27 of 30 valid; RoundRobin at 3.2 req/s saturates in all three repeats (522 of 600 completed) and is reported as saturation. `jsq_heavy_r3` lost 3 responses in warmup |

All four: one trace per campaign, scheduler seed 42 in every repeat, so the repeats sample
hardware jitter only. `summary.{json,md}` beside each run set is the re-derived version;
`summary_iid_v1.*` is the superseded one. Transient cells: RoundRobin on generation 3.2,
balanced 3.2 and summarisation 2.4; StaticWeighted on balanced 3.2 and summarisation 3.2;
Threshold on generation 3.2 and balanced 3.2. RoundRobin on balanced at 2.4 req/s is the
borderline case, steady by the rule with an interval that reaches 1.51.

### Configured, not yet run (the 3050 laptop has to come back)

| Config | What | Runs | Replay time | Audit item |
|---|---|---:|---:|---|
| `hw_calibration_ablation_3050.json` | the value-of-calibration curve: capability ratios 1.0 to 100x, the decode-only definition (S3), the four-slot reference cell, `jsq_fastfirst`, `static_weighted_wrr` and `ect` in both modes, 3 seeded repeats at 0.3 of pool capacity | 48 | about 1.5 h | M2, C5 |
| `hw_seeded_anchor_3050.json` | anchor trace, 5 repeats each with its own arrivals and scheduler seed, load at 0.2, 0.3, 0.4 of pool capacity | 75 | about 2.6 h | C1, C2, M1 |
| `hw_staleness_h1_3050.json` | 2x2 at 0.3 of capacity, veil at 0, 1 and 5 s, 3 seeded repeats | 36 | about 1.2 h | C6 |
| `hw_heavytail_3050.json` | 66-bucket lognormal lengths, Poisson and MMPP interleaved, 0.3 and 0.4 of capacity, 3 seeded repeats | 60 | about 3.1 h | C6, M7 |
| `hw_shapes_matched_3050.json` | three shapes interleaved at 0.7 slow-node utilisation, 3 seeded repeats | 45 | about 4.0 h | C3, D2 |

Replay time excludes scheduler start-up (about 10 s per run). The first three fit one night
at about 7 h; the matched shapes are a second night.
| `demonstrate_jsq` | co-located | | | | | invalid by design, not a data point |

### Simulator sweeps

- `[ ]` None for the paper. `runs/sweeps/capacity_probe_1b` is a historical capacity probe.

---

## 3. What our code can collect

Every row is runnable today unless it says otherwise.

### Per node class

| Measurement | Command | Gives |
|---|---|---|
| Calibration | `uv run calibrate --config configs/calibration_1b_<class>.json` | C-3 snapshot series, τ, envelope, headline tok/s |
| Phase split | `tools/backfill_phase_split.py` | prefill and decode per cell (run after every calibration) |
| R range | `uv run r-range` | deployable R across calibrated classes |
| Phase R | `tools/phase_ratio.py --fast --slow --profile ...` | R on service, prefill and decode, per profile |
| Admissible set | `uv run admissible` | F-13 envelope and cliff |
| Cost-model check | `uv run costcheck runs/exp` | whether C-3 predicts its own hardware (F-7) |

### Node classes reachable with the machines we have

A node class is a machine plus its settings, so two laptops give more than two classes. Two
classes can only share a live pool if they are on different hosts.

| Host | Class | Config | Why we want it | Cost | Status |
|---|---|---|---|---|---|
| fedora (GTX 1650 Ti, Ryzen 5 4600H) | GPU `ngl 99` | `calibration_1b_anchorgrid.json` | the slow GPU of the first pair and the F-23 anchor node | done | `[x]` |
| fedora | CPU `ngl 0` | `calibration_1b_cpu.json` | a large-R partner for the 3050, and a class whose prefill and decode lose ground at different rates than any GPU | about 45 to 60 min (CPU is slow) | `[ ]` |
| fedora | partial offload | new config | an intermediate R on the same box, filling the gap between GPU and CPU (F-9a) | about 30 to 45 min | `[ ]` |
| rtx3050 (RTX 3050 6GB, i5-13450HX) | GPU `ngl 99` | `calibration_1b_rtx3050.json` | the fast GPU of the first pair | done, took about 25 min | `[x]` |
| rtx3050 | CPU `ngl 0` | new config, copy of `calibration_1b_cpu.json` | a second CPU class on a newer CPU, so the CPU result is not one machine | about 45 min | `[ ]` |
| rtx4070 (desktop, X9 boot) | GPU `ngl 99` | `calibration_1b_rtx4070.json` | a high-bandwidth GPU, the only way to get a large decode R | about 25 min once the box is available | `[ ]` needs the box |

Real pairs that gives us, each a different R and phase profile:

| Pair | Expected | Why we want it | Cost | Status |
|---|---|---|---|---|
| 1650 Ti GPU + 3050 GPU | R 1.4 to 2.6, prefill-driven | first real H1 point; service R moves with the workload while decode R stays at 1.2 | 1.6 h anchor, 8 to 12 h shape traces | `[x]` done |
| 1650 Ti CPU + 3050 GPU | large R | second point on the MPR-2 range, near where H2 predicts thresholding takes over | 1 h calibration, 2 to 3 h anchor campaign at lower rates, third machine for the harness | `[ ]` |
| 1650 Ti GPU + 3050 CPU | measure; the GPU should win prefill by far more than decode | a pair where the host roles are reversed, so a host effect is not mistaken for a class effect | 45 min calibration, 2 to 3 h campaign | `[ ]` |
| 1650 Ti CPU + 3050 CPU | CPU against CPU | heterogeneity without a GPU at all, the common case for small edge deployments | both CPU calibrations, 3 h campaign, third machine for the harness | `[ ]` |
| 3050 GPU + 4070 GPU | decode R larger than this pair's | the only pair where decode heterogeneity is large, which is the other side of the elevation's argument | 25 min calibration, 1.6 h campaign, the box | `[ ]` needs the box |

A CPU node on `fedora` shares its CPU with the scheduler and replay client. For those pairs
the harness should move to a third machine, or the contention goes in the threats table.

### Per pool (hardware, `tools/hw_runs.py`)

Axes one campaign config can vary: policies, load points, staleness list, repeats, trace.

| Trace | Requests | Base duration | One campaign (5 policies, 3 points, 3 repeats) |
|---|---:|---:|---|
| anchor | 200 | 222 s | about 1.6 h |
| summarisation, balanced, generation | 600 | 666 s each | about 4 h each at the same points |

Staleness multiplies the whole campaign by the number of values.

### Simulator (`tools/sweep.py`)

| Axis | Supported | Note |
|---|---|---|
| R | `[x]` | synthesised by scaling a measured snapshot |
| phase skew | `[x]` | prefill and decode slowed by different amounts |
| staleness | `[x]` | |
| load | `[x]` | |
| policy | `[x]` | |
| pool size N | `[x]` | `--k-slow` builds 1 fast + k slow pools |
| per-policy F-23 on a two-node pool | `[~]` | `tools/p4_validate.py` replays a hardware manifest through `SimApp`; not yet run against the held-out shape sets on the contrast criterion |
| load at fixed utilisation | `[ ]` | still `rate_scale`, so utilisation rises with R and a rise-then-fall can come from saturation alone |
| drift, online capability estimate, counterfactual regret | `[ ]` | none exist; H3 is not measurable without them. `plots.h3_staleness` draws `regret_ms` as soon as a run set carries it, and says on the figure when it fell back to the circular measure |
| separate random streams for policy and service noise | `[x]` | policy and per-node service noise draw from separate streams |
| capability overrides, ECT, two deterministic baselines | `[x]` | `capability_override`, `capability_concurrency`, `capability_mode`, `ect_mode`; `jsq_fastfirst`, `static_weighted_wrr`, `ect` |

### Figures (`uv run figures`)

| Figure | Needs | Status |
|---|---|---|
| `latency-vs-load`, `throughput-vs-load`, `queue-wait-vs-load`, `node-utilization` | any run set | `[x]` `figures/mpr2_1650ti_3050/` |
| `validation` | matched hardware and simulator points | `[x]` single node |
| `h1-decomposition` | the 2x2 at one R | `[x]` anchor; the per-point version with intervals is `tools/paper_figures.py` |
| `mpr2-range` | the 2x2 at two or more R | `[ ]` needs a second pair |
| `h2-advantage` | a sweep over R | `[ ]` needs a simulator sweep |
| `phase-advantage` | the 2x2 at two or more workload shapes | `[x]` renders on the combined set, but pools load points unequally; `figures/paper/calibration_gain_by_shape.png` is the per-point version to use |
| `h3-staleness` | staleness values and `--tau-s` | `[ ]` plots `routing_error_rate`, which is 0 for WJSQ by construction; switch to counterfactual regret once the simulator logs it |
| `tools/paper_figures.py` H1 and calibration gain | re-derived `summary.json` | `[x]` log interaction primary, steady points only, ratio and ms per router |
| Threshold baseline on the H2 curve | `THRESHOLD_BASELINE` in `plots.py` | `[x]` `h2_advantage_curve` carries `threshold_gap_ms`, and refuses a sweep that ran the baseline at some R and not others |
| The specification's H2 observable | `h2_calibration_curve` | `[x]` WJSQ minus JSQ drawn beside best-aware minus best-blind, so the deviation is visible |

---

## 4. Plan to a publishable result

Revised after the audit. The earlier plan added breadth (pairs, sweeps, repeats) on a design
whose problems were in the analysis and the instruments. The order below fixes those first.
Control-plane items are in issue #21, which supersedes parts of #20.

### Done since the audit (data plane, no machine time)

| # | What | Where |
|---|---|---|
| R1 | Re-analysis of all 132 runs: paired block bootstrap, steady-state gate on every cell, log-scale interaction as the primary H1 statistic, per-node utilisation and operating R, worker-side TTFT, TPOT and SLO attainment, whole-run failure counts, `routing_error_ms` dropped | `tools/campaign_summary.py`, every `summary.*` |
| R2 | Brief rewritten on the re-derived numbers, including the deviations from the spec and the withdrawn claims | `docs/writing-brief.md` |
| R3 | Campaign driver: per-repeat trace seeds and scheduler seeds, interleaved workloads, load as a utilisation target, engine restart detection, engine command line, library and weights hashes in every manifest, failures counted over the whole run | `tools/hw_runs.py`, `tools/pool_load.py` |
| R4 | Worker waits for the client channel before delivering, and counts undelivered responses | `dataplane/worker/serve.py` |
| R5 | Intervals on capability and phase R, at every concurrency | `tools/cell_intervals.py` |
| R6 | τ with a block-bootstrap interval and a detrended fit; 80-minute CPU 1B segment configured | `tools/tau_interval.py`, `calibration_1b_cpu.json` |
| R7 | Engine-restart effect on the 3050: +0.05%/h [-0.19, +0.32] | `tools/restart_effect.py` |
| R8 | Heavy-tailed and MMPP trace configs; four seeded campaign configs | `tools/make_length_mix.py`, `dataplane/configs/` |
| R9 | llama-bench script with build flags and hashes, for the 1650 Ti rebuild check | `tools/engine_bench.sh` |
| R10 | README corrected: τ, the batching claim, the F-23 sentence, `--cache-ram 0` | `README.md` |

### Must have, in order

| # | Status | What | Why | Cost | Owner | Done when |
|---|---|---|---|---|---|---|
| M1 | `[ ]` | **Engine and host control before any new pair.** Run `tools/engine_bench.sh` on both nodes; rebuild the 1650 Ti with `pool-install.sh`; bench again; compare pp512 and tg128 with published numbers for both cards; recalibrate the 1650 Ti under driver 580.178.04, `-c 55296`, `--cache-ram 0` | If the rebuild moves the 1650 Ti's prefill, the 10x prefill R was partly an engine effect and P1 and P2 need redoing. Far cheaper to learn before P3 | 1 h bench and rebuild, 25 min calibration | D | both bench records committed and the prefill ratio stated with or without a change |
| M2 | `[ ]` | **Seeded anchor rerun** (`hw_seeded_anchor_3050.json`) | Intervals that sample arrivals and routing draws; load as utilisation; StaticWeighted on five different streams | 2.6 h | J | summary with `arrivals_independent: true` |
| M2b | `[ ]` | **Calibration ablation on hardware** (`hw_calibration_ablation_3050.json`), after the simulator curve | The hardware points on the value-of-calibration curve, plus S3 | 1.5 h | J | every arm in one run set, labelled by `capability_arm` |
| M3 | `[ ]` | **H1 under stale queue counts** (`hw_staleness_h1_3050.json`) | The most direct test of whether "queue depth recovers most of it" survives imperfect queue information | 1.2 h | J | interaction per staleness value |
| M4 | `[ ]` | **Heavy-tailed and bursty workloads** (`hw_heavytail_3050.json`) | The workload so far is the regime most favourable to JSQ | 3.1 h | J | queue-aware and queue-blind gain per workload with intervals |
| M5 | `[x]` | **Seeds and separate random streams in the simulator; `jsq_fastfirst` and `static_weighted_wrr`** | Common random numbers for policy contrasts; the ordinal-only control | done in 92976e9 | A | both policies run live and simulated |
| M6 | `[~]` | **P4 on contrasts, on the held-out shape runs** | Nothing from the simulator is citable until it reproduces the ranking, WJSQ/JSQ and the interaction within the hardware intervals | driver exists; a run and a verdict remain | A (#21 section 0) | criteria met on the shape run sets, or the misses explained |
| M7 | `[~]` | **Value-of-calibration curve**: capability ratio 1.0 to very large, plus `jsq_fastfirst` and `ect`, in the simulator; then the hardware arm | Answers whether calibrated magnitude adds anything beyond ranking. It decides which paper this is | code done; 1.5 h hardware (`hw_calibration_ablation_3050.json`) | A ran the code, J runs | curve with intervals, hardware arm on it |
| M8 | `[ ]` | **Shapes at matched load**: simulator first (#21 item 9), then `hw_shapes_matched_3050.json` | Separates R from slow-node load on the elevation's axis | 4 h hardware | A, J | queue-aware gain by shape at matched load |
| M9 | `[x]` | **P6 per-request cost-model policy**, known and unknown output length | The strongest calibrated baseline; needed on the heavy-tailed traces | done in 92976e9 as `ect` | A | runs live and simulated |
| M10 | `[ ]` | **P3: more pairs**, only after M1 to M4, with the harness on a third host for any CPU node | One pair is an anecdote; running P3 before the fixes copies every design problem into three pairs | 8 to 12 h | D calibrates, J runs | `mpr2-range` renders across three R |
| M11 | `[~]` | **N ≥ 3 and load normalisation in the simulator**, then P7 for H2 | H2's curve at fixed λ can come from saturation alone; two nodes is the easiest case for JSQ | N ≥ 3 done (`--k-slow`); the sweep still sets load as `rate_scale`, so the normalisation half is open | A (#21 item 7) | H2 at fixed utilisation and fixed λ, 1 fast + k slow |
| M12 | `[ ]` | **H3 instrument**: drift, online capability estimate, counterfactual regret; then the τ measurement on CPU 1B | H3 has no evidence under the current instruments | several days; 80 min calibration | A (#21 item 5), D | regret changes with staleness in units of τ |
| M13 | `[~]` | **Literature check and positioning** (was P8) | Spec threat R1, and "two-server queueing theory, restated" is the likeliest rejection | 2 to 3 days | J | related work read in full |

P5 as planned (five repeats at the points whose intervals sit near zero, same trace and seed)
is dropped: identical traces add almost no information, and choosing points by how close
their intervals sit to zero is optional stopping. M2 replaces it with independent seeds on a
pre-declared set of points.

### Strengthens it

| # | Status | What | Why | Cost | Owner |
|---|---|---|---|---|---|
| S1 | `[ ]` | **Harness on a third host** for one anchor rerun | The slow node shares a 6-core host with the scheduler and the client | 2.6 h, a third machine | J |
| S2 | `[ ]` | **A pair where the faster node flips by phase** | The other route to an LLM-specific result besides the heavy-tailed workloads | a machine we do not have | J |
| S3 | `[ ]` | **Recalibrate the 3050 once more** | One calibration per class, no day-to-day variation on any cell | 25 min | D |
| S4 | `[ ]` | **Larger admissible envelope** (output to 512) | The heavy-tailed trace is clamped at 128 output tokens | about 1 h per class | D |
| S5 | `[ ]` | **Wired LAN** | Transport differs by node; lower value than S1 | 1.6 h | J |
| S6 | `[ ]` | **One 8B point** | External validity; lower value than M4 | 2 to 4 h per class | D |
| S7 | `[ ]` | **Threshold baseline and the spec's H2 observable in `plots.py`** | H2 is WJSQ - JSQ converging to Threshold in the spec | half a day, after M11 | D |

### Machine time

| Block | Hours |
|---|---:|
| M1 | 1.5 |
| M2 + M3 + M4 (one night) | 7 |
| M2b (ablation arms) | 1.5 |
| M8 hardware | 4 |
| M10 | 8 to 12 |
| S1 | 2.6 |
| **Remaining** | **25 to 29** |

## 5. Threats we track

| Threat | Where it bites | Status |
|---|---|---|
| Capability definition decides H1 | policies weighted by one scalar | fixed to service rate on 2026-09-15; S3 reports the sensitivity |
| Engine setting drift across nodes | llama-server fits context to VRAM | pinned `-c 55296` on every node |
| Laptop thermals and power profile | throughput drift inside a campaign | performance profile, plugged in, calibration CV 0.009 on the 3050; policy order shuffled per point |
| Wi-Fi jitter | tail latency | power save off, about 4 ms RTT; transport overhead measured; S4 |
| Harness shares a host with a node | CPU contention on `fedora` | acceptable for GPU nodes; needs a third machine for CPU-node pairs |
| 1650 Ti engine reports build 1 | engine identity in the manifest | same commit and patch as the pin; rebuild before the paper |
| Scale: 1B, 4 slots, consumer GPUs | external validity | frame as heterogeneous consumer serving; S5 |
| Short trace, 200 requests per run | p99 noise | P5 repeats and CIs |
| Novelty (spec R1) | the whole contribution | P8 |
| llama-server's host prompt cache (`--cache-ram`, 8 GB by default) | host memory grew to 5.8 GB on the 1650 Ti laptop and pushed it into swap; a cache hit would also skip prefill | engine logs show every prompt evaluated at full length, so no hit occurred; both engines restarted with the identical command between runs at campaign boundaries; pass `--cache-ram 0` from the next calibration on and record it |
| Anchor and shape traces use different arrivals | comparing the anchor row with the shape rows | compare the three shape traces with each other; the anchor only loosely |
| One arrival path and one routing stream per campaign | every interval on the first pair | open; seeded campaigns configured (M2 to M4, M8) |
| Load moves with R across the shapes at a fixed req/s | the elevation | open; matched-load campaign (M8) |
| Capability is a one-slot scalar, about 2x below operating R | calibrated policies look weaker than they could | open; ratio sweep (M7) and P6 (M9) |
| Workload favours JSQ (known, near-constant lengths, Poisson, exact counts, N = 2) | "queue depth recovers most of it" | open; M3, M4, M11 |
| 1650 Ti build flags unknown; cost model predates the context pin and campaign driver | R as a hardware property | open; M1 |
| Transient cells counted as data | H1 interaction | fixed in the analysis: steady-state gate on every cell |
| iid bootstrap on autocorrelated latencies | every interval | fixed: paired block bootstrap |
| `routing_error_ms` is WJSQ's own score | H3's metric | dropped from summaries; counterfactual regret needed (M12) |
| Responses lost during warmup did not invalidate a run | validity | fixed in the worker and the campaign driver |
| Engine restarts between runs unrecorded | validity and drift | driver now records engine start time and command line per run |
| Anchor trace used for development and evaluation | every anchor result | anchor results reported as development results; shapes are the held-out set |
| Spec PDF disagrees with the analysis on H1's sign and H2's observable | reviewers given the spec | recorded as deviations in the brief; the PDF needs the same note |

---

## 6. Update log

- **2026-09-15** Second node brought up on the RTX 3050 laptop from the X9. 3050 calibrated.
  Capability changed from decode tok/s to service rate in both vehicles. P1 started.
- **2026-09-15, overnight** P1 done, all 45 valid; H1 interaction grows with load. Generation-shape campaign done, all 30 valid. Engines restarted between runs because llama-server's default host prompt cache exhausted memory; no cache hit found in the logs. Fixed the `node-utilization` and `phase-advantage` figures; suite 733 passed, 1 skipped, 100% coverage, no test changed.
- **2026-09-15, morning** Balanced (30 of 30) and summarisation (27 of 30) done, so P2 is done. The best policy moves from WJSQ to Threshold as the prompt share grows; Threshold collapses on generation at 3.2 req/s. Paper figures rendered for all four shapes. Pool shut down; the 3050 laptop returned to its owner and its worker and engine logs copied to `runs/`. Results written up in `docs/writing-brief.md`.
- **2026-09-15, evening** Audited the first pair as a reviewer would and re-analysed all 132 runs
  with a paired block bootstrap and a steady-state gate. H1's interaction is defined at 5 of 9
  points of nine and zero on the ratio scale where RoundRobin is comfortably stable; "shrinks
  with load" is not supported; the queue-blind elevation result is withdrawn. Plan reordered (section 4),
  control-plane half filed as #21, four seeded campaigns configured. Suite 733 passed, 1 skipped,
  100% coverage, no test changed.
