# Week-1 Freeze Checklist

The [requirements spec](scheduling-requirements-spec.pdf) is frozen. It carries the single-engine
decision as F-9 / F-9a / F-9b, and resolves `priority` as a passthrough label in §5.4.
**The interface contract (C-1 – C-6) freezes at end of Week 1.** After that, changes require the same re-scoping ritual as the spec:
state the change, state what it costs against the §6 timeline, both people agree, one PR.

This checklist is the gate. Nothing here is optional, and the last section is an open
decision that must be closed *before* the freeze, not after.

---

## Divyansh Shukla (A)

- [ ] `scheduling.proto` reviewed and agreed with Aditya (C-1)
- [x] **One** worker wrapper — llama.cpp + GGUF — running on a node (F-9, per
      F-9; the second runtime integration is withdrawn from Week 1)
      — `dataplane/src/dataplane/worker/serve.py`, `uv run worker`. It answers `Execute`,
      returns the response to the client directly (F-11), reports the completion to the
      scheduler, and beats every second. The wrapper — not the engine — holds the backlog,
      in a semaphore of exactly `--parallel` permits, so `queue_wait_ns` is measured
      outside `service_ns` instead of hiding inside it.
- [x] `engine_config` (`-ngl`, `--threads`, `--parallel`) plumbed into the manifest's
      node block, because under F-9a this *is* the experimental condition
      — `uv run launch dataplane/configs/pool_1b.json` normalizes the pool description,
      and every anchor manifest carries it per run rather than per machine.
- [x] **F-18 prefill/decode split verified on BOTH backends — both `full`.**
      CUDA (`b10569+cuda13.2`): `/completion` returns `timings.prompt_ms` / `.prompt_n`
      and `.predicted_ms` / `.predicted_n`; `/slots` returns exactly `--parallel` entries
      for `LiveState.kv_frac`.
      Vulkan (`b10569+vulkan`): **also `full`** — same `timings` block, same `/slots`
      shape, checked against the built binary on `Llama-3.2-1B-Instruct` (prefill
      745.9 ms / decode 451.1 ms, 4 slots). So the prefill/decode split comes from one
      code path for the whole pool regardless of backend, and the `partial` fallback is
      not needed by either.

      **This was the last thing about the engine that could have forced a contract
      change, and it will not.** `engine_version` continues to carry the backend as a
      free string (`b10569+cuda13.2`, `b10569+vulkan`); no `backend` field is needed in
      C-6.

      Note for the Week-2 CUDA/Vulkan gap measurement: this box exposes **two** Vulkan
      devices — the GTX 1650 Ti and an integrated AMD Radeon (RADV RENOIR) — so the same
      machine can produce a Vulkan node class on either. It does not help the *R* range,
      because F-9a still forbids two logical nodes on one host.
- [x] Launcher asserts `validity.colocated_nodes == 0` — one logical node per physical
      host, or the contention confound comes straight back
- [x] Trace generator produces a **byte-identical file** on regeneration from the same
      seed and parameters — as a **test**, not an assumption
      - [x] `arrival_offset_s` serialized at exactly 4 decimal places
      - [x] Three independent RNG streams from one `SeedSequence` (arrival / length / content)
      - [x] SHA-256 of the trace recorded and used as its identity downstream
- [x] Replay client runs against the **fake scheduler**, open-loop, with `send_lag_ms`
      asserted per request
- [x] Prompt materializer: `(content_seed, prompt_len, vocab_size)` → exact token
      sequence, deterministic, and agreed with the generator
- [x] C-4 client and worker log schemas + fixture files committed
- [x] `uv run contracts/check.py` green
- [ ] **C-2 decision before the freeze:** the header's `reserved_ids_excluded` is a claim
      about `vocab_size`, and a ceiling cannot exclude a floor. Llama-3 keeps its specials
      at the top so the flag is `true`; Mistral-v0.3 keeps `<unk>`/`<s>`/`</s>` at 0-2 so
      it is `false`. Either accept `false` as honest — nothing measured changes, the worker
      forces `output_len` with `ignore_eos` and no prompt is decoded back to text — or add
      a `reserved_id_floor` field to C-2. It cannot be added after the freeze, and the
      header is `additionalProperties: false`.

## Aditya Gupta (B)

- [ ] `scheduling.proto` reviewed and agreed with Divyansh (C-1)
- [ ] `SimNode.batch_capacity` reads `manifest.nodes[].engine_config.parallel` — under
      F-9 llama.cpp's slot count *is* the node model, exactly, so no
      approximation is needed or wanted
