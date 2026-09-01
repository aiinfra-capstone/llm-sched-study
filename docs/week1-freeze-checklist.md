# Week-1 Freeze Checklist

The [requirements spec](scheduling-requirements-spec.pdf) is frozen. It carries the single-engine
decision as F-9 / F-9a / F-9b, and resolves `priority` as a passthrough label in §5.4.
**The interface contract (C-1 – C-6) freezes at end of Week 1.** After that, changes require the same re-scoping ritual as the spec:
state the change, state what it costs against the §6 timeline, both people agree, one PR.

This checklist is the gate. Nothing here is optional, and the last section is an open
decision that must be closed *before* the freeze, not after.

---

## Divyansh Shukla (A)

- [x] `scheduling.proto` **reviewed — my half of the C-1 sign-off.** Not a rubber stamp: I
      checked the wire against everything that has since been built, and the three invariants
      the proto claims for itself all hold in code.

      1. **`Completion` is separate from `Heartbeat`**, so the scheduler learns about a finish
         immediately rather than at the next tick. Implemented — the worker fires
         `ReportCompletion` per request. Without it, completion news would arrive only on the
         heartbeat interval, which is a second uncontrolled staleness source sitting alongside
         the one H3 injects on purpose.
      2. **`client_endpoint` travels with the request**, which is what makes F-11 cheap.
         Implemented and exercised over a real socket: the worker returns the response
         directly and the scheduler is not in the response path.
      3. **Cross-host stamps are carried, never differenced.** `client_send_mono_ns` and
         `worker_mono_ns` are the only two, and grepping for arithmetic on either returns
         nothing. Every duration in the analysis comes from one machine's monotonic clock.

      All three services and all nine messages are reachable from working code — `Scheduler`,
      `Worker` and `Client` servicers all implemented, `BeginRun` received both through
      `Worker.Begin` and down the `StreamHeartbeat` response stream.

      **Nothing needs to change for Weeks 4–6.** Staleness (H3) is injected inside the
      scheduler from its own snapshot history; the R-sweep, node-count sweep and staleness
      sweep are simulator axes; policy is a config value that reaches the record through C-4.
      None of them touch the wire.

      **One consequence worth stating rather than discovering.** A scheduler cannot learn a
      node's slot count from the wire — `Heartbeat` carries occupancy and queue depth but not
      capacity, and recovering `--parallel` from `inflight / kv_occupancy_frac` divides by
      zero on an idle node. That is deliberate: under F-9a `engine_config` is the experimental
      condition and belongs in the per-run C-6 record, not in a per-second message. But it
      means **the scheduler has to read C-6**, and there is no manifest reader in
      `controlplane/` yet. Raised in issue #6.

      Aditya's box below is his to tick.
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
- [x] **C-2 decision — closed by the freeze itself: `reserved_ids_excluded` stays a
      boolean, and `false` is the honest value for a model like Mistral.** No
      `reserved_id_floor` field was added before the contract froze at end of Week 1, and the
      header is `additionalProperties: false`, so the alternative expired rather than being
      rejected. Recording it because an item left unticked reads like an oversight, and this
      one is a decision.

      Nothing measured changes either way: the worker forces `output_len` with `ignore_eos`
      and no prompt is ever decoded back to text, so a reserved id inside a prompt is a token
      the model has and nothing more. The original text follows.

      **(original)** the header's `reserved_ids_excluded` is a claim
      about `vocab_size`, and a ceiling cannot exclude a floor. Llama-3 keeps its specials
      at the top so the flag is `true`; Mistral-v0.3 keeps `<unk>`/`<s>`/`</s>` at 0-2 so
      it is `false`. Either accept `false` as honest — nothing measured changes, the worker
      forces `output_len` with `ignore_eos` and no prompt is decoded back to text — or add
      a `reserved_id_floor` field to C-2. It cannot be added after the freeze, and the
      header is `additionalProperties: false`.

## Aditya Gupta (B)

- [x] `scheduling.proto` reviewed and agreed with Divyansh (C-1) — signed off on
      2026-08-31 in issue #6: *"I have reviewed the wire schema. The invariants hold
      perfectly in the code, and separating `Completion` from `Heartbeat` is exactly what
      we need to prevent uncontrolled staleness in the simulation. Signed off."*
      **C-1 is closed on both sides**, and `contract-v1` is authorized.
- [x] `SimNode.batch_capacity` reads `manifest.nodes[].engine_config.parallel` — under
      F-9 llama.cpp's slot count *is* the node model, exactly, so no
      approximation is needed or wanted
      — `Manifest.java` / `ManifestParser.java`, `SimNode.batchCapacity()`.
- [x] Scheduler skeleton runs against a **fake worker** that heartbeats scripted state
      — `live/FakeWorker.java`, `live/SchedulerGrpcService.java`.
- [x] `Clock` and `StateStore` interfaces defined — policies never call the system clock
      — `core/interfaces/`, with `sim/SimClock.java` as the DES implementation.
- [x] RoundRobin only, behind the same `choose()` signature the other four will use
      — and all five are now there: `core/policies/`.
- [x] C-4 scheduler log schema + fixture file committed, including the full
      `candidates` array (F-3) with `estimate_age_ms` per candidate
- [x] **Language chosen and committed** for `controlplane/` — one language for
      scheduler + policies + DES (F-21), and `.github/workflows/ci.yml` updated with
      its build/test invocation — Java 17 + Maven, and CI runs `mvn clean compile`
      against a Temurin JDK rather than printing a placeholder.

