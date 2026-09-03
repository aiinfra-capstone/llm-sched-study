# Elevation 1: workplan

Seven workstreams, September 3 to October 3. Ownership follows the base-scope
[split](../base_scope/two-person-split-and-interface-contract.md): Aditya owns the
scheduler, the five policies, the discrete-event simulator and F-23; Divyansh owns the
worker, the calibration campaign, the trace generator, the replay client, the join pipeline
and the figures. MPR-2 is joint.

The one-time exception in which Divyansh edited `controlplane/` directly, on 2026-09-03 to
land the policy and sampler fixes, does not carry forward.

## Order of unblocking

W1 and W2 run in parallel and together gate W3. W4 needs neither and can start immediately.
W5 needs W4. W6 and W7 are independent of the hardware entirely.

```
W1 scheduler dispatch ─┐
                       ├─> W3 MPR-2 on hardware
W2 second machine ─────┘

W4 sweep runner ─> W5 workload profiles

W6 stochastic refit      (independent)
W7 run length            (independent)
```

---

## W1. Close the scheduler dispatch gap

**Aditya. Critical path. Issue #14.**

The live scheduler decides and logs but never calls `Worker.Execute`. Four changes, all in
`controlplane/src/main/java/com/sched/live/`:

1. `LiveSchedulerApp` accepts repeatable `--worker <node_id>=<host:port>` and a `--port`,
   replacing the hardcoded port.
2. Replace the hardcoded admissibility for `fake-node-A` with bounds loaded from the C-3
   snapshots the manifest names. `SimApp` already has this loop.
3. `SchedulerGrpcService` holds a cached channel per node and, after logging the decision,
   calls `Worker.Execute` on the chosen node.
4. Put the node id in `DispatchAck.chosen_node`, not the endpoint, so the join keys line up.

No contract change. `DispatchRequest` already carries every field `ExecuteRequest` needs;
the scheduler adds `decision_seq`, which it already owns.

**Verified by** running the real scheduler with two `--worker` endpoints against two local
workers, before any LAN work. A `scheduler_*.jsonl` appears with a decision record per
request, `uv run pipeline` fills `chosen_node` and `routing_error_ms` for the first time in
the project, and `uv run figures --only node-utilization` stops raising. That smoke test is
co-located and therefore `valid: false` by F-9a, which is correct.

---

## W2. Second machine and a real R

**Divyansh, with joint bring-up.**

The pool is homogeneous in engine, model and quantisation, so the second node runs the same
Llama-3.2-1B Q4_K_M at `ngl 0`. Heterogeneity comes from `ngl`, which is F-9a. This also
matches what `pool-install.sh` fetches.

1. On the new box: `tools/survey.sh`, then `tools/pool-install.sh --role node --backend cpu
   --reference <harness ip>`, then `tools/lan-up.sh --join --ip ...` and `--open node`. None
   of these has run against a remote host before, so budget for breakage; all are idempotent.
2. Calibrate `cpu_ngl0_p4_q4km_llama32_1b` on that box using
   `dataplane/configs/calibration_1b_cpu.json`, whose grid edges and sampled cells are
   byte-identical to the GPU 1B grid. That is load-bearing: the R estimator refuses to
   compute a ratio when two classes share no common cell, and mismatched grids already cost
   this study 20% of its ratio once.
3. `uv run r-range`. `r_max_deployable` comes off 1.00 the moment two classes report
   different host strings.
4. `uv run preflight dataplane/configs/preflight_lan.json` once the hostnames are filled in.

Three gaps to close while cabling, all in the data plane:

- Nothing ever passes `clock_sync` into a manifest. The builder accepts the keyword but
  neither the anchors driver nor the replay client has a parameter for it, so every
  multi-host run would record an unevidenced clock claim.
- `replay --nodes` never counts co-location and writes `colocated_nodes: 0, valid: true`
  even for a co-located pool. The anchors driver has the logic.
- `Validity` has no clock field, so a two-host run with divergent clocks is still valid.
  Decide whether that becomes a counter or stays a warning.

**Verified by** `uv run preflight` exiting 0 with two hosts, and `uv run r-range` reporting
`r_max_deployable` above 1 with `deployable_pair` non-null and `n_hosts: 2`.

---

## W3. MPR-2 on real hardware

**Joint. Needs W1 and W2.**

The four-policy decomposition across the real two-node pool at the three usable operating
points. The anchors driver is reused unchanged, with a policy loop around it rather than a
rewrite: it already orders points by rate scale, settles between them, names runs
consistently and builds the C-6 manifest.