- [ ] Scheduler skeleton runs against a **fake worker** that heartbeats scripted state
- [ ] `Clock` and `StateStore` interfaces defined — policies never call the system clock
- [ ] RoundRobin only, behind the same `choose()` signature the other four will use
- [ ] C-4 scheduler log schema + fixture file committed, including the full
      `candidates` array (F-3) with `estimate_age_ms` per candidate
- [ ] **Language chosen and committed** for `controlplane/` — one language for
      scheduler + policies + DES (F-21), and `.github/workflows/ci.yml` updated with
      its build/test invocation

## Joint gate

- [ ] **End-to-end single request:** real worker, real scheduler, response returns
      worker → client directly (F-11)
- [ ] Harness replays a seeded trace and emits a joined per-request record set
- [ ] All six contract artifacts tagged `contract-v1`

---

## Closed decision

### §13 — the `priority` inconsistency — **RESOLVED**

**Resolution: passthrough label only.** `priority` is generated (F-16) and carried through
the trace and every log record, but no policy reads it, and *"high-priority p99 under
low-priority load"* is withdrawn from §5.4's dependent variables. **The spec reflects
this** — §5.4 states the resolution, and §9 records the deferred alternative.

Why it had to go rather than simply be left alone: with priority drawn independently of
the length bucket and no policy acting on it, high-priority latency equals overall latency
**by construction**. The metric could not have carried information in any condition, in
any run.

Deferred to §9 rather than rejected: correlating priority with request length
(interactive → short, background → long) is the only variant in which the metric becomes
informative, because short requests then queue behind long ones and head-of-line blocking
becomes measurable and genuinely policy-dependent. Declined on cost against a timeline
with no slack, not on merit.

- [x] Resolution chosen: **passthrough label only**
- [x] Decided by: Divyansh Shukla — 2026-08-22
- [x] Schema comments updated to state the chosen resolution
- [x] Spec updated — §5.4 and §9

### Also decide by end of Week 2 (not Week 1, but do not forget)

- [x] **C-3 `form` — RESOLVED: `lookup_table`.** Committed as a module constant
      (`calibration/cost_model.py:FORM`), not a parameter, because an option is a thing
      that gets set differently in two places. Three reasons:
      1. Concurrency is the axis that matters and it is the one a small regression cannot
         fit. Service time is flat while slots are free, then bends sharply once
         `--parallel` saturates — the knee F-4 is about. A ≤6-parameter form either
         smooths it away or spends most of its parameters describing it.
      2. The table *is* the measurement. A regression interposes a functional form between
         what the hardware did and what the scheduler believes, and when the simulator
         later disagrees with hardware (F-23) I could not tell a policy effect from a fit
         artifact.
      3. F-7 also asks for interpretable and inspectable. A reviewer reads a cell off the
         JSON and checks it against a plot.

      The cost, stated: the table only knows its grid. Off-grid queries need
      interpolation, which is Aditya's side of the seam.
      `cost_model.predict_service_ms` is the reference implementation his scheduler must
      agree with **at grid points** — disagreement there is a seam bug, not a matter of
      taste, and it is what the cross-environment determinism test would catch.
- [x] **Synthesizable *R* range — MEASURED, and it is 1.00×.** Two
      `Meta-Llama-3-8B-Instruct` node classes on the one machine I have, both
      `--parallel 4`, sustained at concurrency 4: `-ngl 20` gives 6.39 decode tok/s and
      `-ngl 0` gives 3.85. **Configuration alone reaches 1.66×; deployable *R* is 1.00×**,
      because both classes sit on the same physical host and F-9a forbids co-locating
      logical nodes.

      `uv run r-range runs/calibration/llama3-8b` reports both figures and says which is
      which, because quoting the configured number as if it were deployable would claim a
      heterogeneity the pool cannot be run at.

      **This blocks MPR-2, and the fix is a second machine, not code.** Two things follow
      that we should talk about before Week 3:
      1. MPR-2 is the 2×2 decomposition *across the synthesized R range*. With one host
         there is no range, so Week 3's "multi-node pool" needs at least a second physical
         machine on the LAN or it cannot happen at all.
      2. Even ignoring co-location, 1.66× is narrow — a GTX 1650 Ti holding 20 of 33
         layers is not far ahead of the same box's CPU. The 10–100× in the research
         question needs genuinely different machines, not a wider `-ngl` sweep on this one.

      The R-range tool refuses to compute a ratio across two *models* (F-9), so the
      1B class's 70.3 tok/s does not enter this number.