## Joint gate

- [ ] **End-to-end single request:** real worker, real scheduler, response returns
      worker → client directly (F-11)
- [x] Harness replays a seeded trace and emits a joined per-request record set
      — `uv run pipeline runs/anchors/anchor1b_mid_* --trace ...` produces `joined.parquet`
      from real client and worker logs. The scheduler columns are null until Aditya's
      scheduler is the one in the path: my fixture writes no C-4 decision record, because a
      fixture with no state store cannot fill in `candidates[].estimate_age_ms` honestly and
      fabricating it would be worse than the gap.
- [x] All six contract artifacts tagged **`contract-v1`** — an annotated tag pinning each
      artifact by content hash, so "the contract" names bytes rather than a branch that keeps
      moving. Verified green by `uv run contracts/check.py` at the tagged commit.

      | | Artifact | sha256 (first 16) |
      |---|---|---|
      | C-1 | `scheduling.proto` | `8c2395ebfeb1be12` |
      | C-2 | `trace.schema.json` | `431238ae178fcbf3` |
      | C-3 | `cost_model.schema.json` | `d4607dea9f2e6cf5` |
      | C-4 | `log_client.schema.json` | `451554a02c5914a5` |
      | C-4 | `log_scheduler.schema.json` | `75c48270a2b47109` |
      | C-4 | `log_worker.schema.json` | `305d52b106276fc6` |
      | C-5 | `joined_record.schema.json` | `a8186c4d5335316f` |
      | C-6 | `manifest.schema.json` | `3f08a8dec333b388` |

      The freeze has held in practice, which is the part worth recording: Weeks 2 and 3 added
      a live worker, three calibration campaigns, an admissible-set determination, an anchor
      campaign and a load band, and **none of it required a schema change**. The two places
      that came closest both resolved without one — F-18's prefill/decode split turned out to
      be `full` on both backends so C-6 needed no `backend` field, and the engine patch rides
      inside `engine_version` as the free string it was designed to be.

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
      `--parallel 4`, sustained at concurrency 4 **on an identical cell**: `-ngl 20` gives 7.49 decode tok/s and
      `-ngl 0` gives 3.74. **Configuration alone reaches 2.00×; deployable *R* is 1.00×**,
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
      2. Even ignoring co-location, 2.00× is narrow — a GTX 1650 Ti holding 20 of 33
         layers is not far ahead of the same box's CPU. The 10–100× in the research
         question needs genuinely different machines, not a wider `-ngl` sweep on this one.

      The R-range tool refuses to compute a ratio across two *models* (F-9), so the
      1B class's 71.6 tok/s does not enter this number.
- [ ] **F-9b engine-gap measurement — scoped, and deliberately scheduled into Week 6.**
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

      **DECIDED 2026-08-31 — option (1), and it is now a scoping decision on the record.**
      Aditya, in issue #6: *"Let's go with your suggested Option (a). We will run it at 3B
      (Llama-3.2-3B AWQ) so it fits the 4GB VRAM and explicitly caption the model
      substitution in the final report."* So F-9b is measured at
      `Llama-3.2-3B-Instruct` AWQ rather than at the 8B primary condition, and every place
      the number appears has to say so. The probe node already has somewhere to live: C-6
      types `role: "engine_gap_probe"`, and `pipeline/join.py` drops its rows before the
      join rather than after, so a probe can never be averaged into a policy comparison by
      accident. What remains is the run itself, which needs the vLLM install and a slice of
      GPU time that currently competes with the pool.

      **DEFERRED TO WEEK 6, on purpose rather than by drift.** F-9b is a *bound on a threat*,
      not an input to anything: no hypothesis reads it, no figure waits on it, and nothing
      downstream changes shape depending on what it says. Weeks 4 and 5 are the ones with a
      dependency chain running through them — the simulator has to be parameterised, the
      sweeps have to run, and both compete for the same single GPU that F-9b would occupy.
      Spending an afternoon on the vLLM install and a multi-gigabyte AWQ download now would
      buy a number we cannot use until the threats section is being written anyway.

      So it moves to Week 6 and sits next to the threats-to-validity work it belongs to. The
      risk of deferring is the ordinary one — Week 6 has the least slack of any week — and
      the mitigation is that option (3), reporting it unmeasured, remains available and
      degrades R9 from a number to an argument rather than removing it. That is a worse
      outcome, and it is the one to avoid, which is why this is written down as a scheduled
      item rather than left as an open box that quietly ages.

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
      | quiet | 0.80 | 0.72/s | **200/200** | 26.1 ms |
      | light | 1.15 | 1.03/s | **200/200** | 12.8 ms |
      | mid | 1.45 | 1.30/s | **200/200** | 9.3 ms |
      | heavy | 2.20 | 1.98/s | **200/200** | 5.7 ms |

      Four rather than three, because "at least 3" is a floor and a sweep that loses a run to
      a send-lag violation should still have an anchor set. The manifests are committed.

      These are the numbers from the patched engine. The set collected before `+p1` had
      198/200 at every point — the two missing per run were the engine discarding a completed
      generation, not the pool failing — and it is kept under
      `runs/superseded/week2-unpatched-engine/` rather than deleted, so the two can be
      compared.

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
      gave 4/4 valid with 198/200 ok at every point — and 200/200 once the engine patch
      landed. `perf` tests are deselected by default
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
      unchanged. Llama-3's BPE *does* carry byte tokens — I wrote the opposite here first and
      it was wrong — so a `logit_bias` could suppress them; it is still not a way out,
      because truncation on a **lead** byte fails the same parse.

      This is the same signature as the periodic `engine_error`s the Week-2 CPU node
      produced, which were left unexplained at the time. They are a **response-serialization
      failure, not a capacity limit** — the work was done and the engine refused to hand it
      over.

