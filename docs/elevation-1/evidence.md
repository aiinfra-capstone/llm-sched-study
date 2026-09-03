# Elevation 1: evidence

Every number here was measured on this hardware in September 2026 and is what a decision in
[`scope.md`](scope.md) rests on. Where a measurement contradicts something we previously
wrote down, the contradiction is stated rather than quietly corrected.

Unless noted otherwise: GTX 1650 Ti 4 GB, driver 580.173.02, llama.cpp b10569+p1 with CUDA
13.2, Llama-3.2-1B-Instruct Q4_K_M at `ngl 99 --threads 6 --parallel 4`, and the four
recollected anchors under `runs/anchors`.

---

## 1. The cost of everything that is not the engine

**Why it was measured.** The simulator carried a hardcoded `* 1.05` on every service time,
chosen by trying 0%, 5% and 8% against the F-23 gate. Removing it required knowing what it
had been standing in for.

**Method.** No new instrument. C-5 already derives `transport_residual_ms` per request as
end-to-end latency minus the single-host durations inside it, so this is a summary of a field
we were already recording, over the warmed-up successful rows of all four anchors.

| point | n | mean | sd | p50 |
|---|---|---|---|---|
| quiet | 196 | 5.37 ms | 2.38 | 5.09 |
| light | 193 | 5.54 ms | 1.96 | 5.06 |
| mid | 190 | 6.84 ms | 3.73 | 5.36 |
| heavy | 180 | 5.63 ms | 1.59 | 5.34 |
| **pooled** | **759** | **5.86 ms** | **2.66** | **5.16** |

**What it decides.** The quantity is flat in load, moving from 5.37 ms at quiet to 5.63 ms at
heavy while service time over the same range moves by a factor of ten. So it is additive in
milliseconds and a multiplicative correction was the wrong shape: 5% charges a 13-second
request 650 ms of transport where a 1-second request pays 50 ms, which is the opposite of
what the wire does. It is now carried per environment as `transport_overhead` on the C-6
manifest, and a manifest without that block gets zero rather than a guessed default.

---

## 2. Prefill is flat in concurrency, but only under arrivals that are not synchronised

**Why it was measured.** The correction to `reevaluateActive` rests on prompt evaluation not
stretching when a neighbour arrives. Our own README asserted that, so we checked it before
building on it.

**Under Poisson arrivals**, from the anchor worker logs, mean ms by prompt bucket and batch
size at admission:

| prompt bucket | batch 1 | batch 2 | batch 3 | batch 4 |
|---|---|---|---|---|
| 1-128 prefill | 179.4 | 267.2 | 280.4 | 189.0 |
| 129-256 prefill | 351.7 | 349.8 | 405.3 | 355.2 |
| 257-512 prefill | 701.7 | 750.5 | 756.1 | 706.9 |
| 1-128 decode | 822.0 | 993.8 | 1073.0 | 1587.7 |

Prefill is flat. Decode roughly doubles.

**Under the calibration grid**, which holds `c` requests in flight by firing them together,
from the C-3 snapshot for the same 128-token prompt and 64-token output:

| concurrency | service | prefill | decode |
|---|---|---|---|
| 1 | 615.7 | 174.2 | 433.9 |
| 2 | 923.5 | 341.9 | 566.8 |
| 3 | 1298.9 | 469.4 | 807.7 |
| 4 | 1987.4 | 726.6 | 1234.5 |

Prefill rises by a factor of 4.2.

**Both are correct and they answer different questions.** A Poisson process almost never
starts two prompts at the same instant, so a new request's prefill overlaps its neighbours'
decode rather than their prefill. Our calibration harness synchronises them on purpose, and
there the prefills serialise against each other because prefill is compute-bound.

**What it decides.** Two things. The `reevaluateActive` fix is justified: only the decode
remainder is scaled when batch composition changes. And, less comfortably,
`service_ms_mean` at concurrency above 1 inherits contention that a trace-driven run never
produces. The caveat is now written into the C-3 field description. It must be corrected
together with item 4 below and not before, because the two errors partly cancel.

---

## 3. The heterogeneity ratio differs by phase

**Why it was measured.** It is the empirical basis of the whole elevation.

**Method.** `prefill_ns` and `decode_ns` were recorded per observation at calibration time
and kept, so nothing needed re-running: the split was summarised onto all 59 committed
snapshots as `prefill_ms_mean` and `decode_ms_mean`. The only same-model pair we own is
Llama-3-8B Q4_K_M on CPU at `ngl 0` against the GTX 1650 Ti at `ngl 20`.