- [ ] **F-9b engine-gap measurement — BLOCKED ON VRAM, needs a decision.**
      F-9b asks for vLLM (AWQ, same model class) on the strongest node against an
      identical replayed trace. The strongest node I have is a GTX 1650 Ti: **4096 MiB
      total, compute capability 7.5**. Turing is supported by vLLM, so that is not the
      problem — the problem is that `Meta-Llama-3-8B-Instruct` in 4-bit AWQ is roughly
      5.7 GB of weights before any KV cache. It does not fit, and no vLLM flag makes it
      fit. Confirmed against the card, not assumed: `nvidia-smi` reports 4096 MiB with
      about 2.5 GB free once a server is up.

      Three ways out, and it is a scoping call rather than an engineering one:
      1. **Run F-9b at a smaller model** — `Llama-3.2-3B-Instruct` AWQ is about 2.2 GB and
         fits. The engine gap is still measured and still reported as a magnitude, but at
         3B rather than at the 8B primary condition, so the bound on threat R9 is stated
         at a different size class than the study's headline. Cheapest, and honest as long
         as the writeup says which model the number came from.
      2. **Run it on a machine with more VRAM** — Aditya's, or any node that can hold an
         8B AWQ. Keeps F-9b exactly as specified. Costs a machine I do not currently have
         in the pool.
      3. **Defer and report it as not measured**, which weakens R9 to an argument rather
         than a number. This is the option F-9b exists to prevent, so it should be the
         last resort.

      My recommendation is (1) with the model named in the caption. **Not decided
      unilaterally** — F-9b is the evidence that the single-engine decision was accounted
      for rather than hidden, so which version of it we run is a decision both of us
      should sign off on.

---

## Week-3 record — the admissible set, the anchors, and one decision I could not make alone

### Determined

- [x] **Admissible set (F-13) — `prompt <= 512, output <= 128` at a 60 s ceiling** for the
      `Llama-3.2-1B-Instruct` pool, computed from the committed C-3 series by
      `uv run admissible runs/calibration/llama32-1b`. Drawn on **p95 at every calibrated
      concurrency**, not on the mean at concurrency 1: a bucket that fits when the node is
      idle and blows the ceiling at `--parallel 4` is not admissible, because under load the
      scheduler will put four requests on that node.

      Reported with its own honesty check. A bucket is named by its **ceiling** and sampled
      in its **interior**, so "prompt ≤ 512" rests on samples that reach 256. The tool prints
      the gap rather than leaving it to be noticed in Week 6.

- [x] **Cliff (F-15)** computed from the campaign's discarded samples, which is what those
      samples are for — a fitted table excludes failures by construction and therefore cannot
      say where the cliff is.

- [x] **F-23 validation anchors — 4 valid points on one trace.** `uv run anchors` replays
      the same 200-request trace at four compressions, so `trace_sha256` is identical across
      the set and a Week-4 disagreement between vehicles cannot be a workload difference.

      | point | `rate_scale` | λ | ok | max send lag |
      |---|---:|---:|---:|---:|
      | quiet | 0.80 | 0.72/s | 198/200 | 6.6 ms |
      | light | 1.15 | 1.03/s | 198/200 | 3.7 ms |
      | mid | 1.45 | 1.30/s | 198/200 | 2.8 ms |
      | heavy | 2.20 | 1.98/s | 198/200 | 2.7 ms |

      Four rather than three, because "at least 3" is a floor and a sweep that loses a run to
      a send-lag violation should still have an anchor set. The manifests are committed.

- [x] **Load band (§5.5) — 1.03–1.30 req/s** on the one-node 1B pool, against a measured
      ceiling of about 1.6 req/s. `policy_separable` is `false` in the output and stays there
      until the pool has two hosts: with one node there is no placement to get wrong, so these
      runs bound the band from physics but cannot show that policies *differ* inside it. It is
      the same second machine MPR-2 is waiting on.

      Two rules changed after meeting real data, and both changes are the point of running the
      sweep before trusting the tool:
      1. **Onset is tail against tail**, not tail against median. The first version compared
         each point's p99 to the reference p50, and a trace mixing `p128_o64` with `p512_o128`
         puts p99 three times above p50 from the length spread alone — 1386 ms against
         4120 ms with no queue anywhere — so it fired at the floor of every sweep. Because all
         four points replay the *same* trace, the length composition is identical by
         construction and a tail-to-tail comparison isolates the arrival rate.
      2. **Saturation is read twice.** The trend test alone called a point stable that was
         visibly retiring 1.63 req/s against 1.80 offered: over a 111-second run the backlog
         built slowly enough that the fitted rise reached only 0.30 of the run's p50. The
         shortfall test — achieved against offered, which in an open-loop replay is fixed by
         the trace — sees it immediately.