### Blocked, quantified

- [x] **τ measured — and the answer is that it lives on the CPU node.** Both corrections I
      had to make to get here are worth keeping, because each one changed the conclusion.

      **First, the Week-2 reading was junk.** I took the ngl20 node's error — *"the ACF drops
      to zero within one lag, so tau is shorter than the window it was measured with (6 s)"* —
      as evidence that τ < 6 s. It was not. That ACF ran on windows holding a third of one
      burst; an ACF that collapses in one lag is what an empty series produces. Starved, not
      fast.

      **Second, I had the floor wrong by a factor of four**, and fixing it collapses the whole
      thing to one line. `bursts_per_window` is `samples_per_window / batch_size`, and at
      saturation `samples_per_window = (batch_size / service) × window`, so batch size
      cancels:

      > **`bursts_per_window == window_s / service_s`** — the five-burst floor means
      > **`window ≥ 5 × service`**, and τ is only visible if **τ > 5 × service**.

      Checked against the one node that produced a report: the 1B ran a 1.80 s cell in a 10 s
      window and recorded `bursts_per_window: 5.542`; 10 / 1.80 = 5.54. The Week-2 cells put
      the window *above* τ on every node, so they could not have worked however long they ran.

      **Re-run on the patched engine with cells sized from that rule:**

      | node class | cell | service | window | bursts | τ | *r²* |
      |---|---|---:|---:|---:|---:|---:|
      | `cpu_ngl0` (8B) | p64/o24 c4 | 9.6 s | 48 s | 5.03 ✓ | **69.5 s** | **0.989** |
      | `gtx1650ti_ngl20` (8B) | p64/o24 c4 | 6.0 s | 32 s | 5.43 ✓ | ≤ 32 s | 0.0 |
      | `gtx1650ti_ngl99` (1B) | p64/o32 c4 | 0.81 s | 3.5 s | 4.33 ✗ | ≤ 3.5 s | 0.0 |

      **MPR-1, with a number in it at last:** on `cpu_ngl0`, τ = 69.5 s (integrated 89.0 s,
      1/e 72.1 s — three estimators that do not share a failure mode, on a 0.989 fit), and a
      single calibrated tok/s figure there **understates its own standard error by 1.36×** —
      16.7 independent windows in 31. The two GPU classes show no decay at any timescale their
      service time lets them reach.

      Three things to carry forward rather than bury:
      1. `tau_resolved` is **false** even for the CPU class, because the bar is
         τ ≥ 2 × `resolution_floor_s` and that floor is `max(median request, window)` = 48 s.
         The bar is conservative by construction: the physical confound is one request
         duration, 9.6 s, and τ is 7.3× that. Report the fit and the bar together.
      2. The 1B point is `cadence_limited` (4.33 against 5) — real service came out at 0.81 s
         where the cost table predicted 0.63 s. Its bound holds; its value should not be quoted.
      3. **H3's staleness axis now has a home.** After Week 2 I wrote that a τ below ~10 s
         would put "staleness approaching τ" inside any sane heartbeat interval and force the
         axis to be re-grounded or declared simulator-only. At 70 s on the CPU node it is a
         real regime on real hardware — and it is the class the pool's heterogeneity is built
         from, which is the useful way round.

- [x] **The 8B now has C-3 snapshots — 18 and 23 of them.** This was the Week-4 blocker: the
      DES had nothing to parameterize the headline model from. The censored-snapshot proposal
      is withdrawn for the CPU class, which has a real τ; the ngl20 class carries the window as
      an upper bound with `fit_r2: 0.0` beside it, which is legible from the file alone and
      needs no schema change.

- [x] **R was biased low by 20%, and the cause was a cell mismatch.** Week 2 measured the fast
      class (`ngl20`) at p256 and the slow one (`cpu`) at p64. Decode rate falls as the KV
      context grows, so the fast node carried a handicap the slow one did not, and the ratio
      came out **1.66×**. Both classes on one cell now: **2.00×** configured, 1.00× deployable.

      The general lesson is worth stating because it will recur: **R and τ want different
      cells.** R needs both classes on an identical operating point; τ needs the shortest cell
      the node allows. Forcing one sustained segment to serve both is what produced a biased
      ratio *and* a censored τ. If they conflict again, run two segments.

