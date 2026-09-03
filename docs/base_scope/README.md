# Base scope

This folder is the frozen record of the study as originally specified and built, Weeks 1
through 4. Nothing in here is superseded by the elevation, and the specification is still
the authority for every F-requirement, contract and hypothesis the code cites.

What changed in September 2026 is that we added scope on top, after a scrutiny pass on the
finished Week-4 system. That addition lives in [`../elevation-1/`](../elevation-1/) and is
written as a delta. It restates nothing from here, because a second document that repeats
the specification goes stale within a cycle and a stale one is worse than none.

| File | What it is |
|---|---|
| [`scheduling-requirements-spec.pdf`](scheduling-requirements-spec.pdf) | Scope, hypotheses, F-1 to F-24, non-goals, threats to validity, the MPR ladder. The authority. Edited off-machine as a `.docx` and exported here; there is deliberately no Markdown mirror. |
| [`two-person-split-and-interface-contract.md`](two-person-split-and-interface-contract.md) | Where the seam between the two halves sits, the six artifacts that cross it, and the failure modes to watch. Ownership in the elevation still follows this. |
| [`week1-freeze-checklist.md`](week1-freeze-checklist.md) | What had to be true before the contract froze, and the running record of every week since. |

The UML figure set stays at [`../uml/`](../uml/) rather than moving in here. It is live
tooling with its own `render.sh` and committed output paths, and the figures describe a
system that both scopes share.

## What the base scope achieved

MPR-1 is done: throughput non-stationarity characterised on consumer serving nodes, with
tau = 69.5 s on the CPU class at r-squared 0.989 and nothing measurable on either GPU class.
The instrument limit is part of the result, since a node whose tau is shorter than about
five service times cannot show its own drift.

The contracts C-1 through C-6 are frozen and machine-checked. The simulator validates
against hardware at four operating points. Seven hundred and four data-plane tests pass at
100% coverage.

## What it did not achieve, and why the elevation exists

MPR-2 and MPR-3 are both open. The pool ran on one physical machine, so deployable
heterogeneity sat at 1.00x against a specification that asked for 10x to 100x, and no
parameter sweeps had been run. Those are the first two things the elevation closes.
