# UML Figure Set — Scheduling LLM Inference Under Uncalibrated Heterogeneity

Twelve figures covering the [frozen specification](../scheduling-requirements-spec.pdf). Each entry
gives a draft caption, the requirements it discharges, and the `\includegraphics` line
for LaTeX.

Sources are in [`src/`](src/). Rebuild with [`./render.sh`](render.sh), which fetches
PlantUML on first run and needs `java` and `graphviz`.

Use `out/pdf-notitle/` in the paper — the embedded title is stripped so it does not
compete with the LaTeX caption, and it is the only rendered set committed to the repo.
`out/pdf/` and `out/png/` carry the embedded title, are useful for slides and advisor
review, and are gitignored because they are ~6 MB and regenerable: run `./render.sh`.

---

## Figure 1 — Use Case View of the Measurement Testbed
`fig01_usecase`

> **Draft caption.** Actors and use cases of the testbed. Calibration and
> non-stationarity measurement (MPR-1) are reachable without any policy
> comparison; sweeps beyond the physical pool ratio are confined to the
> simulator and labelled as such.

Discharges: §10 ownership, §7 MPR ladder, F-9, F-24.

```latex
\includegraphics[width=\linewidth]{figures/fig01_usecase.pdf}
```

## Figure 2 — Component View: Control Plane, Data Path, and the Shared-Policy Boundary
`fig02_component`

> **Draft caption.** System decomposition. The scheduler is control-plane only:
> responses travel worker-to-client directly (F-11). The dispatch policy
> component is consumed unchanged by the discrete-event simulator (F-21).
> A single reused runtime backs every worker, so engine-internal batching is
> one mechanism rather than two, and it is never modified (F-5, F-9).

Discharges: F-1–F-5, F-9–F-12, F-21.

```latex
\includegraphics[width=\linewidth]{figures/fig02_component.pdf}
```

## Figure 3 — Deployment View: Heterogeneous Pool on a Trusted LAN
`fig03_deployment`

> **Draft caption.** Physical testbed. Every node runs the same runtime and the
> same quantization format (F-9), so the heterogeneity ratio $R$ is a property
> of hardware class — G1 (discrete GPU) through C1 (CPU only) — and of per-node
> offload and thread configuration, rather than of a difference in engine. That
> configurability is what makes $R$ tunable on physical hardware (F-9a) and what
> places the admissibility cliff. The single vLLM run sits outside the pool
> boundary: it is a measured condition rather than a pool member, run once to
> report the engine gap as a magnitude (F-9b). No duration crosses a host
> boundary: every process reports only durations it observed itself, joined
> offline on `req_id`. Host clocks are disciplined to one reference and the
> measured offsets recorded in C-6, which makes the machines' clock *rates*
> comparable without making the transport stage decomposable.

Discharges: F-9, F-9a, F-9b, F-10–F-13, F-18, §5.1; threat R9.

```latex
\includegraphics[width=\linewidth]{figures/fig03_deployment.pdf}
```

## Figure 4 — Class View: Scheduler Core and the Five-Policy Factorial
`fig04_class_scheduler`

> **Draft caption.** The five policies behind one dispatch interface. The four
> cells form the $2\times2$ factorial of hardware-awareness against
> queue-awareness; Threshold($T$) is the degenerate baseline against which H2
> predicts convergence at high $R$. Clock and randomness are injected, which is
> what permits the same object to run in the simulator.

Discharges: F-1–F-3, F-6–F-8, F-13, F-14; hypotheses H1 and H2.

```latex
\includegraphics[width=\linewidth]{figures/fig04_class_scheduler.pdf}
```

## Figure 5 — Class View: Measurement Harness and Results Pipeline
`fig05_class_harness`

> **Draft caption.** The Week-1 deliverable. Trace generation is offline and
> seeded; replay is open-loop with a send-lag assertion, so a slow pool cannot
> silently throttle offered load. A run manifest makes reproducibility from a
> single config plus a seed checkable rather than merely claimed.

Discharges: F-16–F-20.

```latex
\includegraphics[width=\linewidth]{figures/fig05_class_harness.pdf}
```

## Figure 6 — Class View: Discrete-Event Simulator and Validation
`fig06_class_simulator`

> **Draft caption.** The simulator holds a `DispatchPolicy` object rather than a
> copy, so any divergence from live routing is a difference in service time or
> state, never in policy. Service-time parameters derive from the Week-2
> calibration campaign including a noise term fitted to the observed variance
> envelope and autocorrelation time $\tau$. `SimNode.batch_capacity` models
> llama.cpp's fixed slot count exactly for every node in the pool, rather than
> approximating two different admission models. Sweeps run only after validation.

Discharges: F-21–F-24; threats R2 and R9.

```latex
\includegraphics[width=\linewidth]{figures/fig06_class_simulator.pdf}
```

## Figure 7 — Sequence: Request Lifecycle on Live Hardware
`fig07_seq_request`

> **Draft caption.** One request end to end. The capability view is read *as of*
> `now − s`, where $s$ is the injected staleness of H3. Per-stage durations are
> each measured on the machine that observed both endpoints.