- [x] **The last skipped forward test now runs.**
      `test_the_synthesizable_r_range_is_a_range_not_a_figure` had been skipping since Week 1
      for two reasons: it names the module `dataplane.calibration.r_range` and I built
      `rrange`, and it wants `synthesizable(snapshots) -> (lo, hi)` off C-3 snapshots where I
      had `synthesizable_range(classes) -> RRange` off campaign reports.

      The module is renamed to the name the contract gave it. The `rrange` redirect that
      carried the old import path for one commit is gone, and the two test files that used it
      now import `r_range` directly — an import path and an identifier, no assertion touched. `synthesizable` is a second entry point rather than a replacement, and it is the
      one that matters across the seam — the snapshots are committed, so R is recomputable
      from `contracts/cost_models/` without a copy of my run directories.

      It **only takes the ratio at a cell every node class shares**, which is the Week-3
      lesson made mechanical: comparing a class measured at p256 against one at p64 folds a
      workload difference into R, and that is precisely what read 1.66× instead of 2.00×. No
      common cell now raises rather than quietly producing a number.

      The test needed a two-class fixture, which `example_campaign_dir` is not and must not
      become — it is a *series*, one node measured repeatedly, which is the shape F-8's
      staleness lookup ages through. So `example_pool_dir` sits beside it for the other shape
      the artifact has to support.

      Note for whoever quotes R: the two estimators read **2.07** (fitted table at a shared
      cell) and **2.00** (median per-request decode over the sustained segment) on the same
      snapshots. Both are documented; pick one and say which.

### Resolved without a sign-off — because the cause turned out to be fixable

- [x] **What `validity.dropped_requests` counts — unchanged, and that is the right answer.**
      I had loosened it to "requests the client never heard back about", because at 2 engine
      500s per 200 requests roughly 87% of runs contained one and under the strict rule no
      anchor set could ever be assembled. That was treating the symptom, and it cost a test
      that was correct.

      The 500 was a bug in llama.cpp's `/completion` path, not a fact about the workload
      ([`patches/`](../patches)). With it fixed the anchor campaign returns **200/200 ok at
      every operating point**, and the strict rule is the one that belongs: a replay runs
      *inside* the admissible set, where by construction nothing should fail, so a failure
      means the run is not measuring what it claims. The F-15 cliff is characterized from
      calibration observations, which deliberately probe *outside* that set, and does not
      need replay runs to be allowed to fail.

      Reverted. `tests/test_replay_failure_modes.py::test_a_worker_reported_failure_is_carried_verbatim`
      is green untouched, and so is the rest of the suite. **The general lesson: when a
      guard starts failing constantly, check whether the guard is wrong before you widen
      it.** I nearly widened it.

- [x] **F-20 gap closed: `campaign.json` now embeds the config it ran, plus its hash.** C-6
      manifests carry `config` verbatim precisely so a run reproduces from the record rather
      than from a file someone may since have edited; the calibration report did not, so
      reproducibility rode on a mutable path — and that is exactly how a sustained-cell edit
      went unnoticed while a campaign was mid-flight. Same canonical hash the manifest uses,
      so the two artifacts agree on what "the same config" means.

      **Not backfilled.** The runs already on disk predate the field. Writing provenance
      after the fact is the weakness the field exists to remove, so they stay without it and
      it applies from the next campaign on.

---

## Week-4 record — my half of it, done ahead of the LAN

Week 4's table says *A: pipeline hardened; figure scripts*. Both are pure functions of the
files a run leaves behind, so neither waits on the second machine, and I built them while
the LAN is still to be cabled.

### What the LAN actually gates, now that I have checked

Less than I assumed. The Week-4 **joint gate** is F-23 — the simulator agreeing with the
machine within a stated tolerance — and F-23 asks for 3+ operating points replaying an
*identical* trace. I have four, on one node, all sharing `trace_sha256 bea05462…`. A
single-node validation is a real validation; the LAN widens it to a *heterogeneous* pool,
which is MPR-2, and MPR-2 was already blocked on the second machine.

So the LAN gates MPR-2's 2×2 and `policy_separable`, and nothing in Week 4. What gates
Week 4 is **issue #6**: Aditya's C-3 parser still rejects `Provenance.engine_config`, so 0
of my 50 committed snapshots load into his side and his DES cannot be parameterised from
Week-2 data. That is the item to chase, ahead of cabling.

### Two things the hardening actually fixed

**`--r` was a typed-in flag.** `pipeline --r 2.0` is defensible for one run and is a loaded
gun across a twenty-run sweep: *R* is H2's independent variable, and a wrong value does not
fail, it relabels a point on the x-axis. `runset.deployed_r` derives it from the run's own
`cost_model_snapshots` instead, delegating the division to `r_range.synthesizable` so the
common-cell refusal is not reimplemented. A single-class pool reads exactly **1.0** — the
honest number for one host, and the reason deployable *R* is 1.00× against a synthesizable
2.00×. On the four anchor runs it derives 1.00× unprompted.

**F-24 had nowhere to read `vehicle` from.** The stamp was always meant to come from
`manifest.vehicle`, and a figure drawn from a *set* has no single manifest. The label now
rides in the rows, attached at assembly rather than in `join.py` — C-5's column list is
frozen and the simulator emits against it, so `vehicle` is a property of how a set was
assembled, not a field of a joined record.

### Two corrections to my own earlier work

I wrote the stamping rule backwards. My first version stamped every figure, reasoning that
a missing stamp would then be visibly a bug rather than a claim of hardware provenance. My
own Week-1 forward test says the opposite — *"the label has to mean something, so it cannot
be on everything"* — and it is right. The frozen test won; the code stamps simulated
figures only, refuses a manifest with no `vehicle`, and stamps mixed provenance as
simulated.

And `figures.percentile` took a percent while `loadband._percentile` takes a fraction. Two
same-named functions disagreeing about their unit fail silently toward returning the
minimum. The test pinning the two estimators equal caught it on its first run, which is the
entire reason that test exists: the figure annotates the §5.5 band, so it must use the
band's own estimator. They now agree **exactly** at all four anchor points.

