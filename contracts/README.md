# Contracts — the six frozen artifacts

A and B interact through exactly six things. **Nothing else crosses the seam.**

All six freeze at **end of Week 1**. Changes after that require the same re-scoping
ritual as the requirements spec: state the change, state what it costs in the §6
timeline, and get both people to agree before merging.

| # | Artifact | Direction | Where |
|---|---|---|---|
| C-1 | Wire schemas | bidirectional | [`scheduling.proto`](scheduling.proto) |
| C-2 | Trace file | A → A (B reads for DES) | [`schemas/trace.schema.json`](schemas/trace.schema.json) |
| C-3 | Cost model snapshot | A → B | [`schemas/cost_model.schema.json`](schemas/cost_model.schema.json) |
| C-4 | Log record schemas | A and B → pipeline | [`schemas/log_client.schema.json`](schemas/log_client.schema.json) · [`log_scheduler`](schemas/log_scheduler.schema.json) · [`log_worker`](schemas/log_worker.schema.json) |
| C-5 | Joined record | pipeline → figures | [`schemas/joined_record.schema.json`](schemas/joined_record.schema.json) |
| C-6 | Run manifest | launcher → everything | [`schemas/manifest.schema.json`](schemas/manifest.schema.json) |

C-1 and C-3 are the only true *runtime* couplings. C-2 and C-4–C-6 are file formats,
which means they can be validated offline against fixture files — and should be, before
either side is finished.

## Additive changes since the freeze

Three fields were added in September 2026, during elevation 1. All three are **optional**,
all three are absent on every artifact written before them, and in every case absent means
*not measured* rather than zero. A reader that does not know about them is unaffected, which
is why these went in as additions rather than through the re-scoping ritual above. Anything
that changes or removes an existing field still needs that ritual.

| Contract | Field | Why |
|---|---|---|
| C-3 | `entries[].prefill_ms_mean`, `entries[].decode_ms_mean` | Prefill and decode do not answer to concurrency the same way, and a consumer holding only `service_ms_mean` cannot tell them apart. The simulator needs the split to avoid stretching a request's prompt evaluation when the batch changes around it. |
| C-6 | `transport_overhead` | What a client pays on top of the engine's own span in this environment, as `{mean_ms, sd_ms, n_samples, source, measured_from}`. Measured per environment, never assumed. |

Two things a reader of these fields has to know:

* `prefill_ms_mean + decode_ms_mean` is deliberately **less than** `service_ms_mean`. The
  difference is the engine's own unattributed residual, and splitting it across the two
  phases would invent an attribution the backend never reported.
* `prefill_ms_mean` at concurrency above 1 carries contention between simultaneous prefills
  that the calibration harness creates by firing its requests together. A Poisson workload
  does not synchronise arrivals that way. The `c = 1` row is the uncontended cost; the rest
  describes the harness as much as the hardware. The field's own schema description says so.

Both are documented where they are produced: C-3 in
[`cost_models/README.md`](cost_models/README.md), C-6 in
[`../docs/elevation-1/evidence.md`](../docs/elevation-1/evidence.md).

## Checking conformance

```bash
uv run contracts/check.py
```

Validates every file in `examples/` against its schema and compiles `scheduling.proto`.
CI runs this on every PR. If you add a field, add it to the example in the same commit —
an example that no longer exercises the schema is how drift gets in.

## The three rules that are easy to violate

1. **Clock discipline.** Every timestamp on the wire is labelled with its originating
   host and is **never subtracted across hosts**. Wire timestamps exist for gap
   detection and debugging. Every duration in the analysis comes from a single
   machine's monotonic clock. `transport_residual_ms` in C-5 is named *residual*
   precisely so nobody mistakes it for a measurement.

   C-6's optional `clock_sync` block does not weaken this. It records what each host's
   time daemon reported at LAN setup, and the only field the pipeline acts on is
   `rate_error_ppm`: a clock ticking r ppm fast inflates that host's durations by r ppm,
   multiplicatively, so unlike an offset it does not cancel in the residual. The offset
   is recorded and subtracted from nothing. Absent means not measured, never zero.

2. **Version gates reject, they do not default.** `trace_schema` and
   `cost_model_schema` are integers, and loaders must fail loudly on an unknown value.
   The failure this prevents: A learns something in Week 2, adds a field, and B's DES
   quietly reads garbage.

3. **C-3 is a time-ordered *series*, not a final fitted model.** Staleness injection
   (F-8) serves the scheduler a snapshot from *s* seconds ago. If A emits only one
   snapshot, B has to synthesize aging by perturbing parameters — which turns H3 into a
   study of B's perturbation model rather than of real drift.

## Regenerating gRPC stubs

Stubs are **not** committed (see `.gitignore`); generate them into your own tree:

```bash
# Python (Divyansh, A)
uv run --with grpcio-tools python -m grpc_tools.protoc \
  -Icontracts --python_out=. --grpc_python_out=. contracts/scheduling.proto
```
