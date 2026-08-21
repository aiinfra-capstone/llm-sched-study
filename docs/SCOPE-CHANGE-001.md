# Scope Change 001 — Single Inference Engine

**Status:** Accepted
**Raised:** Week 1 (inside the freeze window; §6 feature freeze is end of Week 3, §4 changes after Week 1 require explicit re-scoping)
**Affects:** §0, §4.4, §5.1, §5.3, §6, §7, §8, §10; Figures 3, 6, 12
**Supersedes:** F-9 as frozen

---

## 1. Change

The worker tier runs a **single inference runtime — llama.cpp with GGUF quantization — on
every node, GPU and CPU alike.** The mixed vLLM-on-GPU / llama.cpp-on-CPU pool described
in the frozen F-9 is withdrawn.

vLLM is retained as **one measured condition**, not as a pool member.

## 2. Rationale

**Primary: engine was a confound on the study's central independent variable.**
§5.3 defines the heterogeneity ratio $R$ as the ratio of maximum to minimum per-node
throughput. In a mixed pool that ratio is a compound of three simultaneous differences —
hardware class, engine (PagedAttention vs. a fixed-slot server), and quantization format
(AWQ vs. GGUF). H1 and H2 are statements about how routing benefit varies with *hardware*
heterogeneity; neither the 2×2 decomposition nor the $R$-sweep can separate an engine
effect from a hardware effect. Holding engine and quantization constant makes $R$ a
property of hardware and configuration alone. This is the same reasoning already applied
to prefix caching (disabled), output length (forced exact), and batching ownership
(§4.2) — the mixed pool was the one remaining uncontrolled confound.

**Secondary: $R$ becomes tunable on physical hardware.**
A single engine exposes capability as configuration — GPU offload fraction (`-ngl`),
thread count, and slot count (`--parallel`). The pool's $R$ can therefore be varied on
real machines rather than only in simulation. §0's constraint 1 is weakened accordingly,
and threat R2 (simulator validated only at low $R$) is materially reduced.

**Tertiary: the simulator's node model becomes exact rather than approximate.**
`SimNode.batch_capacity: int` (Figure 6) is llama.cpp's slot model precisely — a fixed
number of parallel sequences, each holding a fixed KV share. It is not vLLM's model,
where admission is governed by dynamic paged KV occupancy and any integer capacity is
emergent. Under the frozen F-9 the simulator modelled half the pool faithfully and half
crudely, and the discrepancy would have surfaced as validation error absorbed into the
F-23 tolerance. Uniform llama.cpp removes the asymmetry.

**Operational:** one wrapper, one metrics shape, one quantization pipeline, one build
path. §6 records no slack; two runtime integrations in Week 1 was the largest single
consumer of a budget that has none.

## 3. Cost accepted

Absolute GPU throughput is below what vLLM would deliver, so the pool is not
representative of production GPU serving. This is a limit on external validity, not on
internal validity, and it is measured rather than assumed — see F-9b and threat R9 below.

---

## 4. Replacement text — §4.4 Worker & Transport

> **F-9.** Workers MUST wrap a single inference runtime — **llama.cpp with GGUF
> quantization** — on every node, GPU and CPU alike. No engine internals are
> reimplemented. Engine and quantization format are held constant so that the
> heterogeneity ratio $R$ is a property of hardware and node configuration alone. A mixed
> engine pool would confound $R$ with an engine effect of unknown magnitude that §2's
> hypotheses cannot decompose.
>
> **F-9a.** Per-node capability MUST be adjustable through runtime parameters of that
> single engine — GPU offload fraction (`-ngl`), thread count, and slot count
> (`--parallel`) — so that $R$ is tunable on physical hardware and not solely in
> simulation. Throttling MUST be applied to distinct physical machines. Multiple logical
> nodes MUST NOT be co-located on one host, because co-located nodes contend for PCIe,
> memory bandwidth, and cache, which would reintroduce as contention the confound this
> change removes.
>
> **F-9b.** The engine effect MUST be measured once and reported as a magnitude rather
> than assumed away. vLLM (AWQ, same model class) is run on the strongest node at one
> operating point against an identical replayed trace, and the observed throughput ratio
> to llama.cpp on that same node is reported as a stated bound on external validity. This
> is a measured condition, not a pool member: vLLM participates in no policy comparison
> and appears in no figure other than the engine-gap result.

