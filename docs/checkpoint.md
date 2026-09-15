# Checkpoint

What we have measured, what our code can still measure, and what stands between that and a
paper. We update this after every campaign. Status as of **2026-09-15**, after P2.

`[x]` done, `[~]` running or partial, `[ ]` not started. Owners: **D** Divyansh (data plane),
**A** Aditya (control plane), **J** joint.

---

## 1. Where the study stands

| Result | Needs | Status |
|---|---|---|
| **MPR-1** throughput nonstationarity | τ and the variance envelope on real nodes | `[x]` τ resolved on one class (CPU 8B, 69.5 s, r² 0.989); censored at ≤ 5 s on every 1B class |
| **MPR-2** H1 2x2 on real hardware | two-host pool, 2x2 at several load points, a range across R | `[~]` first real pair done: anchor plus three shape traces, 132 valid runs; one pair only |
| **MPR-3** H2 and H3 in a validated simulator | F-23 on a heterogeneous pool, R and staleness sweeps | `[ ]` F-23 passes on one node only; no paper sweep run |
| **Elevation 1** R depends on the workload's phase mix | phase-split cost models on a real pair, the three shape traces run through the 2x2 | `[~]` measured on one pair: queue-blind calibration gain rises 146 to 5484 ms with R at 2.4 req/s, queue-aware gain stays 112 to 168 ms; needs a second pair |
| **Mid-tier journal** | the above, plus section 6 | `[ ]` |

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
| `phase_summarisation_1650ti_3050` | same | summarisation | 2.4, 3.2 | 5 x 3 | 0 | `[x]` 27 of 30 valid; RoundRobin at 3.2 req/s saturates in all three repeats (522 of 600 completed) and is reported as saturation |
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
| pool size N | `[ ]` | every sweep point is two nodes; the spec's N up to 12 needs a runner change |
| per-policy F-23 on a two-node pool | `[ ]` | needs a driver that replays a hardware manifest through `SimApp` and runs `tools/f23_compare.py` per run |

### Figures (`uv run figures`)

| Figure | Needs | Status |
|---|---|---|
| `latency-vs-load`, `throughput-vs-load`, `queue-wait-vs-load`, `node-utilization` | any run set | `[x]` `figures/mpr2_1650ti_3050/` |
| `validation` | matched hardware and simulator points | `[x]` single node |
| `h1-decomposition` | the 2x2 at one R | `[x]` anchor; the per-point version with intervals is `tools/paper_figures.py` |
| `mpr2-range` | the 2x2 at two or more R | `[ ]` needs a second pair |
| `h2-advantage` | a sweep over R | `[ ]` needs a simulator sweep |
| `phase-advantage` | the 2x2 at two or more workload shapes | `[x]` renders on the combined set, but pools load points unequally; `figures/paper/calibration_gain_by_shape.png` is the per-point version to use |
| `h3-staleness` | staleness values and `--tau-s` | `[ ]` |
| Threshold baseline on the H2 curve | `THRESHOLD_BASELINE` in `plots.py` | `[ ]` not implemented; its test is skipped |

---

## 4. Plan to a publishable result

Ordered by what a reviewer would ask first. Cost is machine time plus people time.

### Must have

| # | Status | What | Why it is needed | Cost | Owner | Done when |
|---|---|---|---|---|---|---|
| P1 | `[x]` | **First pair, anchor trace.** `mpr2_1650ti_3050`, 45 runs, then pipeline, costcheck, runset and figures | The first H1 2x2 on real heterogeneous hardware. Without it MPR-2 does not exist. | 1.6 h machine, 1 h analysis | J | every run valid and `h1-decomposition` renders |
| P2 | `[x]` | **First pair, the three shape traces** (summarisation, balanced, generation) through the same 2x2 | The elevation's claim. On one pair, service R moves from 1.39 to 2.59 with the workload alone; if the policy ranking or H1's interaction term moves with it, that is the paper's result. | 8 to 12 h machine (600-request traces; two load points saves a third), half a day analysis | J | `phase-advantage` renders with three shapes |
| P3 | `[ ]` | **Two more real pairs.** Calibrate the CPU classes on both laptops, run the anchor campaign on 1650 Ti CPU + 3050 GPU and one more pair | One pair is an anecdote. H1 has to hold, or visibly change, across more than one R, and MPR-2 is defined as a range. | about 1 h per calibration, 2 to 3 h per campaign, 8 to 12 h total | D calibrates, J runs | `mpr2-range` renders across three R |
| P4 | `[ ]` | **Simulator against hardware on the two-node pool, per policy** | Every simulator figure (N to 12, R to 100, staleness) is only believable if the simulator reproduces real heterogeneous runs, not just one node. | a driver that replays a hardware manifest through `SimApp`, about a day; simulator time is minutes | A | each P1 policy and point within ±25% on p50 and p95, or the misses explained |
| P5 | `[~]` | **Confidence intervals and effect sizes.** Bootstrap CIs on p50, p95 and the H1 interaction term; five repeats at the deciding points | Three repeats of 200 requests leave p95 and p99 noisy. A reviewer rejects a policy ranking whose intervals overlap. | about 3 h machine; `bootstrap_halfwidth` already exists, half a day to wire it into the H1 figures | D | every H1 claim carries an interval |
| P6 | `[ ]` | **A policy that prices each request from the full cost model** | Our capability-aware policies squeeze a node into one number, and we saw that number alone move the pool from 1.18x to 1.57x. Without a per-request policy, scalar WJSQ reads as a strawman and "calibration is redundant" is not earned. | about a day of policy code, then its arm rerun on P1 to P3 (about a fifth of their machine time) | A | the policy runs live and in the simulator and passes the determinism test |
| P7 | `[ ]` | **Simulator sweeps for H2 and H3** over R 1 to 100, phase skew and staleness, parameterised from P1 | H2's non-monotonic curve and H3's staleness shift cannot be measured on two laptops. They are MPR-3. | cheap: minutes to hours of simulator time, half a day to configure and check | A | `h2-advantage` and `h3-staleness` render |
| P8 | `[~]` | **Literature check and positioning** against Splitwise, DistServe, Helix, HexGen, Mélange and 2025 heterogeneous-serving work | Spec threat R1. Prefill/decode asymmetry is already used by disaggregated serving; our question (does calibration add anything over queue depth, and does that depend on the phase mix) has to be shown to be new. | 2 to 3 days of reading and writing | J | related-work section drafted with the gap stated |

