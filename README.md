# Scheduling LLM Inference Under Uncalibrated Heterogeneity

A six-week **measurement study** of how to route LLM inference requests across a pool of
mismatched consumer machines. The scheduler in this repository is an *instrument*, not a
product: it exists to produce measurements, and it is built to the minimum fidelity that
supports the hypotheses below.

---

## The problem

Suppose you have four machines you already own — a desktop with a discrete GPU, a laptop
with a weaker one, an older GPU box, and a CPU-only machine — and you want them to serve
one LLM together. A request arrives. Where should it go?

Two things make this harder than ordinary load balancing:

**The machines differ enormously.** In a datacenter, hardware generations differ by
roughly 2–5×. Consumer machines differ by **10–100×**. A request that takes 2 seconds on
the fast node may take 3 minutes on the slow one — or exceed any reasonable timeout
entirely, which is a *categorical* failure rather than a latency tail.

**You do not reliably know how fast each machine is.** You can measure it, but the
measurement decays: LLM serving throughput is non-stationary under sustained load, drifting
with thermal state, memory pressure, and batch composition. So the scheduler is always
routing on an estimate that is somewhat out of date.

The obvious fix is calibration — measure each node's tokens/second and weight your routing
by it. This study asks whether that is actually worth doing:

> **Research question.** In a pool of consumer machines whose per-node throughput is
> heterogeneous, non-stationary, and known only through stale estimates, does explicit
> hardware calibration improve scheduling **beyond what live queue depth already reveals**
> — and over what range of heterogeneity does that advantage hold?

The reason to doubt it: **queue depth is already a partial proxy for speed.** Slow nodes
accumulate queue, so a scheduler that simply joins the shortest queue is *implicitly*
hardware-aware. Calibration may be measuring something the queue already tells you for free.

## What is being tested

Three hypotheses, each falsifiable, each with a negative result that is worth reporting.

| | Claim | Why it matters |
|---|---|---|
| **H1** | Calibration is largely **redundant** given queue-awareness, and more so as load rises. Formally, the interaction term is negative: `(WJSQ − JSQ) < (StaticWeighted − RoundRobin)`. | If true, the honest headline contradicts the intuition motivating hardware-aware schedulers. If false, calibration carries independent signal and the mechanism needs identifying. |
| **H2** | The advantage of hardware-aware routing is **non-monotonic** in the heterogeneity ratio *R*: it rises, peaks, then declines toward zero. | As *R* → ∞ the best policy converges to *thresholding* — never dispatch below a cutoff — which is behaviourally round-robin over the strong subset, achievable by a one-line static rule with no calibration machinery. So hardware-awareness has a **sweet spot**, and is matched by something trivial outside it. |
| **H3** | Routing quality degrades as the age of a node's estimate approaches the **autocorrelation time τ** of that node's actual throughput. | This turns the non-stationarity of LLM throughput from a threat to the method into the independent variable — and it gives an empirical basis for choosing a heartbeat frequency, which is otherwise arbitrary. |

The policies form a **2×2 factorial, not a ladder** — a ladder confounds hardware-knowledge
with queue-knowledge and cannot decompose the gain:

| | Queue-blind | Queue-aware |
|---|---|---|
| **Hardware-blind** | `RoundRobin` | `JSQ` (join-shortest-queue) |
| **Hardware-aware** | `StaticWeighted` (calibration weights, ignores live queue) | `WJSQ` (capability-weighted queue depth) |

Plus `Threshold(T)` — round-robin over nodes above a calibrated cutoff — as the degenerate
baseline H2 predicts `WJSQ` converges to at high *R*.

## How it is measured

![Deployment view](assets/fig03_deployment.png)

Four machines on a LAN, **all running the same inference runtime** (llama.cpp + GGUF).
Holding engine and quantization constant is deliberate: it makes the heterogeneity ratio
*R* a property of hardware and configuration alone. A mixed-engine pool would confound *R*
with an engine effect that none of the three hypotheses can decompose. Per-node capability
is then set by configuration — GPU offload (`-ngl`), threads, slot count — which also makes
*R* **tunable on real hardware** rather than reachable only in simulation.

Two vehicles, and the split between them is the core methodological commitment:

- **Hardware** measures what the simulator must not assume: cost-model parameters, the
  autocorrelation time τ and variance envelope, validation anchors, and the range of *R*
  that real machines can actually span.
- **A discrete-event simulator** provides breadth — node counts to 12, *R* to 100×,
  controlled estimate staleness — none of which four machines can reach.

The simulator **executes the same policy code as the live scheduler** (not a
reimplementation), is parameterized from measured hardware data, and is validated against
live runs on identical replayed traces before any simulated result is believed. Every
simulated figure is labelled as such.

Three disciplines run through the whole design, and are worth stating because they are the
sort of thing that quietly invalidates measurement studies:

1. **No cross-host clock subtraction.** Host clocks are not synchronised. Every duration in
   the analysis comes from a single machine's monotonic clock; whatever is left over is
   reported as one honest *residual*, not decomposed into invented stages.
2. **Open-loop load generation.** The replay client never waits for a response before
   firing the next request, and asserts its own send-lag per request. A run whose timing
   drifted is marked invalid rather than analysed.
3. **Byte-identical trace regeneration.** A trace is reproducible from `(config, seed)` and
   identified by its SHA-256, so the same workload can be replayed across every policy and
   across the hardware/simulator boundary.

### The pinned engine

*R* is a hardware property only if the engine underneath it does not move, so the engine is
pinned as part of the contract rather than installed per machine and hoped to match:

