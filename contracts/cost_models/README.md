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

Each directory is one node class. Files sort in measurement order.

| Node class | Snapshots | Model | Notes |
|---|---:|---|---|
| `gtx1650ti_ngl99_p4_q4km_llama32_1b` | 9 | `Llama-3.2-1B-Instruct` Q4_K_M | `-ngl 99`, `--threads 6`, `--parallel 4`. Grid cells carried forward from the calibration pass; the sustained cell re-fitted on a rolling 60 s window every 30 s. |

**The two `Meta-Llama-3-8B-Instruct` node classes are missing, and that is a measurement
result rather than an oversight.** C-3 requires `autocorr_time_s > 0`, and at 8B on this
hardware τ cannot be estimated: the node retires about 0.24 requests per second, so a window
holding the five completions an autocorrelation estimate needs is at least ~21 s wide, while
the ACF says τ is under 6 s — and one request takes 16–18 s, which is itself a floor on what
any window can resolve. Those bounds do not move with a longer run, because they scale with
the completion rate rather than the segment length. Writing a τ I did not measure is the one
outcome that would be worse than the gap, so the campaign writes its observations, says why,
and exits non-zero. See `docs/week1-freeze-checklist.md`.