**Verified by** `load_band.json` reporting `policy_separable: true`, and the
`h1-decomposition` and `mpr2-range` figures rendering. Those raise on a missing cell of the
two-by-two, so a partial result fails loudly.

---

## W4. The sweeps

**Aditya. MPR-3.**

`runs/sweeps` is empty and no sweep runner exists anywhere; the only multi-run driver is the
anchors driver, and the one directory under `runs/sweeps` is a historical capacity probe.
The runner should reuse the anchors loop shape and then additionally aggregate to a runset,
which the anchors driver does not do today. Grid: policy by R by staleness by load.

One decision to make explicitly rather than by default. We have three measured node classes,
so a sweep over R cannot come from measurement; it has to come from scaling a measured C-3
snapshot. Synthesised snapshots need their own ids and a provenance note, and every figure
drawn from them has to say so.

---

## W5. Workload-shape profiles

**Divyansh. The elevation. Needs W4.**

Three trace profiles at prompt-to-output ratios of 13.76, 2.00 and 0.50, run through the
sweep across policies and R. Built and validated:

- `dataplane/configs/trace_summarisation_1b.json`
- `dataplane/configs/trace_balanced_1b.json`
- `dataplane/configs/trace_generation_1b.json`

600 requests over 666 s at lambda 0.9, the same as the anchors. They share a `gen_seed`, so
arrival offsets and priorities are byte-identical across the three and only the lengths
move; the comparison is paired. Mean predicted service time varies by 2.1% across them, so
offered load is held constant while the ratio swings 27x. Section 7 of
[`evidence.md`](evidence.md) has the construction.

`tools/phase_ratio.py` is the analysis. It reports R on service, on prefill and on decode
per shared cost-model cell, and, given trace configs, the R each profile sees weighted by
its bucket mix. It defaults to concurrency 1 and warns above it, because our calibration
grid fires requests together and the measured prefill at higher concurrency carries harness
contention rather than hardware.

Running it today against the two 8B classes returns one cell and no profile can be priced,
because those classes were calibrated at a single grid point. That is the concrete reason
W2 calibrates the CPU node on the full 24-cell grid: it is what makes this table exist.

The prediction under test is that queue depth substitutes for calibration on prompt-heavy
work and fails on decode-heavy work.

Trace identity comes from each run's manifest rather than from a number written down here,
because the generator stamps its git sha inside the hashed header and the sha256 therefore
moves with every commit.

---

## W6. Refit the stochastic component

**Divyansh. Issue #13.**

Sigma per concurrency, additive to the C-3 `stochastic` block, with the sampler selecting by
concurrency. The distribution family does not change. This is the last honest source of F-23
error.

It must land together with the calibration-synchronisation correction described in
[`evidence.md`](evidence.md) section 2, not before it. The two errors partly cancel, so
fixing one alone moves the error rather than reducing it.

**Verified by** F-23 error shrinking at the quiet anchor with nothing fitted to the gate,
and the determinism test still passing.

**This one adds code inside the 100% coverage gate**, so it needs tests written
deliberately. Flag it rather than widening the test surface quietly.

---

## W7. Run length

**Divyansh. Cheap.**

Lengthen the trace for the saturated point so its p95 and p99 are defensible, or state that
it is a transient observation and exclude it from steady-state claims. The load-band tool
already flags it; the choice is which of the two honest options we take.

---

## Schedule

| Week | Aditya | Divyansh |
|---|---|---|
| Sep 3-10 | W1 scheduler dispatch | W2 cable, calibrate CPU 1B, R; the three data-plane gaps |
| Sep 10-17 | W4 sweep runner, first sweeps | W6 stochastic refit; W7 run length |
| Sep 17-24 | Sweeps across R, load and staleness | W3 MPR-2 runs (joint); W5 workload profiles |
| Sep 24-Oct 3 | F-23 revalidation on the new snapshots | Figures from real data; threats to validity for the writeup pair |

F-9b, the vLLM engine gap, stays a bounded probe at the end rather than a headline. It is a
4 GB card and vLLM is marginal there; if it fails, that is reported as a limitation.

## Standing checks

`uv run contracts/check.py`, the data-plane suite at 100% coverage, and both CI workflows
green. Any new C-3 or C-6 field has to keep the Java bindings and the schemas consistent,
which the contract checker enforces in both directions.