| | |
|---|---|
| Source | [`ggml-org/llama.cpp`](https://github.com/ggml-org/llama.cpp) tag `b10569`, commit `5a32f7b66ef6cfb3e60deea26e3454cc6ad3438c`, **plus one patch** — [`patches/`](patches) |
| Model | `Meta-Llama-3-8B-Instruct` GGUF — the primary condition; three more are staged, see [*The model set*](#the-model-set) |
| Quantization | `Q4_K_M`, held constant across every model and every node |
| Backends | CUDA 13.2 and Vulkan, **both built from that one commit** |
| Install root | `~/opt/llama.cpp/` — outside the repo; the pin travels in the manifest, not in git |

Two backends are built because the pool is not backend-homogeneous by luck of hardware: an
NVIDIA node runs CUDA, and Vulkan is what a non-NVIDIA node — or one whose distribution has
no CUDA toolkit — can run *without changing the engine*. Same source commit, same model,
same quantization; only the compute backend differs.

**A backend is not a free variable inside a comparison.** Every node in a given policy
comparison runs the same one. Where a machine can run both, the CUDA/Vulkan throughput gap
is measured during the Week-2 calibration campaign as two `node_class` entries — the same
move F-9b makes for vLLM, and for the same reason: the cost of the choice becomes a number
in the writeup instead of an assumption underneath it.

The contract already carries the pin. `manifest.nodes[]` records `engine_version`, `model`,
`quant`, `gpu`, `driver`, and the F-9a knobs under `engine_config`. The backend and the
binary's SHA-256 ride inside `engine_version` (`b10569+p1+cuda13.2`, `b10569+p1+vulkan`),
which the schema holds as a free string — a first-class `backend` field would be a
**Week-1 contract change** and has to be raised before the freeze, not after.

#### The `+p1` — why the engine is patched rather than upgraded

`llama-server` returned HTTP 500 on about 1 request in 100 —
*"The model produced output that does not match the expected Content-only format"* — and
lost the completion. The cause is in
`server_task_result_cmpl_final::update()`: it populates `oaicompat_msg` unconditionally,
which runs the chat PEG parser, which throws on output it cannot parse. But `/completion`
renders through `to_json_non_oaicompat()`, which reads `oaicompat_msg` **zero times**. The
parse is dead work on that path, and it is dead work that can fail. It fires because
`n_predict` + `ignore_eos` stops generation wherever the budget runs out, sometimes
mid-UTF-8-character, and the parser rejects the lone continuation byte.

**Upgrading does not fix it**, which is why the pin moved sideways rather than forward.
The lenient-parsing fix for the same failure ([#20191](https://github.com/ggml-org/llama.cpp/pull/20191),
merged March 2026, for [#20193](https://github.com/ggml-org/llama.cpp/issues/20193)) is
already in b10569 — our commit is five months later — and it only rescues *partial*
parses, so the final result of a non-streaming `/completion` still throws. The call is
still unconditional on master.

So the fix is a two-line guard on `res_type`, kept as a patch in
[`patches/`](patches) with its full rationale, applied to **both** backend builds so the
pool stays homogeneous under F-9. `llama-server --version` still reports
`build 10569, commit 5a32f7b` — it has no idea the tree was modified — which is precisely
why `engine_version` carries `+p1` and why the manifest records the binary hash. A pin
that a running node cannot prove is not a pin.

```bash
git -C ~/opt/llama.cpp/src apply \
  ~/Documents/capstone/patches/llamacpp-b10569-skip-chat-parse-on-completion.patch
cmake --build ~/opt/llama.cpp/b10569-cuda   --target llama-server -j6
cmake --build ~/opt/llama.cpp/b10569-vulkan --target llama-server -j"$(nproc)"
```

```bash
# Prerequisites, once per node (Fedora 43)
# All four matter, and each one only announces itself when the previous is satisfied:
# without vulkan-loader-devel cmake says "Could NOT find Vulkan (missing: Vulkan_LIBRARY)"
# on a box with a working driver, and without spirv-headers-devel it then fails on
# find_package(SPIRV-Headers). The "missing components: glslangValidator" line FindVulkan
# prints along the way is noise — only glslc is REQUIRED.
sudo dnf install -y vulkan-headers vulkan-loader-devel glslc spirv-headers-devel
CUDA_REPO=https://developer.download.nvidia.com/compute/cuda/repos/fedora43/x86_64
sudo dnf config-manager addrepo --from-repofile=$CUDA_REPO/cuda-fedora43.repo
sudo dnf install -y cuda-toolkit-13-2                                     # CUDA backend

# The rpm does not put nvcc on PATH, and cmake will not find it on its own.
export PATH=/usr/local/cuda-13.2/bin:$PATH

# One source tree, pinned; two build trees
git clone --branch b10569 --depth 1 https://github.com/ggml-org/llama.cpp.git \
  ~/opt/llama.cpp/src

cmake -S ~/opt/llama.cpp/src -B ~/opt/llama.cpp/b10569-cuda \
  -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=75 \
  -DLLAMA_BUILD_NUMBER=10569
cmake --build ~/opt/llama.cpp/b10569-cuda -j6      # not $(nproc): see the note below

cmake -S ~/opt/llama.cpp/src -B ~/opt/llama.cpp/b10569-vulkan \
  -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DLLAMA_BUILD_NUMBER=10569
cmake --build ~/opt/llama.cpp/b10569-vulkan -j"$(nproc)"

# `llama-server` is a ~12 KB wrapper — the engine is in the shared libraries beside it, so
# hash those. `--version` is what a running node can prove about itself, and is the value
# manifest.nodes[].engine_version is asserting.
~/opt/llama.cpp/b10569-cuda/bin/llama-server --version
#   version: 0.2.0-dev (build 10569, commit 5a32f7b)
sha256sum ~/opt/llama.cpp/b10569-*/bin/libllama.so ~/opt/llama.cpp/b10569-*/bin/libggml-*.so

# The weights (~4.9 GB), same file on every node
curl -L --output-dir ~/models/gguf --create-dirs -O \
  https://huggingface.co/bartowski/Meta-Llama-3-8B-Instruct-GGUF/resolve/main/Meta-Llama-3-8B-Instruct-Q4_K_M.gguf

# Hashes live in one place, ~/models/gguf/SHA256SUMS — see The model set below.
```

Two Fedora-specific traps, both hit on the first build. The CUDA rpm installs `nvcc` under
`/usr/local/cuda-13.2/bin` and does not add it to `PATH`, so cmake reports no CUDA compiler
on a box that plainly has one. And CUDA 13.2 accepts GCC up to 15, which is exactly what
Fedora 43 ships — so no compat toolchain is needed here, but check `host_config.h` before
assuming that on a newer release.

`-DLLAMA_BUILD_NUMBER` is not cosmetic. llama.cpp derives its build number by counting
commits, so a `--depth 1` clone stamps the binary `build 1` and the engine can no longer
say which pin it is. Passing the number explicitly restores the one field that lets a
running server prove it is b10569: `--version` then reports the build *and* the commit,
which is exactly what `engine_version` claims about it.

Use `-j6` rather than `-j$(nproc)` for the CUDA build on a 16 GB machine: `nvcc`
instantiates ggml's kernel templates at several GB per translation unit, and a parallel
build that gets OOM-killed halfway leaves a tree that looks configured and is not.

`CMAKE_CUDA_ARCHITECTURES` is per node — `75` is Turing (GTX 16-series, RTX 20-series), `86`
is Ampere, `89` is Ada. Setting it wrong costs a long JIT stall on first token, which then
contaminates the very tail latencies MPR-1 is measuring.

> **F-18 on the CUDA build: confirmed, `f18_status: "full"`.** The pinned build's
> `/completion` response carries a `timings` block with both halves of the split —
> `prompt_ms` / `prompt_n` for prefill and `predicted_ms` / `predicted_n` for decode — so
> the prefill/decode split comes from one code path for the whole pool, as F-9 intended.
> `/slots` returns exactly `--parallel` entries with `is_processing`, which is the worker's
> `LiveState.kv_frac`: llama.cpp exposes slot occupancy, not paged-KV occupancy.
>
> **Still to check on the Vulkan build.** If a backend does not emit it, log `service_ns`
> only and set `f18_status: "partial"` rather than faking the split. This was the one thing
> about the engine that could still have forced a contract change, which is why it was
> worth doing on day one rather than in Week 3.

### The model set

One model is an anecdote. A reviewer will ask whether a scheduling result is a property of
scheduling or a property of Llama-3-8B, and "we only ran one model" is not an answer. Seven
GGUFs are staged, varying along three axes that can each be named in a sentence.

| Model | `Q4_K_M` bytes | Fits 4 GB VRAM | KV/token | What it is for |
|---|---:|---|---:|---|
| `Llama-3.2-1B-Instruct` | 807,694,464 | fully, `-ngl 99` | 64 KiB | Fast iteration; the harness smoke path |
| `LFM2-2.6B` | 1,563,668,704 | fully, `-ngl 99` | **16 KiB** | **KV-light hybrid** — GPU-resident and cheap per token |
| `Llama-3.2-3B-Instruct` | 2,019,377,696 | fully, `-ngl 99` | 112 KiB | Scale rung; a fully GPU-resident node class |
| `granite-4.0-h-tiny` | 4,230,976,352 | partial | **8 KiB** | **Mamba-2 hybrid + MoE** — the extreme of the KV axis |
| `Mistral-7B-Instruct-v0.3` | 4,372,812,000 | partial | 128 KiB | Architecture control at the 8B's size class |
| `gemma-4-E4B-it` | 4,977,171,584 | partial | 168 KiB\* | **Bounded KV growth** — 512-token sliding window |
| `Meta-Llama-3-8B-Instruct` | 4,920,734,272 | partial | 128 KiB | The primary condition |

**Axis 1 — scale.** 1B → 3B → 8B, one family, one quantization, separating *bigger model*
from *different model*.

**Axis 2 — architecture at a fixed size class.** Mistral-7B against Llama-3-8B separates
*architecture* from *size*.

**Axis 3 — KV-cache footprint per token**, which is new and is the one that matters most
here. Every slot of `--parallel` holds a KV cache, so KV pressure is what bends the
service-time-versus-concurrency curve — it *is* the knee F-4 is about and the reason the
admissible set is drawn "at every calibrated concurrency". The old four-model set spanned
64–128 KiB, a factor of two, and Mistral and Llama-3-8B are **identical** at 128 KiB: the
architecture control varied the tokenizer and the weights but not the thing the study
measures. The set now spans **8 KiB to 168 KiB, a factor of 21**, and it does so through
three different mechanisms rather than by picking bigger and smaller models:

| | Layers | With attention | Experts | Vocab | Mechanism |
|---|---:|---:|---|---:|---|
| `granite-4.0-h-tiny` | 40 | **4** | 64, **6 used** | 100,352 | Mamba-2 state on 36 of 40 layers; sparse activation |
| `LFM2-2.6B` | 30 | **8** | dense | 65,536 | short convolutions on 22 of 30 layers |
| `gemma-4-E4B-it` | 42 | 42 | dense | 262,144 | 512-token sliding window interleaved with global |

\*Gemma 4's 168 KiB is the per-token figure; with a 512-token sliding window on most layers
its KV per *sequence* saturates rather than growing linearly, so its KV-versus-context curve
is flat where every other model's is a straight line. That is the point of including it.

Granite is the extreme case and the most interesting node a scheduler could be handed: at
4 slots × 2048 tokens it holds **64 MiB** of KV against Llama-3-8B's **1 GiB**, and it
advertises a 1,048,576-token context. It is also MoE, so its decode compute is 1B-scale
while its resident memory is 7B-scale. Weight footprint, compute per token and KV footprint
are three separate things, and this set now varies them independently.

> **The model is held constant inside a pool, exactly like the engine and the
> quantization.** F-9 is not only about llama.cpp. A pool running two models has an *R*
> confounded with a model effect, and neither the H1 2×2 decomposition nor the *R*-sweep can
> pull those apart afterwards. Variety here is a **replication axis across run sets** — the
> same hypotheses re-run end to end at a second model — and `manifest.nodes[].model` with
> `.quant` is what makes that auditable instead of assumed.

The payoff is not only rhetorical. The 1B, 3B and LFM2 builds fit entirely in 4 GB of VRAM,
so on the same card they reach `-ngl 99` node classes the 8B cannot — which *widens the
synthesizable R range*, and §7 asks for that range to be reported as a range rather than a
single figure (MPR-2).

**All three additions load and serve on the pinned, patched engine** — checked rather than
assumed, since `strings libllama.so` reporting an architecture is not the same as loading a
file. Each was started under `llama-server`, health-polled and asked for a completion, and
each returned HTTP 200 with the F-18 `timings` block intact. The pinned commit is dated
2026-08-21, which is why an April-2026 architecture like `gemma4` loads with **no re-pin and
no re-patch**. Two things that do *not* load, so nobody wastes an afternoon: `gemma4moe`
(the 26B-A4B variant) is absent from this build, and of the GLM family only `glm4moe` is
present, which starts at 106B and does not fit.

```bash
cd ~/models/gguf
for m in \
  bartowski/Llama-3.2-1B-Instruct-GGUF/Llama-3.2-1B-Instruct-Q4_K_M.gguf \
  bartowski/Llama-3.2-3B-Instruct-GGUF/Llama-3.2-3B-Instruct-Q4_K_M.gguf \
  bartowski/Mistral-7B-Instruct-v0.3-GGUF/Mistral-7B-Instruct-v0.3-Q4_K_M.gguf \
  unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf \
  ibm-granite/granite-4.0-h-tiny-GGUF/granite-4.0-h-tiny-Q4_K_M.gguf \
  LiquidAI/LFM2-2.6B-GGUF/LFM2-2.6B-Q4_K_M.gguf
do
  f=$(basename "$m")
  curl -sL --fail -C - -o "$f" "https://huggingface.co/$(dirname "$m")/resolve/main/$f"
done
sha256sum *.gguf > SHA256SUMS
```

Run those in parallel rather than in a loop if the link allows: on this connection one
stream managed 1.6 MB/s and three managed 3.0 MB/s together, which turned 110 minutes into
50.

`~/models/gguf/SHA256SUMS` is the check a node runs before it joins the pool — `sha256sum
-c SHA256SUMS` — because "the same model" has to mean the same bytes, not the same name on
a HuggingFace page that can be re-uploaded under you:

```
85a896a047553e842f25297ee5b031d64ff30147d9c4af17b1e4b394cd1fab87  gemma-4-E4B-it-Q4_K_M.gguf
5a38b08c441ae1adbafb1d2b8a7167e0d48734d83af68b268cefea1eec553dcd  granite-4.0-h-tiny-Q4_K_M.gguf
384bc877b6c37064982f96885bef69e4475919f5969218ed4e3b9399ae0340df  LFM2-2.6B-Q4_K_M.gguf
6f85a640a97cf2bf5b8e764087b1e83da0fdb51d7c9fab7d0fece9385611df83  Llama-3.2-1B-Instruct-Q4_K_M.gguf
6c1a2b41161032677be168d354123594c0e6e67d2b9227c84f296ad037c728ff  Llama-3.2-3B-Instruct-Q4_K_M.gguf
8ba9baf3a7345f705a11878397500fb25174034f0fd784e83aa4a96aaa47735f  Meta-Llama-3-8B-Instruct-Q4_K_M.gguf
1270d22c0fbb3d092fb725d4d96c457b7b687a5f5a715abe1e818da303e562b6  Mistral-7B-Instruct-v0.3-Q4_K_M.gguf
```

Each model's `vocab_size` and `reserved_ids_excluded` in `gen_trace.MODELS` are read out of
the **GGUF the node actually loads**, never off a model card. The materializer samples ids
below `vocab_size`, so a wrong number fills prompts with ids the model does not have — and
because a trace stores seeds rather than tokens, that would surface weeks later as strange
prefill figures in someone else's plot. `reserved_ids_excluded` is `true` only where a
*ceiling* can exclude the reserved block: Llama-3 and Granite keep their specials at the top
(128000 of 128256; 100256 of 100352), while Mistral, Gemma and LFM2 keep theirs at ids 0-7.
A ceiling cannot exclude a floor, and LFM2 has reserved ids at **both** ends, so `false` is
the honest value there. Nothing measured changes either way — output length is forced with
`ignore_eos` and no prompt is ever decoded back to text.

### The calibration campaign

Week 2 turns the pinned engine into numbers. One campaign per node class produces all
three of the week's deliverables, and it is one command:

```bash
uv run calibrate --config configs/calibration_1b_dense.json --out runs/calibration/llama32-1b
uv run r-range   runs/calibration/llama3-8b --out runs/calibration/llama3-8b/r_range.json
```

The campaign has two phases and one by-product:

- **A grid pass** over every (prompt bucket × output bucket × concurrency) cell, warmed and
  then sampled. This is the C-3 lookup table. Warmup is discarded *per cell*, not once per
  campaign: the first request into a new `-ngl` pays kernel JIT and the first at a new
  concurrency pays slot allocation, and either folded into a cell mean becomes a one-off
  cost the scheduler then applies to every request forever.
- **A sustained segment** at one operating point, held under constant load for minutes.
  This is what τ and the variance envelope are measured on.
- **A snapshot series**, re-fitting the sustained cell on a rolling window every 60 s. This
  is the least obvious requirement in C-3 and the most expensive to get wrong: if I hand
  Aditya a single fitted model, his staleness injection (F-8) has to *synthesize* age by
  perturbing parameters, and H3 stops being a result about real drift and becomes a study
  of his perturbation model.

Failures are counted, never fitted. A timeout is a censored observation, and a cell mean
that averaged in the 60 s ceiling would report my own `--timeout` setting as the node's
speed. The counts feed the Week-3 admissible-set work (F-13) and the cliff
characterization (F-15).

**C-3 `form` is `lookup_table`**, committed as a module constant rather than a runtime
option — F-7 allowed either that or a ≤6-parameter regression, and supporting both doubles
Aditya's interpolation logic for no research gain. The deciding reason is that service
time against concurrency is flat while slots are free and then bends sharply once
`--parallel` saturates, and that knee — which is what F-4 is about — is exactly what a
six-parameter form cannot hold.

### MPR-1: what the hardware actually said

Measuring τ on an LLM serving node turns out to be bounded below by two things that are
easy to miss, and finding them is most of what Week 2 produced:

1. **You only learn a node's rate when a request finishes.** So the finest timescale the
   throughput series can carry is one request. Worse, spreading each request's tokens
   across its own decode interval — which is the honest way to build the series, since a
   request does not produce its tokens at the instant it completes — *induces* correlation
   out to one request duration. A τ of that order is indistinguishable from the binning.
2. **llama.cpp decodes its `--parallel` slots as one batch**, so requests do not finish
   independently, they finish in bursts. The completion process is near-periodic, and the
   ACF of a near-periodic signal has peaks and troughs that an exponential fit will
   happily read as decay.

The second one was not a theoretical worry. On a 660 s segment at 0.78 completions/s with
four slots — about two bursts per 10 s window — τ came out *censored* at 6, 8 and 12 s
windows and ≈16 s at 10 and 15 s windows, **from the same samples**: the windows that
happened to be near-integer multiples of the ~5 s burst period averaged it out, and the
others aliased against it. A number that moves with the binning is a number about the
binning, and it would have been very easy to report the 16 s.

Both floors are now enforced by the instrument rather than left to judgement.
`resolution_floor_s` is the larger of the median request duration and the window;
`cadence_limited` is true below five slot turnovers per window; and `tau_resolved` is false
unless τ clears both. A run that cannot support a τ says so and keeps its samples instead
of throwing an exception and losing ten minutes of GPU time.

With those floors enforced, the answer is not one number. It is three readings that only
mean anything together, because the limit is the **instrument's** and it lands in a
different place on every node.

**The floor, stated once.** `bursts_per_window` is `samples_per_window / batch_size`, and at
saturation `samples_per_window = (batch_size / service) × window`, so the batch size
cancels:

> `bursts_per_window == window_s / service_s` — the five-burst floor means
> **`window ≥ 5 × service`**, and **τ is only visible if τ > 5 × service**. Concurrency does
> not enter it.

A node that takes seconds per request cannot see a correlation time of seconds. That is not
a statement about this study's patience; it is arithmetic, and it is why the Week-2 cells —
which put the window *above* τ on every node — could not have worked however long they ran.

| node class | sustained cell | service | window | τ | *r²* | reading |
|---|---|---:|---:|---:|---:|---|
| `cpu_ngl0_p4_q4km_llama3_8b` | p64/o24 c4 | 9.6 s | 48 s | **69.5 s** | **0.989** | real drift, cleanly fitted |
| `gtx1650ti_ngl20_p4_q4km_llama3_8b` | p64/o24 c4 | 6.0 s | 32 s | ≤ 32 s | 0.0 | no structure |
| `gtx1650ti_ngl99_p4_q4km_llama32_1b` | p64/o32 c4 | 0.81 s | 3.5 s | ≤ 3.5 s | 0.0 | no structure* |

**The CPU node is MPR-1.** It is the only class in the pool that drifts, and it drifts
cleanly:

| | `cpu_ngl0_p4_q4km_llama3_8b`, `--parallel 4`, saturated, 1500 s |
|---|---|
| τ | **69.5 s** — integrated **89.0 s**, 1/e crossing **72.1 s**, *r²* = **0.989** |
| Variance envelope | p05–p95 = **9.65 – 10.41 tok/s**, band **1.08×**, CV **0.027** |
| Standard-error inflation | **1.36×** — only 16.7 independent windows in 31 |
| Median decode throughput | 3.74 tok/s |
| σ (lognormal multiplier, F-22) | 0.040 |

Three estimators that do not share a failure mode — the exponential fit, Sokal's integrated
time, and the 1/e crossing — land within 30% of each other on a 0.99 fit. So the MPR-1
sentence finally has a number in it:

> **A single calibrated tok/s figure on the CPU node understates its own standard error by
> 1.36×.** Ten minutes of samples buy you sixteen independent observations, not thirty-one.

And the two GPU classes say the opposite, at every timescale their own service time lets
them look: *r²* = 0, τ pinned to the window, no decay. The pool is not uniformly
non-stationary — **the drift lives on the CPU node**, which is exactly the class the pool's
heterogeneity is built out of.

Three caveats belong next to those numbers rather than in a footnote.

**The CPU τ is fitted, not "resolved".** `tau_resolved` is false, because the bar is
τ ≥ 2 × `resolution_floor_s` and that floor is `max(median request, window)` = 48 s, so it
wants 96 s and got 69.5. That bar is deliberately conservative: the *physical* confound is
one request duration, 9.6 s, and τ is **7.3×** that. The bar compares against the window,
which is five times stricter than the effect it guards against. Both numbers are reported
and neither should be quoted alone.

**\*The 1B point is cadence-limited** (4.33 bursts per window against a floor of 5). Real
service came out at 0.81 s where I had predicted 0.63 s from the cost table, so the window
should have been 4.0 s and I ran 3.5. The bound holds; the exact figure should not be
quoted.

**H3 now has somewhere to live.** After Week 2 I wrote that if τ sits below ~10 s then
"staleness approaching τ" is a regime any sane heartbeat interval is already inside, and
H3's staleness axis would have to be re-grounded or declared simulator-only. That worry is
answered: on the CPU node τ is 70 s, comfortably above any heartbeat interval, so the
staleness sweep has a real target on real hardware. It is the GPU classes where the axis is
degenerate.

### The synthesizable *R* range — and the machine I do not have

F-9a's promise is that *R* is tunable on real hardware, because with the engine, the
quantization **and the model** all held constant, per-node capability is set by `-ngl`,
`--threads` and `--parallel` alone. Week 2 is where that promise gets a number. Two node
classes of `Meta-Llama-3-8B-Instruct`, both `--parallel 4`, sustained at concurrency 4:

| Node class | `-ngl` | Decode tok/s |
|---|---:|---:|
| `gtx1650ti_ngl20_p4_q4km_llama3_8b` | 20 | 7.49 |
| `cpu_ngl0_p4_q4km_llama3_8b` | 0 | 3.74 |

```
R in [1.00, 1.00] deployable across 1 host(s) from 2 node class(es);
configuration alone reaches 2.00 but F-9a forbids co-locating the extremes on one host
```

Two numbers, and conflating them would overstate the result badly. **Configuration alone
reaches 2.00×.** **Deployable *R* is 1.00×** — because both classes were measured on the
same physical machine, and F-9a forbids running two logical nodes on one host, since they
would contend for PCIe, memory bandwidth and cache and reintroduce as contention the exact
confound per-node throttling exists to remove.

So the honest Week-2 statement is that **the physical pool cannot yet span any
heterogeneity at all**, and the fix is not code — it is a second machine. MPR-2 is the 2×2
decomposition *across the synthesized R range*, so it stays out of reach until the pool has
at least two hosts. That is worth knowing in Week 2 rather than Week 4.

Also worth stating: 2.00× is a narrow span even ignoring co-location, because a GTX 1650 Ti
holding 20 of 33 layers is not far ahead of the same box's CPU. Reaching the 10–100× the
research question is about will need genuinely different machines, not just a wider `-ngl`
sweep on this one.

The `Llama-3.2-1B-Instruct` class reaches 71.6 tok/s on the same card at `-ngl 99`, which
is roughly 19× the CPU 8B — but that is **not an *R***, and the tool refuses to compute it
as one:

```
ValueError: R would be computed across 2 models (['Llama-3.2-1B-Instruct',
'Meta-Llama-3-8B-Instruct']), which confounds the heterogeneity ratio with a model
effect (F-9). Compute one range per model and report them separately
```

Model variety is a replication axis *across run sets* — the same hypotheses re-run end to
end at a second model — not a way to widen *R* inside one pool. The guard is in the tool
rather than in a reviewer's memory because that is the kind of mistake that produces a
plausible number nobody questions.

### The live pool: what a node actually is

Week 1 gave me an engine *adapter* — something that can drive `llama-server` and describe
what came back. Week 3 needs a node: a process on the network that answers `Execute`,
serves the request, and returns the response to the client. That is
[`worker/serve.py`](dataplane/src/dataplane/worker/serve.py), and three of its decisions are
measurement decisions rather than plumbing.

**The wrapper owns admission, not the engine.** llama.cpp will happily accept more requests
than it has slots and queue them internally, and if I let it, that wait lands inside
`service_ns` where nothing can separate it from compute. So the wrapper holds a semaphore of
exactly `--parallel` permits. `queue_wait_ns` is time spent waiting for a permit and
`service_ns` is the engine's own span, which is what makes C-4's two duration columns mean
two different things — and it is why the simulator can model a node as a fixed-capacity
server without approximating anything: the slot count *is* `SimNode.batch_capacity`.

**`Execute` returns before the work is done.** It answers `queued=true` and hands the
request to a task. A worker that blocked until the completion came back would apply
backpressure to the scheduler, and backpressure at the scheduler silently converts an
open-loop experiment into a closed-loop one — the single failure mode the replay client's
send-lag guard exists to catch. The guard watches the client; this is the other end of the
same rule.

**The response goes to the client, not back through the scheduler (F-11).** The
`client_endpoint` rides on the request for exactly this reason.

One cost paid on purpose: `/slots` is read once per request, after the permit is acquired
and before the completion is posted. It costs a loopback round trip that lands in neither
`queue_wait_ns` nor `service_ns`, and it buys a `kv_occupancy_at_admission` that is a
reading rather than a value copied from a heartbeat up to a second old. A stale per-request
field is indistinguishable from a fresh one once it is in the log.

Bringing a node up is three processes, and they are deliberately three:

```bash
# 1. the engine
llama-server -m ~/models/gguf/Llama-3.2-1B-Instruct-Q4_K_M.gguf \
             --host 127.0.0.1 --port 18080 \
             -ngl 99 --threads 6 --parallel 4 -c 8192 --slots --no-webui

# 2. the node — one per physical host (F-9a)
uv run worker --node-id gtx1650ti --engine http://127.0.0.1:18080 \
              --bind 0.0.0.0:50061 --scheduler <scheduler-host>:50051 \
              --slots 4 --engine-version b10569+cuda13.2 --log-dir runs/worker

# 3. the scheduler. Aditya's LiveSchedulerApp goes here; until the seam findings in
#    issue #5 are closed I drive the pool with my own fixture, which round-robins
#    blindly and writes no decision record.
uv run python fixtures/fake_scheduler/serve.py --bind 0.0.0.0:50051 --worker <node>:50061
```

Nothing here starts anything else. An engine, a wrapper and a scheduler have three
lifetimes, and a campaign runner that owned all three would hide an engine restart inside a
Python traceback — `engine_restarts` is a field in the C-6 validity block precisely because
it is a thing to be *counted*, not something to be papered over by a supervisor.

### The admissible set, and the cliff outside it

F-13 says the primary study operates over a `(prompt, output)` range that **every** pool
node can serve inside a stated timeout ceiling, and that the restricted range is reported
alongside results. It is an intersection, not an average: one slow node shrinks the whole
study's range. `uv run admissible` computes it from the C-3 snapshots the calibration
campaign already wrote, so the boundary is measured rather than assumed:

```
$ uv run admissible runs/calibration/llama3-8b --out runs/admissible/llama3-8b.json
model: Meta-Llama-3-8B-Instruct
admissible set: prompt <= 128, output <= 64 at a 300000 ms ceiling
                                            (limited by cpu_ngl0_p4_q4km_llama3_8b)
trace buckets inside it: p128_o64
  cpu_ngl0_p4_q4km_llama3_8b          prompt<=128  output<=64
  gtx1650ti_ngl20_p4_q4km_llama3_8b   prompt<=512  output<=64
  NOTE: cpu_ngl0_p4_q4km_llama3_8b admits prompts to 128 on samples that reach only 64 —
        the bucket ceiling is claimed, not measured
```

Those two rows are F-13 doing the thing it exists for. The GPU class would serve prompts to
512; the CPU class would not, and **the CPU class is what the pool's range becomes** —
`limited by` names it so the shrunk range travels with the result instead of being applied
silently. The 1B pool, having one class, intersects to that class:

```
$ uv run admissible runs/calibration/llama32-1b --out runs/admissible/llama32-1b.json
model: Llama-3.2-1B-Instruct
admissible set: prompt <= 512, output <= 128 at a 60000 ms ceiling
trace buckets inside it: p128_o64, p256_o64, p512_o128
```

Three things in that output are deliberate.

**The boundary is drawn on p95, not the mean.** A bucket whose mean fits under the ceiling
but whose p95 does not will time out one request in twenty, and those timeouts land in
exactly the tail statistics the study is about.

**"At every calibrated concurrency", not at concurrency 1.** A bucket that fits when the
node is idle and blows the ceiling at `--parallel 4` is not admissible, because the
scheduler will absolutely put four requests on that node under load — that is what the load
band *is*.

**The last line is the honesty check, and it is the one I expect to be argued with.** A
bucket is named by its **ceiling** and sampled in its **interior**. The `(129, 512)` bucket
was admitted on samples that reach 256 tokens, so "prompt ≤ 512" is a claim about 512 that
nothing in the campaign tested. The envelope still reports the ceiling — that is what C-2's
header and the trace generator consume — but `evidence` in the JSON reports what was
actually measured, and the difference is printed rather than left for someone to notice in
Week 6.

The cliff (F-15) is computed from the campaign's **discarded** samples, because a fitted
cost table excludes failures by construction and therefore cannot say where the cliff is.
That is what those samples are for.

The tool refuses to intersect across two models for the same reason `r-range` refuses to
divide across them: F-9 holds the model constant across a pool, so an envelope spanning two
models describes a pool that cannot exist.

### Validation anchors, and the load band they also locate

F-23 fixes the form of the Week-4 answer to *does the simulator agree with the machine?* —
p50 and p95 end-to-end latency, within a stated tolerance, at **three or more operating
points**, on **identical replayed traces**. `uv run anchors` produces the hardware half, and
those two emphasised phrases are the whole design.

**Three points, not one.** A simulator tuned at a single load is not validated, it is
fitted. The interesting failure is a service-time model that is right when the node is idle
and wrong when it is saturated, and a single anchor at either end cannot see it.

**One trace across all of them.** The points differ only by `rate_scale`: the same trace
file, replayed with its arrival timeline compressed. Three separately seeded traces would
change the length draw and the burst structure along with the rate, and a disagreement
between vehicles could then be a workload difference rather than a simulator error. Sharing
the trace is also what makes `trace_sha256` identical across the set, which is the property
`load_anchors` refuses to run without.

```
$ uv run anchors configs/anchors_1b.json
     quiet  x0.80  lambda=0.72/s  200/200 ok  max send lag 26.1 ms  VALID
     light  x1.15  lambda=1.03/s  200/200 ok  max send lag 12.8 ms  VALID
       mid  x1.45  lambda=1.30/s  200/200 ok  max send lag  9.3 ms  VALID
     heavy  x2.20  lambda=1.98/s  200/200 ok  max send lag  5.7 ms  VALID

4/4 anchors valid, written under runs/anchors
```

Four points rather than three, because F-23's "at least 3" is a floor and a sweep that
loses one run to a send-lag violation should still have an anchor set. Each is the same 200
requests of `Llama-3.2-1B-Instruct` on one GTX 1650 Ti at `-ngl 99 --parallel 4`; only the
clock differs. The manifests are committed under [`runs/anchors`](runs/anchors) — they are
the whole record, since the trace regenerates from its seed.

The operating points are chosen against the pool's **measured** capacity, and the first
sweep exists to measure it. My estimate from the C-3 table's concurrency-4 cells was 2.9
req/s; the pool actually retires about 1.65, because the table prices a cell at a controlled
concurrency and a real length mix does not hold concurrency still. That first sweep is kept
under [`runs/sweeps/capacity_probe_1b`](runs/sweeps/capacity_probe_1b) with its own note,
because it is what the anchor rates were chosen from and because it is where the
`engine_error`s finally got a cause.

#### About those `engine_error`s

Two requests in every two hundred come back as `engine_error`, and the reason is not the
hardware:

```
W common_chat_peg_parse: unparsed Content-only output: <0xB2>
W srv operator(): got exception: {"error":{"code":500,
    "message":"The model produced output that does not match the expected Content-only
               format","type":"server_error"}}
```

A lone UTF-8 continuation byte. Forced-length generation (`n_predict` + `ignore_eos`) ends
mid-character, and this build runs `/completion` output through the chat content parser.
The work was done; the engine refused to hand it over. It is a **response-serialization
failure, not a capacity limit**, and it is the same signature as the periodic
`engine_error`s the Week-2 CPU node produced and that I could not explain at the time.

It is not avoidable from my side. `--no-jinja`, `--reasoning-format none` and
`--reasoning off` each leave the rate unchanged at 2 in 200, and Llama-3's BPE *does*
carry byte tokens, so a `logit_bias` could suppress them — but that is not a way out
either: truncation on a **lead** byte fails the same parse, and any token can end mid-
character. So it is reported: the count is in every anchor's summary line, the status is
in the C-4 log, and the rate is stated wherever an anchor is used.

#### The load band

Policy differences vanish at both ends of the load axis and for two different reasons —
too light and nothing is queued, so every policy makes the same placement; too heavy and
everything is queued, so no placement helps. §5.5 makes finding the band between them a
prerequisite step and a reportable characterization in its own right, and it comes off the
same runs:

```
$ uv run load-band runs/anchors
load band: 1.03–1.30 req/s (reference p50 1367 ms, p99 4106 ms); one-node pool, so this is
the band's physical bound only — policy separation is not demonstrated by these runs
     quiet  lambda= 0.72/s  n=196  p50= 1366.6  p95= 3739.0  p99= 4105.9  drift=  -115.8 ms
     light  lambda= 1.03/s  n=193  p50= 1981.2  p95= 4534.4  p99= 5476.0  drift=  -128.0 ms
       mid  lambda= 1.30/s  n=190  p50= 2980.2  p95= 5974.2  p99= 7115.0  drift=  -312.0 ms
     heavy  lambda= 1.98/s  n=180  p50=14137.1  p95=23985.7  p99=25068.3  drift=+12829.6 ms
                                                          [retired only 1.58/s]
```

The band reads cleanly off those four rows. At 0.72 req/s the node is the reference — its
tail is the length spread and nothing else. At 1.03 the tail is a third worse than the
reference's with the *same* requests in it, which is queueing. At 1.30 it is still stable,
its latency still flat across the run. At 1.98 both saturation readings fire at once: the
fitted latency rise is +12.8 s against a p50 of 14.1 s, and the pool retired 1.58 req/s
against 1.98 offered. So the pool's ceiling is about 1.6 req/s and the band sits just under
it.

Worth noting what did **not** move. These are the numbers from the patched engine, and the
band lands in the same place as the pre-patch sweep did — 1.03–1.30 either way. The patch
recovered the ~1% of requests the engine had been dropping without changing the queueing
physics, which is what a fix to a serialization bug should look like. What changed is the
`ok` column: 198/200 on every point before, **200/200 on every point now**.

Three rules, stated rather than tuned:

**Onset is tail against tail.** A point is inside the band once its p99 is 20% worse than
the *reference point's p99*. The first version of this rule compared p99 to the reference
**p50**, which is wrong in a way that looked right: the trace mixes `p128_o64` with
`p512_o128`, so p99 sits several times above p50 from the length spread alone — 1603 ms
against 4987 ms on the lightest run, with no queue anywhere — and the rule fired at the
floor of every sweep. The fix is free from the anchor design: every point replays the same
trace, so the length composition is identical by construction and a tail-to-tail comparison
isolates the one thing that changed.

**Saturation is read two ways.** The trend test fits latency against arrival time and calls
a point saturated when the fitted rise across the window is at least the run's own p50. The
shortfall test compares achieved throughput to the offered rate — in an open-loop replay the
trace fixes the offered rate, so a pool retiring less than that grew a backlog by definition.
Both are here because the trend test missed a point the shortfall test caught: at 1.80 req/s
against a pool that retires 1.65, the backlog builds slowly enough that over 111 seconds the
fitted rise reached only 0.30 of the run's p50, while the pool was visibly retiring 1.63.

**What one node cannot tell you.** With a single-node pool there is no placement to get
wrong, so these runs bound the band from physics — queueing exists here, the queue clears
here — but cannot demonstrate the thing the band is *defined* by, which is that policies
differ inside it. `policy_separable` is `false` in the output until the pool has two hosts,
and it stays in the JSON so a figure drawn from this sweep cannot quietly claim more than the
runs support. It is the same second machine that MPR-2 is waiting on.

## What it produces even if things go wrong

The window has no slack, so the result ladder is defined in advance and strictly ordered:

| | | Depends on |
|---|---|---|
| **MPR-1** ✅ | A characterization of throughput non-stationarity in consumer LLM serving nodes — τ, the variance envelope, and the implication that any single calibrated tok/s figure is a moving average over a non-stationary process. | Nothing. Hardware only. |
| **MPR-2** | The H1 2×2 decomposition on real hardware, across the synthesized *R* range, plus the load-band characterization. | Week 3–4, **and a second machine**. |
| **MPR-3** | H2 and H3 — the non-monotonic advantage curve and its shift under staleness, in the validated simulator. | Weeks 5–6. |

MPR-1 stands alone as a measurement contribution and needs no scheduler comparison at all —
which is exactly why it is the one that has landed. It came out sharper than "here is a τ",
because the drift turned out to belong to a *particular kind of node*: **τ = 69.5 s on the
CPU class, and nothing measurable on either GPU class**, with an instrument limit
(`τ > 5 × service`) that explains why. That last part is the reusable half — it tells anyone
repeating this what their hardware has to be able to do before the question is even askable.

MPR-2 has half its inputs. The load-band characterization is done; the 2×2 decomposition
across *R* is not, and cannot be, while deployable *R* is 1.00×.

---

## Repository layout

```
contracts/       The interface between the two halves — six frozen artifacts,
                 plus the committed C-3 snapshot series the simulator reads.
dataplane/       Workers, calibration campaign, harness, results pipeline.  (Python)
controlplane/    Scheduler, the five policies, discrete-event simulator.
fixtures/        Fake scheduler and fake worker, so neither half blocks on the other.
docs/            Spec, decision records, UML figure set.
patches/         Changes to the pinned engine, with the reasoning that justifies them.
runs/            Measurement output. Only the run manifests and Week 3's two
                 determinations are versioned; the rest regenerates from a seed.
assets/          Images used by this README.
```

The whole system is two processes and a client, and the seam between them is narrow by
design:

![Component view](assets/fig02_component.png)

The scheduler is **control-plane only** — it chooses a node and steps out of the way.
Responses travel from worker straight back to the client, so the scheduler never sits in
the response data path.

### The contract

The two halves interact through exactly six artifacts and **nothing else crosses the
seam**. All six are validated in CI on every pull request:

| # | Artifact | Direction | Format |
|---|---|---|---|
| C-1 | [`scheduling.proto`](contracts/scheduling.proto) | bidirectional | protobuf3 / gRPC |
| C-2 | [Trace file](contracts/schemas/trace.schema.json) | harness → harness, simulator | JSONL + header |
| C-3 | [Cost model snapshot](contracts/schemas/cost_model.schema.json) | data plane → control plane | JSON |
| C-4 | Log records ([client](contracts/schemas/log_client.schema.json) · [scheduler](contracts/schemas/log_scheduler.schema.json) · [worker](contracts/schemas/log_worker.schema.json)) | both → pipeline | JSONL |
| C-5 | [Joined record](contracts/schemas/joined_record.schema.json) | pipeline → figures | Parquet |
| C-6 | [Run manifest](contracts/schemas/manifest.schema.json) | launcher → everything | JSON |

Only C-1 and C-3 are runtime couplings. The rest are file formats, so both halves can be
developed against fixtures without either waiting on the other.

**Why the seam sits where it does.** The binding constraint is that the simulator must run
*the same policy implementations* as the live scheduler. Split those across two people and
they drift — not maliciously, but through ordinary divergence in tie-breaking, or in
whether an in-flight request counts before or after admission. That drift invalidates
validation *silently*, because both systems still run and still produce plausible numbers.
So one person owns the policy code and both of its hosts, and everything else is arranged
around that.

## Getting started

Requires [`uv`](https://docs.astral.sh/uv/). Nothing else — no GPU needed to run the checks.

Bringing up an actual worker node is a separate step — see [*The pinned engine*](#the-pinned-engine)
for the llama.cpp tag, the two backend builds, and the GGUF.

```bash
git clone git@github.com:aiinfra-capstone/llm-sched-study.git
cd llm-sched-study

# Validate the six contract artifacts (schemas + examples + proto compile)
uv run contracts/check.py

# Data plane
cd dataplane && uv sync --all-groups && uv run pytest
```

With a node up (see [*The live pool*](#the-live-pool-what-a-node-actually-is)), the Week-2
and Week-3 determinations are five commands, each of which writes the artifact it prints:

```bash
uv run calibrate  --config configs/calibration_1b.json --out runs/calibration/llama32-1b
uv run r-range    runs/calibration/llama3-8b                     # F-9a — the R range
uv run admissible runs/calibration/llama32-1b --out runs/admissible/llama32-1b.json
uv run anchors    configs/anchors_1b.json                        # F-23 — 4 operating points
uv run load-band  runs/anchors --out runs/anchors/load_band.json  # §5.5
```

Before the pool spans more than one machine, `preflight` checks the network the run will
actually use — and in particular the direction nothing else exercises:

```bash
uv run preflight --serve 0.0.0.0:50071          # on the CLIENT host
uv run preflight --probe <client-host>:50071    # from EACH worker host
uv run preflight configs/preflight_lan.json --out runs/preflight.json
```

Under F-11 the worker returns responses **directly to the client**, so the client's port has
to be reachable *inbound* from every worker host. Nothing about bringing the scheduler up
tests that, and when it is blocked the failure is not loud: the dispatch succeeds, the
worker serves the request, the record says `timeout`, and a firewall is indistinguishable
from a saturated pool. Hence the two-sided `--serve` / `--probe` pair.

Two things it deliberately does **not** check. Bandwidth, because a five-node pool at the
measured load band runs at about **0.08 Mbit/s** — a `Dispatch` is 754 bytes on average, a
`Deliver` is 33, a heartbeat 47 — and even a 30 ms hop is roughly 2% of the fastest
end-to-end latency measured here. And clock synchronisation, because no duration in this
study is computed by subtracting stamps taken on different hosts, and heartbeat gaps are
found through `Heartbeat.seq` rather than through time. NTP is not needed; that is a
property the design paid for deliberately.

Rebuild the UML figures (needs `java` and `graphviz`):

```bash
docs/uml/render.sh
```

## Documentation

| | |
|---|---|
| [Requirements specification](docs/scheduling-requirements-spec.pdf) | Scope, hypotheses, F-1 – F-24, non-goals, threats to validity, the MPR ladder. **Start here** — it is the authority for everything below. |
| [Split & interface contract](docs/two-person-split-and-interface-contract.md) | Where the seam is, the six artifacts across it, and the failure modes to watch for. |
| [Week-1 freeze checklist](docs/week1-freeze-checklist.md) | What must be true before the contract freezes. |
| [UML figure set](docs/uml/FIGURES.md) | Twelve figures with draft captions and the requirements each discharges. |

## Project status

Six weeks, no slack. Feature freeze at end of Week 3; the interface contract freezes at end
of Week 1.

| Week | Focus |
|---|---|
| 1 | Worker wrapper, heartbeat, thin client, measurement harness. One query routed and measured end to end. |
| 2 | Calibration campaign; τ and the variance envelope; synthesizable *R* range. **MPR-1.** — ✅ *τ = 69.5 s on the CPU class (r² = 0.989, SE inflation 1.36×), no measurable drift on either GPU class, with the instrument limit that explains the difference. R = 2.00× configured, 1.00× deployable. F-9b still blocked on VRAM.* |
| 3 | Multi-node pool; all five policies behind one config value; load band identified. **Feature freeze.** — *node serving; admissible set determined on two node classes; 4 valid anchors at 200/200; load band 1.03–1.30 req/s; LAN preflight built. Still one host, so policy separation is not demonstrated and the pool is not yet multi-node.* |
| 4 | Discrete-event simulator sharing policy code; validated against hardware. |
| 5 | Sweeps: *R* × load × staleness × policy. Hypotheses tested. |
| 6 | Analysis, threats to validity, literature positioning, writeup. |

## Team

| | | |
|---|---|---|
| **Divyansh Shukla** (A) | [@divyanshuklai](https://github.com/divyanshuklai) | Data plane & measurement — workers, calibration, harness, pipeline, figures |
| **Aditya Gupta** (B) | [@adityaxgupta](https://github.com/adityaxgupta) | Control plane & simulation — scheduler, policies, staleness injection, simulator, validation |

Ownership marks primary responsibility, not exclusive access; both members can run the full
stack. The specification assigns three roles (A/B/C); B and C are merged here, for the
reason given under *Why the seam sits where it does*.
