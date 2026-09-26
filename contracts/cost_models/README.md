# Committed C-3 snapshot series

`runs/` is not versioned: traces regenerate byte-for-byte from `(config, seed)` at the
generator sha each manifest records, and logs are large and per-run. The **time-ordered** C-3 series is the exception, and it is here
rather than there because two things on the other side of the seam need it and neither
should require a copy of my machine:

* **F-22.** The simulator's service-time model is parameterized from measured hardware,
  including the stochastic component. That is `stochastic` in these files.
* **Provenance.** Every run manifest names the snapshot each node was served by, so a
  committed run can be priced by exactly that file. Each file carries its own
  `measured_at_unix` and a sequenced `snapshot_id`. The series was first kept for H3, where
  staleness injection (F-8) would serve *the snapshot from `s` seconds ago*; H3 is out of
  paper one, and the series stays for M12.

Each directory is one node class. A directory can hold more than one calibration campaign,
and each campaign numbers its files from `000`, so file names sort by index and not by time;
`load_series` orders by `measured_at_unix`. All of it was measured on
`llama.cpp b10569+p1` — the pinned build plus the `/completion` patch in
[`patches/`](../../patches), without which about 1 request in 100 came back as an HTTP 500
and never reached the table.

| Node class | Model | `engine_config` | Snapshots | τ as recorded | *r²* | Resolved |
|---|---|---|---:|---:|---:|---|
| `cpu_ngl0_p4_q4km_llama3_8b` | `Meta-Llama-3-8B-Instruct` Q4_K_M | `ngl 0`, `threads 6`, `parallel 4` | 23 | 69.5 s | 0.989 | no (interval 31 to 91 s) |
| `gtx1650ti_ngl20_p4_q4km_llama3_8b` | `Meta-Llama-3-8B-Instruct` Q4_K_M | `ngl 20`, `threads 6`, `parallel 4` | 18 | 32 s (window) | 0.0 | no |
| `gtx1650ti_ngl99_p4_q4km_llama32_1b` | `Llama-3.2-1B-Instruct` Q4_K_M | `ngl 99`, `threads 6`, `parallel 4` | 9 + 9 + 9 | 5.0 s, 5.0 s, 5.17 s | 0.0, 0.0, 0.784 | no (5 s floor) |
| `rtx3050_ngl99_p4_q4km_llama32_1b` | `Llama-3.2-1B-Instruct` Q4_K_M | `ngl 99`, `threads 6`, `parallel 4` | 9 | 5.0 s | 0.0 | no (5 s floor) |

77 snapshots in all. τ is resolved on no class (`docs/results.md`, K6).


### A thin cell says so

A cell is fitted only from the samples served at the concurrency it claims. When none were
(the batch was draining for every sample), the cell is still fitted, from all of its samples,
and the entry carries `thin: true`, since its service time is then biased low. `n_samples`
counts the samples fitted. `thin` is optional in C-3: the committed snapshots predate it and
leave it out.


### Every entry carries its phase split

Each `entries[]` row now has `prefill_ms_mean` and `decode_ms_mean` alongside
`service_ms_mean`. Nothing was re-measured to get them: `prefill_ns` and `decode_ns` were
recorded per observation at calibration time and kept in each run's `observations.jsonl`, so
`tools/backfill_phase_split.py` summarised them onto the snapshots that predate the split, and
`build_snapshot` writes it itself from then on. All 77 committed snapshots carry it.

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
cost, and the rest is a property of the calibration workload as much as of the hardware.

**That does not make `service_ms_mean` at `c > 1` wrong, and we checked rather than assumed.**
Reconstructing service as `prefill(c=1) + decode(c) + residual(c)` comes out 20 to 40% below
the anchors, where the table as it stands is accurate to within 1.7 to 7.4% at four slots. A
new prefill still slows the decodes running beside it; the engine's flat per-request
`prefill_ns` is a fact about attribution, not about total cost. Use the split to reason about
which part of a running request should stretch when the batch changes, which is what the
simulator does, and not to rebuild the service time.

### The 1B class carries three series

`gtx1650ti_ngl99_p4_q4km_llama32_1b` holds **three campaigns** of nine snapshots each, from
2026-08-30, 2026-08-31 and 2026-09-17. All three are the same node class, since hardware and
`engine_config` are what define one (F-9a). Between the first two the grid changed, as below.
The third is the recalibration on the rebuilt engine (G2), which the current campaign configs
name; the committed runs were served by the second.

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

**The first two series have different bucket geometry**, so read them as separate series,
not one. Every run names its snapshot by id, so nothing looks a snapshot up across the
boundary.

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

**Read `stochastic.autocorr_time_s` as a characterisation, not a parameter.** The simulator
does not read it; its noise is one i.i.d. lognormal multiplier per request from
`stochastic.sigma`. τ is resolved on no class. On the GPU classes the ACF shows no decay, or
decays inside the first window, so the value is the 5 s floor or the window the series was
measured with: an *upper bound*, not an estimate. The CPU class has a fitted 69.5 s with
`r² = 0.989`, but its own calibration record says `tau_resolved: false`, and the
block-bootstrap interval is 31 to 91 s (`tools/tau_interval.py`). Every snapshot carries
`tau_resolved` and `tau_censored` in the `stochastic` block, and C-3 requires both, so a floor
cannot be read as a measurement. The 77 committed snapshots predate the flags, so we copied
them from each snapshot's calibration run (`campaign.json`, the `stationarity` record), after
checking that the run's τ and `r²` match the ones in the snapshot. `build_snapshot` writes
them itself from now on.

That difference is not an accident of effort. The instrument can only see a correlation time
longer than five times one request's service time (`bursts_per_window == window_s /
service_s`, floored at 5), so a node that takes seconds per request cannot resolve a drift of
seconds. The CPU class takes 9.6 s per request, so a drift on a 70 s timescale is inside
what it could see, but 31 windows over a segment 21 τ long do not resolve it; the 1B class
takes 0.81 s and shows nothing above 3.5 s.

Grid coverage differs per class and bounds what the admissible set can claim — the CPU class
was calibrated on one `(prompt, output)` bucket, which is why the 8B pool intersects to
`prompt ≤ 128, output ≤ 64`. See [`runs/admissible/`](../../runs/admissible) and
[`docs/results.md`](../../docs/results.md).