Discharges: F-3, F-4, F-8, F-9, F-10, F-11, F-18.

```latex
\includegraphics[width=0.85\linewidth]{figures/fig07_seq_request.pdf}
```

## Figure 8 — Sequence: Experiment Execution and Simulator Validation
`fig08_seq_experiment`

> **Draft caption.** One experiment. An identical trace is replayed across all
> five policies and across the hardware–simulator boundary. Sweeps proceed only
> if p50 and p95 agree within the stated tolerance at three or more operating
> points; otherwise claims are restricted to the hardware-grounded range.

Discharges: F-17, F-19, F-20, F-22, F-23.

```latex
\includegraphics[width=0.9\linewidth]{figures/fig08_seq_experiment.pdf}
```

## Figure 9 — State Machine: Request Lifecycle
`fig09_state_request`

> **Draft caption.** Request states. Inadmissible requests leave the primary
> statistics and are counted separately in the cliff characterisation, so
> categorical failures under extreme heterogeneity do not degenerate the tail
> metrics. Warmup arrivals are discarded at the recording boundary.

Discharges: F-13–F-15; threat R4.

```latex
\includegraphics[width=0.6\linewidth]{figures/fig09_state_request.pdf}
```

## Figure 10 — State Machine: Worker Node and Its Capability Estimate
`fig10_state_node`

> **Draft caption.** Node lifecycle, with the scheduler-side freshness of that
> node's throughput estimate shown as a separate region. Freshness is what H3
> manipulates: the estimate ages toward the autocorrelation time $\tau$ of the
> node's actual throughput unless a heartbeat resets it.

Discharges: F-10; hypothesis H3; MPR-1.

```latex
\includegraphics[width=0.7\linewidth]{figures/fig10_state_node.pdf}
```

## Figure 11 — Activity: One Dispatch Decision
`fig11_activity_dispatch`

> **Draft caption.** The dispatch decision across client, scheduler, and worker.
> Admissibility is applied to every policy including the baselines, and all five
> branches share one interface, so policy is the only factor that varies across
> a comparison.

Discharges: F-1–F-4, F-14, F-15.

```latex
\includegraphics[width=\linewidth]{figures/fig11_activity_dispatch.pdf}
```

## Figure 12 — Package View: The Shared-Policy Invariant
`fig12_package`

> **Draft caption.** Module dependencies. `live` and `sim` both depend on
> `policies`; `policies` depends on neither and imports no clock, socket, or
> global RNG. A simulated result therefore travels the same code path as a
> hardware result.

Discharges: F-21; supports the F-23 validation argument.

```latex
\includegraphics[width=0.8\linewidth]{figures/fig12_package.pdf}
```

---

## Scope Change 001 — figure edits applied

The single-engine decision (F-9) required edits to Figures 2, 3, 6 and 12. Two further figures carried the
mixed-engine assumption and were updated for consistency; §8 does not mention them.

| Figure | Edit |
|---|---|
| 1 | Secondary actor `Inference Engine` was *vLLM / llama.cpp* → `llama.cpp · GGUF` **(not in §8)** |
| 2 | Worker tier collapsed to a single `«reused» llama.cpp engine · GGUF`; F-5 note strengthened — batching ownership is one mechanism, not two |
| 3 | All four nodes carry `llama.cpp · GGUF 7-8B` with per-node `-ngl` annotation (99 / 40 / 20 / 0, illustrative pending Week 2); node-D note replaced by a pool-wide note on F-9a; detached `«measured condition, not a pool member»` box added for the F-9b vLLM run |
| 6 | `SimNode` note added: `batch_capacity` is llama.cpp's slot model exactly, not an approximation absorbed into the F-23 tolerance |
| 7 | Participant `Engine` was *vLLM / llama.cpp* → `llama.cpp · GGUF` **(not in §8)** |
| 12 | `worker.py` annotated *one runtime, llama.cpp*; no dependency edges change, shared-policy invariant untouched |

Figures 4, 5, 8, 9, 10 and 11 are engine-agnostic and were not edited.

## Rebuilding

```bash
java -jar plantuml.jar -tsvg  -o ../out/svg src/fig*.puml
java -jar plantuml.jar -tpng  -Sdpi=200 -o ../out/png src/fig*.puml
```

All styling lives in `src/theme.iuml`; edit it once to restyle every figure.
Colour encoding: teal = system under study, amber = instrumentation,
grey = reused third-party runtime — including the single vLLM run of F-9b,
which is drawn outside the pool boundary in Figure 3.

To regenerate the title-free variants:

```bash
for f in src/fig*.puml; do sed '/^title Figure/d' "$f" > build/$(basename $f); done
```

## Directory layout

```
src/            .puml sources + theme.iuml
out/svg/        vector, with figure title
out/pdf/        vector, with figure title
out/svg-notitle/  vector, title stripped — for LaTeX
out/pdf-notitle/  vector, title stripped — for LaTeX
out/png/        200 dpi raster
out/uml_figures_all.pdf   all twelve, A4 landscape, for advisor review
```