F-10 through F-12 are unchanged.

## 5. Replacement text — §0, constraint 1

> **1. The simulator is the primary vehicle for breadth.** Physical hardware can span
> part of the heterogeneity axis, because a single engine exposes per-node capability as
> configuration (F-9a); the physical pool therefore contributes more than two or three
> points on $R$. It cannot reach node counts beyond five, controlled staleness, or $R$
> beyond what configuration and the available machines can jointly synthesize. Hardware's
> job remains to measure what the simulator must not assume.

## 6. Amendments — §5.3, §6, §7, §10

**§5.3, row "Heterogeneity ratio $R$":** vehicle becomes
`Simulator (hardware: multiple points via F-9a)`. The synthesizable hardware range is
determined in Week 2 and reported; the simulator covers $R$ beyond it.

**§6, Week 1:** exit criterion reads *worker wrapper (llama.cpp)* — one wrapper, not two.

**§6, Week 2:** the calibration campaign additionally sweeps `-ngl` / thread count /
`--parallel` per machine to establish the synthesizable $R$ range, and executes the F-9b
engine-gap measurement at one operating point.

**§7, MPR-2:** "at whatever heterogeneity ratios the physical pool provides" now means
the *synthesized* range under F-9a, which should be reported as a range rather than a
single figure.

**§10, Member A:** primary responsibility becomes *worker wrapper (llama.cpp), capability
throttling and the cost-model calibration campaign, and the F-9b engine-gap measurement.*

## 7. New threat — §8

| ID | Threat | Response |
|----|--------|----------|
| R9 | A single non-production engine limits external validity: results may not transfer to pools running a paged-KV GPU server | Engine held constant deliberately, to isolate hardware heterogeneity (F-9). The engine gap is measured once on the strongest node and reported as a magnitude (F-9b), converting an uncontrolled confound into a stated bound. |

**R2** gains a sentence: the hardware-validated range of $R$ is wider than the frozen
spec assumed, because capability throttling (F-9a) synthesizes intermediate node classes
on real machines.

---

## 8. Figure edits

PlantUML sources are not in this working copy; apply these to `src/` and rebuild per the
FIGURES.md instructions.

**`fig03_deployment.puml`**
- node-A, node-B: `«reused» vLLM · AWQ 7-8B` → `«reused» llama.cpp · GGUF 7-8B`
- All four nodes carry the same engine stereotype; add per-node config annotation, e.g.
  `-ngl 99`, `-ngl 40`, `-ngl 20`, `-ngl 0` (final values from Week 2).
- Replace the node-D note ("Deliberately weak member…") with a note covering the pool:
  *Engine and quantization are constant across every node. $R$ is set by hardware class
  and by per-node offload and thread configuration (F-9a), which is what makes it tunable
  on physical hardware.*
- Optional: a detached box marked `«measured condition, not a pool member»` for the
  single vLLM run (F-9b), clearly outside the pool boundary.

**`fig06_class_simulator.puml`**
- Amend the `SimNode` note: `batch_capacity` now models llama.cpp's fixed slot count
  exactly, for every node in the pool, rather than approximating two different admission
  models.

**`fig12_package.puml`**
- `live` package: `worker.py` gains the comment *one runtime, llama.cpp*. No dependency
  edges change; the shared-policy invariant is untouched.

**`fig02_component.puml`**
- The worker tier currently shows `«reused» vLLM engine` and `«reused» llama.cpp engine`
  side by side. Collapse to a single `«reused» llama.cpp engine`. The F-5 note is
  unchanged and is in fact strengthened: batching ownership is now one mechanism, not
  two.

**`FIGURES.md`** — captions for Figures 2 and 3 updated in this commit.