### Strengthens it

| # | Status | What | Why it is needed | Cost | Owner |
|---|---|---|---|---|---|
| S1 | `[ ]` | **Staleness on hardware** at 0, 1 and 5 s on P1's pool | H3 measured on real drift, not only in the simulator. τ is censored at ≤ 5 s on the 1B classes, so this needs care in the write-up. | about 1.6 h per extra staleness value on the anchor trace, 3 h for two | J |
| S2 | `[ ]` | **A pair where the faster node flips by phase**, for example an Apple Silicon Mac against an NVIDIA GPU | Our pair only changes how uneven it looks; the 3050 wins both phases. A flip, where the better node depends on the request, would be the most memorable result. | a machine we do not have; then about 4 h (calibration and one campaign) | J |
| S3 | `[ ]` | **Capability sensitivity:** rerun `static_weighted`, `wjsq` and `threshold` with the old decode-only capability | Reviewers will ask whether H1 depends on how capability is defined. Reporting it turns the fix into a finding. | about 1 h machine on the anchor trace (three of five policies), a flag in `Capability` | A code, J runs |
| S4 | `[ ]` | **Wired LAN** for at least one pair | Bounds how much of the tail is Wi-Fi. Transport overhead is measured, but a reviewer will still ask. | one ethernet cable, 1.6 h to rerun P1 | J |
| S5 | `[ ]` | **One 8B point** on the full 24-cell grid | Answers "does this hold beyond a 1B model". | 2 to 4 h calibration per class (8B is slow), one campaign | D |
| S6 | `[ ]` | **Pool size above two:** simulator runner change, and one three-host run with the 4070 | JSQ and WJSQ only have a real choice to make with more than two nodes, and the spec promises N up to 12. | about a day for the runner; the 4070 box for the hardware run | A, J |
| S7 | `[ ]` | **Threshold baseline on the H2 curve** (`THRESHOLD_BASELINE`) | H2 predicts WJSQ collapses to thresholding at high R; the curve needs that baseline drawn to show it. | half a day; its test already exists and is skipped | D |

### Machine time

| Block | Hours |
|---|---:|
| P1 | done |
| P2 | done (about 11 h) |
| P3 | 8 to 12 |
| P5 | 3 |
| P6 reruns | 4 |
| S1 | 3 |
| **Remaining** | **18 to 22** |

---

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

---

## 6. Update log

- **2026-09-15** Second node brought up on the RTX 3050 laptop from the X9. 3050 calibrated.
  Capability changed from decode tok/s to service rate in both vehicles. P1 started.
- **2026-09-15, overnight** P1 done, all 45 valid; H1 interaction grows with load. Generation-shape campaign done, all 30 valid. Engines restarted between runs because llama-server's default host prompt cache exhausted memory; no cache hit found in the logs. Fixed the `node-utilization` and `phase-advantage` figures; suite 733 passed, 1 skipped, 100% coverage, no test changed.
- **2026-09-15, morning** Balanced (30 of 30) and summarisation (27 of 30) done, so P2 is done. The best policy moves from WJSQ to Threshold as the prompt share grows; Threshold collapses on generation at 3.2 req/s. Paper figures rendered for all four shapes. Pool shut down; the 3050 laptop returned to its owner and its worker and engine logs copied to `runs/`. Results written up in `docs/writing-brief.md`.
