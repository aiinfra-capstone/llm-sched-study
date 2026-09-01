# Simulator Deviations

- **F-4 (Partial)**: JSQ and WJSQ both score on `queueDepth() + inflight()`. Dispatch considers the depth of the in-flight set, but not its true composition (how long the in-flight requests are). This is a known deviation for minimum fidelity, exposed via `batch_size_at_admission`.
- **§12.4 Reference**: The C-4 schema description for `estimate_age_ms` references a "staleness ceiling above which a node is treated as unavailable (§12.4)". The frozen spec has no §12.4, and there is no staleness ceiling implemented. This is a dangling reference from an earlier draft and does not reflect the current simulator design.
