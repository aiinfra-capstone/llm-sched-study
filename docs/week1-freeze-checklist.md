# Week-1 Freeze Checklist

The [requirements spec](requirements-spec.md) is frozen, and now incorporates
[SCOPE-CHANGE-001](SCOPE-CHANGE-001.md) (single inference engine) as F-9 / F-9a / F-9b.
**The interface contract (C-1 – C-6) freezes at end of Week 1.** After that, changes require the same re-scoping ritual as the spec:
state the change, state what it costs against the §6 timeline, both people agree, one PR.

This checklist is the gate. Nothing here is optional, and the last section is an open
decision that must be closed *before* the freeze, not after.

---

## Divyansh Shukla (A)

- [ ] `scheduling.proto` reviewed and agreed with Aditya (C-1)
- [ ] **One** worker wrapper — llama.cpp + GGUF — running on a node (F-9, per
      SCOPE-CHANGE-001; the second runtime integration is withdrawn from Week 1)
- [ ] `engine_config` (`-ngl`, `--threads`, `--parallel`) plumbed into the manifest's
      node block, because under F-9a this *is* the experimental condition
- [ ] Launcher asserts `validity.colocated_nodes == 0` — one logical node per physical
      host, or the contention confound comes straight back
- [ ] Trace generator produces a **byte-identical file** on regeneration from the same
      seed and parameters — as a **test**, not an assumption
      - [ ] `arrival_offset_s` serialized at exactly 4 decimal places
      - [ ] Three independent RNG streams from one `SeedSequence` (arrival / length / content)
      - [ ] SHA-256 of the trace recorded and used as its identity downstream
- [ ] Replay client runs against the **fake scheduler**, open-loop, with `send_lag_ms`
      asserted per request
- [ ] Prompt materializer: `(content_seed, prompt_len, vocab_size)` → exact token
      sequence, deterministic, and agreed with the generator
- [ ] C-4 client and worker log schemas + fixture files committed
- [ ] `uv run contracts/check.py` green

## Aditya Gupta (B)

- [ ] `scheduling.proto` reviewed and agreed with Divyansh (C-1)
- [ ] `SimNode.batch_capacity` reads `manifest.nodes[].engine_config.parallel` — under
      SCOPE-CHANGE-001 llama.cpp's slot count *is* the node model, exactly, so no
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

**Resolution: passthrough label only.** See
[SCOPE-CHANGE-002](SCOPE-CHANGE-002.md) for the full record.

`priority` is generated (F-16) and carried through the trace and every log record, but no
policy reads it, and *"high-priority p99 under low-priority load"* is withdrawn from
§5.4's dependent variables.

The reason it had to go rather than simply be left alone: with priority drawn
independently of the length bucket and no policy acting on it, high-priority latency
equals overall latency **by construction**. The metric could not have carried information
in any condition, in any run.

- [x] Resolution chosen: **passthrough label only** (SCOPE-CHANGE-002)
- [x] Decided by: Divyansh Shukla — 2026-08-22
- [x] Schema comments updated to state the chosen resolution
- [ ] **Replacement text applied to the spec `.docx` and the PDF re-exported**
      — §5.4 and §9, per SCOPE-CHANGE-002 §3 and §4. *This is the one step left.*

### Also decide by end of Week 2 (not Week 1, but do not forget)

- [ ] **C-3 `form`** — F-7 permits a lookup table *or* a ≤6-parameter regression.
      Pick one and commit. Supporting both doubles Aditya's interpolation logic for no
      research gain.
- [ ] **Synthesizable *R* range** — sweep `-ngl` / `--threads` / `--parallel` per machine
      and establish what range of *R* the physical pool can actually reach (F-9a).
      Report it as a **range**, not a single figure (§7, MPR-2). This is new work that
      SCOPE-CHANGE-001 adds to Week 2, and it is what buys the reduction in threat R2.
- [ ] **F-9b engine-gap measurement** — vLLM vs llama.cpp on the strongest node, one
      operating point, identical replayed trace. One number, reported as a bound on
      external validity (threat R9). Do not let this slip past Week 2: it is the
      evidence that the single-engine decision was accounted for rather than hidden.

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