### Retrospective audit against the frozen spec

Walked every requirement on our half — F-6 through F-24, C-2 through C-6, MPR-1 and MPR-2,
§5.4 and §5.5 — against the code rather than against the previous week's notes. Most of it
held. Four things did not, and all four are now closed except where closing them would
mean changing a frozen artifact alone.

**F-23 was under-implemented.** The requirement is specific: *"agreement in p50 **and** p95
end-to-end latency within a stated tolerance across at least 3 operating points. The
tolerance and the observed error MUST be reported."* The validation figure plotted p95
only, no tolerance appeared anywhere in the repository as a number, and no error was
computed. `validation_error` now returns both percentiles, the relative error at each
matched operating point, and a pass/fail against a stated tolerance; the figure draws both
panels with the tolerance as a band and writes the verdict into its own title.

**The tolerance is set by the anchors, not chosen for roundness.** Bootstrapping the four
committed anchor runs at 4000 resamples gives 95% intervals on their *own* percentiles of
**±25.9% for p50 and ±24.7% for p95**, at n = 180–196 measured requests per run. That is the
resolution of the instrument the simulator is being compared against, so `F23_TOLERANCE`
is **0.25**. A tighter tolerance would not be a stricter test but an unfalsifiable one.
Raising the precision is a matter of run length rather than analysis — halving the
interval takes roughly four times the requests per anchor — and until those runs exist,
25% is the honest floor. Every reported error now carries the anchor's own interval beside
it so the limiting side of the comparison stays visible.

**§5.4 named four dependent variables and the analysis layer computed one.** End-to-end
p50/p95/p99 was there; queue wait, per-node utilization, and routing-error rate were not.
Queue wait and the routing-error rate now come out of `by_offered_load`, and
`per_node_utilization` is a new function. Queue wait turned out to locate the queueing
onset far more sharply than the p99 test does: p50 queue wait is **0.01 ms** at both the
quiet and light anchors, **731 ms** at mid, and **11.8 s** at heavy. The band's upper edge
sits exactly where that transition happens, which is a third independent confirmation
alongside the latency-drift and throughput-shortfall tests.

The routing-error rate reports `None` rather than `0.0` when no request carries a
scheduler decision. A fixture-driven run observed nothing about routing, and reporting
zero would claim routing was perfect — the opposite claim, and it must not share a value.

**MPR-2 had no estimator.** §7 words it as *"the 2×2 decomposition (H1) across the
synthesized heterogeneity range of F-9a, reported as a range rather than a single
figure"*. We had `h1_interaction` at one operating point, which is the ingredient rather
than the deliverable. `mpr2_interaction_range` evaluates the 2×2 at every *R* in a sweep
and returns the interval with the *R* values that produced its ends, plus a
`sign_consistent` flag — because an interval straddling zero is a publishable negative
result and a different statement from a mean interaction that happens to sit near zero.

### One proposed amendment, raised rather than made

**C-5 cannot express per-node utilization.** Its only node identity is `chosen_node`,
which comes from the scheduler's decision record. The client log knows the responding node
and the worker log knows its own, and C-5 keeps neither — so a run without a scheduler log
cannot say which node did the work, and `per_node_utilization` returns an empty table for
the four committed anchors. It will populate as soon as a real scheduler writes decision
records, so this blocks nothing today.

Adding `served_by` to C-5 would close it properly. The six artifacts froze at the end of
Week 1 and changing one is a joint decision, so this is raised here rather than made
unilaterally. Our view: worth doing, low risk — C-5 is produced and consumed entirely
within the data plane, the simulator emits C-4 rather than C-5, and no schema currently
sets `additionalProperties: false`.

### Where it stands

`runset` and `figures` run on the committed anchor set today. `latency_vs_load` and
`throughput_vs_load` draw, and the throughput curve leaves *y = x* at ≈1.3 req/s —
independently reproducing the load band's upper edge, which was placed by the latency-drift
test rather than by the shortfall test. `validation` is written and refuses until the
simulator's half of the set exists, rather than drawing one vehicle and calling it a
validation.

Landing `figures.render` also un-skipped `test_forward_w5_figures.py`, which had been
skipping since Week 1. It is now green: **572 tests, 100% coverage, no skips.**

---

## Week-4 close — the seam opened, and the first thing through it was bad news

Issue #6 came back resolved on 2026-08-31. All four items on Aditya's list landed in one
commit: `Provenance.engine_config` on the C-3 parser, `Manifest.java` /
`ManifestParser.java` for C-6, `mvn clean compile` wired into CI, and the C-1 sign-off.
`ServiceSampler` also gained nearest-concurrency snapping, which is worth noting because it
means the two sides now interpolate the same way — `predict_service_ms` has always snapped
to the nearest measured concurrency, so this converged on the reference rather than away
from it.

That unblocked the question the whole week is about, and the answer was not the one we
expected.

### The cost model did not predict its own hardware

`uv run costcheck runs/anchors` prices every request the four anchors served against the
C-3 snapshot the manifest says that node was deployed under. It needs no simulator, and
that is the point: an F-23 failure has two suspects on opposite sides of the seam, and this
is the one that can be settled alone.

Scored against the snapshot the anchors actually ran under, the model was wrong by a
**request-weighted 127%**, with eleven of twelve exercised cells outside the ±25% tolerance
and the worst at 353%. No discrete-event simulator parameterised from that table could have
passed F-23, and any argument about the DES would have been an argument about the wrong
half of the system.