| prompt | output | c | R_service | R_prefill | R_decode |
|---|---|---|---|---|---|
| (1,128) | (1,64) | 1 | 1.75 | 1.46 | 1.83 |
| (1,128) | (1,64) | 4 | 1.64 | 1.18 | 1.92 |

Phase share of service time at concurrency 1:

| class | prefill | decode | residual |
|---|---|---|---|
| cpu_ngl0 8B | 16.9% | 82.9% | 0.2% |
| gtx1650ti_ngl20 8B | 20.3% | 79.4% | 0.3% |
| gtx1650ti_ngl99 1B | 28.3% | 70.5% | 1.2% |

**What it decides.** The effect is real and in the predicted direction, and the magnitude is
suppressed by two things we can fix. The faster node in that pair is a partial offload, not
a real GPU, and both 8B classes were calibrated at one grid cell so the prompt-to-output
ratio never varied. A CPU node running the 1B model gives a full-GPU-versus-full-CPU pair,
and three workload profiles give the ratio a 27x swing.

The residual column is the engine's own unattributed time. It is kept rather than
distributed across the two phases, because distributing it would invent an attribution the
backend never reported.

---

## 4. The simulator is under-dispersed, and the distribution family is not the problem

**Why it was measured.** The scrutiny pass claimed real serving latency is bimodal and that
modelling it as log-normal smooths away the tail that causes queue pileups.

**Shape**, from 800 anchor service observations, on the log of service time:

| batch | n | p95/p50 | log-skew | excess kurtosis |
|---|---|---|---|---|
| 1 | 183 | 2.74 | 0.61 | -0.35 |
| 2 | 127 | 2.73 | 0.30 | -0.62 |
| 3 | 84 | 2.06 | -0.18 | -0.66 |
| 4 | 406 | 2.17 | 0.37 | 0.32 |

A perfectly log-normal residual gives log-skew 0 and excess kurtosis 0. These are close, and
nothing here is bimodal. **The form is right.**

**Scale**, as within-cell log standard deviation, which is what a refit would write:

| prompt | output | c=1 | c=2 | c=3 | c=4 |
|---|---|---|---|---|---|
| (1,128) | (1,64) | 0.459 | 0.430 | 0.402 | 0.242 |
| (129,256) | (1,64) | 0.346 | 0.285 | 0.333 | 0.245 |
| (257,512) | (65,128) | 0.394 | 0.321 | 0.286 | 0.145 |
| **pooled** | | **0.403** | **0.368** | **0.363** | **0.228** |

C-3 carries a single global `sigma` of 0.12253 at `fit_r2` 0.0 for all of these, implying a
p95/p50 of 1.22 against an observed 2.06 to 2.74.

**What it decides.** Fit sigma per concurrency, additive to the C-3 `stochastic` block the
same way the phase split was. Do not change the distribution family. The simulator is two to
three times under-dispersed and worst at low concurrency, which is exactly where the
remaining F-23 error sits.

---

## 5. Token budget headroom

**Why it was measured.** The 512-token envelope was called out as avoiding the phenomena
that make LLM serving hard. We wanted the cost of widening it rather than an argument about
it.

**Memory is not the constraint.** With `--parallel 4`:

| context | per slot | VRAM used |
|---|---|---|
| 16384 | 4096 | 1477 MiB of 4096 |
| 32768 | 8192 | 1993 MiB of 4096 |

The 516 MiB delta matches the KV arithmetic exactly at 32 KiB per token for this model
(16 layers, 8 KV heads, head dim 64, two tensors, fp16), so the rest is predictable without
measuring. The original 512 cap was never a hardware limit: it is llama.cpp's default 4096
context divided across four slots, which makes 512 prompt plus 128 output the largest shape
that fits. One flag changes it.

**Time is the constraint**, mean ms at concurrency 1 unless stated:

| prompt | prefill | ms/token | service o=256 c=1 | service o=256 c=4 |
|---|---|---|---|---|
| 128 | 175 | 1.37 | 1.96 s | 4.33 s |
| 512 | 697 | 1.36 | 2.56 s | 6.59 s |
| 1024 | 1430 | 1.40 | 3.40 s | 9.83 s |
| 2048 | 3059 | 1.49 | 5.15 s | 17.15 s |
| 4096 | 6764 | 1.65 | 9.13 s | 33.00 s |

Prefill is close to linear out to 4096, with cost per token rising only 22%, so the quadratic
attention term is not what stops us.

