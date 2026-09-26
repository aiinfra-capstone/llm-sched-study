# System design

The instrument, and the properties of it that a result depends on. The scheduler here is
built to produce measurements, at the lowest fidelity that still supports the claims in
`research-plan.md`.

---

## 1. Shape

Two halves, one seam, six artifacts across it. One person owns each half, and both can run the
whole stack.

| Half | Language | Contents | Owner |
|---|---|---|---|
| `dataplane/` | Python | Worker wrapper and engine adapter, calibration campaign, trace generator, open-loop replay client, join pipeline, figures | D |
| `controlplane/` | Java | Live scheduler, the policies, admission, the staleness veil, the discrete-event simulator | A |
| `contracts/` | JSON Schema, protobuf | The six artifacts, their examples, and the committed cost-model snapshots | J |
| `tools/` | shell, Python | Machine survey, LAN bring-up, engine install and bench, campaign drivers, the analyses that are not console entry points | D |
| `fixtures/` | Python | Fake scheduler and fake worker, so neither half blocks on the other | J |

**Why the seam sits there.** The simulator must run the same policy implementations as the
live scheduler. Split those across two people and they drift, over tie-breaking or over
whether an in-flight request counts before or after admission, and that drift invalidates
validation silently because both systems still run and still produce plausible numbers. So one
person owns the policy code and both of its hosts, and everything else is arranged around
that.

## 2. The contracts

Nothing else crosses the seam. All six are validated in CI on every pull request by
`contracts/check.py`, which also validates an arbitrary file against the contract its name
implies.

| # | Artifact | Direction | Runtime coupling |
|---|---|---|---|
| C-1 | `scheduling.proto` | bidirectional | yes |
| C-2 | Trace file | harness to harness and simulator | no |
| C-3 | Cost model snapshot | data plane to control plane | yes |
| C-4 | Log records, client and scheduler and worker | both to pipeline | no |
| C-5 | Joined record | pipeline to figures | no |
| C-6 | Run manifest | launcher to everything | no |

## 3. One run, end to end

1. The campaign driver writes `manifest.pre.json`: run id, policy, staleness, the pool, which
   C-3 snapshot prices each node, the seed for this repeat, and the load target.
2. It starts one `LiveSchedulerApp` for this run, with one `--worker node_id=host:port` per
   node. One scheduler process serves exactly one run, because the scheduler takes its run id
   and log file from the manifest.
3. The replay client generates or verifies the trace against its SHA-256, materialises every
   prompt before `t0`, and fires open-loop on the trace's own schedule, asserting its send lag
   per request.
4. For each request the scheduler applies the staleness veil, the admission filter and the
   policy, writes the decision record with every candidate's state as the policy saw it,
   forwards `Worker.Execute` to the chosen node, and bumps its own admission counters.
5. The worker holds a semaphore of exactly `--parallel` permits, so queueing is measured in
   the wrapper rather than inside the engine. It calls `/completion` with the prompt as token
   ids, forced output length, `cache_prompt: false` and temperature 0, delivers the response
   directly to the client, and reports completion to the scheduler on its own RPC.
6. The driver settles, stops the scheduler, pulls each node's worker log, and writes the
   post-run `manifest.json`. Only that file, never `manifest.pre.json`, is discovered by the
   run set builder, so a run that dies halfway leaves nothing that looks like a data point.
7. The pipeline joins the three logs into C-5 records, which is a pure function of the
   manifest and the logs: no network, no engine.

## 4. The policies

All eight names are selectable from one config value, in both vehicles, with no code change
between runs (`com.sched.core.policies.Policies`).

| Name | Score or rule | Knows queue | Knows hardware |
|---|---|---|---|
| `round_robin` | rotation | no | no |
| `jsq` | `queueDepth + inflight`, ties broken uniformly | yes | no |
| `jsq_fastfirst` | as `jsq`, ties broken toward the highest capability | yes | ranking only |
| `static_weighted` | one weighted random draw on capability | no | magnitude |
| `static_weighted_wrr` | deterministic weighted round-robin on capability | no | magnitude |
| `wjsq` | `(pending + 1) / capability` | yes | magnitude |
| `threshold` | round-robin over nodes with capability at least T | no | ranking and a cutoff |
| `ect` | predicted completion time from the C-3 cell for this request's prompt, output and the node's concurrency, plus its queue drain | yes | the full cost model |

`jsq_fastfirst` is the control that separates the ranking bit from the magnitude, and `ect` is
the strongest calibrated baseline. Together they are what make the value-of-calibration curve
a measurement rather than a comparison against a straw policy.

**Capability** is one number per node: output tokens per second of total service time, read
from the C-3 cell at the lowest prompt and output bucket at concurrency 1
(`com.sched.core.Capability`), shared by both vehicles so they cannot drift on what it means.
It is a one-slot number, and on the first pair it is about half the ratio the pool operates
at, which is why the believed ratio is an experimental axis rather than a constant.

**Tie-breaking** draws one uniform value, recorded on the decision record as
`tie_break_draw`. A comparator cannot do this: comparing one captured scalar against itself
returns 0 for every pair, and the first node in the list absorbs every tie.

## 5. The two vehicles