Two causes, and neither is a bug in anyone's code.

**The grid was calibrated at lengths the study does not run.** `calibration_1b_dense.json`
sampled prompt 64 and output 32. The anchor trace runs `p128_o64`, `p256_o64` and
`p512_o128`. Both prompt lengths fall inside the same `[1, 128]` bucket and both output
lengths inside `[1, 64]`, so nothing was out of range and nothing raised — but decode time
is linear in output tokens, so a cell measured at 32 tokens under-predicts a request of 64
by about half. `campaign.py`'s own docstring warns about exactly this: *"one measured length
per bucket, chosen by whoever writes the config, because a bucket sampled only at its lower
edge would advertise a speed the bucket's longer requests never see."* The warning was
written and then not followed.

**The concurrency axis had holes where the anchors live.** The grid measured 1 and 4 slots.
Reconstructing in-engine concurrency from the client log alone — a request occupies the node
from send to completion, and the wrapper admits at most `--parallel` of them — the anchors
spend a great deal of their time at 2 and 3:

| anchor | λ (req/s) | mean concurrency | c=1 | c=2 | c=3 | c=4 |
|---|---:|---:|---:|---:|---:|---:|
| quiet | 0.72 | 1.92 | 45.9% | 27.6% | 14.8% | 11.7% |
| light | 1.03 | 2.66 | 26.9% | 17.6% | 17.8% | 37.8% |
| mid | 1.30 | 3.28 | 13.0% | 9.8% | 13.1% | 64.0% |
| heavy | 1.98 | 3.98 | 0.3% | 0.2% | 0.4% | 99.1% |

Nearest-concurrency snapping then does something quietly awful: `c=2` is closer to 1 than to
4, so a request served by two slots is priced at the one-slot rate. At the quiet anchor that
is 28% of requests under-predicted by more than half.

### What the anchors say about batching, which is a result in its own right

The worker log carries the F-18 split, so the two effects can be separated. Prefill is a
function of prompt length and is flat in concurrency (~174 ms at 128 tokens, ~343 at 256,
~698 at 512, moving less than 10% from one slot to four). Decode is where all of the
concurrency dependence lives, and per-request decode throughput falls almost exactly as
`1/c`: 143, 71, 58, 39 tok/s at one through four slots.

**Aggregate decode throughput is therefore flat.** On this node, batching buys nothing —
four concurrent requests finish in about the time four sequential ones would. That is the
signature of a memory-bandwidth-bound decode on a small card, and it is worth stating
plainly because it bounds what any routing policy can achieve here: with no batching gain,
a slot is a slot, and the concurrency knee F-4 is about is a queueing effect rather than a
throughput effect on this hardware. On a node where batching does pay, the same policies
would be choosing between different things.

### Recalibration, and what it bought

Two campaigns, both with the anchors' own lengths as the grid's representative points and
concurrency measured at every level the pool can reach:

  * `calibration_1b_anchorgrid.json`, first pass — prompt {128, 256, 512} × output {64, 128}
    × concurrency {1, 2, 3, 4}, keeping the original bucket edges. Weighted error fell from
    **127% to 27.5%**, and the extrapolation warning disappeared entirely: every cell the
    anchors visit is now a cell that was measured.
  * The same grid with a prompt edge added at 256, so that `[129, 512]` — inside which
    prefill varies fourfold — becomes `[129, 256]` and `[257, 512]`. The anchors'
    `p512_o128` requests had been priced by a cell that averaged prompt 256 and prompt 512
    together. Weighted error **20.8%**, inside the ±25% tolerance, and **9.9% on medians**.
    The `c=4` cells, which carry the most requests and are where the anchors live under
    load, land within 2%.

**127% → 27.5% → 20.8%.** This is the first version of the cost model a discrete-event
simulator could be parameterised from and still have a chance at F-23.

Five cells are still outside on the mean, all of them at one or two slots, and they do not
all have the same explanation. Three come inside on medians — 12.9%, 23.8% and 18.7% — and
those are the mean-versus-median artefact described below. **Two do not: both `c=2` cells
sit at 38.3% and 30.2% on medians as well**, so the two-slot row is genuinely the weakest
part of the table and not a labelling artefact. Two slots is also the row with the fewest
calibration samples relative to how much of the quiet and light anchors live there, which is
the first thing to deepen if this needs to get tighter.

The **second** campaign's nine snapshots are what got committed, alongside the original
Week-2 series which stays where it is. The Week-2 series is not stale data — the four anchor
manifests name its snapshot ids, and rewriting what a run was deployed under would be worse
than carrying two — so `costcheck runs/anchors` with no `--snapshot` still reports 127%,
because that is genuinely what those runs were served by. The first recalibration pass was a
step on the way and is not committed; its numbers are here because the 27.5% is what
identified bucket width as the remaining term.

**A caveat that belongs next to the result.** Choosing bucket representatives that match the
trace's lengths is what `campaign.py` asks for, and the buckets are *named* after those
lengths (`p128_o64`), so this is aligning the instrument with the workload rather than
fitting it to the answer. But the agreement it produces is conditional on that workload: a
future trace that emitted a 300-token prompt would land in `[257, 512]`, be priced at the
512 rate, and be over-predicted. The cost model is calibrated for the traces this study
replays, and that is a limitation to carry into §threats rather than a property to claim in
general.

