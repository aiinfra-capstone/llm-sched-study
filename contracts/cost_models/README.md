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

**The two `Meta-Llama-3-8B-Instruct` node classes are missing, and it is a measurement gap
rather than an oversight.** C-3 requires `autocorr_time_s > 0`, and τ was not obtained at 8B:
the node retires about 0.24 requests per second, so a window holding the five completions an
autocorrelation estimate needs is ~21 s wide, and the Week-2 segments used 6 s and 30 s
windows — one starved, the other too coarse to hold 30 of them. Writing a τ I did not measure
is the one outcome worse than the gap, so the campaign wrote its observations, said why, and
exited non-zero.

The fix is a **shorter sustained cell**, not a longer run: both floors scale with completion
rate, which is set by service time, so a cell of short requests at full concurrency lowers
them together. See `docs/week1-freeze-checklist.md` for the arithmetic and the fallback if
that still does not resolve τ.