- [x] **Slot-leak detection in the worker.** This wrapper is the only thing posting to its
      engine, so `/slots` reporting more busy slots than the wrapper has in flight means the
      engine is holding a slot for work nobody is waiting on. Confirmed over three consecutive
      probes, then the node reports `degraded` (C-1's `engine_state`, used for what it is
      for). One campaign logged **304 `launch_slot_` against 301 `release`** and ended with
      three of four slots stuck `is_processing` at 0% GPU utilization — the node does not fail
      at that point, it silently loses a quarter of its capacity per leak and keeps producing
      plausible numbers.

- [x] **A methodology note I paid for.** The first four-point campaign collapsed — 99 timeouts
      and a 3.7-second client send lag at an offered rate well under capacity — because I ran
      the test suite on the load host while it was measuring. The re-run on a quiet machine
      gave 4/4 valid with 198/200 ok at every point. `perf` tests are deselected by default
      for exactly this reason; the rule has to cover the *whole* suite during a campaign, not
      just the tests labelled as load measurements.

- [x] **The `engine_error`s have a cause, and it is not the hardware.** `llama-server`
      returns HTTP 500 with `"The model produced output that does not match the expected
      Content-only format"`, preceded by
      `common_chat_peg_parse: unparsed Content-only output: <0xB2>`. A lone UTF-8
      continuation byte: forced-length generation (`n_predict` + `ignore_eos`) ends
      mid-character, and this build runs `/completion` output through the chat content
      parser. **Rate: 2 in 200, reproduced three times.** Not avoidable from the request
      side — `--no-jinja`, `--reasoning-format none` and `--reasoning off` each leave it
      unchanged, and Llama-3's BPE has no separate byte-fallback token set to suppress, so
      any token can end mid-character.

      This is the same signature as the periodic `engine_error`s the Week-2 CPU node
      produced, which were left unexplained at the time. They are a **response-serialization
      failure, not a capacity limit** — the work was done and the engine refused to hand it
      over.

### Blocked, quantified

- [ ] **τ is not estimable at 8B on this hardware, and a longer run will not fix it.**
      Measured from the Week-2 sustained segments rather than argued:

      | Node class | completions/s | window needed for 5 completions | segment needed for 30 such windows | one request |
      |---|---:|---:|---:|---:|
      | `gtx1650ti_ngl20_p4_q4km_llama3_8b` | 0.230 | 21.7 s | 651 s | 18.1 s |
      | `cpu_ngl0_p4_q4km_llama3_8b` | 0.247 | 20.3 s | 608 s | 15.7 s |

      **Correction to what I first wrote here.** I read the ngl20 node's error message — *"the
      ACF drops to zero within one lag, so tau is shorter than the window it was measured
      with (6 s)"* — as evidence that τ < 6 s. It is not. At 0.230 completions/s a 6-second
      window holds **1.38 completions against a floor of 5**, so that ACF was computed on
      windows that were mostly empty, and an ACF that collapses in one lag is exactly what an
      empty series produces. The measurement was starved, not fast. `characterize` knows how
      to say that — `cadence_limited` exists for it — but it raises before it builds the
      report, so the diagnosis was thrown away and the misleading message is what I got.

      So the honest statement is that **τ at 8B was not measured**, not that it is small. And
      the floors scale with the completion rate, which is a function of **service time** — so
      they move if the sustained cell is made of shorter requests, and the way to a real
      number is a better-chosen cell rather than a longer run.

      Consequence: **no C-3 snapshot exists for either 8B node class**, because C-3 requires
      `autocorr_time_s > 0` and inventing one is the only genuinely unacceptable outcome.
      That in turn blocks the 8B admissible set (there is nothing to intersect) and leaves
      Week 4's DES with no 8B parameterization. Three ways forward, and the choice affects
      Aditya's loader, so it is **not mine to make alone**:
      1. **Emit a censored snapshot** — `autocorr_time_s` set to the resolution floor with an
         explicit marker in `stochastic`, published as an *upper bound*. The C-3 schema
         permits an extra key inside `stochastic` (only the root is
         `additionalProperties: false`), so this needs no schema change — but it needs
         `CostModelParser` to carry the marker through and the DES to treat it as a bound.
      2. **Run the 8B pool at a much higher offered concurrency**, raising the completion
         rate until the burst floor drops below τ. `--parallel 8` at 8B on a 4 GB card does
         not fit; this needs the second machine.
      3. **Report the 8B as characterized without τ**, and carry MPR-1 on the 1B alone.

      My recommendation is (1), because the bound is real information and losing the whole
      snapshot to preserve one field is a bad trade.