### The mean and the median disagree, and the reason is measurable

`costcheck` reports both, and the gap is informative rather than cosmetic. The prediction is
a mean measured with concurrency **held fixed**. A live run's cell is a mean over requests
labelled by their concurrency **at admission** — and a request admitted alone onto a busy
node does not stay alone. Those requests keep the low-concurrency label and carry a
high-concurrency service time, which lands entirely in the upper tail: at the `[1, 128]`,
`[1, 64]`, `c=1` cell the observed median sits 11.7% above prediction while the mean sits
69.6% above it.

The mean stays the verdict, because a mean is what the model predicts. The median is
reported beside it so that the difference between "the cost model is wrong" and "the
admission-time label is a proxy" can be seen rather than argued about.

### Two guards added to `contracts/check.py`

Both are §12 failure modes turned into tests, and both were prompted by things that arrived
in this commit rather than by hypotheticals.

**C-1 now exists twice on disk.** The control plane's Maven build compiles its own copy of
`scheduling.proto`, and `contract-v1` pins only the one under `contracts/` by content hash.
The check compiles both to descriptor sets and compares messages, field numbers, types,
labels and RPC signatures, ignoring comments, formatting and the `java_package` options that
differ legitimately. They agree today. The point is that they will keep agreeing.

**The far side's JSON bindings are checked against our schemas by name.** A C-3 field added
here is a field Jackson throws on there, which is precisely how 50 committed snapshots
became unreadable for three weeks. A property a *strict* record fails to declare is now a
contract failure. The reverse — a name bound there that no schema defines — prints as a NOTE
rather than failing, because that is a bug in the reader rather than a non-conforming
artifact.

The NOTE currently fires twice, and both are worth acting on:

  * `Manifest.java` binds `manifest_schema`. C-6 has no version field. C-3 has
    `cost_model_schema` and it is required, so expecting the same of C-6 is reasonable —
    but adding it is a post-freeze change to a frozen artifact and therefore a joint call.
  * `Manifest.java` binds `node_class`. C-6's node block does not carry one, deliberately:
    the mapping lives in `cost_model_snapshots`, which is `node_id -> snapshot_id`, and the
    node class is inside the snapshot the id names. **That map is the piece the DES actually
    needs and the reader does not yet read.** Resolving a node to its cost model is
    `manifest.cost_model_snapshots[node_id]` -> load that C-3 file -> `node_class` and
    `entries`. No contract change required.

### One Week-3 gap closed as a side effect

The admissible set was determined in Week 3 as `prompt ≤ 512, output ≤ 128`, and the JSON
said out loud that the ceiling was not evidence: `max_prompt_measured` was **256**, and
`unmeasured_ceiling` carried an entry saying the 512 claim rested on samples that reached
half of it. The new grid measures prompt 512 directly, so re-deriving from it gives the same
envelope with `unmeasured_ceiling` **empty** and `max_prompt_measured: 512`. Nothing about
the determination moved; what moved is that it is now supported at the number it claims.
`runs/admissible/llama32-1b.json` is rewritten from the new campaign for that reason.

### Where Week 4 stands

Our column of the handoff table — pipeline hardened, figure scripts — was already done. What
this week added was the diagnostic that sits upstream of the joint gate, and the answer it
gave, which is that the gate could not have been passed with the cost model we had. It can
be attempted with the one we have now.

`validation` still refuses to draw, and still should: F-23 compares two vehicles and only one
of them exists in the committed run set.

---

## Ahead of the LAN — the clock measurement, and what it does not buy

MPR-2 is still blocked on a second machine, but one piece of the two-machine setup could be
built and reasoned out now rather than in the hour the second box arrives: measuring how far
apart the machines' clocks are, recording it, and correcting what it licenses correcting.

The prompt for this was an argument about whether the LAN needed better hardware. It did
not, and working the numbers is what settled it: a request on this hardware is 600 ms to
2.3 s (prefill 174/343/698 ms at prompt 128/256/512, plus 64 output tokens at 143 tok/s
single-slot down to 39 tok/s at four), while a clean 5 GHz link adds a few ms typical with a
20 to 50 ms tail. That is 0.3% of a request at the median. Network latency is not the
problem, and I had earlier said scheduling effects here live at millisecond scale, which is
wrong: queueing delay at four slots with service times in the hundreds of ms is itself
hundreds of ms.

What survives that is not about jitter at all.

**Offset is not a fraction of anything.** It is a systematic bias on every cross-host
interval and does not shrink because the requests are slow. But this study has no cross-host
interval. `e2e_ms` is client-local, `queue_wait_ms` and `service_ms` are worker-local,
`decide_us` is scheduler-local, and `transport_residual_ms` is a difference of those, in
which a constant offset cancels exactly. So the offset corrects nothing, and I record it
anyway, because "the clocks were disciplined" should be checkable rather than assumed.

**The offset could not correct the wire timestamps even in principle.** `client_send_mono_ns`
and `worker_mono_ns` are `CLOCK_MONOTONIC` reads whose zero is each machine's boot. An NTP
offset is about wall clocks and says nothing about the gap between two boot times. This is
the sharpest reason F-18's transport decomposition stays out of reach, sharper than the one
in the split doc, and it means no amount of clock work reopens that question. Splitting the
residual would need the wire timestamps moved onto a synchronised wall clock, which is a C-1
change, and C-1 is closed on both sides.

