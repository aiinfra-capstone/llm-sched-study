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
| `gtx1650ti_ngl99_p4_q4km_llama32_1b` | `Llama-3.2-1B-Instruct` Q4_K_M | `ngl 99`, `threads 6`, `parallel 4` | 9 | ≤ 3.5 s | 0.0 |

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
`docs/week1-freeze-checklist.md`.