| | Hardware | Simulator |
|---|---|---|
| Arrivals | Open-loop replay client on a real clock | Events at the trace's offsets |
| Service | The engine | C-3 lookup by (prompt bucket, output bucket, concurrency), times one lognormal multiplier per request, `exp(σZ - σ²/2)` with σ from the snapshot's `stochastic.sigma`, independent across requests and with no autocorrelation (K6 resolves no drift, so `autocorr_time_s` is not read) |
| Concurrency effects | Real | `reevaluateActive` rescales the decode remainder when the batch changes; prefill passes through |
| Queue state | The scheduler's own admit and complete counters, aged by the veil | The same, from `SimNodeServer` |
| Transport | Real, and asymmetric by node | One additive per-node term from the manifest |
| Randomness | Hardware, plus the scheduler seed | Separate streams for policy draws, per-node service noise and transport |

Both run the same `Policy`, `Capability`, `AdmissionFilter` and `StalenessVeil` classes. The
simulator writes the same three C-4 logs, so the pipeline and every figure treat a simulated
run like a measured one, distinguished by the `vehicle` field.

## 6. Invariants

These are what the measurements rest on. Breaking one silently is how a measurement study
produces confident nonsense, so each has a check beside it.

| Invariant | Enforced by |
|---|---|
| No duration crosses a host boundary | Every span is stamped on one machine's monotonic clock; the leftover is one honest residual, never decomposed |
| Clock discipline is recorded, not assumed | `clocksync` writes each host's method, offset, dispersion and rate error into every manifest; rate is what matters, and offset is subtracted from nothing |
| Load generation stays open-loop | The client never waits for a response, asserts send lag per request, and a breach invalidates the run |
| A trace is identified by its SHA-256, which covers (config, seed, generator commit) | `gen_trace` prints it, every manifest records the generator sha, regeneration at that sha is byte-identical, and the replay refuses to start unless the file still hashes to it |
| The engine does not move | One tag, one commit, one patch, one quantisation, pinned context, recorded per node; the driver checks engine identity before and after every run and counts an unreadable engine as fatal |
| Output length is an independent variable | `n_predict` plus `ignore_eos`, so service time never measures the model's stopping behaviour |
| Service time does not depend on trace order | `cache_prompt: false` on every request, `--cache-ram 0` on the engine |
| A dispatch sees every admission before it | The live scheduler reads queue state, decides and records the admission under one lock, and forwards to the worker only after; a forward that fails is rolled back. Completions and heartbeats take the same lock. Checked by firing 400 concurrent dispatches at a fixture-mode scheduler, where every decision must see exactly the admissions before it |
| Capability is the calibrated number, not a live signal | The heartbeat refreshes queue depth and inflight only; taking the live throughput EWMA would weight the calibrated policies by a queue signal |
| One scheduler process per run | The run id, policy, staleness and log file come from the manifest at startup |
| A missing cost-model cell refuses | The simulator throws rather than substituting a fabricated service time |

## 7. Known deviations and limits

Written down because each one bounds what a result can mean.

**Simulator.**
- Service times are drawn from a static table, so nothing in it drifts. There is no online
  capability estimate and no counterfactual regret, which is why H3 is out of paper one.
- The veil ages queue counts only. Capability never ages.
- `jsq` and `wjsq` score on `queueDepth() + inflight()`, so dispatch sees the size of the
  in-flight set but not its composition. Exposed through `batch_size_at_admission`.
- Sweeps set load either as a rate scale or as a pool utilisation (`pool_utilisation` in the
  sweep grid). On a rate-scale axis pool utilisation rises with R, so a rise-and-fall in R
  read off it can come from saturation alone; the utilisation axis holds load fixed across R.
- Validation so far is single-node, in sample, and on absolute latency. The contrast criterion
  is in `analysis-plan.md` section 6.6.

**Live path.**
- `dispatch` reads queue state, decides and records the admission under `stateLock`, and
  forwards `Worker.Execute` only after, so two near-simultaneous dispatches each see the
  other's admission. A forward that fails is rolled back under the same lock.
- Transport is asymmetric on the first pair: 5 to 7 ms to the co-located node, 9 to 16 ms to
  the node over Wi-Fi. The simulator now takes a per-node term.
- The harness shares a host with one pool node.

**Contract.** The C-4 description of `estimate_age_ms` refers to a staleness ceiling in a
section the frozen spec does not have. There is no ceiling implemented, and the reference is
from an earlier draft.

## 8. What is versioned

`runs/**` is ignored except for the things another person has to be able to open: the run
manifest for every run, the admissible set, the load band, and the per-run-set `summary.json`
that every number in `results.md` cites. Traces are regenerable byte for byte from (config,
seed) at the generator sha each manifest records, and logs are large and per-run. The time-ordered C-3 snapshot series lives under
`contracts/cost_models/`, so parameterising the simulator does not require a copy of one
laptop.

## 9. Reproducing a result

1. `uv run contracts/check.py` for the six artifacts.
2. `cd dataplane && uv sync --all-groups && uv run pytest` for the suite.
3. A run's manifest carries the seed, the trace SHA-256, the git shas of all four components,
   the engine identity and command line, the clock discipline, and the snapshot that priced
   each node. Everything else is derivable from those.
4. `uv run pipeline`, `uv run costcheck`, `uv run runset`, then
   `tools/campaign_summary.py` for the summary the write-up cites.
