# Committed C-3 snapshot series

`runs/` is not versioned — traces regenerate byte-for-byte from `(config, seed)` and logs
are large and per-run. The **time-ordered** C-3 series is the exception, and it is here
rather than there because two things on the other side of the seam need it and neither
should require a copy of my machine:

* **F-22.** The simulator's service-time model is parameterized from measured hardware,
  including the stochastic component. That is `stochastic` in these files.
* **F-8 / H3.** Staleness injection serves the scheduler *the snapshot from `s` seconds
  ago*. That is a lookup into this series, and it only means anything if the series is
  genuinely a history — which is why each file carries its own `measured_at_unix` and a
  sequenced `snapshot_id`, and why the whole series is committed rather than the last one.

Each directory is one node class; files sort in measurement order. All of it was measured on
`llama.cpp b10569+p1` — the pinned build plus the `/completion` patch in
[`patches/`](../../patches), without which about 1 request in 100 came back as an HTTP 500
and never reached the table.

| Node class | Model | `engine_config` | Snapshots | τ | *r²* |
|---|---|---|---:|---:|---:|
| `cpu_ngl0_p4_q4km_llama3_8b` | `Meta-Llama-3-8B-Instruct` Q4_K_M | `ngl 0`, `threads 6`, `parallel 4` | 23 | **69.5 s** | **0.989** |
| `gtx1650ti_ngl20_p4_q4km_llama3_8b` | `Meta-Llama-3-8B-Instruct` Q4_K_M | `ngl 20`, `threads 6`, `parallel 4` | 18 | ≤ 32 s | 0.0 |
| `gtx1650ti_ngl99_p4_q4km_llama32_1b` | `Llama-3.2-1B-Instruct` Q4_K_M | `ngl 99`, `threads 6`, `parallel 4` | 9 + 9 | ≤ 5.0 s | 0.0 |


### Every entry carries its phase split

Each `entries[]` row now has `prefill_ms_mean` and `decode_ms_mean` alongside
`service_ms_mean`. Nothing was re-measured to get them: `prefill_ns` and `decode_ns` were
recorded per observation at calibration time and kept in each run's `observations.jsonl`, so
`tools/backfill_phase_split.py` summarised them onto all 59 committed snapshots.

They are written as a *share* of each snapshot's own `service_ms_mean` rather than as a raw
mean, because the sustained cell drifts across a series while the grid cells do not, and a
raw mean taken over the whole run would disagree with the service time sitting beside it in
the same entry. The measured ratio is stable where the absolute number is not.

Two things to know before using them.

**`prefill + decode` is less than `service`, on purpose.** The remainder is the engine's own
unattributed residual. Distributing it across the two phases would invent an attribution
llama.cpp never reported.

**Read the concurrency column first.** The grid holds `c` requests in flight by firing them
together, so at `c > 1` their prefills serialise against each other and the number carries
that contention: on this 1B class the same 128-token prompt reads 174 ms at one slot and
727 ms at four. Under the Poisson arrivals a trace actually produces, prefill is flat at
about 180 ms at every batch size, because a new request's prefill overlaps its neighbours'
*decode* rather than their prefill. The `c = 1` row is the uncontended prompt-evaluation
cost. The rest is a property of the calibration workload as much as of the hardware, and
`service_ms_mean` at `c > 1` inherits the same thing.

That last point is a known defect in this table, not a subtlety to work around. It has to be
corrected together with the stochastic refit in issue #13, because the two errors partly
cancel and fixing either alone moves the F-23 error rather than reducing it.

### The 1B class carries two series, measured a day apart, and the second one supersedes the first

`gtx1650ti_ngl99_p4_q4km_llama32_1b` holds **two campaigns**: nine snapshots from
2026-08-30 and nine from 2026-08-31. Both are the same node class — hardware and
`engine_config` are what define one (F-9a), and neither changed. What changed is the grid.

The first campaign sampled prompt 64 and output 32 as its bucket representatives, and
measured concurrency at 1 and 4 only. The traces this study replays use prompt 128, 256 and
512 with output 64 and 128, and the anchors spend a quarter of their time at two and three
slots. `uv run costcheck runs/anchors` priced the anchors against that first table and found
it wrong by a request-weighted **127%** — enough that no simulator parameterised from it
could pass F-23, for reasons that have nothing to do with the simulator.

The second campaign uses the trace's own lengths as the representatives, adds a prompt-bucket
edge at 256 so `[129, 512]` no longer averages a fourfold prefill range, and measures every
concurrency the pool can reach. Scored on the same anchors it reads **21.3%**, inside the
±25% tolerance, and **11.8%** on medians.

**Both are kept, and the older one is not stale data.** The four anchor manifests name its
snapshot ids, and rewriting what a run was deployed under would be worse than carrying two
series. Anything resolving a snapshot by id gets the right one either way, and
`load_series` orders by `measured_at_unix`, so the history stays a history.

**The two series have different bucket geometry**, which matters in exactly one place: F-8
serves the scheduler the snapshot from *s* seconds ago, and a lookup that straddled the
campaign boundary would see the table shape change. It cannot in practice — the boundary is
about 24 hours wide and the staleness axis is seconds — but read the series as two, not one.

**Newly measured for this class: batching buys no throughput here.** The F-18 split across
the anchors shows per-request decode falling as 1/c (143, 71, 58, 39 tok/s at one through
four slots), and prefill flat in concurrency under those arrivals (179 / 267 / 280 / 189 ms
at batch one through four for prompts under 128 tokens, and 702 / 751 / 756 / 707 for 257 to
512). That flatness is conditional on arrivals not being synchronised; see the phase-split
note above. Aggregate decode throughput is therefore constant: four concurrent requests take
about as long as four sequential ones. On this card a slot is worth less than a slot usually
is, and the concurrency effects the policies compete over are queueing effects rather than
throughput effects.

**One number moved in a direction worth noting.** The second campaign's sustained segment ran
immediately after the first and shows a much wider throughput envelope — 114.6–162.1 tok/s
against 153.6–162.4, CV 0.109 against 0.024 — so a single calibrated mean understates its own
standard error by **2.35×** rather than 1.00×. `fit_r2` is still 0.0, so this is a variance
observation and not a τ measurement: the ACF resolves no decay either time. The most likely
cause is thermal, the card having been under load for a quarter of an hour already, and it is
recorded here rather than smoothed away because a back-to-back campaign is exactly the
condition a long sweep will run under.

**Read `stochastic.autocorr_time_s` with the `r²` beside it.** Only the CPU class carries a
fitted τ. On the two GPU classes the ACF shows no decay at all, so the value is the window
the series was measured with — an *upper bound*, not an estimate, and `fit_r2 = 0.0` is how
you can tell from the file alone.

That difference is not an accident of effort. The instrument can only see a correlation time
longer than five times one request's service time (`bursts_per_window == window_s /
service_s`, floored at 5), so a node that takes seconds per request cannot resolve a drift of
seconds. The CPU class takes 9.6 s per request and drifts on a 70 s timescale, which is
comfortably visible; the 1B class takes 0.81 s and shows nothing above 3.5 s. **The pool's
non-stationarity lives on the CPU node**, which is the class its heterogeneity is built from.

Grid coverage differs per class and bounds what the admissible set can claim — the CPU class
was calibrated on one `(prompt, output)` bucket, which is why the 8B pool intersects to
`prompt ≤ 128, output ≤ 64`. See [`runs/admissible/`](../../runs/admissible) and
`docs/base_scope/week1-freeze-checklist.md`.
