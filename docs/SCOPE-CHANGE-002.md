# Scope Change 002 — Priority Resolved as a Passthrough Label

**Status:** Accepted. **Not yet merged into the specification** — the replacement text in
§3 below must be applied to `scheduling-requirements-spec.docx` and the PDF re-exported.
**Raised:** Week 1 (§5 change; §4 and §5 changes after Week 1 require explicit re-scoping
against §6)
**Affects:** §5.4; F-16 (interpretation only, no requirement withdrawn)
**Supersedes:** §13 of the two-person split & interface contract, which flagged this as
the one inconsistency to resolve before the freeze.

---

## 1. Change

`priority` becomes a **passthrough label**. It is generated, it travels in the trace and
in every log record, and **no policy reads it**. The dependent variable *"high-priority
p99 under low-priority load"* is withdrawn from §5.4.

No field is removed from any schema. F-16's configurable priority mix is retained.

## 2. Rationale

The specification currently requires three things that cannot all hold:

- **F-16** requires a configurable priority mix in the generated trace.
- **§5.4** lists *"high-priority p99 under low-priority load"* as a dependent variable.
- **§4.1** defines five policies, **none of which is priority-aware**. Priority tiers were
  dropped from the 6-week scope relative to the earlier pitch.

The consequence is not merely untidy. If no policy reads `priority`, and the trace draws
priority independently of the length bucket — which it does, from a separate RNG stream
(§8.2 of the split doc) — then high-priority requests are statistically indistinguishable
from low-priority ones. The §5.4 metric would equal overall p99 in every condition, in
every run, **by construction rather than by measurement**. It is a quantity that cannot
carry information, and reporting it invites the question of what it was for.

Two alternatives were considered and rejected:

- **Report it as a descriptive observation** — "here is what happens to interactive
  requests when nothing protects them." Coherent, and requires no new policy, but the
  observation is predictable in advance. It reads as a limitation rather than a finding.
- **Correlate priority with request length** (interactive → short, background → long).
  This is the only variant in which the metric becomes informative: under a priority-blind
  policy, short requests queue behind long ones, and the metric measures head-of-line
  blocking, which genuinely differs between `RoundRobin` and `JSQ`/`WJSQ`. Rejected on
  cost, not on merit — it widens the request space, interacts with admissible-set
  determination in Week 3, and adds Week-5 analysis to a timeline §6 records as having no
  slack. Retained in §9 as future work.

**The label is kept rather than removed** because carrying it costs nothing, it keeps the
trace format stable if a follow-on project adds a priority-aware policy, and removing it
would require a trace-schema version bump for no benefit.

## 3. Replacement text — §5.4 Dependent Variables

> End-to-end latency p50/p95/p99; queue wait; per-node utilization; routing-error rate
> (dispatches where an alternative node would have completed materially sooner).
>
> `priority` is generated (F-16) and carried through the trace and every log record, but
> no policy in §4.1 reads it, and no priority-conditioned metric is reported. It is a
> passthrough label, retained so that the trace format remains stable for future work
> (§9). A priority-conditioned metric would be uninformative here: with priority drawn
> independently of request length and no policy acting on it, high-priority latency is
> equal to overall latency by construction.

## 4. Amendment — §9 Deferred (Post-Window)

Add to the deferred list:

> **Priority-aware dispatch, and priority correlated with request length.** Coupling the
> priority draw to the length bucket would make head-of-line blocking measurable and
> policy-dependent; adding a priority-aware policy would make it controllable. Both are
> out of the 6-week window.

## 5. Amendment — F-16 (interpretation, no text change)

F-16's configurable priority mix stands. It is now explicitly a *generator* capability
whose output is a label, not an input to any scheduling decision.

---

## 6. What this touches in the repository

| Artifact | Change |
|---|---|
| `contracts/schemas/trace.schema.json` | `priority` / `priority_mix` descriptions state the resolution instead of pointing at an open question. |
| `contracts/schemas/joined_record.schema.json` | Same, on the `priority` column. |
| `docs/week1-freeze-checklist.md` | §13 open decision closed. |
| `docs/two-person-split-and-interface-contract.md` | §13 marked resolved. |

No schema version bump: no field was added, removed, or retyped.