**Rate is the one that touches a number, and chrony's real value is rate discipline.** A
worker clock ticking r ppm faster inflates every worker-local duration by r ppm relative to
`e2e_ms`, multiplicatively, so it does not cancel. Linux slews `CLOCK_MONOTONIC` along with
the system clock, so a disciplined host's monotonic clock ticks at the reference's rate,
which is what makes two machines' durations comparable in the first place. `join` divides
the difference out and prints its size. This host's chrony reports a 1.1 ppm skew and a
−0.031 ppm residual frequency, so on a two-second service time the correction is about 2 µs,
against a residual quoted in milliseconds. That is the point: the conclusion is unchanged and
is now a measured number in the run summary rather than a claim in a README.

**On the F-23 headroom.** Tolerance is ±25% and the recalibrated model sits at 20.8%
weighted, so 4 points of headroom. If the measured path carried network latency the simulator
does not, that asymmetry would eat into what is left. At the numbers above it does not come
close to mattering, and it belongs in the caveats rather than in a hardware decision.

What went in: `clocksync` (measure per host, combine on one), an optional C-6 `clock_sync`
block, the rate correction in `join`, and a clock reading in `preflight`. Two deliberate
restraints. Preflight reports an undisciplined clock but does **not** fail on it, because a
preflight sees one machine and failing there would conflate "the LAN is misconfigured" with
"chronyd is not installed on the box I ran this from"; the teeth are in
`clocksync --combine` and in the join summary, both of which see every host. And an
unmeasured host is recorded as unsynchronised, never as offset zero, because zero reads as
agreement and agreement is the one claim nobody made.

Aditya needs to know about the C-6 addition, which is why it went to him as an issue rather
than only into this file. It is additive and optional, his `Manifest.java` is a lenient
reader that ignores it, and the simulator has one host and one clock and nothing to correct.

---

## Week 5 — the hypothesis figures, built ahead of the data

The four estimators had been here since Week 4, tested as arithmetic and drawn by nothing.
`FIGURES` held three characterisation plots and none of them called an estimator, so
`uv run figures` could render the load band and the F-23 validation but not one of the
three results the study exists to report. That is now closed: H1, H2, MPR-2 and H3 all
render, and they render from `example_sweep`-shaped fixtures rather than waiting on the
pool, so the day the LAN comes up the sweep output has somewhere to go.

Four decisions in there worth keeping.

**H1 is an interaction plot, not bars.** H1 is a claim about two lines being non-parallel.
Grouped bars carry the same four numbers and hide it, because the eye compares heights
within a group rather than slopes across one.

**The unit is a run.** Every estimator takes one row per run, and `sweep_from` is what
reduces the frame. A run replayed one trace under one condition, so its requests are
correlated rather than independent samples of that condition, and pooling requests would
weight each cell by run length. The suite pins the size of that: on two runs of the same
policy at the same *R*, per-run gives 204.5 ms and pooled per-request gives 266.3 ms.

**τ is an argument, never a default.** H3's axis is estimate age over the measured
autocorrelation time, and that is the whole reason the axis transfers to other hardware.
Defaulting it to anything, including the heartbeat interval, would silently rescale the
only axis the hypothesis is about, so the figure is skipped without `--tau-s` rather than
drawn against a guess.

**Skipped by default, refused by name.** `drawable()` names the conditions under which
each figure's estimator would refuse anyway, so a partial set renders what it can instead
of failing on the first gap. Asking for a skipped figure explicitly still raises and says
why. Silence and refusal are both correct; which one you get should depend on whether you
asked.

One thing the fixture had to fight: an H2 fixture that rose monotonically in *R* would let
an estimator reporting the wrong shape pass anyway, so the test data rises and falls, and
the test asserts the recovered curve does too.

That leaves nothing unbuilt on my side. F-9b is scheduled into Week 6 and wants vLLM and
the 8 GB node; `fig03`'s deployment note is corrected in source but its committed PDF is
stale because rendering needs graphviz, which is not installed here; and the C-5
`served_by` amendment is still raised rather than made, which is correct for a contract
change.

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
   measurement. C-6's `clock_sync` is the second half of this and not a relaxation of the
   first: the offset it records is subtracted from nothing, and the only field the pipeline
   acts on is the clock *rate*, which is the one error that survives a difference of
   single-host durations. The new way to get this wrong is to see a measured offset in a
   manifest and conclude the residual can now be decomposed. It cannot: the wire timestamps
   are monotonic and have no shared epoch.
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
| 2 | Calibration campaign incl. `-ngl`/thread/slot sweep for the synthesizable *R* range (F-9a); **time-ordered C-3 snapshots**; τ and variance envelope | Remaining four policies against fixture cost models; StalenessVeil | **MPR-1 achieved.** C-3 frozen. |
| 3 | Admissible-set determination; validation-anchor runs at 3+ operating points | All five policies live from one config value; admission filter | Load band identified. **Feature freeze.** |
| 4 | Pipeline hardened; figure scripts | DES parameterised from Week-2 snapshots; F-23 validation | Simulator agrees within stated tolerance. |
| 5 | Figures for H1/H2/H3 | R × load × staleness × policy sweeps | Hypotheses tested. |
| 6 | Threats to validity, limitations; **F-9b engine-gap measurement** (moved from Week 2 — it bounds threat R9 and nothing else reads it, so it belongs beside the section that uses it) | Literature check (R-1), positioning | Report. |