### Needs sign-off — the one thing I changed and could not verify alone

- [ ] **What `validity.dropped_requests` counts.** It counted every request whose final
      status was not `ok`. I changed it to count only requests the client **never heard back
      about** (`responding_node` empty: the Dispatch RPC failed, the scheduler refused it, or
      no `ResponseDelivery` arrived inside the ceiling), because `Validity.reasons()` already
      describes it that way — *"N request(s) never returned a response"* — and the
      implementation disagreed with the message.

      The reason it matters now: at 2 engine 500s per 200 requests, **roughly 87% of
      200-request runs contain at least one**, so under the old rule almost no run is ever
      valid and an F-23 anchor set cannot be assembled at all. A request the worker served
      and reported as `timeout`, `oom` or `engine_error` **is a measurement** — it is the
      cliff F-15 requires be characterized — and counting it as a load-generator fault
      conflates the instrument with the result.

      **This breaks one existing test**
      (`tests/test_replay_failure_modes.py::test_a_worker_reported_failure_is_carried_verbatim`),
      which asserts the old count. I have not touched it. Either the test moves with the
      definition, or the definition goes back and Week 3's anchors stay blocked on the
      upstream engine bug — that is a call to make deliberately, not by editing whichever
      one is more convenient.

---

## Failure modes at the seam — the standing watch list

These are not one-time checks. They are the things that will go wrong quietly.

1. **Silent policy drift** between live scheduler and DES.
   *Mitigation:* the cross-environment determinism test — same trace, same seed,
   deterministic service times injected into the DES, assert the **dispatch sequence**
   is identical. **Run it in CI, not by hand.**
2. **Cost model schema evolution mid-project.** A learns something in Week 2 and wants
   another field; that breaks B's DES.
   *Mitigation:* `cost_model_schema` version field, and B's loader **rejects unknown
   versions loudly** rather than defaulting.
3. **Clock discipline erosion.** Someone will, at some point, compute a duration by
   subtracting a worker timestamp from a client timestamp because it is convenient.
   *Mitigation:* the joined schema contains no cross-host subtraction, and
   `transport_residual_ms` is named "residual" precisely so nobody mistakes it for a
   measurement.
4. **Heartbeat gaps treated as zero load.** If a node stops heartbeating, a naive
   StateStore shows stale-but-plausible state and the policy keeps routing to it.
   *Mitigation:* `estimate_age_ms` is already in the decision record — add an explicit
   staleness ceiling above which a node is treated as unavailable, and log when it fires.
5. **Warmup discarded differently in the two vehicles.**
   *Mitigation:* `is_warmup` computed from the trace's `intended_offset_s` in both,
   never from run wall-clock.

---

## Handoff schedule

| Week | A delivers | B delivers | Joint gate |
|---|---|---|---|
| 1 | `scheduling.proto` frozen; **one** worker wrapper (llama.cpp); trace generator with byte-identical determinism test; replay client against a **fake scheduler**; log schemas + fixture files | Scheduler skeleton against a **fake worker** that heartbeats scripted state; `Clock` and `StateStore` interfaces; RoundRobin only | End-to-end single request, real worker, real scheduler. Harness replays a seeded trace and emits joined records. |
| 2 | Calibration campaign incl. `-ngl`/thread/slot sweep for the synthesizable *R* range (F-9a); **F-9b engine-gap measurement**; **time-ordered C-3 snapshots**; τ and variance envelope | Remaining four policies against fixture cost models; StalenessVeil | **MPR-1 achieved.** C-3 frozen. |
| 3 | Admissible-set determination; validation-anchor runs at 3+ operating points | All five policies live from one config value; admission filter | Load band identified. **Feature freeze.** |
| 4 | Pipeline hardened; figure scripts | DES parameterised from Week-2 snapshots; F-23 validation | Simulator agrees within stated tolerance. |
| 5 | Figures for H1/H2/H3 | R × load × staleness × policy sweeps | Hypotheses tested. |
| 6 | Threats to validity, limitations | Literature check (R-1), positioning | Report. |