**What it decides.** 2048 prompt and 256 output is affordable; 4096 is not, because a single
request at the corner averages 33 s against a 60 s timeout ceiling and the p95 tail would be
censored. Widening costs a calibration grid pass rising from 3.8 to 21 minutes, and drops
saturated node throughput from 2.55 requests per second at (128, 64) to 0.23 at (2048, 256),
which lengthens every run in the sweep proportionally.

We are not widening now, because the three workload profiles reach a 27x swing in the
prompt-to-output ratio entirely inside the existing envelope, and every bucket they use
lands in a cell the cost model has already measured. Section 7 has their construction.

One thing to fix if the envelope is ever widened: the 1B snapshot declares admissibility to
2048 prompt and 256 output while only 512 and 128 were ever sampled. That gap used to be
absorbed by a silent 100 ms fabrication in the simulator, which now throws instead.

---

## 6. Three of four operating points are steady state, and the instrument already says which

**Why it was measured.** Two hundred requests is short, and a percentile taken off a filling
queue is not a percentile.

From `runs/anchors/load_band.json`:

| point | offered | achieved | p50 | p99 | drift | saturated | short |
|---|---|---|---|---|---|---|---|
| quiet | 0.72 | 0.74 | 1296.0 | 4065.6 | -133.1 | no | no |
| light | 1.035 | 1.06 | 1820.1 | 5278.0 | +64.0 | no | no |
| mid | 1.305 | 1.34 | 2931.0 | 7034.7 | -471.6 | no | no |
| heavy | 1.98 | 1.60 | 13421.7 | 23594.5 | +11688.3 | **yes** | **yes** |

**What it decides.** Quiet, light and mid are steady state and their percentiles stand.
Heavy is transient queue filling, achieving 1.60 requests per second against 1.98 offered
with latency still climbing by 11.7 seconds across the window. Either lengthen that run or
state plainly that it is a transient observation and keep it out of steady-state claims.
Both are honest. Reporting its p99 without saying so is not.

---

## 7. The workload-shape profiles are load-matched by construction

**Why it matters.** If the three profiles differ in mean service time, then varying the
prompt-to-output ratio also varies offered load, and the phase result is confounded with the
thing it is supposed to be measured against.

**Method.** The buckets were chosen against the cost model rather than picked for
roundness. Each profile is three buckets that all land in a single C-3 cell, so the
simulator prices the profile exactly, and the hardware-side cost was matched using the
measured 1.36 ms per prompt token and 6.9 ms per output token.

| profile | buckets | mean rho | C-3 cell | cell service | prefill share | predicted hardware |
|---|---|---|---|---|---|---|
| summarisation | p512_o32, p480_o36, p448_o40 | 13.76 | (257,512) x (1,64) | 1160.3 ms | 60.0% | 903 ms |
| balanced | p192_o96, p176_o88, p208_o104 | 2.00 | (129,256) x (65,128) | 1248.1 ms | 27.3% | 922 ms |
| generation | p60_o120, p64_o128, p56_o112 | 0.50 | (1,128) x (65,128) | 1064.0 ms | 16.5% | 911 ms |

Mean predicted service time varies by 2.1% across the three while the ratio moves 27x and
the prefill share moves 3.6x.

**The comparison is paired.** All three configs carry the same `gen_seed`, and the generator
draws arrivals and lengths from separate streams, so the three traces have byte-identical
arrival offsets and byte-identical priority assignments. Verified over all 600 requests.
Only the lengths differ, which is the whole point.

**What it decides.** Offered load in requests per second means the same thing in all three
profiles, so any difference in the queue-versus-calibration gap is attributable to workload
shape. Every bucket also sits inside the existing 512 and 128 envelope and inside a cell the
cost model has already measured, so no recalibration and no envelope change is needed.

One caveat on identity: the generator stamps its git sha inside the hashed header, so a
trace's sha256 moves with every commit even when the request stream does not. The identity
that matters is the one each run manifest records, not one written down here.

---

## 8. What the fixes did to F-23

Nothing below was fitted to the tolerance.

| stage | quiet | light | mid | heavy |
|---|---|---|---|---|
| with the 5% multiplier | -17.2% | -12.0% | -2.8% | +16.3% |
| multiplier out, measured transport in | -23.8% | -21.3% | -18.3% | -7.1% |
| plus prefill held invariant | -18.3% | -19.4% | -16.5% | -7.8% |

Worst of all eight p50 and p95 comparisons: 23.8% before, 21.7% after.

The middle row is the honest picture the multiplier had been hiding. Removing it made the
error look worse at three of four points, because a scalar applied to every service time had
been rotating the error around the middle of the load range rather than reducing it. The
simulator is now uniformly low instead, and uniformly low is a shape with a cause: item 4.
